"""Mocked tests for `poa.py intake` (SUGGESTIONS.md #70). No network, no real key.
Run: python3 -m unittest tests/test_intake.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_in", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
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


class IntakeTests(unittest.TestCase):
    def test_order_is_sha256_of_intake_address_epoch(self):
        import hashlib
        c = addrs("A", 20)
        o = poa.intake_order(c, 7)
        self.assertEqual(o, sorted(c, key=lambda a: hashlib.sha256(f"intake:{a}:7".encode()).hexdigest()))
        self.assertNotEqual(o, poa.intake_order(c, 8))  # each close is a fresh draw

    def test_rank_gap_and_admission(self):
        pool = addrs("B", 30) + [ME]
        order = poa.intake_order(pool, 3)
        admitted = order[:2] + order[3:6]   # rank 3 skipped (already established via genesis carry)
        eps = [close(2), close(3, eligible=pool, admitted=admitted)]
        r = poa.intake_report(eps, ME, {"estab_ttl": 6})
        row = r["rows"][-1]
        self.assertEqual(row["candidates"], 31)
        self.assertEqual(row["admitted_ranks"], [1, 2, 4, 5, 6])
        self.assertEqual(row["gaps"], [3])
        self.assertTrue(row["matches_order"])
        me_rank = order.index(ME) + 1
        self.assertEqual(row["me"]["rank"], me_rank)
        self.assertEqual(row["me"]["of"], 31)
        self.assertEqual(row["me"]["admitted"], me_rank in (1, 2, 4, 5, 6))

    def test_admitted_then_lapse_projection(self):
        pool = addrs("C", 6) + [ME]
        eps = [close(1, eligible=pool, admitted=[ME]), close(2), close(3, eligible=[ME]), close(4)]
        r = poa.intake_report(eps, ME, {"estab_ttl": 6})
        self.assertTrue(r["you"]["established_per_records"])
        self.assertEqual(r["you"]["admitted_at_close"], 1)
        self.assertEqual(r["you"]["last_eligible_close"], 3)
        self.assertEqual(r["you"]["established_through_close"], 9)
        self.assertEqual(r["you"]["lapses_at_close"], 10)
        self.assertEqual(r["you"]["closes_left_before_lapse"], 5)
        # already established accounts are not candidates in later closes
        self.assertFalse(r["rows"][2]["me"]["candidate"])
        self.assertTrue(r["rows"][2]["me"]["already_established"])
        self.assertNotIn("standing_lapses_soon", r["flags"])

    def test_removed_and_not_drawn_flags(self):
        pool = addrs("D", 40) + [ME]
        order = poa.intake_order(pool, 5)
        others = [a for a in order if a != ME][:5]
        eps = [close(5, eligible=pool, admitted=others)]
        r = poa.intake_report(eps, ME)
        self.assertFalse(r["you"]["established_per_records"])
        self.assertIn("candidate_but_not_drawn", r["flags"])
        eps2 = [close(1, eligible=[ME], admitted=[ME]), close(8, removed=[ME])]
        r2 = poa.intake_report(eps2, ME, {"estab_ttl": 6})
        self.assertFalse(r2["you"]["established_per_records"])
        self.assertEqual(r2["you"]["removed_at_close"], 8)
        self.assertTrue(r2["rows"][-1]["me"]["removed"])

    def test_output_never_contains_an_address(self):
        pool = addrs("E", 12) + [ME]
        eps = [close(1, eligible=pool, admitted=poa.intake_order(pool, 1)[:5])]
        r = poa.intake_report(eps, ME)
        blob = json.dumps(r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_intake(r)
        for a in pool:
            self.assertNotIn(a, blob)
            self.assertNotIn(a, buf.getvalue())

    def test_empty_records(self):
        self.assertFalse(poa.intake_report([], ME)["ok"])

    def test_cmd_uses_mocked_reads_only(self):
        pool = addrs("F", 10) + [ME]
        eps = [close(1, eligible=pool, admitted=poa.intake_order(pool, 1)[:5])]
        calls = []

        def fake_api(method, path, **kw):
            calls.append((method, path, kw.get("need_key", True)))
            return eps if path == "/api/epochs" else {"estab_ttl": 6}

        args = argparse.Namespace(last=0, address=ME, network=False, json=True, timeout=5)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_intake(args)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("GET", "/api/epochs", False), ("GET", "/api/params", False)])
        self.assertNotIn(ME, buf.getvalue())

    def test_cmd_network_mode_has_no_you(self):
        eps = [close(1, eligible=addrs("G", 6), admitted=addrs("G", 6)[:5])]
        args = argparse.Namespace(last=1, address="", network=True, json=True, timeout=5)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=lambda m, p, **k: eps if p == "/api/epochs" else {}), \
             mock.patch.object(poa, "local_address", side_effect=AssertionError("address read")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_intake(args)
        out = json.loads(buf.getvalue())
        self.assertNotIn("you", out)
        self.assertNotIn("me", out["rows"][0])


if __name__ == "__main__":
    unittest.main()
