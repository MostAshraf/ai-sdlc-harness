"""M1 done-criteria: the clarify-then-approve gate sequence (RC3/RC4 spec)."""
from __future__ import annotations

import unittest

from harness import gates


def _rec(at: str, text: str) -> dict:
    return {"at": at, "text": text, "hash": f"h-{at}"}


class GateDecisions(unittest.TestCase):
    def setUp(self):
        self.state = {"gates": {}}
        self.options = ["approved", "rejected"]

    def test_clarify_then_approve_takes_most_recent(self):
        gates.present(self.state, "approve-plan", "2026-01-01T00:00:00+00:00")
        records = [
            _rec("2026-01-01T00:01:00+00:00", "why is task T2 needed?"),   # clarify
            _rec("2026-01-01T00:03:00+00:00", "APPROVED"),                  # approve
        ]
        entry = gates.decide(self.state, "approve-plan", records, self.options,
                             "2026-01-01T00:03:01+00:00")
        self.assertEqual(entry["decision"], "approved")
        self.assertEqual(entry["evidence"], "h-2026-01-01T00:03:00+00:00")

    def test_no_input_after_presentation_refuses(self):
        gates.present(self.state, "g", "2026-01-01T00:05:00+00:00")
        stale = [_rec("2026-01-01T00:01:00+00:00", "APPROVED")]  # before stamp
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", stale, self.options, "now")

    def test_qualified_approval_is_not_an_approval(self):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED but rename T3 first")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now")

    def test_restamp_invalidates_earlier_approval(self):
        # An ad-hoc interaction re-presents; a pre-restamp APPROVED must not count.
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED")]
        gates.present(self.state, "g", "2026-01-01T00:02:00+00:00")  # re-stamp
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now")

    def test_rejection_with_notes_decides_when_lenient(self):
        """Field (session D): 'REJECTED — split the web work' refused,
        costing a triage spawn + a re-present round-trip for the canonical
        reply shape at a plan gate. Non-forward options may lead the reply
        and carry notes; over-rejecting is the safe direction."""
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00",
                        "REJECTED — split the web work into two tasks")]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             lenient=frozenset({"rejected"}))
        self.assertEqual(entry["decision"], "rejected")

    def test_library_default_stays_strict_without_lenient(self):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "rejected — see notes")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now")

    def test_qualified_approval_refused_even_with_rejection_leniency(self):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED but rename T3")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now",
                         lenient=frozenset({"rejected"}))

    def test_lenient_word_must_lead_the_reply(self):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00",
                        "not rejected, just have questions")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now",
                         lenient=frozenset({"rejected"}))

    def test_lenient_disposition_with_notes(self):
        # security gate: fix-now is the non-forward disposition
        gates.present(self.state, "sec", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00",
                        "fix-now: the token is real, remediate first")]
        entry = gates.decide(self.state, "sec", records,
                             ["fix-now", "waive", "defer"], "now",
                             lenient=frozenset({"fix-now"}))
        self.assertEqual(entry["decision"], "fix-now")

    def test_numbered_option_and_disposition_options(self):
        gates.present(self.state, "sec", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "2")]
        entry = gates.decide(self.state, "sec", records,
                             ["fix-now", "waive", "defer"], "now")
        self.assertEqual(entry["decision"], "waive")

    def test_option_text_match_case_insensitive(self):
        gates.present(self.state, "sec", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "Defer")]
        entry = gates.decide(self.state, "sec", records,
                             ["fix-now", "waive", "defer"], "now")
        self.assertEqual(entry["decision"], "defer")

    def test_undecided_gate_cannot_be_decided_without_presentation(self):
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "never-shown", [_rec("x", "APPROVED")],
                         self.options, "now")

    def test_out_of_range_number_refused(self):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "7")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "g", records, self.options, "now")


class MultiSelectGate(unittest.TestCase):
    """select-comments and any future `select` gate: a comma-separated
    numbered selection parses to a LIST decision (adversarial-review
    finding — the prior single-decision model couldn't express "address
    comments 1 and 3")."""

    def setUp(self):
        self.state = {"gates": {}}
        self.options = ["c1", "c2", "c3"]

    def test_comma_separated_numbers_resolve_to_option_list(self):
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "1, 3")]
        entry = gates.decide(self.state, "select-comments", records,
                             self.options, "now", multi=True)
        self.assertEqual(entry["decision"], ["c1", "c3"])

    def test_single_number_still_works_multi(self):
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "2")]
        entry = gates.decide(self.state, "select-comments", records,
                             self.options, "now", multi=True)
        self.assertEqual(entry["decision"], ["c2"])

    def test_duplicate_selection_deduped_preserving_order(self):
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "2,1,2")]
        entry = gates.decide(self.state, "select-comments", records,
                             self.options, "now", multi=True)
        self.assertEqual(entry["decision"], ["c2", "c1"])

    def test_one_bad_token_refuses_whole_selection(self):
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "1,9")]
        with self.assertRaises(gates.GateRefusal):
            gates.decide(self.state, "select-comments", records,
                         self.options, "now", multi=True)

    def test_option_name_selection_works_multi(self):
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        records = [_rec("2026-01-01T00:01:00+00:00", "c3,c1")]
        entry = gates.decide(self.state, "select-comments", records,
                             self.options, "now", multi=True)
        self.assertEqual(entry["decision"], ["c3", "c1"])

    def test_none_sentinel_parses_to_an_empty_selection(self):
        # adversarial-review round 2 finding, independently confirmed by
        # both review lenses: no real human-typed text could ever produce
        # decision=[] before this — every string either matched real
        # options or refused as unparseable — even though the manifest and
        # step docs document an empty selection as forward-legal. This
        # derives it from a REAL gates.decide() call, not a manually
        # injected [] (which the prior test coverage relied on).
        gates.present(self.state, "select-comments", "2026-01-01T00:00:00+00:00")
        for reply in ("none", "NONE", "None."):
            records = [_rec("2026-01-01T00:01:00+00:00", reply)]
            entry = gates.decide(self.state, "select-comments", records,
                                 self.options, "now", multi=True)
            self.assertEqual(entry["decision"], [])


if __name__ == "__main__":
    unittest.main()
