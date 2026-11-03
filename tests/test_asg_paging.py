"""Release v0.81-asgcap: assignment listings are paged (limit max 100, default 50; follow offset while
has_more). Mocked, no network, no real key. Run: python3 -m unittest tests/test_asg_paging.py"""
import importlib.util, json, os, pathlib, tempfile, unittest, urllib.parse
from unittest import mock

os.environ["POA_HOME"] = tempfile.mkdtemp()
FAKE = "pk_fakePAGING0123456789"
os.environ["POA_API_KEY"] = FAKE
spec = importlib.util.spec_from_file_location("poa_pg", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


def page(rows, offset, total, limit=100):
    return json.dumps({"epoch": 37, "has_seat": bool(total), "assignments": rows, "total": total,
                       "offset": offset, "limit": limit, "has_more": offset + len(rows) < total}).encode()


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=30, follow_redirects=True):
        self.calls.append({"url": url, "headers": dict(headers or {}), "follow_redirects": follow_redirects})
        return self.responses.pop(0)


def rows(a, b):
    return [{"answer_txid": f"{i:04x}", "epoch": 36} for i in range(a, b)]


def qs(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


class PagingTests(unittest.TestCase):
    def setUp(self):
        os.environ["POA_API_KEY"] = FAKE

    def test_hosted_follows_has_more(self):
        rec = Recorder([page(rows(0, 100), 0, 230), page(rows(100, 200), 100, 230), page(rows(200, 230), 200, 230)])
        with mock.patch.object(poa, "http_request", rec):
            res = poa.hosted_assignments()
        self.assertEqual(len(res["assignments"]), 230)
        self.assertEqual(res["pages"], 3)
        self.assertFalse(res["has_more"]); self.assertFalse(res["truncated"])
        self.assertEqual([qs(c["url"])["offset"] for c in rec.calls], ["0", "100", "200"])
        for c in rec.calls:
            self.assertEqual(qs(c["url"])["limit"], "100")
            self.assertEqual(qs(c["url"])["sort"], "oldest")
            self.assertNotIn(FAKE, c["url"]); self.assertNotIn("key=", c["url"])
            self.assertEqual(c["headers"].get("X-Api-Key"), FAKE)
            self.assertFalse(c["follow_redirects"])

    def test_single_page_and_old_server_without_has_more(self):
        rec = Recorder([page([], 0, 0)])
        with mock.patch.object(poa, "http_request", rec):
            self.assertEqual(poa.hosted_assignments()["assignments"], [])
        self.assertEqual(len(rec.calls), 1)
        rec = Recorder([b'{"assignments": [{"answer_txid": "ab"}]}'])
        with mock.patch.object(poa, "http_request", rec):
            self.assertEqual(len(poa.hosted_assignments()["assignments"]), 1)
        self.assertEqual(len(rec.calls), 1)

    def test_duplicate_rows_across_pages_dropped(self):
        rec = Recorder([page(rows(0, 100), 0, 150), page(rows(90, 150), 100, 150)])
        with mock.patch.object(poa, "http_request", rec):
            res = poa.hosted_assignments()
        self.assertEqual(len(res["assignments"]), 150)

    def test_max_pages_marks_truncated(self):
        pages = [page(rows(i * 100, i * 100 + 100), i * 100, 10_000) for i in range(3)]
        calls = iter(pages)
        res = poa._paged_assignments(lambda off: json.loads(next(calls)), max_pages=3)
        self.assertTrue(res["truncated"]); self.assertTrue(res["has_more"])
        self.assertEqual(len(res["assignments"]), 300)

    def test_keyless_route_pages_and_sends_no_key(self):
        rec = Recorder([page(rows(0, 100), 0, 120), page(rows(100, 120), 100, 120)])
        with mock.patch.object(poa, "http_request", rec):
            code, res = poa._keyless_assignments("poa1testaddressnotreal")
        self.assertEqual(code, 200)
        self.assertEqual(len(res["assignments"]), 120)
        for c in rec.calls:
            self.assertNotIn("X-Api-Key", c["headers"])
            self.assertIn("/api/assignments/poa1testaddressnotreal/?", c["url"])


if __name__ == "__main__":
    unittest.main()
