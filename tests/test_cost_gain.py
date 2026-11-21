"""Offline tests for `poa.py cost-gain` and the warn-only cost_gain block in the answer check
(SUGGESTIONS.md #163). No network, no real key: api_call, http_request and socket.create_connection
are patched to fail if anything tries to connect. Run: python3 -m unittest tests/test_cost_gain.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, socket, sys, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("poa_cost_gain", ROOT / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

WORDS = ["harbor", "lantern", "cedar"]
FULL = ("Each new identity must lock a refundable 200 POA bond at the harbor gate for 4 epochs. "
        "1,000 fakes therefore lock 200,000 POA to earn at most 50,000 POA in payouts, a net loss of 150,000. "
        "One honest agent locks 200 once and gets it back. A lantern audit samples 5% of answers; a failed audit burns the bond. "
        "It fails if a rich attacker accepts the loss, so cap rewards per bond like a cedar fence.")
NO_GAIN = ("Each new identity must lock a refundable 200 POA bond at the harbor gate for 4 epochs. "
           "1,000 fakes therefore lock 200,000 POA and wait 4 epochs each. One honest agent locks 200 once. "
           "A lantern audit samples 5% of answers; a failed audit burns the bond, like a cedar fence.")
NO_NUMBER = ("Each new identity must stake a bond at the harbor gate, and a lantern audit checks work. "
             "A sybil farm must fund every fake account, so faking costs more than the rewards it could earn, "
             "like a cedar fence that is cheaper to walk around.")
NO_MECH = "The harbor is quiet tonight, a lantern glows and the cedar smells sweet near the water."
GAIN_NO_COMPARE = ("Each new identity must lock a 200 POA bond at the harbor gate. 1,000 fakes lock 200,000 POA. "
                   "Each fake could earn a payout of 40 POA per epoch. A lantern audit burns the bond, like a cedar fence.")
RATIO = ("Require a 500 POA bond per identity at the harbor. A farm of 100 fakes stakes 50,000 POA versus "
         "a reward of about 8,000 POA, so the attack cost is 6x the gain. A lantern audit burns bonds; cedar rules apply.")


def no_net(*a, **k):
    raise AssertionError("network call attempted")


@contextlib.contextmanager
def offline():
    with mock.patch.object(poa, "api_call", side_effect=no_net), \
         mock.patch.object(poa, "http_request", side_effect=no_net), \
         mock.patch.object(socket, "create_connection", side_effect=no_net):
        yield


class CostGainCheckTests(unittest.TestCase):
    def test_full_estimate_is_ok(self):
        r = poa.cost_gain_check(FULL)
        self.assertEqual(r["verdict"], "ok")
        self.assertEqual(r["missing"], [])
        self.assertIsNone(r["warning"])
        self.assertTrue(r["parts"]["gain_number"])
        self.assertEqual(r["metric_version"], "cg-1")

    def test_cost_without_gain_warns(self):
        r = poa.cost_gain_check(NO_GAIN)
        self.assertEqual(r["verdict"], "warn")
        self.assertIn("attacker_gain", r["missing"])
        self.assertIn("comparison", r["missing"])
        self.assertNotIn("attack_cost_number", r["missing"])
        self.assertIn("cost-vs-gain", r["warning"])

    def test_comparison_without_numbers_warns_on_cost_number(self):
        r = poa.cost_gain_check(NO_NUMBER)
        self.assertEqual(r["verdict"], "warn")
        self.assertEqual(r["missing"], ["attack_cost_number"])

    def test_gain_without_comparison_warns(self):
        r = poa.cost_gain_check(GAIN_NO_COMPARE)
        self.assertEqual(r["verdict"], "warn")
        self.assertEqual(r["missing"], ["comparison"])

    def test_ratio_and_versus_count_as_comparison(self):
        self.assertEqual(poa.cost_gain_check(RATIO)["verdict"], "ok")

    def test_no_mechanism_is_na_without_warning(self):
        r = poa.cost_gain_check(NO_MECH)
        self.assertEqual(r["verdict"], "n/a")
        self.assertIsNone(r["warning"])
        self.assertEqual(r["missing"], [])

    def test_empty_text(self):
        self.assertEqual(poa.cost_gain_check("")["verdict"], "n/a")


class AnswerCheckIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(poa, "ANSWERS_LOG", pathlib.Path(self.tmp.name) / "answers.log")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_warn_never_blocks(self):
        with offline():
            r = poa.check_answer_quality(NO_GAIN, WORDS)
        self.assertTrue(r["passed"])
        self.assertEqual(r["cost_gain"]["verdict"], "warn")
        self.assertTrue(any("cost-vs-gain" in w for w in r["warnings"]))

    def test_ok_adds_no_warning(self):
        with offline():
            r = poa.check_answer_quality(FULL, WORDS)
        self.assertTrue(r["passed"])
        self.assertEqual(r["cost_gain"]["verdict"], "ok")
        self.assertFalse(any("cost-vs-gain" in w for w in r["warnings"]))


class CommandTests(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        code = 0
        with offline(), mock.patch.object(sys, "argv", ["poa.py"] + argv), contextlib.redirect_stdout(buf):
            try:
                poa.main()
            except SystemExit as e:
                code = e.code or 0
        return code, buf.getvalue()

    def test_cli_json_multiple_files(self):
        with tempfile.TemporaryDirectory() as d:
            a = pathlib.Path(d) / "a.txt"; a.write_text(FULL, encoding="utf-8")
            b = pathlib.Path(d) / "b.txt"; b.write_text(NO_GAIN, encoding="utf-8")
            code, out = self.run_main(["cost-gain", "--file", str(a), "--file", str(b), "--json"])
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertTrue(rep["read_only"])
        self.assertEqual([x["verdict"] for x in rep["results"]], ["ok", "warn"])

    def test_cli_text_human(self):
        code, out = self.run_main(["cost-gain", "--text", NO_GAIN])
        self.assertEqual(code, 0)
        self.assertIn("WARN", out)
        self.assertIn("warn only", out)

    def test_cli_needs_input(self):
        code, out = self.run_main(["cost-gain", "--json"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_cli_missing_file(self):
        code, out = self.run_main(["cost-gain", "--file", "/nonexistent/draft.txt", "--json"])
        self.assertEqual(code, 2)

    def test_version(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.23")


if __name__ == "__main__":
    unittest.main()
