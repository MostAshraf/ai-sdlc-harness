"""M1 done-criteria: the clarify-then-approve gate sequence (RC3/RC4 spec)."""
from __future__ import annotations

import unittest

from harness import gates


def _rec(at: str, text: str, session: str | None = None) -> dict:
    rec = {"at": at, "text": text, "hash": f"h-{at}"}
    if session is not None:
        # the capture hook tags with the DIGEST, never the raw id
        rec["session"] = gates.session_digest(session)
    return rec


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

    def test_stale_capture_and_no_capture_refuse_DIFFERENTLY(self):
        """Field, 2026-08-04 (BUG-2's approve-pre-pr): both causes reported
        identically, and the advice that fits one ("re-present and have the
        human reply again") is what DESTROYS the evidence in the other. The
        human ended up typing APPROVED twice."""
        gates.present(self.state, "g", "2026-01-01T00:05:00+00:00")
        with self.assertRaises(gates.GateRefusal) as none_yet:
            gates.decide(self.state, "g", [], self.options, "now")
        self.assertIn("no human input captured", str(none_yet.exception))
        stale = [_rec("2026-01-01T00:01:00+00:00", "APPROVED")]
        with self.assertRaises(gates.GateRefusal) as aged_out:
            gates.decide(self.state, "g", stale, self.options, "now")
        msg = str(aged_out.exception)
        self.assertIn("PREDATES this presentation", msg)
        self.assertIn("Do NOT", msg)          # names the trap explicitly
        self.assertIn("2026-01-01T00:01:00+00:00", msg)   # which reply aged out

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


class SessionScopedDecisions(unittest.TestCase):
    """Field, 2026-08-26: a `/dev-workflow` run sat mid-gate while a SECOND
    session in the same workspace ran `/story-workflow`; that prompt was
    captured into this run's ledger and, being the newest record after the
    stamp, was parsed as the decision. Capture stays unconditional and
    lossless (the ledger is an append-only audit record) — it only TAGS
    each record with the session it was typed in, and the filtering happens
    here, where the deciding session's own identity resolves the ambiguity
    capture cannot."""

    S_DEV = "test-session-AAAA-dev-workflow"
    S_STORY = "test-session-BBBB-story-workflow"

    def setUp(self):
        self.state = {"gates": {}}
        self.options = ["approved", "rejected"]

    def _present(self, session=None):
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00",
                      session=session)

    def test_a_foreign_sessions_reply_is_ignored_and_names_that_cause(self):
        """The field bug: gate stamped S_dev, deciding from S_dev, the one
        record tagged S_story. It must NOT decide — and the refusal must
        name the session mismatch rather than falling through to the
        generic "no human input captured" text, whose remedy (cd back to
        the workspace root) is actively wrong for this cause."""
        self._present(session=self.S_DEV)
        records = [_rec("2026-01-01T00:01:00+00:00", "rejected",
                        session=self.S_STORY)]
        with self.assertRaises(gates.GateRefusal) as ctx:
            gates.decide(self.state, "g", records, self.options, "now",
                         deciding_session=self.S_DEV)
        msg = str(ctx.exception)
        self.assertIn("1 human repl(y/ies) captured", msg)   # how many
        self.assertIn("DIFFERENT session", msg)              # which cause
        self.assertIn("--re-present", msg)                   # the remedy
        self.assertNotIn("no human input captured", msg)     # not the generic one
        self.assertNotIn("decision", self.state["gates"]["g"])

    def test_an_own_session_reply_is_never_over_rejected(self):
        # The other half of the field trace — the human types in the dev
        # session and the very same call decides — and the over-rejection
        # direction of the rule: mutating the membership test to exclude
        # the OWN session breaks this, while "no filtering at all" does
        # not (newest-wins reaches the same answer). Named for the
        # property it actually discriminates on.
        self._present(session=self.S_DEV)
        records = [_rec("2026-01-01T00:01:00+00:00", "/story-workflow new XD-5",
                        session=self.S_STORY),
                   _rec("2026-01-01T00:02:00+00:00", "rejected",
                        session=self.S_DEV)]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             deciding_session=self.S_DEV)
        self.assertEqual(entry["decision"], "rejected")

    def test_the_deciding_session_qualifies_what_the_stamp_does_not(self):
        """Resumed session — the case the rejected capture-time design
        destroyed. The gate was stamped by S1 (dead terminal); the human
        replies in S2, which is also the session running `--decide`. Since
        `--decide` is invoked BY the session driving the run, an S2-tagged
        record IS this human talking to this run right now."""
        self._present(session="test-session-DEAD-old-terminal")
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                        session=self.S_DEV)]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             deciding_session=self.S_DEV)
        self.assertEqual(entry["decision"], "approved")

    def test_an_untagged_record_still_decides_a_stamped_gate(self):
        """Unknown means USABLE: a pre-fix ledger record, or one captured
        by a platform whose payload has no `session_id`, carries no tag and
        must never be filtered out — the gate being stamped says nothing
        about the ledger's vintage."""
        self._present(session=self.S_DEV)
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED")]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             deciding_session=self.S_DEV)
        self.assertEqual(entry["decision"], "approved")

    def test_the_stamp_alone_qualifies_a_reply_with_no_deciding_session(self):
        """The stamp half of the rule, on its own — the half that had NO
        behavioral test (review finding: deleting `entry["session"]` from
        the allowed set left the whole suite green, because only
        state-shape assertions defended it). The gate is stamped, the
        deciding process reports no session id at all, and the record
        carries the presenting session's tag: the stamp is then the ONLY
        thing that can qualify it, and it must."""
        self._present(session=self.S_DEV)
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                        session=self.S_DEV)]
        entry = gates.decide(self.state, "g", records, self.options, "now")
        self.assertEqual(entry["decision"], "approved")

    def test_an_unstamped_gate_never_filters_even_for_a_known_decider(self):
        """ARMING: the gate's own stamp, and nothing else. Mid-gate
        upgrade (review finding, traced): the run was presented by a
        harness that wrote no stamp, the human replied in session A, the
        terminal was resumed as C, and `--decide` ran from C. Arming on
        "either side is known" hard-refused that genuine reply — a path
        that worked before this rule existed — and the refusal's text was
        false there besides ("a DIFFERENT session than the one that
        presented this gate", when nothing presented it under any
        session). An unstamped gate cannot vouch for any tag, so it must
        not filter."""
        self._present()                                  # no stamp
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                        session="test-session-CCCC-before-the-resume")]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             deciding_session=self.S_DEV)
        self.assertEqual(entry["decision"], "approved")

    def test_a_non_string_stamp_reads_as_unknown_not_as_matches_nothing(self):
        """F11: `entry["session"]` is the one place a digest is read back
        out of state, and state.yaml can be hand-edited or migrated (the
        same reason the CLI catches TypeError at its boundary). A stamp of
        the wrong SHAPE would otherwise arm the filter with a value no tag
        can ever equal — wedging every tagged reply into a permanent
        refusal with no way to reply out of it.

        Each shape runs with NO deciding session as well: with one, a
        broken arming rule is masked, because the decider re-widens
        `allowed` and the reply qualifies for the wrong reason. Only the
        decider-less half proves the stamp shape alone disarmed the
        filter — and the `""` case is defended by `or not stamp` rather
        than by the isinstance check, so without this it had no
        discriminating cover at all (re-verification F2)."""
        for bad in (12345, ["x"], {"a": 1}, True, ""):
            for decider in (self.S_DEV, None):
                with self.subTest(stamp=bad, deciding_session=decider):
                    self.state = {"gates": {}}
                    self._present()
                    self.state["gates"]["g"]["session"] = bad
                    records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                                    session=self.S_DEV)]
                    entry = gates.decide(self.state, "g", records,
                                         self.options, "now",
                                         deciding_session=decider)
                    self.assertEqual(entry["decision"], "approved")

    def test_a_sessionless_present_refuses_to_clear_a_stamp(self):
        """RC3, traced end to end: `present` popped the stamp when the
        process reported no session id, so the `--re-present` that the
        session-mismatch refusal STEERS THE CALLER ONTO disarmed the gate
        — and the very next prompt from any session decided it. Refuse
        instead, and leave the entry untouched (nothing may be mutated
        before the raise, or a refused present would still have re-stamped
        the window)."""
        self._present(session=self.S_DEV)
        stamp = self.state["gates"]["g"]["session"]
        for absent in (None, "", "   \t "):
            with self.subTest(session=absent):
                with self.assertRaises(gates.GateRefusal) as ctx:
                    gates.present(self.state, "g", "2026-01-01T00:07:00+00:00",
                                  session=absent)
                self.assertIn("no session id", str(ctx.exception))
                entry = self.state["gates"]["g"]
                self.assertEqual(entry["session"], stamp)
                self.assertEqual(entry["presented_at"],
                                 "2026-01-01T00:00:00+00:00")

    def test_a_sessionless_present_is_fine_when_there_is_no_stamp(self):
        """The refusal above must not wedge a session-less platform. A
        workspace whose presents never carried an id has no stamp to
        clear, so every gate there presents and re-presents exactly as
        before — the condition needs an existing stamp to fire."""
        self._present()
        gates.present(self.state, "g", "2026-01-01T00:07:00+00:00")
        entry = self.state["gates"]["g"]
        self.assertEqual(entry["presented_at"], "2026-01-01T00:07:00+00:00")
        self.assertNotIn("session", entry)

    def test_a_whitespace_only_session_id_stamps_nothing_and_never_nulls(self):
        """`present` must branch on the DIGEST, not on raw truthiness: a
        whitespace-only id is truthy but digests to None, so `if session:`
        stored `session: null` — the null key `present`'s own contract
        forbids ("absent, never null") — while the filter stayed entirely
        inert."""
        gates.present(self.state, "g", "2026-01-01T00:00:00+00:00",
                      session="   \t ")
        self.assertNotIn("session", self.state["gates"]["g"])

    def test_no_stamp_and_no_deciding_session_filters_nothing(self):
        """Qwen Code and every pre-fix run: the gate carries no stamp, so
        the filter never arms and the rule is entirely inert —
        byte-for-byte the behavior before it existed, INCLUDING for a
        record that happens to carry a tag from somewhere."""
        self._present()
        records = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                        session=self.S_STORY)]
        entry = gates.decide(self.state, "g", records, self.options, "now")
        self.assertEqual(entry["decision"], "approved")

    def test_a_newer_foreign_reply_never_beats_an_own_session_one(self):
        """The discriminating case: both sessions typed, and the FOREIGN
        record is the NEWEST. Newest-wins must run over the survivors of
        the session filter, not over the raw window — otherwise the field
        bug reproduces exactly (the foreign `/story-workflow` prompt WAS
        the newest record when it swallowed the human's `rejected`)."""
        self._present(session=self.S_DEV)
        records = [_rec("2026-01-01T00:01:00+00:00", "rejected",
                        session=self.S_DEV),
                   _rec("2026-01-01T00:09:00+00:00", "/story-workflow new XD-5",
                        session=self.S_STORY)]
        entry = gates.decide(self.state, "g", records, self.options, "now",
                             deciding_session=self.S_DEV)
        self.assertEqual(entry["decision"], "rejected")
        self.assertEqual(entry["evidence"], "h-2026-01-01T00:01:00+00:00")

    def test_a_stale_own_session_reply_still_reports_the_stale_cause(self):
        """Ordering guard between the two refusals: nothing lands in the
        window at all here, so the session branch must not claim a mismatch
        — the aged-out diagnostic (and its very different remedy) still
        owns this case."""
        gates.present(self.state, "g", "2026-01-01T00:05:00+00:00",
                      session=self.S_DEV)
        stale = [_rec("2026-01-01T00:01:00+00:00", "APPROVED",
                      session=self.S_DEV)]
        with self.assertRaises(gates.GateRefusal) as ctx:
            gates.decide(self.state, "g", stale, self.options, "now",
                         deciding_session=self.S_DEV)
        self.assertIn("PREDATES this presentation", str(ctx.exception))

    def test_session_digest_never_raises_on_a_non_string(self):
        """`session_digest` runs INSIDE the UserPromptSubmit hook, whose
        payload is untrusted JSON from another process. Before this, an
        int `session_id` raised AttributeError there — failing the whole
        capture open and dropping evidence for every run in the
        workspace."""
        for bad in (12345, None, {"id": "x"}, ["x"], b"bytes", 1.5, "", "  \t "):
            with self.subTest(session=bad):
                self.assertIsNone(gates.session_digest(bad))
        self.assertEqual(gates.session_digest("  abc  "),
                         gates.session_digest("abc"))
        self.assertEqual(len(gates.session_digest("abc")), 16)
        self.assertNotIn("abc", gates.session_digest("abc"))


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
