"""Mocked tests for MCP queued receipts (G0 notice 2026-10-29-mcp-queue, release v0.64.1-mcpdocs):
MCP poa_answer / poa_grade may return {job_id, poll_url} instead of the result. That is accepted;
poll it, never resend. No network, no real key. Run: python3 -m unittest tests/test_mcp_queue.py"""
import importlib.util, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_mcpq", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "ef" * 32
JOB = "0123456789abcdef0123456789abcdef"


class SameOriginPollUrlTests(unittest.TestCase):
    def test_absolute_same_origin_accepted(self):
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"):
            self.assertEqual(poa.busy_poll_path({"poll_url": f"https://minepoa.com/api/job/{JOB}"}), f"/api/job/{JOB}")
            self.assertEqual(poa.busy_poll_path({"poll_url": f"https://MINEPOA.com/api/job/{JOB}"}), f"/api/job/{JOB}")

    def test_absolute_other_origin_or_extras_refused(self):
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"):
            for bad in (f"https://evil.example/api/job/{JOB}", f"http://minepoa.com/api/job/{JOB}",
                        f"https://minepoa.com.evil.example/api/job/{JOB}", f"https://minepoa.com:8443/api/job/{JOB}",
                        f"https://minepoa.com/api/job/{JOB}?key=x", f"https://u@minepoa.com/api/job/{JOB}",
                        "https://minepoa.com/api/hosted/me"):
                with self.assertRaises(poa.PoaHTTPError):
                    poa.busy_poll_path({"poll_url": bad})


class McpQueueTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.polls, self.mcp_sends = [], []
        self.p = [mock.patch.object(poa, "POA_DIR", pathlib.Path(self.home)),
                  mock.patch.object(poa.time, "sleep", lambda s: None),
                  mock.patch.object(poa, "_backoff", lambda a: None),
                  mock.patch.object(poa, "mcp_fallback_enabled", lambda: True)]
        for x in self.p:
            x.start()
        poa._NONCE["adopted"] = None

    def tearDown(self):
        for x in self.p:
            x.stop()

    def fake_api(self, poll_replies):
        replies = list(poll_replies)

        def call(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            if path == "/api/hosted/me":
                return {"next_nonce": 7}
            if path.startswith("/api/job/"):
                self.polls.append(path)
                r = replies.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            raise poa.PoaHTTPError("network", f"network down on {path}")
        return call

    def fake_mcp(self, tool, arguments=None, need_key=True, timeout=30):
        self.mcp_sends.append(tool)
        return {"job_id": JOB, "poll_url": f"https://minepoa.com/api/job/{JOB}", "retry_after_seconds": 2, "_via": "mcp"}

    def test_mcp_answer_receipt_is_polled_not_counted_as_landed(self):
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"), \
             mock.patch.object(poa, "api_call", self.fake_api([{"txid": "ab" * 32, "next_nonce": 8}])), \
             mock.patch.object(poa, "mcp_call", self.fake_mcp):
            r = poa.submit_answer_safely("answer text", 951)
        self.assertEqual((r["landed"], r["txid"]), (True, "ab" * 32))
        self.assertEqual(self.mcp_sends, ["poa_answer"])
        self.assertEqual(self.polls, [f"/api/job/{JOB}"])

    def test_mcp_answer_still_queued_is_unknown_and_not_resent(self):
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"), mock.patch.object(poa, "BUSY_MAX_WAIT_S", 3), \
             mock.patch.object(poa, "api_call", self.fake_api([{"job_id": JOB, "status": "queued", "retry_after_seconds": 2}] * 3)), \
             mock.patch.object(poa, "mcp_call", self.fake_mcp):
            r = poa.submit_answer_safely("answer text", 952)
            r2 = poa.submit_answer_safely("answer text", 952)
        self.assertEqual((r["landed"], r["kind"]), (None, "queued"))
        self.assertNotEqual(r2.get("how"), "response")
        self.assertEqual(self.mcp_sends, ["poa_answer"])

    def test_mcp_grade_queued_is_not_resent(self):
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"), mock.patch.object(poa, "BUSY_MAX_WAIT_S", 3), \
             mock.patch.object(poa, "api_call", self.fake_api([{"job_id": JOB, "status": "queued", "retry_after_seconds": 2}] * 3)), \
             mock.patch.object(poa, "mcp_call", self.fake_mcp), \
             mock.patch.object(poa, "grade_shown_on_assignments", lambda t: {"confirmed": None}):
            r = poa.submit_grade_safely(TX, 2, "reason")
        self.assertEqual(r["how"], "queued")
        self.assertNotEqual(r.get("landed"), False)
        self.assertEqual(self.mcp_sends, ["poa_grade"])

    def test_mcp_receipt_to_foreign_host_is_refused_without_polling(self):
        def evil(tool, arguments=None, need_key=True, timeout=30):
            self.mcp_sends.append(tool)
            return {"job_id": JOB, "poll_url": f"https://evil.example/api/job/{JOB}"}
        with mock.patch.object(poa, "BASE_URL", "https://minepoa.com"), \
             mock.patch.object(poa, "api_call", self.fake_api([])), mock.patch.object(poa, "mcp_call", evil):
            r = poa.submit_answer_safely("answer text", 953)
        self.assertNotEqual(r.get("landed"), True)
        self.assertEqual(self.polls, [])
        self.assertEqual(self.mcp_sends, ["poa_answer"])


if __name__ == "__main__":
    unittest.main()
