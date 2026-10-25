"""Mocked tests for `poa.py close-pull` (no network, no real key). Run: python3 -m unittest tests/test_close_pull.py"""
import importlib.util, os, pathlib, tempfile, unittest

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_cp", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

CLOSE_TS = 1_792_943_326
EPOCHS = [{"epoch": 2, "ts": CLOSE_TS - 21600}, {"epoch": 3, "ts": CLOSE_TS}]


class FakeClock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def asg(rows, has_seat=True, epoch=4):
    return {"epoch": epoch, "has_seat": has_seat, "assignments": rows, "total": len(rows), "already_graded": 0}


class ClassifyTests(unittest.TestCase):
    def test_outcomes(self):
        c = lambda res, code=200: poa.classify_pull(res, code, 3, CLOSE_TS, CLOSE_TS + 10, CLOSE_TS + 11)["outcome"]
        self.assertEqual(c(asg([], has_seat=False)), "no-seat")
        self.assertEqual(c(asg([], has_seat=True)), "seat-no-rows")
        self.assertEqual(c(asg([{"epoch": 4, "seals_seen": 0}] * 3)), "pool-only")
        self.assertEqual(c(asg([{"epoch": 2, "backlog": True, "seals_seen": 0}])), "dealt")
        self.assertEqual(c(asg([{"epoch": 4, "seals_seen": 2}])), "dealt")
        self.assertEqual(c(None, 503), "error")
        self.assertEqual(c(None, None), "error")

    def test_row_fields(self):
        rows = [{"epoch": 1, "backlog": True, "seals_seen": 3}, {"epoch": 2, "backlog": True, "seals_seen": 2},
                {"epoch": 4, "seals_seen": 0}]
        r = poa.classify_pull(asg(rows), 200, 3, CLOSE_TS, CLOSE_TS + 418.04, CLOSE_TS + 419.6, label="+300s")
        self.assertEqual((r["sent_s"], r["returned_s"], r["http"]), (418.0, 419.6, 200))
        self.assertEqual((r["rows"], r["backlog_rows"], r["fresh_rows"]), (3, 2, 1))
        self.assertEqual(r["rows_by_epoch"], {"1": 1, "2": 1, "4": 1})
        self.assertEqual(r["seals_seen"], {"0": 1, "2": 1, "3": 1})
        self.assertEqual(r["close_at"], "2026-10-25T15:48:46+00:00")
        self.assertNotIn("addr", r)

    def test_newest_close(self):
        self.assertEqual(poa.newest_close(EPOCHS), (3, float(CLOSE_TS)))
        self.assertEqual(poa.newest_close([]), (None, None))


class WatchTests(unittest.TestCase):
    def test_waits_for_close_then_pulls_at_offsets(self):
        clk = FakeClock(CLOSE_TS - 60)
        status = iter([3, 3, 3, 4])  # close lands on the 4th status read
        calls = []

        def fetch_asg():
            calls.append(clk())
            return 200, asg([] if len(calls) == 1 else [{"epoch": 2, "backlog": True, "seals_seen": 3}])

        r = poa.close_pull("poa1fake", followups=[300], wait=True, max_wait=900, interval=20,
                           fetch_status=lambda: next(status), fetch_assignments=fetch_asg,
                           fetch_epochs=lambda: EPOCHS, clock=clk, sleep=clk.sleep)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status_polls"], 4)
        self.assertEqual(len(r["pulls"]), 2)
        self.assertEqual(r["pulls"][0]["outcome"], "seat-no-rows")
        self.assertEqual(r["pulls"][1]["label"], "+300s")
        self.assertEqual(r["pulls"][1]["sent_s"], 300.0)
        self.assertEqual(r["pulls"][1]["outcome"], "dealt")

    def test_gives_up_without_close_and_never_pulls(self):
        clk = FakeClock(CLOSE_TS)
        pulled = []
        r = poa.close_pull("poa1fake", wait=True, max_wait=100, interval=20, fetch_status=lambda: 4,
                           fetch_assignments=lambda: pulled.append(1) or (200, asg([])),
                           fetch_epochs=lambda: EPOCHS, clock=clk, sleep=clk.sleep)
        self.assertFalse(r["ok"])
        self.assertEqual(pulled, [])

    def test_interval_floor_and_followup_cap(self):
        clk = FakeClock(CLOSE_TS + 5)
        sleeps = []
        status = iter([1, 2])

        def slp(s):
            sleeps.append(s); clk.sleep(s)

        pulls = []
        r = poa.close_pull("poa1fake", followups=[60, 120, 180, 240, 60], wait=True, interval=1,
                           fetch_status=lambda: next(status), fetch_assignments=lambda: pulls.append(1) or (200, asg([])),
                           fetch_epochs=lambda: EPOCHS, clock=clk, sleep=slp)
        self.assertEqual(sleeps[0], poa.CLOSE_PULL_MIN_INTERVAL_S)
        self.assertEqual(len(pulls), 1 + poa.CLOSE_PULL_MAX_FOLLOWUPS)
        self.assertEqual([p["label"] for p in r["pulls"]], ["first", "+60s", "+120s", "+180s"])

    def test_now_mode_skips_status(self):
        clk = FakeClock(CLOSE_TS + 4657)

        def boom():
            raise AssertionError("status should not be read in --now mode")

        r = poa.close_pull("poa1fake", followups=[], wait=False, fetch_status=boom,
                           fetch_assignments=lambda: (200, asg([], has_seat=False)), fetch_epochs=lambda: EPOCHS,
                           clock=clk, sleep=clk.sleep)
        self.assertEqual(r["status_polls"], 0)
        self.assertEqual(r["pulls"][0]["sent_s"], 4657.0)
        self.assertEqual(r["pulls"][0]["outcome"], "no-seat")


if __name__ == "__main__":
    unittest.main()
