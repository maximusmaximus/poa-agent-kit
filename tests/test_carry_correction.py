"""Mocked tests for kit 1.9.4: `poa.py release` reports the v0.83 carry correction (ballot 2464 = A).
No network, no real key. Run: python3 -m unittest tests/test_carry_correction.py"""
import contextlib, importlib.util, io, os, pathlib, tempfile, unittest

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_cc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

H = "a" * 64
REL = {"current": {**{k: H for k in poa.RELEASE_HASH_KEYS}, "version": "v0.83-carrycorr-inert", "ts": 1793813657,
                   "txid": "ca" * 32}, "history": []}
ENTRIES = {"poa1Bexample": "12.5", "poa1Aexample": 3, "poa1Cexample": 7.0}
# Expected value computed with Node: JSON.stringify(Object.keys(e).sort().map(k => [k, String(e[k])])) then sha256.
EXPECTED_JSON = '[["poa1Aexample","3"],["poa1Bexample","12.5"],["poa1Cexample","7"]]'


class CarryCorrectionTests(unittest.TestCase):
    def test_inert_when_not_served(self):
        r = poa.release_report(REL, {"estab_ttl": 26})
        self.assertFalse(r["carry_correction"]["served"])
        self.assertTrue(r["carry_correction"]["effect"].startswith("inert"))
        self.assertNotIn("carry_correction_table_mismatch", r["flags"])

    def test_table_hash_matches_engine_canonical_form(self):
        import hashlib
        self.assertEqual(poa.carry_correction_table_sha(ENTRIES), hashlib.sha256(EXPECTED_JSON.encode()).hexdigest())

    def test_served_and_hash_ok(self):
        cc = {"from_epoch": 44, "entries": ENTRIES, "table_sha256": poa.carry_correction_table_sha(ENTRIES)}
        r = poa.release_report(REL, {"carry_correction": cc})
        c = r["carry_correction"]
        self.assertTrue(c["served"] and c["table_ok"])
        self.assertEqual((c["from_epoch"], c["entries"], c["pinned_total"]), (44, 3, 22.5))
        self.assertNotIn("carry_correction_table_mismatch", r["flags"])

    def test_hash_mismatch_flagged(self):
        cc = {"from_epoch": 44, "entries": ENTRIES, "table_sha256": "0" * 64}
        r = poa.release_report(REL, {"carry_correction": cc})
        self.assertFalse(r["carry_correction"]["table_ok"])
        self.assertIn("carry_correction_table_mismatch", r["flags"])

    def test_print_never_shows_addresses(self):
        cc = {"from_epoch": 44, "entries": ENTRIES, "table_sha256": poa.carry_correction_table_sha(ENTRIES)}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_release(poa.release_report(REL, {"carry_correction": cc}))
        out = buf.getvalue()
        self.assertIn("carry correction (as served): from epoch 44, 3 entries", out)
        self.assertNotIn("poa1", out)


if __name__ == "__main__":
    unittest.main()
