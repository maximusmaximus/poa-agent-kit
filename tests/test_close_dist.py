"""Mocked tests for `poa.py close-dist` (SUGGESTIONS.md #182, kit half). No network, no real key:
`http_request` (the transport under api_call and fetch_epoch_body) is patched in every command test
and the fake refuses any other URL.
Run: python3 -m unittest tests/test_close_dist.py"""
import argparse, contextlib, hashlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_cd", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_795_000_000
H = 21600


def addr(i):
    return f"poa1ACCT{i:04d}" + "x" * 30


def tx(i):
    return hashlib.sha256(f"row{i}".encode()).hexdigest()


def body(n, rewards, eligible=(), settled=(), pending=None, answer=None, grading=None):
    fp = {"channels": 1, "trigger": 1.25, "referral": {"E": "10.0"}}
    if answer is not None:
        fp["answer"] = answer
    if grading is not None:
        fp["grading"] = grading
    return {"epoch": n, "ts": T0 + n * H, "rewards": rewards, "challenges": {}, "weights": {}, "propagation": {},
            "basis": {"challenge_eligible": list(eligible), "settled": list(settled), "pending": pending or {},
                      "final_payout": fp}}


def closes():
    # E7: pending book holds rows 0..9 (since 1..6)
    pend = {tx(i): {"kind": "short", "since": 1 + (i % 6), "panel": []} for i in range(10)}
    e7 = body(7, {addr(0): "1.0"}, pending=pend)
    # E8: ten payees 100..1000, two zero entries, eligible = top five; rows 0..9 settle (rows 0-5 passed)
    rewards = {addr(i): f"{100.0 * (i + 1):.6f}" for i in range(10)}
    rewards[addr(98)] = "0.000000"
    rewards[addr(99)] = "0"
    rewards[addr(4)] = "400.000000"
    rewards[addr(10)] = "400.000000"   # mode 400 paid to 2 accounts
    settled = [{"answer": tx(i), "from": addr(i), "passed": i < 6, "jury": []} for i in range(10)]
    settled.append({"answer": tx(500), "from": addr(1), "passed": True, "jury": []})   # not in E7's book
    ans = {"W": 4, "E": "2000.0", "capped": {addr(1): "1.0", addr(2): "1.0"},
           "repooled": {addr(i): f"{10.0 * (i + 1)}" for i in range(5)}, "burned": "0"}
    gr = {"W": 10, "E": "500.0", "capped": {}, "repooled": {}, "burned": "0"}
    e8 = body(8, rewards, eligible=[addr(i) for i in range(5, 10)], settled=settled, answer=ans, grading=gr)
    return [e7, e8]


class CloseDistReportTests(unittest.TestCase):
    def test_nearest_rank(self):
        s = list(range(1, 11))
        self.assertEqual(poa.nearest_rank(s, 10), 1)
        self.assertEqual(poa.nearest_rank(s, 50), 5)
        self.assertEqual(poa.nearest_rank(s, 90), 9)
        self.assertEqual(poa.nearest_rank(s, 100), 10)
        self.assertEqual(poa.nearest_rank([7], 10), 7)
        self.assertIsNone(poa.nearest_rank([], 50))
        self.assertEqual(poa.nearest_rank(list(range(1, 2390)), 10), 239)

    def test_quantiles_split_and_mode(self):
        r = poa.close_dist_report(closes(), last=1)["rows"][0]
        self.assertEqual(r["close"], 8)
        self.assertEqual(r["listed"], 13)
        self.assertEqual(r["zero_entries"], 2)
        self.assertEqual(r["paid"]["n"], 11)
        self.assertEqual(r["paid"]["p50"], 400.0)
        self.assertEqual(r["paid"]["p90"], 900.0)
        self.assertEqual(r["in_eligible"]["n"], 5)
        self.assertEqual(r["in_eligible"]["p50"], 800.0)
        self.assertEqual(r["out_of_eligible"]["n"], 6)
        self.assertEqual(r["out_of_eligible"]["p50"], 300.0)
        self.assertEqual(r["eligible_median_ratio"], 2.67)
        self.assertEqual(r["mode"], {"value": 400.0, "accounts": 3})

    def test_channels_per_unit_and_repool(self):
        r = poa.close_dist_report(closes(), last=1)["rows"][0]
        a = r["channels"]["answer"]
        self.assertEqual(a["per_unit"], 500.0)
        self.assertEqual(a["capped_accounts"], 2)
        self.assertEqual(a["repooled_total"], 150.0)
        self.assertEqual(a["repool_recipients"], 5)
        self.assertEqual(a["repool_top_share"], 1.0)
        self.assertEqual(r["channels"]["grading"]["per_unit"], 50.0)
        self.assertEqual(r["channels"]["referral_E"], 10.0)

    def test_settle_lag_from_previous_pending_book(self):
        lag = poa.close_dist_report(closes(), last=1)["rows"][0]["settle_lag"]
        self.assertTrue(lag["known"])
        self.assertEqual(lag["settled_rows"], 11)
        self.assertEqual(lag["matched"], 10)
        self.assertEqual(lag["unmatched"], 1)
        # since = 1 + i%6 for i 0..9 -> lags 7,6,5,4,3,2,7,6,5,4
        self.assertEqual(lag["all"]["p50"], 5)
        self.assertEqual(lag["all"]["p10"], 2)
        self.assertEqual(lag["passed"]["n"], 6)
        self.assertEqual(lag["fast_path"]["all"], 0)

    def test_lag_unknown_without_previous_close(self):
        r = poa.close_dist_report(closes()[1:], last=1)["rows"][0]
        self.assertFalse(r["settle_lag"]["known"])

    def test_own_rank_without_address(self):
        eps = closes()
        eps[1]["rewards"][ME] = "850.0"
        r = poa.close_dist_report(eps, last=1, me=ME)
        self.assertEqual(r["rows"][0]["you"]["rank_pct"], round(100 * 9 / 12, 1))
        self.assertFalse(r["rows"][0]["you"]["eligible"])
        self.assertNotIn(ME, json.dumps(r))
        self.assertNotIn(addr(1), json.dumps(r))

    def test_unpaid_rank_is_none_and_empty_close(self):
        r = poa.close_dist_report(closes(), last=1, me=ME)["rows"][0]
        self.assertIsNone(r["you"]["rank_pct"])
        eps = closes()
        eps[1]["rewards"] = {}
        r = poa.close_dist_report(eps, last=1)["rows"][0]
        self.assertEqual(r["paid"]["n"], 0)
        self.assertIsNone(r["paid"]["p50"])
        self.assertIsNone(r["mode"])
        self.assertFalse(poa.close_dist_report([])["ok"])


class CloseDistCommandTests(unittest.TestCase):
    def fake(self, eps):
        raws = {e["epoch"]: json.dumps(e).encode() for e in eps}
        index = {"v": 1, "closed_through": max(raws), "count": len(raws),
                 "epochs": [{"n": n, "sha256": hashlib.sha256(r).hexdigest(), "bytes": len(r), "close_ts": T0 + n * H}
                            for n, r in raws.items()]}
        self.raws = raws
        self.calls = []

        def _fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            self.calls.append((method, url, dict(headers or {})))
            if url == f"{poa.BASE_URL}/api/epochs":
                return json.dumps(index).encode()
            for n, r in raws.items():
                if url == f"{poa.BASE_URL}/api/epochs/{n}":
                    return r
            raise AssertionError(f"unexpected request {url}")
        return _fake

    def run_cmd(self, eps, http=None, **kw):
        args = argparse.Namespace(last=kw.get("last", 1), address=kw.get("address", ME), no_me=kw.get("no_me", False),
                                  line=kw.get("line", False), json=kw.get("json", False), timeout=5)
        out = io.StringIO()
        code = 0
        with mock.patch.object(poa, "http_request", http or self.fake(eps)), contextlib.redirect_stdout(out):
            try:
                poa.cmd_close_dist(args)
            except SystemExit as ex:
                code = ex.code
        return out.getvalue(), code

    def test_keyless_hash_pinned_and_address_free(self):
        out, code = self.run_cmd(closes(), json=True)
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual([x["close"] for x in r["rows"]], [8])
        self.assertEqual(r["rows"][0]["sha256"], hashlib.sha256(self.raws[8]).hexdigest())
        self.assertTrue(r["rows"][0]["settle_lag"]["known"])   # the close before the window was read too
        self.assertNotIn(ME, out)
        self.assertEqual(len(self.calls), 3)
        for method, url, headers in self.calls:
            self.assertEqual(method, "GET")
            self.assertNotIn("X-Api-Key", headers)
            self.assertNotIn("pk_test_fake", url)
            self.assertNotIn(ME, url)

    def test_text_and_line_output(self):
        out, code = self.run_cmd(closes())
        self.assertEqual(code, 0)
        self.assertIn("close-dist: payout distribution, newest close E8", out)
        self.assertIn("answer: E 2.0K / W 4 = 500.00 per unit", out)
        self.assertIn("body sha256 ", out)
        out, _ = self.run_cmd(closes(), line=True, no_me=True)
        self.assertTrue(out.startswith("E8 paid=11 p50=400.00 in/out=800.00/300.00 per_unit=500.00 W=4 lag_p50=5"))
        self.assertNotIn("you=", out)

    def test_hash_mismatch_fails_closed(self):
        fake = self.fake(closes())

        def tampered(method, url, **kw):
            raw = fake(method, url, **kw)
            return raw.replace(b'"900.000000"', b'"901.000000"') if url.endswith("/api/epochs/8") else raw
        out, code = self.run_cmd(closes(), http=tampered, json=True)
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_parser_wired(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.25")


if __name__ == "__main__":
    unittest.main()
