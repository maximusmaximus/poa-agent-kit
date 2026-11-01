"""Mocked tests for SUGGESTIONS.md #92: `disposal` projects the close where each pending row
leaves pending under the settle cap, and shows seal-based jury odds (no network, no real key).
Run: python3 -m unittest tests/test_disposal.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_dp", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


def tx(i):
    return f"{i:064x}"


def pend(since, seals=0):
    return {"kind": "short", "panel": [], "since": since, "grades": [{"grader": "g", "score": 2}] * seals}


def build():
    # Snapshot close E5, ttl 2, cap 3. Rows since 2 (due E4, carried) and since 3 (due E5) and 4 (due E6).
    p4 = {tx(1): pend(2), tx(2): pend(2, 1), tx(3): pend(3), tx(4): pend(3)}
    p5 = {tx(2): pend(2, 1), tx(3): pend(3), tx(4): pend(3), tx(5): pend(3), tx(6): pend(4, 1), tx(7): pend(4)}
    settled5 = [{"answer": tx(1), "from": "poa1a", "passed": False, "expired": True, "jury": [], "winners": [], "losers": []},
                {"answer": tx(9), "from": "poa1b", "passed": True, "jury": [{"grader": "g", "aligned": True}], "winners": [], "losers": []}]
    p4[tx(9)] = pend(2, 2)
    eps = [{"epoch": n, "basis": {"pending": {}, "settled": []}} for n in range(4)]
    eps.append({"epoch": 4, "basis": {"pending": p4, "settled": []}})
    eps.append({"epoch": 5, "basis": {"pending": p5, "settled": settled5}})
    return eps


class DisposalTests(unittest.TestCase):
    def test_schedule_cap_and_order(self):
        p = build()[-1]["basis"]["pending"]
        s = poa.disposal_schedule(p, 5, ttl=2, cap=3)
        # E6: due rows are since<=4 (all 6); cap 3 takes (2,tx2),(3,tx3),(3,tx4); E7 takes the rest.
        self.assertEqual([s[tx(i)] for i in (2, 3, 4, 5, 6, 7)], [6, 6, 6, 7, 7, 7])

    def test_schedule_waits_for_due(self):
        s = poa.disposal_schedule({tx(1): pend(10)}, 5, ttl=2, cap=3)
        self.assertEqual(s[tx(1)], 12)

    def test_jury_rates_by_seal_bucket(self):
        eps = build()
        r = poa.jury_rates(eps[4]["basis"]["pending"], eps[5]["basis"]["settled"])
        self.assertEqual((r["0"]["rows"], r["0"]["juried"]), (1, 0))
        self.assertEqual((r["1+"]["rows"], r["1+"]["juried"], r["1+"]["rate"]), (1, 1, 1.0))

    def test_report_states(self):
        r = poa.disposal_report(build(), ["0x" + tx(5).upper(), tx(2)[:12], tx(1), tx(9), "abcd\u2026ef", tx(77)], ttl=2, cap=3)
        self.assertTrue(r["ok"]); self.assertTrue(r["history_complete"]); self.assertEqual(r["snapshot_close"], 5)
        rows = r["rows"]
        self.assertEqual(rows[0]["state"], "pending")
        self.assertEqual((rows[0]["nominal_due_close"], rows[0]["projected_disposal_close"], rows[0]["carried_closes"]), (5, 7, 2))
        self.assertEqual(rows[0]["likely_outcome"], "expiry likely (no jury)")
        self.assertEqual((rows[1]["seals"], rows[1]["projected_disposal_close"], rows[1]["likely_outcome"]), (1, 6, "jury likely"))
        self.assertEqual((rows[2]["state"], rows[2]["settled_close"]), ("expired", 5))
        self.assertEqual(rows[3]["state"], "passed")
        self.assertEqual(rows[4]["state"], "bad-key")
        self.assertEqual(rows[5]["state"], "unresolved"); self.assertTrue(rows[5]["history_complete"])

    def test_missing_close_is_incomplete_not_expired(self):
        eps = [e for e in build() if e["epoch"] != 2]
        r = poa.disposal_report(eps, [tx(77)], ttl=2, cap=3)
        self.assertFalse(r["history_complete"]); self.assertEqual(r["missing_closes"], [2])
        self.assertEqual(r["rows"][0]["state"], "unresolved")
        self.assertIn("incomplete", r["rows"][0]["reason"])

    def test_cmd_uses_mocked_reads_only(self):
        eps = build()
        calls = []

        def fake_api(method, path, **k):
            calls.append(path)
            return eps if path == "/api/epochs" else {"pending_ttl_epochs": 2, "settle_cap_per_close": 3}
        args = argparse.Namespace(txid=[tx(5)], mine=False, address="", log="", all=False, json=True, timeout=5)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api), \
             mock.patch.object(poa, "explorer_get", side_effect=AssertionError("no explorer read")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_disposal(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["rows"][0]["projected_disposal_close"], 7)
        self.assertEqual(sorted(calls), ["/api/epochs", "/api/params"])

    def test_cmd_table_mine(self):
        eps = build()
        args = argparse.Namespace(txid=[], mine=True, address="poa1TEST", log="", all=False, json=False, timeout=5)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=lambda m, path, **k: eps if path == "/api/epochs" else {"pending_ttl_epochs": 2, "settle_cap_per_close": 3}), \
             mock.patch.object(poa, "own_answer_candidates", return_value={tx(6): {"epoch": 4, "source": "explorer"}}), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(buf):
            poa.cmd_disposal(args)
        text = buf.getvalue()
        self.assertIn("snapshot close E5", text)
        self.assertIn("jury likely", text)
        self.assertNotIn("poa1TEST", text)


if __name__ == "__main__":
    unittest.main()
