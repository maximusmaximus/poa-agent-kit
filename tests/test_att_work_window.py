"""Mocked tests for the attestation work window (release v0.72-fixc). No network. Run: python3 -m unittest tests/test_att_work_window.py"""
import contextlib, importlib.util, io, os, pathlib, tempfile, unittest

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_aww", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


class AttWorkWindowTests(unittest.TestCase):
    def test_inactive_without_param(self):
        self.assertEqual(poa.att_work_window({"auto_epoch_seconds": 21600}), {"active": False})
        self.assertEqual(poa.att_work_window(None), {"active": False})
        self.assertEqual(poa.att_work_window({"close_summary_from_epoch": True}), {"active": False})

    def test_falls_back_to_epoch_length(self):
        w = poa.att_work_window({"close_summary_from_epoch": 28, "auto_epoch_seconds": 21600})
        self.assertTrue(w["active"])
        self.assertEqual((w["from_close_epoch"], w["window_seconds"], w["window_hours"], w["window_source"]),
                         (28, 21600, 6.0, "auto_epoch_seconds"))

    def test_explicit_window_wins(self):
        w = poa.att_work_window({"close_summary_from_epoch": 28, "close_summary_window_s": 7200, "auto_epoch_seconds": 21600})
        self.assertEqual((w["window_seconds"], w["window_source"]), (7200, "close_summary_window_s"))

    def test_default_when_no_epoch_length(self):
        w = poa.att_work_window({"close_summary_from_epoch": 0})
        self.assertEqual((w["window_seconds"], w["window_source"]), (21600, "default"))

    def test_epoch_time_shows_window(self):
        r = poa.epoch_time_table({"close_summary_from_epoch": 28, "auto_epoch_seconds": 21600}, None)
        self.assertTrue(r["att_work_window"]["active"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_epoch_time(r)
        self.assertIn("Attestation work window: 6.0h", buf.getvalue())
        r2 = poa.epoch_time_table({"auto_epoch_seconds": 21600}, None)
        self.assertFalse(r2["att_work_window"]["active"])


if __name__ == "__main__":
    unittest.main()
