"""Mocked tests for `poa.py juror-audit --margin` (SUGGESTIONS.md #167).
No network, no real key: api_call, http_request and socket.create_connection are patched to
fail (or to return canned /api/settled bodies). Run: python3 -m unittest tests/test_juror_margin.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, socket, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_jm", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
O = ["poa1OTHER%sxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" % i for i in range(6)]


def no_net(*a, **k):
    raise AssertionError("network call in an offline test")


def row(outcome, me_aligned, others):
    """others: list of aligned bools for other jurors; me_aligned None = not on the jury."""
    jury = [{"grader": O[i], "aligned": a} for i, a in enumerate(others)]
    if me_aligned is not None:
        jury.append({"grader": ME, "aligned": me_aligned})
    return {"answer": "cd" * 32, "from": O[5], "outcome": outcome, "jury": jury}


def settled(ep, rows):
    return {"epoch": ep, "count": len(rows), "rows": rows}


# Near-tie rows: 2 of 4 aligned -> margin 0; mid: 3 of 4 aligned -> |1.5-1| = 0.5;
# lopsided: 4 of 4 aligned -> 1.0.
NEAR_MISS = row("passed", False, [True, True, False])      # me strict miss, a=2/4
NEAR_HIT = row("passed", True, [True, False, False])        # a=2/4
MID_HIT = row("failed", True, [True, True, False])          # a=3/4
MID_MISS_LEN = row("failed", False, [True, True, True])     # a=3/4, lenient miss
LOP_HIT = row("passed", True, [True, True, True])           # a=4/4
LOP_MISS = row("passed", False, [True, True, True, True, True])  # a=5/6 -> 0.667 lopsided


def ns(**kw):
    base = dict(address=ME, closes="", last=8, file=[], json=True, timeout=5, margin=True,
                min_seats=poa.MARGIN_MIN_SEATS_DEFAULT)
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


class TestBuckets(unittest.TestCase):
    def test_bucket_edges(self):
        self.assertEqual(poa.margin_bucket(2, 4), "near_tie")      # 0.0
        self.assertEqual(poa.margin_bucket(5, 8), "near_tie")      # 0.25 inclusive
        self.assertEqual(poa.margin_bucket(3, 4), "mid")           # 0.5
        self.assertEqual(poa.margin_bucket(4, 5), "lopsided")      # 0.6 inclusive
        self.assertEqual(poa.margin_bucket(0, 3), "lopsided")      # all misaligned
        self.assertEqual(poa.margin_bucket(1, 1), "lopsided")      # one-seat jury
        self.assertIsNone(poa.margin_bucket(0, 0))

    def test_binom_tails(self):
        lo, hi = poa._binom_tails(0, 3, 0.5)
        self.assertAlmostEqual(lo, 0.125)
        self.assertAlmostEqual(hi, 1.0)
        lo, hi = poa._binom_tails(5, 10, 0.5)
        self.assertAlmostEqual(lo, 0.623, places=3)
        self.assertAlmostEqual(hi, 0.623, places=3)
        lo, hi = poa._binom_tails(1500, 3000, 0.54)   # large n must not overflow
        self.assertLess(lo, 0.001)
        self.assertEqual(poa._binom_tails(2, 2, 1.0), (1.0, 1.0))


class TestSplit(unittest.TestCase):
    def test_counts_directions_and_floor(self):
        rows = [NEAR_MISS] * 3 + [NEAR_HIT] * 3 + [MID_HIT] * 2 + [MID_MISS_LEN] + [LOP_HIT] * 5 + [LOP_MISS]
        m = poa.juror_margin_split({91: settled(91, rows)}, ME, min_seats=5)
        b = m["buckets"]
        self.assertEqual((b["near_tie"]["seats"], b["near_tie"]["aligned"], b["near_tie"]["strict_misses"]), (6, 3, 3))
        self.assertEqual(b["near_tie"]["status"], "ok")
        self.assertEqual(b["near_tie"]["aligned_rate"], 0.5)
        # network near-tie rate = 2/4 on every near-tie row
        self.assertEqual(b["near_tie"]["network_aligned_rate"], 0.5)
        self.assertEqual(b["mid"]["seats"], 3)
        self.assertEqual(b["mid"]["lenient_misses"], 1)
        self.assertEqual(b["mid"]["status"], "n/a")          # below the 5-seat floor
        self.assertIsNone(b["mid"]["aligned_rate"])
        self.assertEqual((b["lopsided"]["seats"], b["lopsided"]["strict_misses"]), (6, 1))
        self.assertEqual(m["seats"], 15)
        self.assertEqual((m["strict_misses"], m["lenient_misses"]), (4, 1))
        self.assertEqual(m["metric_version"], "margin-1")
        self.assertNotIn(ME, json.dumps(m))

    def test_reading_lopsided_vs_close_call(self):
        # Clear-jury disagreement: we miss every lopsided row the rest of the jury agreed on.
        lop = [row("passed", False, [True] * 5)] * 6 + [row("passed", True, [True] * 5)] * 20
        m = poa.juror_margin_split({1: settled(1, lop)}, ME)
        self.assertEqual(m["reading"], "lopsided-misses")
        self.assertLess(m["buckets"]["lopsided"]["p_at_or_below"], 0.05)
        # Only near-tie rows are low; lopsided fine -> close-call threshold.
        near = ([row("passed", False, [True, True, True, False, False])] * 8
                + [row("passed", True, [True, True, False, False, False])] * 1)
        near += [row("passed", True, [True] * 5)] * 8
        near += [row("passed", None, [True, True, True, False])] * 40   # network near-tie rows, me absent
        m2 = poa.juror_margin_split({1: settled(1, near)}, ME)
        self.assertEqual(m2["buckets"]["lopsided"]["aligned_rate"], 1.0)
        self.assertEqual(m2["reading"], "close-call-threshold")
        # Too few seats anywhere.
        m3 = poa.juror_margin_split({1: settled(1, [NEAR_MISS, LOP_HIT])}, ME)
        self.assertEqual(m3["reading"], "too-few-seats")

    def test_expired_unavailable_and_pooling(self):
        rows = [row("expired", False, [True])] + [NEAR_HIT] * 3
        m = poa.juror_margin_split({90: None, 91: settled(91, rows), 92: settled(92, [NEAR_HIT] * 2)}, ME)
        self.assertEqual(m["closes_measured"], [91, 92])
        self.assertEqual(m["buckets"]["near_tie"]["seats"], 5)       # pooled across closes
        self.assertEqual(m["buckets"]["lopsided"]["seats"], 0)       # expired row ignored


class TestCommand(unittest.TestCase):
    def test_network_path_is_mocked(self):
        calls = []
        rows = [NEAR_MISS] * 5 + [LOP_HIT] * 5

        def fake(method, path, query=None, **kw):
            calls.append((path, kw.get("need_key")))
            if path == "/api/settled":
                return settled(query["epoch"], rows)
            raise AssertionError(path)

        with mock.patch.object(poa, "api_call", side_effect=fake), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(closes="92-93"))
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["margin"]["buckets"]["near_tie"]["seats"], 10)
        self.assertEqual(r["margin"]["buckets"]["near_tie"]["strict_misses"], 10)
        self.assertEqual(r["pooled"]["passed_misaligned"], 10)       # existing audit unchanged
        self.assertTrue(all(p == "/api/settled" and k is False for p, k in calls))
        self.assertNotIn(ME, out)

    def test_without_flag_no_margin_block(self):
        with mock.patch.object(poa, "api_call", side_effect=lambda m, p, query=None, **k: settled(query["epoch"], [NEAR_HIT])), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, _ = run(ns(closes="93", margin=False))
        self.assertNotIn("margin", json.loads(out))

    def test_offline_file_text_output(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "settled-93.json")
        with open(f, "w") as fh:
            json.dump(settled(93, [NEAR_MISS] * 5 + [LOP_HIT] * 6), fh)
        with mock.patch.object(poa, "api_call", side_effect=no_net), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch.object(socket, "create_connection", side_effect=no_net):
            out, code = run(ns(file=[f], json=False))
        self.assertEqual(code, 0)
        self.assertIn("margin split (margin-1", out)
        self.assertIn("near-tie", out)
        self.assertIn("reading:", out)
        self.assertNotIn(ME, out)

    def test_parser_has_flags(self):
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=no_net), \
                mock.patch.object(poa, "http_request", side_effect=no_net), \
                mock.patch("sys.argv", ["poa.py", "juror-audit", "--help"]), \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                poa.main()
        self.assertIn("--margin", buf.getvalue())
        self.assertIn("--min-seats", buf.getvalue())

    def test_version(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.19")


if __name__ == "__main__":
    unittest.main()
