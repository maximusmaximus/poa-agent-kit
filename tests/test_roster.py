"""Mocked tests for `poa.py standing --roster` (SUGGESTIONS.md #111). No network, no real key.
Run: python3 -m unittest tests/test_roster.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ro", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
GRADER = "poa1GRADERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_792_878_384
H = 21600
PARAMS = {"estab_intake_floor": 5, "estab_intake_rate": 0.1, "estab_waitlist_epochs": 3}


def close(n, eligible=(), admitted=(), removed=(), graders=None):
    return {"epoch": n, "ts": T0 + n * H, "pool": "1000.0", "rewards": {},
            "basis": {"challenge_eligible": list(eligible), "established_after": list(admitted),
                      "established_removed": list(removed),
                      "graders_served": dict(graders or {})}}


def cr(n, seatless=None, seatless_count=None, panel_data="in-memory", est_before=0, waitlisted=0):
    out = {"close_epoch": n, "panel_data": panel_data,
           "establishment": {"established_before": est_before, "eligible": 0, "admitted": 0,
                             "waitlisted": waitlisted, "admitted_with_seats": 0},
           "seatless": seatless, "seatless_count": seatless_count}
    return out


class RosterTests(unittest.TestCase):
    def history(self):
        # E1: ME passes, not admitted → left waiting / later waitlist. OTHER admitted.
        # E2: ME on waitlist (carried); still not admitted. GRADER on prior roster after E1? no - OTHER is.
        # E3: ME on seatless list; panel ok. GRADER served.
        return [
            close(1, eligible=[ME, OTHER], admitted=[OTHER]),
            close(2, eligible=[GRADER], admitted=[GRADER], graders={OTHER: 2}),
            close(3, eligible=[ME], admitted=[], graders={GRADER: 1}),
        ]

    def reports(self):
        return {
            1: cr(1, seatless=[ME], seatless_count=1, est_before=0),
            2: cr(2, seatless=[ME], seatless_count=1, est_before=1),
            3: cr(3, seatless=[ME], seatless_count=1, est_before=2),
        }

    def test_flags_prior_seatless_waitlist_admitted_graders(self):
        r = poa.roster_report(self.history(), self.reports(), ME, PARAMS)
        self.assertTrue(r["ok"])
        e1, e2, e3 = r["rows"]
        self.assertFalse(e1["prior_roster"])
        self.assertTrue(e1["seatless"])
        self.assertTrue(e1["left_waiting"])
        self.assertTrue(e1["waitlisted"])
        self.assertFalse(e1["admitted"])
        self.assertFalse(e1["graders_served"])
        # E2: ME carried on waitlist, still not prior
        self.assertFalse(e2["prior_roster"])
        self.assertTrue(e2["on_waitlist"])
        self.assertTrue(e2["waitlisted"])
        self.assertFalse(e2["admitted"])
        # E3: still seatless, not prior
        self.assertTrue(e3["seatless"])
        self.assertEqual(r["you"]["lane"], "never-dealt-or-seatless")
        self.assertIn("seatless_lane", r["flags"])

    def test_grader_on_roster(self):
        r = poa.roster_report(self.history(), self.reports(), GRADER, PARAMS, last=2)
        # After E2 admit, at E3 GRADER is prior and graders_served
        e3 = r["rows"][-1]
        self.assertTrue(e3["prior_roster"])
        self.assertTrue(e3["graders_served"])
        self.assertEqual(e3["graders_served_seats"], 1)
        self.assertFalse(e3["seatless"])

    def test_seatless_unknown_when_panel_data_unavailable(self):
        reps = {3: cr(3, seatless=None, seatless_count=None,
                      panel_data="unavailable: dealt panels not captured for this close")}
        r = poa.roster_report(self.history(), reps, ME, PARAMS, last=1)
        row = r["rows"][0]
        self.assertIsNone(row["seatless"])
        self.assertFalse(row["seatless_known"])
        self.assertIn("seatless_unknown", row["flags"])
        self.assertIn("panel_data_unavailable", row["flags"])
        self.assertIn("seatless_unknown_in_window", r["flags"])

    def test_missing_close_report_is_unknown(self):
        r = poa.roster_report(self.history(), {}, ME, PARAMS, last=1)
        self.assertIsNone(r["rows"][0]["seatless"])
        self.assertFalse(r["rows"][0]["seatless_known"])

    def test_output_never_contains_address(self):
        r = poa.roster_report(self.history(), self.reports(), ME, PARAMS)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_roster(r)
        text = buf.getvalue() + json.dumps(r)
        self.assertNotIn(ME, text)
        self.assertNotIn(OTHER, text)
        self.assertNotIn(GRADER, text)

    def test_empty_records(self):
        self.assertFalse(poa.roster_report([], {}, ME)["ok"])

    def test_cmd_uses_mocked_keyless_reads_only(self):
        eps = self.history()
        calls = []

        def fake_api(method, path, query=None, **kw):
            calls.append((method, path, (query or {}).get("epoch"), kw.get("need_key", True)))
            if path == "/api/epochs":
                return eps
            if path == "/api/close-report":
                return self.reports().get(query["epoch"], cr(query["epoch"]))
            if path == "/api/params":
                return PARAMS
            raise AssertionError(f"unexpected {path}")

        args = argparse.Namespace(timeout=5, address=ME, log="/nonexistent", json=True, last=2,
                                  no_me=True, roster=True)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")):
            with contextlib.redirect_stdout(buf):
                poa.cmd_standing(args)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["closes"], 2)
        # only keyless reads
        for method, path, epoch, need_key in calls:
            self.assertFalse(need_key)
            self.assertIn(path, ("/api/epochs", "/api/params", "/api/close-report"))
        self.assertNotIn(ME, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
