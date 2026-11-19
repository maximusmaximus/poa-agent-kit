"""standing --checkpoint (SUGGESTIONS #37). Mocked responses only: http_request, the transport under
api_call, is patched in every command test, so nothing reaches the live server; POA_HOME is a temp dir
and the key is a dummy set in the environment."""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest, urllib.error
from unittest import mock

spec = importlib.util.spec_from_file_location("poa_ec", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

ADDR = "poa1" + "T" * 32
KEY = "pk_dummyTESTkey0000000000"
NOW = 1_800_000_000


def st(h, **kw):
    d = {"checkpoint_height": h, "as_of": "2027-01-15T08:00:00Z", "epochs_closed": 99}
    d.update(kw)
    return d


class ReportTests(unittest.TestCase):
    def test_bracketed_current(self):
        r = poa.established_checkpoint_report(st(500), {"established": True}, st(500), now=NOW)
        self.assertEqual(r["verdict"], "current")
        self.assertEqual(r["checkpoint_height"], 500)
        self.assertIn("bracketed", r["checkpoint_source"])
        self.assertIn("me_names_no_checkpoint", r["flags"])
        self.assertEqual(r["checkpoint_unmoved_min"], 0)

    def test_me_checkpoint_wins(self):
        r = poa.established_checkpoint_report(st(500), {"established": False, "checkpoint_height": 498}, st(501), now=NOW)
        self.assertEqual(r["checkpoint_height"], 498)
        self.assertEqual(r["checkpoint_source"], "/api/hosted/me")
        self.assertNotIn("me_names_no_checkpoint", r["flags"])
        r = poa.established_checkpoint_report(None, {"established": True, "checkpoint": {"height": 7}}, None, now=NOW)
        self.assertEqual(r["checkpoint_height"], 7)

    def test_moved_during_read_is_possibly_stale(self):
        r = poa.established_checkpoint_report(st(500), {"established": True}, st(501), now=NOW)
        self.assertEqual(r["checkpoint_height"], 501)
        self.assertIn("checkpoint_moved_during_read", r["flags"])
        self.assertEqual(r["verdict"], "possibly-stale")

    def test_frozen_from_samples(self):
        samples = [{"ts": NOW - 7200, "checkpoint_height": 499, "me_established": False},
                   {"ts": NOW - 3600, "checkpoint_height": 500, "me_established": False},
                   {"ts": NOW - 600, "checkpoint_height": 500, "me_established": False}]
        r = poa.established_checkpoint_report(st(500), {"established": False}, st(500), samples=samples, now=NOW)
        self.assertEqual(r["checkpoint_unmoved_min"], 60)
        self.assertIn("checkpoint_frozen", r["flags"])
        self.assertEqual(r["verdict"], "possibly-stale")
        self.assertFalse(r["changed_since_last_sample"])
        r = poa.established_checkpoint_report(st(500), {"established": False}, st(500), samples=samples, now=NOW, frozen_min=90)
        self.assertEqual(r["verdict"], "current")

    def test_changed_without_checkpoint_move(self):
        samples = [{"ts": NOW - 300, "checkpoint_height": 500, "me_established": True}]
        r = poa.established_checkpoint_report(st(500), {"established": False}, st(500), samples=samples, now=NOW)
        self.assertTrue(r["changed_since_last_sample"])
        self.assertIn("established_changed_without_checkpoint_move", r["flags"])
        samples = [{"ts": NOW - 300, "checkpoint_height": 499, "me_established": True}]
        r = poa.established_checkpoint_report(st(500), {"established": False}, st(500), samples=samples, now=NOW)
        self.assertNotIn("established_changed_without_checkpoint_move", r["flags"])

    def test_stale_status_and_close_view_disagreement(self):
        r = poa.established_checkpoint_report(st(500, stale=True, stale_age_s=900), {"established": True}, None,
                                              close_res={"established": False, "epoch": 98, "since_epoch": None},
                                              now=NOW)
        self.assertIn("status_stale", r["flags"])
        self.assertIn("me_disagrees_with_close_view", r["flags"])
        self.assertEqual(r["verdict"], "possibly-stale")
        self.assertEqual(r["close_view"]["epoch"], 98)

    def test_unknown_without_me(self):
        r = poa.established_checkpoint_report(st(500), None, st(500), now=NOW)
        self.assertEqual(r["verdict"], "unknown")
        self.assertTrue(r["ok"])
        r = poa.established_checkpoint_report(None, None, None, now=NOW)
        self.assertFalse(r["ok"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(poa, "POA_DIR", pathlib.Path(self.tmp.name)),
                  mock.patch.object(poa, "_backoff", lambda a: None),
                  mock.patch.dict(os.environ, {"POA_API_KEY": KEY, "POA_ADDRESS": ADDR, "POA_NO_HTTPX": "1"})):
            p.start(); self.addCleanup(p.stop)
        self.calls = []

    def fake(self, me_body=None, heights=(500, 500)):
        hs = list(heights)

        def http(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            self.calls.append((method, url, dict(headers or {}), follow_redirects))
            if url.endswith("/api/status"):
                return json.dumps(st(hs.pop(0))).encode()
            if url.endswith("/api/hosted/me"):
                return json.dumps(me_body or {"established": True}).encode()
            if "/api/established/" in url:
                return json.dumps({"established": True, "epoch": 99, "since_epoch": 90, "expires_after_epoch": 110}).encode()
            raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b"{}"))
        return http

    def run_cmd(self, *argv):
        p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="command")
        a = sub.add_parser("standing")
        a.add_argument("--address", default=""); a.add_argument("--no-me", action="store_true")
        a.add_argument("--checkpoint", action="store_true"); a.add_argument("--json", action="store_true")
        a.add_argument("--frozen-min", type=int, default=45); a.add_argument("--no-record", action="store_true")
        args = p.parse_args(["standing", "--checkpoint", "--json", *argv])
        out = io.StringIO(); code = 0
        with contextlib.redirect_stdout(out):
            try:
                poa.cmd_standing(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_command_reads_and_records(self):
        with mock.patch.object(poa, "http_request", self.fake()):
            code, out = self.run_cmd()
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["verdict"], "current")
        self.assertEqual(r["close_view"]["since_epoch"], 90)
        self.assertNotIn(KEY, out)
        self.assertNotIn(ADDR, out)
        # No /api/epochs read; key only in the header of the /me request, never in a URL; keyed read refuses redirects.
        urls = [c[1] for c in self.calls]
        self.assertFalse(any("/api/epochs" in u for u in urls))
        for method, url, headers, follow in self.calls:
            self.assertNotIn(KEY, url)
            if url.endswith("/api/hosted/me"):
                self.assertEqual(headers.get("X-Api-Key"), KEY)
                self.assertFalse(follow)
            else:
                self.assertNotIn("X-Api-Key", headers)
        saved = (pathlib.Path(self.tmp.name) / "established-checkpoints.jsonl").read_text()
        self.assertNotIn(ADDR, saved); self.assertNotIn(KEY, saved)
        self.assertEqual(json.loads(saved.splitlines()[0])["checkpoint_height"], 500)
        if os.name != "nt":
            self.assertEqual(oct((pathlib.Path(self.tmp.name) / "established-checkpoints.jsonl").stat().st_mode & 0o777), "0o600")

    def test_second_read_same_bit_flip_flags(self):
        with mock.patch.object(poa, "http_request", self.fake({"established": True})):
            self.run_cmd()
        with mock.patch.object(poa, "http_request", self.fake({"established": False})):
            code, out = self.run_cmd()
        r = json.loads(out)
        self.assertIn("established_changed_without_checkpoint_move", r["flags"])
        self.assertEqual(r["samples_read"], 1)

    def test_no_me_is_keyless_and_unknown(self):
        with mock.patch.object(poa, "http_request", self.fake()):
            code, out = self.run_cmd("--no-me", "--no-record")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["verdict"], "unknown")
        self.assertFalse(any(c[1].endswith("/api/hosted/me") for c in self.calls))
        self.assertFalse((pathlib.Path(self.tmp.name) / "established-checkpoints.jsonl").exists())

    def test_redirect_on_me_is_refused_not_followed(self):
        def http(method, url, data=None, headers=None, timeout=30, follow_redirects=True):
            self.calls.append((method, url, dict(headers or {}), follow_redirects))
            if url.endswith("/api/hosted/me"):
                raise urllib.error.HTTPError(url, 302, "Found", {"Location": "https://evil.example/"}, io.BytesIO(b""))
            if url.endswith("/api/status"):
                return json.dumps(st(500)).encode()
            return json.dumps({}).encode()
        with mock.patch.object(poa, "http_request", http):
            code, out = self.run_cmd()
        self.assertEqual(json.loads(out)["established_per_me"], None)
        self.assertFalse(any("evil.example" in c[1] for c in self.calls))


if __name__ == "__main__":
    unittest.main()
