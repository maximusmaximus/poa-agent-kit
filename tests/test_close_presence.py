"""Mocked tests for `poa.py close-presence` (SUGGESTIONS.md #122).
No network, no real key. Run: python3 -m unittest tests/test_close_presence.py"""
import argparse, contextlib, importlib.util, io, json, os, pathlib, tempfile, unittest
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location(
    "poa_cp", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py"
)
poa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poa)

ME = "poa1TESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def cr(n, *, seats=None, seatless_count=None, seatless=None, aws=0, drawn=100,
       panel_data="in-memory asg-index panels for the closed epoch",
       pool_size=100, admitted=10, extra_top=None):
    """Build a close-report shaped like the live E48/E50/E52/E54 family."""
    out = {
        "close_epoch": n,
        "close_ts": 1_794_000_000 + n,
        "grade_quorum": 4,
        "panel_data": panel_data,
        "panel_pool": {"source": "established", "size": pool_size},
        "panels": {
            "drawn": drawn,
            "fill_hist": {},
            "grades_cast": 1000,
            "rows": 5000,
            "rows_reaching_quorum": 200,
            "carried_newly_dealt": 50,
        },
        "seats": seats,
        "seatless": seatless,
        "seatless_count": seatless_count,
        "establishment": {
            "established_before": pool_size,
            "eligible": pool_size - 5,
            "admitted": admitted,
            "waitlisted": 0,
            "admitted_with_seats": aws,
            "removed_ttl": 0,
            "removed_slashed": 0,
        },
        "settled_split": {
            "passed": 100, "failed": 10,
            "expired_empty_jury": 50, "expired_with_seals": 0,
        },
        "settled_count": 160,
        "concentration": {"distinct_graders": 9},
        "appeals": {},
        "seed": "abc",
    }
    if extra_top:
        out.update(extra_top)
    return out


def e48_like(n=48):
    return cr(n, seats={"dealt": 342306, "sealed": 13208, "expired_unsealed": 8190},
              seatless_count=1188, seatless=["x"] * 3, aws=0, drawn=38034, pool_size=855)


def e50_null(n=50):
    return cr(n, seats=None, seatless_count=None, seatless=None, aws=None, drawn=None,
              panel_data="unavailable: dealt panels not captured for this close (node restarted mid-close window)",
              pool_size=1337, admitted=418)


def e52_restored(n=52):
    return cr(n, seats={"dealt": 332217, "sealed": 20700, "expired_unsealed": 8271},
              seatless_count=824, seatless=["y"] * 2, aws=0, drawn=36913, pool_size=2090,
              admitted=523)


class WalkPresenceTests(unittest.TestCase):
    def test_null_parent_makes_nested_absent(self):
        state, kind = poa._walk_presence(e50_null(), "seats.dealt")
        self.assertEqual(state, "absent")
        self.assertIsNone(kind)

    def test_null_leaf(self):
        state, kind = poa._walk_presence(e50_null(), "seatless_count")
        self.assertEqual(state, "null")
        self.assertEqual(kind, "null")

    def test_present_int(self):
        state, kind = poa._walk_presence(e48_like(), "seats.dealt")
        self.assertEqual(state, "present")
        self.assertEqual(kind, "int")

    def test_panel_data_unavailable_kind(self):
        state, kind = poa._walk_presence(e50_null(), "panel_data")
        self.assertEqual(state, "unavailable")
        self.assertEqual(kind, "unavailable")

    def test_absent_key(self):
        state, kind = poa._walk_presence({"close_epoch": 1}, "seatless_count")
        self.assertEqual(state, "absent")


class PresenceReportTests(unittest.TestCase):
    def history(self):
        # E48 integers → E50/E51 null → E52 integers restored (matches live series)
        return {
            48: e48_like(48),
            50: e50_null(50),
            51: e50_null(51),
            52: e52_restored(52),
        }

    def test_detects_occupancy_null_to_int_flips(self):
        r = poa.presence_report(self.history(), last=8)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "close-presence")
        self.assertEqual(r["newest_close"], 52)
        self.assertEqual(r["verdict"], "flips")  # restored, not currently null
        self.assertEqual(r["occupancy_bad_newest"], [])
        # Critical flips include seats null→object and panel_data unavailable→str
        paths = {f["path"] for f in r["flips"] if f["critical"]}
        self.assertIn("seats", paths)
        self.assertIn("seatless_count", paths)
        self.assertIn("establishment.admitted_with_seats", paths)
        self.assertIn("panel_data", paths)
        # First flip of seats should be E50 (int/object → null)
        seats_flip = next(f for f in r["flips"] if f["path"] == "seats")
        self.assertEqual(seats_flip["close"], 50)
        self.assertEqual(seats_flip["from_kind"], "object")
        self.assertEqual(seats_flip["to_state"], "null")
        restore = [f for f in r["flips"] if f["path"] == "seats" and f["close"] == 52][0]
        self.assertEqual(restore["from_state"], "null")
        self.assertEqual(restore["to_kind"], "object")

    def test_occupancy_null_verdict_on_newest(self):
        r = poa.presence_report({50: e50_null(50), 51: e50_null(51)}, last=2)
        self.assertEqual(r["verdict"], "occupancy-null")
        self.assertIn("seats", r["occupancy_bad_newest"])
        self.assertIn("panel_data", r["occupancy_bad_newest"])

    def test_stable_when_no_flips(self):
        r = poa.presence_report({52: e52_restored(52), 53: e52_restored(53), 54: e52_restored(54)}, last=3)
        self.assertEqual(r["verdict"], "stable")
        self.assertEqual(r["flip_count"], 0)

    def test_distinguishes_absent_null_and_zero(self):
        # admitted_with_seats: absent vs null vs 0
        a = e52_restored(52)
        del a["establishment"]["admitted_with_seats"]
        b = e50_null(53)  # aws None
        c = e52_restored(54)  # aws 0
        r = poa.presence_report({52: a, 53: b, 54: c}, last=3)
        series = next(m for m in r["matrix"] if m["path"] == "establishment.admitted_with_seats")["series"]
        self.assertEqual([s["state"] for s in series], ["absent", "null", "present"])
        self.assertEqual(series[2]["kind"], "int")

    def test_top_key_set_flip(self):
        a = e52_restored(52)
        b = e52_restored(53)
        b["brand_new_field"] = 1
        r = poa.presence_report({52: a, 53: b}, last=2)
        self.assertTrue(r["key_flips"])
        self.assertEqual(r["key_flips"][0]["added"], ["brand_new_field"])

    def test_matrix_first_flip_close(self):
        r = poa.presence_report(self.history(), last=8)
        seats = next(m for m in r["matrix"] if m["path"] == "seats")
        self.assertEqual(seats["first_flip_close"], 50)
        self.assertEqual(seats["newest_kind"], "object")

    def test_output_never_contains_address(self):
        reps = {52: e52_restored(52)}
        reps[52]["seatless"] = [ME, "poa1OTHER"]
        r = poa.presence_report(reps, last=1)
        blob = json.dumps(r)
        self.assertNotIn(ME, blob)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_presence(r)
        self.assertNotIn(ME, buf.getvalue())

    def test_empty_input(self):
        r = poa.presence_report({}, last=4)
        self.assertFalse(r["ok"])


class PresenceLogAndCmdTests(unittest.TestCase):
    def test_append_and_load_log(self):
        with tempfile.TemporaryDirectory() as td:
            p = str(pathlib.Path(td) / "presence.jsonl")
            snap = poa.snapshot_close_report(e52_restored(52))
            poa.append_presence_log(p, [snap])
            rows = poa.load_presence_log(p)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close_epoch"], 52)
            self.assertIn("fields", rows[0])
            # no seatless address arrays in the log
            blob = pathlib.Path(p).read_text()
            self.assertNotIn("poa1", blob)

    def test_cmd_uses_fetch_close_reports_path(self):
        epochs = [{"epoch": 50}, {"epoch": 51}, {"epoch": 52}]
        reports = {50: e50_null(50), 51: e50_null(51), 52: e52_restored(52)}

        def fake_api(method, path, query=None, need_key=False, timeout=30, retries=1):
            if path == "/api/epochs":
                return epochs
            if path == "/api/close-report":
                return reports[int(query["epoch"])]
            raise AssertionError(f"unexpected {method} {path}")

        args = argparse.Namespace(last=3, log="", no_log=True, json=True, full=False, timeout=30)
        buf = io.StringIO()
        with mock.patch.object(poa, "api_call", side_effect=fake_api):
            with contextlib.redirect_stdout(buf):
                try:
                    poa.cmd_close_presence(args)
                    code = 0
                except SystemExit as e:
                    code = e.code or 0
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["newest_close"], 52)
        self.assertGreater(out["critical_flip_count"], 0)
        self.assertNotIn("snapshots", out)  # default JSON omits them


if __name__ == "__main__":
    unittest.main()
