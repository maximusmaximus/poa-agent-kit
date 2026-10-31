"""Mocked tests for SUGGESTIONS.md #88: empty-rewards closes and missing basis keys are labelled
network-wide in `close-health` and `close-check` (no network, no real key).
Run: python3 -m unittest tests/test_empty_close.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ec", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
H = 21600
FULL_BASIS_KEYS = ("final_payout", "ceiling", "referral2", "spotcheck")


def close(n, ts, paid=True, full_basis=True, attesters=20):
    b = {"attestations": {f"poa1att{i}": 3 for i in range(attesters)},
         "challenge_eligible": [f"e{i}" for i in range(30)] + [ME],
         "established_after": [], "established_removed": [], "slashed": [], "ejected": [],
         "settled": [], "pending": {f"p{i}": {"since": 1} for i in range(5)}}
    if full_basis:
        b.update({"final_payout": {"burned": "10.0"}, "ceiling": {}, "referral2": {}, "spotcheck": {}})
    return {"epoch": n, "ts": 1000 + n * H, "pool": "1000.0",
            "rewards": {ME: "12.5", "poa1other": "3.0"} if paid else {},
            "weights": {ME: 1} if paid else {}, "challenges": {ME: 1} if paid else {}, "basis": b}


NORMAL = [close(0, 0), close(1, 0), close(2, 0)]
EMPTY = NORMAL + [close(3, 0, paid=False, full_basis=False)]


class EmptyCloseTests(unittest.TestCase):
    def test_labels_pure(self):
        eps = poa._epoch_list(EMPTY)
        net = poa.close_network_labels(eps, 3)
        self.assertTrue(net["empty_rewards"])
        self.assertEqual(net["missing_basis_keys"], sorted(FULL_BASIS_KEYS))
        self.assertTrue(net["network_wide"])
        ok = poa.close_network_labels(eps, 2)
        self.assertEqual((ok["empty_rewards"], ok["missing_basis_keys"], ok["network_wide"]), (False, [], False))

    def test_zero_valued_rewards_still_empty(self):
        e = close(3, 0, paid=False)
        e["rewards"] = {ME: "0.000000"}
        net = poa.close_network_labels(poa._epoch_list(NORMAL + [e]), 3)
        self.assertTrue(net["empty_rewards"])
        self.assertEqual(net["missing_basis_keys"], [])

    def test_genesis_previous_close_not_compared(self):
        eps = poa._epoch_list([close(0, 0, full_basis=False), close(1, 0)])
        self.assertEqual(poa.close_network_labels(eps, 1)["missing_basis_keys"], [])

    def test_close_health_network_empty_verdict(self):
        r = poa.close_health(EMPTY, k=5, address=ME)
        self.assertEqual(r["verdict"], "network-empty")
        self.assertIn("empty_rewards", r["flags"])
        self.assertIn("basis_keys_missing", r["flags"])
        self.assertEqual(r["missing_basis_keys"], sorted(FULL_BASIS_KEYS))
        self.assertTrue(r["network_wide"])
        self.assertEqual(poa.close_health(NORMAL, k=5)["verdict"], "healthy")

    def test_halted_like_wins_over_empty(self):
        r = poa.close_health(NORMAL + [close(3, 0, paid=False, attesters=0)], k=5)
        self.assertEqual(r["verdict"], "halted-like")
        self.assertIn("empty_rewards", r["flags"])

    def test_close_check_row_and_verdict(self):
        r = poa.close_check(EMPTY, {3: "abcdabcd1234"}, ME, last=2)
        prev, newest = r["rows"]
        self.assertNotIn("network_empty_close", prev["flags"])
        self.assertIn("not_paid", newest["flags"])
        self.assertIn("network_empty_close", newest["flags"])
        self.assertIn("basis_keys_missing", newest["flags"])
        self.assertEqual(newest["missing_basis_keys"], sorted(FULL_BASIS_KEYS))
        self.assertEqual(r["verdict"], "network-empty")
        self.assertNotIn(ME, json.dumps(r))

    def test_cmd_close_health_mocked_human_output(self):
        args = argparse.Namespace(epoch=None, address=ME, no_address=False, k=None, json=False, timeout=5)
        calls = []

        def fake(method, path, **_):
            calls.append(path)
            return {"/api/status": {"K": 5}, "/api/epochs": EMPTY}[path]
        out = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                poa.cmd_close_health(args)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(calls, ["/api/status", "/api/epochs"])
        self.assertIn("NETWORK-EMPTY", out.getvalue())
        self.assertIn("not a personal miss", out.getvalue())
        self.assertIn("basis keys missing vs previous close: ceiling, final_payout, referral2, spotcheck", out.getvalue())


if __name__ == "__main__":
    unittest.main()
