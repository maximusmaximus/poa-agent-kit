"""Mocked tests for `poa.py juror-audit` (SUGGESTIONS.md #128).
No network, no real key. Run: python3 -m unittest tests/test_juror_audit.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_ja", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERADDRESSyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"


def row(outcome, *jury):
    return {"answer": "ab" * 32, "from": OTHER, "outcome": outcome,
            "jury": [{"grader": g, "aligned": a} for g, a in jury]}


def settled(ep, rows):
    return {"epoch": ep, "count": len(rows), "rows": rows}


# E57: strict shape (misaligned on passed rows). E58: lenient shape.
S57 = settled(57, [row("passed", (ME, False), (OTHER, True))] * 4
              + [row("passed", (ME, True))] * 5 + [row("failed", (ME, True))] * 2
              + [row("expired", (ME, True))])
S58 = settled(58, [row("failed", (ME, False))] * 3 + [row("passed", (ME, True))] * 6
              + [row("passed", (OTHER, False))] * 10)


def ns(**kw):
    base = dict(address=ME, closes="", last=8, file=[], json=True, timeout=5)
    base.update(kw)
    return argparse.Namespace(**base)


def run(args):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            poa.cmd_juror_audit(args)
        except SystemExit as e:
            code = e.code or 0
    return buf.getvalue(), code


class TestPure(unittest.TestCase):
    def test_range(self):
        self.assertEqual(poa.parse_close_range("50-53"), [50, 51, 52, 53])
        self.assertEqual(poa.parse_close_range("58-56,50"), [50, 56, 57, 58])
        self.assertEqual(poa.parse_close_range(""), [])

    def test_split_and_direction(self):
        r = poa.juror_audit({57: S57, 58: S58}, ME)
        e57, e58 = r["per_close"]
        self.assertEqual((e57["passed_aligned"], e57["passed_misaligned"],
                          e57["failed_aligned"], e57["failed_misaligned"]), (5, 4, 2, 0))
        self.assertEqual(e57["expired_seats"], 1)
        self.assertEqual(e57["direction"], "strict")
        self.assertEqual((e58["passed_aligned"], e58["failed_misaligned"]), (6, 3))
        self.assertEqual(e58["direction"], "lenient")
        p = r["pooled"]
        self.assertEqual(p["seats"], 20)
        self.assertEqual(p["direction"], "mixed")  # 4 strict vs 3 lenient
        self.assertNotIn(ME, json.dumps(r))

    def test_few_and_none(self):
        s = settled(1, [row("passed", (ME, False))] + [row("passed", (ME, True))] * 3)
        self.assertEqual(poa.juror_audit({1: s}, ME)["pooled"]["direction"], "too-few-misaligned")
        self.assertEqual(poa.juror_audit({1: settled(1, [])}, ME)["pooled"]["direction"], "no-seats")
        s2 = settled(1, [row("failed", (ME, True))])
        self.assertEqual(poa.juror_audit({1: s2}, ME)["pooled"]["direction"], "aligned")

    def test_unavailable_is_not_zero(self):
        r = poa.juror_audit({56: None, 57: S57}, ME)
        self.assertEqual(r["closes_unavailable"], [56])
        self.assertEqual(r["closes_measured"], 1)
        self.assertEqual(r["per_close"][0]["status"], "unavailable")
        self.assertNotIn("seats", r["per_close"][0])


class TestCommand(unittest.TestCase):
    def test_network_path_is_mocked(self):
        calls = []

        def fake(method, path, query=None, **kw):
            calls.append((method, path, query, kw.get("need_key")))
            if path == "/api/settled":
                return {57: S57, 58: S58}[query["epoch"]]
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            out, code = run(ns(closes="57-58"))
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["pooled"]["passed_misaligned"], 4)
        self.assertTrue(all(c[1] == "/api/settled" and c[3] is False for c in calls))
        self.assertNotIn(ME, out)

    def test_default_last_uses_status(self):
        def fake(method, path, query=None, **kw):
            if path == "/api/status":
                return {"epochs_closed": 58}
            if path == "/api/settled":
                if query["epoch"] == 57:
                    raise poa.ApiError("timeout") if hasattr(poa, "ApiError") else RuntimeError("timeout")
                return S58
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            out, code = run(ns(last=2))
        r = json.loads(out)
        self.assertEqual(r["closes_unavailable"], [57])
        self.assertEqual(r["pooled"]["direction"], "lenient")

    def test_offline_files_no_network(self):
        d = tempfile.mkdtemp()
        f1 = os.path.join(d, "settled-57.json")
        with open(f1, "w") as fh:
            json.dump(S57, fh)
        with mock.patch.object(poa, "api_call", side_effect=AssertionError("network")):
            out, code = run(ns(file=[f1], json=False))
        self.assertEqual(code, 0)
        self.assertIn("strict", out)
        self.assertIn("E57", out)
        self.assertNotIn(ME, out)

    def test_bad_address(self):
        with mock.patch.object(poa, "api_call", side_effect=AssertionError("network")), \
             mock.patch.object(poa, "local_address", return_value=""):
            out, code = run(ns(address=""))
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
