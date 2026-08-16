"""M1 done-criteria: legal/illegal transitions (both FSMs), collision refusal,
round-bound escalation, red-proof guard, stall procedure, escalation edge."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import chain, gates, ndjson, state as state_mod, transitions, workflow
from harness.cli import load_declared
from tests import support

T0 = "2026-01-01T00:00:00+00:00"


def _bootstrap(workspace: Path, mode: str, tasks=None,
               intents=("test_val",), repo_ambiguity="single") -> tuple[Path, dict]:
    run = workspace / "ai" / "2026-01-01-TEST-1"
    # `repo-ambiguity` is a fetch-produced artifact that quick's `confirm-repo`
    # predicate reads, and eval_predicate RAISES on a missing artifact rather
    # than reading it as false — so these raw-bootstrap fixtures (which bypass
    # the fetch verb) have to seed it exactly as fetch would. Default
    # "single": these are one-repo fixtures, which is the case where
    # confirm-repo is skipped and the walk stays fetch -> preflight.
    st = state_mod.bootstrap(
        run, workspace,
        work_item={"id": "TEST-1", "title": "t", "provider_ref": ""},
        mode=mode, change_type="fix",
        tasks=tasks or [{"id": "T1"}], entry_step="fetch",
        artifacts={"repo-ambiguity": repo_ambiguity})
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
            if step_def.get("verdict_bound"):
                # unit shortcut for the hook-captured reviewer verdict a
                # verdict_bound step's exits derive from
                support.seed_review_verdict(
                    run, mode=step_def["verdict_bound"]["mode"])
            cands = transitions.cursor_candidates(st, self.manifest,
                                                  self.config, run=run)
            self.assertTrue(cands, f"stuck at {current}")
            nxt = next(iter(cands))
            transitions.advance_cursor(st, self.manifest, self.config, nxt, T0,
                                       run=run)
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


class RunLockWaitsOutItsHolder(Harness):
    """Round 4, measured: with `merge-task` holding the run lock across the
    merge itself, EVERY other run-scoped verb queues behind it — and on
    Windows `msvcrt.LK_LOCK` gave up after ~10s with a raw
    `OSError: Resource deadlock avoided`. A 15.6s merge on a 20k-file
    checkout therefore killed every concurrent verb at ~9.4s, `show`
    included. Waiting is the correct behaviour; the budget exists only so an
    abandoned lock ends in a named refusal rather than a hang."""

    HOLD = 0.6      # seconds — short and synthetic; the point is that the
                    # second verb WAITS, not how long it can wait
    TINY_BUDGET = 0.5           # injected into the WAITER (env override)
    REVERT_WOULD_ACQUIRE_BY = 6.0   # holder cap: inside LK_LOCK's own ~10s
                                    # retry, so a reverted build acquires here
                                    # rather than erroring for the wrong reason

    def test_a_second_verb_waits_for_the_holder_and_then_succeeds(self):
        """Kills the no-retry mutation: a bare non-blocking lock attempt
        fails instantly here, where the fix's retry loop rides out the
        hold. Deliberately NOT a 10s hold — that would test the platform's
        old budget rather than this code, and cost 10s every run."""
        run, _ = _bootstrap(self.workspace, "full")
        acquired: list[float] = []
        holder_has_it = threading.Event()

        def holder():
            with state_mod.locked(run):
                holder_has_it.set()
                time.sleep(self.HOLD)

        def waiter():
            holder_has_it.wait(timeout=10)
            started = time.monotonic()
            with state_mod.locked(run):       # must WAIT, never raise
                acquired.append(time.monotonic() - started)

        t_hold = threading.Thread(target=holder)
        t_wait = threading.Thread(target=waiter)
        t_hold.start()
        self.assertTrue(holder_has_it.wait(timeout=10), "holder never locked")
        t_wait.start()
        t_hold.join(timeout=30)
        t_wait.join(timeout=30)
        self.assertEqual(len(acquired), 1,
                         "the second verb never acquired the lock")
        # …and it genuinely waited rather than sailing through a lock that
        # wasn't held (POSIX flock and the Windows retry loop both block)
        self.assertGreater(acquired[0], self.HOLD / 2)

    def test_past_the_budget_the_refusal_names_the_lock_and_the_retry(self):
        """The other half: a lock nobody ever releases must end in a
        StateError that says what is holding it and what to do — not the
        platform's `Resource deadlock avoided`, which named neither and
        reached the CLI's JSON error contract verbatim.

        Driven through the retry helper with an always-failing attempt, so
        it runs on POSIX too (where the real `flock` path blocks forever by
        design and cannot produce this state at all)."""
        def never_acquires():
            raise OSError(36, "Resource deadlock avoided")

        with mock.patch.object(state_mod, "LOCK_WAIT_BUDGET", 0.05):
            with self.assertRaises(state_mod.StateError) as ctx:
                state_mod._wait_for_lock(never_acquires,
                                         self.workspace / "ai" / "r"
                                         / ".state.lock")
        msg = str(ctx.exception)
        self.assertIn("run lock", msg)
        self.assertIn("merge-task", msg)          # the likely holder class
        self.assertIn("retry the identical command", msg)
        self.assertIn(".state.lock", msg)         # which lock

    def test_the_budget_is_injectable_per_process_for_a_real_cli_run(self):
        """The env override exists so a subprocess CLI invocation can be
        given a tiny budget; a garbage value falls back to the declared
        default rather than crashing the verb that reads it."""
        with mock.patch.dict(os.environ,
                             {state_mod._BUDGET_ENV: "0.25"}):
            self.assertEqual(state_mod._lock_wait_budget(), 0.25)
        for garbage in ("not-a-number", "inf", "nan"):
            # inf and nan PARSE — and each defeats the budget's whole purpose
            # (inf never expires; nan makes every deadline comparison False),
            # which is the hang the StateError exists to replace.
            with mock.patch.dict(os.environ,
                                 {state_mod._BUDGET_ENV: garbage}):
                self.assertEqual(state_mod._lock_wait_budget(),
                                 state_mod.LOCK_WAIT_BUDGET, garbage)

    @unittest.skipUnless(sys.platform == "win32",
                         "the retry loop is Windows-only in effect — POSIX "
                         "`flock` blocks in the kernel and never reaches it")
    def test_the_retry_loop_itself_is_what_waits_not_the_platform(self):
        """The DISCRIMINATOR the 0.6s-hold test above is not: reverting
        `_lock_exclusive` to a plain `msvcrt.LK_LOCK` rides out a sub-second
        hold exactly as well as the retry loop does, so that test alone left
        the whole fix mutable.

        Injects a tiny budget behind a holder that outlasts it by an order of
        magnitude. THIS code refuses at ~the budget with the StateError that
        names the lock; LK_LOCK — whose own retry is ~10 one-second attempts,
        wider than the hold — would instead sit there and ACQUIRE once the
        holder released, raising nothing at all. Asserting both the refusal
        and that it arrived fast is what tells the two apart."""
        run, _ = _bootstrap(self.workspace, "full")
        holder_has_it = threading.Event()
        release = threading.Event()

        def holder():
            with state_mod.locked(run):
                holder_has_it.set()
                release.wait(timeout=self.REVERT_WOULD_ACQUIRE_BY)
        t_hold = threading.Thread(target=holder)
        t_hold.start()
        try:
            self.assertTrue(holder_has_it.wait(timeout=10),
                            "holder never locked")
            started = time.monotonic()
            with mock.patch.dict(
                    os.environ,
                    {state_mod._BUDGET_ENV: str(self.TINY_BUDGET)}):
                with self.assertRaises(state_mod.StateError) as ctx:
                    with state_mod.locked(run):
                        pass                  # LK_LOCK's revert lands HERE
            elapsed = time.monotonic() - started
        finally:
            # released and joined INSIDE the test, not via addCleanup: those
            # run after tearDown, and Windows refuses to unlink a .state.lock
            # a live holder thread still has open.
            release.set()
            t_hold.join(timeout=30)
        self.assertIn("run lock", str(ctx.exception))
        self.assertLess(elapsed, 5.0,
                        "the refusal came from waiting out the holder, not "
                        f"from the injected {self.TINY_BUDGET}s budget")


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

    def test_the_sync_point_refusal_names_the_laggard_and_its_status(self):
        """The sync point used to fall through to the generic "not declared
        legal / legal: none (gate undecided?)" message — which names the one
        thing develop does NOT have (a gate) and never the thing the
        orchestrator has to act on. With DAG-pipelined dispatch several tasks
        are moving at once, so "which one is still going, and how far is it"
        is the whole content of the answer."""
        run, st = _bootstrap(self.workspace, "full",
                             tasks=[{"id": "T1"}, {"id": "T2"}, {"id": "T3"}])
        self.advance_to(st, run, "develop")
        st["tasks"][0]["status"] = "done"
        st["tasks"][1]["status"] = "in-review"
        st["tasks"][2]["status"] = "pending"
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(st, self.manifest, self.config,
                                       "approve-impl", T0, run=run)
        msg = str(ctx.exception)
        self.assertIn("T2 is in-review", msg)
        self.assertIn("T3 is pending", msg)
        self.assertNotIn("T1", msg)              # terminal tasks are not news
        self.assertIn("ready-tasks", msg)        # the verb that shows the why
        self.assertNotIn("gate undecided", msg)  # the old misdirection
        # …and once they are all terminal the same move is simply legal —
        # the refusal is about task state, never about this edge
        for t in st["tasks"]:
            t["status"] = "done"
        transitions.advance_cursor(st, self.manifest, self.config,
                                   "approve-impl", T0, run=run)
        self.assertEqual(st["cursor"]["current_step"], "approve-impl")

    def test_an_undeclared_target_still_gets_the_generic_refusal(self):
        """The dedicated message must not swallow every refusal AT develop:
        with all tasks terminal, a nonsense target is a legality error and
        has to read as one."""
        run, st = _bootstrap(self.workspace, "full", tasks=[{"id": "T1"}])
        self.advance_to(st, run, "develop")
        for t in st["tasks"]:
            t["status"] = "done"
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(st, self.manifest, self.config,
                                       "create-pr", T0, run=run)
        self.assertIn("not declared legal", str(ctx.exception))

    def test_a_bogus_target_gets_the_generic_refusal_MID_develop_too(self):
        """Round 4: declared legality is checked FIRST. The sync-point
        message claims the tasks are what is holding the move — true only
        for a target the manifest would otherwise allow. A typo'd target got
        it too, so the orchestrator was sent to go finish its tasks for a
        move that would still be refused afterwards."""
        run, st = _bootstrap(self.workspace, "full",
                             tasks=[{"id": "T1"}, {"id": "T2"}])
        self.advance_to(st, run, "develop")
        st["tasks"][0]["status"] = "in-review"        # genuinely mid-develop
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(st, self.manifest, self.config,
                                       "create-pr", T0, run=run)
        msg = str(ctx.exception)
        self.assertIn("not declared legal", msg)
        self.assertNotIn("T1 is in-review", msg)      # not the tasks' fault
        # …while the target the sync point IS holding still gets named
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(st, self.manifest, self.config,
                                       "approve-impl", T0, run=run)
        self.assertIn("T1 is in-review", str(ctx.exception))
        self.assertIn("shows where each one is", str(ctx.exception))


class TerminalVocabularyIsDeclared(Harness):
    """RC-H: `terminal:` in pipeline/task-fsm.yaml is the ONE definition of
    "this task is finished", read by every site that asks. Seven hardcoded
    `("done", "archived")` literals were the alternative, and six of them
    were chances for a future status to be terminal to part of the engine
    and live to the rest. Driven by patching the loader's cache: if a site
    still carried its own literal, that site would not move with the
    declaration."""

    def _declaring(self, *statuses):
        return mock.patch.object(transitions, "_TERMINAL_CACHE",
                                 tuple(statuses))

    def test_the_picture_and_the_dependency_guard_move_with_it(self):
        run, st = _bootstrap(self.workspace, "full",
                             tasks=[{"id": "T1"},
                                    {"id": "T2", "depends_on": ["T1"]}])
        st["tasks"][0]["status"] = "done"
        with self._declaring("archived"):     # `done` no longer counts
            picture = workflow.dispatch_picture(st)
            self.assertEqual(picture["terminal"], [])
            self.assertEqual(picture["in_flight"],
                             [{"id": "T1", "status": "done"}])
            self.assertEqual(picture["blocked"],
                             [{"id": "T2", "waiting_on": ["T1"]}])
            with self.assertRaises(transitions.TransitionError):
                transitions.transition_task(st, self.fsm, self.config, run,
                                            self.key, "T2", "in-progress")
        # …and under the real declaration the identical state dispatches
        picture = workflow.dispatch_picture(st)
        self.assertEqual((picture["terminal"], picture["ready"]),
                         (["T1"], ["T2"]))
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T2", "in-progress")

    def test_the_develop_sync_point_moves_with_it_on_both_halves(self):
        run, st = _bootstrap(self.workspace, "full", tasks=[{"id": "T1"}])
        self.advance_to(st, run, "develop")
        st["tasks"][0]["status"] = "done"
        with self._declaring("archived"):
            # legality half
            self.assertEqual(
                transitions.cursor_candidates(st, self.manifest, self.config),
                {})
            # refusal half — and it names the declared vocabulary, not a
            # hardcoded "(done/archived)"
            with self.assertRaises(transitions.TransitionError) as ctx:
                transitions.advance_cursor(st, self.manifest, self.config,
                                           "approve-impl", T0, run=run)
            self.assertIn("(archived)", str(ctx.exception))
            self.assertIn("T1 is done", str(ctx.exception))
        self.assertIn("approve-impl",
                      transitions.cursor_candidates(st, self.manifest,
                                                    self.config))

    def test_complete_and_env_check_move_with_it_too(self):
        run, st = _bootstrap(self.workspace, "full", tasks=[{"id": "T1"}])
        self.advance_to(st, run, "metrics",
                        artifacts={"security": {"security.max_severity": "low"}})
        st["tasks"][0]["status"] = "done"    # advance_to's own shortcut, pinned
        st["tasks"][0]["env_requires"] = ["docker"]
        state_mod.save(run, self.workspace, st)
        probe = {"env_requirements": {"docker": {"probe": support.NOP_CMD,
                                                 "hint": "start it"}}}
        with self._declaring("archived"):
            # `complete` refuses a run whose tasks are not terminal…
            with self.assertRaises(transitions.TransitionError) as ctx:
                workflow.complete_run(self.workspace, run, self.manifest)
            self.assertIn("are not terminal", str(ctx.exception))
            # …and env-check still probes a task it no longer considers done
            self.assertEqual(
                [c["name"] for c in
                 workflow.env_check(self.workspace, run, probe)["checked"]],
                ["docker"])
        # a done task is nobody's problem, and the run completes
        self.assertEqual(
            workflow.env_check(self.workspace, run, probe)["checked"], [])
        self.assertTrue(
            workflow.complete_run(self.workspace, run, self.manifest)["completed"])

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


class VerdictBoundExits(Harness):
    """plan-review's exits are DERIVED from the hook-captured verdict
    ledger (`verdict_bound`): fail-closed with no in-window verdict,
    loop-forcing on CHANGES_REQUESTED under the bound, forward-only on
    APPROVED or bound exhaustion (the human sees the failing report —
    never a deadlock, never an auto-approval)."""

    def setUp(self):
        super().setUp()
        self.run, self.st = _bootstrap(self.workspace, "full")
        # advance_to stops the moment current == target, so no verdict has
        # been seeded for plan-review itself: the ledger window is clean.
        self.advance_to(self.st, self.run, "plan-review")

    def _cands(self):
        return transitions.cursor_candidates(self.st, self.manifest,
                                             self.config, run=self.run)

    def test_no_verdict_means_no_exits(self):
        self.assertEqual(self._cands(), {})
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(self.st, self.manifest, self.config,
                                       "approve-plan", T0, run=self.run)
        self.assertIn("no in-window reviewer verdict", str(ctx.exception))

    def test_no_run_handle_fails_closed(self):
        # A caller that can't provide the ledger gets NO exits, not a guess.
        support.seed_review_verdict(self.run)
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config), {})

    def test_changes_requested_under_bound_forces_the_loop(self):
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        self.assertEqual(self._cands(), {"plan": "returns_to"})
        # and the loop edge actually walks: back to plan (which re-arms
        # registration), re-register, forward again
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan", T0, run=self.run)
        for t in self.st["tasks"]:   # unit shortcut for plan-register
            t.pop("provisional", None)
        self.assertIn("plan-review", transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run))

    def test_approved_opens_forward_only(self):
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        support.seed_review_verdict(self.run, verdict="APPROVED")  # latest wins
        self.assertEqual(self._cands(), {"approve-plan": "sequence"})

    def test_bound_exhaustion_opens_forward_with_the_failing_verdict(self):
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        self.assertEqual(self._cands(), {"approve-plan": "sequence"})

    def test_gate_decision_resets_the_round_window(self):
        # A human rejection at approve-plan starts a fresh cycle: verdicts
        # from before the decision must not satisfy (or exhaust) the new one.
        support.seed_review_verdict(self.run, verdict="APPROVED")
        self.st["gates"]["approve-plan"] = {
            "decision": "rejected", "decided_at": "2999-01-01T00:00:00+00:00"}
        self.assertEqual(self._cands(), {})

    def test_corrupt_ledger_fails_closed(self):
        support.seed_review_verdict(self.run)
        with open(self.run / "reviews.ndjson", "a", encoding="utf-8") as fh:
            fh.write("{torn record\n")
        with self.assertRaises(transitions.TransitionError) as ctx:
            self._cands()
        self.assertIn("corrupt", str(ctx.exception))

    def test_outcome_artifact_engine_recorded(self):
        # `pending` was stamped on entry (advance_to walked us in), and the
        # forward exit resolves it from the ledger — the value lean's
        # exception gate trusts is never orchestrator-written.
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "pending")
        support.seed_review_verdict(self.run, verdict="APPROVED")
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan", T0, run=self.run)
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "approved")

    def test_outcome_artifact_exhausted_on_bound_exit(self):
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan", T0, run=self.run)
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "exhausted")

    def test_outcome_stays_pending_across_the_revision_loop(self):
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan", T0, run=self.run)
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "pending")   # loop edge decides nothing
        for t in self.st["tasks"]:   # unit shortcut for plan-register
            t.pop("provisional", None)
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan-review", T0, run=self.run)
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "pending")   # re-entry re-stamps


    def test_lens_verdicts_are_advisory_and_invisible_to_the_engine(self):
        # The adversarial panel's advisory/binding line, engine-pinned:
        # plan-attack records (task-less, a different reviewer mode) must
        # never open this step's exits, close them, or burn the round
        # budget — only the synthesizer's mode counts. A future edit
        # normalizing capture modes would fail here, not in production.
        support.seed_review_verdict(self.run, mode="plan-attack",
                                    verdict="APPROVED")
        self.assertEqual(self._cands(), {})   # no synthesizer verdict yet
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, mode="plan-attack",
                                        verdict="CHANGES_REQUESTED")
        support.seed_review_verdict(self.run, verdict="APPROVED")
        # the lens CRs burned nothing: the synthesizer's APPROVED opens
        # forward instead of the bound reading as exhausted
        self.assertEqual(self._cands(), {"approve-plan": "sequence"})

    def test_timestamp_tie_prefers_changes_requested(self):
        # Two hook processes can stamp identical `at` (coarse OS clock);
        # a first-wins max() would let APPROVED shadow the rejection.
        tie = "2026-01-01T00:00:01+00:00"
        for verdict in ("APPROVED", "CHANGES_REQUESTED"):
            ndjson.append_record(self.run / "reviews.ndjson",
                                 {"task": None, "mode": "plan-review",
                                  "verdict": verdict, "at": tie})
        self.assertEqual(self._cands(), {"plan": "returns_to"})

    def test_returns_to_reentry_rearms_registration(self):
        # The loop edge re-arms requires_tasks_registered: a revised plan
        # must re-register (else round 1's task list sails to the gate).
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan", T0, run=self.run)
        self.assertTrue(all(t.get("provisional")
                            for t in self.st["tasks"]))
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run), {})
        for t in self.st["tasks"]:   # unit shortcut for plan-register
            t.pop("provisional", None)
        self.assertIn("plan-review", transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run))

    def test_returns_to_reentry_rearms_the_repo_confirmation(self):
        # The repo marker's half of the same rule (adversarial-review B/8:
        # the re-arm shipped with no test at all). cursor_candidates hardens
        # `requires_repo_confirmed` against EVERY exit including future loop
        # edges — but a marker that never cleared would satisfy a re-entered
        # confirming step on the PREVIOUS round's confirmation and release it
        # immediately. No mode has such an edge today, so this is driven
        # against a synthetic one, exactly as the hardening is written for.
        manifest = copy.deepcopy(self.manifest)
        manifest["steps"]["plan"]["requires_repo_confirmed"] = True
        self.st["repo_confirmed"] = {"repo": "/p", "at": T0}
        for t in self.st["tasks"]:      # isolate from the sibling re-arm
            t.pop("provisional", None)
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        transitions.advance_cursor(self.st, manifest, self.config,
                                   "plan", T0, run=self.run)
        self.assertNotIn("repo_confirmed", self.st)
        self.assertEqual(transitions.cursor_candidates(
            self.st, manifest, self.config, run=self.run), {})

    def test_gate_decision_is_consumed_by_the_edge_it_legalizes(self):
        # A stale rejection must not re-open its on_reject edge on a later
        # arrival — after one human rejection, a humanless
        # plan↔plan-review↔approve-plan cycle was engine-legal without this.
        support.seed_review_verdict(self.run)
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan", T0, run=self.run)
        gates.present(self.st, "approve-plan", T0)
        self.st["gates"]["approve-plan"]["decision"] = "rejected"
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan", T0, run=self.run)
        entry = self.st["gates"]["approve-plan"]
        self.assertNotIn("decision", entry)
        self.assertEqual(entry["consumed_decision"], "rejected")
        # presented_at is consumed WITH the decision (re-verification
        # finding: left behind, the capture hook's presented-and-undecided
        # window stayed open forever, and a decide at a re-arrived gate
        # could qualify stray prompts against the stale presentation)
        self.assertNotIn("presented_at", entry)
        # re-arrival at the gate fail-closes until a fresh present+decide
        for t in self.st["tasks"]:
            t.pop("provisional", None)   # unit shortcut for re-register
        support.seed_review_verdict(self.run)
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan-review", T0, run=self.run)
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan", T0, run=self.run)
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run), {})


class ShowNextSteps(Harness):
    """`harness show` surfaces the ENGINE-legal next cursor moves and any
    ledger-fresh verdict_bound outcome WITHOUT ever writing state. Field
    motive: at a verdict_bound step (plan-review) the persisted
    `<step>.outcome` artifact is stamped `pending` on step ENTRY and only
    re-derived from the reviewer-verdict ledger by the next `cursor --to`, so
    an orchestrator polling `show` between the reviewer's captured verdict and
    the move it legalizes saw a stale `pending` and no hint that the forward
    exit was already the sole engine-legal one. The output also carries
    `probe_error` — null when the candidates walk completed, else the engine's
    own reason there is no legal move yet — so an empty `next_steps` is never
    silently conflated with a wedged run. These tests drive the REAL CLI over a
    subprocess so the emitted JSON contract itself is pinned."""

    def _plan_review_run(self):
        """A full-mode run PERSISTED on disk with the cursor parked at
        plan-review and a clean verdict window (advance_to stops the moment
        current == target, before plan-review's own verdict is seeded)."""
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "plan-review")
        state_mod.save(run, self.workspace, st)   # persist cursor at plan-review
        return run, st

    def _show(self, run) -> dict:
        proc = subprocess.run(
            [str(support.HARNESS_BIN), "--workspace", str(self.workspace),
             "--run", str(run), "show"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "NO_COLOR": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_approved_verdict_surfaces_forward_exit_and_fresh_outcome(self):
        # The whole point: an APPROVED verdict is captured but no advance has
        # consumed it yet, so state.yaml still caches `pending`.
        run, _ = self._plan_review_run()
        support.seed_review_verdict(run, verdict="APPROVED")
        out = self._show(run)
        # next_steps is the same {step_id: reason} dict `cursor --to`
        # validates against — the forward exit is already legal
        self.assertEqual(out["next_steps"].get("approve-plan"), "sequence")
        # derived carries the ledger-FRESH outcome the persisted cache hasn't
        # caught up to...
        self.assertEqual(out["derived"], {"plan-review.outcome": "approved"})
        # ...while the emitted `state` stays the UNMODIFIED persisted snapshot
        # (auditability: show's `state` must match disk, never a silent
        # rewrite of the ledger-fresh value back into artifacts)
        self.assertEqual(
            out["state"]["artifacts"]["plan-review.outcome"], "pending")
        self.assertIsNone(out["probe_error"])   # the walk completed cleanly

    def test_show_is_read_only_state_file_byte_identical(self):
        # cursor_candidates MUTATES the dict it's handed (it re-stamps the
        # outcome via set_artifact); the deep-copy guard must keep that off
        # disk. A stray re-seal would flip both files below.
        run, _ = self._plan_review_run()
        support.seed_review_verdict(run, verdict="APPROVED")
        state_file = state_mod.state_path(run)
        hmac_file = state_file.with_name(state_file.name + ".hmac")
        before, hmac_before = state_file.read_bytes(), hmac_file.read_bytes()
        self._show(run)
        self.assertEqual(state_file.read_bytes(), before, "show rewrote state.yaml")
        self.assertEqual(hmac_file.read_bytes(), hmac_before, "show re-sealed state")
        # and the integrity chain still verifies (load raises on a broken
        # seal) — the persisted outcome is still the pre-show `pending`
        reloaded = state_mod.load(run, self.workspace)
        self.assertEqual(reloaded["artifacts"]["plan-review.outcome"], "pending")

    def test_outstanding_flagged_summary_is_reported_per_kind(self):
        """The wait-vs-stall decision is made at exactly the moment the
        orchestrator re-reads `show`, and `show` reported no flagged events
        at all — so the one record that answers it (`spawn-pending`: that
        subagent is still running) was invisible without reading ndjson by
        hand. Same shared filter as `status`/metrics, so the numbers can
        never disagree; resolved items pair off and drop out."""
        run, _ = self._plan_review_run()
        self.assertEqual(self._show(run)["outstanding_flagged"], {})
        for rec in ({"kind": "spawn-pending", "agent_id": "a-1",
                     "task": "step:plan-review:contradictions"},
                    {"kind": "spawn-pending", "agent_id": "a-2",
                     "task": "step:plan-review:coverage"},
                    {"kind": "hook-blocked", "actor": "reviewer"}):
            ndjson.append_record(run / "events.ndjson", rec)
        self.assertEqual(self._show(run)["outstanding_flagged"],
                         {"spawn-pending": 2, "hook-blocked": 1})
        ndjson.append_record(run / "events.ndjson",
                             {"kind": "spawn-captured", "agent_id": "a-1",
                              "actor": "capture"})
        self.assertEqual(self._show(run)["outstanding_flagged"],
                         {"spawn-pending": 1, "hook-blocked": 1})

    def test_no_verdict_yields_empty_next_steps(self):
        # fail-closed exactly like the engine: no in-window verdict -> no
        # legal exit, and nothing to refresh off the `pending` cache
        run, _ = self._plan_review_run()   # clean window, no verdict seeded
        out = self._show(run)
        self.assertEqual(out["next_steps"], {})
        self.assertEqual(out["derived"], {})
        # probe_error stays NULL here: the walk COMPLETED and legitimately
        # found no legal exit (fail-closed), which is a different thing from
        # the walk raising — the honest distinction probe_error exists for.
        self.assertIsNone(out["probe_error"])
        # state is still emitted in full — show never regresses to a refusal
        self.assertEqual(
            out["state"]["cursor"]["current_step"], "plan-review")

    def test_terminal_run_shows_no_next_steps(self):
        # A terminal run (here aborted — the same marker ensure_live refuses
        # every mutation on) has no legal cursor move: show reports empty
        # next_steps/derived and still emits state, working exactly as it did
        # before these fields existed.
        run, st = _bootstrap(self.workspace, "full")
        st["aborted"] = {"at": T0, "reason": "drill"}
        state_mod.save(run, self.workspace, st)
        out = self._show(run)
        self.assertEqual(out["next_steps"], {})
        self.assertEqual(out["derived"], {})
        self.assertIsNone(out["probe_error"])   # walk skipped, not raised
        self.assertTrue(out["state"]["aborted"])

    def test_changes_requested_surfaces_the_loop_edge(self):
        # An in-bound CHANGES_REQUESTED forces the revision loop: the only
        # legal move is back to `plan`, and the outcome stays `pending` (the
        # loop edge decides nothing), so nothing is derived.
        run, _ = self._plan_review_run()
        support.seed_review_verdict(run, verdict="CHANGES_REQUESTED")
        out = self._show(run)
        self.assertEqual(out["next_steps"], {"plan": "returns_to"})
        self.assertEqual(out["derived"], {})
        self.assertIsNone(out["probe_error"])

    def test_bound_exhaustion_surfaces_forward_and_exhausted_outcome(self):
        # `review_rounds.max` CHANGES_REQUESTED verdicts exhaust the bound:
        # the forward exit opens (the human sees the failing report) and the
        # engine-derived outcome is `exhausted` — ledger-fresh, while the
        # persisted cache is still `pending`.
        run, _ = self._plan_review_run()
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(run, verdict="CHANGES_REQUESTED")
        out = self._show(run)
        self.assertEqual(out["next_steps"].get("approve-plan"), "sequence")
        self.assertEqual(out["derived"], {"plan-review.outcome": "exhausted"})
        self.assertIsNone(out["probe_error"])
        self.assertEqual(
            out["state"]["artifacts"]["plan-review.outcome"], "pending")

    def test_corrupt_ledger_degrades_with_probe_error_not_a_crash(self):
        # New failure surface `show` gains by consulting the ledger: a torn
        # reviews.ndjson at a verdict_bound step fails the derivation closed.
        # `show` is the diagnostic reached for WHEN a run is wedged, so it must
        # still exit 0 and emit the full state — the corruption is reported in
        # probe_error, not raised. (The loud enforcement refusal stays on
        # `cursor --to`, covered by test_corrupt_ledger_fails_closed above.)
        run, _ = self._plan_review_run()
        support.seed_review_verdict(run, verdict="APPROVED")
        with (run / "reviews.ndjson").open("a", encoding="utf-8") as fh:
            fh.write("{torn record\n")
        out = self._show(run)
        self.assertEqual(out["next_steps"], {})
        self.assertIsNotNone(out["probe_error"])
        self.assertIn("corrupt", out["probe_error"])
        # The LedgerCorruption message names the ledger FILE; `show` output is
        # copy-pasted into shared channels, so the absolute run path must be
        # scrubbed to a placeholder (the loud local `cursor --to` refusal
        # keeps the real path for the operator).
        self.assertIn("<run>", out["probe_error"])
        self.assertNotIn(str(run), out["probe_error"])
        self.assertNotIn(str(run.resolve()), out["probe_error"])
        self.assertEqual(   # full state still emitted, unmodified
            out["state"]["cursor"]["current_step"], "plan-review")

    def test_malformed_sealed_state_degrades_not_crashes(self):
        # Regression for the too-narrow guard: a seal-valid state whose `mode`
        # is absent from the manifest (hand-repair + reseal, or a mode renamed
        # out from under a parked run) makes cursor_candidates raise a BARE
        # KeyError deep in the walk — not a TransitionError. The broadened
        # guard must catch it: pre-change `show` emitted such a state fine
        # (rc 0), and degrading here preserves that instead of dumping a raw
        # traceback on the exact wedged run `show` exists to diagnose.
        run, st = _bootstrap(self.workspace, "full")
        st["mode"] = "ghost-mode"               # undeclared -> KeyError in walk
        state_mod.save(run, self.workspace, st)  # reseals via the state module
        out = self._show(run)
        self.assertEqual(out["next_steps"], {})
        self.assertIsNotNone(out["probe_error"])
        self.assertIn("ghost-mode", out["probe_error"])
        self.assertEqual(out["state"]["mode"], "ghost-mode")   # emitted as-is

    def test_security_pre_scan_empty_is_honest_via_probe_error(self):
        # A LIVE, healthy step can also produce an empty next_steps: at
        # `security`, approve-security's `when` predicate reads
        # `security.max_severity` — an artifact `security` itself produces —
        # before the scan records it, so eval_predicate raises. Without
        # probe_error this is byte-identical to a wedged run; with it the
        # orchestrator sees "run this step", not "stuck".
        run, st = _bootstrap(self.workspace, "full")
        self.advance_to(st, run, "security")   # scan artifact not yet recorded
        state_mod.save(run, self.workspace, st)
        out = self._show(run)
        self.assertEqual(out["next_steps"], {})
        self.assertEqual(out["derived"], {})
        self.assertIsNotNone(out["probe_error"])
        self.assertIn("security.max_severity", out["probe_error"])

    def test_lean_approved_panel_surfaces_the_self_skip(self):
        # Lean's exception gate self-skips on an approved panel: `show` must
        # surface the forward edge that skips PAST approve-plan-lean, so an
        # orchestrator sees the gate won't fire before it moves the cursor.
        run, st = _bootstrap(self.workspace, "lean")
        self.advance_to(st, run, "plan-review")
        state_mod.save(run, self.workspace, st)
        support.seed_review_verdict(run, verdict="APPROVED")
        out = self._show(run)
        self.assertEqual(out["next_steps"], {"preflight": "sequence"})
        self.assertEqual(out["derived"], {"plan-review.outcome": "approved"})
        self.assertIsNone(out["probe_error"])


class LeanModeGating(Harness):
    """Lean's exception gate: an approved panel self-skips ⟨approve-plan-
    lean⟩ (ledgered as gate-skipped), an exhausted one fires it — the
    predicate reads the ENGINE-recorded outcome, so a drifting orchestrator
    cannot skip a human past a rejecting panel."""

    def setUp(self):
        super().setUp()
        self.run, self.st = _bootstrap(self.workspace, "lean")
        self.advance_to(self.st, self.run, "plan-review")

    def test_approved_panel_skips_the_exception_gate(self):
        support.seed_review_verdict(self.run)   # panel APPROVED
        cands = transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run)
        self.assertEqual(cands, {"preflight": "sequence"})   # gate skipped
        skipped = transitions.advance_cursor(
            self.st, self.manifest, self.config, "preflight", T0,
            run=self.run)
        self.assertEqual([s["step"] for s in skipped], ["approve-plan-lean"])

    def test_exhausted_panel_fires_the_exception_gate(self):
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        cands = transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run)
        self.assertEqual(list(cands), ["approve-plan-lean"])   # gate REQUIRED
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan-lean", T0, run=self.run)
        # undecided gate: nothing legal until the human decides
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run), {})
        gates.present(self.st, "approve-plan-lean", T0)
        self.st["gates"]["approve-plan-lean"]["decision"] = "rejected"
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run),
            {"plan": "on_reject"})

    def test_lean_walks_develop_to_harden_without_an_impl_gate(self):
        support.seed_review_verdict(self.run)
        self.advance_to(self.st, self.run, "develop")
        for t in self.st["tasks"]:
            t["status"] = "done"   # unit shortcut for the real TDD path
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run),
            {"harden": "sequence"})

    def test_exhaustion_latches_within_the_window(self):
        # adversarial-review (lean round): once the bound is hit,
        # returns_to is closed — no legitimate revision can exist in this
        # window, so a later same-window APPROVED (stall re-spawn,
        # manipulation) must NOT flip the outcome and self-skip the
        # exception gate past a plan the panel rejected `bound` times.
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        support.seed_review_verdict(self.run, verdict="APPROVED")  # round 6
        cands = transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run)
        self.assertEqual(list(cands), ["approve-plan-lean"])   # still fires
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "exhausted")

    def test_second_cycle_skips_after_rejection_and_a_clean_panel(self):
        # exhausted → gate fires → human rejects (fresh window opens at
        # decided_at) → revise + re-register → panel approves → the
        # exception gate self-skips on cycle 2. Pins the full interplay of
        # decision consumption, window re-anchoring, registration re-arm,
        # and the outcome refresh.
        for _ in range(self.config["review_rounds"]["max"]):
            support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "approve-plan-lean", T0, run=self.run)
        gates.present(self.st, "approve-plan-lean", T0)
        self.st["gates"]["approve-plan-lean"].update(
            decision="rejected", decided_at=ndjson.now_iso())
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan", T0, run=self.run)
        for t in self.st["tasks"]:   # unit shortcut for plan-register
            t.pop("provisional", None)
        transitions.advance_cursor(self.st, self.manifest, self.config,
                                   "plan-review", T0, run=self.run)
        support.seed_review_verdict(self.run, verdict="APPROVED")
        self.assertEqual(transitions.cursor_candidates(
            self.st, self.manifest, self.config, run=self.run),
            {"preflight": "sequence"})   # cycle-2 skip
        self.assertEqual(self.st["artifacts"]["plan-review.outcome"],
                         "approved")

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
            tasks=[{"id": "T1"}], entry_step="fetch",
            artifacts={"repo-ambiguity": "single"})
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

    def test_reviewer_verdict_timestamp_tie_fails_closed(self):
        """Same tie rule as verdict_bound: two hook processes can stamp
        identical `at`, and a first-read APPROVED must not shadow a
        same-instant rejection."""
        from harness import ndjson
        run, st = _bootstrap(self.workspace, "quick", intents=())
        self.advance_to(st, run, "develop")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-progress")
        transitions.transition_task(st, self.fsm, self.config, run, self.key,
                                    "T1", "in-review")
        tie = "2999-01-01T00:00:00+00:00"   # both after in_review_at
        for verdict in ("APPROVED", "CHANGES_REQUESTED"):
            ndjson.append_record(run / "reviews.ndjson",
                                 {"task": "T1", "mode": "review",
                                  "verdict": verdict, "at": tie})
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.transition_task(st, self.fsm, self.config, run,
                                        self.key, "T1", "done")
        self.assertIn("not APPROVED", str(ctx.exception))

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


class StallVerdictGuard(Harness):
    """`stall` must not count a stall the run's own verdict ledger already
    answers (field: dual-run comparison — the orchestrator looked for
    a plan-review verdict in events.ndjson, where verdicts never live, and
    re-ran a whole lens panel for a verdict already on disk; the duplicate
    CHANGES_REQUESTED burned one of five rounds straight into exhaustion).

    Round counting and the exhaustion latch stay untouched by design — this
    closes the false stall UPSTREAM instead."""

    KEY = "step:plan-review"
    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        super().setUp()
        self.run, self.st = _bootstrap(self.workspace, "full")
        self.advance_to(self.st, self.run, "plan-review")

    def _cli(self, *args) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "harness", "--workspace",
             str(self.workspace), "--run", str(self.run), *args],
            cwd=self.ROOT, capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def _guard(self, key=None):
        transitions.guard_stall_verdict(self.st, self.manifest, self.run,
                                        key or self.KEY)

    def test_in_round_verdict_refuses_and_mutates_nothing(self):
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        before = json.dumps(self.st, sort_keys=True, default=str)
        events_before = len(ndjson.read_records(self.run / "events.ndjson"))
        with self.assertRaises(transitions.TransitionError) as ctx:
            self._guard()
        msg = str(ctx.exception)
        self.assertIn("CHANGES_REQUESTED", msg)
        self.assertIn("reviews.ndjson", msg)
        self.assertIn("--confirm-no-verdict", msg)
        # state untouched: no step_stalls key, no counter, no event
        self.assertEqual(json.dumps(self.st, sort_keys=True, default=str),
                         before)
        self.assertNotIn(self.KEY, self.st.get("step_stalls", {}))
        self.assertEqual(len(ndjson.read_records(self.run / "events.ndjson")),
                         events_before)

    def test_genuine_stall_with_no_verdict_still_counts(self):
        self._guard()   # no verdict in the ledger at all -> no refusal
        self.assertEqual(
            transitions.record_stall(self.st, self.config, self.KEY),
            "reinvoke")

    def test_previous_round_verdict_does_not_over_refuse(self):
        # Round 1 rejects; the plan is re-registered (the declared
        # round_marker), and round 2's synthesizer genuinely stalls. The
        # round-1 verdict is still inside _verdict_window's window, so a
        # naive "any verdict exists" check would refuse here forever.
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "plan-registered", "actor": "plan-register"})
        self._guard()
        self.assertEqual(
            transitions.record_stall(self.st, self.config, self.KEY),
            "reinvoke")

    def test_recorded_stall_reopens_the_window(self):
        # Two genuine stalls in a row, with a verdict captured only before
        # the first: the recorded stall itself is a round mark, so the
        # second stall must still count (escalation must stay reachable).
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "plan-registered", "actor": "plan-register"})
        transitions.record_stall(self.st, self.config, self.KEY)
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "stall", "task": self.KEY,
                              "action": "reinvoke"})
        self._guard()
        self.assertEqual(
            transitions.record_stall(self.st, self.config, self.KEY),
            "recovery")

    def test_gate_decision_reopens_the_window(self):
        # A human rejection at approve-plan starts a fresh cycle; the
        # pre-decision verdict must stop suppressing stalls (same anchor
        # _verdict_window uses, so the two can never disagree).
        support.seed_review_verdict(self.run, verdict="APPROVED")
        self.st["gates"]["approve-plan"] = {
            "decision": "rejected", "decided_at": "2999-01-01T00:00:00+00:00"}
        self._guard()

    def test_per_task_stall_is_unaffected(self):
        # No verdict_bound governs a per-task spawn — the guard is a no-op
        # there even with a fresh plan-review verdict on the ledger.
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        self._guard("T1")
        self.assertEqual(
            transitions.record_stall(self.st, self.config, "T1"), "reinvoke")

    def _pending(self, key, agent_id="a-1"):
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "spawn-pending", "task": key,
                              "actor": "reviewer", "agent_id": agent_id})

    def test_an_open_pending_refuses_the_stall(self):
        """A background spawn between launch and completion leaves the
        ledgers looking exactly like a stall — no verdict, no status block —
        and `spawn-pending` is the record that says otherwise. The stall
        layer was the one layer never taught to read it: executed, `stall`
        returned `reinvoke` over a live background reviewer, both copies
        finished, and latest-wins handed the run the STALE APPROVED.

        Checked for a PER-TASK key too (that is the proven case): the
        verdict-ledger half below never runs for one, but "the agent is
        still running" is true of any spawn."""
        self._pending("T1")
        before = json.dumps(self.st, sort_keys=True, default=str)
        with self.assertRaises(transitions.TransitionError) as ctx:
            self._guard("T1")
        msg = str(ctx.exception)
        self.assertIn("still RUNNING", msg)
        self.assertIn("SubagentStop", msg)
        self.assertIn("--confirm-no-verdict", msg)
        # refuses BEFORE record_stall, so no counter moved (same contract as
        # the verdict half)
        self.assertEqual(json.dumps(self.st, sort_keys=True, default=str),
                         before)
        # …and a pending under a DIFFERENT key is that key's business, not
        # this one's — the guard only refuses what it can attribute
        self._guard(self.KEY)

    def test_a_captured_spawn_stops_refusing(self):
        # the pending pairs off by agent id the moment SubagentStop captures
        # its reply, and the stall procedure is available again immediately
        self._pending("T1")
        with self.assertRaises(transitions.TransitionError):
            self._guard("T1")
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "spawn-captured", "agent_id": "a-1",
                              "actor": "capture", "shape": "reviewer"})
        self._guard("T1")
        self.assertEqual(
            transitions.record_stall(self.st, self.config, "T1"), "reinvoke")

    def test_a_forged_capture_cannot_unblock_the_stall(self):
        # the resolver is actor-checked in BOTH readers (gauge and guard):
        # here the forgery direction is unblocking a refusal, and the
        # agent_id is published in the very ledger `log-event` appends to
        self._pending("T1")
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "spawn-captured", "agent_id": "a-1"})
        with self.assertRaises(transitions.TransitionError):
            self._guard("T1")

    def test_the_pending_refusal_is_overridable(self):
        # for the spawn that genuinely died (session ended, CLI crashed) the
        # existing escape hatch still records the stall — and still leaves
        # the visible override marker
        state_mod.save(self.run, self.workspace, self.st)
        self._pending(self.KEY)
        blocked = self._cli("stall")
        self.assertFalse(blocked["ok"])
        self.assertIn("still RUNNING", blocked["error"])
        forced = self._cli("stall", "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(forced["action"], "reinvoke")
        override = next(e for e in ndjson.read_records(self.run / "events.ndjson")
                        if e["kind"] == "stall-verdict-override")
        self.assertIn("still RUNNING", override["reason"])

    def test_the_override_abandons_the_pending_it_overrode(self):
        """Declaring a spawn dead has to RETIRE it. Otherwise the pending
        stays open forever: this same guard keeps refusing the next stall,
        the spawn guard's one-live-spawn rule keeps refusing the reinvoke the
        stall just prescribed, and no verb exists to clear either."""
        state_mod.save(self.run, self.workspace, self.st)
        self._pending(self.KEY)
        self._cli("stall", "--confirm-no-verdict")
        gone = next(e for e in ndjson.read_records(self.run / "events.ndjson")
                    if e["kind"] == "spawn-abandoned")
        self.assertEqual((gone["agent_id"], gone["task"], gone["actor"]),
                         ("a-1", self.KEY, "stall"))
        # the pending is closed for every reader of the pairing…
        self.assertEqual(
            transitions.open_spawn_pendings(self.run, self.KEY,
                                            self.manifest), [])
        # …so a LATER genuine stall on this key is no longer refused as
        # "a spawn is still running"
        self.st = state_mod.load(self.run, self.workspace)
        self._guard()

    def _task_less(self, mode, agent_id):
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "spawn-pending", "task": None,
                              "actor": "reviewer", "mode": mode,
                              "agent_id": agent_id})

    def _abandoned(self):
        return sorted(e["agent_id"] for e in
                      ndjson.read_records(self.run / "events.ndjson")
                      if e["kind"] == "spawn-abandoned")

    def test_a_task_less_pending_matches_the_step_key_it_stalls_under(self):
        """The two spellings of one spawn. A task-less spawn (plan-review,
        pre-pr, a panel lens) carries no `harness-task` header, so capture
        records its pending with task=None — while its stall is counted per
        STEP. Equality alone missed that pairing completely: the plan-review
        synthesizer, the field case this guard was written for, could be live
        in the background and still get `reinvoke`. With backgrounding legal
        the miss compounds — the spawn guard refuses the re-spawn as already
        in flight, and the override that frees it never fires because nothing
        refused: a wedged run with no verb able to move it."""
        state_mod.save(self.run, self.workspace, self.st)
        self._task_less("plan-review", "a-taskless")
        with self.assertRaises(transitions.TransitionError) as ctx:
            self._guard()                       # step:plan-review
        self.assertIn("a-taskless", str(ctx.exception))
        # A PER-TASK key never sees it — asserted HERE, while the pending is
        # still OPEN, because after the abandonment below every key answers
        # [] and the pin proves nothing (it sat below and was vacuous:
        # dropping the guard that keeps task keys out of the task-less
        # predicate left every test green).
        self.assertEqual(
            transitions.open_spawn_pendings(self.run, "T1", self.manifest), [])
        # …and the escape hatch reaches it, so the key can be re-spawned
        self._cli("stall", "--confirm-no-verdict")
        gone = next(e for e in ndjson.read_records(self.run / "events.ndjson")
                    if e["kind"] == "spawn-abandoned")
        self.assertEqual(gone["agent_id"], "a-taskless")
        self.assertEqual(
            transitions.open_spawn_pendings(self.run, self.KEY,
                                            self.manifest), [])

    def test_a_lens_override_never_retires_the_synthesizer(self):
        """Executed in adversarial review: `stall --task
        step:plan-review:<lens> --confirm-no-verdict` — the per-lens counter
        plan-review.md mandates — abandoned the LIVE synthesizer along with
        every lens, because a task-less pending matched any `step:`-prefixed
        key. The synthesizer's is the one verdict the FSM reads
        (verdict_bound.mode), so recovering one advisory lens threw away the
        round's actual work. A lens key now reaches lens modes only: the
        step's declared spawn-set minus its verdict_bound mode."""
        state_mod.save(self.run, self.workspace, self.st)
        self._task_less("plan-attack", "lens-1")
        self._task_less("plan-attack", "lens-2")
        self._task_less("plan-review", "synth")
        forced = self._cli("stall", "--task", "step:plan-review:gaps",
                           "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(self._abandoned(), ["lens-1", "lens-2"])
        # the synthesizer is untouched: still open, and still refusing a
        # stall on the step key it belongs to
        self.assertEqual(
            [e["agent_id"] for e in transitions.open_spawn_pendings(
                self.run, self.KEY, self.manifest)], ["synth"])
        self.st = state_mod.load(self.run, self.workspace)
        with self.assertRaises(transitions.TransitionError) as ctx:
            self._guard()
        self.assertIn("synth", str(ctx.exception))

    def test_another_steps_override_does_not_reach_this_steps_pendings(self):
        """`stall --confirm-no-verdict` at `step:develop` abandoned a live
        plan-review synthesizer and a live pre-pr reviewer — pendings of
        steps it has nothing to do with (executed). The key's reach is the
        modes the manifest says THAT step spawns."""
        state_mod.save(self.run, self.workspace, self.st)
        self._task_less("plan-review", "synth")
        self._task_less("pre-pr", "prepr")
        forced = self._cli("stall", "--task", "step:develop",
                           "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(self._abandoned(), [])
        self.assertEqual(
            [e["agent_id"] for e in transitions.open_spawn_pendings(
                self.run, self.KEY, self.manifest)], ["synth"])
        self.assertEqual(
            [e["agent_id"] for e in transitions.open_spawn_pendings(
                self.run, "step:pre-pr", self.manifest)], ["prepr"])

    def test_a_cross_step_ghost_refuses_nothing_but_clears_via_its_own_key(self):
        """The cascade the widening caused: ONE dangling task-less pending
        (a spawn whose stop never came, at a step the run has long left)
        falsely refused EVERY later step-keyed stall in the run — each of
        which then had to be forced with `--confirm-no-verdict`, flagging
        and degrading a run that was fine. Coherent semantics instead: the
        ghost belongs to its own step's key, refuses only there, and clears
        there."""
        state_mod.save(self.run, self.workspace, self.st)
        self._task_less("plan-review", "ghost")
        self._guard("step:develop")             # a later step: no refusal
        self._guard("step:pre-pr")
        self._guard("T1")                       # nor any per-task key
        with self.assertRaises(transitions.TransitionError):
            self._guard()                       # …only its own step's key
        forced = self._cli("stall", "--task", self.KEY,
                           "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(self._abandoned(), ["ghost"])

    def test_a_typoed_task_key_still_frees_the_pending(self):
        """G4, executed: `record_stall` raises "unknown task" for a key that
        is not in state["tasks"], and nothing ever validates a spawn
        prompt's `harness-task:` header against the task list — so ONE typo
        produced a pending under a key the counter rejects, with the
        abandonment sitting after the raise: the key stayed blocked by the
        one-live-spawn rule, health DEGRADED, and no verb could move it. The
        override's contract is "record it anyway"; the counter failing must
        not void the retirement."""
        state_mod.save(self.run, self.workspace, self.st)
        self._pending("T-TYPO", agent_id="a-typo")
        failed = self._cli("stall", "--task", "T-TYPO",
                           "--confirm-no-verdict")
        self.assertFalse(failed["ok"], failed)          # the verb still fails
        self.assertIn("unknown task", failed["error"])
        # …and the retirement happened anyway: the key is free
        self.assertEqual(self._abandoned(), ["a-typo"])
        self.assertEqual(
            transitions.open_spawn_pendings(self.run, "T-TYPO",
                                            self.manifest), [])

    def test_a_forged_abandonment_cannot_unblock_the_stall(self):
        # actor-checked in this reader too: `log-event` is unvalidated and
        # the agent_id is published in the same ledger it appends to
        self._pending("T1")
        for forged in ({"kind": "spawn-abandoned", "agent_id": "a-1"},
                       {"kind": "spawn-abandoned", "agent_id": "a-1",
                        "actor": "capture"}):
            ndjson.append_record(self.run / "events.ndjson", forged)
            with self.assertRaises(transitions.TransitionError):
                self._guard("T1")

    def test_a_genuine_stall_abandons_nothing(self):
        # the abandonment rides on the OVERRIDE, not on the flag: a stall
        # with no refusal to suppress retires no spawn (and a pending under
        # another key is never in scope for this one)
        state_mod.save(self.run, self.workspace, self.st)
        self._pending("T1")                     # a different key's spawn
        forced = self._cli("stall", "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        kinds = [e["kind"] for e in
                 ndjson.read_records(self.run / "events.ndjson")]
        self.assertNotIn("spawn-abandoned", kinds)
        self.assertEqual(
            len(transitions.open_spawn_pendings(self.run, "T1",
                                                self.manifest)), 1)

    def test_step_without_verdict_bound_is_unaffected(self):
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        self._guard("step:develop")
        # a lens sub-key resolves to no manifest step at all: also a no-op
        self._guard("step:plan-review:contradictions")

    def test_corrupt_verdict_ledger_fails_open_not_closed(self):
        # adversarial-review, both lenses: _verdict_window reads
        # reviews.ndjson with strict=True and raises on a torn line — and a
        # torn tail there is written by a capture hook killed mid-append,
        # the event most correlated with the stall being recorded. A guard
        # that SUPPRESSES must fail open, or `stall` (the only escalation
        # route to the human) dies exactly when it is needed.
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        with open(self.run / "reviews.ndjson", "a", encoding="utf-8") as fh:
            fh.write("{torn record\n")
        self._guard()
        self.assertEqual(
            transitions.record_stall(self.st, self.config, self.KEY),
            "reinvoke")

    def test_unreadable_ledger_fails_open_like_a_corrupt_one(self):
        # pre-release review: only the CORRUPTION spelling (LedgerCorruption
        # -> TransitionError) was caught, so an exists-but-unreadable ledger
        # (EACCES/EIO) failed CLOSED on exactly the wedged-run path — the
        # I/O failures most likely to accompany a wedge
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        from unittest import mock
        with mock.patch("harness.transitions.ndjson.read_records",
                        side_effect=OSError("EACCES")):
            self._guard()   # no refusal: cannot-read is not proof-of-verdict

    def test_corrupt_events_ledger_does_not_brick_the_procedure(self):
        # The anchor reads events leniently: a torn line may only LOWER the
        # anchor (-> over-refusal, escapable), never raise and wedge the one
        # path a stuck run depends on.
        ndjson.append_record(self.run / "events.ndjson",
                             {"kind": "plan-registered", "actor": "plan-register"})
        with open(self.run / "events.ndjson", "a", encoding="utf-8") as fh:
            fh.write("{torn record\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._guard()
        # …but the pending reader must SAY so: it decides "no spawn is in
        # flight" from absence, so a torn `spawn-pending` line silently
        # disables this refusal (adversarial review). Lenient AND loud.
        self.assertIn("unparseable line(s)", err.getvalue())

    def test_cli_refuses_then_honours_the_escape_hatch(self):
        state_mod.save(self.run, self.workspace, self.st)
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        blocked = self._cli("stall")
        self.assertFalse(blocked["ok"])
        self.assertIn("--confirm-no-verdict", blocked["error"])
        st = state_mod.load(self.run, self.workspace)
        self.assertNotIn(self.KEY, st.get("step_stalls", {}))

        forced = self._cli("stall", "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        self.assertEqual(forced["action"], "reinvoke")
        st = state_mod.load(self.run, self.workspace)
        self.assertEqual(st["step_stalls"][self.KEY], 1)

    def test_the_override_is_visible_in_the_ledger(self):
        """Whole-branch adversarial review: every OTHER escape hatch here is
        flagged and reviewer-visible (`verify-red --revise` -> test-revision,
        coverage-skipped, pr-recorded-manually), while this one wrote a stall
        record byte-identical to a guarded one — so nobody reviewing a
        DEGRADED run could tell a suppressed refusal from a genuine stall."""
        state_mod.save(self.run, self.workspace, self.st)
        support.seed_review_verdict(self.run, verdict="CHANGES_REQUESTED")
        self._cli("stall", "--confirm-no-verdict")
        events = ndjson.read_records(self.run / "events.ndjson")
        stall = next(e for e in events if e["kind"] == "stall")
        self.assertEqual(stall.get("override"), "confirm-no-verdict")
        override = next(e for e in events
                        if e["kind"] == "stall-verdict-override")
        self.assertEqual(override["task"], self.KEY)
        self.assertIn("CHANGES_REQUESTED", override["reason"])
        # …and it counts on the same gauge the other escape hatches do
        from harness.workflow import FLAGGED_EVENT_KINDS
        self.assertIn("stall-verdict-override", FLAGGED_EVENT_KINDS)

    def test_a_malformed_ledger_cannot_break_the_escape_hatch(self):
        """Re-verification finding: before the visibility marker, the flag
        SKIPPED the guard entirely, so nothing the guard could do was able to
        break the escape hatch. Running it for the marker handed it that
        power back — a record with a non-string `at` makes _verdict_window's
        comparison raise TypeError, which took `stall --confirm-no-verdict`
        out of the JSON error contract on the one path a wedged run depends
        on. The flag's contract is 'record it anyway'."""
        state_mod.save(self.run, self.workspace, self.st)
        ndjson.append_record(self.run / "reviews.ndjson",
                             {"task": None, "mode": "plan-review",
                              "verdict": "CHANGES_REQUESTED", "at": 12345})
        forced = self._cli("stall", "--confirm-no-verdict")
        self.assertTrue(forced.get("ok"), forced)     # never an empty stdout
        self.assertEqual(forced["action"], "reinvoke")
        st = state_mod.load(self.run, self.workspace)
        self.assertEqual(st["step_stalls"][self.KEY], 1)

    def test_the_flag_on_a_genuine_stall_records_nothing_extra(self):
        # the marker means "a refusal was suppressed", not "the flag was
        # passed" — an orchestrator that always passes it must not paint
        # every genuine stall as an override
        state_mod.save(self.run, self.workspace, self.st)
        forced = self._cli("stall", "--confirm-no-verdict")
        self.assertTrue(forced["ok"], forced)
        events = ndjson.read_records(self.run / "events.ndjson")
        stall = next(e for e in events if e["kind"] == "stall")
        self.assertNotIn("override", stall)
        self.assertNotIn("stall-verdict-override",
                         [e["kind"] for e in events])


if __name__ == "__main__":
    unittest.main()
