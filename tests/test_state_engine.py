"""M1 done-criteria: legal/illegal transitions (both FSMs), collision refusal,
round-bound escalation, red-proof guard, stall procedure, escalation edge."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from harness import chain, gates, ndjson, state as state_mod, transitions, workflow
from harness.cli import load_declared
from tests import support

T0 = "2026-01-01T00:00:00+00:00"


def _bootstrap(workspace: Path, mode: str, tasks=None,
               intents=("test_val",)) -> tuple[Path, dict]:
    run = workspace / "ai" / "2026-01-01-TEST-1"
    st = state_mod.bootstrap(
        run, workspace,
        work_item={"id": "TEST-1", "title": "t", "provider_ref": ""},
        mode=mode, change_type="fix",
        tasks=tasks or [{"id": "T1"}], entry_step="fetch")
    # a full-mode TDD task carries plan-declared intents; `intents=()`
    # models the docs/chore opt-out (`test_intents: []`), which exempts
    # the red-proof completion guard
    for t in st["tasks"]:
        t["test_intents"] = list(intents)
    state_mod.save(run, workspace, st)
    return run, st


class Harness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.manifest, self.fsm, self.config = load_declared(self.workspace)
        self.key = chain.load_or_create_key(self.workspace)

    def tearDown(self):
        support.rmtree(self.workspace)

    # -- helpers ----------------------------------------------------------
    def advance_to(self, st, run, target_step, artifacts=None):
        """Walk the cursor to `target_step`, auto-approving gates, marking
        tasks done at a `requires_tasks_terminal` step, and recording
        declared artifacts on the way."""
        artifacts = artifacts or {}
        for _ in range(40):
            current = st["cursor"]["current_step"]
            if current == target_step:
                return
            for name, value in artifacts.get(current, {}).items():
                transitions.set_artifact(st, self.manifest, name, value)
            step_def = self.manifest["steps"][current]
            if step_def.get("gate") and "decision" not in (st["gates"].get(current) or {}):
                gates.present(st, current, T0)
                st["gates"][current]["decision"] = "approved"  # unit shortcut
            if step_def.get("requires_tasks_terminal"):
                for t in st.get("tasks", []):
                    t["status"] = "done"  # unit shortcut for the real TDD completion path
            cands = transitions.cursor_candidates(st, self.manifest, self.config)
            self.assertTrue(cands, f"stuck at {current}")
            nxt = next(iter(cands))
            transitions.advance_cursor(st, self.manifest, self.config, nxt, T0)
        self.fail(f"never reached {target_step}")


class CollisionRefusal(Harness):
    def test_second_bootstrap_refused(self):
        _bootstrap(self.workspace, "full")
        with self.assertRaises(state_mod.CollisionError) as ctx:
            _bootstrap(self.workspace, "full")
        self.assertIn("Resume or Abort", str(ctx.exception))

    def _bootstrap_dated(self, run_name, item_id):
        run = self.workspace / "ai" / run_name
        return run, state_mod.bootstrap(
            run, self.workspace,
            work_item={"id": item_id, "title": "t", "provider_ref": ""},
            mode="full", change_type="fix", tasks=[{"id": "T1"}],
            entry_step="fetch", manifest=self.manifest)

    def test_work_item_scoped_collision_across_dates(self):
        # adversarial-review finding: parking a run Monday and resuming
        # Tuesday used to bootstrap a silent SECOND run under the new date
        # instead of refusing — the original check compared only the exact
        # ai/<today>-<id>/ path, not "does a live run for this item exist
        # anywhere".
        run1, _ = self._bootstrap_dated("2026-01-01-SAME-1", "SAME-1")
        with self.assertRaises(state_mod.CollisionError) as ctx:
            self._bootstrap_dated("2026-01-02-SAME-1", "SAME-1")
        self.assertIn("Resume or Abort", str(ctx.exception))
        self.assertIn(str(run1), str(ctx.exception))

    def test_same_day_refetch_after_abort_gets_a_fresh_slot(self):
        """Field (session D phase 0): bootstrap's exact-path collision was
        existence-only and terminal-blind — abort's documented slot release
        held for every date EXCEPT today's, because the deterministic
        `<date>-<id>` dir still existed."""
        run1, st = self._bootstrap_dated("2026-01-01-SLOT-1", "SLOT-1")
        st["aborted"] = {"at": T0, "reason": "drill"}
        state_mod.save(run1, self.workspace, st)
        base = self.workspace / "ai" / "2026-01-01-SLOT-1"
        slot = state_mod.next_run_slot(base, self.workspace, self.manifest)
        self.assertEqual(slot.name, "2026-01-01-SLOT-1-2")
        run2, _ = self._bootstrap_dated(slot.name, "SLOT-1")  # no collision
        # a THIRD same-day ask stops AT the live slot-2 run, so bootstrap's
        # collision refusal still fires for genuinely-live occupants
        self.assertEqual(
            state_mod.next_run_slot(base, self.workspace, self.manifest), run2)

    def test_terminal_occupant_direct_bootstrap_says_terminal_not_live(self):
        run1, st = self._bootstrap_dated("2026-01-03-SLOT-2", "SLOT-2")
        st["aborted"] = {"at": T0, "reason": "drill"}
        state_mod.save(run1, self.workspace, st)
        with self.assertRaises(state_mod.CollisionError) as ctx:
            self._bootstrap_dated("2026-01-03-SLOT-2", "SLOT-2")
        self.assertIn("terminal", str(ctx.exception))
        self.assertNotIn("live run", str(ctx.exception))

    def test_live_suffixed_slot_still_blocks_other_dates(self):
        # the sibling scan must recognize `-<n>` slot names as the same item
        self._bootstrap_dated("2026-01-01-SLOT-3-2", "SLOT-3")
        with self.assertRaises(state_mod.CollisionError):
            self._bootstrap_dated("2026-01-02-SLOT-3", "SLOT-3")

    def test_suffix_grammar_does_not_cross_items(self):
        # a REAL item literally named 'SLOT-4-2' shares its dir name with
        # item 'SLOT-4' slot 2 — the sealed state's own id is the
        # tiebreaker, so neither blocks the other
        self._bootstrap_dated("2026-01-01-SLOT-4-2", "SLOT-4-2")
        self._bootstrap_dated("2026-01-02-SLOT-4", "SLOT-4")  # no collision

    def test_terminal_sibling_does_not_block_a_new_run(self):
        run1, st = self._bootstrap_dated("2026-01-01-DONE-1", "DONE-1")
        st["cursor"]["current_step"] = self.manifest["modes"]["full"][-1]
        state_mod.save(run1, self.workspace, st)
        self._bootstrap_dated("2026-01-02-DONE-1", "DONE-1")  # must not raise

    def test_suffix_collision_avoided_between_ids(self):
        # '1' vs 'TEST-1': a naive suffix/glob match on the safe-id portion
        # of the run-dir name would treat "...-TEST-1" as colliding with a
        # bootstrap for id '1' — the fixed-width-date slice must not.
        self._bootstrap_dated("2026-01-01-TEST-1", "TEST-1")
        self._bootstrap_dated("2026-01-02-1", "1")  # must not raise

    def test_concurrent_same_item_different_date_bootstraps_serialize(self):
        # adversarial-review finding (reproduced 150/150): two concurrent
        # bootstraps of one item under DIFFERENT dates took different
        # per-run locks, so nothing serialized them and BOTH passed the
        # no-live-sibling check — two live runs for one item, breaking the
        # B5 invariant abort's slot-release depends on. The item-level lock
        # serializes them: exactly one wins.
        import threading
        results: list = []

        def boot(date):
            try:
                self._bootstrap_dated(f"{date}-RACE-1", "RACE-1")
                results.append("ok")
            except state_mod.CollisionError:
                results.append("refused")
            except Exception as exc:  # pragma: no cover
                results.append(f"err:{exc}")

        for _ in range(25):
            for d in self.workspace.glob("ai/*"):
                support.rmtree(d, ignore_errors=True)
            results.clear()
            t1 = threading.Thread(target=boot, args=("2026-03-01",))
            t2 = threading.Thread(target=boot, args=("2026-03-02",))
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(sorted(results), ["ok", "refused"],
                             f"both bootstraps saw no sibling: {results}")

    def test_corrupt_sibling_blocks_a_new_bootstrap_fails_closed(self):
        # adversarial-review round 2 finding: an unreadable sibling (crashed
        # mid-write, or genuinely tampered — this module can't tell the two
        # apart) used to be silently SKIPPED by the collision check, letting
        # a second bootstrap proceed right when B5 matters most — a corrupt
        # run might still hold real in-progress work.
        run1, _ = self._bootstrap_dated("2026-01-01-BAD-1", "BAD-1")
        (run1 / "state.yaml").write_text(
            (run1 / "state.yaml").read_text(encoding="utf-8") + "# tampered\n")
        with self.assertRaises(state_mod.CollisionError) as ctx:
            self._bootstrap_dated("2026-01-02-BAD-1", "BAD-1")
        self.assertIn("Resume or Abort", str(ctx.exception))


class LockedReadMutualExclusion(Harness):
    """adversarial-review round 2 finding: an earlier fix for the
    stray-directory-on-typo'd-run bug dropped locking for show/verify
    entirely, reasoning that state_mod.load's atomic-replace made a bare
    read safe on its own — it doesn't, since chain.seal's content-then-seal
    write is two SEPARATE atomic replaces (not one transaction), so an
    unlocked reader could land between them and see a spurious
    IntegrityError. locked_read's shared lock must actually block against
    a concurrent exclusive writer, not just exist as a no-op wrapper."""

    def test_locked_read_blocks_until_an_exclusive_writer_releases(self):
        run, st = _bootstrap(self.workspace, "full")
        events: list[str] = []
        writer_holds = threading.Event()
        release_writer = threading.Event()

        def writer():
            with state_mod.locked(run):
                writer_holds.set()
                release_writer.wait(timeout=5)
                events.append("writer-done")

        def reader():
            writer_holds.wait(timeout=5)
            with state_mod.locked_read(run):
                events.append("reader-acquired")

        t_writer = threading.Thread(target=writer)
        t_reader = threading.Thread(target=reader)
        t_writer.start()
        self.assertTrue(writer_holds.wait(timeout=5), "writer never acquired the lock")
        t_reader.start()
        # The reader must still be blocked here — give it every chance to
        # (wrongly) acquire immediately before asserting it hasn't.
        t_reader.join(timeout=0.3)
        self.assertEqual(events, [], "reader acquired a shared lock while "
                         "an exclusive writer still held it")
        release_writer.set()
        t_writer.join(timeout=5)
        t_reader.join(timeout=5)
        self.assertEqual(events, ["writer-done", "reader-acquired"])

    def test_locked_read_does_not_mkdir_a_nonexistent_run(self):
        bogus = self.workspace / "ai" / "2026-01-01-NOPE-1"
        self.assertFalse(bogus.exists())
        with state_mod.locked_read(bogus):
            pass
        self.assertFalse(bogus.exists())


class CursorLegality(Harness):
    def test_sequence_order_enforced(self):
        run, st = _bootstrap(self.workspace, "full")
        with self.assertRaises(transitions.TransitionError):
            transitions.advance_cursor(st, self.manifest, self.config, "develop", T0)

    def test_gate_blocks_until_decided_then_forwards(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "approve-plan")
        self.assertEqual(transitions.cursor_candidates(st, self.manifest, self.config), {})
        gates.present(st, "approve-plan", T0)
        st["gates"]["approve-plan"]["decision"] = "approved"
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(list(cands), ["preflight"])

    def test_gate_rejection_routes_to_declared_reentry(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "approve-plan")
        gates.present(st, "approve-plan", T0)
        st["gates"]["approve-plan"]["decision"] = "rejected"
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(cands, {"plan": "on_reject"})

    def test_conditional_gate_skipped_when_predicate_false(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "security")
        transitions.set_artifact(st, self.manifest, "security-report", "reports/sec.md")
        transitions.set_artifact(st, self.manifest, "security.max_severity", "low")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("pre-pr", cands)          # gate skipped
        self.assertNotIn("approve-security", cands)

    def test_conditional_gate_required_when_predicate_true(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "security")
        transitions.set_artifact(st, self.manifest, "security.max_severity", "high")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("approve-security", cands)
        self.assertNotIn("pre-pr", cands)

    def test_security_waive_forwards_fix_now_reenters(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "security")
        transitions.set_artifact(st, self.manifest, "security.max_severity", "high")
        transitions.advance_cursor(st, self.manifest, self.config, "approve-security", T0)
        st["gates"]["approve-security"] = {"presented_at": T0, "decision": "waive"}
        self.assertIn("pre-pr", transitions.cursor_candidates(st, self.manifest, self.config))
        st["gates"]["approve-security"]["decision"] = "fix-now"
        self.assertEqual(transitions.cursor_candidates(st, self.manifest, self.config),
                         {"develop": "on_reject"})

    def test_pre_pr_fix_side_step_and_return(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "approve-pre-pr",
                        artifacts={"security": {"security.max_severity": "low"}})
        gates.present(st, "approve-pre-pr", T0)
        st["gates"]["approve-pre-pr"]["decision"] = "rejected"
        transitions.advance_cursor(st, self.manifest, self.config, "pre-pr-fixes", T0)
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("pre-pr", cands)          # declared return edge

    def test_group_entry_and_repeatable_reentry(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "reconcile",
                        artifacts={"security": {"security.max_severity": "low"}})
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("analyze-comments", cands)   # group available after create-pr
        transitions.advance_cursor(st, self.manifest, self.config, "analyze-comments", T0)
        self.advance_to(st, run, "apply-fixes")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(cands.get("analyze-comments"), "group:pr-comments:reenter")

    def test_apply_fixes_has_a_group_exit_to_reconcile(self):
        # adversarial-review finding: without the declared `returns_to`
        # edge, `analyze-comments` (reenter) was the ONLY legal move from
        # apply-fixes — a permanent cursor trap with no way to ever reach
        # reconcile/metrics.
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "reconcile",
                        artifacts={"security": {"security.max_severity": "low"}})
        transitions.advance_cursor(st, self.manifest, self.config, "analyze-comments", T0)
        self.advance_to(st, run, "apply-fixes")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(cands.get("reconcile"), "returns_to")
        self.assertEqual(cands.get("analyze-comments"), "group:pr-comments:reenter")
        transitions.advance_cursor(st, self.manifest, self.config, "reconcile", T0)
        self.assertEqual(st["cursor"]["current_step"], "reconcile")

    def test_develop_blocks_forward_while_any_task_is_not_terminal(self):
        # adversarial-review finding: design.md claims "fail-closed at sync
        # points" is "enforced naturally by the task FSM + gate
        # preconditions" — it wasn't; cursor_candidates never inspected
        # task status at all, so `cursor --to approve-impl` was legal even
        # with every task still pending.
        run, st = _bootstrap(self.workspace, "full",
                             tasks=[{"id": "T1"}, {"id": "T2"}])
        self.advance_to(st, run, "develop")
        st["tasks"][0]["status"] = "done"
        st["tasks"][1]["status"] = "in-review"   # one task not yet terminal
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(cands, {})
        st["tasks"][1]["status"] = "done"
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("approve-impl", cands)

    def test_quick_escalation_edge_switches_mode(self):
        run, st = _bootstrap(self.workspace, "quick")
        self.advance_to(st, run, "quick-recheck")
        transitions.set_artifact(st, self.manifest, "recheck-verdict", "dirty")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertEqual(cands.get("security"), "escalate:full")
        transitions.advance_cursor(st, self.manifest, self.config, "security", T0)
        self.assertEqual(st["mode"], "full")

    def test_quick_clean_recheck_continues(self):
        run, st = _bootstrap(self.workspace, "quick")
        self.advance_to(st, run, "quick-recheck")
        transitions.set_artifact(st, self.manifest, "recheck-verdict", "clean")
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("pre-pr", cands)

    def test_artifact_must_be_declared_output(self):
        run, st = _bootstrap(self.workspace, "full")
        with self.assertRaises(transitions.TransitionError):
            transitions.set_artifact(st, self.manifest, "security.max_severity", "low")


class RunCompletion(Harness):
    """0.16.13 field class (e2e E2E-1): a run that exhausted its walk
    parked at the final step as 'live' forever ('finished successfully'
    had no first-class form), and approve-security's declared predicate
    self-skip left no ledger trace — indistinguishable from a hole."""

    def test_sequence_advance_returns_skipped_conditional_steps(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "security")
        transitions.set_artifact(st, self.manifest, "security.max_severity",
                                 "info")
        skipped = transitions.advance_cursor(st, self.manifest, self.config,
                                             "pre-pr", T0)
        self.assertEqual([s["step"] for s in skipped], ["approve-security"])
        self.assertIn("security.max_severity", skipped[0]["reason"])
        self.assertIn("'info'", skipped[0]["reason"])

    def test_adjacent_advance_returns_no_skips(self):
        run, st = _bootstrap(self.workspace, "full")
        self.assertEqual(
            transitions.advance_cursor(st, self.manifest, self.config,
                                       "intake", T0), [])

    def test_complete_marks_terminal_and_refuses_further_mutation(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "metrics",
                        artifacts={"security": {"security.max_severity": "low"}})
        state_mod.save(run, self.workspace, st)
        out = workflow.complete_run(self.workspace, run, self.manifest)
        self.assertTrue(out["completed"])
        st2 = state_mod.load(run, self.workspace)
        self.assertTrue(st2["completed"]["at"])
        self.assertIn("metrics", st2["cursor"]["completed_steps"])
        self.assertTrue(st2["metrics"]["metrics"]["ended_at"])
        kinds = [r["kind"] for r in
                 ndjson.read_records(run / "events.ndjson")]
        self.assertIn("completed", kinds)
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.ensure_live(st2, "cursor --to anywhere")
        self.assertIn("completed run", str(ctx.exception))
        with self.assertRaises(transitions.TransitionError):
            workflow.complete_run(self.workspace, run, self.manifest)

    def test_complete_refuses_off_final_step_and_on_live_tasks(self):
        run, st = _bootstrap(self.workspace, "full")
        with self.assertRaises(transitions.TransitionError) as ctx:
            workflow.complete_run(self.workspace, run, self.manifest)
        self.assertIn("final step", str(ctx.exception))
        self.advance_to(st, run, "metrics",
                        artifacts={"security": {"security.max_severity": "low"}})
        st["tasks"][0]["status"] = "in-progress"
        state_mod.save(run, self.workspace, st)
        with self.assertRaises(transitions.TransitionError) as ctx:
            workflow.complete_run(self.workspace, run, self.manifest)
        self.assertIn("not terminal", str(ctx.exception))

    def test_completed_sibling_does_not_block_a_new_run(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "metrics",
                        artifacts={"security": {"security.max_severity": "low"}})
        state_mod.save(run, self.workspace, st)
        workflow.complete_run(self.workspace, run, self.manifest)
        run2 = self.workspace / "ai" / "2026-01-02-TEST-1"
        state_mod.bootstrap(  # released slot: no CollisionError
            run2, self.workspace,
            work_item={"id": "TEST-1", "title": "t", "provider_ref": ""},
            mode="full", change_type="fix", tasks=[{"id": "T1"}],
            entry_step="fetch", manifest=self.manifest)


class SelectGate(Harness):
    """select-comments: a `select` gate has no forward_on/on_reject binary —
    any parsed selection (including an empty one) is forward-legal."""

    def _to_select_comments(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "reconcile",
                        artifacts={"security": {"security.max_severity": "low"}})
        transitions.advance_cursor(st, self.manifest, self.config, "analyze-comments", T0)
        transitions.advance_cursor(st, self.manifest, self.config, "select-comments", T0)
        return run, st

    def test_blocked_until_a_selection_is_recorded(self):
        run, st = self._to_select_comments()
        gates.present(st, "select-comments", T0)
        self.assertEqual(transitions.cursor_candidates(st, self.manifest, self.config), {})

    def test_any_selection_forwards_no_reject_branch(self):
        run, st = self._to_select_comments()
        gates.present(st, "select-comments", T0)
        st["gates"]["select-comments"]["decision"] = ["c2"]
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("apply-fixes", cands)

    def test_empty_selection_still_forwards(self):
        run, st = self._to_select_comments()
        gates.present(st, "select-comments", T0)
        st["gates"]["select-comments"]["decision"] = []   # nothing selected — not a rejection
        cands = transitions.cursor_candidates(st, self.manifest, self.config)
        self.assertIn("apply-fixes", cands)


class TaskFsm(Harness):
    def test_legal_chain_and_illegal_skip(self):
        run, st = _bootstrap(self.workspace, "quick")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        with self.assertRaises(transitions.TransitionError):
            transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                        "T1", "done")   # skips in-review

    def test_no_intents_task_completes_without_red_proof(self):
        """`test_intents: []` (the 0.15.8 TDD opt-out, human-approved at
        the plan gate) exempted only the pre-red
        WRITE lock — this completion guard still demanded a proof that
        verify-red can never produce for a docs-only task (the suite never
        goes red), deadlocking the task and, since develop requires every
        task terminal, the whole run. The opt-out now spans both
        enforcement points; the REVIEW requirement is deliberately NOT
        exempted — docs still get reviewed."""
        run, st = _bootstrap(self.workspace, "full", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")   # no proof demanded
        with self.assertRaises(transitions.TransitionError):
            # review is NOT exempt: done still needs a captured APPROVED
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")

    def test_red_proof_required_in_full_develop(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                        "T1", "in-review")
        self.assertIn("no red-proof", str(ctx.exception))
        proof = {"task": "T1", "test_files": {"tests/x.py": "abc"}, "evidence": "F"}
        path = transitions.redproof_path(run, "T1")
        path.parent.mkdir(parents=True, exist_ok=True)
        chain.seal(path, json.dumps(proof).encode(), self.key,
                   label=transitions.redproof_label("T1"))
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")   # now legal

    def test_tampered_red_proof_is_integrity_error(self):
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        path = transitions.redproof_path(run, "T1")
        path.parent.mkdir(parents=True, exist_ok=True)
        chain.seal(path, b'{"task": "T1"}', self.key,
                   label=transitions.redproof_label("T1"))
        path.write_bytes(b'{"task": "T1", "forged": true}')   # bypass write
        with self.assertRaises(chain.IntegrityError):
            transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                        "T1", "in-review")

    def test_red_proof_is_not_transferable_between_tasks(self):
        """Adversarial-review finding (guarantee seam): the seal used to bind
        CONTENT only, so copying T1's proof + .hmac to T2's proof path
        verified fine and completed T2 with no red proof of its own. The
        identity label in the digest makes the copied pair fail verification
        outright; proof["task"] is re-asserted as belt-and-braces."""
        run, st = _bootstrap(self.workspace, "full",
                             tasks=[{"id": "T1"}, {"id": "T2"}])
        self.advance_to(st, run, "develop")
        for tid in ("T1", "T2"):
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, tid, "in-progress")
        proof = {"task": "T1", "tests": {}, "closure": {}, "evidence": "F"}
        t1 = transitions.redproof_path(run, "T1")
        t1.parent.mkdir(parents=True, exist_ok=True)
        chain.seal(t1, json.dumps(proof).encode(), self.key,
                   label=transitions.redproof_label("T1"))
        # the replay: file-copy T1's proof and seal onto T2's path
        t2 = transitions.redproof_path(run, "T2")
        t2.write_bytes(t1.read_bytes())
        t2.with_name(t2.name + ".hmac").write_bytes(
            t1.with_name(t1.name + ".hmac").read_bytes())
        with self.assertRaises(chain.IntegrityError):
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T2", "in-review")
        # belt-and-braces: even a proof RE-SEALED under T2's label refuses
        # when its content declares another task
        chain.seal(t2, json.dumps(proof).encode(), self.key,
                   label=transitions.redproof_label("T2"))
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T2", "in-review")
        self.assertIn("never transferable", str(ctx.exception))

    def test_red_proof_keys_on_intents_not_mode(self):
        """Composability round 2026-07-08: the guard's activation is the
        task's own declared test_intents — the same condition as the
        hook-side pre-red write lock — never a `mode == full and step ==
        develop` literal pair. Quick stays relaxed because its seed task
        declares no intents; an intent-carrying task demands the proof in
        ANY mode, so a new manifest mode gets TDD enforcement for free."""
        # (a) the real quick shape: intent-less seed task -> no proof needed
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")   # relaxed by declaration
        # (b) an intent-carrying task is guarded even outside (full, develop)
        run2 = self.workspace / "ai" / "2026-01-02-TEST-2"
        st2 = state_mod.bootstrap(
            run2, self.workspace,
            work_item={"id": "TEST-2", "title": "t", "provider_ref": ""},
            mode="quick", change_type="fix",
            tasks=[{"id": "T1"}], entry_step="fetch")
        st2["tasks"][0]["test_intents"] = ["test_val"]
        state_mod.save(run2, self.workspace, st2)
        self.advance_to(st2, run2, "develop")
        transitions.transition_task(st2, self.fsm, self.config, run2, self.key,
                                    "T1", "in-progress")
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st2, self.fsm, self.config, run2,
                                        self.key, "T1", "in-review")
        self.assertIn("no red-proof", str(ctx.exception))

    def test_review_round_bound_refuses_beyond_max(self):
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        task = st["tasks"][0]
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")   # round 1
        self.assertEqual(task["review_rounds"], 1)
        task["review_rounds"] = self.config["review_rounds"]["max"]
        task["status"] = "in-review"
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                        "T1", "in-progress")
        self.assertIn("plan drift", str(ctx.exception))

    def test_reviewer_verdict_required_for_done(self):
        """Adversarial-review finding: in-review -> done had NO guard — the
        per-task review loop was pure orchestrator obedience. Now the
        hook-written reviews.ndjson record is the completion evidence."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("no reviewer verdict", str(ctx.exception))
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review",
                              "verdict": "CHANGES_REQUESTED"})
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("not APPROVED", str(ctx.exception))
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review",
                              "verdict": "APPROVED"})
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "done")   # now legal

    def test_missing_in_review_stamp_fails_closed(self):
        """Adversarial-review finding: `entered = task.get('in_review_at')
        or ''` let every historical record satisfy `> ''` when the stamp
        was absent (a pre-stamp run, or hand-edited state) — a stale
        approval could complete. No stamp → refuse."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        st["tasks"][0].pop("in_review_at", None)   # simulate a pre-stamp run
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review", "verdict": "APPROVED"})
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("no in-review timestamp", str(ctx.exception))

    def test_corrupt_review_ledger_fails_closed(self):
        """A torn newest verdict must not let an older APPROVED win."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review", "verdict": "APPROVED"})
        with (run / "reviews.ndjson").open("a") as fh:
            fh.write('{"task":"T1","mode":"review","verdict":"CHANGES_REQ')
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("corrupt", str(ctx.exception))

    def test_stale_approval_does_not_complete_a_rework(self):
        """A round-1 APPROVED must not complete a round-2 rework whose
        re-review never happened: the verdict must postdate the task's
        LATEST entry into in-review."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review",
                              "verdict": "APPROVED"})
        # reviewer-requested rework, then re-completion without a re-review
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("no reviewer verdict", str(ctx.exception))

    def test_task_dependency_order_enforced(self):
        """depends_on used to be stored and enforced by nothing — the
        declared task DAG was decorative (adversarial-review finding)."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick",
                             tasks=[{"id": "T1"},
                                    {"id": "T2", "depends_on": ["T1"]}], intents=())
        self.advance_to(st, run, "develop")
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T2", "in-progress")
        self.assertIn("depends_on T1", str(ctx.exception))
        for to in ("in-progress", "in-review"):
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", to)
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review",
                              "verdict": "APPROVED"})
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "done")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T2", "in-progress")   # now legal

    def test_unsafe_task_id_refused_at_bootstrap(self):
        # task ids flow into git branch/worktree/proof-file names unsanitized
        with self.assertRaises(state_mod.StateError):
            _bootstrap(self.workspace, "quick", tasks=[{"id": "T 1; rm -rf"}])

    def test_hotfix_edge_needs_declared_context(self):
        run, st = _bootstrap(self.workspace, "quick")
        st["tasks"][0]["status"] = "archived"
        with self.assertRaises(transitions.TransitionError):
            transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                        "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress", context="hotfix-clone")

    def test_stall_procedure_is_bounded(self):
        run, st = _bootstrap(self.workspace, "quick")
        actions = [transitions.record_stall(st, self.config, "T1") for _ in range(3)]
        self.assertEqual(actions, ["reinvoke", "recovery", "human"])


if __name__ == "__main__":
    unittest.main()
