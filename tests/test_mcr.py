"""Mocked tests for `poa.py mcr` (SUGGESTIONS.md #156, advisory minority-correct rate).
No network, no real key. Run: python3 -m unittest tests/test_mcr.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, socket, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_mcr", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
A = "poa1GRADERAAAAAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "poa1GRADERBBBBBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
C = "poa1GRADERCCCCCcccccccccccccccccccccccccccccccc"
D = "poa1GRADERDDDDDdddddddddddddddddddddddddddddddd"


def row(outcome, *jury):
    return {"answer": "ab" * 32, "from": D, "outcome": outcome,
            "jury": [{"grader": g, "aligned": a} for g, a in jury]}


def settled(ep, rows):
    return {"epoch": ep, "count": len(rows), "rows": rows}


# E81: ME dissents (3:1 minority) 4x, ties won 2x, ties lost 1x, majority 5x; one unanimous row,
# one expired row, one row where the smaller side won (verdict sided with ME and C).
S81 = settled(81,
              [row("passed", (A, True), (B, True), (C, True), (ME, False))] * 4
              + [row("failed", (ME, True), (A, True), (B, False), (C, False))] * 2
              + [row("passed", (ME, False), (A, False), (B, True), (C, True))]
              + [row("passed", (ME, True), (A, True), (B, True), (C, False))] * 5
              + [row("passed", (ME, True), (A, True))]
              + [row("expired")]
              + [row("failed", (ME, True), (C, True), (A, False), (B, False), (D, False))])
S82 = settled(82, [row("passed", (A, True), (B, True), (C, False))] * 3)


def ns(**kw):
    base = dict(address=ME, closes="", last=8, file=[], json=True, timeout=5, window=30, min_n=10)
    base.update(kw)
    return argparse.Namespace(**base)


def run(args):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            poa.cmd_mcr(args)
        except SystemExit as e:
            code = e.code or 0
    return buf.getvalue(), code


def no_net(*a, **k):
    raise AssertionError("network reached in a test")


class TestPure(unittest.TestCase):
    def test_own_counts(self):
        r = poa.mcr_report({81: S81, 82: S82}, ME)
        y = r["yours"]
        self.assertEqual(y["contested_seats"], 13)
        self.assertEqual(y["dissents"], 5)          # 4 lost 3:1 plus the 2:3 win
        self.assertEqual(y["dissents_vindicated"], 1)
        self.assertEqual((y["tie_seats"], y["ties_won"]), (3, 2))
        self.assertEqual(y["status"], "ok")
        self.assertAlmostEqual(y["dissent_rate"], round(5 / 13, 4))
        self.assertEqual(y["mcr"], 0.2)
        self.assertEqual(y["epochs"], [81, 81])
        self.assertEqual(r["rows"], {"contested": 16, "tied": 3, "minority_won": 1})
        self.assertNotIn(ME, json.dumps(r))
        for g in (A, B, C, D):
            self.assertNotIn(g, json.dumps(r))

    def test_unanimous_and_expired_ignored(self):
        r = poa.mcr_report({1: settled(1, [row("passed", (ME, True), (A, True)), row("expired")])}, ME)
        self.assertEqual(r["rows"]["contested"], 0)
        self.assertEqual(r["yours"]["status"], "n/a")
        self.assertIsNone(r["yours"]["mcr"])

    def test_min_n_is_na_not_zero(self):
        r = poa.mcr_report({82: S82}, C, min_n=10)
        self.assertEqual(r["yours"]["contested_seats"], 3)
        self.assertEqual(r["yours"]["status"], "n/a")
        self.assertIsNone(r["yours"]["dissent_rate"])
        r2 = poa.mcr_report({82: S82}, C, min_n=3)
        self.assertEqual(r2["yours"]["dissent_rate"], 1.0)
        self.assertEqual(r2["yours"]["mcr"], 0.0)

    def test_window_keeps_newest(self):
        r = poa.mcr_report({81: S81, 82: S82}, C, window=3, min_n=3)
        self.assertEqual(r["yours"]["epochs"], [82, 82])
        self.assertEqual(r["yours"]["dissents"], 3)

    def test_zero_dissent_and_grid(self):
        r = poa.mcr_report({81: S81}, "", min_n=5)
        n = r["network"]
        self.assertIsNone(r["yours"])
        self.assertEqual(n["graders_seen"], 5)
        self.assertGreaterEqual(n["graders_measured"], 3)
        self.assertTrue(all(isinstance(v, int) for v in n["dissent_x_mcr"].values()))
        self.assertEqual(sum(n["dissent_x_mcr"].values()), n["graders_measured"])

    def test_unavailable_is_unknown(self):
        r = poa.mcr_report({80: None, 81: S81}, ME)
        self.assertEqual(r["closes_unavailable"], [80])
        self.assertEqual(r["closes_measured"], [81])
        r2 = poa.mcr_report({80: None}, ME)
        self.assertFalse(r2["ok"])


class TestCommand(unittest.TestCase):
    def test_network_path_is_mocked(self):
        calls = []

        def fake(method, path, query=None, **kw):
            calls.append((method, path, query, kw.get("need_key")))
            if path == "/api/status":
                return {"epochs_closed": 82}
            if path == "/api/settled":
                return {81: S81, 82: S82}[query["epoch"]]
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(last=2))
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["closes_measured"], [81, 82])
        self.assertEqual(r["yours"]["dissents"], 5)
        self.assertTrue(all(m == "GET" and nk is False for m, _, _, nk in calls))
        self.assertEqual([c[1] for c in calls], ["/api/status", "/api/settled", "/api/settled"])
        self.assertNotIn(ME, out)

    def test_settled_error_is_unavailable(self):
        def fake(method, path, query=None, **kw):
            raise RuntimeError("boom")

        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa, "http_request", side_effect=no_net):
            out, code = run(ns(closes="81-82"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["closes_unavailable"], [81, 82])

    def test_offline_files_and_text(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "settled-81.json")
        with open(p, "w") as fh:
            json.dump(S81, fh)
        with mock.patch.object(poa, "api_call", side_effect=no_net), \
                mock.patch.object(poa, "http_request", side_effect=no_net):
            out, code = run(ns(file=[p], json=False))
        self.assertEqual(code, 0)
        self.assertIn("mcr (mcr-1, advisory)", out)
        self.assertIn("MCR 20.0%", out)
        self.assertNotIn(ME, out)

    def test_bad_address_refused(self):
        with mock.patch.object(poa, "api_call", side_effect=no_net):
            out, code = run(ns(address="not-an-address"))
        self.assertEqual(code, 2)

    def test_parser_has_mcr(self):
        with mock.patch.object(poa, "api_call", side_effect=no_net), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch("sys.argv", ["poa.py", "mcr", "--help"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                poa.main()
        self.assertIn("--min-n", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
