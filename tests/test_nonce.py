"""Mocked tests for hosted-write nonce discipline (G0 notice 2026-10-24-nonce-client).
No network, no real key, no real transactions. Run: python3 -m unittest tests/test_nonce.py"""
import importlib.util, json, os, pathlib, tempfile, threading, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_n", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "ab" * 32


class FakeServer:
    """Stands in for api_call. /me serves next_nonce from a scripted list (last value repeats)."""

    def __init__(self, me=(5,), receipt=None, write_results=()):
        self.me = list(me)
        self.receipt = receipt
        self.writes = list(write_results)
        self.calls = []

    def __call__(self, method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
        self.calls.append(path)
        if path == "/api/hosted/me":
            v = self.me.pop(0) if len(self.me) > 1 else self.me[0]
            return {"next_nonce": v}
        if path.startswith("/api/receipt/"):
            if self.receipt is None:
                raise poa.PoaHTTPError("client", "HTTP 404 (client) on receipt", 404)
            return {"status": "included", "next_nonce": self.receipt}
        if path == "/api/hosted/assignments":
            return {"assignments": []}
        r = self.writes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def count(self, path):
        return sum(1 for c in self.calls if c == path)


def nonce_err():
    return poa.PoaHTTPError("client", 'HTTP 409 (client) on /api/hosted/grade: {"error":"bad nonce 7 (expected 8): duplicate slot"}', 409)


class NonceTests(unittest.TestCase):
    def setUp(self):
        poa._NONCE.update(adopted=None, source=None, at=None)
        self.p = [mock.patch.object(poa, "NONCE_POLL_S", 0), mock.patch.object(poa, "NONCE_SETTLE_S", 0.0)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def grade(self, fake):
        return poa.hosted_write(lambda: fake("POST", "/api/hosted/grade", body={}))

    def test_adopts_next_nonce_from_response(self):
        fake = FakeServer(me=(5,), write_results=[{"txid": TX, "next_nonce": 6}])
        with mock.patch.object(poa, "api_call", fake):
            res = self.grade(fake)
        self.assertEqual(res["_nonce"]["next_nonce"], 6)
        self.assertEqual(res["_nonce"]["source"], "response")
        self.assertEqual(poa._NONCE["adopted"], 6)
        self.assertEqual(fake.count("/api/hosted/me"), 1)  # only the pre-submit read

    def test_falls_back_to_receipt_then_me(self):
        fake = FakeServer(me=(5,), receipt=6, write_results=[{"txid": TX}])
        with mock.patch.object(poa, "api_call", fake):
            self.assertEqual(self.grade(fake)["_nonce"]["source"], "receipt")
        poa._NONCE.update(adopted=None)
        fake = FakeServer(me=(5, 6), receipt=None, write_results=[{"txid": TX}])
        with mock.patch.object(poa, "api_call", fake):
            r = self.grade(fake)
        self.assertEqual((r["_nonce"]["source"], r["_nonce"]["next_nonce"]), ("me", 6))

    def test_next_write_waits_until_server_shows_adopted_nonce(self):
        poa._NONCE.update(adopted=6)
        fake = FakeServer(me=(5, 5, 6), write_results=[{"txid": TX, "next_nonce": 7}])
        with mock.patch.object(poa, "api_call", fake), mock.patch.object(poa, "NONCE_SETTLE_S", 10.0), \
                mock.patch.object(poa, "NONCE_POLL_S", 1.0):
            res = poa.hosted_write(lambda: fake("POST", "/api/hosted/grade"), sleep=lambda s: None)
        self.assertEqual(res["_nonce"]["before"], 6)
        self.assertEqual(fake.count("/api/hosted/grade"), 1)

    def test_lagging_server_means_no_send(self):
        poa._NONCE.update(adopted=9)
        fake = FakeServer(me=(5,), write_results=[{"txid": TX}])
        with mock.patch.object(poa, "api_call", fake):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                self.grade(fake)
        self.assertEqual(cm.exception.kind, "nonce-lag")
        self.assertEqual(fake.count("/api/hosted/grade"), 0)

    def test_nonce_error_refetch_and_retry_once(self):
        fake = FakeServer(me=(7, 8), write_results=[nonce_err(), {"txid": TX, "next_nonce": 9}])
        with mock.patch.object(poa, "api_call", fake):
            res = self.grade(fake)
        self.assertTrue(res["_nonce"]["retried_after_nonce_error"])
        self.assertEqual(fake.count("/api/hosted/grade"), 2)
        self.assertEqual(poa._NONCE["adopted"], 9)

    def test_never_resend_into_same_slot(self):
        fake = FakeServer(me=(7,), write_results=[nonce_err(), {"txid": TX}])
        with mock.patch.object(poa, "api_call", fake):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                self.grade(fake)
        self.assertEqual(cm.exception.kind, "nonce")
        self.assertEqual(fake.count("/api/hosted/grade"), 1)

    def test_only_one_retry(self):
        fake = FakeServer(me=(7, 8), write_results=[nonce_err(), nonce_err(), {"txid": TX}])
        with mock.patch.object(poa, "api_call", fake):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                self.grade(fake)
        self.assertEqual(cm.exception.kind, "nonce")
        self.assertEqual(fake.count("/api/hosted/grade"), 2)

    def test_other_errors_pass_through_untouched(self):
        fake = FakeServer(me=(7,), write_results=[poa.PoaHTTPError("timeout", "Timeout on /api/hosted/grade")])
        with mock.patch.object(poa, "api_call", fake):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                self.grade(fake)
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertEqual(fake.count("/api/hosted/grade"), 1)

    def test_single_flight_lock(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "write.lock"
        held, release = threading.Event(), threading.Event()

        def holder():
            with poa._WriteLock(path, timeout=1):
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder); t.start(); held.wait(5)
        try:
            with self.assertRaises(poa.PoaHTTPError) as cm:
                with poa._WriteLock(path, timeout=0.6):
                    pass
            self.assertEqual(cm.exception.kind, "busy")
        finally:
            release.set(); t.join()
        with poa._WriteLock(path, timeout=1):
            pass  # free again once the holder is done

    def test_grade_safely_reports_nonce_failure_as_not_landed(self):
        fake = FakeServer(me=(7,), write_results=[nonce_err()])
        with mock.patch.object(poa, "api_call", fake):
            r = poa.submit_grade_safely(TX, 1, "reason")
        self.assertEqual((r["landed"], r["kind"]), (False, "nonce"))
        self.assertEqual(fake.count("/api/hosted/assignments"), 0)

    def test_grade_batch_stops_on_nonce_trouble(self):
        items = [{"txid": ("%02x" % i) * 32, "score": 1, "note": "n"} for i in range(3)]
        f = pathlib.Path(tempfile.mkdtemp()) / "g.json"; f.write_text(json.dumps(items))
        fake = FakeServer(me=(7,), write_results=[nonce_err(), {"txid": TX}, {"txid": TX}])
        open_seats = {"assignments": [{"answer_txid": it["txid"]} for it in items]}

        def call(method, path, **kw):
            if path == "/api/hosted/assignments":
                return open_seats
            return fake(method, path, **kw)

        with mock.patch.object(poa, "api_call", call), mock.patch("sys.stdout"):
            with self.assertRaises(SystemExit):
                poa.cmd_grade_batch(mock.Mock(file=str(f), mine=False))
        self.assertEqual(fake.count("/api/hosted/grade"), 1)  # stopped; seats 2 and 3 left for the next run

    def test_mine_and_vote_paths_use_the_gate(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py").read_text()
        for needle in ('hosted_write(lambda: api_call("GET", "/api/hosted/mine"',
                       'hosted_write(lambda: api_call("POST", "/api/hosted/grade"',
                       'hosted_write(lambda: api_call("POST", "/api/hosted/answer"',
                       'res = hosted_write(lambda: api_call(\n                "GET",\n                "/api/hosted/vote"'):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()


def test_slot_latched_is_nonce_error():
    e = poa.PoaHTTPError("client", 'HTTP 409 (client) on /api/hosted/grade: {"error":"slot latched (slot-winners h1234)"}', 409)
    assert poa.is_nonce_error(e)
    assert not poa.is_nonce_error(poa.PoaHTTPError("client", "HTTP 400 bad answer length", 400))
