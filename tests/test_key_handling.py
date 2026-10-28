"""Key handling: X-Api-Key header by default, ?key= only for header-rejecting routes or once
after a 401, never in the URL of a POST, never follow a 3xx, never leak the key (mocked, no network)."""
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

    def test_assignments_route_uses_query_fallback(self):
        rec = Recorder([b'{"assignments": []}'])
        with mock.patch.object(poa, "http_request", rec):
            poa.api_call("GET", "/api/hosted/assignments", query={"sort": "oldest", "limit": 200})
        c = rec.calls[0]
        self.assertIn("key=", c["url"])
        self.assertIn("sort=oldest", c["url"])
        self.assertNotIn("X-Api-Key", c["headers"])
        self.assertFalse(c["follow_redirects"])

    def test_query_routes_env_override(self):
        os.environ["POA_KEY_QUERY_ROUTES"] = ""
        self.assertEqual(poa.key_mode_for("/api/hosted/assignments"), "header")
        os.environ["POA_KEY_QUERY_ROUTES"] = "/api/hosted/vote"
        self.assertEqual(poa.key_mode_for("/api/hosted/vote"), "query")
        self.assertEqual(poa.key_mode_for("/api/hosted/me"), "header")

    def test_401_falls_back_to_query_once(self):
        rec = Recorder([http_err("u", 401, b'{"error":"missing key"}'), b'{"epoch": 4}'])
        with mock.patch.object(poa, "http_request", rec):
            self.assertEqual(poa.api_call("GET", "/api/hosted/challenge", retries=1)["epoch"], 4)
        self.assertEqual(len(rec.calls), 2)
        self.assertNotIn("key=", rec.calls[0]["url"])
        self.assertIn("key=", rec.calls[1]["url"])
        self.assertFalse(rec.calls[1]["follow_redirects"])

    def test_401_twice_raises_auth_after_one_fallback(self):
        rec = Recorder([http_err("u", 401), http_err("u", 401)])
        with mock.patch.object(poa, "http_request", rec):
            with self.assertRaises(poa.PoaHTTPError) as cm:
                poa.api_call("GET", "/api/hosted/me")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(rec.calls), 2)

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


if __name__ == "__main__":
    unittest.main()
