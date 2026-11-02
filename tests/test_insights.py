"""Mocked tests for kit 1.9.0: `poa.py insights` over the per-close insight endpoints (release v0.80-insights).
No network, no real key. Run: python3 -m unittest tests/test_insights.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ins", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1" + "A" * 40
OTHER = "poa1" + "B" * 40
ECON = {"estab_intake_floor": 25, "estab_intake_rate": 0.25, "estab_ttl": 26, "estab_waitlist_epochs": 3,
        "grade_quorum": 4, "carry_cap_pools": 1, "emission_carry_from_epoch": 36,
        "final_payout_basis_from_epoch": 36, "pending_ttl_epochs": 16, "panel_size": 7,
        "auto_epoch_seconds": 21600, "K": 5}
FACTS = {"chain_id": "poa-mainnet-1", "open_epoch": 36, "params_sha256": "0f" * 32,
         "system_addresses": [{"label": "v1", "addr": OTHER}], "trusted_graders": [OTHER, OTHER], "economics": ECON}
CLOSE = {"close_epoch": 35, "close_ts": 1793644197, "grade_quorum": 4,
         "establishment": {"established_before": 33, "eligible": 476, "admitted": 5, "waitlisted": 466,
                           "removed_ttl": 0, "removed_slashed": 0},
         "panels": {"grades_cast": 3246, "rows_reaching_quorum": 657, "rows": 46563},
         "concentration": {"distinct_graders": 9, "n_eff_inverse_herfindahl": 5.007, "top_grader_share": 0.243},
         "settled_split": {"passed": 419, "failed": 238, "expired_empty_jury": 2867, "expired_with_seals": 0},
         "settled_count": 3524, "panel_data": "unavailable: node restarted mid-close window"}
EST = {"open_epoch": 36, "computed_at_close": 35, "estab_ttl": 26,
       "current": {"epoch": 36, "size": 2, "members": [
           {"addr": ME, "since_epoch": 29, "last_eligible_epoch": 33, "expires_after_epoch": 59},
           {"addr": OTHER, "since_epoch": 31, "last_eligible_epoch": 31, "expires_after_epoch": 57}]},
       "prior": {"epoch": 35, "size": 1, "members": []},
       "delta": {"added": [OTHER], "removed": []}}
MINE = {"addr": ME, "established": True, "since_epoch": 29, "expires_after_epoch": 59, "in_prior_set": True, "epoch": 36}
RELEASE = {"current": {"version": "v0.80-insights", "params_sha256": "0f" * 32}}


class InsightsReportTests(unittest.TestCase):
    def test_summary_fields_and_no_address(self):
        r = poa.insights_report(CLOSE, FACTS, EST, MINE, dict(ECON), RELEASE)
        self.assertTrue(r["ok"])
        self.assertEqual(r["close"]["close_epoch"], 35)
        self.assertEqual(r["close"]["establishment"]["admitted"], 5)
        self.assertEqual(r["close"]["expired_share"], round(2867 / 3524, 3))
        self.assertEqual(r["established"]["size"], 2)
        self.assertEqual(r["established"]["earliest_expiry_after_epoch"], 57)
        self.assertEqual(r["established"]["added"], 1)
        self.assertEqual(r["established"]["estab_ttl"], 26)
        self.assertEqual(r["you"]["expires_after_epoch"], 59)
        self.assertTrue(r["params_hash_matches_release"])
        self.assertEqual(r["economics_vs_params"], {"differs": [], "not_in_params": []})
        self.assertIn("close_report_panel_data_unavailable", r["flags"])
        blob = json.dumps(r)
        self.assertNotIn(ME, blob)
        self.assertNotIn(OTHER, blob)

    def test_flags_when_params_lag_facts(self):
        params = {"estab_ttl": 6, "pending_ttl_epochs": 16}
        rel = {"current": {"params_sha256": "96" * 32}}
        r = poa.insights_report(None, FACTS, None, {"established": False}, params, rel)
        self.assertIn("facts_economics_differ_from_params", r["flags"])
        self.assertIn("estab_ttl", r["economics_vs_params"]["differs"])
        self.assertIn("estab_intake_floor", r["economics_vs_params"]["not_in_params"])
        self.assertIn("facts_params_hash_differs_from_release", r["flags"])
        self.assertIn("you_not_established", r["flags"])

    def test_unreadable_inputs_flagged(self):
        r = poa.insights_report({"error": "no close-report recorded"}, {"_error": "x"}, None, None)
        self.assertIn("close_report_unreadable", r["flags"])
        self.assertIn("network_facts_unreadable", r["flags"])


class ExpirySettledTests(unittest.TestCase):
    def test_expiry_sums_seal_buckets(self):
        sched = {"as_of_close": 35, "pending_ttl_epochs": 16, "total_pending": 10, "schedule": [
            {"expires_at_close": 36, "by_kind": {"short": {"0": 3, "1": 1, "2": 0, "3+": 1}, "long": {"0": 2}}, "total": 7}]}
        r = poa.expiry_report(sched)
        self.assertEqual(r["rows"][0]["seals"], {"0": 5, "1": 1, "2": 0, "3+": 1})
        self.assertFalse(poa.expiry_report({"error": "x"})["ok"])

    def test_settled_counts_and_own_rows(self):
        st = {"epoch": 35, "count": 3, "rows": [
            {"answer": "ab" * 32, "from": ME, "outcome": "passed", "jury": [1, 2, 3, 4]},
            {"answer": "cd" * 32, "from": OTHER, "outcome": "expired", "jury": []},
            {"answer": "ef" * 32, "from": OTHER, "outcome": "failed", "jury": [1, 2, 3, 4, 5]}]}
        r = poa.settled_report(st, ME)
        self.assertEqual(r["outcomes"], {"passed": 1, "expired": 1, "failed": 1})
        self.assertEqual(r["jury_size_hist"], {"4": 1, "0": 1, "5": 1})
        self.assertEqual(r["yours"], [{"answer": "ab" * 6, "outcome": "passed", "jury": 4}])
        self.assertNotIn(ME, json.dumps(r))


class CmdInsightsTests(unittest.TestCase):
    def run_cmd(self, view, address="", epoch=None):
        calls = []
        routes = {"/api/close-report": CLOSE, "/api/network-facts": FACTS, "/api/established": EST,
                  f"/api/established/{ME}": MINE, "/api/params": dict(ECON), "/api/release": RELEASE}

        def fake(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            calls.append((method, path, query, need_key))
            return routes[path]
        args = argparse.Namespace(view=view, address=address, network=not address, epoch=epoch, json=True)
        out = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(out):
            poa.cmd_insights(args)
        return json.loads(out.getvalue()), calls

    def test_keyless_and_epoch_query(self):
        r, calls = self.run_cmd("summary", address=ME, epoch=35)
        self.assertTrue(r["ok"])
        self.assertTrue(all(c[3] is False for c in calls))
        self.assertIn(("GET", "/api/close-report", {"epoch": 35}, False), calls)
        self.assertIn(f"/api/established/{ME}", [c[1] for c in calls])
        self.assertTrue(r["you"]["established"])

    def test_bad_address_not_queried(self):
        r, calls = self.run_cmd("summary", address="poa1../../hosted/me")
        self.assertFalse(any(c[1].startswith("/api/established/") for c in calls))


if __name__ == "__main__":
    unittest.main()
