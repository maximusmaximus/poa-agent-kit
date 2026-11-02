"""Mocked tests for `poa.py intake --what-if` (SUGGESTIONS.md #96). No network, no real key.
Run: python3 -m unittest tests/test_intake_whatif.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_wi", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_792_878_384
H = 21600


def addrs(prefix, k):
    return [f"poa1{prefix}{i:04d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(k)]


def close(n, eligible=(), admitted=(), removed=()):
    return {"epoch": n, "ts": T0 + n * H, "pool": "1000.0", "rewards": {},
            "basis": {"challenge_eligible": list(eligible), "established_after": list(admitted),
                      "established_removed": list(removed)}}


def history():
    """Close 1 admits 5 of 20; close 2 has 30 fresh eligibles, so the 15 left over from close 1 wait."""
    c1 = addrs("A", 19) + [ME]
    adm1 = [a for a in poa.intake_order(c1, 1) if a != ME][:5]
    c2 = addrs("B", 30)
    adm2 = poa.intake_order(c2, 2)[:5]
    return [close(1, c1, adm1), close(2, c2 + adm1, adm2)], c1, adm1, c2


class IntakeWhatIfTests(unittest.TestCase):
    def test_defaults_reproduce_records(self):
        eps, *_ = history()
        r = poa.intake_whatif(eps, ME, {"estab_ttl": 6})
        self.assertTrue(r["ok"])
        self.assertEqual(r["settings"], {"floor": 5, "rate": 0.10, "waitlist": 0, "order": "behind"})
        self.assertEqual([x["admitted"] for x in r["rows"]], [5, 5])
        self.assertEqual(r["baseline_count_match"], "2 of 2")
        self.assertEqual(r["flags"], [])
        self.assertIsNone(r["you"]["admitted_at_close"])

    def test_waitlist_behind_fresh_admits_nobody_when_fresh_fill_the_cap(self):
        eps, *_ = history()
        r = poa.intake_whatif(eps, ME, {}, waitlist=3, order="behind")
        row = r["rows"][1]
        self.assertEqual(row["waitlist"], 15)          # 20 - 5 admitted at close 1 (adm1 re-eligible, already established)
        self.assertEqual(row["from_waitlist"], 0)
        self.assertEqual(r["admitted_from_waitlist"], 0)

    def test_waitlist_ahead_admits_waiting_accounts_first(self):
        eps, c1, adm1, _ = history()
        r = poa.intake_whatif(eps, ME, {}, waitlist=3, order="ahead")
        row = r["rows"][1]
        self.assertEqual(row["from_waitlist"], 5)
        waiters = poa.intake_order(sorted(set(c1) - set(adm1)), 2)
        self.assertEqual(r["you"]["admitted_at_close"] is not None, ME in waiters[:5])
        self.assertEqual(row["me"]["on_waitlist"], True)

    def test_oldest_first_orders_by_wait_start(self):
        c0 = addrs("Z", 6)                     # close 0: 6 eligible, cap 5 -> one waits from close 0
        o0 = poa.intake_order(c0, 0)
        c1 = addrs("Y", 6)                     # close 1: 6 more, nobody admitted beyond 5
        eps = [close(0, c0, o0[:5]), close(1, c1, poa.intake_order(c1, 1)[:5]), close(2, [], [])]
        r = poa.intake_whatif(eps, o0[5], {}, waitlist=3, order="oldest", start=2)
        row = r["rows"][2]
        self.assertTrue(row["me"]["admitted"])
        self.assertEqual(row["me"]["position"], 1)   # waited since close 0, ahead of close-1 leftovers

    def test_floor_and_rate_raise_the_cap(self):
        eps, *_ = history()
        r = poa.intake_whatif(eps, "", {}, floor=12)
        self.assertEqual([x["cap"] for x in r["rows"]], [12, 12])
        self.assertEqual(r["rows"][0]["admitted"], 12)
        big = [close(1, addrs("C", 100), addrs("C", 100)[:5])]
        r2 = poa.intake_whatif(big, "", {}, rate=0.5, start=1)
        self.assertEqual(r2["rows"][0]["cap"], 5)    # 0 established before the first close
        self.assertEqual(r2["settings"]["rate"], 0.5)

    def test_bad_values_fall_back_and_live_params_are_used(self):
        eps, *_ = history()
        r = poa.intake_whatif(eps, "", {"estab_intake_floor": 7, "estab_intake_rate": 2, "estab_waitlist_epochs": 2},
                              rate=-1, order="sideways")
        self.assertEqual(r["live_settings"], {"floor": 7, "rate": 0.10, "waitlist": 2})
        self.assertEqual(r["settings"], {"floor": 7, "rate": 0.10, "waitlist": 2, "order": "behind"})

    def test_auto_start_skips_unreproducible_early_closes(self):
        a = addrs("G", 30)
        eps = [close(1, a, poa.intake_order(a, 1)[:9]),          # genesis-era close admitted 9 (cap can't explain it)
               close(2, addrs("H", 10), poa.intake_order(addrs("H", 10), 2)[:5])]
        r = poa.intake_whatif(eps, "", {})
        self.assertEqual(r["replay_from_close"], 2)
        self.assertTrue(r["replay_start_auto"])
        self.assertFalse(r["rows"][0]["replayed"])
        self.assertEqual(r["rows"][0]["admitted"], 9)
        self.assertEqual(r["baseline_count_match"], "1 of 1")

    def test_ttl_decay_removes_counterfactual_admits(self):
        a = addrs("K", 5)
        eps = [close(0, a, a)] + [close(n) for n in range(1, 9)]
        r = poa.intake_whatif(eps, "", {"estab_ttl": 6}, start=0)
        self.assertEqual(r["rows"][7]["removed"], 5)   # last eligible at 0, removed at the first close > 0 + 6
        self.assertEqual(r["established_at_end"], 0)

    def test_output_never_contains_an_address(self):
        eps, c1, adm1, c2 = history()
        r = poa.intake_whatif(eps, ME, {}, waitlist=3, order="ahead")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_intake_whatif(r)
        blob = json.dumps(r) + buf.getvalue()
        for x in c1 + c2:
            self.assertNotIn(x, blob)

    def test_empty_records(self):
        self.assertFalse(poa.intake_whatif([], ME)["ok"])

    def test_cmd_what_if_uses_mocked_reads_only(self):
        eps, *_ = history()
        calls = []

        def fake_api(method, path, **kw):
            calls.append((method, path, kw.get("need_key", True)))
            return eps if path == "/api/epochs" else {"estab_ttl": 6}

        args = argparse.Namespace(last=0, address=ME, network=False, json=True, timeout=5, what_if=True,
                                  floor=None, rate=None, waitlist=4, order="ahead", from_close=None)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_intake(args)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["what_if"])
        self.assertEqual(out["settings"]["waitlist"], 4)
        self.assertEqual(calls, [("GET", "/api/epochs", False), ("GET", "/api/params", False)])
        self.assertNotIn(ME, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
