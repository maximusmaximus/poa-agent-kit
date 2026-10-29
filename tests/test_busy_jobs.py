"""Mocked tests for the busy-job store (SUGGESTIONS #79): a still-queued 202 job is saved and the
next run polls it once before deciding, never resending. No network, no real key: api_call is
patched (the real call path of submit_answer_safely and check_busy_jobs), and http_request is
patched to fail loudly in case anything slips past. Run: python3 -m unittest tests/test_busy_jobs.py"""
import importlib.util, io, json, os, pathlib, stat, tempfile, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_jobs", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
JID = "0123456789abcdef0123456789abcdef"
RECEIPT = {"job_id": JID, "poll_url": f"api/job/{JID}", "retry_after_seconds": 3}
TXA = "ab" * 32


def no_network(*a, **k):
    raise AssertionError("test reached the network layer")


class BusyJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.p = [mock.patch.object(poa, "POA_DIR", self.home),
                  mock.patch.object(poa, "CACHE_DIR", self.home / "cache"),
                  mock.patch.object(poa, "BUSY_MAX_WAIT_S", 4),
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
            r = src.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return call

    def sends(self, path="/api/hosted/answer"):
        return sum(1 for m, p in self.calls if m == "POST" and p == path)

    def queue_answer(self, epoch, extra_polls=()):
        still = {"job_id": JID, "status": "queued", "retry_after_seconds": 3}
        fake = self.fake([dict(RECEIPT)], [still] + list(extra_polls))
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_answer_safely("answer text", epoch)
        self.assertEqual(r["kind"], "queued")
        return fake

    def test_queued_answer_is_saved_private_with_intent_hash(self):
        self.queue_answer(910)
        rows = poa.load_busy_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["state"], rows[0]["epoch"], rows[0]["route"]), ("queued", 910, "/api/hosted/answer"))
        self.assertEqual(len(rows[0]["intent_sha256"]), 64)
        self.assertNotIn("answer text", json.dumps(rows))
        mode = stat.S_IMODE(os.stat(self.home / poa.BUSY_JOBS_FILE).st_mode)
        if os.name == "posix":
            self.assertEqual(mode & 0o077, 0)

    def test_next_run_resolves_saved_job_without_resending(self):
        self.queue_answer(911)
        fake = self.fake([], [{"txid": TXA, "next_nonce": 8}])
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_answer_safely("answer text", 911)
        self.assertEqual((r["landed"], r["how"], r["txid"]), (True, "busy_job", TXA))
        self.assertEqual(self.sends(), 1)  # only the original send, in queue_answer
        row = poa.load_busy_jobs()[0]
        self.assertEqual((row["state"], row["txid"], row["job_id"]), ("done", TXA, None))
        self.assertEqual(poa.load_epoch_cache(911).get("txid_answer"), TXA)

    def test_expired_job_becomes_tombstone_never_resent(self):
        self.queue_answer(912)
        gone = poa.PoaHTTPError("client", f"HTTP 404 (client) on /api/job/{JID}: not found", 404)
        fake = self.fake([], [gone])
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_answer_safely("answer text", 912)
        self.assertEqual(r["how"], "sent_before")
        self.assertEqual(poa.load_busy_jobs()[0]["state"], "tombstone")
        self.assertEqual(self.sends(), 1)

    def test_still_queued_stays_queued_and_records_poll(self):
        self.queue_answer(913)
        fake = self.fake([], [{"job_id": JID, "status": "running"}])
        with mock.patch.object(poa, "api_call", fake):
            out = poa.check_busy_jobs(913)
        self.assertEqual(out[0]["state"], "queued")
        row = poa.load_busy_jobs()[0]
        self.assertTrue(row["last_poll_at"])
        self.assertEqual(row["last_poll"], "still queued")

    def test_first_poll_404_saves_tombstone_directly(self):
        gone = poa.PoaHTTPError("client", "HTTP 404 (client) on /api/job/x: not found", 404)
        fake = self.fake([dict(RECEIPT)], [gone])
        with mock.patch.object(poa, "api_call", fake):
            poa.submit_answer_safely("answer text", 914)
        self.assertEqual(poa.load_busy_jobs()[0]["state"], "tombstone")

    def test_malformed_job_id_is_never_polled(self):
        poa.save_busy_jobs([{"job_id": "NOT-HEX", "route": "/api/hosted/answer", "epoch": 915, "state": "queued"}])
        with mock.patch.object(poa, "api_call", self.fake([], [])):
            out = poa.check_busy_jobs()
        self.assertEqual(out[0]["state"], "invalid-id")
        self.assertFalse([c for c in self.calls if c[1].startswith("/api/job/")])

    def test_grade_queued_is_saved_with_assignment(self):
        seat = "ef" * 32
        still = {"job_id": JID, "status": "queued", "retry_after_seconds": 3}
        fake = self.fake([dict(RECEIPT)], [still])
        with mock.patch.object(poa, "api_call", fake), mock.patch.object(poa, "grade_shown_on_assignments", lambda t: {"confirmed": None}):
            poa.submit_grade_safely(seat, 2, "reason")
        row = poa.load_busy_jobs()[0]
        self.assertEqual((row["route"], row["assignment"], row["state"]), ("/api/hosted/grade", seat, "queued"))
        self.assertEqual(self.sends("/api/hosted/grade"), 1)

    def test_jobs_command_masks_job_id(self):
        self.queue_answer(916)
        buf = io.StringIO()
        with redirect_stdout(buf):
            poa.cmd_jobs(SimpleNamespace(check=False, epoch=None, prune=False, json=True))
        text = buf.getvalue()
        self.assertNotIn(JID, text)
        self.assertIn(JID[:6], text)
        buf = io.StringIO()
        with redirect_stdout(buf):
            poa.cmd_jobs(SimpleNamespace(check=False, epoch=None, prune=False, json=False))
        self.assertNotIn(JID, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
