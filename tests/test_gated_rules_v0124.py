"""Mocked tests for kit 1.9.22: `poa.py release` reports the three rules release v0.124 switches on from /api/params
(#974 distinct verifiers, #1007 escrow auto-refund, #1419 derived close ts). No network, no real key.
Run: python3 -m unittest tests/test_gated_rules_v0124.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_g124", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

EMPTY_SHA = "258fac13354af75e3260f55f87305970addfc34904be7ee47048ec2ae6f63701"  # served in /api/params on Nov 20 2026
LIVE = {"estab_ttl": 26, "att_distinct_verifier_from_ts": 1795212859, "close_ts_derived_from_epoch": 108,
        "escrow_autorefund_from_epoch": 107, "escrow_autorefund_per_close": 500,
        "escrow_fill_seen_backfill": {"through_ts": 1795193093, "offers": [], "sha256": EMPTY_SHA}}


def rel(version, ts, txid, ps):
    r = {k: "a" * 64 for k in poa.RELEASE_HASH_KEYS}
    r.update(version=version, ts=ts, txid=txid, params_sha256=ps)
    return r


CUR = rel("v0.124-release-1120", 1795216398, "bb" * 32, "a4" * 32)
RELEASE = {"current": CUR, "history": [rel("v0.123-server-1446", 1795140000, "e8" * 32, "bc" * 32), CUR]}


class BackfillShaTests(unittest.TestCase):
    def test_empty_table_matches_served_hash(self):
        self.assertEqual(poa.escrow_backfill_sha([]), EMPTY_SHA)

    def test_order_and_type_do_not_matter(self):
        self.assertEqual(poa.escrow_backfill_sha(["b", "a"]), poa.escrow_backfill_sha(["a", "b"]))
        self.assertNotEqual(poa.escrow_backfill_sha(["a"]), EMPTY_SHA)


class GatedRulesTests(unittest.TestCase):
    def test_all_off_when_not_served(self):
        g = poa.gated_rules_report({"estab_ttl": 26})
        self.assertFalse(any(g[k]["served"] for k in g))

    def test_live_values(self):
        g = poa.gated_rules_report(LIVE, now_ts=1795212859 - 1)
        a = g["att_distinct_verifier"]
        self.assertTrue(a["served"]); self.assertFalse(a["active"])
        self.assertEqual(a["from_utc"], "2026-11-20T22:14:19+00:00")
        self.assertTrue(poa.gated_rules_report(LIVE, now_ts=1795212859)["att_distinct_verifier"]["active"])
        e = g["escrow_autorefund"]
        self.assertEqual((e["from_epoch"], e["per_close"], e["grace_s"]), (107, 500, 86400))
        self.assertTrue(e["per_close_served"]); self.assertFalse(e["grace_served"])
        self.assertEqual(e["backfill_offers"], 0); self.assertTrue(e["backfill_table_ok"])
        c = g["close_ts_derived"]
        self.assertEqual((c["from_epoch"], c["max_late_s"], c["max_late_served"]), (108, 3600, False))

    def test_engine_defaults_for_per_close(self):
        p = dict(LIVE); p.pop("escrow_autorefund_per_close")
        self.assertEqual(poa.gated_rules_report(p)["escrow_autorefund"]["per_close"], 25)

    def test_bad_backfill_table_is_flagged(self):
        p = dict(LIVE, escrow_fill_seen_backfill={"through_ts": 1, "offers": ["x"], "sha256": EMPTY_SHA})
        r = poa.release_report(RELEASE, p)
        self.assertFalse(r["gated_rules"]["escrow_autorefund"]["backfill_table_ok"])
        self.assertIn("escrow_backfill_table_mismatch", r["flags"])

    def test_bool_is_not_an_epoch(self):
        self.assertFalse(poa.gated_rules_report({"close_ts_derived_from_epoch": True})["close_ts_derived"]["served"])


class ReleaseCommandTests(unittest.TestCase):
    def _run(self, as_json):
        calls = []

        def fake(method, path, **kw):
            calls.append(path)
            return RELEASE if path == "/api/release" else LIVE
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(buf):
            poa.cmd_release(argparse.Namespace(json=as_json))
        return buf.getvalue(), calls

    def test_text_output_lists_rules(self):
        out, calls = self._run(False)
        self.assertEqual(calls, ["/api/release", "/api/params"])
        self.assertIn("gated rules in /api/params (engine v0.124):", out)
        self.assertIn("from close E107", out)
        self.assertIn("table hash ok", out)
        self.assertIn("from close E108", out)
        self.assertNotIn("pk_test_fake", out)

    def test_json_output(self):
        out, _ = self._run(True)
        r = json.loads(out)
        self.assertNotIn("escrow_backfill_table_mismatch", r["flags"])
        self.assertTrue(r["gated_rules"]["close_ts_derived"]["served"])


if __name__ == "__main__":
    unittest.main()
