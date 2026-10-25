"""Mocked tests for `poa.py close-check` and the takeover-check epoch window (no network, no real key).
Run: python3 -m unittest tests/test_close_check.py"""
import argparse, contextlib, datetime, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_cc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
H = 21600
T0 = 1_792_878_384
UTC = datetime.timezone.utc


def close(n, ts, rewards=None, settled=(), pending=None, eligible=(), removed=()):
    return {"epoch": n, "ts": ts, "pool": "1000.0", "rewards": rewards or {}, "challenges": {},
            "basis": {"attestations": {}, "challenge_eligible": list(eligible), "established_after": [],
                      "established_removed": list(removed), "slashed": [], "settled": list(settled),
                      "pending": pending or {}}}


class CloseCheckTests(unittest.TestCase):
    def test_answers_from_explorer_and_segment_log_only(self):
        log = "\n".join([
            "2026-10-24T08:00-07:00 | node=antigravity | epoch=1 | event=ANSWERED | note=txid=aaaaaaaa1111 old segment",
            "2026-10-24T20:54-07:00 | node=antigravity | epoch=2 | event=ANSWERED | note=txid=bbbbbbbb2222 landed",
        ])
        txs = [{"type": "challenge", "id": "cccccccc3333", "epoch": 1}, {"type": "transfer", "id": "x", "epoch": 1}]
        got = poa.own_answers_by_epoch(txs, log, poa.SEGMENT2_GENESIS_DT)
        self.assertEqual(got, {1: "cccccccc3333", 2: "bbbbbbbb2222"})

    def test_pending_with_no_panel_is_flagged_and_not_paid(self):
        eps = [close(0, T0), close(1, T0 + H, rewards={"poa1other": "763398.69"},
                                  pending={"c5cb47f9e358aaaa": {"kind": "short", "panel": [], "since": 1}})]
        r = poa.close_check(eps, {1: "c5cb47f9e358"}, ME, last=1)
        row = r["rows"][0]
        self.assertEqual(row["answer"], "pending")
        self.assertIn("no_panel_drawn", row["flags"])
        self.assertIn("not_paid", row["flags"])
        self.assertEqual(r["verdict"], "waiting")
        self.assertEqual(r["answers_stuck_no_panel"], 1)
        self.assertTrue(r["open_epoch_unanswered"])

    def test_settled_pass_and_paid(self):
        st = [{"answer": "dddd0000ffff1111", "from": ME, "passed": True, "jury": [{"g": 1}] * 4}]
        eps = [close(4, T0, rewards={ME: "763398.692810"}, settled=st, eligible=[ME])]
        r = poa.close_check(eps, {4: "dddd0000ffff", 5: "eeee1111"}, ME, last=1)
        row = r["rows"][0]
        self.assertEqual((row["answer"], row["passed"], row["graded"], row["paid"]), ("settled", True, True, True))
        self.assertEqual(row["payout"], "763398.692810")
        self.assertEqual(r["verdict"], "paid")
        self.assertEqual(r["open_epoch_answer_txid"], "eeee1111")

    def test_failed_missing_and_removed(self):
        st = [{"answer": "ffff2222aaaa", "passed": False, "jury": []}]
        eps = [close(1, T0, settled=st), close(2, T0 + H, removed=[ME])]
        r = poa.close_check(eps, {1: "ffff2222aaaa", 2: "99998888"}, ME, last=2)
        self.assertIn("answer_failed", r["rows"][0]["flags"])
        self.assertIn("answer_not_in_settled_or_pending", r["rows"][1]["flags"])
        self.assertIn("established_removed", r["rows"][1]["flags"])
        self.assertEqual(r["verdict"], "not-paid")

    def test_output_never_contains_address(self):
        eps = [close(1, T0, rewards={ME: "5.0"}, eligible=[ME])]
        out = json.dumps(poa.close_check(eps, {1: "abcdabcd"}, ME))
        self.assertNotIn(ME, out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_close_check(poa.close_check(eps, {1: "abcdabcd"}, ME))
        self.assertNotIn(ME, buf.getvalue())

    def test_cli_exit_codes(self):
        eps = [close(1, T0, rewards={ME: "5.0"})]
        args = argparse.Namespace(timeout=5, address=ME, log="/nonexistent", json=True, last=2)
        with mock.patch.object(poa, "api_call", side_effect=lambda m, path, **k: eps if path == "/api/epochs" else {"grade_quorum": 4}), \
             mock.patch.object(poa, "explorer_get", return_value={"txs": [{"type": "challenge", "id": "abcdabcd", "epoch": 1}]}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_close_check(args)  # paid -> no exit
            self.assertNotIn(ME, buf.getvalue())
        eps2 = [close(1, T0)]
        with mock.patch.object(poa, "api_call", side_effect=lambda m, path, **k: eps2 if path == "/api/epochs" else {}), \
             mock.patch.object(poa, "explorer_get", return_value={}):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                poa.cmd_close_check(args)
            self.assertEqual(cm.exception.code, 3)


class EpochWindowTests(unittest.TestCase):
    def test_overdue_close_keeps_growing_age(self):
        # E1 closed at T0+H; E2 is open; the E2 close is 2h late. Old grid logic reset age to ~2h.
        eps = [close(0, T0), close(1, T0 + H)]
        now = datetime.datetime.fromtimestamp(T0 + H + H + 7200, UTC)
        w = poa.epoch_window(now=now, epochs_res=eps, current_epoch=2, epoch_seconds=H)
        self.assertTrue(w["anchor_exact"])
        self.assertEqual(w["window_age_hours"], 8.0)
        self.assertTrue(w["close_overdue"])

    def test_on_time_epoch(self):
        eps = [close(0, T0), close(1, T0 + H)]
        now = datetime.datetime.fromtimestamp(T0 + H + 1800, UTC)
        w = poa.epoch_window(now=now, epochs_res=eps, current_epoch=2, epoch_seconds=H)
        self.assertEqual(w["window_age_hours"], 0.5)
        self.assertFalse(w["close_overdue"])

    def test_fallback_grid_when_previous_close_unknown(self):
        eps = [close(0, T0)]
        now = datetime.datetime.fromtimestamp(T0 + H + 1800, UTC)
        w = poa.epoch_window(now=now, epochs_res=eps, current_epoch=5, epoch_seconds=H)
        self.assertFalse(w["anchor_exact"])
        self.assertEqual(w["window_age_hours"], 0.5)

    def test_takeover_decides_on_overdue_epoch(self):
        eps = [close(0, T0), close(1, T0 + H)]
        log = pathlib.Path(tempfile.mkdtemp()) / "LOG.md"
        fresh = datetime.datetime.now(UTC).isoformat(timespec="minutes")
        log.write_text(f"{fresh} | node=antigravity | epoch=2 | event=HEARTBEAT | note=x\n")
        real_now = datetime.datetime.fromtimestamp(T0 + 2 * H + 7200, UTC)

        real_window = poa.epoch_window

        def fake_window(**kw):
            return real_window(now=real_now, epochs_res=eps, **kw)
        args = argparse.Namespace(log=str(log), node="antigravity", hours=2.0)
        with mock.patch.object(poa, "read_challenge", return_value={"epoch": 2, "words": []}), \
             mock.patch.object(poa, "is_challenge_answered", return_value=False), \
             mock.patch.object(poa, "api_call", return_value={"auto_epoch_seconds": H}), \
             mock.patch.object(poa, "epoch_window", side_effect=fake_window):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_takeover_check(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["decision"], "takeover")
        self.assertTrue(out["clock"]["anchor_exact"])


if __name__ == "__main__":
    unittest.main()
