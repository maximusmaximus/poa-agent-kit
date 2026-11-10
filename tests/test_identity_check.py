"""Mocked tests for `poa.py identity-check` (SUGGESTIONS.md #97).
No network, no real key: every read is injected or patched on the real call path."""
import importlib.util, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_idc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERADDRESSyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
TX = "ab" * 32
SEED = "572df43da7288306" + "0" * 48
WORDS = ["bridge", "atlas", "pearl"]


def me(addr=ME, vault=None, label="web"):
    return {"address": addr, "vault_pub": vault, "label": label, "next_nonce": 7, "as_of": "2026-11-10T18:00:00Z"}


def ch(epoch=66, answered=True, txid=TX, seed=SEED, words=WORDS, superseded=False):
    return {"epoch": epoch, "answered": answered, "answered_txid": txid if answered else None,
            "seed": seed, "words": words, "superseded": superseded}


def cache(**kw):
    c = {"sent_at": "2026-11-10T17:00:00Z", "txid_answer": TX, "seed_at_send": SEED, "words": WORDS}
    c.update(kw)
    return c


def no_net(*a, **k):
    raise AssertionError("network call attempted in a mocked test")


class IdentityCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "identity-baseline.json"
        p = mock.patch.object(poa, "http_request", side_effect=no_net)
        p.start()
        self.addCleanup(p.stop)

    def run_ic(self, m=None, fm=None, c=None, cc=None, **kw):
        def raise_or(v):
            def f():
                if isinstance(v, Exception):
                    raise v
                return v
            return f
        return poa.identity_check(baseline_path=self.tmp, fetch_me=raise_or(m if m is not None else me()),
                                  fetch_forum_me=raise_or(fm if fm is not None else {"publicId": "pub_abc"}),
                                  fetch_challenge=raise_or(c if c is not None else ch()),
                                  read_cache=lambda ep: cc if cc is not None else cache(),
                                  configured=kw.pop("configured", ME), forum=kw.pop("forum", True), **kw)

    def test_record_then_match(self):
        r = self.run_ic(record=True)
        self.assertTrue(r["recorded"])
        self.assertEqual(r["verdict"], "ok")
        r = self.run_ic()
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(set(r["identity"].values()) - {"match"}, set())
        self.assertEqual(r["challenge"]["answer_ref"], "match")
        self.assertEqual(r["challenge"]["seed"], "match")

    def test_never_prints_address_or_values(self):
        self.run_ic(record=True)
        r = self.run_ic(m=me(addr=OTHER))
        blob = json.dumps(r) + self.tmp.read_text()
        self.assertNotIn(ME, blob)
        self.assertNotIn(OTHER, blob)
        self.assertNotIn("pub_abc", blob)
        self.assertNotIn("pk_test_fake", blob)

    def test_503_then_wrong_identity_then_right(self):
        self.run_ic(record=True)
        r = self.run_ic(m=poa.PoaHTTPError("halted", "503", status=503))
        self.assertEqual(r["reachable"]["/api/hosted/me"], "halted")
        self.assertEqual(r["verdict"], "unreadable")
        r = self.run_ic(m=me(addr=OTHER))
        self.assertEqual(r["verdict"], "incident")
        self.assertEqual(r["identity"]["configured_address"], "mismatch")
        self.assertEqual(r["identity"]["address"], "changed")
        r = self.run_ic()
        self.assertEqual(r["verdict"], "ok")

    def test_vault_and_label_flip_with_same_address(self):
        self.run_ic(record=True)
        r = self.run_ic(m=me(vault="VAULTPUBxyz", label=None))
        self.assertEqual(r["identity"]["address"], "match")
        self.assertEqual(r["identity"]["vault_pub"], "changed")
        self.assertEqual(r["identity"]["label"], "changed")
        self.assertEqual(r["verdict"], "incident")

    def test_forum_public_id_changed(self):
        self.run_ic(record=True)
        r = self.run_ic(fm={"user": {"publicId": "pub_minted_new"}})
        self.assertIn("public_id", r["incidents"])

    def test_record_refused_during_incident(self):
        self.run_ic(record=True)
        before = self.tmp.read_text()
        r = self.run_ic(m=me(addr=OTHER), record=True)
        self.assertFalse(r["recorded"])
        self.assertEqual(self.tmp.read_text(), before)

    def test_token_expired_never_renews(self):
        r = self.run_ic(fm=poa.LabLeakTokenExpired("expired"), record=True)
        self.assertEqual(r["reachable"]["/lableak/api/me"], "token-expired")
        self.assertEqual(r["identity"]["public_id"], "unknown (not read)")

    def test_answer_txid_changed_while_answered_stays_true(self):
        r = self.run_ic(c=ch(txid="cd" * 32), record=True)
        self.assertEqual(r["challenge"]["answer"], "present")
        self.assertEqual(r["challenge"]["answer_ref"], "changed")
        self.assertEqual(r["verdict"], "incident")

    def test_missing_seed_reports_unknown_not_match(self):
        r = self.run_ic(cc=cache(seed_at_send=None), record=True)
        self.assertEqual(r["challenge"]["seed"], "unknown")

    def test_one_changed_word_fails(self):
        r = self.run_ic(c=ch(words=["bridge", "atlas", "pebble"]), record=True)
        self.assertEqual(r["challenge"]["seed"], "mismatch")

    def test_superseded_seed_is_not_an_incident(self):
        r = self.run_ic(c=ch(seed="9dbd29c0de4f94ea" + "1" * 48, superseded=True), record=True)
        self.assertTrue(r["challenge"]["seed"].startswith("superseded"))
        self.assertEqual(r["verdict"], "ok")

    def test_real_call_path_patched(self):
        """Default fetchers go through api_call/lableak_call; patch those, not the network."""
        with mock.patch.object(poa, "api_call", side_effect=lambda m, p, **k: me() if p == "/api/hosted/me" else ch()) as ac, \
             mock.patch.object(poa, "lableak_call", return_value={"publicId": "pub_abc"}) as lc, \
             mock.patch.object(poa, "load_epoch_cache", return_value=cache()):
            r = poa.identity_check(baseline_path=self.tmp, configured=ME, forum=True, record=True)
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual([c.args[1] for c in ac.call_args_list], ["/api/hosted/me", "/api/hosted/challenge"])
        lc.assert_called_once_with("GET", "/api/me", need_auth=True)


if __name__ == "__main__":
    unittest.main()
