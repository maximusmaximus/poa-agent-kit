"""Mocked tests for `poa.py capacity` (SUGGESTIONS.md #160, grading-capacity line per close).
No network, no real key. Run: python3 -m unittest tests/test_capacity.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, socket, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_capacity", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)


def addr(i):
    return "poa1GRADER" + format(i, "06d") + "x" * 30


def close_report(ep, rows, empty, loads, distinct, sealed=None, dealt=1000, passed=70, failed=30):
    sl = {addr(i): n for i, n in enumerate(loads)}
    return {"close_epoch": ep,
            "panels": {"rows": rows, "fill_hist": {"1": 1, "2": 1, "3": 1, "empty": empty, "gte_quorum": 1},
                       "rows_reaching_quorum": 5},
            "seats": {"dealt": dealt, "sealed": sum(loads) if sealed is None else sealed, "expired_unsealed": 0},
            "concentration": {"distinct_graders": distinct},
            "settled_split": {"passed": passed, "failed": failed},
            "panel_pool": {"size": 4200},
            "seal_load": sl}


# 10 addresses: loads 1..10 (sum 55); median (nearest rank) 5, p90 9, top decile = 10/55
CR84 = close_report(84, rows=200, empty=40, loads=list(range(1, 11)), distinct=10)
# 20 addresses all with 2 seals plus one zero entry (ignored); distinct_graders disagrees
CR85 = close_report(85, rows=200, empty=50, loads=[2] * 20 + [0], distinct=9, passed=60, failed=40)


def ns(**kw):
    base = dict(closes="", last=6, file=[], json=True, timeout=5)
    base.update(kw)
    return argparse.Namespace(**base)


def run(args):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            poa.cmd_capacity(args)
        except SystemExit as e:
            code = e.code or 0
    return buf.getvalue(), code


def no_net(*a, **k):
    raise AssertionError("network reached in a test")


class TestPure(unittest.TestCase):
    def test_line(self):
        ln = poa.capacity_line(CR84)
        self.assertEqual(ln["epoch"], 84)
        self.assertEqual(ln["empty_share"], 0.2)
        self.assertEqual(ln["pass_rate"], 0.7)
        sl = ln["seal_load"]
        self.assertEqual((sl["addresses"], sl["seals"], sl["median"], sl["p90"], sl["max"]), (10, 55, 5, 9, 10))
        self.assertEqual(sl["top_decile_share"], round(10 / 55, 4))
        self.assertEqual(ln["sealed_per_dealt"], 0.055)
        self.assertTrue(ln["recon"]["agree"])

    def test_report_delta_and_flags(self):
        r = poa.capacity_report({84: CR84, 85: CR85})
        self.assertTrue(r["ok"])
        self.assertEqual(r["closes_measured"], [84, 85])
        l85 = r["lines"][1]
        self.assertEqual(l85["empty_share_delta"], 0.05)
        self.assertEqual(l85["seal_load"]["addresses"], 20)   # zero-seal entry ignored
        self.assertEqual(l85["seal_load"]["median"], 2)
        self.assertFalse(l85["recon"]["agree"])
        self.assertEqual(len(r["flags"]), 1)
        self.assertIn("E85", r["flags"][0])
        self.assertIsNone(r["lines"][0]["empty_share_delta"])

    def test_no_addresses_in_output(self):
        blob = json.dumps(poa.capacity_report({84: CR84, 85: CR85}))
        self.assertNotIn("poa1", blob)

    def test_unavailable_is_unknown(self):
        r = poa.capacity_report({83: None, 84: CR84, 85: {"error": "x"}})
        self.assertEqual(r["closes_measured"], [84])
        self.assertEqual(r["closes_unavailable"], [83, 85])
        r2 = poa.capacity_report({84: None})
        self.assertFalse(r2["ok"])

    def test_wrong_epoch_body_skipped(self):
        r = poa.capacity_report({86: CR85})
        self.assertEqual(r["closes_measured"], [])
        self.assertIn(86, r["closes_unavailable"])

    def test_seal_sum_mismatch_flag(self):
        cr = close_report(84, rows=100, empty=10, loads=[5] * 10, distinct=10, sealed=500)
        r = poa.capacity_report({84: cr})
        self.assertTrue(any("seats.sealed" in f for f in r["flags"]))


class TestCommand(unittest.TestCase):
    def test_live_path_mocked(self):
        calls = []

        def fake(method, path, query=None, need_key=True, **kw):
            calls.append((method, path, dict(query or {}), need_key))
            if path == "/api/status":
                return {"epochs_closed": 85}
            if path == "/api/close-report":
                return {84: CR84, 85: CR85}.get(query["epoch"])
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(last=2))
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["closes_measured"], [84, 85])
        self.assertTrue(all(c[0] == "GET" and c[3] is False for c in calls))
        self.assertEqual([c[2].get("epoch") for c in calls if c[1] == "/api/close-report"], [84, 85])
        self.assertNotIn("poa1", out)

    def test_closes_range_and_text(self):
        def fake(method, path, query=None, need_key=True, **kw):
            if path == "/api/close-report":
                if query["epoch"] == 83:
                    raise RuntimeError("timeout")
                return {84: CR84, 85: CR85}[query["epoch"]]
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(closes="83-85", json=False))
        self.assertEqual(code, 0)
        self.assertIn("unavailable [83]", out)
        self.assertIn("E85", out)
        self.assertIn("flag: E85", out)
        self.assertNotIn("poa1", out)

    def test_status_unreadable(self):
        with mock.patch.object(poa, "api_call", side_effect=RuntimeError("down")), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns())
        self.assertEqual(code, 2)
        self.assertIn("epochs_closed", out)

    def test_offline_files(self):
        d = tempfile.mkdtemp()
        paths = []
        for ep, body in ((84, CR84), (85, CR85)):
            p = os.path.join(d, f"close-report-{ep}.json")
            with open(p, "w") as fh:
                json.dump(body, fh)
            paths.append(p)
        with mock.patch.object(poa, "api_call", side_effect=no_net), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(file=paths))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["closes_measured"], [84, 85])

    def test_parser_wired(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.25")


if __name__ == "__main__":
    unittest.main()
