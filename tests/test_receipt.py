"""Mocked tests for `poa.py receipt` (no network, no real key). Run: python3 -m unittest tests/test_receipt.py"""
import importlib.util, io, json, os, pathlib, tempfile, unittest, urllib.error
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_r", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "23b9d52cabcdef0123456789"


def http_err(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b'{"error":"x"}'))


class ReceiptTests(unittest.TestCase):
    def run_with(self, **kw):
        with mock.patch.object(poa, "http_request", **kw) as m:
            return poa.write_receipt(TX), m

    def test_final(self):
        body = json.dumps({"tx": {"id": TX, "type": "challenge", "epoch": 10, "final": 1, "atts": 9}}).encode()
        r, _ = self.run_with(return_value=body)
        self.assertEqual((r["outcome"], r["resend_ok"], r["attestations"], r["epoch"]), ("landed-final", False, 9, 10))

    def test_pending_flat_body_and_att_list(self):
        r, _ = self.run_with(return_value=json.dumps({"id": TX, "type": "grade", "final": 0, "atts": ["a", "b"]}).encode())
        self.assertEqual((r["outcome"], r["attestations"]), ("landed-pending", 2))

    def test_404_is_not_found(self):
        r, _ = self.run_with(side_effect=http_err(404))
        self.assertEqual((r["outcome"], r["resend_ok"]), ("not-found", True))

    def test_timeout_is_unknown_not_failed(self):
        r, _ = self.run_with(side_effect=TimeoutError("timed out"))
        self.assertEqual((r["outcome"], r["resend_ok"], r["error_kind"]), ("unknown", False, "timeout"))

    def test_503_is_unknown(self):
        r, _ = self.run_with(side_effect=http_err(503))
        self.assertEqual((r["outcome"], r["resend_ok"]), ("unknown", False))

    def test_3xx_redirect_is_unknown_never_resend(self):
        for code in (301, 302, 303, 307, 308):
            r, m = self.run_with(side_effect=http_err(code))
            self.assertEqual((r["outcome"], r["resend_ok"], r["error_kind"]), ("unknown", False, "redirect"), code)
            self.assertIn("NOT INDEXED", r["note"])
            self.assertIs(m.call_args.kwargs.get("follow_redirects"), False)

    def test_urllib_does_not_follow_redirect(self):
        # 307 -> page that would 404: must surface as 307 (unknown), never as not-found
        opener = mock.MagicMock()
        opener.open.side_effect = http_err(307)
        with mock.patch.dict(os.environ, {"POA_NO_HTTPX": "1"}), \
             mock.patch.object(poa.urllib.request, "build_opener", return_value=opener) as bo, \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=http_err(404)) as uo:
            r = poa.write_receipt(TX)
        self.assertEqual((r["outcome"], r["resend_ok"], r["http_status"]), ("unknown", False, 307))
        self.assertIs(bo.call_args.args[0], poa._NoRedirect)
        uo.assert_not_called()

    def test_noredirect_handler_returns_none(self):
        self.assertIsNone(poa._NoRedirect().redirect_request(None, None, 307, "x", {}, "https://e/x"))

    def test_garbage_body_unknown(self):
        r, _ = self.run_with(return_value=b"{}")
        self.assertEqual(r["outcome"], "unknown")

    def test_invalid_txid_makes_no_call(self):
        with mock.patch.object(poa, "http_request") as m:
            r = poa.write_receipt("../etc")
        self.assertEqual((r["outcome"], m.call_count), ("invalid", 0))


if __name__ == "__main__":
    unittest.main()
