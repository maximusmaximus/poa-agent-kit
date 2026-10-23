"""Mocked tests for `poa.py close-health` (no network, no real key). Run: python3 -m unittest tests/test_close_health.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ch", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def close(n, ts, attesters, eligible, burned="100.000000", rewards=None, removed=(), after=(), settled=0, pending=0):
    return {
        "epoch": n, "ts": ts, "pool": "1000.000000",
        "rewards": rewards or {},
        "basis": {
            "attestations": {f"poa1att{i}": 3 for i in range(attesters)},
            "challenge_eligible": list(eligible),
            "established_after": list(after),
            "established_removed": list(removed),
            "slashed": [], "ejected": [],
            "settled": [{"answer": f"s{i}"} for i in range(settled)],
            "pending": {f"p{i}": {"since": 1} for i in range(pending)},
            "final_payout": {"burned": burned},
        },
    }


H = 21600
HEALTHY = [
    close(0, 1000, 0, []),
    close(1, 1000 + H, 40, [f"e{i}" for i in range(50)]),
    close(2, 1000 + 2 * H, 45, [f"e{i}" for i in range(60)] + [ME], rewards={ME: "12.500000", "poa1other": "0.000000"},
          after=[ME], settled=30, pending=10),
]
HALTED = HEALTHY + [close(3, 1000 + 3 * H + 7200, 0, ["e1"], burned="900.000000", removed=[ME, "poa1x"], pending=99)]


class CloseHealthTests(unittest.TestCase):
    def test_healthy_newest_close(self):
        r = poa.close_health(HEALTHY, k=5, address=ME)
        self.assertEqual((r["epoch"], r["verdict"], r["flags"]), (2, "healthy", []))
        self.assertEqual((r["distinct_attesters"], r["challenge_eligible"], r["settled"], r["pending"]), (45, 61, 30, 10))
        self.assertEqual(r["hours_since_previous_close"], 6.0)
        self.assertEqual((r["addresses_paid"], r["rewards_total"], r["burned_share"]), (1, "12.500000", 0.1))
        self.assertEqual(r["you"], {"challenge_eligible": True, "attested": False, "reward": "12.500000",
                                    "established_after": True, "established_removed": False, "slashed": False, "ejected": False})

    def test_halted_like_close(self):
        r = poa.close_health(HALTED, k=5, address=ME)
        self.assertEqual(r["verdict"], "halted-like")
        self.assertEqual(set(r["flags"]), {"attesters_below_k", "eligible_thin", "mostly_burned"})
        self.assertEqual(r["median_eligible_earlier_closes"], 55.5)  # E0 genesis excluded
        self.assertTrue(r["you"]["established_removed"])
        self.assertEqual(r["you"]["reward"], "0.000000")
        self.assertEqual(r["hours_since_previous_close"], 8.0)

    def test_thin_but_not_halted(self):
        eps = HEALTHY + [close(3, 1000 + 3 * H, 10, ["a", "b"])]
        r = poa.close_health(eps, k=5)
        self.assertEqual((r["verdict"], r["flags"]), ("thin", ["eligible_thin"]))
        self.assertNotIn("you", r)

    def test_pick_epoch_and_wrapped_unsorted_input(self):
        r = poa.close_health({"epochs": list(reversed(HALTED))}, k=5, epoch=1)
        self.assertEqual((r["epoch"], r["verdict"]), (1, "healthy"))
        self.assertFalse(poa.close_health(HALTED, epoch=42)["ok"])
        self.assertFalse(poa.close_health([], k=5)["ok"])

    def test_k_is_configurable(self):
        self.assertEqual(poa.close_health(HEALTHY, k=46)["verdict"], "halted-like")

    def run_cmd(self, responses, **kw):
        args = argparse.Namespace(epoch=None, address=ME, no_address=False, k=None, json=True, timeout=5)
        vars(args).update(kw)
        calls = []

        def fake(method, path, **_):
            calls.append(path)
            v = responses[path]
            if isinstance(v, Exception):
                raise v
            return v
        out = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(out):
            try:
                poa.cmd_close_health(args)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), calls

    def test_cmd_reads_k_from_status_and_exits_3_when_flagged(self):
        code, out, calls = self.run_cmd({"/api/status": {"K": 5}, "/api/epochs": HALTED})
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["verdict"], "halted-like")
        self.assertEqual(calls, ["/api/status", "/api/epochs"])

    def test_cmd_healthy_exit_0_and_status_fallback(self):
        code, out, _ = self.run_cmd({"/api/status": TimeoutError("t"), "/api/epochs": HEALTHY})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["k"], 5)

    def test_cmd_epochs_unreadable_exit_2(self):
        code, out, _ = self.run_cmd({"/api/epochs": TimeoutError("timed out")}, k=5, json=False)
        self.assertEqual(code, 2)
        self.assertIn("could not read", out)

    def test_human_output_mentions_verdict(self):
        code, out, _ = self.run_cmd({"/api/epochs": HALTED}, k=5, json=False)
        self.assertEqual(code, 3)
        self.assertIn("E3 closed", out)
        self.assertIn("HALTED-LIKE", out)
        self.assertIn("you: eligible False", out)


if __name__ == "__main__":
    unittest.main()
