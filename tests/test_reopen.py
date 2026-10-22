"""Mocked tests for `poa.py reopen` (no network, no real key). Run: python3 -m unittest tests/test_reopen.py"""
import argparse, datetime, importlib.util, json, os, pathlib, sys, tempfile, unittest
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["POA_HOME"] = TMP
os.environ["POA_ADDRESS"] = "poa1testaddressnotreal"
os.environ["POA_API_KEY"] = "pk_test_fake"
spec = importlib.util.spec_from_file_location("poa", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

CH = {"epoch": 10, "words": ["zenith", "ferrous", "karst"], "answered": False}
ANSWER = ("Zenith thinking about ferrous ore in karst country: the limestone dissolves, iron oxide stains the "
          "cave walls, and the highest point of the ridge is where the water table first breaks. ") * 3


def now_line(node, event, epoch=10, minutes_ago=0):
    ts = (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)).isoformat(timespec="minutes")
    return f"{ts} | node={node} | epoch={epoch} | event={event} | words= | note=test\n"


class ReopenTests(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.log = self.d / "LOG.md"
        self.log.write_text(now_line("antigravity", "HEARTBEAT", minutes_ago=10))
        self.ans = self.d / "a.txt"; self.ans.write_text(ANSWER)
        self.grades = self.d / "g.json"
        self.grades.write_text(json.dumps([{"txid": "t9", "score": 2, "note": "n", "epoch": 9},
                                           {"txid": "t6", "score": 3, "note": "n", "epoch": 6}]))
        poa.CACHE_DIR = self.d / "cache"; poa.ANSWERS_LOG = self.d / "answers.log"
        poa.REOPEN_LOCK = self.d / "reopen.lock"; poa.HANDOFF_LOG = self.log
        self.p = [mock.patch.object(poa, "other_poa_loops", return_value=[]),
                  mock.patch.object(poa, "explorer_find_answer", return_value={"found": False})]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()

    def args(self, **kw):
        a = dict(node="antigravity", primary="antigravity", hours=2.0, log=str(self.log), answer_file=str(self.ans),
                 grades_file=str(self.grades), git_sync=False, dry_run=False, ignore_running=False, timeout=5,
                 confirm_min=0, confirm_max=0)
        a.update(kw); return argparse.Namespace(**a)

    def test_halted_503_one_probe_no_retry(self):
        calls = []
        def fake(m, path, **kw):
            calls.append(path); raise poa.PoaHTTPError("halted", "HTTP 503 (halted): API hardening", 503)
        with mock.patch.object(poa, "api_call", side_effect=fake):
            code, rep = poa.run_reopen(self.args())
        self.assertEqual((code, rep["verdict"], calls), (2, "still-halted", ["/api/hosted/challenge"]))

    def test_dropped_connection_counts_as_halted(self):
        with mock.patch.object(poa, "api_call", side_effect=poa.PoaHTTPError("network", "reset")) as m:
            code, rep = poa.run_reopen(self.args())
        self.assertEqual((code, m.call_count), (2, 1))

    def test_lock_held_by_live_loop(self):
        poa.REOPEN_LOCK.write_text(json.dumps({"pid": os.getppid(), "at": __import__("time").time()}))
        with mock.patch.object(poa, "api_call") as m:
            code, _ = poa.run_reopen(self.args())
        self.assertEqual((code, m.call_count), (4, 0))

    def test_stale_lock_replaced_and_released(self):
        poa.REOPEN_LOCK.write_text(json.dumps({"pid": 999999, "at": 0}))
        with mock.patch.object(poa, "api_call", side_effect=poa.PoaHTTPError("halted", "503")):
            code, _ = poa.run_reopen(self.args())
        self.assertEqual(code, 2); self.assertFalse(poa.REOPEN_LOCK.exists())

    def test_old_loops_block(self):
        with mock.patch.object(poa, "other_poa_loops", return_value=[{"pid": 1, "cmd": "poa.py answer"}]), \
             mock.patch.object(poa, "api_call") as m:
            code, _ = poa.run_reopen(self.args())
        self.assertEqual((code, m.call_count), (4, 0))

    def _resumed(self, ch=CH):
        return mock.patch.object(poa, "api_call", return_value=dict(ch))

    def test_resumed_answers_once_then_grades_oldest_first(self):
        sent = []
        with self._resumed(), \
             mock.patch.object(poa, "submit_answer_safely", side_effect=lambda t, e: sent.append(e) or {"landed": True, "how": "response", "txid": "x"}), \
             mock.patch.object(poa, "confirm_answer_landed", return_value={"landed": True, "how": "explorer"}), \
             mock.patch.object(poa, "submit_grade_safely", side_effect=lambda t, s, r: sent.append(t) or {"landed": True}):
            code, rep = poa.run_reopen(self.args())
        self.assertEqual((code, sent), (0, [10, "t6", "t9"]))
        log = self.log.read_text()
        self.assertIn("event=CLAIM", log); self.assertIn("event=ANSWERED", log)
        # second run: handoff shows ANSWERED -> no second send
        with self._resumed(), mock.patch.object(poa, "submit_answer_safely") as sa, \
             mock.patch.object(poa, "submit_grade_safely", return_value={"landed": True}):
            code, rep = poa.run_reopen(self.args())
        sa.assert_not_called(); self.assertEqual(code, 0)

    def test_hosted_answered_flag_skips(self):
        with self._resumed(dict(CH, answered=True)), mock.patch.object(poa, "submit_answer_safely") as sa, \
             mock.patch.object(poa, "submit_grade_safely", return_value={"landed": True}):
            code, _ = poa.run_reopen(self.args())
        sa.assert_not_called(); self.assertEqual(code, 0)

    def test_explorer_found_skips(self):
        with self._resumed(), mock.patch.object(poa, "explorer_find_answer", return_value={"found": True}), \
             mock.patch.object(poa, "submit_answer_safely") as sa, mock.patch.object(poa, "submit_grade_safely", return_value={}):
            poa.run_reopen(self.args())
        sa.assert_not_called()

    def test_other_node_fresh_claim_waits(self):
        with self.log.open("a") as f: f.write(now_line("grokbot", "CLAIM", minutes_ago=2))
        with self._resumed(), mock.patch.object(poa, "submit_answer_safely") as sa:
            code, _ = poa.run_reopen(self.args())
        sa.assert_not_called(); self.assertEqual(code, 3)

    def test_backup_waits_while_primary_alive(self):
        with self._resumed(), mock.patch.object(poa, "submit_answer_safely") as sa:
            code, _ = poa.run_reopen(self.args(node="grokbot"))
        sa.assert_not_called(); self.assertEqual(code, 3)

    def test_backup_takes_over_after_silence(self):
        self.log.write_text(now_line("antigravity", "HEARTBEAT", minutes_ago=180))
        with self._resumed(), \
             mock.patch.object(poa, "submit_answer_safely", return_value={"landed": True, "txid": "x"}) as sa, \
             mock.patch.object(poa, "confirm_answer_landed", return_value={"landed": True}), \
             mock.patch.object(poa, "submit_grade_safely", return_value={"landed": True}):
            code, _ = poa.run_reopen(self.args(node="grokbot"))
        sa.assert_called_once(); self.assertEqual(code, 0)

    def test_halted_on_send_stops(self):
        with self._resumed(), mock.patch.object(poa, "submit_answer_safely", return_value={"landed": False, "how": "halted"}), \
             mock.patch.object(poa, "submit_grade_safely") as sg:
            code, _ = poa.run_reopen(self.args())
        sg.assert_not_called(); self.assertEqual(code, 2)

    def test_dry_run_sends_nothing(self):
        with self._resumed(), mock.patch.object(poa, "submit_answer_safely") as sa, mock.patch.object(poa, "submit_grade_safely") as sg:
            code, rep = poa.run_reopen(self.args(dry_run=True))
        sa.assert_not_called(); sg.assert_not_called(); self.assertEqual(code, 0)
        self.assertNotIn("CLAIM", self.log.read_text())

    def test_key_never_in_report(self):
        with mock.patch.object(poa, "api_call", side_effect=poa.PoaHTTPError("halted", "503")):
            _, rep = poa.run_reopen(self.args())
        self.assertNotIn("pk_test_fake", json.dumps(rep))


if __name__ == "__main__":
    unittest.main()
