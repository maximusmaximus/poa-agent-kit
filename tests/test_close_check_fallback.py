"""Mocked tests for close-check --via-settled / automatic fallback (SUGGESTIONS.md #131).
No network, no real key. Run: python3 -m unittest tests/test_close_check_fallback.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_ccf", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERADDRESSyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"


def cr(n):
    return {"close_epoch": n, "establishment": {"established_before": 10, "eligible": 5, "admitted": 2,
            "waitlisted": 0, "removed_ttl": 1, "removed_slashed": 0},
            "settled_split": {"passed": 3, "failed": 1, "expired_empty_jury": 0, "expired_with_seals": 0}, "settled_count": 4}


def st(n, outcome):
    return {"epoch": n, "count": 2, "rows": [
        {"answer": "ab" * 32, "from": ME, "outcome": outcome, "jury": [{"grader": OTHER}] * 3},
        {"answer": "cd" * 32, "from": OTHER, "outcome": "passed", "jury": []}]}


def ns(**kw):
    base = dict(address=ME, last=2, log="", tri_cap=False, json=True, timeout=5, via_settled=False)
    base.update(kw)
    return argparse.Namespace(**base)


def fake_api(epochs_down=True, settled_outcome="passed"):
    calls = []

    def f(method, path, query=None, **kw):
        calls.append(path)
        if path == "/api/epochs":
            raise RuntimeError("HTTP 503 temporarily paused")
        if path == "/api/close-report":
            return cr((query or {}).get("epoch", 61))
        if path == "/api/settled":
            return st((query or {}).get("epoch"), settled_outcome)
        raise AssertionError("unexpected path " + path)
    f.calls = calls
    return f


def run(args, api):
    buf = io.StringIO()
    code = 0
    with mock.patch.object(poa, "api_call", api), contextlib.redirect_stdout(buf):
        try:
            poa.cmd_close_check(args)
        except SystemExit as e:
            code = e.code
    return code, buf.getvalue()


class Pure(unittest.TestCase):
    def test_passed_and_unavailable_listed(self):
        r = poa.close_check_fallback({61: cr(61)}, {61: st(61, "passed")}, ME)
        self.assertTrue(r["ok"]); self.assertEqual(r["verdict"], "passed-at-newest")
        self.assertEqual(r["closes"][0]["yours"][0]["jury"], 3)
        self.assertTrue(any("per-address rewards" in u for u in r["unavailable"]))
        self.assertNotIn(ME, json.dumps(r))

    def test_failed_and_no_row(self):
        self.assertEqual(poa.close_check_fallback({61: cr(61)}, {61: st(61, "failed")}, ME)["verdict"], "no-pass-at-newest")
        self.assertEqual(poa.close_check_fallback({61: cr(61)}, {61: st(61, "failed")}, "poa1NOBODY")["verdict"], "no-row-at-newest")

    def test_unreadable_not_zero(self):
        r = poa.close_check_fallback({61: None}, {61: None}, ME)
        self.assertFalse(r["ok"])
        r = poa.close_check_fallback({61: cr(61), 60: None}, {61: None, 60: st(60, "passed")}, ME)
        self.assertEqual(r["verdict"], "unknown")
        c60 = [c for c in r["closes"] if c["close"] == 60][0]
        self.assertFalse(c60["close_report"]); self.assertNotIn("settled_split", c60)


class Cmd(unittest.TestCase):
    def test_auto_fallback_when_epochs_paused(self):
        api = fake_api()
        code, out = run(ns(), api)
        d = json.loads(out)
        self.assertEqual(code, 0); self.assertEqual(d["via"], "settled+close-report")
        self.assertIn("503", d["epochs_error"]); self.assertEqual(d["newest_close"], 61)
        self.assertEqual([c["close"] for c in d["closes"]], [61, 60])

    def test_not_passed_exit3(self):
        code, _ = run(ns(), fake_api(settled_outcome="failed"))
        self.assertEqual(code, 3)

    def test_via_settled_skips_epochs(self):
        api = fake_api()
        code, out = run(ns(via_settled=True, json=False), api)
        self.assertNotIn("/api/epochs", api.calls)
        self.assertIn("via=settled+close-report", out); self.assertIn("unavailable on this path", out)
        self.assertNotIn(ME, out)

    def test_everything_down_exit2(self):
        def dead(*a, **k): raise RuntimeError("down")
        code, _ = run(ns(), dead)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
