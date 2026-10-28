"""Key handling: the key goes only in the X-Api-Key header (and a POST's JSON body), never in any URL,
with no ?key= route list and no fallback after a 401 or 5xx; never follow a 3xx; never leak the key
(mocked, no network)."""
import importlib.util, io, os, pathlib, tempfile, unittest, urllib.error, urllib.parse
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ["POA_HOME"] = TMP
os.environ["POA_ADDRESS"] = "poa1testaddressnotreal"
FAKE = "pk_fakeKEY0123456789+/="
os.environ["POA_API_KEY"] = FAKE
os.environ.pop("POA_KEY_QUERY_ROUTES", None)
spec = importlib.util.spec_from_file_location("poa_keyh", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)


def http_err(url, code, body=b"{}", headers=None):
    return urllib.error.HTTPError(url, code, "x", headers or {}, io.BytesIO(body))


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        self.calls.append({"method": method, "url": url, "data": data, "headers": dict(headers or {}),
                           "follow_redirects": follow_redirects})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class KeyHandlingTests(unittest.TestCase):
    def setUp(self):
        os.environ["POA_API_KEY"] = FAKE
        os.environ.pop("POA_KEY_QUERY_ROUTES", None)
        self.sleep = mock.patch.object(poa, "_backoff", lambda a: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def _enc_variants(self):
        return [FAKE, urllib.parse.quote(FAKE, safe=""), urllib.parse.quote_plus(FAKE)]

    def test_get_uses_header_not_url(self):
        rec = Recorder([b'{"epoch": 3}'])
        with mock.patch.object(poa, "http_request", rec):
            self.assertEqual(poa.api_call("GET", "/api/hosted/me")["epoch"], 3)
        c = rec.calls[0]
        self.assertEqual(c["headers"].get("X-Api-Key"), FAKE)
        for v in self._enc_variants():
            self.assertNotIn(v, c["url"])
        self.assertNotIn("key=", c["url"])
        self.assertFalse(c["follow_redirects"])

    def test_post_key_in_header_and_body_never_url(self):
        rec = Recorder([b'{"txid": "ab"}'])
        with mock.patch.object(poa, "http_request", rec):
            poa.api_call("POST", "/api/hosted/answer", body={"answer": "x"})
        c = rec.calls[0]
        self.assertNotIn("key=", c["url"])
        self.assertIn(FAKE, c["data"].decode())
        self.assertEqual(c["headers"].get("X-Api-Key"), FAKE)
        self.assertFalse(c["follow_redirects"])

    def _assert_no_key_in_urls(self, rec):
        for c in rec.calls:
            for v in self._enc_variants():
                self.assertNotIn(v, c["url"])
            self.assertNotIn("key=", c["url"])
            self.assertFalse(c["follow_redirects"])

    def test_assignments_header_only_trailing_slash(self):
        rec = Recorder([b'{"assignments": []}'])
        with mock.patch.object(poa, "http_request", rec):
            poa.hosted_assignments()
        c = rec.calls[0]
        self.assertEqual(c["headers"].get("X-Api-Key"), FAKE)
        self.assertIn("/api/hosted/assignments/?", c["url"])
        self.assertIn("sort=oldest", c["url"])
        self._assert_no_key_in_urls(rec)

    def test_no_query_key_mode_any_route(self):
        os.environ["POA_KEY_QUERY_ROUTES"] = "/api/hosted/assignments,/api/hosted/vote"  # old override is ignored
        for path in ("/api/hosted/assignments", "/api/hosted/assignments/", "/api/hosted/vote", "/api/hosted/me"):
            self.assertEqual(poa.key_mode_for(path), "header")
            rec = Recorder([b"{}"])
            with mock.patch.object(poa, "http_request", rec):
                poa.api_call("GET", path, query={"a": 1})
            self._assert_no_key_in_urls(rec)
            self.assertEqual(rec.calls[0]["headers"].get("X-Api-Key"), FAKE)

    def test_caller_supplied_key_query_is_dropped(self):
        rec = Recorder([b"{}"])
        with mock.patch.object(poa, "http_request", rec):
            poa.api_call("GET", "/api/hosted/me", query={"key": FAKE, "api_key": FAKE, "token": FAKE, "x": 1})
        self._assert_no_key_in_urls(rec)
        self.assertIn("x=1", rec.calls[0]["url"])

    def test_key_in_path_is_refused_before_sending(self):
        rec = Recorder([b"{}"])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(poa.PoaHTTPError):
                poa.api_call("GET", "/api/hosted/x/" + urllib.parse.quote(FAKE, safe=""))
        self.assertEqual(rec.calls, [])

    def test_401_never_falls_back_to_url(self):
        rec = Recorder([http_err("u", 401, b'{"error":"auth required"}'), b'{"epoch": 4}'])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                poa.api_call("GET", "/api/hosted/challenge", retries=3)
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(rec.calls), 1)
        self._assert_no_key_in_urls(rec)

    def test_assignments_502_is_unknown_and_never_url_retried(self):
        errs = [http_err("u", 502, b"cloudflare bad gateway")] * 4 + [TimeoutError("timed out")] * 4
        for exc in (errs[0], errs[4]):
            rec = Recorder([exc] * 4)
            with mock.patch.object(poa, "http_request", rec):
                with self.assertRaises(poa.PoaHTTPError) as cm:
                    poa.hosted_assignments(retries=4)
            self.assertIn("unknown", str(cm.exception))
            self.assertTrue(getattr(cm.exception, "seats_unknown", False))
            self.assertEqual(len(rec.calls), 4)
            self._assert_no_key_in_urls(rec)
            for c in rec.calls:
                self.assertEqual(c["headers"].get("X-Api-Key"), FAKE)

    def test_assignments_401_or_3xx_is_unknown_single_call(self):
        for code in (401, 307):
            rec = Recorder([http_err("u", code, b"{}", {"Location": "https://x/?key=" + FAKE})])
            with mock.patch.object(poa, "http_request", rec):
                with self.assertRaises(poa.PoaHTTPError) as cm:
                    poa.hosted_assignments(retries=4)
            self.assertTrue(cm.exception.seats_unknown)
            self.assertEqual(len(rec.calls), 1)
            self._assert_no_key_in_urls(rec)
            for v in self._enc_variants():
                self.assertNotIn(v, str(cm.exception))

    def test_assignments_warming_waits_then_reads(self):
        warm = http_err("u", 503, b'{"error":"assignments index is building","warming":true,"retry_after_seconds":20}')
        rec = Recorder([warm, b'{"assignments": [{"answer_txid": "ab"}]}'])
        slept = []
        with mock.patch.object(poa, "http_request", rec):
            res = poa.hosted_assignments(retries=1, sleep=slept.append)
        self.assertEqual(res["assignments"][0]["answer_txid"], "ab")
        self.assertEqual(slept, [20])
        self._assert_no_key_in_urls(rec)

    def test_read_open_seats_unknown_not_zero(self):
        rec = Recorder([http_err("u", 502, b"bad gateway")] * 4)
        with mock.patch.object(poa, "http_request", rec), mock.patch.object(poa, "mcp_fallback_enabled", return_value=False):
            r = poa.grade_shown_on_assignments("ab")
        self.assertIsNone(r["confirmed"])
        self._assert_no_key_in_urls(rec)

    def test_source_has_no_query_key_builder(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py").read_text(encoding="utf-8")
        self.assertNotIn('q["key"] = raw_key', src)
        self.assertNotIn("KEY_QUERY_ROUTES", src)
        self.assertNotIn("fell_back", src)

    def test_every_command_path_url_is_keyless(self):
        """Exercise every keyed GET/POST helper with a recorder; no URL may ever carry the key."""
        rec = Recorder([b'{"assignments": [], "next_nonce": 1, "epoch": 1}'] * 40)
        with mock.patch.object(poa, "http_request", rec), mock.patch.object(poa, "mcp_fallback_enabled", return_value=False):
            for path in ("/api/hosted/me", "/api/hosted/challenge", "/api/hosted/mine", "/api/hosted/vote"):
                poa.api_call("GET", path, query={"ballot": "b"})
            poa.api_call("POST", "/api/hosted/answer", body={"answer": "x"})
            poa.hosted_assignments()
            poa.read_open_seats()
        self.assertGreaterEqual(len(rec.calls), 7)
        self._assert_no_key_in_urls(rec)

    def test_post_401_does_not_fall_back_to_url(self):
        rec = Recorder([http_err("u", 401)])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(poa.PoaHTTPError):
                poa.api_call("POST", "/api/hosted/grade", body={"assignment": "t", "score": 2, "reasoning": "r"})
        self.assertEqual(len(rec.calls), 1)
        self.assertNotIn("key=", rec.calls[0]["url"])

    def test_3xx_is_refused_not_followed_and_not_retried(self):
        loc = "https://evil.example/x?key=" + FAKE
        for code in (301, 302, 307, 308):
            rec = Recorder([http_err("u", code, b"moved " + loc.encode(), {"Location": loc})])
            with mock.patch.object(poa, "http_request", rec):
                with self.assertRaises(poa.PoaHTTPError) as cm:
                    poa.api_call("GET", "/api/hosted/me", retries=4)
            self.assertEqual(cm.exception.kind, "redirect-refused")
            self.assertEqual(cm.exception.status, code)
            self.assertEqual(len(rec.calls), 1)
            self.assertNotIn("evil.example", str(cm.exception))
            self.assertNotIn(FAKE, str(cm.exception))

    def test_urllib_transport_does_not_follow_redirect(self):
        """Real http_request(urllib path) with follow_redirects=False raises 3xx instead of fetching Location."""
        import http.server, threading
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path.startswith("/start"):
                    self.send_response(307)
                    self.send_header("Location", "/elsewhere?key=" + urllib.parse.quote(FAKE, safe=""))
                    self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            with mock.patch.dict(os.environ, {"POA_NO_HTTPX": "1"}):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    poa.http_request("GET", f"http://127.0.0.1:{srv.server_port}/start",
                                     headers={"X-Api-Key": FAKE}, timeout=5, follow_redirects=False)
            self.assertEqual(cm.exception.code, 307)
            self.assertEqual(hits, ["/start"])
        finally:
            srv.shutdown()

    def test_redact_covers_raw_encoded_and_query_params(self):
        enc = urllib.parse.quote(FAKE, safe="")
        s = f"GET https://h/x?key={enc}&a=1 raw={FAKE} plus={urllib.parse.quote_plus(FAKE)} guid=?guid=abc123"
        out = poa.redact(s, FAKE)
        for v in self._enc_variants():
            self.assertNotIn(v, out)
        self.assertIn("key=[REDACTED]", out)
        self.assertIn("&a=1", out)
        self.assertNotIn("abc123", out)

    def test_errors_and_exception_chain_never_contain_key(self):
        url_with_key = "https://minepoa.com/api/hosted/assignments?key=" + urllib.parse.quote(FAKE, safe="")
        for exc in (http_err(url_with_key, 500, ("echo " + FAKE).encode()),
                    OSError("ConnectError: failed for " + url_with_key),
                    TimeoutError("timed out " + url_with_key)):
            rec = Recorder([exc])
            with mock.patch.object(poa, "http_request", rec):
                with self.assertRaises(poa.PoaHTTPError) as cm:
                    poa.api_call("GET", "/api/hosted/assignments", retries=1)
            e = cm.exception
            text = str(e) + repr(e.args)
            for v in self._enc_variants():
                self.assertNotIn(v, text)
            self.assertTrue(e.__suppress_context__)
            self.assertIsNone(e.__cause__)

    def test_keyless_calls_have_no_key_anywhere(self):
        rec = Recorder([b"{}"])
        with mock.patch.object(poa, "http_request", rec):
            poa.api_call("GET", "/api/status", need_key=False)
        c = rec.calls[0]
        self.assertNotIn("X-Api-Key", c["headers"])
        self.assertNotIn("key=", c["url"])



class LabLeakRenewalTests(unittest.TestCase):
    """LabLeak token renewal never puts the API key in a URL (or anywhere on the wire)."""

    def setUp(self):
        os.environ["POA_API_KEY"] = FAKE
        os.environ.pop("POA_LABLEAK_TOKEN", None)
        self.tokfile = poa.LABLEAK_TOKEN_FILE
        if self.tokfile.exists():
            self.tokfile.unlink()

    def tearDown(self):
        os.environ.pop("POA_LABLEAK_TOKEN", None)
        if self.tokfile.exists():
            self.tokfile.unlink()

    def _assert_no_key(self, calls):
        enc = [FAKE, urllib.parse.quote(FAKE, safe=""), urllib.parse.quote_plus(FAKE)]
        for c in calls:
            for v in enc:
                self.assertNotIn(v, c["url"])
                self.assertNotIn(v, (c["data"] or b"").decode("utf-8", "replace"))
                self.assertNotIn(v, " ".join(map(str, c["headers"].values())))
            self.assertNotIn("guid=", c["url"])
            self.assertNotIn("agentconnect/join", c["url"])
            self.assertNotIn("key=", c["url"])
            self.assertFalse(c["follow_redirects"])

    def test_401_does_not_rejoin_with_key_in_url(self):
        self.tokfile.write_text("psk_oldtokenvalue1234\n")
        rec = Recorder([http_err("https://minepoa.com/lableak/api/me", 401)])
        with mock.patch.object(poa, "http_request", rec), \
                mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("urlopen used")), \
                mock.patch.object(poa.urllib.request, "build_opener", side_effect=AssertionError("opener used")):
            with self.assertRaises(poa.LabLeakTokenExpired) as cm:
                poa.lableak_call("GET", "/api/me", need_auth=True)
        self.assertEqual(len(rec.calls), 1)
        self._assert_no_key(rec.calls)
        self.assertEqual(rec.calls[0]["headers"].get("Authorization"), "Bearer psk_oldtokenvalue1234")
        msg = str(cm.exception)
        self.assertIn("expired", msg)
        self.assertIn("header", msg)
        self.assertNotIn(FAKE, msg)

    def test_missing_token_makes_no_request(self):
        rec = Recorder([])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(poa.LabLeakTokenExpired):
                poa.lableak_call("POST", "/api/posts", body={"title": "t"}, need_auth=True)
        self.assertEqual(rec.calls, [])

    def test_refresh_never_touches_network(self):
        rec = Recorder([])
        with mock.patch.object(poa, "http_request", rec), \
                mock.patch.object(poa.urllib.request, "urlopen", side_effect=AssertionError("urlopen used")), \
                mock.patch.object(poa.urllib.request, "build_opener", side_effect=AssertionError("opener used")):
            with self.assertRaises(poa.LabLeakTokenExpired):
                poa.load_lableak_token(refresh=True)
        self.assertEqual(rec.calls, [])

    def test_authed_post_key_never_sent_and_redirect_refused(self):
        os.environ["POA_LABLEAK_TOKEN"] = "psk_envtokenvalue5678"
        rec = Recorder([b'{"ok": true}', http_err("https://minepoa.com/lableak/api/posts", 302,
                                                   headers={"Location": "https://evil.example/"})])
        with mock.patch.object(poa, "http_request", rec):
            self.assertTrue(poa.lableak_call("POST", "/api/posts", body={"title": "t"}, need_auth=True)["ok"])
            with self.assertRaises(RuntimeError) as cm:
                poa.lableak_call("GET", "/api/me", need_auth=True)
        self.assertIn("redirect-refused", str(cm.exception))
        self._assert_no_key(rec.calls)

    def test_credential_query_params_refused(self):
        rec = Recorder([])
        with mock.patch.object(poa, "http_request", rec):
            for k in ("guid", "key", "token", "api_key"):
                with self.assertRaises(RuntimeError):
                    poa.lableak_call("GET", "/api/posts", query={k: FAKE})
        self.assertEqual(rec.calls, [])

    def test_errors_redact_key_and_token(self):
        os.environ["POA_LABLEAK_TOKEN"] = "psk_envtokenvalue5678"
        body = ("echo " + FAKE + " psk_envtokenvalue5678").encode()
        rec = Recorder([http_err("https://minepoa.com/lableak/api/posts", 500, body)])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(RuntimeError) as cm:
                poa.lableak_call("GET", "/api/posts", need_auth=True)
        text = str(cm.exception)
        self.assertNotIn(FAKE, text)
        self.assertNotIn("psk_envtokenvalue5678", text)

    def test_source_never_builds_a_guid_join_url(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py").read_text(encoding="utf-8")
        self.assertNotIn('{"guid": raw_key}', src)
        self.assertNotIn("join?{qs}", src)


if __name__ == "__main__":
    unittest.main()
