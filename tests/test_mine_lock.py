"""Hourly mine lock tests (mocked responses only: no network, no real key)."""
import importlib.util, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_ADDRESS", "poa1testaddressnotreal")
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_minelock", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)


class _FakeServer:
    """Stands in for poa.http_request (the single HTTP choke point) and records every call."""
    def __init__(self): self.calls = []
    def __call__(self, method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        self.calls.append(url)
        if "/hosted/mine" in url:
            return json.dumps({"ok": True, "swept": 0}).encode()
        return json.dumps({"ok": True}).encode()
    def mine_calls(self): return [u for u in self.calls if "/hosted/mine" in u]


def _no_real_network(*a, **k):
    raise AssertionError("test tried to reach the real network")


class MineLockTests(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self._orig = poa.POA_DIR
        poa.POA_DIR = self.home
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("POA_MINE_HOURLY_LOCK", None)

    def tearDown(self):
        self._env.stop()
        poa.POA_DIR = self._orig

    def _server(self):
        fake = _FakeServer()
        self._patches = [mock.patch.object(poa, "http_request", side_effect=fake),
                         mock.patch("urllib.request.urlopen", side_effect=_no_real_network),
                         mock.patch.object(poa, "mcp_fallback_enabled", return_value=False)]
        for p in self._patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        return fake

    def test_first_call_hits_server_second_blocked_offline(self):
        srv = self._server()
        r1 = poa.safe_mine()
        self.assertEqual(len(srv.mine_calls()), 1)
        self.assertNotEqual(r1.get("kind"), "already-mined-this-hour")
        n = len(srv.calls)
        r2 = poa.safe_mine()
        self.assertEqual(len(srv.calls), n, "second mine in the same hour must make zero network calls")
        self.assertEqual(r2["kind"], "already-mined-this-hour")
        self.assertTrue(r2["skipped"])
        locks = [p.name for p in (self.home / "mine-locks").iterdir()]
        self.assertEqual(len(locks), 1)
        self.assertRegex(locks[0], r"^mined-\d{10}$")  # hour only: no address or key in the name
        self.assertNotIn("poa1", locks[0])

    def test_env_var_disables_lock(self):
        os.environ["POA_MINE_HOURLY_LOCK"] = "0"
        srv = self._server()
        poa.safe_mine()
        r2 = poa.safe_mine()
        self.assertEqual(len(srv.mine_calls()), 2)
        self.assertNotEqual(r2.get("kind"), "already-mined-this-hour")
        self.assertFalse((self.home / "mine-locks").exists())

    def test_old_day_locks_are_pruned(self):
        d = self.home / "mine-locks"
        d.mkdir(parents=True)
        (d / "mined-2000010100").touch()
        self._server()
        poa.safe_mine()
        self.assertFalse((d / "mined-2000010100").exists())


if __name__ == "__main__":
    unittest.main()
