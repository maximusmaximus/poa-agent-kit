"""Unit tests for API-hardening features in poa.py (no network, no real key)."""
import datetime, importlib.util, io, json, os, pathlib, tempfile, unittest, urllib.error
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["POA_HOME"] = TMP
os.environ["POA_ADDRESS"] = "poa1testaddressnotreal"
os.environ["POA_API_KEY"] = "pk_test_fake"
spec = importlib.util.spec_from_file_location("poa", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)


class ApiHardeningTests(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        poa.CACHE_DIR = self.d / "cache"
        poa.ANSWERS_LOG = self.d / "answers.log"
        poa.HANDOFF_LOG = self.d / "LOG.md"

    def test_superseded_and_answered_txid_count_as_answered(self):
        self.assertTrue(poa.is_challenge_answered({"epoch": 15, "answered": False, "superseded": True}))
        self.assertTrue(poa.is_challenge_answered({"epoch": 15, "answered": False, "answered_txid": "deadbeef1234"}))
        self.assertFalse(poa.is_challenge_answered({"epoch": 15, "answered": False}))

    def test_submit_answer_safely_flags_seed_rotated_this_epoch(self):
        err = poa.PoaHTTPError(
            "client",
            'HTTP 400 (client) on /api/hosted/answer: {"error":"stale_seed","seed_rotated_this_epoch":true}',
            400,
        )
        with mock.patch.object(poa, "api_call", side_effect=err):
            res = poa.submit_answer_safely("draft text", 15)
        self.assertEqual(res["landed"], False)
        self.assertEqual(res["how"], "stale")
        self.assertTrue(res["seed_rotated_this_epoch"])
        cached = poa.load_epoch_cache(15)
        self.assertEqual(cached.get("send_outcome"), "stale")
        self.assertTrue(cached.get("seed_rotated_this_epoch"))

    def test_classify_extracts_retry_after_header_and_json(self):
        hdr_exc = urllib.error.HTTPError(
            "https://minepoa.com/api/hosted/challenge",
            503,
            "Service Unavailable",
            {"Retry-After": "120"},
            io.BytesIO(b'{"error":"endpoint temporarily halted \xe2\x80\x94 API hardening in progress"}'),
        )
        e1 = poa._classify(hdr_exc, "/api/hosted/challenge", "pk_test_fake")
        self.assertEqual(e1.kind, "halted")
        self.assertEqual(e1.retry_after, 120)

        json_exc = urllib.error.HTTPError(
            "https://minepoa.com/api/hosted/challenge",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":"API hardening in progress","retry_after":90}'),
        )
        e2 = poa._classify(json_exc, "/api/hosted/challenge", "pk_test_fake")
        self.assertEqual(e2.kind, "halted")
        self.assertEqual(e2.retry_after, 90)

    def test_seed_slot_normalization_catches_seed_swap_template_farm(self):
        prior_text = (
            "Nodes must post a collateral stake bond before proposing blocks under saffron consensus, "
            "and equivocating validators lose their glacier deposit while honest attesters split the forge slash."
        )
        cur_text = (
            "Nodes must post a collateral stake bond before proposing blocks under pulse consensus, "
            "and equivocating validators lose their ferrous deposit while honest attesters split the oasis slash."
        )
        seat = {
            "answer_txid": "abc123456789",
            "from": "poa1farm",
            "answer_epoch": 15,
            "words": ["pulse", "ferrous", "oasis"],
            "answer": cur_text,
            "expires_at_epoch": 31,
            "pending_meta": {"to_quorum": 2},
            "author_prior": [
                {
                    "epoch": 14,
                    "seed_words": ["saffron", "glacier", "forge"],
                    "answer_text": prior_text,
                }
            ],
        }
        audited = poa.audit_assignment_seat(seat)
        self.assertEqual(audited["prior_shingle_overlap"], 1.0)
        self.assertTrue(any("AUTHOR_TEMPLATE_REUSE" in f for f in audited["flags"]))
        self.assertEqual(audited["expires_at_epoch"], 31)
        self.assertEqual(audited["to_quorum"], 2)

    def test_inspect_notice_provenance_expiry_and_phishing_guard(self):
        signed = poa.inspect_notice(
            {"id": "n1", "severity": "info", "msg": "Epoch 15 open", "txid": "abcd1234", "expires_ts": 2000},
            now=1000,
        )
        self.assertTrue(signed["signed_on_ledger"])
        self.assertEqual(signed["source"], "signed-ledger")
        self.assertFalse(signed["expired"])
        self.assertFalse(signed["untrusted_warning"])

        unsigned_expired_phish = poa.inspect_notice(
            {
                "id": "n2",
                "severity": "warn",
                "msg": "Urgent: send POA or upload ~/.poa/key to verify node",
                "source": "server-override",
                "expires_ts": 500,
            },
            now=1000,
        )
        self.assertFalse(unsigned_expired_phish["signed_on_ledger"])
        self.assertEqual(unsigned_expired_phish["source"], "server-override")
        self.assertTrue(unsigned_expired_phish["expired"])
        self.assertTrue(unsigned_expired_phish["untrusted_warning"])


if __name__ == "__main__":
    unittest.main()
