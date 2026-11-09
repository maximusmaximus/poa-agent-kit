"""Mocked tests for the live epoch length (auto_epoch_seconds 21600 -> 30600, Nov 9, 2026) and the
6h close_summary_window_s. No network, no real key. Run: python3 -m unittest tests/test_epoch_length_live.py"""
import datetime, importlib.util, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_elive", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
UTC = datetime.timezone.utc


class LiveEpochSecondsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.p = mock.patch.object(poa, "EPOCH_SECONDS_CACHE", self.tmp / "epoch-seconds.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_reads_live_value_and_remembers_it(self):
        with mock.patch.object(poa, "api_call", return_value={"auto_epoch_seconds": 30600}):
            self.assertEqual(poa.live_epoch_seconds(), 30600.0)
        self.assertEqual(json.loads((self.tmp / "epoch-seconds.json").read_text())["auto_epoch_seconds"], 30600)

    def test_falls_back_to_remembered_value_when_api_down(self):
        (self.tmp / "epoch-seconds.json").write_text(json.dumps({"auto_epoch_seconds": 30600}))
        with mock.patch.object(poa, "api_call", side_effect=RuntimeError("timeout")):
            self.assertEqual(poa.live_epoch_seconds(), 30600.0)

    def test_falls_back_to_default_with_nothing_known(self):
        with mock.patch.object(poa, "api_call", side_effect=RuntimeError("timeout")):
            self.assertEqual(poa.live_epoch_seconds(), float(poa.EPOCH_SECONDS))

    def test_ignores_bad_live_value(self):
        with mock.patch.object(poa, "api_call", return_value={"auto_epoch_seconds": 0}):
            self.assertEqual(poa.live_epoch_seconds(), float(poa.EPOCH_SECONDS))

    def test_grid_fallback_uses_8_5h_not_6h(self):
        # Last close 7h ago, epoch number unknown: with 8.5h epochs the window still starts at that close.
        now = datetime.datetime(2026, 11, 9, 20, 0, tzinfo=UTC)
        close_ts = int((now - datetime.timedelta(hours=7)).timestamp())
        eps = {"epochs": [{"epoch": 61, "ts": close_ts}]}
        w = poa.epoch_window(now=now, epochs_res=eps, epoch_seconds=30600)
        self.assertEqual(w["window_age_hours"], 7.0)
        w6 = poa.epoch_window(now=now, epochs_res=eps, epoch_seconds=21600)
        self.assertEqual(w6["window_age_hours"], 1.0)


class WorkWindowStays6hTests(unittest.TestCase):
    def test_explicit_6h_window_wins_over_8_5h_epoch(self):
        w = poa.att_work_window({"close_summary_from_epoch": 28, "close_summary_window_s": 21600, "auto_epoch_seconds": 30600})
        self.assertEqual((w["window_seconds"], w["window_hours"], w["window_source"]), (21600, 6.0, "close_summary_window_s"))


if __name__ == "__main__":
    unittest.main()
