"""Mocked tests for the /api/epochs index shape (server v0.110-edge-epochs-1130): read_epochs()
fetches only the newest close bodies from /api/epochs/<n>, checks each against the index sha256,
and passes the legacy full-record shape through unchanged (no network, no real key).
Run: python3 -m unittest tests/test_epochs_index.py"""
import hashlib, importlib.util, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ei", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


def body(n):
    return json.dumps({"epoch": n, "ts": 1794000000 + n * 21600, "basis": {"pending": {}}, "rewards": {"poa1x": "1.0"}}).encode()


BODIES = {n: body(n) for n in range(10)}


def index(bad=None):
    ents = [{"n": n, "sha256": hashlib.sha256(b).hexdigest() if n != bad else "0" * 64, "bytes": len(b),
             "close_ts": 0, "key": f"epochs/poa-mainnet-1/{n}.json"} for n, b in BODIES.items()]
    return {"chain_id": "poa-mainnet-1", "v": 1, "closed_through": 9, "count": 10, "epochs": ents}


class EpochsIndexTest(unittest.TestCase):
    def setUp(self):
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        return BODIES[int(url.rsplit("/", 1)[1])]

    def test_detects_index_not_legacy(self):
        self.assertTrue(poa.is_epochs_index(index()))
        self.assertFalse(poa.is_epochs_index({"epochs": [json.loads(BODIES[1])]}))
        self.assertFalse(poa.is_epochs_index([json.loads(BODIES[1])]))

    def test_expands_newest_only_and_verifies(self):
        r = poa.expand_epochs_index(index(), last=3, fetch=self.fetch)
        self.assertEqual([e["epoch"] for e in r["epochs"]], [7, 8, 9])
        self.assertEqual(r["_index"]["fetched"], [7, 8, 9])
        self.assertTrue(r["_index"]["sha256_verified"])
        self.assertTrue(all(u.endswith(f"/api/epochs/{n}") for u, n in zip(self.urls, (7, 8, 9))))
        self.assertFalse(any("key=" in u for u in self.urls))

    def test_hash_mismatch_fails_closed(self):
        with self.assertRaises(ValueError):
            poa.expand_epochs_index(index(bad=9), last=2, fetch=self.fetch)

    def test_wrong_epoch_body_fails_closed(self):
        with self.assertRaises(ValueError):
            poa.fetch_epoch_body(5, None, fetch=lambda url: BODIES[4])

    def test_legacy_passthrough(self):
        legacy = {"epochs": [json.loads(BODIES[1])]}
        self.assertIs(poa.expand_epochs_index(legacy, fetch=self.fetch), legacy)
        self.assertEqual(self.urls, [])

    def test_read_epochs_feeds_epoch_list(self):
        with mock.patch.object(poa, "api_call", return_value=index()), \
             mock.patch.object(poa, "http_request", side_effect=lambda m, url, **kw: self.fetch(url)):
            r = poa.read_epochs(timeout=5, retries=0, last=2)
        self.assertEqual([e["epoch"] for e in poa._epoch_list(r)], [8, 9])
        n, pend = poa._latest_pending_basis(r)
        self.assertEqual(n, 9)

    def test_default_last_env(self):
        with mock.patch.dict(os.environ, {"POA_EPOCHS_LAST": "4"}):
            r = poa.expand_epochs_index(index(), fetch=self.fetch)
        self.assertEqual(len(r["epochs"]), 4)


if __name__ == "__main__":
    unittest.main()
