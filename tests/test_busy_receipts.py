"""Mocked tests for busy-node receipts (G0 notice 2026-10-29-busy-node-receipts, release
v0.60-acceptqueue-buildtx): answer and grade may return 202 + poll_url. 202 = accepted, never resubmit.
No network, no real key, no real transactions. Run: python3 -m unittest tests/test_busy_receipts.py"""
import importlib.util, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_busy", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "ef" * 32
RECEIPT = {"job_id": "j123", "poll_url": "api/job/j123", "retry_after_seconds": 3}


class BusyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.sleeps = []
        self.home = tempfile.mkdtemp()
        self.p = [mock.patch.object(poa, "POA_DIR", pathlib.Path(self.home)),
                  mock.patch.object(poa.time, "sleep", lambda s: self.sleeps.append(s)),
                  mock.patch.object(poa, "_backoff", lambda a: None)]
        for x in self.p:
            x.start()
        poa._NONCE["adopted"] = None

    def tearDown(self):
        for x in self.p:
            x.stop()

    def fake(self, writes, polls):
        writes, polls = list(writes), list(polls)

        def call(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            self.calls.append((method, path, need_key))
            if path == "/api/hosted/me":
                return {"next_nonce": 7}
            if path == "/api/hosted/assignments/":
                return {"assignments": []}
            src = polls if path.startswith("/api/job/") else writes
            r = src.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return call

    def writes_sent(self, path):
        return sum(1 for m, p, _ in self.calls if m == "POST" and p == path)

    def test_detects_receipt_but_not_results(self):
        self.assertTrue(poa.is_busy_receipt(RECEIPT))
        self.assertTrue(poa.is_busy_receipt({"job_id": "j1", "status": "queued"}))
        self.assertFalse(poa.is_busy_receipt({"txid": "aa", "next_nonce": 8, "job_id": "j1"}))
        self.assertFalse(poa.is_busy_receipt({"ok": True}))

    def test_refuses_foreign_poll_url(self):
        for bad in ("https://evil.example/api/job/1", "//evil/api/job/1", "api/hosted/me", "api/job/../x"):
            with self.assertRaises(poa.PoaHTTPError):
                poa.busy_poll_path({"poll_url": bad})
        self.assertEqual(poa.busy_poll_path(RECEIPT), "/api/job/j123")

    def test_answer_202_polls_once_sent_once(self):
        fake = self.fake([dict(RECEIPT)], [{"job_id": "j123", "status": "queued", "retry_after_seconds": 2},
                                           {"txid": "ab" * 32, "next_nonce": 8}])
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_answer_safely("answer text", 901)
        self.assertEqual((r["landed"], r["txid"]), (True, "ab" * 32))
        self.assertEqual(self.writes_sent("/api/hosted/answer"), 1)
        self.assertEqual([p for _, p, _ in self.calls if p.startswith("/api/job/")], ["/api/job/j123"] * 2)
        self.assertEqual(self.sleeps[:2], [3.0, 2.0])

    def test_answer_still_queued_is_unknown_and_not_resent(self):
        with mock.patch.object(poa, "BUSY_MAX_WAIT_S", 4):
            fake = self.fake([dict(RECEIPT)], [{"job_id": "j123", "status": "queued", "retry_after_seconds": 3}])
            with mock.patch.object(poa, "api_call", fake):
                r = poa.submit_answer_safely("answer text", 902)
                r2 = poa.submit_answer_safely("answer text", 902)
        self.assertEqual((r["landed"], r["kind"]), (None, "queued"))
        self.assertEqual(r2["how"], "sent_before")
        self.assertEqual(self.writes_sent("/api/hosted/answer"), 1)

    def test_409_in_flight_is_queued_not_duplicate_landed(self):
        e = poa.PoaHTTPError("client", 'HTTP 409 (client) on /api/hosted/answer: {"error":"duplicate_in_flight"}', 409)
        with mock.patch.object(poa, "api_call", self.fake([e], [])):
            r = poa.submit_answer_safely("answer text", 903)
        self.assertEqual((r["landed"], r["how"]), (None, "queued"))

    def test_expired_job_is_queued(self):
        gone = poa.PoaHTTPError("client", "HTTP 404 (client) on /api/job/j123: not found", 404)
        with mock.patch.object(poa, "api_call", self.fake([dict(RECEIPT)], [gone])):
            r = poa.submit_answer_safely("answer text", 904)
        self.assertEqual((r["landed"], r["kind"]), (None, "queued"))

    def test_grade_202_resolves_and_confirms_on_assignments(self):
        fake = self.fake([dict(RECEIPT)], [{"txid": "cd" * 32, "next_nonce": 8}])
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_grade_safely(TX, 2, "reason")
        self.assertTrue(r["landed"])
        self.assertEqual(self.writes_sent("/api/hosted/grade"), 1)

    def test_grade_in_flight_never_resent(self):
        e = poa.PoaHTTPError("client", 'HTTP 409 (client) on /api/hosted/grade: duplicate_in_flight', 409)
        fake = self.fake([e], [])
        with mock.patch.object(poa, "api_call", fake), mock.patch.object(poa, "grade_shown_on_assignments", lambda t: {"confirmed": False}):
            r = poa.submit_grade_safely(TX, 2, "reason")
        self.assertEqual((r["landed"], r["how"]), (None, "queued"))
        self.assertEqual(self.writes_sent("/api/hosted/grade"), 1)

    def test_poll_never_puts_key_in_url(self):
        seen = []

        def fake_http(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            seen.append((url, follow_redirects, (headers or {}).get("X-Api-Key")))
            return b'{"txid":"aa","next_nonce":3}'
        with mock.patch.object(poa, "http_request", fake_http):
            poa.resolve_busy_receipt(dict(RECEIPT), "/api/hosted/answer", sleep=lambda s: None)
        url, follow, hdr = seen[0]
        self.assertTrue(url.endswith("/api/job/j123"))
        self.assertNotIn("pk_test_fake", url)
        self.assertFalse(follow)
        self.assertTrue(hdr)


if __name__ == "__main__":
    unittest.main()
