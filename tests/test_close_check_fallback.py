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
    base = dict(address=ME, last=2, log="", tri_cap=False, json=True, timeout=5, via_settled=False, authored="", states=False)
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


class States(unittest.TestCase):
    """SUGGESTIONS #143 three-state settlement report."""

    def test_three_buckets_and_late_settled(self):
        reports = {70: cr(70), 69: cr(69), 68: cr(68)}
        # E70 sealed but absent from settled; E69 judged-pass at same close;
        # E68 sealed, appears in E69 settled (late); E48 panel-0.
        settled = {
            70: {"epoch": 70, "count": 1, "rows": [
                {"answer": "zz" * 32, "from": OTHER, "outcome": "passed", "jury": []}]},
            69: {"epoch": 69, "count": 2, "rows": [
                {"answer": "b1" * 32, "from": ME, "outcome": "passed", "jury": [{"grader": OTHER}] * 5},
                {"answer": "c1" * 32, "from": ME, "outcome": "passed", "jury": [{"grader": OTHER}] * 4}]},
            68: {"epoch": 68, "count": 0, "rows": []},
        }
        authored = {
            70: [{"answer": "a1" * 32, "sealed": True, "panel": 4, "seals": 4}],
            69: [{"answer": "b1" * 32, "sealed": True, "panel": 5, "seals": 5}],
            68: [{"answer": "c1" * 32, "sealed": True, "panel": 4, "seals": 4}],
            48: [{"answer": "d1" * 32, "sealed": False, "panel": 0, "seals": 3}],
        }
        r = poa.close_check_fallback(reports, settled, ME, authored=authored)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verdict"], "no-row-at-newest")
        by = {s["close"]: s for s in r["states"]}
        self.assertEqual(by[70]["state"], "sealed-not-settled")
        self.assertEqual(by[69]["state"], "judged-pass")
        self.assertEqual(by[69]["jury"], 5)
        self.assertEqual(by[68]["state"], "late-settled")
        self.assertEqual(by[68]["settled_in"], 69)
        self.assertEqual(by[48]["state"], "panel-0")
        self.assertEqual(r["state_counts"]["sealed-not-settled"], 1)
        self.assertEqual(r["state_counts"]["panel-0"], 1)
        self.assertNotIn(ME, json.dumps(r))

    def test_judged_fail(self):
        authored = {61: [{"answer": "ab" * 32, "sealed": True, "panel": 3}]}
        r = poa.close_check_fallback({61: cr(61)}, {61: st(61, "failed")}, ME, authored=authored)
        self.assertEqual(r["states"][0]["state"], "judged-fail")
        self.assertEqual(r["verdict"], "no-pass-at-newest")

    def test_cmd_authored_file_no_address(self):
        authored = {61: [{"answer": "ab" * 32, "sealed": True, "panel": 3}]}
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        pathlib.Path(path).write_text(json.dumps(authored))
        try:
            code, out = run(ns(via_settled=True, json=True, authored=path), fake_api())
            d = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual(d["states"][0]["state"], "judged-pass")
            self.assertNotIn(ME, out)
        finally:
            os.unlink(path)

    def test_no_authored_has_no_states_key(self):
        r = poa.close_check_fallback({61: cr(61)}, {61: st(61, "passed")}, ME)
        self.assertNotIn("states", r)
