"""Mocked tests for `poa.py close-check --tri-cap` (SUGGESTIONS.md #119).
No network, no real key. Run: python3 -m unittest tests/test_tri_cap.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_tc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
T0 = 1_792_878_384
H = 21600


def close(n, ts, pending=None, settled=None):
    return {
        "epoch": n,
        "ts": ts,
        "pool": "1000.0",
        "rewards": {},
        "challenges": {},
        "basis": {
            "attestations": {},
            "challenge_eligible": [],
            "established_after": [],
            "established_removed": [],
            "slashed": [],
            "settled": list(settled or []),
            "pending": dict(pending or {}),
        },
    }


def pend(since, grades, panel=None, scores=None):
    """grades: int count; scores optional list overriding default score=2."""
    if scores is None:
        scores = [2] * grades
    g = [{"grader": f"poa1G{i}", "score": scores[i], "ts": T0 + i} for i in range(grades)]
    return {"kind": "short", "panel": list(panel or []), "since": since, "grades": g}


class TriCapTests(unittest.TestCase):
    def e50_like(self):
        # Shape like E50: lots of zero/1/2, 3 tri-capped (one with panel, some unanimous), settled juries ≥4
        pending = {
            "aaa000000001" + "0" * 52: pend(35, 0),
            "aaa000000002" + "0" * 52: pend(40, 1),
            "aaa000000003" + "0" * 52: pend(41, 2),
            "bbbTRI000001" + "0" * 52: pend(35, 3, panel=[], scores=[2, 2, 2]),  # unanimous, no panel
            "bbbTRI000002" + "0" * 52: pend(36, 3, panel=["poa1P"], scores=[3, 2, 1]),  # with panel, not unan
            "bbbTRI000003" + "0" * 52: pend(37, 3, panel=[], scores=[1, 1, 1]),  # unanimous
            "ownTRI000001" + "0" * 52: pend(38, 3, panel=[], scores=[2, 2, 2]),  # OUR tri-capped
            "ownSHORT0001" + "0" * 52: pend(39, 1, panel=[]),  # OUR seals-but-short
        }
        settled = [
            {"answer": "settled000001" + "0" * 52, "from": OTHER, "passed": True, "jury": [{}] * 4},
            {"answer": "settled000002" + "0" * 52, "from": OTHER, "passed": True, "jury": [{}] * 6},
        ]
        return [close(49, T0, pending={"x" + "0" * 63: pend(30, 3)}),
                close(50, T0 + H, pending=pending, settled=settled)]

    def test_counts_tri_cap_split_and_unanimous(self):
        eps = self.e50_like()
        answers = {38: "ownTRI000001", 39: "ownSHORT0001"}
        r = poa.tri_cap_report(eps, answers, ME, quorum=4, last=1)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "tri-cap")
        row = r["rows"][0]
        self.assertEqual(row["close"], 50)
        self.assertEqual(row["pending_total"], 8)
        self.assertEqual(row["zero_grade"], 1)
        # grades: 0,1,2,3,3,3,3,1 -> short = 1,2,3,3,3,3,1 = 7? 0<g<4 so all except zero = 7
        self.assertEqual(row["seals_but_short"], 7)
        self.assertEqual(row["tri_capped"], 4)
        self.assertEqual(row["tri_capped_with_panel"], 1)
        self.assertEqual(row["tri_capped_panel_pct"], 25.0)
        self.assertEqual(row["tri_capped_unanimous"], 3)  # three unanimous triples
        self.assertEqual(row["never_dealt_panel_empty"], 7)  # only one has panel
        self.assertEqual(row["settled_min_jury"], 4)
        self.assertIn("tri_cap_is_quorum_boundary", row["flags"])
        self.assertEqual(r["verdict"], "own-tri-capped")
        self.assertEqual(row["own_in_tri_cap"], 1)
        own = {o["txid"]: o for o in row["own_pending"]}
        self.assertTrue(own["ownTRI000001"]["tri_capped"])
        self.assertTrue(own["ownTRI000001"]["unanimous"])
        self.assertTrue(own["ownSHORT0001"]["seals_but_short"])
        self.assertFalse(own["ownSHORT0001"]["tri_capped"])

    def test_no_own_tri_cap_verdict(self):
        eps = self.e50_like()
        r = poa.tri_cap_report(eps, {40: "aaa000000002"}, ME, last=1)
        self.assertEqual(r["verdict"], "tri-cap-void")
        self.assertEqual(r["own_in_tri_cap_newest"], 0)
        # own has 1 grade, not tri
        self.assertEqual(r["rows"][0]["own_pending"][0]["grades"], 1)

    def test_empty_epochs(self):
        self.assertFalse(poa.tri_cap_report([], {}, ME)["ok"])

    def test_output_never_contains_address(self):
        eps = self.e50_like()
        r = poa.tri_cap_report(eps, {38: "ownTRI000001"}, ME, last=2)
        out = json.dumps(r)
        self.assertNotIn(ME, out)
        self.assertNotIn(OTHER, out)
        # grader addresses from fixtures also should not appear? We strip to counts only;
        # pending graders are not copied into output — only counts. Good.
        self.assertNotIn("poa1G0", out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_tri_cap(r)
        text = buf.getvalue()
        self.assertNotIn(ME, text)
        self.assertIn("TRI-CAPPED", text)
        self.assertIn("close-check --tri-cap", text)

    def test_cli_tri_cap_exit_codes(self):
        eps = self.e50_like()
        args = argparse.Namespace(
            timeout=5, address=ME, log="/nonexistent", json=True, last=1, tri_cap=True
        )
        with mock.patch.object(
            poa, "api_call",
            side_effect=lambda m, path, **k: eps if path == "/api/epochs" else {"grade_quorum": 4},
        ), mock.patch.object(
            poa, "explorer_get",
            return_value={"txs": [{"type": "challenge", "id": "ownTRI000001", "epoch": 38}]},
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                poa.cmd_close_check(args)
            self.assertEqual(cm.exception.code, 3)  # own in void
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["mode"], "tri-cap")
            self.assertNotIn(ME, buf.getvalue())

        # no own tri-cap -> exit 0
        with mock.patch.object(
            poa, "api_call",
            side_effect=lambda m, path, **k: eps if path == "/api/epochs" else {"grade_quorum": 4},
        ), mock.patch.object(poa, "explorer_get", return_value={"txs": []}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_close_check(args)  # no SystemExit
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["verdict"], "tri-cap-void")
            self.assertEqual(payload["own_in_tri_cap_newest"], 0)

    def test_helpers(self):
        self.assertEqual(poa._pending_grade_count({"grades": [1, 2, 3]}), 3)
        self.assertEqual(poa._pending_grade_count({"grades": {"a": 1}}), 1)
        self.assertEqual(poa._pending_panel_len({"panel": ["x", "y"]}), 2)
        self.assertTrue(poa._grades_unanimous({"grades": [{"score": 2}, {"score": 2}]}))
        self.assertFalse(poa._grades_unanimous({"grades": [{"score": 2}, {"score": 1}]}))


if __name__ == "__main__":
    unittest.main()
