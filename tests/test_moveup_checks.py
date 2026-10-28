"""Offline tests for plain-style scoring, drift, the overlap guard, grade-plan/grade-calibrate,
seat-check and the takeover threshold. No network, no real key; fixtures are synthetic text.
Run: python3 -m unittest tests/test_moveup_checks.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("poa_mu", ROOT / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)
FIX = json.loads((ROOT / "tests" / "fixtures" / "grading_calibration.json").read_text(encoding="utf-8"))["rows"]
BY_ID = {r["id"]: r for r in FIX}
ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class StyleTests(unittest.TestCase):
    def test_plain_pattern_scores_full(self):
        s = poa.plain_style_score(BY_ID["good-plain-3"]["answer"])
        self.assertEqual(s["style_breakdown"], {"mechanism_20": 20, "attacker_cost_30": 30, "honest_cost_20": 20,
                                                "failure_mode_15": 15, "plain_language_15": 15})
        self.assertEqual(s["style_score"], 100)
        self.assertFalse(s["drift"])
        self.assertEqual(s["style_flags"], [])

    def test_jargon_and_drift_are_penalised(self):
        s = poa.plain_style_score(BY_ID["offtopic-drift"]["answer"])
        self.assertTrue(s["drift"])
        self.assertGreater(s["jargon_per_100_words"], 2.5)
        self.assertEqual(s["style_breakdown"]["plain_language_15"], 0)
        self.assertTrue(any(f.startswith("STYLE_DRIFT") for f in s["style_flags"]))
        self.assertTrue(any(f.startswith("STYLE_JARGON_DENSE") for f in s["style_flags"]))
        self.assertLess(s["style_score"], 30)

    def test_missing_parts_are_flagged(self):
        s = poa.plain_style_score(BY_ID["coherent-2"]["answer"])
        self.assertFalse(s["drift"])
        names = " ".join(s["style_flags"])
        self.assertIn("STYLE_NO_ATTACKER_COST_MATH", names)
        self.assertIn("STYLE_NO_HONEST_COST_LINE", names)
        self.assertIn("STYLE_NO_FAILURE_MODE", names)

    def test_second_number_in_fake_sentence_counts_as_cost(self):
        t = "Require a sequential delay before any paid action. A ring of 10,000 clones then waits 10,000 times in a row. Honest agents wait once. Limit: a rich attacker can still buy machines."
        self.assertEqual(poa.plain_style_score(t)["style_breakdown"]["attacker_cost_30"], 30)

    def test_check_answer_quality_carries_style(self):
        r = poa.check_answer_quality(BY_ID["good-plain-3"]["answer"], ["harbor", "lantern", "cedar"])
        self.assertEqual(r["style_score"], 100)
        self.assertIn("combined_score", r)
        self.assertFalse(r["drift"])


class OverlapGuardTests(unittest.TestCase):
    PRIOR = ("Each new identity must lock a refundable bond before it earns and the bond unlocks only after four "
             "epochs of verified work so a farm of fake identities pays the bond many times over while one honest agent pays once")

    def test_copy_is_refused(self):
        draft = self.PRIOR + " and the failure mode is a rich attacker."
        g = poa.overlap_guard(draft, [{"src": "top1", "epoch": 12, "text": self.PRIOR}])
        self.assertTrue(g["refused"])
        self.assertEqual(g["over_threshold"][0]["src"], "top1")
        self.assertTrue(g["verdict"].startswith("REFUSE"))

    def test_own_words_pass(self):
        g = poa.overlap_guard(BY_ID["good-plain-3"]["answer"], [{"src": "top1", "epoch": 12, "text": self.PRIOR}])
        self.assertFalse(g["refused"])
        self.assertLessEqual(g["max_overlap"], 0.25)

    def test_threshold_is_strictly_greater(self):
        # 4 shingles in draft, exactly 1 shared -> 0.25 -> allowed; 2 shared -> 0.5 -> refused
        self.assertFalse(poa.overlap_guard("a b c d e f", [{"src": "top1", "text": "a b c x"}])["refused"])
        self.assertTrue(poa.overlap_guard("a b c d e f", [{"src": "top1", "text": "a b c d"}])["refused"])

    def test_own_past_answers_are_checked_and_identical_resend_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "answers.log"
            log.write_text("2026-10-27T00:00:00+00:00\tchain=poa-mainnet-1\tepoch=11\twords=a,b,c\t" + self.PRIOR + "\n", encoding="utf-8")
            corpus = poa.own_answers_from_log(log)
            self.assertEqual(corpus[0]["epoch"], "11")
            self.assertTrue(poa.overlap_guard(self.PRIOR + " again", corpus)["refused"])
            self.assertFalse(poa.overlap_guard(self.PRIOR, corpus)["refused"])  # the same answer, already sent

    def test_top_accounts_rank_and_exclude_self(self):
        eps = [{"epoch": 1, "rewards": {"A": "5", "B": "1", ME: "99"}}, {"epoch": 2, "rewards": {"B": "10", "C": "2"}}]
        self.assertEqual(poa.top_accounts_from_epochs(eps, top=2, closes=5, exclude=ME), ["B", "A"])
        self.assertEqual(poa.top_accounts_from_epochs(eps, top=5, closes=1, exclude=ME), ["B", "C"])

    def test_refresh_writes_local_cache_without_addresses(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "overlap-cache.json"
            eps = [{"epoch": 3, "ts": 1, "rewards": {"poa1TOPxxxxxxxx": "100"}}]
            def answers(a):
                return [{"epoch": 3, "txid12": "abc123abc123", "text": ("mine " if a == ME else "top ") + self.PRIOR}]
            c = poa.refresh_overlap_cache(path=path, fetch_epochs=lambda: eps, fetch_answers=answers, address=ME, clock=lambda: 1000.0)
            self.assertEqual([x["src"] for x in c["answers"]], ["top1", "self"])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(ME, raw)
            self.assertNotIn("poa1TOP", raw)
            self.assertEqual(poa.overlap_cache_age_h(c, now=1000.0 + 7200), 2.0)

    def test_recent_public_answers_reads_explorer(self):
        calls = []
        def fetch(path):
            calls.append(path)
            if path.startswith("/api/explorer/address/"):
                return {"txs": [{"id": "t1" * 32, "type": "challenge", "ts": 5}, {"id": "g1", "type": "grade-sealed", "ts": 6}]}
            return {"body": {"epoch": 12, "answer": "  hello world answer  "}}
        r = poa.recent_public_answers(ME, per_account=3, fetch=fetch, sleep=lambda s: None)
        self.assertEqual(r, [{"epoch": 12, "txid12": ("t1" * 32)[:12], "text": "hello world answer"}])
        self.assertEqual(len(calls), 2)

    def test_cli_refuses_with_exit_2_offline(self):
        with tempfile.TemporaryDirectory() as d:
            cache = pathlib.Path(d) / "c.json"
            cache.write_text(json.dumps({"fetched_at": 0, "answers": [{"src": "top1", "epoch": 9, "text": self.PRIOR}]}), encoding="utf-8")
            draft = pathlib.Path(d) / "draft.txt"
            draft.write_text(self.PRIOR, encoding="utf-8")
            args = argparse.Namespace(file=str(draft), cache=str(cache), refresh=False, offline=True, max_age_h=6.0,
                                      top=10, closes=5, per_account=6, threshold=0.25, require_top=False)
            buf = io.StringIO()
            with mock.patch.object(poa, "ANSWERS_LOG", pathlib.Path(d) / "none.log"), contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    poa.cmd_overlap_guard(args)
            self.assertEqual(cm.exception.code, 2)
            self.assertTrue(json.loads(buf.getvalue())["refused"])


class GradingPrepTests(unittest.TestCase):
    def test_calibration_fixture_agrees_with_consensus(self):
        r = poa.calibrate_grades(FIX)
        self.assertEqual(r["rows"], len(FIX))
        self.assertGreaterEqual(r["agreement"], 0.76)
        self.assertEqual(r["exact_score_match"], 1.0)

    def test_calibrate_cli_fails_below_floor(self):
        flipped = [dict(x, passed=not x["passed"]) for x in FIX]
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "f.json"
            f.write_text(json.dumps(flipped), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    poa.cmd_grade_calibrate(argparse.Namespace(fixtures=str(f), min_agreement=0.76))
            self.assertEqual(cm.exception.code, 2)

    def test_grade_plan_offline_then_grade_batch_with_fake_server(self):
        seats_file = ROOT / "tests" / "fixtures" / "seats_offline.json"
        seats = json.loads(seats_file.read_text(encoding="utf-8"))["assignments"]
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "queue.json"
            with mock.patch.object(poa, "api_call", side_effect=AssertionError("no network in grade-plan --seats")), \
                    contextlib.redirect_stdout(io.StringIO()) as buf:
                poa.cmd_grade_plan(argparse.Namespace(seats=str(seats_file), out=str(out)))
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep["sent"], 0)
            queue = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([q["epoch"] for q in queue], [7, 8, 9])  # oldest first
            self.assertEqual({q["txid"][0]: q["score"] for q in queue}, {"b": 1, "c": 0, "a": 3})
            sent = []
            def fake_submit(txid, score, note):
                sent.append((txid, score))
                return {"landed": True, "how": "sent", "result": {"id": len(sent)}}
            dealt = {"assignments": [{"answer_txid": s["answer_txid"]} for s in seats if s["epoch"] != 8]}
            with mock.patch.object(poa, "api_call", return_value=dealt), \
                    mock.patch.object(poa, "submit_grade_safely", side_effect=fake_submit), \
                    contextlib.redirect_stdout(io.StringIO()) as buf2:
                poa.cmd_grade_batch(argparse.Namespace(file=str(out), mine=False))
            res = json.loads(buf2.getvalue())
            self.assertEqual(res["sealed_count"], 2)
            self.assertEqual(res["already_graded_count"], 1)  # epoch-8 seat no longer dealt: never sent
            self.assertEqual([s[0][0] for s in sent], ["b", "a"])

    def test_propose_grade_rules(self):
        w = ["harbor", "lantern", "cedar"]
        self.assertEqual(poa.propose_grade(BY_ID["too-short"]["answer"], w)["score"], 0)
        self.assertEqual(poa.propose_grade(BY_ID["injection"]["answer"], w)["score"], 0)
        self.assertEqual(poa.propose_grade(BY_ID["missing-word"]["answer"], w)["score"], 1)
        self.assertEqual(poa.propose_grade(BY_ID["coherent-2"]["answer"], w)["score"], 2)


class SeatCheckTests(unittest.TestCase):
    def test_reports_change_once_and_never_prints_address(self):
        with tempfile.TemporaryDirectory() as d:
            st = pathlib.Path(d) / "seat.json"
            none = lambda: (200, {"has_seat": False, "assignments": []})
            r1 = poa.seat_check(ME, fetch_assignments=none, state_path=st, clock=lambda: 1_793_000_000)
            self.assertTrue(r1["changed"])  # first run: nothing stored yet
            r2 = poa.seat_check(ME, fetch_assignments=none, state_path=st, clock=lambda: 1_793_003_600)
            self.assertFalse(r2["changed"])
            self.assertEqual(r2["action"], "none")
            dealt = lambda: (200, {"has_seat": True, "assignments": [{"backlog": True, "epoch": 9}]})
            r3 = poa.seat_check(ME, fetch_assignments=dealt, fetch_me=lambda: {"established": True}, state_path=st,
                                clock=lambda: 1_793_007_200)
            self.assertTrue(r3["changed"])
            self.assertTrue(r3["established"])
            self.assertIn("grade-plan", r3["action"])
            self.assertNotIn(ME, json.dumps(r3) + st.read_text(encoding="utf-8"))

    def test_error_does_not_overwrite_state(self):
        with tempfile.TemporaryDirectory() as d:
            st = pathlib.Path(d) / "seat.json"
            poa.seat_check(ME, fetch_assignments=lambda: (200, {"has_seat": False, "assignments": []}), state_path=st)
            before = st.read_text(encoding="utf-8")
            r = poa.seat_check(ME, fetch_assignments=lambda: (503, None), state_path=st)
            self.assertFalse(r["ok"])
            self.assertFalse(r["changed"])
            self.assertEqual(st.read_text(encoding="utf-8"), before)


class TakeoverThresholdTests(unittest.TestCase):
    def run_check(self, age_h, after_min):
        args = argparse.Namespace(log="/nonexistent/LOG.md", node="antigravity", hours=2.0, after_min=after_min)
        with mock.patch.object(poa, "pc_silence", return_value={"silent": False}), \
                mock.patch.object(poa, "read_challenge", return_value={"epoch": 14, "words": ["a", "b", "c"]}), \
                mock.patch.object(poa, "is_challenge_answered", return_value=False), \
                mock.patch.object(poa, "api_call", return_value={"auto_epoch_seconds": 21600}), \
                mock.patch.object(poa, "epoch_window", return_value={"window_age_hours": age_h}), \
                mock.patch.object(poa, "handoff_lines", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            poa.cmd_takeover_check(args)
        return json.loads(buf.getvalue())

    def test_takes_over_after_45_minutes(self):
        self.assertEqual(self.run_check(0.70, 45)["decision"], "wait")   # 42 min
        r = self.run_check(0.80, 45)                                       # 48 min
        self.assertEqual(r["decision"], "takeover")
        self.assertEqual(r["takeover_after_min"], 45)

    def test_old_one_hour_rule_still_available(self):
        self.assertEqual(self.run_check(0.80, 60)["decision"], "wait")


if __name__ == "__main__":
    unittest.main()
