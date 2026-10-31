"""Mocked tests for go.md "Receipts and job polling" (release v0.71-mcpjoin, Oct 31, 2026):
a poll answering 408 {"state":"expired","error":"job_expired"} means never processed (safe to
rebuild and submit once), a bare 202 {"state":"queued"} poll is still pending, and 404 stays a
tombstone (no resend). No network, no real key. Run: python3 -m unittest tests/test_job_expired.py"""
import importlib.util, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_jobexp", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
JID = "0123456789abcdef0123456789abcdef"
RECEIPT = {"job_id": JID, "poll_url": f"api/job/{JID}", "retry_after_seconds": 2}
TXA = "cd" * 32


def no_network(*a, **k):
    raise AssertionError("test reached the network layer")


class JobExpiredTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(poa, "POA_DIR", self.home),
                  mock.patch.object(poa, "CACHE_DIR", self.home / "cache"),
                  mock.patch.object(poa, "BUSY_MAX_WAIT_S", 20),
                  mock.patch.object(poa.time, "sleep", lambda s: None),
                  mock.patch.object(poa, "_backoff", lambda a: None),
                  mock.patch.object(poa, "http_request", no_network)]
        for x in self.p:
            x.start()
        poa._NONCE["adopted"] = None

    def tearDown(self):
        for x in self.p:
            x.stop()

    def fake(self, writes, polls):
        writes, polls = list(writes), list(polls)

        def call(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            self.calls.append((method, path))
            if path == "/api/hosted/me":
                return {"next_nonce": 7}
            if path == "/api/hosted/assignments/":
                return {"assignments": []}
            src = polls if path.startswith("/api/job/") else writes
            item = src.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return call

    def test_queued_state_without_job_id_is_pending(self):
        self.assertTrue(poa.is_busy_receipt({"state": "queued"}))
        self.assertFalse(poa.is_busy_receipt({"state": "done", "txid": TXA}))
        self.assertFalse(poa.is_busy_receipt({"state": "expired", "error": "job_expired"}))

    def test_poll_keeps_waiting_through_bare_queued_then_returns_result(self):
        call = self.fake([], [{"state": "queued"}, {"state": "done", "txid": TXA}])
        with mock.patch.object(poa, "api_call", call):
            res = poa.resolve_busy_receipt(dict(RECEIPT), path="/api/hosted/answer")
        self.assertEqual(res["txid"], TXA)
        self.assertEqual([c[1] for c in self.calls], [f"/api/job/{JID}"] * 2)

    def test_408_job_expired_raises_job_expired(self):
        err = poa.PoaHTTPError("client", 'HTTP 408 (client) on /api/job/x: {"state":"expired","error":"job_expired"}', 408)
        call = self.fake([], [err])
        with mock.patch.object(poa, "api_call", call):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                poa.resolve_busy_receipt(dict(RECEIPT), path="/api/hosted/answer")
        self.assertEqual(cm.exception.kind, "job_expired")
        self.assertTrue(cm.exception.unprocessed)

    def test_404_still_tombstone_queued(self):
        err = poa.PoaHTTPError("client", "HTTP 404 (client) on /api/job/x: unknown or expired job id", 404)
        call = self.fake([], [err])
        with mock.patch.object(poa, "api_call", call):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                poa.resolve_busy_receipt(dict(RECEIPT), path="/api/hosted/answer")
        self.assertEqual(cm.exception.kind, "queued")

    def test_answer_job_expired_allows_one_later_send(self):
        err = poa.PoaHTTPError("client", 'HTTP 408 (client) on /api/job/x: {"state":"expired","error":"job_expired"}', 408)
        call = self.fake([dict(RECEIPT), {"txid": TXA, "next_nonce": 8}], [err])
        with mock.patch.object(poa, "api_call", call):
            first = poa.submit_answer_safely("alpha bravo charlie answer", 41)
            self.assertFalse(first["landed"])
            self.assertEqual(first["how"], "job_expired")
            self.assertEqual(poa.load_epoch_cache(41)["send_outcome"], "job_expired")
            second = poa.submit_answer_safely("alpha bravo charlie answer", 41)
        self.assertTrue(second["landed"])
        self.assertEqual(second["txid"], TXA)
        posts = [c for c in self.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)

    def test_saved_job_408_marks_expired_unprocessed(self):
        poa.save_busy_jobs([{"job_id": JID, "route": "/api/hosted/answer", "epoch": 42, "state": "queued"}])
        poa.save_epoch_cache(42, sent_at="x", send_outcome="queued")
        err = poa.PoaHTTPError("client", "HTTP 408 job_expired", 408)
        out = poa.check_busy_jobs(42, poll=lambda path: (_ for _ in ()).throw(err))
        self.assertEqual(out[0]["state"], "expired-unprocessed")
        self.assertEqual(poa.load_epoch_cache(42)["send_outcome"], "job_expired")
        self.assertIsNone(poa.load_busy_jobs()[0]["job_id"])


if __name__ == "__main__":
    unittest.main()
