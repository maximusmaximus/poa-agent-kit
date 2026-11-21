"""Mocked tests for `poa.py close-why` (SUGGESTIONS.md #179, kit half). No network, no real key:
`http_request` (the transport under api_call and fetch_epoch_body) is patched in every command test.
Run: python3 -m unittest tests/test_close_why.py"""
import argparse, contextlib, hashlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_cw", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_795_000_000
H = 21600


def body(n, rewards, eligible=(), settled=(), attest=None, challenges=None, ref_paid=None, ref_work=None,
         capped=None, marks=None, removed=()):
    return {"epoch": n, "ts": T0 + n * H, "rewards": rewards, "challenges": challenges or {},
            "weights": {}, "propagation": {},
            "basis": {"attestations": attest or {}, "challenge_eligible": list(eligible), "settled": list(settled),
                      "referral2": {"paid": ref_paid or {}, "work": ref_work or {}},
                      "ceiling": {"capped": capped or {}, "repooled": {}},
                      "marks": marks or {}, "established_removed": list(removed), "slashed": [], "ejected": []}}


def srow(frm, passed, grader=OTHER):
    return {"answer": hashlib.sha256(f"{frm}{passed}".encode()).hexdigest(), "from": frm, "passed": passed,
            "jury": [{"grader": grader, "aligned": True}]}


def closes():
    full = body(7, {ME: "500.0", OTHER: "300.0"}, eligible=[ME, OTHER], settled=[srow(ME, True), srow(OTHER, True)],
                attest={ME: 100, OTHER: 50}, challenges={ME: "600.0"}, ref_work={ME: "600.0"})
    # E8: our good answer still pending at the close -> not eligible, no tally, referral share only, payout drops
    drop = body(8, {ME: "40.0", OTHER: "300.0"}, eligible=[OTHER], settled=[srow(OTHER, True, grader=ME)],
                attest={OTHER: 50}, challenges={ME: "80.0"}, ref_paid={ME: "80.0"})
    # E9: same, payout not under half of E8 -> reduced
    reduced = body(9, {ME: "45.0", OTHER: "300.0"}, eligible=[OTHER], settled=[srow(OTHER, True)],
                   attest={OTHER: 50}, challenges={ME: "90.0"}, ref_paid={ME: "90.0"})
    return [full, drop, reduced]


class CloseWhyReportTests(unittest.TestCase):
    def test_full_close_is_paid_without_reasons(self):
        r = poa.close_why_report(closes(), ME, last=3)
        row = r["rows"][0]
        self.assertEqual(row["verdict"], "paid")
        self.assertTrue(row["eligible"] and row["attested"] and row["paid"])
        self.assertEqual(row["passed_here"], 1)
        self.assertEqual(row["reasons"], [])

    def test_pending_answer_close_names_fields(self):
        r = poa.close_why_report(closes(), ME, last=3)
        row = r["rows"][1]
        self.assertEqual(row["verdict"], "dropped")
        codes = {x["code"]: x["field"] for x in row["reasons"]}
        self.assertEqual(codes["no_passed_row_settled"], "basis.settled")
        self.assertEqual(codes["not_challenge_eligible"], "basis.challenge_eligible")
        self.assertEqual(codes["attestations_not_tallied"], "basis.attestations")
        self.assertIn("entitlement_referral_share_only", codes)
        self.assertNotIn("unreconciled", codes)
        self.assertEqual(row["lines"]["jury_seats_settled"], 1)
        self.assertEqual(r["rows"][2]["verdict"], "reduced")
        self.assertEqual(r["newest_verdict"], "reduced")

    def test_zero_payout_without_explanation_is_unreconciled(self):
        eps = closes()
        eps[2]["rewards"].pop(ME)
        eps[2]["basis"]["challenge_eligible"].append(ME)
        eps[2]["basis"]["attestations"][ME] = 3
        eps[2]["basis"]["settled"].append(srow(ME, True))
        row = poa.close_why_report(eps, ME, last=1)["rows"][0]
        self.assertEqual(row["verdict"], "zero")
        self.assertEqual([x["code"] for x in row["reasons"]], ["unreconciled"])

    def test_zero_ceiling_marks_and_removed(self):
        eps = closes()
        b = eps[2]["basis"]
        eps[2]["rewards"].pop(ME)
        b["challenge_eligible"].append(ME); b["attestations"][ME] = 3; b["settled"].append(srow(ME, True))
        b["ceiling"]["capped"][ME] = 0
        b["marks"][ME] = 1
        b["established_removed"].append(ME)
        codes = [x["code"] for x in poa.close_why_report(eps, ME, last=1)["rows"][0]["reasons"]]
        for c in ("ceiling_factor_zero", "marked", "established_removed"):
            self.assertIn(c, codes)
        self.assertNotIn("unreconciled", codes)

    def test_passed_but_not_eligible_and_network_empty(self):
        eps = closes()
        eps[2]["basis"]["settled"].append(srow(ME, True))
        codes = [x["code"] for x in poa.close_why_report(eps, ME, last=1)["rows"][0]["reasons"]]
        self.assertIn("passed_but_not_eligible", codes)
        self.assertNotIn("no_passed_row_settled", codes)
        eps[2]["rewards"] = {}
        row = poa.close_why_report(eps, ME, last=1)["rows"][0]
        self.assertEqual(row["verdict"], "zero")
        self.assertIn("network_empty_close", [x["code"] for x in row["reasons"]])

    def test_network_line(self):
        nw = poa.close_why_report(closes(), ME, last=3)["rows"][1]["network"]
        self.assertEqual(nw["eligible"], 1)
        self.assertEqual(nw["passed_authors"], 1)
        self.assertEqual(nw["eligible_without_passed_row"], 0)
        self.assertEqual(nw["payees"], 2)
        self.assertEqual(nw["median_payout"], "170.000000")

    def test_no_address(self):
        self.assertFalse(poa.close_why_report(closes(), "", last=2)["ok"])
        self.assertFalse(poa.close_why_report([], ME)["ok"])

    def test_report_never_contains_address(self):
        txt = json.dumps(poa.close_why_report(closes(), ME, last=3))
        self.assertNotIn(ME, txt)
        self.assertNotIn(OTHER, txt)


class CloseWhyCommandTests(unittest.TestCase):
    def fake(self, eps):
        raws = {e["epoch"]: json.dumps(e).encode() for e in eps}
        index = {"v": 1, "closed_through": max(raws), "count": len(raws),
                 "epochs": [{"n": n, "sha256": hashlib.sha256(r).hexdigest(), "bytes": len(r), "close_ts": T0 + n * H}
                            for n, r in raws.items()]}
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

    def run_cmd(self, eps, **kw):
        args = argparse.Namespace(last=kw.get("last", 2), address=ME, line=kw.get("line", False),
                                  json=kw.get("json", False), timeout=5)
        out = io.StringIO()
        code = 0
        with mock.patch.object(poa, "http_request", self.fake(eps)), contextlib.redirect_stdout(out):
            try:
                poa.cmd_close_why(args)
            except SystemExit as ex:
                code = ex.code
        return out.getvalue(), code

    def test_keyless_and_address_free(self):
        out, code = self.run_cmd(closes(), json=True)
        self.assertEqual(code, 3)
        r = json.loads(out)
        self.assertEqual([x["close"] for x in r["rows"]], [8, 9])
        self.assertEqual(r["rows"][0]["previous_payout"], "500.000000")
        self.assertNotIn(ME, out)
        for method, url, headers in self.calls:
            self.assertEqual(method, "GET")
            self.assertNotIn("X-Api-Key", headers)
            self.assertNotIn("pk_test_fake", url)
            self.assertNotIn(ME, url)

    def test_line_output_and_exit_zero_when_paid(self):
        eps = closes()[:1]
        out, code = self.run_cmd(eps, line=True, last=1)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("E7 eligible=yes tally=yes rewards=yes (500.000000) verdict=paid"))

    def test_hash_mismatch_fails_closed(self):
        eps = closes()
        fake = self.fake(eps)

        def tampered(method, url, **kw):
            raw = fake(method, url, **kw)
            return raw.replace(b'"40.0"', b'"41.0"') if url.endswith("/api/epochs/8") else raw
        args = argparse.Namespace(last=2, address=ME, line=False, json=True, timeout=5)
        out = io.StringIO()
        with mock.patch.object(poa, "http_request", tampered), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                poa.cmd_close_why(args)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
