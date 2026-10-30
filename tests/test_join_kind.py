"""Join challenge 'kind' field (server v0.67-joindiff, Oct 29 2026). Mocked responses only."""
import argparse, contextlib, importlib.util, io, json, pathlib, unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("poa_jk", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


class JoinKindTests(unittest.TestCase):
    def test_pow_kind_is_mined(self):
        frag = poa.mined_join_pow({"challenge": "abc", "kind": "pow", "bits": 4, "expires_ts": None})
        self.assertEqual(frag["challenge"], "abc")
        self.assertEqual(frag["_bits"], 4)

    def test_missing_kind_still_mined(self):
        frag = poa.mined_join_pow({"challenge": "abc", "bits": 2})
        self.assertIsNotNone(frag["nonce"])

    def test_unknown_kind_refused_without_mining(self):
        with mock.patch.object(poa, "solve_join_pow") as solve:
            with self.assertRaises(RuntimeError) as cm:
                poa.mined_join_pow({"challenge": "abc", "kind": "edge", "bits": 30})
            solve.assert_not_called()
        self.assertIn("not 'pow'", str(cm.exception))

    def test_join_pow_command_shows_kind(self):
        with mock.patch.object(poa, "fetch_join_pow", return_value={"challenge": "c", "kind": "pow", "bits": 20, "required": True}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poa.cmd_join_pow(argparse.Namespace(solve=False))
        out = json.loads(buf.getvalue())
        self.assertEqual(out["kind"], "pow")
        self.assertEqual(out["bits"], 20)


if __name__ == "__main__":
    unittest.main()
