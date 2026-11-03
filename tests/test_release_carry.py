"""Mocked tests for kit 1.9.2: `poa.py release` reports the carry effect of carry_cap_pools (release v0.82-carryoff).
No network, no real key. Run: python3 -m unittest tests/test_release_carry.py"""
import contextlib, importlib.util, io, os, pathlib, tempfile, unittest

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_carry", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)

H = "a" * 64


def rel(version, ts, txid, **over):
    r = {k: H for k in poa.RELEASE_HASH_KEYS}
    r.update(version=version, ts=ts, txid=txid)
    r.update(over)
    return r


PREV = rel("v0.81-asgcap", 1793669204, "b7" * 32, params_sha256="0f" * 32, server_sha256="45" * 32)
CUR = rel("v0.82-carryoff", 1793693552, "0b" * 32, params_sha256="5e" * 32, server_sha256="45" * 32,
          changelog_ref="carry_cap_pools 1->0 (burn not carry; ep37 stands)")
RELEASE = {"current": CUR, "history": [PREV, CUR]}
BASE = {"estab_ttl": 26, "emission_carry_from_epoch": 36, "final_payout_basis_from_epoch": 36}


class CarryEffectTests(unittest.TestCase):
    def test_cap_zero_means_burn(self):
        r = poa.release_report(RELEASE, dict(BASE, carry_cap_pools=0))
        self.assertTrue(r["carry_effect"].startswith("burn"))
        self.assertIn("carry_capped_to_zero", r["flags"])
        self.assertTrue(r["params_only_release"])

    def test_cap_one_means_carry(self):
        r = poa.release_report(RELEASE, dict(BASE, carry_cap_pools=1))
        self.assertTrue(r["carry_effect"].startswith("carry: from epoch 36, up to 1x"))
        self.assertNotIn("carry_capped_to_zero", r["flags"])

    def test_signed_before_served(self):
        # The release signs the new params hash while /api/params still serves the old cap: report what is served.
        r = poa.release_report(RELEASE, dict(BASE, carry_cap_pools=1))
        self.assertIn("params_hash_changed", r["flags"])
        self.assertEqual(r["current"]["changelog_ref"], "carry_cap_pools 1->0 (burn not carry; ep37 stands)")

    def test_carry_off_when_not_served(self):
        r = poa.release_report(RELEASE, {"estab_ttl": 26})
        self.assertTrue(r["carry_effect"].startswith("off"))

    def test_cap_absent_uses_engine_default_one(self):
        r = poa.release_report(RELEASE, BASE)
        self.assertTrue(r["carry_effect"].startswith("carry: from epoch 36, up to 1x"))

    def test_print_shows_effect(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            poa.print_release(poa.release_report(RELEASE, dict(BASE, carry_cap_pools=0)))
        self.assertIn("carry effect (as served): burn", buf.getvalue())
        self.assertIn("flag: carry_capped_to_zero", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
