"""Mocked tests for `poa.py intake --waitlist-report` (SUGGESTIONS.md #102). No network, no real key.
Run: python3 -m unittest tests/test_waitlist_report.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_wl", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_792_878_384
H = 21600
PARAMS = {"estab_intake_floor": 5, "estab_intake_rate": 0.1, "estab_waitlist_epochs": 3}


def addrs(prefix, k):
    return [f"poa1{prefix}{i:04d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(k)]


def close(n, eligible=(), admitted=(), removed=()):
    return {"epoch": n, "ts": T0 + n * H, "pool": "1000.0", "rewards": {},
            "basis": {"challenge_eligible": list(eligible), "established_after": list(admitted),
                      "established_removed": list(removed)}}


def cr(n, eligible, admitted, waitlisted, seats=0):
    return {"close_epoch": n, "establishment": {"established_before": 0, "eligible": eligible, "admitted": admitted,
                                                "waitlisted": waitlisted, "admitted_with_seats": seats}}


def history(admit_waiter=False):
    """Close 1: 10 pass, 5 admitted (ME left waiting). Close 2: 8 fresh pass; 5 admitted, all fresh
    unless admit_waiter, in which case one close-1 waiter takes the last seat. Close 3: nobody passes."""
    c1 = addrs("A", 9) + [ME]
    adm1 = [a for a in poa.intake_order(c1, 1) if a != ME][:5]
    waiters = [a for a in c1 if a not in adm1 and a != ME]
    c2 = addrs("B", 8)
    adm2 = poa.intake_order(c2, 2)[:4] + ([waiters[0]] if admit_waiter else poa.intake_order(c2, 2)[4:5])
    return [close(1, c1, adm1), close(2, c2, adm2), close(3, [], [])]


class WaitlistReportTests(unittest.TestCase):
    def test_splits_fresh_and_waitlist_and_flags_nobody(self):
        eps = history()
        r = poa.waitlist_report(eps, {1: cr(1, 10, 5, 5), 2: cr(2, 8, 5, 3)}, "", PARAMS)
        self.assertTrue(r["ok"])
        self.assertEqual(r["waitlist_closes"], 3)
        r2 = r["rows"][1]
        self.assertEqual((r2["fresh"], r2["waitlist_carried_in"], r2["admitted_fresh"], r2["admitted_from_waitlist"]),
                         (8, 5, 5, 0))
        self.assertEqual(r2["left_waiting"], 3)
        self.assertTrue(r2["recorded_match"])
        self.assertEqual(r["admitted_from_waitlist_total"], 0)
        self.assertIn("waitlist_admitted_nobody", r["flags"])
        self.assertIn("close_report_missing", r["flags"])   # close 3 not read

    def test_counts_a_waitlist_admission(self):
        eps = history(admit_waiter=True)
        r = poa.waitlist_report(eps, {}, "", PARAMS)
        self.assertEqual(r["rows"][1]["admitted_from_waitlist"], 1)
        self.assertEqual(r["rows"][1]["admitted_fresh"], 4)
        self.assertNotIn("waitlist_admitted_nobody", r["flags"])

    def test_recorded_mismatch_is_flagged(self):
        eps = history()
        r = poa.waitlist_report(eps, {2: cr(2, 8, 5, 7)}, "", PARAMS, last=2)
        self.assertFalse(r["rows"][0]["recorded_match"])
        self.assertIn("recorded_counts_differ", r["flags"])

    def test_your_position_and_closes_left(self):
        eps = history()
        r = poa.waitlist_report(eps, {}, ME, PARAMS)
        y = r["you"]
        self.assertTrue(y["waiting_for_next_close"])
        self.assertEqual(y["last_left_waiting_close"], 1)
        self.assertEqual(y["closes_left_on_waitlist"], 1)        # left at E1, W=3, newest E3
        waiting = set(history()[0]["basis"]["challenge_eligible"]) - set(history()[0]["basis"]["established_after"])
        waiting |= set(addrs("B", 8)) - set(history()[1]["basis"]["established_after"])
        order = poa.intake_order(waiting, 4)
        self.assertEqual(y["waitlist_position"], order.index(ME) + 1)
        self.assertEqual(y["waitlist_size"], len(order))
        self.assertEqual(r["rows"][1]["me"]["on_waitlist"], True)

    def test_waitlist_off_means_no_carry(self):
        eps = history()
        r = poa.waitlist_report(eps, {}, ME, {})
        self.assertEqual(r["waitlist_closes"], 0)
        self.assertEqual(r["rows"][1]["waitlist_carried_in"], 0)
        self.assertNotIn("waitlist_admitted_nobody", r["flags"])
        self.assertFalse(r["you"]["waiting_for_next_close"])

    def test_output_never_contains_an_address(self):
        eps = history()
        r = poa.waitlist_report(eps, {1: cr(1, 10, 5, 5)}, ME, PARAMS)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_waitlist_report(r)
        text = buf.getvalue() + json.dumps(r)
        self.assertNotIn("poa1", text.replace("poa1...", ""))

    def test_empty_records(self):
        self.assertFalse(poa.waitlist_report([], {}, "")["ok"])

    def test_cmd_uses_mocked_keyless_reads_only(self):
        eps = history()
        calls = []

        def fake_api(method, path, query=None, **kw):
            calls.append((method, path, (query or {}).get("epoch"), kw.get("need_key", True)))
            if path == "/api/epochs":
                return eps
            if path == "/api/close-report":
                return cr(query["epoch"], 0, 0, 0)
            return PARAMS

        args = argparse.Namespace(last=2, address=ME, network=False, json=True, timeout=5, what_if=False,
                                  waitlist_report=True, floor=None, rate=None, waitlist=None, order="behind",
                                  from_close=None)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_intake(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["closes"], 2)
        self.assertEqual(calls, [("GET", "/api/epochs", None, False), ("GET", "/api/params", None, False),
                                 ("GET", "/api/close-report", 2, False), ("GET", "/api/close-report", 3, False)])
        self.assertTrue(all(c[3] is False for c in calls))
        self.assertNotIn(ME, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
