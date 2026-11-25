"""Offline tests for prior-containment (SUGGESTIONS #191). No network: http_request, api_call and
socket.create_connection are patched to fail in every test; POA_HOME is a temp dir; the key is a dummy."""
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("POA_HOME", tempfile.mkdtemp())
os.environ.setdefault("POA_API_KEY", "pk_test_fake")
spec = importlib.util.spec_from_file_location("poa_pc", pathlib.Path(__file__).resolve().parents[1] / "bin" / "poa.py")
poa = importlib.util.module_from_spec(spec); spec.loader.exec_module(poa)


def _no_net(*a, **k):
    raise AssertionError("network call attempted in an offline test")


PRIOR = ("A ledger of unvetted entrants cannot bypass the gate of council sign-off, because each new tundra "
         "identity must lock a jade bond that is slashed when a garden audit finds a duplicate, so an attacker "
         "pays the bond per fake identity while an honest agent pays it once and gets it back.")
# Same frame, seed words swapped (tundra/jade/garden -> comet/oasis/willow): a reskin.
RESKIN = ("A ledger of unvetted entrants cannot bypass the gate of council sign-off, because each new comet "
          "identity must lock a oasis bond that is slashed when a willow audit finds a duplicate, so an attacker "
          "pays the bond per fake identity while an honest agent pays it once and gets it back.")
FRESH = ("Graders sample three random rows per close and post a stake; a row that a later panel overturns costs the "
         "grader its stake, so a farm running fifty comet keys must fund fifty oasis stakes and lose them on the "
         "first willow audit, while one honest grader risks a single stake it rarely loses.")
W_NEW = ["comet", "oasis", "willow"]
W_OLD = ["tundra", "jade", "garden"]


class Offline(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(poa, "http_request", side_effect=_no_net),
                  mock.patch.object(poa, "api_call", side_effect=_no_net),
                  mock.patch.object(socket, "create_connection", side_effect=_no_net)):
            p.start(); self.addCleanup(p.stop)


def seat(answer, priors, txid="ab" * 32, epoch=125):
    return {"answer_txid": txid, "answer_epoch": epoch, "answer": answer, "words": W_NEW, "author_prior": priors}


def prior(text=PRIOR, words=W_OLD, epoch=122, txid="cd" * 32):
    return {"answer_text": text, "seed_words": words, "epoch": epoch, "answer_txid": txid}


class ContainmentTests(Offline):
    def test_reskin_is_near_copy_after_seed_masking(self):
        r = poa.prior_containment(RESKIN, W_NEW, [prior()])
        self.assertEqual(r["band"], "near_copy")
        self.assertEqual(r["prior_containment_5"], 1.0)
        self.assertEqual(r["matched_prior"]["epoch"], 122)
        self.assertEqual(r["matched_prior"]["txid"], "cd" * 6)
        self.assertEqual(r["priors_n"], 1)

    def test_unmasked_would_be_lower(self):
        # Without masking the swapped seed words break 5-grams; masking is what catches the frame.
        base = poa.seed_masked_ngrams(RESKIN, [])
        other = poa.seed_masked_ngrams(PRIOR, [])
        self.assertLess(poa.ngram_containment(base, other), 0.8)

    def test_fresh_answer_is_low(self):
        r = poa.prior_containment(FRESH, W_NEW, [prior()])
        self.assertEqual(r["band"], "low")
        self.assertLess(r["prior_containment_5"], 0.5)

    def test_short_reskin_inside_longer_prior_is_one_way(self):
        short = RESKIN.split(", so an attacker")[0] + "."
        r = poa.prior_containment(short, W_NEW, [prior()])
        self.assertEqual(r["band"], "near_copy")
        # Symmetric Jaccard on the same pair stays well under the containment value.
        a = poa.word_shingles(poa.normalize_seed_slots(short, W_NEW), 3)
        b = poa.word_shingles(poa.normalize_seed_slots(PRIOR, W_OLD), 3)
        self.assertLess(len(a & b) / len(a | b), r["prior_containment_5"])

    def test_partial_reuse_is_read_prior(self):
        half = RESKIN.split(", so an attacker")[0] + ", and a kiln ledger keeps every refund visible to the next panel so nobody spends twice."
        r = poa.prior_containment(half, W_NEW, [prior()])
        self.assertEqual(r["band"], "read_prior", r)

    def test_max_over_priors_and_which_matched(self):
        r = poa.prior_containment(RESKIN, W_NEW, [prior(FRESH, W_NEW, 120, "ee" * 32), prior(epoch=123, txid="ff" * 32)])
        self.assertEqual(r["matched_prior"]["index"], 1)
        self.assertEqual(r["matched_prior"]["epoch"], 123)
        self.assertEqual(len(r["per_prior"]), 2)

    def test_no_prior_and_too_short(self):
        self.assertEqual(poa.prior_containment(RESKIN, W_NEW, [])["band"], "no_prior")
        self.assertIsNone(poa.prior_containment(RESKIN, W_NEW, [])["prior_containment_5"])
        self.assertEqual(poa.prior_containment("too short", W_NEW, [prior()])["band"], "too_short")


class SeatAuditTests(Offline):
    def test_audit_has_fields_but_not_in_flags(self):
        a = poa.audit_assignment_seat(seat(RESKIN, [prior()]))
        self.assertEqual(a["prior_containment_band"], "near_copy")
        self.assertEqual(a["prior_containment_5"], 1.0)
        self.assertEqual(a["author_priors_n"], 1)
        self.assertEqual(a["prior_containment_match"]["epoch"], 122)
        self.assertFalse(any("CONTAIN" in f for f in a["flags"]))
        self.assertIn("prior_shingle_overlap", a)  # Jaccard kept alongside

    def test_proposed_scores_unchanged_by_new_fields(self):
        # propose_grade reads only `flags`; dropping the new fields must not change the proposal.
        for s in (seat(RESKIN, [prior()]), seat(FRESH, [prior()]), seat(FRESH, [])):
            a = poa.audit_assignment_seat(s)
            stripped = {k: v for k, v in a.items() if not k.startswith(("prior_containment", "author_priors"))}
            self.assertEqual(poa.propose_grade(s["answer"], s["words"], a),
                             poa.propose_grade(s["answer"], s["words"], stripped))

    def test_plan_grades_has_no_containment_keys(self):
        q = poa.plan_grades([seat(RESKIN, [prior()])])
        self.assertEqual(set(q[0]) - {"txid", "score", "note", "epoch", "style", "flags"}, set())


class CommandTests(Offline):
    def run_cmd(self, data, *extra):
        d = tempfile.mkdtemp()
        f = pathlib.Path(d) / "seats.json"
        f.write_text(json.dumps(data))
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["poa.py", "prior-containment", str(f), *extra]), redirect_stdout(buf):
            try:
                poa.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, buf.getvalue()

    def test_text_output(self):
        code, out = self.run_cmd({"assignments": [seat(RESKIN, [prior()]), seat(FRESH, [prior()], txid="12" * 32)]})
        self.assertEqual(code, 0, out)
        self.assertIn("near_copy matched prior #1 (E122)", out)
        self.assertIn("counts: low=1, near_copy=1", out)
        self.assertIn("not a score", out)

    def test_json_output(self):
        code, out = self.run_cmd([seat(RESKIN, [prior()])], "--json")
        self.assertEqual(code, 0)
        j = json.loads(out)
        self.assertTrue(j["read_only"])
        self.assertEqual(j["sent"], 0)
        self.assertEqual(j["rows"][0]["band"], "near_copy")
        self.assertEqual(j["reference_3390"][-1]["pass_rate"], 0.595)

    def test_empty_and_bad_file(self):
        code, _ = self.run_cmd({"assignments": []})
        self.assertEqual(code, 2)
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["poa.py", "prior-containment", "/nonexistent/x.json"]), redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                poa.main()
        self.assertEqual(cm.exception.code, 2)

    def test_version(self):
        self.assertEqual(poa.KIT_VERSION, "1.9.27")


if __name__ == "__main__":
    unittest.main()
