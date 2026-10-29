"""Mocked tests for rejected receipts (go.md "READING A REJECTED RECEIPT", release v0.66-ipfix-receipts).
No network, no real key. Run: python3 -m unittest tests/test_rejected_receipts.py"""
import importlib.util, io, json, os, pathlib, tempfile, unittest, urllib.error
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_rr", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
TX = "ab12cd34ef56ab12cd34ef56"


def http_err(code, body=b'{"error":"x"}'):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))


class RejectedReceiptTests(unittest.TestCase):
    def setUp(self):
        d = mock.patch.object(poa, "POA_DIR", pathlib.Path(tempfile.mkdtemp()))
        d.start(); self.addCleanup(d.stop)

    def run_seq(self, *effects, txid=TX):
        with mock.patch.object(poa, "http_request", side_effect=list(effects)) as m:
            return poa.write_receipt(txid), m

    def test_rejected_nonce_slot_used(self):
        body = json.dumps({"status": "rejected", "error": "slot used", "code": "nonce_slot_used",
                           "hint": "rebuild with next_nonce", "from": "poa1x", "nonce": 7, "next_nonce": 9}).encode()
        r, m = self.run_seq(http_err(404), body)
        self.assertEqual((r["outcome"], r["resend_ok"], r["code"], r["next_nonce"], r["nonce"]),
                         ("rejected", False, "nonce_slot_used", 9, 7))
        self.assertIn("NEW tx", r["action"])
        self.assertNotIn("from", r)  # never echo the sender address
        self.assertEqual(m.call_count, 2)
        urls = [c[0][1] for c in m.call_args_list]
        self.assertTrue(urls[1].endswith(f"/api/receipt/{TX}"))
        for c in m.call_args_list:
            self.assertNotIn("pk_test_fake", c[0][1])
            self.assertNotIn("X-Api-Key", (c[1].get("headers") or {}))

    def test_codes_map_to_actions(self):
        for i, (code, word) in enumerate((("off_panel", "assignments"), ("duplicate_grade", "move on"), ("bad_stamp", "stamp"), ("bad_nonce", "next_nonce"))):
            body = json.dumps({"status": "rejected", "code": code, "hint": "h"}).encode()
            r, _ = self.run_seq(http_err(404), body, txid=f"{i:02d}" + TX[2:])  # fresh txid: a 404 is cached per txid for 60 s
            self.assertEqual((r["outcome"], r["resend_ok"]), ("rejected", False), code)
            self.assertIn(word, r["action"], code)

    def test_unknown_code_follows_hint(self):
        r, _ = self.run_seq(http_err(404), json.dumps({"status": "rejected", "code": "new_thing", "hint": "do x"}).encode())
        self.assertEqual((r["outcome"], r["action"], r["hint"]), ("rejected", "follow the hint", "do x"))

    def test_node_included_when_explorer_404(self):
        body = json.dumps({"status": "included", "type": "answer", "checkpoint_covered": False, "next_nonce": 4}).encode()
        r, _ = self.run_seq(http_err(404), body)
        self.assertEqual((r["outcome"], r["resend_ok"], r["next_nonce"]), ("landed-pending", False, 4))

    def test_both_404_resubmit_once(self):
        r, _ = self.run_seq(http_err(404), http_err(404, b'{"error":"unknown txid"}'))
        self.assertEqual((r["outcome"], r["resend_ok"], r["node_receipt"]), ("not-found", True, "404 unknown txid"))
        self.assertIn("ONCE", r["note"])

    def test_node_unreadable_keeps_not_found(self):
        r, _ = self.run_seq(http_err(404), TimeoutError("t"))
        self.assertEqual((r["outcome"], r["resend_ok"]), ("not-found", True))

    def test_explorer_unknown_does_not_ask_node(self):
        r, m = self.run_seq(TimeoutError("t"))
        self.assertEqual((r["outcome"], m.call_count), ("unknown", 1))

    def test_landed_does_not_ask_node(self):
        r, m = self.run_seq(json.dumps({"tx": {"id": TX, "type": "grade", "final": 1}}).encode())
        self.assertEqual((r["outcome"], m.call_count), ("landed-final", 1))


if __name__ == "__main__":
    unittest.main()
