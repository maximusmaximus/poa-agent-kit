"""Mocked tests for `poa.py grade-outcome` (SUGGESTIONS.md #190). Offline only: the command never
calls the network, and every test patches `http_request`, `api_call` and `socket.create_connection`
to fail, so nothing here can reach a live server. POA_HOME is a temp dir; the key is a dummy.
Run: python3 -m unittest tests/test_grade_outcome.py"""
import contextlib, importlib.util, io, json, os, pathlib, socket, sys, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_go", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

TX = "ab" * 32


def _no_net(*a, **k):
    raise AssertionError("network call attempted in an offline test")


class Offline(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(poa, "http_request", side_effect=_no_net),
                  mock.patch.object(poa, "api_call", side_effect=_no_net),
                  mock.patch.object(socket, "create_connection", side_effect=_no_net)):
            p.start(); self.addCleanup(p.stop)


class ClassifyTests(Offline):
    def c(self, resp, status=None):
        return poa.classify_grade_outcome(resp, status=status)

    def test_landed_inline_and_polled(self):
        self.assertEqual(self.c({"txid": TX, "next_nonce": 9})["outcome"], "landed")
        self.assertEqual(self.c({"state": "done", "result": {"txid": TX}})["outcome"], "landed")
        self.assertEqual(self.c({"state": "done"})["outcome"], "landed")
        r = self.c({"status": 200, "body": {"txid": TX}})
        self.assertEqual((r["outcome"], r["http_status"], r["resubmit"]), ("landed", 200, False))

    def test_202_receipt_is_poll_not_fail(self):
        r = self.c({"job_id": "j_42ab", "poll_url": "api/job/j_42ab", "retry_after_seconds": 5}, status=202)
        self.assertEqual((r["outcome"], r["action"], r["job_id"], r["retry_after_s"]), ("accepted_poll_job", "poll", "j_42ab", 5))
        self.assertIn("/api/job/j_42ab", r["next_step"])
        self.assertFalse(r["resubmit"])
        self.assertEqual(self.c({"state": "queued"})["outcome"], "accepted_poll_job")
        self.assertEqual(self.c("HTTP 202 accepted; node is under load — poll job j_9zz1")["job_id"], "j_9zz1")

    def test_job_expired_is_the_only_resubmit(self):
        for resp, st in (({"state": "expired", "error": "job_expired"}, 408), ({"error": "job_expired"}, None),
                         ("HTTP 408 job_expired: rebuild with a fresh ts and resubmit", None)):
            r = self.c(resp, st)
            self.assertEqual((r["outcome"], r["action"], r["resubmit"]), ("job_expired", "resubmit_once", True))
        outcomes = [k for k in poa.GRADE_OUTCOMES if k != "job_expired"]
        self.assertTrue(all(poa.GRADE_OUTCOMES[k][0] != "resubmit_once" for k in outcomes))

    def test_refusals_stop(self):
        r = self.c({"state": "done", "error": "answer already settled"})
        self.assertEqual((r["outcome"], r["action"]), ("already_settled", "stop"))
        self.assertEqual(self.c({"error": "Answer already settled"}, 400)["outcome"], "already_settled")
        self.assertEqual(self.c({"state": "done", "result": {"error": "answer already settled"}})["outcome"], "already_settled")
        self.assertEqual(self.c({"error": "duplicate sealed grade"}, 409)["outcome"], "duplicate")
        self.assertEqual(self.c({"state": "done", "error": "unknown assignment"})["outcome"], "unknown_assignment")
        self.assertEqual(self.c({"error": "duplicate_in_flight"}, 409)["outcome"], "in_flight")

    def test_rate_limit_backs_off(self):
        r = self.c({"error": "rate limited"}, 429)
        self.assertEqual((r["outcome"], r["action"], r["retry_after_s"]), ("rate_limited", "back_off", 60))
        self.assertEqual(self.c({"error": "slow down", "retry_after": 17}, 429)["retry_after_s"], 17)
        self.assertEqual(self.c("HTTP 429 Too Many Requests")["outcome"], "rate_limited")

    def test_timeout_and_unknowns_check_before_resubmit(self):
        for resp in (None, "read timed out", "Remote end closed connection without response", "HTTP 504 Gateway Timeout"):
            r = self.c(resp)
            self.assertEqual((r["outcome"], r["action"]), ("timeout", "check_landed"), resp)
            self.assertIn("nonce or POAScan", r["next_step"])
        self.assertEqual(self.c({"error": "bad gateway"}, 502)["outcome"], "server_error")
        self.assertEqual(self.c({"error": "maintenance halt"}, 503)["outcome"], "halted")
        self.assertEqual(self.c({"error": "not found"}, 404)["outcome"], "other")
        self.assertEqual(self.c({"error": "job not found"}, 404)["outcome"], "job_forgotten")
        self.assertEqual(self.c("something odd")["outcome"], "other")
        self.assertEqual(self.c({"error": "invalid key"}, 401)["outcome"], "auth")

    def test_timeout_with_job_id_polls(self):
        r = self.c("read timed out after 202, job_id=j_77aa")
        self.assertEqual(r["outcome"], "accepted_poll_job")


class SeatAgeTests(Offline):
    def test_seventeen_epochs_refused_from_e123(self):
        a = poa.grade_seat_age_check(106, 123)
        self.assertTrue(a["will_be_refused"]); self.assertEqual(a["age"], 17)
        b = poa.grade_seat_age_check(107, 123)
        self.assertFalse(b["will_be_refused"]); self.assertIn("refused from E124", b["note"])
        c = poa.grade_seat_age_check(100, 122)        # rule not live before E123
        self.assertFalse(c["will_be_refused"]); self.assertFalse(c["rule_live"])
        self.assertFalse(poa.grade_seat_age_check("x", 123)["ok"])


class ReportAndCliTests(Offline):
    def run_cli(self, argv, stdin=None):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["poa.py"] + argv), contextlib.redirect_stdout(buf):
            if stdin is not None:
                with mock.patch.object(sys, "stdin", io.StringIO(stdin)):
                    code = self._main()
            else:
                code = self._main()
        return code, buf.getvalue()

    def _main(self):
        try:
            poa.main()
            return 0
        except SystemExit as e:
            return e.code or 0

    def test_per_line_counts(self):
        log = "\n".join(["HTTP 202 accepted; node is under load - poll job j_1aaa", "HTTP 202 accepted; poll job j_2bbb",
                         "read timed out", "HTTP 429 too many requests", '{"state":"done","error":"answer already settled"}',
                         "", json.dumps({"txid": TX})])
        r = poa.grade_outcome_report(log, per_line=True)
        self.assertEqual(r["counts"], {"accepted_poll_job": 2, "timeout": 1, "rate_limited": 1, "already_settled": 1, "landed": 1})

    def test_cli_file_json_and_age(self):
        d = pathlib.Path(tempfile.mkdtemp())
        f = d / "resp.json"
        f.write_text(json.dumps({"status": 202, "body": {"job_id": "j_5cc5", "poll_url": "api/job/j_5cc5"}}))
        code, out = self.run_cli(["grade-outcome", str(f), "--answer-epoch", "100", "--epoch", "123", "--json"])
        self.assertEqual(code, 0)
        j = json.loads(out)
        self.assertEqual(j["classification"]["items"][0]["outcome"], "accepted_poll_job")
        self.assertTrue(j["seat_age"]["will_be_refused"])

    def test_cli_stdin_text(self):
        code, out = self.run_cli(["grade-outcome", "-"], stdin='{"error":"answer already settled"}')
        self.assertEqual(code, 0)
        self.assertIn("outcome=already_settled", out)
        self.assertIn("Stop for good", out)

    def test_cli_needs_input(self):
        code, out = self.run_cli(["grade-outcome"])
        self.assertEqual(code, 2)

    def test_cli_age_only_and_redaction(self):
        code, out = self.run_cli(["grade-outcome", "--answer-epoch", "110", "--epoch", "123"])
        self.assertEqual(code, 0); self.assertIn("seat-age: ok: age 13", out)
        code, out = self.run_cli(["grade-outcome", "-", "--json"], stdin="read timed out key=pk_test_fake")
        self.assertNotIn("pk_test_fake", out)


if __name__ == "__main__":
    unittest.main()
