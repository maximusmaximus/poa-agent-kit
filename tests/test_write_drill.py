"""Mocked tests for `poa.py write-drill` (#62). No network, no real key. Run: python3 -m unittest tests/test_write_drill.py"""
import importlib.util, io, json, os, pathlib, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_wd", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


def never(*a, **k):
    raise AssertionError("real http_request reached during the offline drill")


class WriteDrillTests(unittest.TestCase):
    def setUp(self):
        home = pathlib.Path(tempfile.mkdtemp())
        for name, val in (("POA_DIR", home), ("CACHE_DIR", home / "cache")):
            p = mock.patch.object(poa, name, val); p.start(); self.addCleanup(p.stop)
        self.home = home
        p = mock.patch.object(poa, "http_request", side_effect=never); self.http = p.start(); self.addCleanup(p.stop)

    def test_all_cases_pass_and_stay_offline(self):
        r = poa.write_drill()
        self.assertTrue(r["ok"], [c for c in r["cases"] if not c["pass"]])
        self.http.assert_not_called()
        self.assertTrue(all(not c["offroute"] for c in r["cases"]))
        self.assertFalse((self.home / "cache").exists())  # the real POA_HOME cache is never written

    def test_case_counts(self):
        rows = {c["case"]: c for c in poa.write_drill()["cases"]}
        self.assertEqual((rows["crash-after-send"]["sends"], rows["crash-after-send"]["run2"]), (1, "sent_before"))
        self.assertEqual(rows["crash-before-send"]["sends"], 0)
        self.assertEqual((rows["timeout-not-landed"]["sends"], rows["timeout-not-landed"]["landed_writes"]), (1, 0))
        self.assertIn("held", rows["timeout-not-landed"]["final_state"])
        self.assertEqual(rows["cf524-after-commit"]["run1"], "unknown")
        self.assertEqual((rows["stale-seed-refused"]["sends"], rows["stale-seed-refused"]["landed_writes"]), (2, 1))
        self.assertEqual(rows["duplicate-is-receipt"]["run1"], "duplicate")
        self.assertEqual(rows["duplicate-is-receipt"]["final_state"], "landed")
        self.assertEqual((rows["write-lock-busy"]["run1"], rows["write-lock-busy"]["sends"]), ("busy", 1))

    def test_globals_restored(self):
        before = (poa.POA_DIR, poa.CACHE_DIR, poa.http_request, poa.load_key, poa.WRITE_LOCK_TIMEOUT_S, dict(poa._NONCE))
        poa.write_drill()
        after = (poa.POA_DIR, poa.CACHE_DIR, poa.http_request, poa.load_key, poa.WRITE_LOCK_TIMEOUT_S, dict(poa._NONCE))
        self.assertEqual(before, after)

    def test_drill_catches_a_client_that_resends(self):
        real = poa.submit_answer_safely

        def careless(text, epoch):  # forgets the durable record: every restart sends again
            poa.save_epoch_cache(epoch, send_outcome="halted")
            return real(text, epoch)

        with mock.patch.object(poa, "submit_answer_safely", careless):
            r = poa.write_drill()
        self.assertFalse(r["ok"])
        bad = {c["case"] for c in r["cases"] if not c["pass"]}
        self.assertIn("timeout-not-landed", bad)
        self.assertIn("crash-after-send", bad)

    def test_cli_exit_codes(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ex:
            poa.cmd_write_drill(mock.Mock(json=True))
        self.assertEqual(ex.exception.code, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])
        with mock.patch.object(poa, "write_drill", return_value={"ok": False, "cases": [], "note": ""}), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ex2:
            poa.cmd_write_drill(mock.Mock(json=False))
        self.assertEqual(ex2.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
