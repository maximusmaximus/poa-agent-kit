"""Mocked tests for `poa.py att-reconcile` (SUGGESTIONS.md #187). No network, no real key:
`http_request` (the transport under api_call and fetch_epoch_body) is patched in every command and
mine test, and the fake refuses any URL it doesn't know. POA_HOME is a temp dir.
Run: python3 -m unittest tests/test_att_reconcile.py"""
import argparse, contextlib, hashlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_ar", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "poa1OTHERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_795_000_000
H = 21600


def ts(n):
    return T0 + n * H


def body(n, attest, rewards=None):
    return {"epoch": n, "ts": ts(n), "rewards": rewards if rewards is not None else {ME: "10.0", OTHER: "5.0"},
            "basis": {"attestations": attest, "challenge_eligible": [], "settled": []}}


def closes():
    # E115 anchor only; E116 match 28=28; E117 absent; E118 undercount (body 30 < ours 40)
    return [body(115, {ME: 5}), body(116, {ME: 28, OTHER: 9}), body(117, {OTHER: 4}),
            body(118, {ME: 30, OTHER: 2})]


def recs():
    return [{"ts": ts(115) + 60, "attested": 20, "node": "a"}, {"ts": ts(115) + 3600, "attested": 8, "node": "b"},
            {"ts": ts(116) + 10, "attested": 48, "node": "a"},
            {"ts": ts(117) + 5, "attested": 25, "node": "a"}, {"ts": ts(118), "attested": 15, "node": "a"},
            {"ts": ts(118) + 30, "attested": 7, "node": "a"}]


class TallyTests(unittest.TestCase):
    def test_value_extraction(self):
        self.assertEqual(poa._mine_attested_value({"attested": 13}), 13)
        self.assertEqual(poa._mine_attested_value({"result": {"attested": "4"}}), 4)
        self.assertIsNone(poa._mine_attested_value({"kind": "already-mined-this-hour", "skipped": True, "attested": 3}))
        self.assertIsNone(poa._mine_attested_value({"kind": "queued"}))
        self.assertIsNone(poa._mine_attested_value({"attested": True}))
        self.assertIsNone(poa._mine_attested_value({"attested": -1}))
        self.assertIsNone(poa._mine_attested_value(None))

    def test_record_and_load_merge_dedup(self):
        d = pathlib.Path(tempfile.mkdtemp())
        a, b = d / "a.jsonl", d / "b.jsonl"
        poa.record_mine_attested({"attested": 13}, path=a, now=100, node="box")
        poa.record_mine_attested({"attested": 0}, path=a, now=200, node="box")
        poa.record_mine_attested({"skipped": True}, path=a, now=300)
        poa.record_mine_attested({"attested": 13}, path=b, now=100, node="box")   # same record copied
        poa.record_mine_attested({"attested": 5}, path=b, now=150, node="pc")
        b.write_text(b.read_text() + "not json\n{\"ts\": 1}\n")
        txt = a.read_text()
        self.assertEqual(len(txt.splitlines()), 2)
        self.assertNotIn("poa1", txt)
        if os.name != "nt":
            self.assertEqual(os.stat(a).st_mode & 0o777, 0o600)
        got = poa.load_att_tally([a, b, d / "missing.jsonl"])
        self.assertEqual([(r["ts"], r["attested"], r["node"]) for r in got], [(100, 13, "box"), (150, 5, "pc"), (200, 0, "box")])

    def test_record_off_switch_and_never_raises(self):
        d = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"POA_ATT_TALLY": "0"}):
            self.assertIsNone(poa.record_mine_attested({"attested": 3}, path=d / "x.jsonl", now=1))
        self.assertFalse((d / "x.jsonl").exists())
        blocker = d / "file"
        blocker.write_text("x")
        self.assertIsNone(poa.record_mine_attested({"attested": 3}, path=blocker / "sub" / "t.jsonl", now=1))


class ReportTests(unittest.TestCase):
    def test_statuses_per_close(self):
        r = poa.att_reconcile_report(closes(), ME, recs(), last=3)
        self.assertTrue(r["ok"])
        rows = {x["close"]: x for x in r["rows"]}
        self.assertEqual(sorted(rows), [116, 117, 118])
        self.assertEqual((rows[116]["ours"], rows[116]["body"], rows[116]["diff"], rows[116]["status"]), (28, 28, 0, "match"))
        self.assertEqual(rows[116]["calls"], 2)
        self.assertEqual(rows[116]["nodes"], ["a", "b"])
        self.assertEqual((rows[117]["ours"], rows[117]["body"], rows[117]["status"]), (48, None, "absent"))
        self.assertEqual(rows[117]["diff"], -48)
        # a call exactly at the close ts belongs to that close
        self.assertEqual((rows[118]["ours"], rows[118]["body"], rows[118]["diff"], rows[118]["status"]), (40, 30, -10, "undercount"))
        self.assertEqual(r["newest_status"], "undercount")
        self.assertEqual(r["open_epoch"], {"epoch": 119, "calls": 1, "ours": 7})
        self.assertEqual(rows[117]["network"], {"map_keys": 1, "paid_accounts": 2, "paid_without_entry": 1})

    def test_over_no_tally_no_map_and_partial(self):
        eps = closes()
        eps[3]["basis"]["attestations"][ME] = 99
        eps[2]["basis"].pop("attestations")
        rr = [x for x in recs() if x["ts"] > ts(117)]   # tally starts inside E118's window
        rows = {x["close"]: x for x in poa.att_reconcile_report(eps, ME, rr, last=3)["rows"]}
        self.assertEqual(rows[116]["status"], "no-local-tally")
        self.assertIsNone(rows[116]["diff"])
        self.assertEqual(rows[117]["status"], "no-map")
        self.assertEqual(rows[118]["status"], "over")
        self.assertTrue(rows[118]["partial_window"])
        self.assertFalse(rows[116]["partial_window"])

    def test_first_close_without_anchor_is_partial(self):
        rows = poa.att_reconcile_report(closes()[:1], ME, [{"ts": ts(115) - 5, "attested": 5, "node": ""}], last=4)["rows"]
        self.assertEqual(rows[0]["status"], "match")
        self.assertTrue(rows[0]["partial_window"])
        self.assertIsNone(rows[0]["window_start"])

    def test_att_log_flag_and_line(self):
        eps = closes()
        eps[3]["basis"]["att_log"] = []
        r = poa.att_reconcile_report(eps, ME, recs(), last=1)
        self.assertTrue(r["rows"][0]["att_log_in_body"])
        self.assertEqual(poa.att_reconcile_line(r["rows"][0]), "E118 att ours=40 (2 calls) body=30 diff=-10 status=undercount")

    def test_errors_and_no_address_in_output(self):
        self.assertFalse(poa.att_reconcile_report(closes(), "", recs())["ok"])
        self.assertFalse(poa.att_reconcile_report([], ME, recs())["ok"])
        txt = json.dumps(poa.att_reconcile_report(closes(), ME, recs(), last=3))
        self.assertNotIn(ME, txt)
        self.assertNotIn(OTHER, txt)


def fake_transport(eps, calls):
    raws = {e["epoch"]: json.dumps(e).encode() for e in eps}
    index = {"v": 1, "closed_through": max(raws), "count": len(raws),
             "epochs": [{"n": n, "sha256": hashlib.sha256(r).hexdigest(), "bytes": len(r), "close_ts": ts(n)}
                        for n, r in raws.items()]}

    def _fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        calls.append((method, url, dict(headers or {})))
        if url == f"{poa.BASE_URL}/api/epochs":
            return json.dumps(index).encode()
        for n, r in raws.items():
            if url == f"{poa.BASE_URL}/api/epochs/{n}":
                return r
        raise AssertionError(f"unexpected request {url}")
    return _fake


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.tally = self.home / "att-tally.jsonl"
        self.tally.write_text("".join(json.dumps(r) + "\n" for r in recs()))
        self.calls = []

    def run_cmd(self, eps, fn=None, **kw):
        fn = fn or poa.cmd_att_reconcile
        args = argparse.Namespace(last=kw.get("last", 3), address=ME, line=kw.get("line", False), tally=kw.get("tally", []),
                                  json=kw.get("json", False), timeout=5)
        out = io.StringIO()
        code = 0
        with mock.patch.object(poa, "http_request", fake_transport(eps, self.calls)), \
                mock.patch.object(poa, "ATT_TALLY_LOG", self.tally), contextlib.redirect_stdout(out):
            try:
                fn(args)
            except SystemExit as ex:
                code = ex.code
        return out.getvalue(), code

    def test_keyless_address_free_and_exit_3_on_undercount(self):
        out, code = self.run_cmd(closes(), json=True)
        self.assertEqual(code, 3)
        r = json.loads(out)
        self.assertEqual([x["status"] for x in r["rows"]], ["match", "absent", "undercount"])
        self.assertNotIn(ME, out)
        self.assertTrue(self.calls)
        for method, url, headers in self.calls:
            self.assertEqual(method, "GET")
            self.assertNotIn("X-Api-Key", headers)
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("pk_test_fake", url)
            self.assertNotIn(ME, url)

    def test_line_exit_zero_on_match(self):
        out, code = self.run_cmd(closes()[:2], line=True, last=1)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "E116 att ours=28 (2 calls) body=28 diff=+0 status=match partial")  # tally starts in this window

    def test_merge_extra_tally(self):
        extra = self.home / "pc.jsonl"
        extra.write_text(json.dumps({"ts": ts(117) + 9, "attested": -5}) + "\n" + json.dumps({"ts": ts(118) - 9, "attested": 3, "node": "pc"}) + "\n")
        eps = closes()
        eps[3]["basis"]["attestations"][ME] = 43
        out, code = self.run_cmd(eps, json=True, tally=[str(extra)], last=1)
        self.assertEqual(code, 0)
        row = json.loads(out)["rows"][0]
        self.assertEqual((row["ours"], row["status"], row["nodes"]), (43, "match", ["a", "pc"]))

    def test_hash_mismatch_fails_closed(self):
        eps = closes()
        calls = []
        good = fake_transport(eps, calls)

        def tampered(method, url, **kw):
            raw = good(method, url, **kw)
            return raw.replace(b'"5.0"', b'"6.0"') if url.endswith("/api/epochs/117") else raw
        args = argparse.Namespace(last=3, address=ME, line=False, tally=[], json=True, timeout=5)
        out = io.StringIO()
        with mock.patch.object(poa, "http_request", tampered), mock.patch.object(poa, "ATT_TALLY_LOG", self.tally), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                poa.cmd_att_reconcile(args)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_close_why_shows_reconcile_when_tally_exists(self):
        out, code = self.run_cmd(closes(), fn=poa.cmd_close_why, line=True, last=3)
        self.assertIn("att=undercount (ours 40, body 30, diff -10)", out)
        self.assertNotIn(ME, out)
        out, _ = self.run_cmd(closes(), fn=poa.cmd_close_why, json=True, last=3)
        rows = json.loads(out)["rows"]
        self.assertEqual([x["att_reconcile"]["status"] for x in rows], ["match", "absent", "undercount"])

    def test_close_why_unchanged_without_tally(self):
        self.tally.unlink()
        out, _ = self.run_cmd(closes(), fn=poa.cmd_close_why, line=True, last=3)
        self.assertNotIn("att=", out)


class MineRecordsTallyTests(unittest.TestCase):
    """safe_mine appends to the tally on a real (mocked) mine and not on a lock skip; the hourly lock stays."""

    def test_mine_then_lock_skip(self):
        home = pathlib.Path(tempfile.mkdtemp())
        tally = home / "att-tally.jsonl"
        seen = []

        def _fake(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            seen.append(url)
            if url.endswith("/api/hosted/mine"):
                return json.dumps({"ok": True, "attested": 17}).encode()
            raise AssertionError(f"unexpected request {url}")
        with mock.patch.object(poa, "POA_DIR", home), mock.patch.object(poa, "ATT_TALLY_LOG", tally), \
                mock.patch.object(poa, "http_request", _fake), \
                mock.patch.object(poa, "hosted_write", lambda send, action=None, **kw: send()), \
                mock.patch.dict(os.environ, {"POA_MINE_HOURLY_LOCK": "1", "POA_ATT_TALLY": "1"}):
            r1 = poa.safe_mine()
            r2 = poa.safe_mine()
        self.assertEqual(r1.get("attested"), 17)
        self.assertEqual(r2.get("kind"), "already-mined-this-hour")
        self.assertEqual(sum(1 for u in seen if u.endswith("/api/hosted/mine")), 1)
        lines = [json.loads(x) for x in tally.read_text().splitlines()]
        self.assertEqual([x["attested"] for x in lines], [17])
        self.assertNotIn("poa1", tally.read_text())


class VersionTests(unittest.TestCase):
    def test_kit_version(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.26")


if __name__ == "__main__":
    unittest.main()
