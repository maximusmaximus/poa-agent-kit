"""Mocked tests for `poa.py epoch-time` (no network, no real key). Run: python3 -m unittest tests/test_epoch_time.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_et", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

H = 21600
PARAMS = {
    "auto_epoch_seconds": H, "vesting_epochs": 100, "pending_ttl_epochs": 16, "estab_ttl": 6,
    "seat_suspend_epochs": 48, "halving_epochs": 720, "vesting_from_epoch": 0, "pending_merge_from_epoch": 7,
    "checkpoint_lag_seconds": 600, "unhalt_timelock_pre_s": 900, "require_chain_id": True, "K": 5,
}
T0 = 1_760_000_000


def closes(gaps):
    ts, out = T0, [{"epoch": 0, "ts": T0}]
    for i, g in enumerate(gaps, 1):
        ts += g
        out.append({"epoch": i, "ts": ts})
    return out


class EpochTimeTests(unittest.TestCase):
    def test_duration_keys_skip_activation_points_and_flags(self):
        self.assertEqual(poa._epoch_duration_keys(PARAMS),
                         ["estab_ttl", "halving_epochs", "pending_ttl_epochs", "seat_suspend_epochs", "vesting_epochs"])
        self.assertEqual(poa._seconds_keys(PARAMS), ["checkpoint_lag_seconds", "unhalt_timelock_pre_s"])

    def test_nominal_and_observed_with_drift(self):
        eps = closes([H + 77] * 7)  # ~1.28 min drift per close, as measured on LabLeak
        r = poa.epoch_time_table(PARAMS, {"epochs": eps}, now=eps[-1]["ts"] + 3600)
        self.assertEqual(r["nominal_epoch_hours"], 6.0)
        self.assertEqual(r["drift_minutes_per_close"], 1.28)
        vest = next(x for x in r["rows"] if x["setting"] == "vesting_epochs")
        self.assertEqual(vest["nominal"], {"hours": 600.0, "days": 25.0})
        self.assertEqual(vest["observed"]["days"], 25.09)
        ttl = next(x for x in r["rows"] if x["setting"] == "pending_ttl_epochs")
        self.assertEqual(ttl["nominal"]["hours"], 96.0)
        self.assertEqual(r["pauses"], [])
        self.assertNotIn("overdue_hours", r)
        self.assertEqual(r["hours_since_last_close"], 1.0)

    def test_pause_excluded_from_cadence_and_reported(self):
        eps = closes([H, H, int(40.07 * 3600), H, H])
        r = poa.epoch_time_table(PARAMS, eps)
        self.assertEqual(r["observed_epoch_hours"], 6.0)
        self.assertEqual(r["pauses"], [{"from": 2, "to": 3, "hours": 40.07}])

    def test_overdue_when_closes_stall(self):
        eps = closes([H] * 3)
        r = poa.epoch_time_table(PARAMS, eps, now=eps[-1]["ts"] + 9 * 3600)
        self.assertEqual(r["overdue_hours"], 3.0)
        self.assertEqual(r["last_close"]["epoch"], 3)

    def test_window_uses_recent_gaps(self):
        eps = closes([H] * 5 + [H + 600] * 2)
        r = poa.epoch_time_table(PARAMS, eps, window=2)
        self.assertEqual((r["observed_from_gaps"], r["drift_minutes_per_close"]), (2, 10.0))

    def test_no_epochs_still_gives_nominal_table(self):
        r = poa.epoch_time_table(PARAMS, None)
        self.assertIsNone(r["observed_epoch_hours"])
        self.assertTrue(all("observed" not in x for x in r["rows"]))
        self.assertEqual(next(x for x in r["seconds_rows"] if x["setting"] == "unhalt_timelock_pre_s")["hours"], 0.25)

    def test_cli_mocked_reads_only(self):
        eps = closes([H] * 4)
        calls = []

        def fake(method, path, **kw):
            calls.append((method, path, kw.get("need_key")))
            return PARAMS if path == "/api/params" else {"epochs": eps}

        args = argparse.Namespace(window=8, json=True, timeout=5)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(buf):
            poa.cmd_epoch_time(args)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"] and out["read_only"])
        self.assertEqual(calls, [("GET", "/api/params", False), ("GET", "/api/epochs", False)])
        args.json = False
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake), contextlib.redirect_stdout(buf):
            poa.cmd_epoch_time(args)
        self.assertIn("vesting_epochs", buf.getvalue())

    def test_cli_params_unreachable_exits_2(self):
        args = argparse.Namespace(window=8, json=True, timeout=5)
        with mock.patch.object(poa, "api_call", side_effect=RuntimeError("timeout")), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            poa.cmd_epoch_time(args)
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
