"""Mocked tests for the Oct 26 2026 server releases (no network, no real key).
v1.2-txlookup-hardening: 30/min tx-lookup cap, 60 s negative cache, unknown txid -> 503 + Retry-After 60.
v1.3-doorpow: /api/join-pow admission proof for new registrations.
Run: python3 -m unittest tests/test_site_changes_20261026.py"""
import argparse, contextlib, hashlib, importlib.util, io, json, os, pathlib, tempfile, unittest, urllib.error, urllib.parse
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_sc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "ab12cd34ef567890ab12cd34"
BUSY = b'{"error":"origin busy \\u2014 retry shortly","retry_after_seconds":60}'


def http_err(code, body=b'{"error":"x"}', headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(body))


class Isolated(unittest.TestCase):
    def setUp(self):
        d = mock.patch.object(poa, "POA_DIR", pathlib.Path(tempfile.mkdtemp()))
        d.start(); self.addCleanup(d.stop)


class RetryAfterTests(Isolated):
    def test_body_retry_after_seconds_is_parsed(self):
        err = poa._classify(http_err(503, BUSY), "/api/explorer/tx/x", "")
        self.assertEqual((err.kind, err.status, err.retry_after), ("server", 503, 60))

    def test_header_still_wins(self):
        err = poa._classify(http_err(429, b"{}", {"Retry-After": "17"}), "/p", "")
        self.assertEqual(err.retry_after, 17)


class ReceiptNegativeCacheTests(Isolated):
    def test_503_busy_is_unknown_and_cached(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(503, BUSY)) as m:
            r1 = poa.write_receipt(TX)
            r2 = poa.write_receipt(TX)
        self.assertEqual((r1["outcome"], r1["resend_ok"], r1["http_status"], r1["retry_after_s"]), ("unknown", False, 503, 60))
        self.assertTrue(r1["retry_not_before"])
        self.assertEqual((r2["outcome"], r2["resend_ok"], r2["error_kind"]), ("unknown", False, "cached-miss"))
        self.assertEqual(m.call_count, 1, "a cached miss must not hit the network again within 60 s")

    def test_404_not_found_says_recheck_after_60s_and_is_cached(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(404)) as m:
            r1 = poa.write_receipt(TX)
            r2 = poa.write_receipt(TX)
        self.assertEqual((r1["outcome"], r1["resend_ok"], r1["recheck_after_s"]), ("not-found", True, 60))
        self.assertEqual((r2["outcome"], r2["resend_ok"]), ("unknown", False))
        self.assertEqual(m.call_count, 1)

    def test_cache_expires(self):
        poa.txlookup_mark_negative(TX, 503, 60, now=1000.0)
        self.assertIsNotNone(poa.txlookup_negative(TX, now=1059.0))
        self.assertIsNone(poa.txlookup_negative(TX, now=1061.0))

    def test_longer_retry_after_is_respected(self):
        poa.txlookup_mark_negative(TX, 429, 120, now=1000.0)
        self.assertIsNotNone(poa.txlookup_negative(TX, now=1100.0))

    def test_found_tx_is_not_cached(self):
        body = json.dumps({"id": TX, "type": "challenge", "final": 1}).encode()
        with mock.patch.object(poa, "http_request", return_value=body) as m:
            poa.write_receipt(TX); poa.write_receipt(TX)
        self.assertEqual(m.call_count, 2)


class ReceiptNonceTests(Isolated):
    def test_receipt_next_nonce_marks_and_skips(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(404)) as m:
            self.assertIsNone(poa.receipt_next_nonce(TX))
            self.assertIsNone(poa.receipt_next_nonce(TX))
        self.assertEqual(m.call_count, 1)

    def test_receipt_next_nonce_ok(self):
        with mock.patch.object(poa, "http_request", return_value=b'{"next_nonce": 12}'):
            self.assertEqual(poa.receipt_next_nonce(TX), 12)


class ExplorerGetTests(Isolated):
    def test_tx_path_miss_is_cached(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(503, BUSY)) as m:
            a = poa.explorer_get(f"/api/explorer/tx/{TX}")
            b = poa.explorer_get(f"/api/explorer/tx/{TX}")
        self.assertEqual((a["status"], a["retry_after"]), (503, 60))
        self.assertTrue(b.get("cached_miss"))
        self.assertEqual(m.call_count, 1)

    def test_address_path_is_not_guarded(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(503, BUSY)) as m:
            poa.explorer_get("/api/explorer/address/poa1x?sort=new"); poa.explorer_get("/api/explorer/address/poa1x?sort=new")
        self.assertEqual(m.call_count, 2)


class WindowTests(Isolated):
    def test_window_waits_when_full(self):
        clock = {"t": 1000.0}
        slept = []
        def sleep(d):
            slept.append(d); clock["t"] += d
        with mock.patch.object(poa, "TXLOOKUP_PER_MIN", 3):
            for _ in range(3):
                self.assertEqual(poa.txlookup_slot(sleep=sleep, now_fn=lambda: clock["t"]), 0.0)
            waited = poa.txlookup_slot(sleep=sleep, now_fn=lambda: clock["t"])
        self.assertGreaterEqual(waited, 59.0)
        self.assertLessEqual(waited, 65.0)

    def test_window_bounded_by_max_wait(self):
        clock = {"t": 0.0}
        def sleep(d):
            clock["t"] += d
        with mock.patch.object(poa, "TXLOOKUP_PER_MIN", 1):
            poa.txlookup_slot(sleep=sleep, now_fn=lambda: clock["t"])
            self.assertLessEqual(poa.txlookup_slot(sleep=sleep, now_fn=lambda: clock["t"], max_wait=5), 5.0)


class JoinPowTests(Isolated):
    CH = "3f5e7b8f4082fbc409bb8560059de84e"

    def test_leading_zero_bits(self):
        self.assertEqual(poa.pow_leading_zero_bits(bytes([0, 0, 0x0F]) + bytes(29)), 20)
        self.assertEqual(poa.pow_leading_zero_bits(bytes([0x80]) + bytes(31)), 0)
        self.assertEqual(poa.pow_leading_zero_bits(bytes([0x01]) + bytes(31)), 7)

    def test_known_vector_matches_reference_js(self):
        # nonce found by the go.md reference JS for this challenge at 20 bits
        d = hashlib.sha256((self.CH + ":1021410").encode()).digest()
        self.assertGreaterEqual(poa.pow_leading_zero_bits(d), 20)
        r = poa.solve_join_pow(self.CH, 12)
        d2 = hashlib.sha256((self.CH + ":" + r["nonce"]).encode()).digest()
        self.assertGreaterEqual(poa.pow_leading_zero_bits(d2), 12)

    def test_max_iters(self):
        self.assertIsNone(poa.solve_join_pow(self.CH, 64, max_iters=10)["nonce"])

    def test_register_sends_pow_label_and_invited_by(self):
        info = {"challenge": self.CH, "bits": 8, "required": True, "expires_ts": 9999999999}
        sent = {}
        def fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            sent.update(method=method, url=url, body=json.loads(data))
            return b'{"api_key":"pk_new_fake","address":"poa1new"}'
        with mock.patch.object(poa, "http_request", side_effect=fake):
            out = poa.register_with_pow({"label": "Ada", "invited_by": "poa1ref"}, fetch=lambda: info, log=lambda *a, **k: None)
        self.assertEqual(out["api_key"], "pk_new_fake")
        self.assertEqual(sent["method"], "POST")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(sent["url"]).query)
        self.assertEqual((q["label"], q["invited_by"]), (["Ada"], ["poa1ref"]))
        pw = sent["body"]["pow"]
        self.assertEqual(pw["challenge"], self.CH)
        d = hashlib.sha256((self.CH + ":" + pw["nonce"]).encode()).digest()
        self.assertGreaterEqual(poa.pow_leading_zero_bits(d), 8)

    def test_structured_403_remines_once(self):
        infos = iter([{"challenge": "aa", "bits": 4}, {"challenge": "bb", "bits": 4}])
        calls = []
        def fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            calls.append(json.loads(data)["pow"]["challenge"])
            if len(calls) == 1:
                raise http_err(403, b'{"error":"pow","required_bits":6,"get_challenge":"/api/join-pow"}')
            return b'{"api_key":"pk_new_fake"}'
        with mock.patch.object(poa, "http_request", side_effect=fake):
            poa.register_with_pow({}, fetch=lambda: next(infos), log=lambda *a, **k: None)
        self.assertEqual(calls, ["aa", "bb"])

    def test_other_403_is_not_retried(self):
        with mock.patch.object(poa, "http_request", side_effect=http_err(403, b'{"error":"banned"}')) as m:
            with self.assertRaises(poa.PoaHTTPError):
                poa.register_with_pow({}, fetch=lambda: {"challenge": "aa", "bits": 2}, log=lambda *a, **k: None)
        self.assertEqual(m.call_count, 1)

    def test_join_pow_command_is_keyless_and_does_not_submit(self):
        info = {"challenge": self.CH, "bits": 6, "required": True, "single_use": True, "expires_ts": 9999999999}
        buf = io.StringIO()
        with mock.patch.object(poa, "fetch_join_pow", return_value=info), mock.patch.object(poa, "http_request") as m, \
             contextlib.redirect_stdout(buf):
            poa.cmd_join_pow(argparse.Namespace(solve=True))
        out = json.loads(buf.getvalue())
        self.assertEqual((out["bits"], out["expected_hashes"], out["solved"]), (6, 64, True))
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
