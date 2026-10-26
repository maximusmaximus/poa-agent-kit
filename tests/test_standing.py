"""Mocked tests for `poa.py standing` (SUGGESTIONS.md #60). No network, no real key.
Run: python3 -m unittest tests/test_standing.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_sd", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
H = 21600
T0 = 1_792_878_384


def close(n, ts, rewards=None, settled=(), pending=None, eligible=(), est_after=(), removed=(), attest=None):
    return {"epoch": n, "ts": ts, "pool": "1000.0", "rewards": rewards or {}, "challenges": {},
            "basis": {"attestations": attest or {}, "challenge_eligible": list(eligible),
                      "established_after": list(est_after), "established_removed": list(removed),
                      "slashed": [], "settled": list(settled), "pending": pending or {}}}


def jury(aligned, total):
    return [{"grader": f"g{i}", "aligned": i < aligned} for i in range(total)]


class StandingTests(unittest.TestCase):
    def eps(self):
        st6 = [{"answer": "aaaa1111bbbb2222", "from": ME, "passed": True, "epoch": 1, "jury": jury(3, 4)},
               {"answer": "cccc3333dddd4444", "from": OTHER, "passed": True, "epoch": 2, "jury": jury(4, 4)}]
        return [close(5, T0), close(6, T0 + H, rewards={ME: "12.5"}, settled=st6, eligible=[ME, OTHER],
                                    est_after=[OTHER], attest={ME: 1},
                                    pending={"eeee5555ffff6666": {"panel": [], "grades": [], "since": 6}})]

    def test_passed_and_eligible_but_not_established(self):
        r = poa.standing_report(self.eps(), {"aaaa1111bbbb": 1, "eeee5555ffff": 6}, ME)
        row = r["rows"][-1]
        self.assertEqual(row["passed"], 1)
        self.assertEqual(row["settled"][0]["aligned"], 3)
        self.assertEqual(row["settled"][0]["jury"], 4)
        self.assertEqual(row["settled"][0]["born_epoch"], 1)
        self.assertTrue(row["eligible"] and row["attested"] and row["paid"])
        self.assertFalse(row["established_after"])
        self.assertIn("passed_and_eligible_but_not_established", row["flags"])
        self.assertEqual(row["network"], {"passed_authors": 2, "passed_and_eligible": 2, "established_after": 1,
                                          "passed_eligible_established": 1, "established_removed": 0})
        self.assertEqual(r["verdict"], "passed-not-established")
        self.assertEqual(r["answers_pending"], 1)
        self.assertIn("passed_but_not_established", r["flags"])

    def test_me_overrides_and_disagreement_flag(self):
        r = poa.standing_report(self.eps(), {"aaaa1111bbbb": 1}, ME, me_res={"established": True})
        self.assertEqual(r["verdict"], "established")
        self.assertIsNone(r["established_per_records"])
        self.assertNotIn("me_disagrees_with_close_records", r["flags"])
        eps = self.eps()
        eps[-1]["basis"]["established_after"].append(ME)
        r2 = poa.standing_report(eps, {"aaaa1111bbbb": 1}, ME, me_res={"established": False})
        self.assertIn("me_disagrees_with_close_records", r2["flags"])

    def test_passed_but_not_eligible_and_last(self):
        eps = self.eps()
        eps[-1]["basis"]["challenge_eligible"] = [OTHER]
        r = poa.standing_report(eps, {"aaaa1111bbbb": 1}, ME, last=1)
        self.assertEqual(len(r["rows"]), 1)
        self.assertIn("passed_but_not_eligible", r["rows"][0]["flags"])

    def test_empty_records(self):
        self.assertFalse(poa.standing_report([], {}, ME)["ok"])

    def test_output_never_contains_address(self):
        r = poa.standing_report(self.eps(), {"aaaa1111bbbb": 1}, ME)
        self.assertNotIn(ME, json.dumps(r))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_standing(r)
        self.assertNotIn(ME, buf.getvalue())
        self.assertIn("aligned 3/4", buf.getvalue())

    def _run(self, no_me, me_payload):
        calls = []
        def fake_api(method, path, **k):
            calls.append(path)
            if path == "/api/epochs":
                return self.eps()
            if path == "/api/hosted/me":
                return me_payload
            raise AssertionError(f"unexpected call {path}")
        args = argparse.Namespace(timeout=5, address=ME, log="/nonexistent", json=True, last=0, no_me=no_me)
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa, "explorer_get", return_value={"txs": [{"type": "challenge", "id": "aaaa1111bbbb2222", "epoch": 1}]}), \
             mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                poa.cmd_standing(args)
                raise SystemExit(0)
        return cm.exception.code, json.loads(buf.getvalue()), calls

    def test_cli_keyless_and_keyed(self):
        code, out, calls = self._run(True, None)
        self.assertEqual(code, 3)
        self.assertNotIn("/api/hosted/me", calls)
        self.assertIsNone(out["established_per_me"])
        code, out, calls = self._run(False, {"established": True})
        self.assertEqual(code, 0)
        self.assertIn("/api/hosted/me", calls)
        self.assertEqual(out["verdict"], "established")


if __name__ == "__main__":
    unittest.main()
