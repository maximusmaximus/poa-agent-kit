"""Mocked tests for server release v1.12-gradecap (global grade ceiling grade_rpm_global).
No network, no real key, no real transactions. Run: python3 -m unittest tests/test_gradecap.py"""
import importlib.util, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_gc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "cd" * 32


def cap_err(ra=30):
    return poa.PoaHTTPError("server", 'HTTP 429 (server) on /api/hosted/grade: {"error":"grade_rpm_global"}', 429, retry_after=ra)


class GradeCapTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake(self, writes):
        writes = list(writes)

        def call(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            self.calls.append(path)
            if path == "/api/hosted/me":
                return {"next_nonce": 3}
            if path == "/api/hosted/assignments":
                return {"assignments": [{"answer_txid": ("%02x" % i) * 32} for i in range(3)]}
            r = writes.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return call

    def test_rate_limited_grade_is_not_landed_and_skips_read_back(self):
        with mock.patch.object(poa, "api_call", self.fake([cap_err(30)])):
            r = poa.submit_grade_safely(TX, 2, "reason")
        self.assertEqual((r["landed"], r["kind"], r["retry_after"]), (False, "rate-limited", 30))
        self.assertEqual(self.calls.count("/api/hosted/assignments"), 0)

    def test_body_marker_without_429_is_detected(self):
        e = poa.PoaHTTPError("server", "HTTP 503 on /api/hosted/grade: grade_rpm_global exceeded", 503)
        self.assertTrue(poa.is_grade_rate_limited(e))
        self.assertFalse(poa.is_grade_rate_limited(poa.PoaHTTPError("server", "HTTP 502 bad gateway", 502)))

    def test_default_wait_when_no_retry_after(self):
        with mock.patch.object(poa, "api_call", self.fake([cap_err(None)])):
            r = poa.submit_grade_safely(TX, 2, "reason")
        self.assertEqual(r["retry_after"], poa.GRADE_RATE_LIMIT_DEFAULT_WAIT_S)

    def test_grade_batch_stops_on_rate_limit(self):
        items = [{"txid": ("%02x" % i) * 32, "score": 2, "note": "n"} for i in range(3)]
        f = pathlib.Path(tempfile.mkdtemp()) / "g.json"; f.write_text(json.dumps(items))
        with mock.patch.object(poa, "api_call", self.fake([cap_err(45), {"txid": TX}, {"txid": TX}])), \
                mock.patch("sys.stdout"):
            try:
                poa.cmd_grade_batch(mock.Mock(file=str(f), mine=False))
            except SystemExit:
                pass
        self.assertEqual(self.calls.count("/api/hosted/grade"), 1)  # seats 2 and 3 left for the next run


if __name__ == "__main__":
    unittest.main()
