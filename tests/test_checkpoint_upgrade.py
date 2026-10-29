"""Server release v0.21-pusher (Oct 28, 2026): checkpoint roll-ups. Mocked only, no network."""
import importlib.util, os, pathlib, tempfile, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["POA_HOME"] = TMP
os.environ["POA_ADDRESS"] = "poa1testaddressnotreal"
os.environ["POA_API_KEY"] = "pk_fakeKEYcheckpoint000"
spec = importlib.util.spec_from_file_location("poa_cp", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ANCHORS = [
    {"height": 93456, "chain": "base-mainnet", "tx": "0xaa", "state_root": "e2", "anchored_at": "2026-10-28T16:16:55.175Z"},
    {"height": 93456, "chain": "solana-mainnet", "tx": "5T", "state_root": "e2", "anchored_at": "2026-10-28T16:16:55.175Z"},
    {"height": 93137, "chain": "base-mainnet", "tx": "0xbb", "state_root": "ec", "anchored_at": "2026-10-28T16:01:00Z"},
]
NOW = 1793206800  # 2026-10-28T17:00:00Z


class LedgerStatusTests(unittest.TestCase):
    def test_fresh_status_reports_no_lag_and_skips_github(self):
        with mock.patch.object(poa, "ledger_ls", side_effect=AssertionError("GitHub mirror must not be read")):
            r = poa.ledger_status("poa-mainnet-1", fetch_status=lambda: {"checkpoint_height": 94113, "state_root": "e3", "epochs_closed": 16},
                                  fetch_anchors=lambda: ANCHORS, clock=lambda: NOW)
        self.assertEqual(r["latest_checkpoint"]["height"], 94113)
        self.assertEqual(r["latest_checkpoint"]["via"], "/api/status")
        self.assertEqual(r["checkpoint_lag_seconds"], 0)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["latest_anchors"]["base-mainnet"]["height"], 93456)
        self.assertNotIn("github_mirror", r)

    def test_stale_view_uses_stale_age(self):
        r = poa.ledger_status("b", fetch_status=lambda: {"checkpoint_height": 93456, "stale": True, "stale_age_s": 151},
                              fetch_anchors=lambda: [], clock=lambda: NOW)
        self.assertEqual(r["status"], "stale")
        self.assertEqual(r["checkpoint_lag_seconds"], 151)

    def test_status_down_falls_back_to_anchors_with_unknown_lag(self):
        def boom():
            raise poa.PoaHTTPError("timeout", "Timeout on /api/status")
        r = poa.ledger_status("b", fetch_status=boom, fetch_anchors=lambda: ANCHORS, clock=lambda: NOW)
        self.assertEqual(r["latest_checkpoint"]["height"], 93456)
        self.assertEqual(r["latest_checkpoint"]["via"], "POAScan anchors")
        self.assertIsNone(r["checkpoint_lag_seconds"])
        self.assertEqual(r["status"], "unknown")

    def test_everything_down_is_unknown_not_lag(self):
        def boom():
            raise OSError("down")
        r = poa.ledger_status("b", fetch_status=boom, fetch_anchors=boom, clock=lambda: NOW)
        self.assertEqual(r["status"], "unknown")
        self.assertIsNone(r["checkpoint_lag_hours"])


class SinceCheckpointLabelTests(unittest.TestCase):
    def test_status_counts_labelled(self):
        r = poa.label_status_counts({"txs": 426, "final": 380, "invalid": 0, "checkpoint_height": 94113})
        self.assertEqual(set(r["counts_scope"]), {"txs", "final", "invalid"})
        self.assertIn("since latest checkpoint", r["counts_scope"]["txs"])
        self.assertEqual(r["txs"], 426)

    def test_me_attestations_labelled(self):
        r = poa.label_me_counts({"attestations_by_me": 3, "next_nonce": 9})
        self.assertIn("since latest checkpoint", r["attestations_by_me_scope"])
        self.assertEqual(poa.label_me_counts({"x": 1}), {"x": 1})


class ExplorerUnknownTests(unittest.TestCase):
    def test_not_listed_is_unknown(self):
        with mock.patch.object(poa, "explorer_get", return_value={"txs": [], "as_of": "t", "checkpoint_height": 94113}):
            r = poa.explorer_find_answer("poa1testaddressnotreal", 16)
        self.assertIsNone(r["found"])
        self.assertEqual(r["status"], "unknown")

    def test_listed_is_found(self):
        with mock.patch.object(poa, "explorer_get", return_value={"txs": [{"type": "challenge", "epoch": 16, "id": "ab"}]}):
            self.assertTrue(poa.explorer_find_answer("poa1testaddressnotreal", 16)["found"])

    def test_explorer_not_found_never_resends_when_challenge_answered(self):
        self.assertTrue(poa.is_challenge_answered({"epoch": 16, "answered": True}, 16))
        with mock.patch.object(poa, "explorer_get", return_value={"txs": []}):
            r = poa.explorer_find_answer("poa1testaddressnotreal", 16)
        self.assertNotEqual(r["found"], False)


class VersionTests(unittest.TestCase):
    def test_version_and_user_agent(self):
        self.assertEqual(poa.KIT_VERSION, "1.8.1")
        self.assertEqual(poa.USER_AGENT, "poa-agent-kit/1.8.1")


if __name__ == "__main__":
    unittest.main()
