"""Mocked tests for kit 1.8.9: `poa.py release`, live intake settings in `intake`, and `intake --what-if --ttl`.
No network, no real key. Run: python3 -m unittest tests/test_release_closeecon.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_rel", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

H = "a" * 64


def rel(version, ts, txid, **over):
    r = {k: H for k in poa.RELEASE_HASH_KEYS}
    r.update(version=version, ts=ts, txid=txid)
    r.update(over)
    return r


PREV = rel("v0.78-closeecon-inert", 1793637596, "7e" * 32, params_sha256="96" * 32)
CUR = rel("v0.79-closeecon-flip", 1793647183, "1b" * 32, params_sha256="0f" * 32,
          changelog_ref="v0.79 close-econ FLIP @ep36: intake 25/0.25, payout-basis+carry@36 cap1, waitlist3, ttl 6->26.")
RELEASE = {"current": CUR, "history": [rel("v0.77-precheck", 1793600000, "a6" * 32, params_sha256="96" * 32), PREV, CUR]}
PARAMS_OLD = {"estab_ttl": 6, "pending_ttl_epochs": 16}
PARAMS_NEW = {"estab_ttl": 26, "pending_ttl_epochs": 16, "estab_intake_floor": 25, "estab_intake_rate": 0.25,
              "estab_waitlist_epochs": 3, "final_payout_basis_from_epoch": 36, "emission_carry_from_epoch": 36,
              "carry_cap_pools": 1}


class ReleaseReportTests(unittest.TestCase):
    def test_params_only_release_with_settings_not_served(self):
        r = poa.release_report(RELEASE, PARAMS_OLD)
        self.assertTrue(r["ok"])
        self.assertEqual(r["current"]["version"], "v0.79-closeecon-flip")
        self.assertEqual(r["current"]["signed_at_utc"], "2026-11-02T19:19:43+00:00")
        self.assertEqual(r["previous"]["version"], "v0.78-closeecon-inert")
        self.assertEqual(r["changed_vs_previous"], ["params_sha256"])
        self.assertTrue(r["params_only_release"])
        self.assertEqual(r["close_econ_served"], [])
        self.assertIn("params_hash_changed", r["flags"])
        self.assertIn("close_econ_settings_not_served", r["flags"])
        ttl = next(x for x in r["close_econ_settings"] if x["key"] == "estab_ttl")
        self.assertEqual(ttl, {"key": "estab_ttl", "served": True, "value": 6})
        floor = next(x for x in r["close_econ_settings"] if x["key"] == "estab_intake_floor")
        self.assertEqual(floor, {"key": "estab_intake_floor", "served": False, "engine_default": "5"})

    def test_settings_served_clears_not_served_flag(self):
        r = poa.release_report(RELEASE, PARAMS_NEW)
        self.assertEqual(r["flags"], ["params_hash_changed"])
        self.assertEqual(set(r["close_econ_served"]), {"estab_intake_floor", "estab_intake_rate", "estab_waitlist_epochs",
                                                       "final_payout_basis_from_epoch", "emission_carry_from_epoch",
                                                       "carry_cap_pools"})
        ttl = next(x for x in r["close_econ_settings"] if x["key"] == "estab_ttl")
        self.assertEqual(ttl["value"], 26)

    def test_never_reads_values_from_changelog(self):
        r = poa.release_report(RELEASE, {})
        self.assertTrue(all(not x["served"] for x in r["close_econ_settings"]))

    def test_bad_response(self):
        self.assertFalse(poa.release_report({"history": []}, {})["ok"])
        self.assertFalse(poa.release_report(None, {})["ok"])

    def test_cmd_release_is_keyless_and_prints(self):
        calls = []

        def fake(method, path, query=None, body=None, need_key=True, timeout=None, retries=None):
            calls.append((method, path, need_key))
            return RELEASE if path == "/api/release" else PARAMS_OLD

        out = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(out):
            poa.cmd_release(argparse.Namespace(json=False))
        self.assertEqual(calls, [("GET", "/api/release", False), ("GET", "/api/params", False)])
        text = out.getvalue()
        self.assertIn("release v0.79-closeecon-flip", text)
        self.assertIn("changed params", text)
        self.assertIn("estab_intake_floor", text)
        self.assertIn("not served (engine default: 5)", text)
        self.assertNotIn("pk_test_fake", text)

    def test_cmd_release_json(self):
        out = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=lambda m, p, **k: RELEASE if p == "/api/release" else PARAMS_NEW), \
                contextlib.redirect_stdout(out):
            poa.cmd_release(argparse.Namespace(json=True))
        self.assertTrue(json.loads(out.getvalue())["ok"])


class IntakeSettingsTests(unittest.TestCase):
    def test_defaults_when_absent(self):
        self.assertEqual(poa.intake_settings({}), {"floor": 5, "rate": 0.10, "waitlist": 0, "served": []})

    def test_served_values(self):
        s = poa.intake_settings(PARAMS_NEW)
        self.assertEqual((s["floor"], s["rate"], s["waitlist"]), (25, 0.25, 3))
        self.assertEqual(len(s["served"]), 3)

    def test_invalid_values_fall_back_like_the_engine(self):
        s = poa.intake_settings({"estab_intake_floor": 0, "estab_intake_rate": 1.5, "estab_waitlist_epochs": True})
        self.assertEqual((s["floor"], s["rate"], s["waitlist"], s["served"]), (5, 0.10, 0, []))

    def test_cap(self):
        self.assertEqual(poa.intake_cap(30, 5, 0.10), 5)
        self.assertEqual(poa.intake_cap(51, 5, 0.10), 6)
        self.assertEqual(poa.intake_cap(215, 25, 0.25), 54)
        self.assertEqual(poa.intake_cap(10, 25, 0.25), 25)


def addrs(prefix, k):
    return [f"poa1{prefix}{i:04d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(k)]


def close(n, eligible=(), admitted=(), removed=()):
    return {"epoch": n, "ts": 1_792_878_384 + n * 21600, "pool": "1000.0", "rewards": {},
            "basis": {"challenge_eligible": list(eligible), "established_after": list(admitted),
                      "established_removed": list(removed)}}


class IntakeLiveSettingsTests(unittest.TestCase):
    def test_intake_report_uses_served_floor(self):
        c1 = addrs("A", 40)
        eps = [close(1, c1, poa.intake_order(c1, 1)[:5]), close(2, addrs("B", 10), [])]
        r = poa.intake_report(eps, "", PARAMS_NEW)
        self.assertEqual(r["intake_settings"]["floor"], 25)
        self.assertEqual(r["rows"][1]["cap_from_records"], 25)
        r0 = poa.intake_report(eps, "", PARAMS_OLD)
        self.assertEqual(r0["rows"][1]["cap_from_records"], 5)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            poa.print_intake(r0)
        self.assertIn("all engine defaults", out.getvalue())


class WhatIfTtlTests(unittest.TestCase):
    def history(self):
        # close 1 admits 5; those 5 never answer again, so with estab_ttl 2 they decay at close 4
        c1 = addrs("A", 5)
        eps = [close(1, c1, c1)]
        for n in range(2, 7):
            eps.append(close(n, addrs(f"N{n}", 3), []))
        return eps

    def test_ttl_override_changes_decay_but_not_baseline(self):
        eps = self.history()
        short = poa.intake_whatif(eps, "", {"estab_ttl": 2}, start=2)
        long_ = poa.intake_whatif(eps, "", {"estab_ttl": 2}, start=2, ttl=26)
        self.assertEqual(short["estab_ttl"], 2)
        self.assertEqual(long_["estab_ttl"], 26)
        self.assertEqual(long_["live_estab_ttl"], 2)
        self.assertGreater(sum(r["removed"] for r in short["rows"]), 0)
        self.assertEqual(sum(r["removed"] for r in long_["rows"]), 0)
        self.assertGreater(long_["established_at_end"], short["established_at_end"])
        self.assertEqual(short["baseline_count_match"], long_["baseline_count_match"])

    def test_ttl_defaults_to_live(self):
        r = poa.intake_whatif(self.history(), "", {"estab_ttl": 26})
        self.assertEqual((r["estab_ttl"], r["live_estab_ttl"]), (26, 26))
        r = poa.intake_whatif(self.history(), "", {}, ttl=0)
        self.assertEqual(r["estab_ttl"], 6)


if __name__ == "__main__":
    unittest.main()
