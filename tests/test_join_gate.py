"""Join gate pause handling (SUGGESTIONS #168). Mocked responses only: http_request, the call path under
api_call and _register_once, is patched in every test, so nothing reaches the live server."""
import argparse, contextlib, importlib.util, io, json, pathlib, unittest, urllib.error
from unittest import mock

spec = importlib.util.spec_from_file_location("poa_jg", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

PAUSE_BODY = json.dumps({"error": "paused", "notice": {"msg": "This endpoint is temporarily paused. Your balances, landed answers and pending grades are safe. Retry in a few minutes.", "until": "retry in a few minutes"}})


def http_503(body=PAUSE_BODY):
    def fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(body.encode()))
    return fake


def http_ok(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
    return json.dumps({"challenge": "c1", "kind": "pow", "bits": 4, "required": True}).encode()


def http_timeout(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
    raise TimeoutError("timed out")


class JoinGateTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(poa, "_backoff", lambda a: None)
        p.start(); self.addCleanup(p.stop)

    def test_503_is_paused_on_first_answer(self):
        fake = mock.Mock(side_effect=http_503())
        with mock.patch.object(poa, "http_request", fake):
            g = poa.probe_join_gate(retries=3)
        self.assertEqual(g["gate"], "paused")
        self.assertEqual(fake.call_count, 1)
        self.assertIn("temporarily paused", g["notice"])
        self.assertEqual(g["until"], "retry in a few minutes")
        self.assertFalse(g["until_is_timestamp"])

    def test_plain_text_503_and_timestamp_until(self):
        with mock.patch.object(poa, "http_request", http_503("temporarily paused")):
            self.assertEqual(poa.probe_join_gate()["gate"], "paused")
        body = json.dumps({"notice": {"msg": "paused", "until": "2026-11-18T20:00:00Z"}})
        with mock.patch.object(poa, "http_request", http_503(body)):
            self.assertTrue(poa.probe_join_gate()["until_is_timestamp"])

    def test_200_is_open(self):
        with mock.patch.object(poa, "http_request", http_ok):
            g = poa.probe_join_gate()
        self.assertEqual(g["gate"], "open")
        self.assertEqual(g["challenge_info"]["challenge"], "c1")

    def test_timeout_is_unknown_not_closed(self):
        fake = mock.Mock(side_effect=http_timeout)
        with mock.patch.object(poa, "http_request", fake):
            g = poa.probe_join_gate(retries=2)
        self.assertEqual(g["gate"], "unknown")
        self.assertEqual(fake.call_count, 2)

    def test_register_stops_before_mining_on_pause(self):
        solve = mock.Mock()
        reg = mock.Mock()
        with mock.patch.object(poa, "http_request", http_503()), mock.patch.object(poa, "_register_once", reg):
            with self.assertRaises(poa.JoinGateClosed) as cm:
                poa.register_with_pow({"label": "x"}, solve=solve, log=lambda *a, **k: None)
        solve.assert_not_called()
        reg.assert_not_called()
        self.assertIn("will not help", str(cm.exception))

    def test_register_post_503_paused_is_closed_gate(self):
        calls = []
        def fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            calls.append((method, url))
            if url.endswith("/api/join-pow"):
                return http_ok(method, url)
            return http_503()(method, url)
        with mock.patch.object(poa, "http_request", fake):
            with self.assertRaises(poa.JoinGateClosed):
                poa.register_with_pow({"label": "x"}, log=lambda *a, **k: None)
        self.assertEqual([m for m, _ in calls], ["GET", "POST"])

    def test_gate_command_output(self):
        with mock.patch.object(poa, "http_request", http_503()):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_join_pow(argparse.Namespace(solve=False, gate=True))
        out = json.loads(buf.getvalue())
        self.assertEqual(out["gate"], "paused")
        self.assertIn("advice", out)
        with mock.patch.object(poa, "http_request", http_ok):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_join_pow(argparse.Namespace(solve=False, gate=True))
        out = json.loads(buf.getvalue())
        self.assertEqual((out["gate"], out["bits"]), ("open", 4))
        self.assertNotIn("challenge_info", out)

    def test_plain_join_pow_on_pause_raises_closed(self):
        with mock.patch.object(poa, "http_request", http_503()):
            with self.assertRaises(poa.JoinGateClosed):
                poa.cmd_join_pow(argparse.Namespace(solve=False))


if __name__ == "__main__":
    unittest.main()
