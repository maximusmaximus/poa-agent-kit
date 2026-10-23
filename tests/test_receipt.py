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

    def test_garbage_body_unknown(self):
        r, _ = self.run_with(return_value=b"{}")
        self.assertEqual(r["outcome"], "unknown")

    def test_invalid_txid_makes_no_call(self):
        with mock.patch.object(poa, "http_request") as m:
            r = poa.write_receipt("../etc")
        self.assertEqual((r["outcome"], m.call_count), ("invalid", 0))


if __name__ == "__main__":
    unittest.main()
