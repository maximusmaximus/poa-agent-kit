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


class AllAnswersTests(unittest.TestCase):
    """Shaped like the real E6 close (Oct 26 2026): an E1-born answer settled and passed at E6
    while the E6-born answer stayed pending; 105 passed+eligible accounts, only 11 established."""

    def e6_like(self):
        others = [f"poa1OTHER{i:03d}" for i in range(105)]
        settled = [{"answer": "c5cb47f9e358f1bf" + "0" * 48, "from": ME, "passed": True, "jury": [{"grader": "g", "aligned": True}] * 5}]
        settled += [{"answer": f"{i:064x}", "from": a, "passed": True, "jury": [{}] * 4} for i, a in enumerate(others[:104])]
        pending = {"31495b46c021" + "1" * 52: {"kind": "short", "panel": ["poa1G"], "since": 6, "grades": [{"score": 2}]},
                   "1723548d8479" + "2" * 52: {"kind": "short", "panel": [], "since": 2, "grades": []}}
        eps = [close(n, T0 + n * H) for n in range(6)]
        e6 = close(6, T0 + 6 * H, rewards={ME: "511996.651201"}, settled=settled, pending=pending, eligible=[ME] + others[:104])
        e6["basis"]["established_after"] = others[:11]
        e6["basis"]["attestations"] = {ME: 10}
        return eps + [e6]

    def test_older_answer_settling_at_newest_close_is_seen(self):
        eps = self.e6_like()
        answers = {1: "c5cb47f9e358", 2: "1723548d8479", 6: "31495b46c021"}
        r = poa.close_check(eps, answers, ME, last=4)
        row = r["rows"][-1]
        self.assertEqual(row["answer"], "pending")  # the E6-born answer itself
        self.assertEqual([a["txid"] for a in row["settled_here"]], ["c5cb47f9e358"])
        self.assertEqual(row["settled_here"][0]["born_epoch"], 1)
        self.assertEqual(row["passed_here"], 1)
        self.assertEqual(r["verdict"], "paid")
        self.assertEqual((r["answers_total"], r["answers_settled"], r["answers_passed"], r["answers_pending"]), (3, 1, 1, 2))
        led = {a["txid"]: a for a in r["answers_all"]}
        self.assertEqual((led["c5cb47f9e358"]["status"], led["c5cb47f9e358"]["settled_at_close"]), ("settled", 6))
        self.assertIn("no_panel_drawn", led["1723548d8479"]["flags"])

    def test_pass_does_not_imply_established(self):
        eps = self.e6_like()
        r = poa.close_check(eps, {1: "c5cb47f9e358"}, ME, last=1)
        self.assertIn("passed_not_established", r["rows"][-1]["flags"])
        est = r["established_from_closes"]
        self.assertIsNone(est["established"])
        self.assertTrue(est["passed_but_not_established"])
        self.assertEqual(est["closes_with_a_passed_answer"], [6])
        b = eps[-1]["basis"]
        passed = {x["from"] for x in b["settled"] if x["passed"]} & set(b["challenge_eligible"])
        self.assertEqual((len(passed), len(passed & set(b["established_after"]))), (105, 11))
        with mock.patch.object(poa, "api_call", return_value=eps):
            self.assertIsNone(poa.edge_read_epochs_summary(ME)["established_from_epochs"])

    def test_established_after_then_removed(self):
        eps = [close(1, T0), close(2, T0 + H), close(3, T0 + 2 * H, removed=[ME])]
        eps[0]["basis"]["established_after"] = [ME]
        st = poa.established_from_closes(eps[:2], ME)
        self.assertEqual((st["established"], st["last_change_close"]), (True, 1))
        st = poa.established_from_closes(eps, ME)
        self.assertEqual((st["established"], st["last_change_close"]), (False, 3))

    def test_all_answers_keeps_two_in_one_epoch_and_settled_from_rows(self):
        eps = self.e6_like()
        txs = [{"type": "challenge", "id": "aaaa1111", "epoch": 3}, {"type": "challenge", "id": "bbbb2222", "epoch": 3}]
        got = poa.own_answers_all(txs, "", None, eps, ME)
        self.assertEqual(got["aaaa1111"], 3)
        self.assertEqual(got["bbbb2222"], 3)
        self.assertIn("c5cb47f9e358f1bf" + "0" * 48, got)  # found via settled.from even with no txid on file
        out = json.dumps(poa.close_check(eps, {}, ME, all_answers=got))
        self.assertNotIn(ME, out)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_close_check(poa.close_check(eps, {}, ME, all_answers=got))
        self.assertNotIn(ME, buf.getvalue())
        self.assertIn("settled here: c5cb47f9e358", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
