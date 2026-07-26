"""M5 done-criteria: full + quick manifests walk end-to-end on local-markdown;
quick->full escalation fires on a seeded auth-touching diff; a two-repo story
completes with contract reconciliation surfaced; worktree lifecycle."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import gitops, ndjson, state as state_mod
from tests.test_gitops import FAILING_TEST, TEST_CMD, make_repo
from tests import support

ROOT = Path(__file__).resolve().parent.parent


class BreadthHarness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.stories = self.workspace / "stories"
        self.stories.mkdir()
        self.repo = make_repo(self.workspace)

    def tearDown(self):
        support.rmtree(self.workspace)

    def story(self, sid, title, body="", type_="Bug"):
        (self.stories / f"{sid}.md").write_text(
            f"# {sid}: {title}\nType: {type_}\nStatus: Open\n\n"
            f"## Description\n{body}\n\n## Acceptance Criteria\n- [ ] works\n")

    def cli(self, *args, run=None, expect=0):
        cmd = [sys.executable, "-m", "harness", "--workspace", str(self.workspace)]
        if run:
            cmd += ["--run", str(run)]
        proc = subprocess.run([*cmd, *args], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"harness {' '.join(map(str, args))} -> {payload} {proc.stderr}")
        return payload

    def init(self, extra_repos="", extra_test_cmd=""):
        args = ["--stories-dir", str(self.stories),
                "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}"]
        if extra_repos:
            args += ["--repo", extra_repos]
        if extra_test_cmd:
            args += ["--test-cmd", extra_test_cmd]
        self.cli("init", *args)

    def gate(self, run, gate_id, reply="APPROVED", options=None):
        # `options` is legal only for select gates, and only at --present
        # (the candidate list is sealed into state there); binary gates take
        # theirs from the manifest's declared dispositions — never a caller
        # flag at --decide (the gate-options guarantee-seam fix).
        present = ["gate", "--id", gate_id, "--present"]
        if options:
            present += ["--options", options]
        self.cli(*present, run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": reply})
        self.cli("gate", "--id", gate_id, "--decide", run=run)

    def _force_tasks_done(self, run):
        """Test-only shortcut for tests exercising something OTHER than task
        completion itself (e.g. security-scan aggregation) that still need
        to cross the develop step's requires_tasks_terminal sync point —
        bypasses the real TDD completion path on purpose."""
        st = state_mod.load(run, self.workspace)
        for t in st["tasks"]:
            t["status"] = "done"
        state_mod.save(run, self.workspace, st)

    def tdd_task(self, run, task_id, worktree: Path):
        self.cli("task", "--id", task_id, "--to", "in-progress", run=run)
        (worktree / "tests" / "test_x.py").write_text(FAILING_TEST)
        self.cli("verify-red", "--repo", str(worktree), "--task", task_id,
                 "--test-cmd", TEST_CMD, "--intents", "test_val", run=run)
        (worktree / "x.py").write_text("def val():\n    return 1\n")
        self.cli("commit", "--repo", str(worktree), "--task-id", task_id,
                 "--summary", "implement val", run=run)
        self.cli("task", "--id", task_id, "--to", "in-review",
                 "--repo", str(worktree), "--test-cmd", TEST_CMD, run=run)
        self.review_approve(run, task_id)

    def review_approve(self, run, task_id, verdict="APPROVED"):
        """Simulate the SubagentStop hook's reviewer-verdict capture — the
        `reviewer-approved` guard on in-review -> done reads this ledger
        (in production only the hook writes it; AUTHORITY_RE blocks
        direct writes from agent tool calls)."""
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": task_id, "mode": "review",
                              "verdict": verdict})

    def scope(self, run, *repos):
        """Record the user-confirmed target-repo scope (intake's job in
        production) — plan-register refuses tasks without/outside it."""
        self.cli("scope-register", "--repos-json",
                 json.dumps([str(r) for r in (repos or (self.repo,))]),
                 run=run)

    def pass_plan_review(self, run, verdict="APPROVED"):
        """Cross plan-review: enter it, then seed the task-less hook-captured
        verdict its verdict_bound exits derive from."""
        self.cli("cursor", "--to", "plan-review", run=run)
        support.seed_review_verdict(run, verdict=verdict)

    def _force_cursor(self, run, step):
        """Test-only shortcut for tests exercising something OTHER than
        cursor legality itself (gate option machinery, deferral plumbing)
        that still need the cursor AT a given step — `gate --decide` is
        cursor-anchored, and walking the whole pipeline would drown those
        tests in unrelated setup."""
        st = state_mod.load(run, self.workspace)
        st["cursor"]["current_step"] = step
        state_mod.save(run, self.workspace, st)


class FullWalk(BreadthHarness):
    def test_full_manifest_end_to_end(self):
        self.story("W-10", "Fix parser crash")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-10", "--date", "2026-02-01")["run"])

        self.cli("cursor", "--to", "intake", run=run)
        self.scope(run)
        self.cli("cursor", "--to", "plan", run=run)
        (run / "plan.md").write_text("# Plan\n## T1\n")
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.gate(run, "approve-plan")   # decided AT the gate (cursor-anchored)
        self.cli("cursor", "--to", "preflight", run=run)
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.cli("cursor", "--to", "develop", run=run)

        # worktree lane (M5 charter): create, work, merge, remove
        wt = self.cli("worktree-add", "--repo", str(self.repo),
                      "--task-id", "T1", "--base", branch, run=run)
        worktree = Path(wt["path"])
        self.assertTrue(worktree.is_dir())
        resumed = self.cli("worktree-add", "--repo", str(self.repo),
                           "--task-id", "T1", "--base", branch, run=run)
        self.assertTrue(resumed["resumed"])           # idempotent resume
        self.assertEqual(resumed["path"], wt["path"])

        self.tdd_task(run, "T1", worktree)
        gitops.run_git(self.repo, "checkout", branch)
        self.cli("merge-task", "--repo", str(self.repo), "--task-id", "T1",
                 "--task-branch", wt["branch"], "--summary", "fix crash", run=run)
        self.cli("task", "--id", "T1", "--to", "done", run=run)
        self.cli("worktree-remove", "--repo", str(self.repo), "--task-id", "T1",
                 run=run)
        self.assertFalse(worktree.exists())

        self.cli("cursor", "--to", "approve-impl", run=run)
        self.gate(run, "approve-impl")
        self.cli("cursor", "--to", "harden", run=run)
        self.cli("cursor", "--to", "security", run=run)
        sev = self.cli("security-scan", run=run)
        self.assertEqual(sev["max_severity"], "info")   # no scanner configured
        self.cli("cursor", "--to", "pre-pr", run=run)   # gate skipped (info<medium)

        self.cli("reconcile-contracts", run=run)        # no contracts -> clean
        (run / "reports").mkdir(exist_ok=True)
        (run / "reports" / "pre-pr.md").write_text("# Pre-PR\nAll good.\n")
        self.cli("cursor", "--to", "approve-pre-pr", run=run)
        self.gate(run, "approve-pre-pr")
        self.cli("cursor", "--to", "create-pr", run=run)
        pr = self.cli("create-pr", "--repo", str(self.repo), run=run)
        self.assertEqual(pr["title"], "fix: #W-10 Fix parser crash")

        self.cli("cursor", "--to", "reconcile", run=run)
        self.cli("reconcile", run=run)
        # provider write-back (conservative default: on_done)
        self.assertIn("Status: Done", (self.stories / "W-10.md").read_text(encoding="utf-8"))
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["tasks"][0]["status"], "archived")

        self.cli("cursor", "--to", "metrics", run=run)
        # two same-key token records (the report aggregates per task×role,
        # not per invocation) — what the SubagentStop hook writes in real runs
        for out in (20, 30):
            ndjson.append_record(run / "tokens.ndjson", {
                "task": "T1", "mode": "develop", "role": "developer",
                "model": "m1", "input": 10, "output": out,
                "cache_read": 0, "cache_write": 0})
        report = Path(self.cli("metrics", run=run)["report"])
        text = report.read_text(encoding="utf-8")
        self.assertIn("## Step timings", text)
        # human-view tables (regenerable projection of the ledgers): the
        # task row, the review verdict rows, aggregated tokens + totals
        self.assertRegex(text, r"\| T1 \|.*\| archived \|")
        self.assertIn("## Review verdicts", text)
        self.assertRegex(text, r"\| T1 \| review \| APPROVED \|")
        self.assertRegex(text, r"\| T1 \| developer \| m1 \| 2 \| 20 \| 50 \|")
        self.assertIn("| **Total** |", text)
        self.cli("verify", run=run)                     # chain intact end-to-end

        # the security->pre-pr move above skipped approve-security by its
        # declared predicate — that evaluation is now ledgered (e2e E2E-1:
        # the silent self-skip was indistinguishable from an FSM hole), and
        # status + metrics count flagged events off ONE shared list (they
        # used to drift: 18 vs 23 on the same run)
        events = [json.loads(line) for line in
                  (run / "events.ndjson").read_text(encoding="utf-8").splitlines()]
        skips = [e for e in events if e["kind"] == "gate-skipped"]
        self.assertEqual([e["step"] for e in skips], ["approve-security"])
        self.assertIn("gate-skipped", text)
        status = self.cli("status")["runs"][0]
        reported = int(text.split("## Flagged events (")[1].split(")")[0])
        self.assertEqual(status["flagged_events"], reported)

        # close the run — the successful sibling of abort (e2e E2E-1: a
        # finished run used to park at the final step as 'live' forever)
        self.cli("complete", run=run)
        status = self.cli("status")["runs"][0]
        self.assertTrue(status["completed"]["at"])
        out = self.cli("cursor", "--to", "metrics", run=run, expect=1)
        self.assertIn("completed run", out["error"])


class ShowTypoRun(BreadthHarness):
    def test_show_on_a_typoed_run_path_creates_no_stray_directory(self):
        # adversarial-review finding: `show`/`verify` routed through the
        # generic locked() block, whose unconditional run.mkdir() ran
        # BEFORE load() had a chance to refuse a nonexistent run — a
        # typo'd --run path left a stray empty directory (just a
        # .state.lock file in it) in ai/ instead of a clean error.
        bogus = self.workspace / "ai" / "2026-01-01-TYPO"
        self.assertFalse(bogus.exists())
        out = self.cli("show", run=bogus, expect=1)
        self.assertFalse(out["ok"])
        self.assertFalse(bogus.exists(),
                         "show on a nonexistent run must not create it")


class WriteBackMilestones(BreadthHarness):
    def test_develop_start_writes_back_in_progress(self):
        self.story("W-51", "thing", type_="Bug")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-51", "--date", "2026-02-12")["run"])
        out = self.cli("write-back", "--milestone", "develop_start", run=run)
        self.assertEqual(out["written"], True)
        self.assertEqual(out["to"], "In Progress")
        self.assertIn("Status: In Progress", (self.stories / "W-51.md").read_text(encoding="utf-8"))

    def test_in_review_is_a_noop_by_shipped_default(self):
        self.story("W-52", "thing", type_="Bug")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-52", "--date", "2026-02-13")["run"])
        out = self.cli("write-back", "--milestone", "in_review", run=run)
        self.assertEqual(out["written"], False)
        self.assertNotIn("Status: In Review", (self.stories / "W-52.md").read_text(encoding="utf-8"))


class FetchCollisionPreservesWorkItemJson(BreadthHarness):
    def test_same_day_refetch_collision_does_not_clobber_work_item_json(self):
        # adversarial-review round 2 finding: writing work-item.json BEFORE
        # bootstrap() (the round-1 crash-recovery fix) did so
        # UNCONDITIONALLY — a same-day re-fetch of a work item that already
        # has a live run overwrote the EXISTING run's work-item.json with
        # the new fetch's content before bootstrap's own collision check
        # ever raised, permanently mismatching it against the original
        # run's state.yaml/tasks/plan even though the collision was
        # (correctly) refused right after.
        self.story("W-70", "Original title")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-70", "--date", "2026-02-17")["run"])
        original = json.loads((run / "work-item.json").read_text(encoding="utf-8"))
        self.assertEqual(original["title"], "Original title")

        self.story("W-70", "Changed title")   # source ticket edited
        out = self.cli("fetch", "--id", "W-70", "--date", "2026-02-17", expect=1)
        self.assertIn("Resume or Abort", out["error"])

        after = json.loads((run / "work-item.json").read_text(encoding="utf-8"))
        self.assertEqual(after["title"], "Original title")   # untouched


class ResealRecovery(BreadthHarness):
    """`harness reseal` — human-invoked recovery when state.yaml's seal is
    missing/unreadable (adversarial-review finding: chain.seal's content
    and seal writes are two separate atomic ops; a crash between them
    bricked the run with no recovery verb at all)."""

    def test_reseal_on_a_typoed_run_creates_no_stray_directory(self):
        # re-review finding: the brand-new reseal verb reintroduced the
        # exact stray-directory bug class this same commit fixed for
        # show/verify/status — state_mod.locked()'s unconditional mkdir ran
        # before chain.reseal got the chance to refuse the missing file.
        self.init()
        bogus = self.workspace / "ai" / "2026-01-01-TYPO"
        self.assertFalse(bogus.exists())
        out = self.cli("reseal", "--reason", "oops", run=bogus, expect=1)
        self.assertIn("nothing to reseal", out["error"])
        self.assertFalse(bogus.exists(),
                         "reseal on a nonexistent run must not create it")

    def test_reseal_recovers_a_run_whose_seal_is_missing(self):
        self.story("W-50", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-50", "--date", "2026-02-11")["run"])
        seal_file = run / "state.yaml.hmac"
        seal_file.unlink()
        self.cli("show", run=run, expect=3)   # integrity violation, blocked (3; 2 is argparse usage)
        out = self.cli("reseal", "--reason", "crash during set-state", run=run)
        self.assertEqual(out["seq"], 0)
        state = self.cli("show", run=run)["state"]   # verifies clean again
        self.assertEqual(state["work_item"]["id"], "W-50")
        events = ndjson.read_records(run / "events.ndjson")
        reseal_events = [e for e in events if e.get("kind") == "reseal"]
        self.assertEqual(len(reseal_events), 1)
        self.assertEqual(reseal_events[0]["reason"], "crash during set-state")


class WorktreeDeadPathResume(BreadthHarness):
    def test_worktree_add_recreates_when_the_recorded_path_was_deleted(self):
        # adversarial-review finding: a worktree deleted on disk (manual
        # cleanup, disk-space script, crash) while still recorded in state
        # used to "resume" straight to a dead path with no existence check
        # at all.
        self.story("W-60", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-60", "--date", "2026-02-16")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.gate(run, "approve-plan")   # decided AT the gate (cursor-anchored)
        self.cli("cursor", "--to", "preflight", run=run)
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.cli("cursor", "--to", "develop", run=run)

        wt = self.cli("worktree-add", "--repo", str(self.repo),
                      "--task-id", "T1", "--base", branch, run=run)
        support.rmtree(wt["path"])   # simulate manual cleanup / crash

        resumed = self.cli("worktree-add", "--repo", str(self.repo),
                           "--task-id", "T1", "--base", branch, run=run)
        self.assertFalse(resumed["resumed"])
        self.assertTrue(Path(resumed["path"]).is_dir())
        self.assertNotEqual(resumed["path"], wt["path"])


class PreflightDefaultBranch(BreadthHarness):
    """preflight now shares gitops.ensure_default_branch (the same
    precondition discover() uses) so the feature branch is always cut from
    a known-clean default branch, never from whatever branch/dirty state
    the repo was left on."""

    def _to_preflight(self, sid):
        self.story(sid, "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", sid, "--date", "2026-02-10")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        (run / "plan.md").write_text("# Plan\n## T1\n")
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.gate(run, "approve-plan")   # decided AT the gate (cursor-anchored)
        self.cli("cursor", "--to", "preflight", run=run)
        return run

    def test_switches_to_default_branch_before_cutting_feature_branch(self):
        run = self._to_preflight("W-40")
        gitops.run_git(self.repo, "checkout", "-b", "stray")
        (self.repo / "stray.txt").write_text("only on stray\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "stray-only commit")
        self.cli("preflight", "--repo", str(self.repo), run=run)
        # cut from main, not from the stray branch left checked out
        subjects = gitops.run_git(self.repo, "log", "--format=%s",
                                  "main..HEAD").splitlines()
        self.assertEqual(subjects, [])
        self.assertFalse((self.repo / "stray.txt").exists())

    def test_refuses_when_repo_is_dirty(self):
        run = self._to_preflight("W-41")
        (self.repo / "uncommitted.txt").write_text("oops\n")
        out = self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        self.assertIn("uncommitted", out["error"])
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_retry_after_success_is_idempotent_not_relocating(self):
        """Regression: retrying preflight (a supported crash/resume path)
        used to see the already-correct feature-branch checkout as "clean,
        off-target" and switch it back to default before failing on
        checkout -b's "already exists" — silently relocating an
        already-correct checkout. Must now return the recorded branch
        directly, untouched."""
        run = self._to_preflight("W-42")
        first = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), first)
        second = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(second, first)
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), first)


class SaveReport(BreadthHarness):
    """field: dual-run comparison — lens reports for two whole rounds do not
    exist on disk in one run: read-only lens agents returned them in-reply
    and the orchestrator hand-copied them ~3 times before the practice
    lapsed. Persistence was PROSE ("the orchestrator persists it"), the
    trust-the-prose shape the harness refuses everywhere else."""

    def setUp(self):
        super().setUp()
        self.story("W-97", "reports")
        self.init()
        self.run_dir = Path(
            self.cli("fetch", "--id", "W-97", "--date", "2026-02-13")["run"])

    def _save(self, *args, body="# Findings\n\n[R1] CRITICAL thing\n", expect=0):
        cmd = [sys.executable, "-m", "harness", "--workspace",
               str(self.workspace), "--run", str(self.run_dir),
               "save-report", *args]
        proc = subprocess.run(cmd, cwd=ROOT, input=body, capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def test_body_file_carries_prose_the_command_line_cannot(self):
        """re-verify finding (blocking): the documented `printf … | save-report`
        spelling is unusable on the reports it was built for — a report with
        an apostrophe breaks the shell quoting, and one quoting a run-authority
        path or a `>` blockquote is BLOCKED by the harness's own bash guard.
        The fallback would have been the hand-copying this verb replaced."""
        body = ("> the plan's own note: see ai/<run>/events.ndjson\n"
                "[R1] CRITICAL doesn't hold\n")
        scratch = self.workspace / "body.md"
        scratch.write_text(body, encoding="utf-8")
        out = self._save("--mode", "pre-pr", "--body-file", str(scratch),
                         body="")   # stdin unused
        self.assertEqual(
            (self.run_dir / out["path"]).read_text(encoding="utf-8"), body)

    def test_round_is_derived_from_the_plan_generation(self):
        # re-verify finding: an optional, hand-tracked round number is
        # skippable and mis-typeable, so "prior rounds stay recoverable" was
        # still a promise resting on prose
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "plan-registered", "actor": "plan-register"})
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "plan-registered", "actor": "plan-register"})
        out = self._save("--mode", "plan-review")
        self.assertEqual(out["round"], 2)
        self.assertIn("reports/plan-review-r2.md", out["paths"])

    def test_rewriting_a_round_snapshot_with_different_content_refuses(self):
        self._save("--mode", "plan-review", "--round", "1", body="one\n")
        self._save("--mode", "plan-review", "--round", "1", body="one\n")  # idempotent
        out = self._save("--mode", "plan-review", "--round", "1",
                         body="rewritten\n", expect=1)
        self.assertIn("immutable", out["error"])

    def test_typoed_run_refuses_instead_of_manufacturing_a_phantom(self):
        # pre-release review: save-report was the one run-scoped verb that
        # never checked the run exists — mkdir(parents=True) built the whole
        # phantom path and reported success while the real run's gate later
        # presented a missing report
        ghost = self.workspace / "ai" / "2026-02-13-TYPO-1"
        proc = subprocess.run(
            [sys.executable, "-m", "harness", "--workspace",
             str(self.workspace), "--run", str(ghost), "save-report",
             "--mode", "pre-pr"],
            cwd=ROOT, input="body\n", capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("not a run", json.loads(proc.stdout)["error"])
        self.assertFalse(ghost.exists())

    def test_recorded_stall_reopens_the_round_snapshot(self):
        """pre-release review (reproduced): synthesis saved, verdict never
        captured, stall recorded, spawn re-invoked — the re-invoked reply
        must be saveable in the SAME round, or the live path keeps the
        pre-stall synthesis while reviews.ndjson holds the re-invoked
        verdict: a report/verdict mismatch at the human gate. A recorded
        stall for the mode's step reopens the snapshot; without one the
        immutability refusal stands (previous test)."""
        self._save("--mode", "plan-review", body="pre-stall synthesis\n")
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "stall", "task": "step:plan-review",
                              "action": "reinvoke"})
        out = self._save("--mode", "plan-review", body="re-invoked synthesis\n")
        self.assertEqual(out["round"], 1)   # same round, superseded
        self.assertEqual(
            (self.run_dir / "reports/plan-review.md").read_text(),
            "re-invoked synthesis\n")
        # a lens stall (finer key, same step prefix) reopens its lens too
        self._save("--mode", "plan-attack", "--lens", "gaps", body="a\n")
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "stall", "task": "step:plan-review:gaps",
                              "action": "reinvoke"})
        self._save("--mode", "plan-attack", "--lens", "gaps", body="b\n")

    def test_pre_pr_round_advances_on_gate_rejection_not_plan_generation(self):
        # pre-release review (reproduced): pre-pr rounds are gate-rejection-
        # driven; anchoring them to the plan generation made every second
        # pre-pr save refuse with a plan-review-flavored message
        first = self._save("--mode", "pre-pr", body="round one\n")
        self.assertEqual(first["round"], 1)
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "gate-decision", "gate": "approve-pre-pr",
                              "decision": "rejected"})
        second = self._save("--mode", "pre-pr", body="round two\n")
        self.assertEqual(second["round"], 2)
        self.assertEqual(
            (self.run_dir / "reports/pre-pr-r1.md").read_text(), "round one\n")

    def test_forged_plan_registered_does_not_move_the_round(self):
        # actor-checked, the same anti-forgery stance outstanding_flagged
        # takes for exactly this kind
        ndjson.append_record(self.run_dir / "events.ndjson",
                             {"kind": "plan-registered"})   # no actor: forged
        out = self._save("--mode", "plan-review")
        self.assertEqual(out["round"], 1)

    def test_writes_live_path_and_round_snapshot_in_one_call(self):
        out = self._save("--mode", "plan-attack", "--lens", "contradictions",
                         "--round", "2")
        self.assertEqual(out["paths"],
                         ["reports/plan-attack-contradictions.md",
                          "reports/plan-attack-contradictions-r2.md"])
        for p in out["paths"]:
            self.assertIn("[R1] CRITICAL", (self.run_dir / p).read_text())
        kinds = [e.get("kind")
                 for e in ndjson.read_records(self.run_dir / "events.ndjson")]
        self.assertIn("report-saved", kinds)

    def test_live_path_always_holds_the_latest_round(self):
        self._save("--mode", "plan-review", "--round", "1", body="round one\n")
        self._save("--mode", "plan-review", "--round", "2", body="round two\n")
        self.assertEqual(
            (self.run_dir / "reports/plan-review.md").read_text(), "round two\n")
        # …and the earlier round stays recoverable, which is what the
        # hand-rolled `-r<n>` convention was reaching for
        self.assertEqual(
            (self.run_dir / "reports/plan-review-r1.md").read_text(), "round one\n")

    def test_lens_modes_require_a_safe_lens_slug(self):
        self.assertIn("needs --lens",
                      self._save("--mode", "plan-attack", expect=1)["error"])
        for bad in ("../../etc/passwd", "a/b", "Up Case"):
            out = self._save("--mode", "plan-attack", "--lens", bad, expect=1)
            self.assertIn("lowercase slug", out["error"])

    def test_empty_report_and_bad_mode_refuse(self):
        self.assertIn("empty report",
                      self._save("--mode", "pre-pr", body="   \n",
                                 expect=1)["error"])
        self.assertIn("unknown report mode",
                      self._save("--mode", "nonsense", expect=1)["error"])


class EnvPrerequisites(BreadthHarness):
    """field: dual-run comparison — Docker was down in both runs. One never
    executed its Testcontainers integration test at all and shipped that path
    verified by code review only; the other lost ~1h38m mid-develop stopping
    to ask, starting Docker, and re-running. The requirement was knowable at
    plan time in both cases and nothing asked, so `env-check` probes what the
    plan declared BEFORE the developer spawn."""

    def setUp(self):
        super().setUp()
        self.story("W-96", "needs docker")
        self.init()
        self.run_dir = Path(
            self.cli("fetch", "--id", "W-96", "--date", "2026-02-12")["run"])
        self.cli("cursor", "--to", "intake", run=self.run_dir)
        self.cli("cursor", "--to", "plan", run=self.run_dir)
        self.scope(self.run_dir)

    def _declare(self, probe, name="docker", hint="start it"):
        """Point the shipped `docker` requirement at a probe we control —
        the config layer is the declared data, so no real daemon is needed."""
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"env_requirements":
                             {name: {"probe": probe, "hint": hint}}}))

    def _register(self, requires=("docker",)):
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo),
                              "env_requires": list(requires)}]),
                 run=self.run_dir)

    def test_present_requirement_passes(self):
        self._declare(support.NOP_CMD)
        self._register()
        out = self.cli("env-check", run=self.run_dir)
        self.assertTrue(out["ok"])
        self.assertEqual([c["name"] for c in out["checked"]], ["docker"])
        self.assertTrue(out["checked"][0]["present"])
        self.assertEqual(out["missing"], [])

    def test_missing_requirement_refuses_with_the_declared_hint(self):
        self._declare("exit 7", hint="start Docker Desktop and re-run")
        self._register()
        out = self.cli("env-check", run=self.run_dir, expect=1)
        self.assertFalse(out["ok"])
        self.assertEqual([m["name"] for m in out["missing"]], ["docker"])
        self.assertIn("Docker Desktop", out["missing"][0]["hint"])
        kinds = [e.get("kind")
                 for e in ndjson.read_records(self.run_dir / "events.ndjson")]
        self.assertIn("env-prereq-missing", kinds)

    def test_nothing_declared_is_a_clean_pass(self):
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=self.run_dir)
        out = self.cli("env-check", run=self.run_dir)
        self.assertTrue(out["ok"])
        self.assertEqual(out["checked"], [])

    def test_requirement_declared_without_a_probe_counts_as_missing(self):
        # plan-register validates the NAME against the config map; a name
        # whose entry carries no `probe` still registers. "cannot check"
        # must never render as "checked".
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"env_requirements": {"emulator": {"hint": "x"}}}))
        self._register(("emulator",))
        out = self.cli("env-check", run=self.run_dir, expect=1)
        self.assertEqual(out["missing"][0]["detail"], "unprobeable")
        self.assertIn("no `probe` declared", out["missing"][0]["hint"])

    def test_task_scoped_clean_probe_does_not_clear_other_flags(self):
        """pre-release review, both lenses independently (reproduced): T1
        needs docker (up), T2 needs emulator (down, flagged). The natural
        human move after fixing docker — `env-check --task T1` — used to
        append `env-prereq-satisfied`, and outstanding_flagged clears every
        open miss on one satisfied event, so the emulator's flag left the
        dashboard while the wall was still there. Only the run-wide check
        the develop step documents may clear."""
        self._declare(support.NOP_CMD, name="docker")
        self._declare("exit 1", name="emulator")
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo),
                              "env_requires": ["docker"]},
                             {"id": "T2", "repo": str(self.repo),
                              "env_requires": ["emulator"]}]),
                 run=self.run_dir)
        self.cli("env-check", run=self.run_dir, expect=1)     # emulator down
        flagged = next(r for r in self.cli("status")["runs"]
                       if r["run"] == self.run_dir.name)["flagged_events"]
        self.cli("env-check", "--task", "T1", run=self.run_dir)  # docker fine
        still = next(r for r in self.cli("status")["runs"]
                     if r["run"] == self.run_dir.name)["flagged_events"]
        self.assertEqual(still, flagged)     # emulator's flag survives
        self._declare(support.NOP_CMD, name="emulator")
        self.cli("env-check", run=self.run_dir)              # run-wide clean
        cleared = next(r for r in self.cli("status")["runs"]
                       if r["run"] == self.run_dir.name)["flagged_events"]
        self.assertEqual(cleared, flagged - 1)

    def test_revision_that_drops_the_requirement_clears_the_flag(self):
        # a plan revision may remove env_requires entirely: nothing left to
        # probe still RESOLVES the outstanding miss (pre-release review)
        self._declare("exit 1")
        self._register()
        self.cli("env-check", run=self.run_dir, expect=1)
        st = state_mod.load(self.run_dir, self.workspace)
        st["tasks"][0]["env_requires"] = []
        state_mod.save(self.run_dir, self.workspace, st)
        out = self.cli("env-check", run=self.run_dir)
        self.assertTrue(out["ok"])
        kinds = [e.get("kind")
                 for e in ndjson.read_records(self.run_dir / "events.ndjson")]
        self.assertIn("env-prereq-satisfied", kinds)

    def test_satisfying_the_requirement_clears_the_flag(self):
        # re-verify finding: unlike its sibling kinds this one is genuinely
        # RESOLVABLE (the human starts the service and re-runs), so a
        # permanent flag would leave every such run reading DEGRADED forever
        self._declare("exit 1")
        self._register()
        self.cli("env-check", run=self.run_dir, expect=1)
        flagged = next(r for r in self.cli("status")["runs"]
                       if r["run"] == self.run_dir.name)["flagged_events"]
        self.assertGreaterEqual(flagged, 1)

        self._declare(support.NOP_CMD)          # the human fixed it
        self.assertTrue(self.cli("env-check", run=self.run_dir)["ok"])
        cleared = next(r for r in self.cli("status")["runs"]
                       if r["run"] == self.run_dir.name)["flagged_events"]
        self.assertEqual(cleared, flagged - 1)

    def test_reports_every_missing_requirement_not_just_the_first(self):
        # one round-trip must tell the human everything to fix
        self._declare("exit 1", name="docker")
        self._declare("exit 1", name="emulator")
        self._register(("docker", "emulator"))
        out = self.cli("env-check", run=self.run_dir, expect=1)
        self.assertEqual(sorted(m["name"] for m in out["missing"]),
                         ["docker", "emulator"])

    def test_done_tasks_are_out_of_scope_but_task_flag_still_reaches_them(self):
        self._declare("exit 1")
        self._register()
        self._force_tasks_done(self.run_dir)
        self.assertTrue(self.cli("env-check", run=self.run_dir)["ok"])
        out = self.cli("env-check", "--task", "T1", run=self.run_dir, expect=1)
        self.assertEqual([m["name"] for m in out["missing"]], ["docker"])


class PreflightPriorWork(PreflightDefaultBranch):
    """field: dual-run comparison — the deterministic branch template
    makes a same-item RERUN collide by construction, but `_branch_exists`
    only ever looked at local refs. A fresh clone therefore sailed through
    preflight and hit the collision hours later, at push, as a
    non-fast-forward — five tasks of work already committed and a prior
    run's MR already open on the name in two repos."""

    def _add_bare_origin(self) -> Path:
        bare = self.workspace / "origin.git"
        gitops.run_git(self.workspace, "init", "--bare", str(bare))
        gitops.run_git(self.repo, "remote", "add", "origin", str(bare))
        gitops.run_git(self.repo, "push", "origin", "main")
        return bare

    def _expected_branch(self, run, suffix=None) -> str:
        from harness import workflow
        from harness.cli import load_declared
        _m, _f, config = load_declared(self.workspace)
        return workflow._render_feature_branch(
            config, state_mod.load(run, self.workspace), suffix)

    def _kinds(self, run) -> list[str]:
        return [e.get("kind")
                for e in ndjson.read_records(run / "events.ndjson")]

    def test_remote_branch_collision_refuses_without_side_effects(self):
        run = self._to_preflight("W-45")
        bare = self._add_bare_origin()
        branch = self._expected_branch(run)
        gitops.run_git(bare, "branch", branch, "main")   # the prior run's branch
        out = self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        self.assertIn("already exists on the remote", out["error"])
        self.assertIn("--feature-branch-suffix", out["error"])
        # nothing was cut locally and the checkout never moved
        self.assertFalse(gitops._branch_exists(self.repo, branch))
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        st = state_mod.load(run, self.workspace)
        self.assertNotIn("branches", st.get("artifacts") or {})
        self.assertIn("remote-branch-exists", self._kinds(run))

    def test_suffix_is_the_declared_branch_aside_remedy(self):
        run = self._to_preflight("W-46")
        bare = self._add_bare_origin()
        gitops.run_git(bare, "branch", self._expected_branch(run), "main")
        out = self.cli("preflight", "--repo", str(self.repo),
                       "--feature-branch-suffix", "rerun", run=run)
        self.assertEqual(out["branch"], self._expected_branch(run, "rerun"))
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"),
            out["branch"])

    def test_clean_remote_proceeds_unchanged(self):
        run = self._to_preflight("W-47")
        self._add_bare_origin()
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        # probe answered "absent" — no flag of either kind
        kinds = self._kinds(run)
        self.assertNotIn("remote-branch-exists", kinds)
        self.assertNotIn("remote-branch-unverified", kinds)

    def test_no_remote_configured_skips_the_probe_silently(self):
        # make_repo has no remote. That is STRUCTURAL — there is no collision
        # to have — so it must not put a permanent unresolvable flag on every
        # preflight in every local-only workspace (adversarial-review).
        run = self._to_preflight("W-48")
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertNotIn("remote-branch-unverified", self._kinds(run))

    def test_unreachable_remote_flags_the_unanswered_probe(self):
        # …but a remote that RESOLVED and would not answer is transient and
        # genuinely worth a human's eye: continue, but record it.
        run = self._to_preflight("W-52")
        gitops.run_git(self.repo, "remote", "add", "origin",
                       str(self.workspace / "no-such-repo.git"))
        self.cli("preflight", "--repo", str(self.repo), run=run)
        unverified = next(e for e in ndjson.read_records(run / "events.ndjson")
                          if e["kind"] == "remote-branch-unverified")
        self.assertEqual(unverified["reason"], "probe-failed")

    def test_suffix_on_an_already_cut_repo_refuses_instead_of_lying(self):
        # adversarial-review: the idempotent fast path used to return the
        # ORIGINAL name with ok:true, leaving the run on the colliding branch
        # believing the branch-aside remedy had been applied.
        run = self._to_preflight("W-53")
        first = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        out = self.cli("preflight", "--repo", str(self.repo),
                       "--feature-branch-suffix", "rerun", run=run, expect=1)
        self.assertIn("already cut branch", out["error"])
        self.assertIn("--feature-branch-suffix", out["error"])
        # the recorded branch is untouched, and a suffix-free retry still no-ops
        self.assertEqual(
            self.cli("preflight", "--repo", str(self.repo), run=run)["branch"],
            first)

    def test_unverified_probe_is_flagged_once_per_run(self):
        # pre-release review: the probe runs BEFORE the clean/default-branch
        # checks, so a probe-failed repo that was also dirty appended one
        # permanent flag per refusal-and-retry loop iteration
        run = self._to_preflight("W-54")
        gitops.run_git(self.repo, "remote", "add", "origin",
                       str(self.workspace / "no-such-repo.git"))
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        unverified = [e for e in ndjson.read_records(run / "events.ndjson")
                      if e["kind"] == "remote-branch-unverified"]
        self.assertEqual(len(unverified), 1)

    def test_idempotent_retry_does_not_reprobe(self):
        # The recorded-`branches` fast path returns BEFORE the probe: a
        # crash-and-retry must not start refusing just because the first
        # attempt's own push landed the branch on the remote.
        run = self._to_preflight("W-49")
        bare = self._add_bare_origin()
        first = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        gitops.run_git(bare, "branch", first, "main")
        second = self.cli("preflight", "--repo", str(self.repo), run=run)
        self.assertEqual(second["branch"], first)
        self.assertNotIn("remote-branch-exists", self._kinds(run))


class FetchAlreadyDone(BreadthHarness):
    """field: dual-run comparison — a story still marked `Done` by a
    run three days earlier was re-fetched and rebuilt end-to-end. The intake
    noticed and flagged it as an ambiguity; nothing mechanical acted on it."""

    def test_done_item_warns_and_flags_but_still_bootstraps(self):
        self.story("W-50", "already shipped")
        (self.stories / "W-50.md").write_text(
            (self.stories / "W-50.md").read_text().replace(
                "Status: Open", "Status: ✅ Done — 2026-07-22"))
        self.init()
        out = self.cli("fetch", "--id", "W-50", "--date", "2026-02-11")
        run = Path(out["run"])
        # warn, never block: replays and re-plans are legitimate
        self.assertTrue(out["ok"])
        self.assertIn("Done", out["already_done"])
        self.assertIn("already in state", out["warning"])
        self.assertTrue(state_mod.state_path(run).exists())
        # and it reaches the shared flagged-events surface
        status = next(r for r in self.cli("status")["runs"]
                      if r["run"] == run.name)
        self.assertGreaterEqual(status["flagged_events"], 1)
        self.assertIn("work-item-already-done",
                      [e.get("kind")
                       for e in ndjson.read_records(run / "events.ndjson")])

    def test_list_shaped_status_mapping_stays_in_the_error_contract(self):
        # pre-release review: an unvalidated status_mapping reached
        # done_state_match — the FIRST verb a run touches — as .get() on a
        # list, escaping as a raw AttributeError traceback (the identical
        # class this branch fixed for env_requirements)
        self.story("W-55", "config typo")
        self.init()
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"status_mapping": ["done", "open"]}))
        out = self.cli("fetch", "--id", "W-55", "--date", "2026-02-14",
                       expect=1)
        self.assertIn("status_mapping", out["error"])
        self.assertIn("must be a mapping", out["error"])

    def test_open_item_is_not_flagged(self):
        self.story("W-51", "still open")     # Status: Open
        self.init()
        out = self.cli("fetch", "--id", "W-51", "--date", "2026-02-11")
        self.assertNotIn("already_done", out)
        self.assertNotIn("work-item-already-done",
                         [e.get("kind") for e in ndjson.read_records(
                             Path(out["run"]) / "events.ndjson")])


class QuickWalk(BreadthHarness):
    def _to_quick_recheck(self, sid, touch_auth: bool):
        self.story(sid, "fix typo in docs page", body="Mode: quick\njust a typo",
                   type_="Task")
        self.init()
        run = Path(self.cli("fetch", "--id", sid, "--date", "2026-02-02")["run"])
        self.assertEqual(self.cli("show", run=run)["state"]["mode"], "quick")
        self.cli("cursor", "--to", "preflight", run=run)
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.cli("cursor", "--to", "develop", run=run)
        self.cli("task", "--id", "T1", "--to", "in-progress", run=run)
        target = (self.repo / "auth" / "check.py") if touch_auth \
            else (self.repo / "docs.md")
        target.parent.mkdir(exist_ok=True)
        target.write_text("fixed\n")
        self.cli("commit", "--repo", str(self.repo), "--task-id", "T1",
                 "--summary", "typo", run=run)
        self.cli("task", "--id", "T1", "--to", "in-review", run=run)  # relaxed
        self.review_approve(run, "T1")  # review is NOT relaxed in quick
        self.cli("task", "--id", "T1", "--to", "done", run=run)
        self.cli("cursor", "--to", "quick-recheck", run=run)
        return run, branch

    def test_quick_mode_provisional_flag_clears_on_first_in_progress(self):
        # adversarial-review finding: quick mode has no plan-register step
        # at all (the only place that ever cleared `provisional`), so the
        # fetch-seeded task stayed flagged provisional forever, even once
        # genuinely worked and done — misleading, since the seed IS the
        # ratified plan in quick mode.
        self.story("Q-13", "fix typo in docs page",
                   body="Mode: quick\njust a typo", type_="Task")
        self.init()
        run = Path(self.cli("fetch", "--id", "Q-13", "--date", "2026-02-15")["run"])
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["tasks"][0]["provisional"], True)
        self.cli("cursor", "--to", "preflight", run=run)
        self.cli("preflight", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "develop", run=run)
        self.cli("task", "--id", "T1", "--to", "in-progress", run=run)
        state = self.cli("show", run=run)["state"]
        self.assertNotIn("provisional", state["tasks"][0])

    def test_quick_walk_clean_to_metrics(self):
        run, _ = self._to_quick_recheck("Q-10", touch_auth=False)
        v = self.cli("quick-recheck", "--repo", str(self.repo), "--base", "main",
                     run=run)
        self.assertEqual(v["verdict"], "clean")
        self.cli("cursor", "--to", "pre-pr", run=run)
        (run / "reports").mkdir(exist_ok=True)
        (run / "reports" / "pre-pr.md").write_text("# Pre-PR\nok\n")
        self.cli("cursor", "--to", "approve-pre-pr", run=run)
        self.gate(run, "approve-pre-pr")
        self.cli("cursor", "--to", "create-pr", run=run)
        self.cli("create-pr", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "reconcile", run=run)
        self.cli("reconcile", run=run)
        self.cli("cursor", "--to", "metrics", run=run)
        self.cli("metrics", run=run)

    def test_escalation_fires_on_auth_touching_diff(self):
        run, _ = self._to_quick_recheck("Q-11", touch_auth=True)
        v = self.cli("quick-recheck", "--repo", str(self.repo), "--base", "main",
                     run=run)
        self.assertEqual(v["verdict"], "dirty")
        # pre-pr is NOT legal now; the declared escalation edge is:
        self.cli("cursor", "--to", "pre-pr", run=run, expect=1)
        self.cli("cursor", "--to", "security", run=run)
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["mode"], "full")        # mode switched
        self.cli("security-scan", run=run)
        self.cli("cursor", "--to", "pre-pr", run=run)  # continues in full

    def test_escalation_fires_on_oversized_diff_even_without_a_pattern_hit(self):
        # adversarial-review finding: quick_mode.loc_max/files_max were
        # schema-validated but never consumed — a diff far past the
        # configured size cap used to pass recheck as long as it avoided
        # the disqualify paths entirely.
        self.story("Q-12", "fix typo in docs page",
                   body="Mode: quick\njust a typo", type_="Task")
        self.init()
        run = Path(self.cli("fetch", "--id", "Q-12", "--date", "2026-02-14")["run"])
        self.cli("cursor", "--to", "preflight", run=run)
        self.cli("preflight", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "develop", run=run)
        self.cli("task", "--id", "T1", "--to", "in-progress", run=run)
        for i in range(6):   # files_max default is 5 — benign paths only
            (self.repo / f"docs-{i}.md").write_text("line\n" * 20)
        self.cli("commit", "--repo", str(self.repo), "--task-id", "T1",
                 "--summary", "big benign change", run=run)
        self.cli("task", "--id", "T1", "--to", "in-review", run=run)
        self.review_approve(run, "T1")
        self.cli("task", "--id", "T1", "--to", "done", run=run)
        self.cli("cursor", "--to", "quick-recheck", run=run)
        v = self.cli("quick-recheck", "--repo", str(self.repo), "--base", "main",
                     run=run)
        self.assertEqual(v["verdict"], "dirty")
        event = next(e for e in ndjson.read_records(run / "events.ndjson")
                    if e.get("kind") == "quick-recheck")
        self.assertEqual(event["files_touched"], 6)
        self.assertGreater(event["loc_changed"], 80)   # past shipped loc_max
        self.cli("cursor", "--to", "pre-pr", run=run, expect=1)
        self.cli("cursor", "--to", "security", run=run)   # escalated to full


class TwoRepoContracts(BreadthHarness):
    def test_contract_drift_surfaced_then_clean(self):
        repo_b = make_repo(self.workspace, "repo-b")
        self.story("W-20", "add api v2 across services")
        self.init(extra_repos=f"repo-b={repo_b}",
                 extra_test_cmd=f"repo-b={TEST_CMD}")
        run = Path(self.cli("fetch", "--id", "W-20", "--date", "2026-02-03")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run, self.repo, repo_b)
        self.cli("plan-register",
                 "--tasks-json", json.dumps([
                     {"id": "T1", "repo": str(self.repo)},
                     {"id": "T2", "repo": str(repo_b)}]),
                 "--contracts-json", json.dumps([
                     {"id": "C1", "signature": "def api_v2(payload)",
                      "repos": ["repo", "repo-b"]}]),
                 run=run)

        (self.repo / "api.py").write_text("def api_v2(payload):\n    return 1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "chore: api in A only")

        v = self.cli("reconcile-contracts", run=run)
        self.assertEqual(v["verdict"], "drift")        # surfaced, not auto-fixed
        report = (run / "reports" / "contracts.md").read_text(encoding="utf-8")
        self.assertIn("C1 @ repo-b: **MISSING**", report)
        self.assertIn("C1 @ repo: present", report)

        (repo_b / "api.py").write_text("def api_v2(payload):\n    return 2\n")
        gitops.run_git(repo_b, "add", "-A")
        gitops.run_git(repo_b, "commit", "-m", "chore: api in B")
        self.assertEqual(self.cli("reconcile-contracts", run=run)["verdict"],
                         "clean")

    def test_mirror_copy_never_satisfies_the_scan(self):
        """Field (session D, agent-diagnosed): every preflighted repo
        carries the run's committed ai/<run>/ mirror, whose state.yaml
        holds the contract declarations verbatim — so fragments were
        matching their own declaration: prose-annotated fragments that
        can never appear in source passed as CLEAN, while PyYAML's
        line-wrapping of longer ones flagged implemented code as MISSING.
        ai/** is now excluded from the scan."""
        self.story("W-29", "mirror must not satisfy contracts")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-29",
                            "--date", "2026-02-22")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register",
                 "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 "--contracts-json", json.dumps([
                     {"id": "C1", "signature": "declared_only_in_mirror(x)",
                      "repos": ["repo"]}]),
                 run=run)
        # simulate the published mirror: the declaration lands IN the repo
        mirror = self.repo / "ai" / run.name
        mirror.mkdir(parents=True)
        (mirror / "state.yaml").write_text(
            "contracts:\n- signature: declared_only_in_mirror(x)\n")
        (mirror / ".mirror").write_text("published snapshot\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m",
                       "chore(harness): publish run snapshot")
        v = self.cli("reconcile-contracts", run=run)
        self.assertEqual(v["verdict"], "drift")   # the mirror match is void
        # a REAL source implementation still satisfies the fragment
        (self.repo / "impl.py").write_text(
            "def declared_only_in_mirror(x):\n    return x\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat: implement")
        self.assertEqual(self.cli("reconcile-contracts", run=run)["verdict"],
                         "clean")

    def test_plan_register_guards(self):
        self.story("W-21", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-21", "--date", "2026-02-04")["run"])
        out = self.cli("plan-register", "--tasks-json", '[{"id": "T1"}]',
                       run=run, expect=1)
        self.assertIn("legal only at the plan step", out["error"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.cli("plan-register", "--tasks-json",
                 '[{"id": "T1"}, {"id": "T1"}]', run=run, expect=1)

    def test_plan_register_stores_test_intents(self):
        self.story("W-22", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-22", "--date", "2026-02-05")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo),
                              "test_intents": ["test_a", "test_b"],
                              "files": ["src/thing.py",
                                        "tests/test_thing.py"]}]),
                 run=run)
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["tasks"][0]["test_intents"], ["test_a", "test_b"])
        self.assertEqual(state["tasks"][0]["files"],
                         ["src/thing.py", "tests/test_thing.py"])

    def test_plan_register_accepts_json_files(self):
        # File input avoids shell-quoting large payloads / space-containing
        # workspace paths that inline `$(cat …)` substitution is fragile around.
        self.story("W-23", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-23", "--date", "2026-02-06")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        tasks_file = self.workspace / "tasks.json"
        tasks_file.write_text(json.dumps([
            {"id": "T1", "repo": str(self.repo)},
            {"id": "T2", "repo": str(self.repo)}]))
        contracts_file = self.workspace / "contracts.json"
        contracts_file.write_text(json.dumps([
            {"id": "C1", "signature": "def f()", "repos": ["repo"]}]))
        out = self.cli("plan-register",
                       "--tasks-json-file", str(tasks_file),
                       "--contracts-json-file", str(contracts_file), run=run)
        self.assertEqual(out["tasks"], ["T1", "T2"])
        self.assertEqual(out["contracts"], ["C1"])
        state = self.cli("show", run=run)["state"]
        self.assertEqual([t["id"] for t in state["tasks"]], ["T1", "T2"])

    def test_plan_register_rejects_both_inline_and_file(self):
        self.story("W-24", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-24", "--date", "2026-02-07")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        tasks_file = self.workspace / "tasks.json"
        tasks_file.write_text('[{"id": "T1"}]')
        out = self.cli("plan-register", "--tasks-json", '[{"id": "T1"}]',
                       "--tasks-json-file", str(tasks_file), run=run, expect=1)
        self.assertIn("only one of", out["error"])

    def test_plan_register_requires_a_task_source(self):
        self.story("W-25", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-25", "--date", "2026-02-08")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        out = self.cli("plan-register", run=run, expect=1)
        self.assertIn("--tasks-json", out["error"])

    def test_create_pr_without_preflight_record_fails_closed_not_guessed(self):
        # re-review finding: with no recorded per-repo base branch,
        # create_pr used to fall back to a guessed 'main' — silently
        # targeting the wrong base on any repo whose default branch
        # differs. It must refuse and point at preflight instead.
        self.story("W-28", "no preflight yet")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-28", "--date", "2026-02-19")["run"])
        out = self.cli("create-pr", "--repo", str(self.repo), run=run, expect=1)
        self.assertIn("preflight", out["error"])

    def test_preflight_and_create_pr_are_keyed_per_repo_not_overwritten(self):
        # adversarial-review finding: preflight's idempotency check and
        # create_pr's single 'pr' artifact both used to be run-level
        # singletons — the second repo's call silently returned/overwrote
        # the first repo's record instead of creating its own.
        repo_b = make_repo(self.workspace, "repo-b")
        self.story("W-27", "two repo feature")
        self.init(extra_repos=f"repo-b={repo_b}",
                 extra_test_cmd=f"repo-b={TEST_CMD}")
        run = Path(self.cli("fetch", "--id", "W-27", "--date", "2026-02-10")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run, self.repo, repo_b)
        self.cli("plan-register",
                 "--tasks-json", json.dumps([
                     {"id": "T1", "repo": str(self.repo)},
                     {"id": "T2", "repo": str(repo_b)}]),
                 run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.gate(run, "approve-plan")   # decided AT the gate (cursor-anchored)
        self.cli("cursor", "--to", "preflight", run=run)

        branch_a = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        # Same naming template -> same branch NAME in both repos (that's fine,
        # they're different git repos); the bug was the ARTIFACT overwriting,
        # not the name. Confirm repo B's preflight actually did its own work
        # (its checkout really moved) rather than short-circuiting on repo A's
        # already-recorded artifact and returning without touching repo B.
        self.assertEqual(gitops.run_git(repo_b, "rev-parse", "--abbrev-ref", "HEAD"),
                         "main")
        branch_b = self.cli("preflight", "--repo", str(repo_b), run=run)["branch"]
        self.assertEqual(branch_b, branch_a)   # same template, distinct repos
        self.assertEqual(gitops.run_git(repo_b, "rev-parse", "--abbrev-ref", "HEAD"),
                         branch_b)
        # retry on repo A must still return repo A's own record (idempotent
        # resume) rather than erroring or re-deriving — the bug this
        # regresses against would have short-circuited on the FIRST repo's
        # entry for every subsequent repo, or errored re-deriving a branch
        # that already exists.
        retry_a = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(retry_a, branch_a)

        state = self.cli("show", run=run)["state"]
        branches = state["artifacts"]["branches"]
        self.assertEqual(set(branches), {"repo", "repo-b"})
        self.assertEqual(branches["repo"]["branch"], branch_a)
        self.assertEqual(branches["repo-b"]["branch"], branch_b)
        self.assertEqual(branches["repo"]["base"], "main")

        self.cli("cursor", "--to", "develop", run=run)
        for task_id, repo, branch in (("T1", self.repo, branch_a),
                                      ("T2", repo_b, branch_b)):
            wt = self.cli("worktree-add", "--repo", str(repo), "--task-id", task_id,
                          "--base", branch, run=run)
            worktree = Path(wt["path"])
            self.tdd_task(run, task_id, worktree)
            gitops.run_git(repo, "checkout", branch)
            self.cli("merge-task", "--repo", str(repo), "--task-id", task_id,
                     "--task-branch", wt["branch"], "--summary", "impl", run=run)
            self.cli("task", "--id", task_id, "--to", "done", run=run)
            self.cli("worktree-remove", "--repo", str(repo), "--task-id", task_id,
                     run=run)

        self.cli("cursor", "--to", "approve-impl", run=run)
        self.gate(run, "approve-impl")
        self.cli("cursor", "--to", "harden", run=run)
        self.cli("cursor", "--to", "security", run=run)
        self.cli("security-scan", run=run)
        self.cli("cursor", "--to", "pre-pr", run=run)   # gate skipped (info<medium)
        (run / "reports").mkdir(exist_ok=True)
        (run / "reports" / "pre-pr.md").write_text("# Pre-PR\nAll good.\n")
        self.cli("cursor", "--to", "approve-pre-pr", run=run)
        self.gate(run, "approve-pre-pr")
        self.cli("cursor", "--to", "create-pr", run=run)

        pr_a = self.cli("create-pr", "--repo", str(self.repo), run=run)
        pr_b = self.cli("create-pr", "--repo", str(repo_b), run=run)
        self.assertNotEqual(pr_a["url"], pr_b["url"])   # distinct records, not one overwriting the other
        state = self.cli("show", run=run)["state"]
        prs = state["artifacts"]["pr"]
        self.assertEqual(set(prs), {"repo", "repo-b"})
        self.assertEqual(prs["repo"]["branch"], branch_a)
        self.assertEqual(prs["repo-b"]["branch"], branch_b)

    def test_fetch_seeds_provisional_placeholder_task(self):
        # The fetch-seeded T1 is a positional-default placeholder, not a scope
        # decision — it must be self-describing in state, and plan-register
        # must drop the flag once the real plan lands.
        self.story("W-26", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-26", "--date", "2026-02-09")["run"])
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["tasks"][0]["provisional"], True)
        events = ndjson.read_records(run / "events.ndjson")
        fetched = next(e for e in events if e["kind"] == "fetched")
        self.assertIn("positional-default", fetched["seed_task"]["basis"])
        status = self.cli("status")
        this_run = next(r for r in status["runs"] if r["run"] == run.name)
        self.assertEqual(this_run["provisional_tasks"], ["T1"])
        # plan-register replaces the seed wholesale — no residual flag.
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        state = self.cli("show", run=run)["state"]
        self.assertNotIn("provisional", state["tasks"][0])
        status = self.cli("status")
        this_run = next(r for r in status["runs"] if r["run"] == run.name)
        self.assertEqual(this_run["provisional_tasks"], [])


class SecurityScanParsing(BreadthHarness):
    def test_configured_scanner_severity_parsed(self):
        self.story("W-30", "sec thing")
        self.init()
        # user config overrides shipped defaults (piece 4 resolution)
        ctx = self.workspace / ".claude" / "context"
        (ctx / "security-override.yaml").write_text(
            'security:\n  severity_order: [info, low, medium, high, critical]\n'
            '  gate_threshold: medium\n'
            '  scan_cmd:\n'
            f'    repo: "echo FINDING high: hardcoded token; exit 1"\n')
        run = Path(self.cli("fetch", "--id", "W-30", "--date", "2026-02-05")["run"])
        st = self.cli("show", run=run)["state"]
        for step in ("intake", "plan", "plan-review", "approve-plan",
                     "preflight", "develop",
                     "approve-impl", "harden", "security"):
            if st["cursor"]["current_step"] == "security":
                break
            if step == "plan-review":
                # leaving `plan` requires the confirmed scope + registered
                # (non-provisional) task list — the requires_tasks_registered
                # mechanization
                self.scope(run)
                self.cli("plan-register", "--tasks-json",
                         json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                         run=run)
            if step == "approve-plan":
                # seeded while the cursor is still at plan-review — the
                # verdict is what legalizes the move into the gate
                support.seed_review_verdict(run)
            if step == "preflight":
                self.gate(run, "approve-plan")   # decided AT the gate
            if step == "approve-impl":
                self._force_tasks_done(run)
            if step == "harden":
                self.gate(run, "approve-impl")   # decided AT the gate
            self.cli("cursor", "--to", step, run=run)
        sev = self.cli("security-scan", run=run)
        self.assertEqual(sev["max_severity"], "high")
        # gate now REQUIRED (high >= medium): skipping to pre-pr is illegal
        self.cli("cursor", "--to", "pre-pr", run=run, expect=1)
        self.cli("cursor", "--to", "approve-security", run=run)
        self.gate(run, "approve-security", reply="2")
        # manifest dispositions [fix-now, waive, defer]: "2" = waive -> forward
        self.cli("cursor", "--to", "pre-pr", run=run)

    def test_multi_repo_aggregates_max_severity_not_last_write_wins(self):
        """Regression: security-scan used to run once per --repo, each call
        overwriting the run's one max_severity artifact — a clean repo
        scanned after a critical one silently erased the critical finding
        and let the mandatory gate be skipped. Now one call scans every
        registered repo and takes the true max across all of them."""
        repo_b = make_repo(self.workspace, "repo-b")
        self.story("W-31", "sec thing across repos")
        self.init(extra_repos=f"repo-b={repo_b}",
                 extra_test_cmd=f"repo-b={TEST_CMD}")
        ctx = self.workspace / ".claude" / "context"
        (ctx / "security-override.yaml").write_text(
            'security:\n  severity_order: [info, low, medium, high, critical]\n'
            '  gate_threshold: medium\n'
            '  scan_cmd:\n'
            '    repo: "echo FINDING critical: sql injection; exit 1"\n'
            '    repo-b: "echo clean"\n')
        run = Path(self.cli("fetch", "--id", "W-31", "--date", "2026-02-06")["run"])
        st = self.cli("show", run=run)["state"]
        for step in ("intake", "plan", "plan-review", "approve-plan",
                     "preflight", "develop",
                     "approve-impl", "harden", "security"):
            if st["cursor"]["current_step"] == "security":
                break
            if step == "plan-review":
                # leaving `plan` requires the confirmed scope + registered
                # (non-provisional) task list — the requires_tasks_registered
                # mechanization
                self.scope(run)
                self.cli("plan-register", "--tasks-json",
                         json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                         run=run)
            if step == "approve-plan":
                # seeded while the cursor is still at plan-review — the
                # verdict is what legalizes the move into the gate
                support.seed_review_verdict(run)
            if step == "preflight":
                self.gate(run, "approve-plan")   # decided AT the gate
            if step == "approve-impl":
                self._force_tasks_done(run)
            if step == "harden":
                self.gate(run, "approve-impl")   # decided AT the gate
            self.cli("cursor", "--to", step, run=run)
        sev = self.cli("security-scan", run=run)
        self.assertEqual(sev["max_severity"], "critical")
        report = (run / "reports" / "security.md").read_text(encoding="utf-8")
        self.assertIn("## repo", report)
        self.assertIn("## repo-b", report)
        # gate REQUIRED (critical >= medium): the critical finding survived
        self.cli("cursor", "--to", "pre-pr", run=run, expect=1)


class AbortRun(BreadthHarness):
    """`harness abort` — previously promised by every "offer Resume or
    Abort" message and implemented nowhere, leaving zombie runs that
    permanently blocked re-bootstrapping their work item."""

    def test_abort_is_terminal_and_releases_the_work_item_slot(self):
        self.story("W-80", "abandoned mid-flight")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-80", "--date", "2026-02-01")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        # same-item bootstrap is refused while the run is live (B5)
        self.cli("fetch", "--id", "W-80", "--date", "2026-02-02", expect=1)
        self.cli("abort", "--reason", "requirements withdrawn", run=run)
        # aborted = terminal: every mutating verb refuses...
        out = self.cli("cursor", "--to", "develop", run=run, expect=1)
        self.assertIn("aborted", out["error"])
        self.cli("task", "--id", "T1", "--to", "in-progress", run=run, expect=1)
        self.cli("abort", "--reason", "again", run=run, expect=1)  # not twice
        # ...the dashboard says so...
        entry = next(r for r in self.cli("status")["runs"]
                     if r["run"] == run.name)
        self.assertEqual(entry["aborted"]["reason"], "requirements withdrawn")
        # ...the audit trail records it...
        kinds = [e["kind"] for e in ndjson.read_records(run / "events.ndjson")]
        self.assertIn("aborted", kinds)
        # ...and the SAME work item can now bootstrap fresh (slot released)
        run2 = Path(self.cli("fetch", "--id", "W-80",
                             "--date", "2026-02-03")["run"])
        self.assertNotEqual(run2, run)

    def test_side_effecting_verbs_also_refuse_on_an_aborted_run(self):
        """Adversarial-review finding: `ensure_live`'s "every mutating entry
        point" claim missed worktree-add (re-leaks a swept worktree),
        write-back (pushes a live tracker status for a dead run),
        verify-red/security-scan/quick-recheck/metrics/merge-task/log-event."""
        self.story("W-83", "abandoned")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-83", "--date", "2026-02-01")["run"])
        self.cli("cursor", "--to", "preflight", run=run, expect=1)  # provisional
        self.cli("abort", "--reason", "withdrawn", run=run)
        for args in (
            ["worktree-add", "--repo", str(self.repo), "--task-id", "T1",
             "--base", "main"],
            ["worktree-remove", "--repo", str(self.repo), "--task-id", "T1"],
            ["write-back", "--milestone", "develop_start"],
            ["security-scan"],
            ["metrics"],
            ["merge-task", "--repo", str(self.repo), "--task-id", "T1",
             "--task-branch", "task/T1"],
            ["log-event", "--json", '{"kind":"x"}'],
        ):
            out = self.cli(*args, run=run, expect=1)
            self.assertIn("aborted", out["error"], args[0])


class CliBoundary(BreadthHarness):
    """Boundary failures land in the JSON error contract, never a raw
    traceback (adversarial-review findings, one test per escape route)."""

    def test_malformed_context_yaml_refuses_cleanly_and_names_the_file(self):
        self.init()
        bad = self.workspace / ".claude" / "context" / "overrides.yaml"
        bad.write_text("provider:\n\t- broken tab indent\n")
        out = self.cli("status", expect=1)   # any verb — config load precedes all
        self.assertIn("overrides.yaml", out["error"])
        self.assertIn("invalid YAML", out["error"])
        bad.write_text("- a\n- top-level list\n")
        out = self.cli("status", expect=1)
        self.assertIn("must be a mapping", out["error"])

    def test_provider_missing_required_flag_is_a_refusal_not_a_traceback(self):
        self.init()
        out = self.cli("provider", "--op", "work_item.transition",
                       "--id", "7", expect=1)
        self.assertIn("--to", out["error"])

    def test_tasks_json_file_typo_is_a_refusal_not_a_traceback(self):
        self.story("W-90", "boundary")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-90", "--date", "2026-02-01")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        out = self.cli("plan-register", "--tasks-json-file",
                       str(run / "nope.json"), run=run, expect=1)
        self.assertIn("FileNotFoundError", out["error"])

    def test_status_isolates_a_corrupt_run(self):
        self.story("W-91", "healthy")
        self.story("W-92", "corrupt")
        self.init()
        good = Path(self.cli("fetch", "--id", "W-91", "--date", "2026-02-01")["run"])
        bad = Path(self.cli("fetch", "--id", "W-92", "--date", "2026-02-01")["run"])
        sf = bad / "state.yaml"
        sf.write_text(sf.read_text(encoding="utf-8") + "# tampered\n")
        out = self.cli("status")
        by_name = {r["run"]: r for r in out["runs"]}
        self.assertEqual(by_name[good.name]["work_item"], "W-91")   # survives
        self.assertIn("IntegrityError", by_name[bad.name]["error"])
        self.assertIn("reseal", by_name[bad.name]["remediation"])

    def test_bootstrap_task_spec_repo_may_contain_colons(self):
        self.init()
        run = self.workspace / "ai" / "2026-02-01-COLON-1"
        self.cli("bootstrap", "--work-item-id", "COLON-1", "--title", "t",
                 "--mode", "quick", "--change-type", "fix",
                 "--task", r"T1:C:\repos\x", run=run)
        st = state_mod.load(run, self.workspace)
        self.assertEqual(st["tasks"][0]["repo"], r"C:\repos\x")


class PlanRegisterValidation(BreadthHarness):
    def setUp(self):
        super().setUp()
        self.story("W-95", "deps")
        self.init()
        self.run_dir = Path(
            self.cli("fetch", "--id", "W-95", "--date", "2026-02-01")["run"])
        self.cli("cursor", "--to", "intake", run=self.run_dir)
        self.cli("cursor", "--to", "plan", run=self.run_dir)
        self.scope(self.run_dir)

    def _register(self, tasks, expect=0):
        return self.cli("plan-register", "--tasks-json", json.dumps(tasks),
                        run=self.run_dir, expect=expect)

    def test_dangling_dependency_refused(self):
        out = self._register([{"id": "T1", "depends_on": ["T99"]}], expect=1)
        self.assertIn("unknown task", out["error"])

    def test_dependency_cycle_refused(self):
        out = self._register([{"id": "T1", "depends_on": ["T2"]},
                              {"id": "T2", "depends_on": ["T1"]}], expect=1)
        self.assertIn("cycle", out["error"])

    def test_unsafe_task_id_refused(self):
        out = self._register([{"id": "T 1"}], expect=1)
        self.assertIn("not usable", out["error"])

    def test_valid_dag_registers(self):
        self._register([{"id": "T1", "repo": str(self.repo)},
                        {"id": "T2", "repo": str(self.repo),
                         "depends_on": ["T1"]}])

    def test_task_repo_outside_confirmed_scope_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo)},
                              {"id": "T2", "repo": "/somewhere/else"}],
                             expect=1)
        self.assertIn("outside the confirmed scope", out["error"])

    def test_repo_less_task_refused_as_off_scope(self):
        # the legacy "." default is never in a confirmed scope — a task
        # must carry its registered repo path explicitly
        out = self._register([{"id": "T1"}], expect=1)
        self.assertIn("outside the confirmed scope", out["error"])

    def test_http_verb_prefixed_fragment_is_flagged_not_refused(self):
        """field: dual-run comparison — one run registered
        `GET /api/v1/admin/workflows/discovery`; the method+path form appears
        verbatim in neither client nor controller source, so
        reconcile-contracts reported drift on a correct implementation and
        pre-PR had to adjudicate it away. The other run registered the bare
        route and got clean. FLAGGED, not refused: the shape is documented
        and legal, and it matches fine where source really carries it."""
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 "--contracts-json",
                 json.dumps([{"id": "C1", "type": "http",
                              "producer": str(self.repo),
                              "consumers": [str(self.repo)],
                              "signature": ["GET /api/v1/admin/discovery"]}]),
                 run=self.run_dir)
        flagged = [e for e in ndjson.read_records(self.run_dir / "events.ndjson")
                   if e.get("kind") == "contract-fragment-weak"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["contract"], "C1")
        self.assertIn("/api/v1/admin/discovery", flagged[0]["reason"])

    def test_route_only_fragment_is_not_flagged(self):
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 "--contracts-json",
                 json.dumps([{"id": "C1", "type": "http",
                              "producer": str(self.repo),
                              "consumers": [str(self.repo)],
                              "signature": ["admin/workflows/discovery"]}]),
                 run=self.run_dir)
        self.assertNotIn("contract-fragment-weak",
                         [e.get("kind") for e in ndjson.read_records(
                             self.run_dir / "events.ndjson")])

    def test_env_requires_must_name_a_declared_probe(self):
        # half-enforced-vocabulary bar: an unprobeable requirement is worse
        # than none — env-check would skip it while it READS as checked
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "env_requires": ["kubernetes"]}], expect=1)
        self.assertIn("no probe declared", out["error"])
        self.assertIn("docker", out["error"])          # names the known set

    def test_env_requires_shape_is_validated(self):
        for bad in ("docker", [""], [7], {"docker": True}):
            out = self._register([{"id": "T1", "repo": str(self.repo),
                                   "env_requires": bad}], expect=1)
            self.assertIn("env_requires", out["error"])

    def test_env_requires_registers_normalized_and_deduped(self):
        self._register([{"id": "T1", "repo": str(self.repo),
                         "env_requires": ["docker", " docker ", "docker"]}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["tasks"][0]["env_requires"], ["docker"])

    def test_above_low_risk_without_tests_or_reason_refused(self):
        """Zero-test policy (field 459226): one medium-risk task shipped
        with empty test_intents and no red-proof, justified only by an
        unrecorded repo coverage convention — the gap surfaced only in a
        manual post-mortem. The opt-out now needs a recorded why."""
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "risk": "medium", "test_intents": []}],
                             expect=1)
        self.assertIn("no_test_reason", out["error"])

    def test_custom_risk_tier_fails_closed(self):
        # risk is free-form vocabulary — anything other than "low" demands
        # the reason, so a custom tier can't silently duck the policy
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "risk": "critical"}], expect=1)
        self.assertIn("no_test_reason", out["error"])

    def test_blank_no_test_reason_refused(self):
        # whitespace is not a recorded decision
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "risk": "medium", "no_test_reason": "   "}],
                             expect=1)
        self.assertIn("no_test_reason", out["error"])

    def test_recorded_no_test_reason_registers_stores_and_flags(self):
        self._register([{"id": "T1", "repo": str(self.repo),
                         "risk": "medium", "test_intents": [],
                         "no_test_reason": "repo [ExcludeFromCodeCoverage] "
                                           "convention for this layer"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertIn("ExcludeFromCodeCoverage",
                      st["tasks"][0]["no_test_reason"])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        flagged = [e for e in events if e.get("kind") == "risk-without-tests"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["task"], "T1")
        self.assertIn("ExcludeFromCodeCoverage", flagged[0]["reason"])
        # and the kind is human-visible: in the shared flagged filter
        from harness.workflow import FLAGGED_EVENT_KINDS, outstanding_flagged
        self.assertIn("risk-without-tests", FLAGGED_EVENT_KINDS)
        self.assertEqual(len(outstanding_flagged(events)), 1)

    def test_low_risk_zero_test_opt_out_stays_silent(self):
        # the docs/chore opt-out is unchanged: no reason demanded, no flag
        self._register([{"id": "T1", "repo": str(self.repo),
                         "risk": "low", "test_intents": []}])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "risk-without-tests"])

    def test_above_low_risk_with_tests_needs_no_reason(self):
        self._register([{"id": "T1", "repo": str(self.repo),
                         "risk": "high", "test_intents": ["test_x"],
                         "files": ["src/x.py", "tests/test_x.py"]}])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "risk-without-tests"])

    def test_reregistration_supersedes_prior_risk_flags(self):
        """Adversarial review of this change (both lenses, independently):
        the event asserts live plan STATE, and registration replaces the
        task list wholesale — so each `plan-registered` marker supersedes
        every earlier `risk-without-tests` batch. A withdrawn opt-out must
        not haunt the flagged gauge at later gates."""
        from harness.workflow import outstanding_flagged
        opt_out = {"id": "T1", "repo": str(self.repo), "risk": "medium",
                   "test_intents": [], "no_test_reason": "layer convention"}
        self._register([opt_out])
        self._register([opt_out])   # unchanged decomposition, re-ratified
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        live = [e for e in outstanding_flagged(events)
                if e.get("kind") == "risk-without-tests"]
        self.assertEqual(len(live), 1)   # latest batch only, not 2
        # revision round adds the tests — the opt-out is withdrawn
        self._register([{"id": "T1", "repo": str(self.repo),
                         "risk": "medium", "test_intents": ["test_x"],
                         "files": ["src/x.py", "tests/test_x.py"]}])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in outstanding_flagged(events)
                          if e.get("kind") == "risk-without-tests"])

    def test_default_risk_zero_test_task_registers_silently(self):
        # the most common docs/chore shape: no risk key at all — defaults
        # low, no reason demanded, no flag (pins the `t.get(risk, "low")`
        # default against regression)
        self._register([{"id": "T1", "repo": str(self.repo)}])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "risk-without-tests"])

    def test_low_risk_reason_stored_but_not_flagged(self):
        # a volunteered reason on a low-risk opt-out is kept on the task
        # (harmless context) but the policy demands nothing — no event
        self._register([{"id": "T1", "repo": str(self.repo), "risk": "low",
                         "no_test_reason": "pure docs move"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["tasks"][0]["no_test_reason"], "pure docs move")
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "risk-without-tests"])

    def test_stale_reason_alongside_intents_normalized_away(self):
        # a reason riding with declared intents is a self-contradictory
        # record — normalized to None, never stored
        self._register([{"id": "T1", "repo": str(self.repo),
                         "risk": "medium", "test_intents": ["test_x"],
                         "files": ["src/x.py", "tests/test_x.py"],
                         "no_test_reason": "stale from an earlier draft"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertIsNone(st["tasks"][0]["no_test_reason"])

    def test_non_list_test_intents_refused(self):
        # a string would read as per-character intents at verify-red —
        # refuse the shape at the owned entry point
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": "test_x"}], expect=1)
        self.assertIn("LIST", out["error"])

    def test_non_string_no_test_reason_refused(self):
        # a dict stringifies truthy — garbage must not become a
        # "recorded decision"
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "risk": "medium",
                               "no_test_reason": {"why": "nope"}}],
                             expect=1)
        self.assertIn("no_test_reason must be a string", out["error"])

    # ---- coverage-backfill policy (field 459226 postmortem F-2, the
    # ---- mechanical half): a test-carrying task must register a files
    # ---- manifest naming at least one non-test path

    def test_test_carrying_task_without_files_refused(self):
        # fail-closed: an absent manifest must not duck the policy — the
        # same stance as the custom risk tier for no_test_reason
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"]}], expect=1)
        self.assertIn("no `files` manifest", out["error"])

    def test_all_test_manifest_without_reason_refused(self):
        # the field shape verbatim: a pure test-add on unmodified
        # production code sailed through both review rounds and aborted
        # the run at develop — now it can't register
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["tests/test_x.py"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])

    def test_all_test_manifest_with_reason_registers_stores_and_flags(self):
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_shared_fixture_shape"],
                         "files": ["tests/conftest_helpers.py",
                                   "tests/test_fixture_shape.py"],
                         "test_only_reason": "task's product IS the shared "
                                             "fixture layer"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertIn("shared", st["tasks"][0]["test_only_reason"])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        flagged = [e for e in events
                   if e.get("kind") == "tests-without-production"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["task"], "T1")
        # human-visible in the shared filter, but never health-degrading:
        # a recorded, reviewed decision — the risk-without-tests class
        from harness.workflow import (FLAGGED_EVENT_KINDS,
                                      HEALTH_DEGRADING_KINDS,
                                      outstanding_flagged, run_health)
        self.assertIn("tests-without-production", FLAGGED_EVENT_KINDS)
        self.assertNotIn("tests-without-production", HEALTH_DEGRADING_KINDS)
        self.assertEqual(len(outstanding_flagged(events)), 1)
        self.assertEqual(run_health(events)[0], "HEALTHY")

    def test_production_entry_registers_silently_and_persists_files(self):
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": [" src/x.py", "tests/test_x.py"]}])
        st = state_mod.load(self.run_dir, self.workspace)
        # entries are stripped on persist; order preserved
        self.assertEqual(st["tasks"][0]["files"],
                         ["src/x.py", "tests/test_x.py"])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "tests-without-production"])

    def test_zero_test_task_needs_no_files(self):
        # the policy reads files only for test-carrying tasks — a docs
        # task without a manifest registers exactly as before
        self._register([{"id": "T1", "repo": str(self.repo)}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["tasks"][0]["files"], [])

    def test_java_and_js_test_layouts_classify_as_tests(self):
        # the vocabulary is language.test_paths (the same one verify-red
        # reads) — not a Python-only heuristic
        out = self._register(
            [{"id": "T1", "repo": str(self.repo),
              "test_intents": ["shouldAuthorize"],
              "files": ["src/test/java/AuthTest.java",
                        "web/__tests__/auth.spec.ts"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])

    def test_non_list_files_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": "src/x.py"}], expect=1)
        self.assertIn("files must be a LIST", out["error"])

    def test_non_string_files_entry_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["src/x.py", 7]}], expect=1)
        self.assertIn("files must be a LIST", out["error"])

    def test_absolute_files_entry_refused(self):
        # an absolute path can't be honestly classified against the
        # repo's test globs — and would trivially defeat the policy
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["/etc/passwd",
                                         "tests/test_x.py"]}], expect=1)
        self.assertIn("repo-relative", out["error"])

    def test_parent_traversal_files_entry_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["../elsewhere/x.py",
                                         "tests/test_x.py"]}], expect=1)
        self.assertIn("repo-relative", out["error"])

    def test_windows_style_paths_are_normalized_for_the_policy(self):
        # backslash separators and drive letters: the former classify,
        # the latter refuse — the harness runs on Windows too
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["tests\\test_x.py"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["C:/work/x.py"]}], expect=1)
        self.assertIn("repo-relative", out["error"])

    def test_stale_test_only_reason_normalized_away(self):
        # a reason riding with a production entry is self-contradictory —
        # normalized to None, never stored, never flagged (the
        # no_test_reason mirror)
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": ["src/x.py", "tests/test_x.py"],
                         "test_only_reason": "stale from an earlier draft"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertIsNone(st["tasks"][0]["test_only_reason"])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "tests-without-production"])

    def test_non_string_test_only_reason_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["tests/test_x.py"],
                               "test_only_reason": ["not", "a",
                                                    "string"]}], expect=1)
        self.assertIn("test_only_reason must be a string", out["error"])

    def test_reregistration_supersedes_prior_backfill_flags(self):
        """The same live-gauge rule as risk-without-tests: registration
        replaces the task list wholesale, so a revision that adds the
        production change must clear the stale backfill flag."""
        from harness.workflow import outstanding_flagged
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": ["tests/test_x.py"],
                         "test_only_reason": "fixture-layer task"}])
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": ["src/x.py", "tests/test_x.py"]}])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in outstanding_flagged(events)
                          if e.get("kind") == "tests-without-production"])

    def test_forged_marker_does_not_clear_the_gauge(self):
        # adversarial-review on this change: the supersession marker is
        # actor-checked — a stray `log-event` record with the marker's
        # kind (log-event is unvalidated by design) must not silently
        # clear outstanding plan flags. Fail-closed, like the deferral
        # resolver.
        from harness.workflow import outstanding_flagged
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": ["tests/test_x.py"],
                         "test_only_reason": "fixture-layer task"}])
        self.cli("log-event", "--json",
                 json.dumps({"kind": "plan-registered",
                             "actor": "drifting-orchestrator"}),
                 run=self.run_dir)
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertEqual(len([e for e in outstanding_flagged(events)
                              if e.get("kind")
                              == "tests-without-production"]), 1)

    def test_dot_slash_prefix_still_classifies_as_test(self):
        # adversarial-review on this change: `./tests/…` classified as
        # production — two characters re-opened the F-2 dead-end. Entries
        # are normalized (`./` collapsed) before classification.
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["./tests/test_x.py"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])

    def test_directory_shaped_entries_refused(self):
        # "." or a trailing-slash entry would satisfy the production
        # requirement vacuously — structure failures, inside the gate's
        # own jurisdiction
        for entry in [".", "src/", "./"]:
            out = self._register([{"id": "T1", "repo": str(self.repo),
                                   "test_intents": ["test_x"],
                                   "files": ["tests/test_x.py", entry]}],
                                 expect=1)
            self.assertIn("directory, not a file", out["error"])

    def test_root_conftest_classifies_as_test(self):
        # classification is test_paths ∪ test_closure — the set verify-red
        # SHA-locks. Root conftest.py is closure: a fixture-layer task
        # needs its recorded reason regardless of directory layout.
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["conftest.py",
                                         "tests/test_x.py"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])

    def test_root_level_test_file_matches_the_anchored_glob(self):
        # `**/test_*.py` must also match a ROOT-level test_x.py (the
        # `**/`-anchor special case in gitops._match) at plan time
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["test_x.py"]}], expect=1)
        self.assertIn("coverage-backfill", out["error"])

    def test_empty_files_list_refused_like_missing(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": []}], expect=1)
        self.assertIn("no `files` manifest", out["error"])

    def test_whitespace_only_files_entry_refused(self):
        out = self._register([{"id": "T1", "repo": str(self.repo),
                               "test_intents": ["test_x"],
                               "files": ["   "]}], expect=1)
        self.assertIn("non-empty path strings", out["error"])

    def test_persisted_files_are_normalized(self):
        # ONE normal form judged and stored: forward slashes, ./ collapsed
        self._register([{"id": "T1", "repo": str(self.repo),
                         "test_intents": ["test_x"],
                         "files": ["src\\x.py", "./tests/test_x.py"]}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["tasks"][0]["files"],
                         ["src/x.py", "tests/test_x.py"])

    def test_test_only_reason_without_intents_normalized_away(self):
        # the reason means nothing on a zero-test task — the exact mirror
        # of the stale-no_test_reason rule
        self._register([{"id": "T1", "repo": str(self.repo),
                         "files": ["docs/notes.md"],
                         "test_only_reason": "stale label"}])
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertIsNone(st["tasks"][0]["test_only_reason"])
        events = ndjson.read_records(self.run_dir / "events.ndjson")
        self.assertFalse([e for e in events
                          if e.get("kind") == "tests-without-production"])


class LeanModeEntry(BreadthHarness):
    """Mode selection is declared data end to end: the work-item hint (or
    the workspace default_mode) mints a classify verdict; the manifest's
    selects_mode mapping alone turns it into the lean mode."""

    def test_lean_hint_selects_lean_mode(self):
        self.story("W-99", "routine change")
        stories_file = self.stories / "W-99.md"
        stories_file.write_text(stories_file.read_text(encoding="utf-8")
                                .replace("## Description\n",
                                         "## Description\nMode: lean\n"))
        self.init()
        out = self.cli("fetch", "--id", "W-99", "--date", "2026-02-01")
        self.assertEqual(out["mode"], "lean")

    def test_workspace_default_mode_lean(self):
        self.story("W-98", "no hints at all")
        self.init()
        (self.workspace / ".claude" / "context" / "mode-override.yaml"
         ).write_text("default_mode: lean\n")
        out = self.cli("fetch", "--id", "W-98", "--date", "2026-02-01")
        self.assertEqual(out["mode"], "lean")
        # and the ledger records the verdict, not just the mode
        run = Path(out["run"])
        fetched = next(e for e in ndjson.read_records(run / "events.ndjson")
                       if e["kind"] == "fetched")
        self.assertEqual(fetched["mode_verdict"], "lean-requested")
        self.assertEqual(fetched["classify_reason"], "workspace default_mode")

    def test_full_hint_escapes_a_lean_workspace_default(self):
        self.story("W-97", "risky payments change")
        f = self.stories / "W-97.md"
        f.write_text(f.read_text(encoding="utf-8")
                     .replace("## Description\n", "## Description\nMode: full\n"))
        self.init()
        (self.workspace / ".claude" / "context" / "mode-override.yaml"
         ).write_text("default_mode: lean\n")
        out = self.cli("fetch", "--id", "W-97", "--date", "2026-02-01")
        self.assertEqual(out["mode"], "full")

    def test_outcome_artifact_is_engine_owned(self):
        self.story("W-96", "outcome forgery attempt")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-96",
                            "--date", "2026-02-01")["run"])
        out = self.cli("artifact", "--name", "plan-review.outcome",
                       "--value", "approved", run=run, expect=1)
        self.assertIn("engine-recorded", out["error"])


class LeanWalk(BreadthHarness):
    def test_lean_walk_one_gate_to_metrics(self):
        """Lean end-to-end through the real CLI: the exception gate
        self-skips on an approved panel (ledgered), develop flows into
        harden with no impl gate, and the run's only human decision is
        approve-pre-pr."""
        self.story("W-95", "lean end to end")
        f = self.stories / "W-95.md"
        f.write_text(f.read_text(encoding="utf-8")
                     .replace("## Description\n", "## Description\nMode: lean\n"))
        self.init()
        out = self.cli("fetch", "--id", "W-95", "--date", "2026-02-01")
        self.assertEqual(out["mode"], "lean")
        run = Path(out["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        (run / "plan.md").write_text("# Plan\n## T1\n")
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.pass_plan_review(run)                     # panel APPROVED
        self.cli("cursor", "--to", "preflight", run=run)   # gate self-skipped
        skipped = [e for e in ndjson.read_records(run / "events.ndjson")
                   if e["kind"] == "gate-skipped"]
        self.assertEqual([e["step"] for e in skipped], ["approve-plan-lean"])
        self.cli("preflight", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "develop", run=run)
        self._force_tasks_done(run)
        self.cli("cursor", "--to", "harden", run=run)  # no impl gate in lean
        self.cli("cursor", "--to", "security", run=run)
        self.assertEqual(self.cli("security-scan", run=run)["max_severity"],
                         "info")
        self.cli("cursor", "--to", "pre-pr", run=run)  # security gate skipped
        (run / "reports").mkdir(exist_ok=True)
        (run / "reports" / "pre-pr.md").write_text("# Pre-PR\nok\n")
        self.cli("cursor", "--to", "approve-pre-pr", run=run)
        self.gate(run, "approve-pre-pr")               # THE one human gate
        self.cli("cursor", "--to", "create-pr", run=run)
        self.cli("create-pr", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "reconcile", run=run)
        self.cli("reconcile", run=run)
        self.cli("cursor", "--to", "metrics", run=run)
        self.cli("metrics", run=run)
        self.cli("verify", run=run)                    # chain intact
        state = self.cli("show", run=run)["state"]
        self.assertNotIn("approve-impl",
                         state["cursor"]["completed_steps"])
        decided = [g for g, v in state["gates"].items()
                   if v.get("decision") or v.get("consumed_decision")]
        self.assertEqual(decided, ["approve-pre-pr"])


class ScopeRegisterValidation(BreadthHarness):
    """The human-confirmed target-repo set is an owned entry point with
    fail-closed validation — never a prose convention the planner could
    drift past (plan-register's scope containment reads what this records)."""

    def _fetch(self, sid="W-98", **init_kw):
        self.story(sid, "scope thing")
        self.init(**init_kw)
        return Path(self.cli("fetch", "--id", sid,
                             "--date", "2026-02-01")["run"])

    def test_refused_outside_intake_or_plan(self):
        run = self._fetch()
        out = self.cli("scope-register", "--repos-json",
                       json.dumps([str(self.repo)]), run=run, expect=1)
        # legality is DERIVED from the manifest's `produces: scope` steps,
        # never a hardcoded step list
        self.assertIn("a step the manifest declares producing 'scope' "
                      "(intake, plan)", out["error"])

    def test_unregistered_path_refused(self):
        run = self._fetch()
        self.cli("cursor", "--to", "intake", run=run)
        out = self.cli("scope-register", "--repos-json",
                       json.dumps(["/not/registered"]), run=run, expect=1)
        self.assertIn("not registered", out["error"])

    def test_empty_or_malformed_payload_refused(self):
        run = self._fetch()
        self.cli("cursor", "--to", "intake", run=run)
        out = self.cli("scope-register", "--repos-json", "[]",
                       run=run, expect=1)
        self.assertIn("non-empty", out["error"])
        out = self.cli("scope-register", "--repos-json", '"just-a-string"',
                       run=run, expect=1)
        self.assertIn("non-empty", out["error"])
        out = self.cli("scope-register", "--repos-json", "not json",
                       run=run, expect=1)
        self.assertIn("not valid JSON", out["error"])

    def test_records_scope_artifact_state_and_event(self):
        run = self._fetch()
        self.cli("cursor", "--to", "intake", run=run)
        out = self.cli("scope-register", "--repos-json",
                       json.dumps([str(self.repo)]), run=run)
        self.assertEqual(out["scope"], [str(self.repo)])
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["scope"]["repos"], [str(self.repo)])
        self.assertEqual(state["artifacts"]["scope"], [str(self.repo)])
        kinds = [r["kind"] for r in
                 ndjson.read_records(run / "events.ndjson")]
        self.assertIn("scope-registered", kinds)

    def test_generic_artifact_verb_cannot_write_scope(self):
        run = self._fetch()
        self.cli("cursor", "--to", "intake", run=run)
        out = self.cli("artifact", "--name", "scope", "--value",
                       json.dumps([str(self.repo)]), run=run, expect=1)
        self.assertIn("scope-register", out["error"])

    def test_narrowing_below_registered_tasks_refused(self):
        # containment is an invariant, not a point-in-time check: a
        # re-registration must not strand already-registered tasks
        repo_b = make_repo(self.workspace, "repo-scope-b")
        run = self._fetch(extra_repos=f"repo-scope-b={repo_b}",
                          extra_test_cmd=f"repo-scope-b={TEST_CMD}")
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("scope-register", "--repos-json",
                 json.dumps([str(self.repo), str(repo_b)]), run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.cli("plan-register", "--tasks-json", json.dumps([
            {"id": "T1", "repo": str(self.repo)},
            {"id": "T2", "repo": str(repo_b)}]), run=run)
        out = self.cli("scope-register", "--repos-json",
                       json.dumps([str(self.repo)]), run=run, expect=1)
        self.assertIn("would fall outside the new scope", out["error"])

    def test_task_less_stall_counts_per_step(self):
        run = self._fetch()
        self.assertEqual(self.cli("stall", run=run)["action"], "reinvoke")
        self.assertEqual(self.cli("stall", run=run)["action"], "recovery")
        self.assertEqual(self.cli("stall", run=run)["action"], "human")
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["step_stalls"], {"step:fetch": 3})

    def test_reregistration_at_plan_replaces_the_set(self):
        # The plan step's 0c escape valve: widening is legal (full-set
        # replace), silent widening is not — plan-register reads the result.
        repo_b = make_repo(self.workspace, "repo-b")
        run = self._fetch(extra_repos=f"repo-b={repo_b}",
                          extra_test_cmd=f"repo-b={TEST_CMD}")
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("scope-register", "--repos-json",
                 json.dumps([str(self.repo)]), run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.cli("scope-register", "--repos-json",
                 json.dumps([str(self.repo), str(repo_b)]), run=run)
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["scope"]["repos"],
                         sorted([str(self.repo), str(repo_b)]))


class GateOptionsAreDeclaredData(BreadthHarness):
    """Guarantee-seam regression: the option list a numbered human reply
    resolves against is manifest-declared (dispositions) or sealed at
    --present (select gates) — never a caller flag at --decide, which let a
    drifting orchestrator record the human's '1' as a different option."""

    def setUp(self):
        super().setUp()
        self.story("W-70", "Gate seam")
        self.init()
        self.run_dir = Path(
            self.cli("fetch", "--id", "W-70", "--date", "2026-02-01")["run"])

    def test_decide_refuses_caller_options(self):
        self.cli("gate", "--id", "approve-plan", "--present", run=self.run_dir)
        ndjson.append_record(self.run_dir / "human-input.ndjson", {"text": "1"})
        out = self.cli("gate", "--id", "approve-plan", "--decide",
                       "--options", "rejected,approved", run=self.run_dir,
                       expect=1)
        self.assertIn("never legal at --decide", out["error"])

    def test_binary_gate_options_come_from_manifest_dispositions(self):
        out = self.cli("gate", "--id", "approve-security", "--present",
                       "--options", "a,b", run=self.run_dir, expect=1)
        self.assertIn("only for select gates", out["error"])
        self.cli("gate", "--id", "approve-security", "--present", run=self.run_dir)
        ndjson.append_record(self.run_dir / "human-input.ndjson", {"text": "2"})
        self._force_cursor(self.run_dir, "approve-security")  # decide is cursor-anchored
        self.cli("gate", "--id", "approve-security", "--decide", run=self.run_dir)
        st = state_mod.load(self.run_dir, self.workspace)
        # manifest dispositions: [fix-now, waive, defer] -> "2" is waive
        self.assertEqual(st["gates"]["approve-security"]["decision"], "waive")
        self.assertEqual(st["gates"]["approve-security"]["options"],
                         ["fix-now", "waive", "defer"])

    def test_select_gate_candidates_sealed_at_present(self):
        out = self.cli("gate", "--id", "select-comments", "--present",
                       run=self.run_dir, expect=1)
        self.assertIn("needs --options at --present", out["error"])
        self.cli("gate", "--id", "select-comments", "--present",
                 "--options", "c1,c2,c3", run=self.run_dir)
        ndjson.append_record(self.run_dir / "human-input.ndjson", {"text": "2,3"})
        self._force_cursor(self.run_dir, "select-comments")  # decide is cursor-anchored
        self.cli("gate", "--id", "select-comments", "--decide", run=self.run_dir)
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["gates"]["select-comments"]["decision"], ["c2", "c3"])

    def test_non_gate_step_refused(self):
        out = self.cli("gate", "--id", "fetch", "--present", run=self.run_dir,
                       expect=1)
        self.assertIn("not a declared gate step", out["error"])

    def test_decide_away_from_the_gate_refused(self):
        # adversarial-review (plan-accuracy round): an any-cursor decide
        # could bank an approval before the gate's artifacts exist, or move
        # the verdict window mid-plan-cycle and reset the review budget.
        self.cli("gate", "--id", "approve-plan", "--present", run=self.run_dir)
        ndjson.append_record(self.run_dir / "human-input.ndjson",
                             {"text": "APPROVED"})
        out = self.cli("gate", "--id", "approve-plan", "--decide",
                       run=self.run_dir, expect=1)   # cursor is at fetch
        self.assertIn("not the current step", out["error"])


class AutosquashMultiRepo(BreadthHarness):
    def test_autosquash_scopes_sha_lookups_to_the_target_repo(self):
        """merge-task --autosquash built its SHA->subject map from EVERY
        task in state.yaml, so on a multi-repo run it ran
        `git log <sibling-repo-sha>` in the target repo and crashed. The
        map now filters by the task's own `repo` field."""
        repo_b = make_repo(self.workspace, name="repo-b")
        self.story("W-77", "Two repos")
        self.init(extra_repos=f"repo-b={repo_b}")
        run = Path(self.cli("fetch", "--id", "W-77", "--date", "2026-02-02")["run"])
        # task commit on a feature branch above main (find_commit_by_subject
        # re-derives within base..HEAD), on the TARGET repo
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "a.txt").write_text("a\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "chore: #W T1 work")
        sha_a = gitops.run_git(self.repo, "rev-parse", "HEAD")
        # the sibling repo's task SHA — unknown to self.repo by construction
        sha_b = gitops.run_git(repo_b, "rev-parse", "HEAD")
        st = state_mod.load(run, self.workspace)
        st["tasks"] = [
            {**st["tasks"][0], "id": "T1", "repo": str(self.repo),
             "commit_sha": sha_a},
            {**st["tasks"][0], "id": "T2", "repo": str(repo_b),
             "commit_sha": sha_b},
        ]
        state_mod.save(run, self.workspace, st)
        # pre-fix this crashed resolving T2's (repo-b) SHA inside self.repo
        self.cli("merge-task", "--repo", str(self.repo), "--autosquash",
                 "--base", "main", run=run)
        tasks = {t["id"]: t for t in self.cli("show", run=run)["state"]["tasks"]}
        self.assertEqual(tasks["T1"]["commit_sha"], sha_a)  # re-derived, same
        self.assertEqual(tasks["T2"]["commit_sha"], sha_b)  # untouched


class ManualPrRecord(BreadthHarness):
    def test_create_pr_url_records_without_provider_call(self):
        """A reverse proxy in front of a self-hosted GitLab can 404 every
        path-encoded project lookup, so `glab mr create` can't resolve the
        project even though pushes and numeric-ID reads work — the human
        creates the MR by hand and the run has no way to record it (no
        override, and hand-editing state.yaml is blocked for everyone).
        `--url` records it through the same owned entry point."""
        self.story("W-88", "Manual MR")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-88", "--date", "2026-02-03")["run"])
        st = state_mod.load(run, self.workspace)
        st["cursor"]["current_step"] = "create-pr"   # the declaring step
        state_mod.save(run, self.workspace, st)
        url = "https://git.example.com/grp/proj/-/merge_requests/12"
        out = self.cli("create-pr", "--repo", str(self.repo), "--url", url,
                       run=run)
        self.assertEqual(out["id"], "12")            # comment-loop id derived
        self.assertTrue(out["manual"])
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["artifacts"]["pr"]["repo"]["url"], url)
        kinds = [e["kind"] for e in ndjson.read_records(run / "events.ndjson")]
        self.assertIn("pr-recorded-manually", kinds)  # audit: no provider call

    def test_manual_pr_url_must_end_in_the_number(self):
        # fetch-pr-comments derives the PR/MR id from the URL tail — a
        # project-page URL would break the comment loop later, refuse now
        self.story("W-89", "Manual MR bad url")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-89", "--date", "2026-02-04")["run"])
        st = state_mod.load(run, self.workspace)
        st["cursor"]["current_step"] = "create-pr"
        state_mod.save(run, self.workspace, st)
        out = self.cli("create-pr", "--repo", str(self.repo), "--url",
                       "https://git.example.com/grp/proj", run=run, expect=1)
        self.assertIn("ending in", out["error"])


class PublishMirrorPush(BreadthHarness):
    def test_publish_mirror_push_lands_the_snapshot_on_the_remote(self):
        """create-pr.md's sequence is push → create-pr → publish-mirror,
        and nothing ever pushed again — every run ended with its final
        audit snapshot stranded exactly one commit ahead of the remote,
        invisible to the PR reviewer. `--push` closes the loop through
        the same owned push machinery."""
        self.story("W-90", "Push the snapshot")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-90", "--date", "2026-02-05")["run"])
        bare = self.workspace / "origin.git"
        gitops.run_git(self.workspace, "init", "--bare", "origin.git")
        gitops.run_git(self.repo, "remote", "add", "origin", str(bare))
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        gitops.push_branch(self.repo, "feature")
        out = self.cli("publish-mirror", "--repo", str(self.repo), "--push",
                       run=run)
        self.assertEqual(out["pushed"], "feature")
        self.assertEqual(gitops.run_git(bare, "rev-parse", "feature"),
                         gitops.run_git(self.repo, "rev-parse", "HEAD"))
        # without --push (the develop-loop publishes, pre-remote-branch):
        # unchanged behavior, no push attempted, no `pushed` key
        (run / "notes.md").write_text("delta\n")
        out = self.cli("publish-mirror", "--repo", str(self.repo), run=run)
        self.assertNotIn("pushed", out)


class RejectionWithNotes(BreadthHarness):
    def test_cli_decides_leading_rejected_with_notes(self):
        """Field (session D, approve-plan): the CLI must wire the
        manifest's forward_on into the lenient set — 'REJECTED — <notes>'
        decides as rejected and the on_reject edge opens; 'APPROVED but…'
        still refuses (forward stays bare)."""
        self.story("W-96", "notes ride the rejection")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-96",
                            "--date", "2026-03-02")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("gate", "--id", "approve-plan", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson",
                             {"text": "APPROVED but rename T1 first"})
        out = self.cli("gate", "--id", "approve-plan", "--decide", run=run,
                       expect=1)
        self.assertIn("FORWARD", out["error"])
        self.cli("gate", "--id", "approve-plan", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson",
                             {"text": "REJECTED — split T1 into two tasks"})
        self.cli("gate", "--id", "approve-plan", "--decide", run=run)
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["gates"]["approve-plan"]["decision"],
                         "rejected")
        self.cli("cursor", "--to", "plan", run=run)   # on_reject edge opens
        # the rejection was CONSUMED by that edge (single-use): it stays on
        # the dashboard as history, and the re-armed registration plus a
        # fresh verdict window govern the new cycle
        state = self.cli("show", run=run)["state"]
        self.assertNotIn("decision", state["gates"]["approve-plan"])
        self.assertEqual(
            state["gates"]["approve-plan"]["consumed_decision"], "rejected")
        self.assertTrue(all(t["provisional"] for t in state["tasks"]))


class DeferFollowThrough(BreadthHarness):
    def test_defer_decision_returns_follow_up_and_flags_pending(self):
        """Field (session D, approve-security — an audit near-miss): the
        follow-through happened correctly 43s after the decide, but an
        audit snapshot inside that window was indistinguishable from a
        silent drop (the obligation was prose-only, the ledger silent).
        The follow-up now rides the decide RESULT and a flagged
        deferral-pending event lands WITH the decision, pairable with a
        deferral-recorded event — in-flight, done, and dropped become
        three distinguishable ledger states."""
        self.story("W-97", "defer follow-through")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-97",
                            "--date", "2026-03-03")["run"])
        self._force_cursor(run, "approve-security")  # decide is cursor-anchored
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        out = self.cli("gate", "--id", "approve-security", "--decide",
                       run=run)
        self.assertEqual(out["decision"], "defer")
        self.assertIn("work_item.create", out["follow_up"])
        kinds = [json.loads(line)["kind"] for line in
                 (run / "events.ndjson").read_text(encoding="utf-8").splitlines()]
        self.assertIn("deferral-pending", kinds)
        report = Path(self.cli("metrics", run=run)["report"])
        self.assertIn("deferral-pending", report.read_text(encoding="utf-8"))
        # non-defer decisions carry no follow_up and flag nothing
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "waive"})
        out = self.cli("gate", "--id", "approve-security", "--decide",
                       run=run)
        self.assertEqual(out["decision"], "waive")
        self.assertNotIn("follow_up", out)

    def test_recorded_deferral_clears_from_flagged_events_in_status_and_metrics(self):
        """Validation-walk F5: a `deferral-pending` is flagged, but a matching
        `deferral-recorded` RESOLVES it — status.flagged_events and metrics
        must count only OUTSTANDING deferrals (a live gauge, not a permanent
        tally), and the two must AGREE (they share `outstanding_flagged`, the
        same invariant FLAGGED_EVENT_KINDS protects)."""
        self.story("W-96", "defer clears")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-96",
                            "--date", "2026-03-05")["run"])
        self._force_cursor(run, "approve-security")  # decide is cursor-anchored
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        self.cli("gate", "--id", "approve-security", "--decide", run=run)

        def _status_flagged():
            return next(r for r in self.cli("status")["runs"]
                        if r["run"] == run.name)["flagged_events"]

        def _metrics_flagged():
            text = Path(self.cli("metrics", run=run)["report"]).read_text(encoding="utf-8")
            return int(text.split("## Flagged events (")[1].split(")")[0])

        before = _status_flagged()
        self.assertGreaterEqual(before, 1)                # the pending is owed
        self.assertEqual(before, _metrics_flagged())      # status == metrics

        # record the follow-up work item -> pairs the pending -> resolved
        self.cli("log-event", "--json",
                 json.dumps({"kind": "deferral-recorded", "item": "FU-1"}),
                 run=run)
        after = _status_flagged()
        self.assertEqual(after, before - 1)               # no longer counted
        self.assertEqual(after, _metrics_flagged())       # still consistent

    def test_spurious_deferral_recorded_does_not_under_count_the_gauge(self):
        """Review finding (F5 fail-closed): a `deferral-recorded` with no open
        `deferral-pending` ahead of it (spurious / duplicate / out-of-order —
        `log-event` is unvalidated) resolves nothing. It must NOT silently hide
        a genuinely-outstanding deferral by decrementing the count."""
        self.story("W-94", "spurious recorded")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-94",
                            "--date", "2026-03-06")["run"])
        # a stray deferral-recorded BEFORE any defer — nothing open to resolve
        self.cli("log-event", "--json",
                 json.dumps({"kind": "deferral-recorded", "item": "STRAY"}),
                 run=run)
        self._force_cursor(run, "approve-security")  # decide is cursor-anchored
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        self.cli("gate", "--id", "approve-security", "--decide", run=run)
        flagged = next(r for r in self.cli("status")["runs"]
                       if r["run"] == run.name)["flagged_events"]
        self.assertGreaterEqual(flagged, 1)   # the real pending is still owed


class RunHealth(BreadthHarness):
    """The process-health verdict (field 459226 rec: a run 'completed
    green' over 11 flagged events and 2 stalls, visible only via manual
    post-mortem). One shared rule — workflow.run_health — read by both
    `status` and metrics' leading '## Run health' section."""

    def setUp(self):
        super().setUp()
        self.story("W-93", "health")
        self.init()
        self.run_dir = Path(self.cli("fetch", "--id", "W-93",
                                     "--date", "2026-03-07")["run"])

    def _status_health(self):
        return next(r for r in self.cli("status")["runs"]
                    if r["run"] == self.run_dir.name)["health"]

    def _metrics_text(self):
        return Path(self.cli("metrics", run=self.run_dir)["report"]).read_text(
            encoding="utf-8")

    def test_fresh_run_reads_healthy_in_status_and_metrics(self):
        self.assertEqual(self._status_health(), "HEALTHY")
        text = self._metrics_text()
        self.assertIn("## Run health", text)
        self.assertIn("**HEALTHY**", text)

    def test_degrading_event_flips_both_surfaces(self):
        ndjson.append_record(self.run_dir / "events.ndjson", {
            "kind": "missing-status-block", "task": "T1", "actor": "planner"})
        self.assertEqual(self._status_health(), "DEGRADED")
        text = self._metrics_text()
        self.assertIn("**DEGRADED**", text)
        self.assertIn("missing-status-block: 1", text)

    def test_every_declared_degrading_kind_flips_the_verdict(self):
        # mutation-proofing (adversarial review of this change: shrinking
        # the tuple passed the suite): pin the exact declared contents AND
        # that each kind flips the shared rule
        from harness.workflow import HEALTH_DEGRADING_KINDS, run_health
        self.assertEqual(set(HEALTH_DEGRADING_KINDS),
                         {"missing-status-block", "verdict-uncaptured",
                          "background-spawn-uncaptured"})
        for kind in HEALTH_DEGRADING_KINDS:
            verdict, counts = run_health([{"kind": kind}])
            self.assertEqual((verdict, counts), ("DEGRADED", {kind: 1}), kind)

    def test_engaged_stall_procedure_degrades_without_any_event(self):
        """Adversarial review of this change (both lenses): the stall
        procedure can engage on paths that write no capture event at all
        (hook attribution failure, a hung spawn with no PostToolUse
        payload) — the verdict reads the state counters the stall verb
        writes, so those stalls still degrade the run instead of hiding
        behind a green completion."""
        self.cli("stall", run=self.run_dir)   # task-less: step-keyed counter
        self.assertEqual(self._status_health(), "DEGRADED")
        text = self._metrics_text()
        self.assertIn("**DEGRADED**", text)
        self.assertIn("stalls: 1", text)

    def test_flagged_but_healthy_kinds_never_degrade(self):
        # the exclusions are the point: a captured-verdict formatting slip,
        # a declared gate self-skip, and a recorded zero-test decision are
        # all flagged for humans yet leave the machinery healthy — the
        # fork's field runs read DEGRADED on every run until this split
        for kind in ("status-block-malformed", "gate-skipped",
                     "risk-without-tests"):
            ndjson.append_record(self.run_dir / "events.ndjson",
                                 {"kind": kind, "task": "T1"})
        self.assertEqual(self._status_health(), "HEALTHY")
        text = self._metrics_text()
        self.assertIn("**HEALTHY**", text)
        self.assertIn("status-block-malformed (non-degrading): 1", text)
        # a degrading kind must always also be human-visible
        from harness.workflow import (FLAGGED_EVENT_KINDS,
                                      HEALTH_DEGRADING_KINDS)
        for kind in HEALTH_DEGRADING_KINDS:
            self.assertIn(kind, FLAGGED_EVENT_KINDS)


class LensResolution(BreadthHarness):
    """Per-change_type plan-review panels (field 459226 rec #3: lean ran
    the full adversarial panel, full round budget, for an all-low-risk
    chore). One resolution rule — workflow.resolve_lenses — surfaced by
    the resolve-lenses verb the orchestrator calls."""

    def test_resolve_lenses_change_type_mapping(self):
        from harness.workflow import resolve_lenses
        cfg = {"plan_review": {"lenses": ["contradictions", "gaps"],
                               "lenses_by_change_type": {"chore": [],
                                                         "fix": ["gaps"]}}}
        self.assertEqual(resolve_lenses(cfg, "chore"), [])       # mapped empty
        self.assertEqual(resolve_lenses(cfg, "fix"), ["gaps"])   # mapped override
        self.assertEqual(resolve_lenses(cfg, "feature"),         # unmapped →
                         ["contradictions", "gaps"])             # full default
        self.assertEqual(resolve_lenses({}, "feature"),          # no config →
                         ["contradictions", "gaps"])             # shipped pair

    def test_resolve_lenses_cli_reads_the_runs_change_type(self):
        self.story("W-92", "lenses")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-92",
                            "--date", "2026-03-08")["run"])
        st = state_mod.load(run, self.workspace)
        out = self.cli("resolve-lenses", run=run)
        self.assertEqual(out["change_type"], st["change_type"])
        expected = ([] if st["change_type"] in ("chore", "docs")
                    else ["contradictions", "gaps"])
        self.assertEqual(out["lenses"], expected)


class AbortRefetchSameDay(BreadthHarness):
    def test_same_day_abort_then_refetch_bootstraps_a_fresh_slot(self):
        """Field (session D phase 0, verbatim sequence): fetch → collision
        drill → abort → re-fetch THE SAME DAY. The exact-path collision
        check was existence-only, so the re-fetch refused with 'a live run
        already exists' about a run whose own state recorded the abort."""
        self.story("W-95", "abort decoy")
        self.init()
        run1 = Path(self.cli("fetch", "--id", "W-95",
                             "--date", "2026-03-01")["run"])
        # live occupant: same-day refetch still collides (drill D7)
        out = self.cli("fetch", "--id", "W-95", "--date", "2026-03-01",
                       expect=1)
        self.assertIn("Resume or Abort", out["error"])
        self.cli("abort", "--reason", "session D abort drill", run=run1)
        run2 = Path(self.cli("fetch", "--id", "W-95",
                             "--date", "2026-03-01")["run"])
        self.assertEqual(run2.name, f"{run1.name}-2")   # fresh slot, same day
        self.cli("abort", "--reason", "second drill", run=run2)
        run3 = Path(self.cli("fetch", "--id", "W-95",
                             "--date", "2026-03-01")["run"])
        self.assertEqual(run3.name, f"{run1.name}-3")


class SecretSweepBreadth(BreadthHarness):
    def test_preflight_pins_exclude_and_commit_backstop_flags_event(self):
        """A stray integrity key inside a repo checkout can be swept into
        a task commit by `harness commit`'s own `git add -A` — surfacing
        review rounds later as a dangling secret-bearing commit needing an
        object-level scrub. Preflight now pins `.harness-key` into
        .git/info/exclude; a repo preflighted by an OLDER version hits the
        commit-verb backstop, which refuses, unstages, and logs a
        dashboard-flagged event."""
        self.story("W-91", "sweep-proof")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-91", "--date", "2026-02-21")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        self.scope(run)
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.pass_plan_review(run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.gate(run, "approve-plan")   # decided AT the gate (cursor-anchored)
        self.cli("cursor", "--to", "preflight", run=run)
        self.cli("preflight", "--repo", str(self.repo), run=run)
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertIn(".harness-key", exclude.read_text(encoding="utf-8"))

        exclude.write_text("")            # simulate a pre-0.16.12 preflight
        key = self.repo / ".claude" / "context" / ".harness-key"
        key.parent.mkdir(parents=True)
        key.write_text("stray\n")
        (self.repo / "w.txt").write_text("work\n")
        out = self.cli("commit", "--repo", str(self.repo), "--task-id", "T1",
                       "--summary", "sweep attempt", run=run, expect=1)
        self.assertIn("integrity key", out["error"])
        kinds = [json.loads(line)["kind"] for line in
                 (run / "events.ndjson").read_text(encoding="utf-8").splitlines()]
        self.assertIn("secret-sweep-blocked", kinds)
        report = Path(self.cli("metrics", run=run)["report"])
        self.assertIn("secret-sweep-blocked", report.read_text(encoding="utf-8"))


class WorkspaceResolution(BreadthHarness):
    def _raw(self, *args, cwd, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "harness", *args], cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"{args} -> {payload} {proc.stderr}")
        return payload

    def test_workspace_derived_from_run_despite_drifted_cwd(self):
        """An absolute --run with --workspace omitted used cwd as the
        workspace — a shell cwd drifted into a repo minted a stray key
        there and reported the genuinely-sealed state.yaml as 'integrity
        seal mismatch' (forensics for a phantom). Runs live at
        <workspace>/ai/<name> by construction, so --run names its own
        workspace."""
        self.story("W-91", "cwd drift")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-91", "--date", "2026-02-06")["run"])
        out = self._raw("--run", str(run), "show", cwd=self.repo)  # cwd = a repo!
        self.assertEqual(out["state"]["work_item"]["id"], "W-91")
        # and no stray key was minted in the repo
        self.assertFalse((self.repo / ".claude").exists())

    def test_missing_key_refused_loudly_never_minted(self):
        """Read paths no longer mint keys: an explicitly-wrong --workspace
        gets a pointed 'wrong --workspace?' error (exit 3), not a random
        fresh key plus a phantom tamper alarm."""
        self.story("W-92", "strict key")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-92", "--date", "2026-02-07")["run"])
        wrong = self.workspace / "not-a-workspace"
        wrong.mkdir()
        out = self._raw("--workspace", str(wrong), "--run", str(run), "show",
                        cwd=self.workspace, expect=3)
        self.assertIn("no integrity key", out["error"])
        self.assertIn("wrong --workspace", out["error"])
        self.assertFalse((wrong / ".claude").exists())   # nothing minted


class MetricsRendering(unittest.TestCase):
    """The report's pure formatting helpers — the tables themselves are
    asserted in the full walk (FullModeWalk) against a real run."""

    def test_duration_formatting(self):
        from harness.workflow import _fmt_duration as d
        self.assertEqual(d("2026-01-01T10:00:00+00:00",
                           "2026-01-01T14:41:03+00:00"), "4h 41m")
        self.assertEqual(d("2026-01-01T10:00:00+00:00",
                           "2026-01-01T10:12:05+00:00"), "12m 05s")
        self.assertEqual(d("2026-01-01T10:00:00+00:00",
                           "2026-01-01T10:00:08+00:00"), "8s")
        self.assertEqual(d("2026-01-01T10:00:00+00:00", None), "running")
        self.assertEqual(d(None, None), "—")

    def test_cell_escaping_keeps_a_paragraph_reason_in_one_row(self):
        # a hook-blocked reason is multi-line prose with pipes — it must
        # not break the GFM row it lands in
        from harness.workflow import _md_cell
        self.assertEqual(_md_cell("a | b\nand a\nnew line"),
                         "a \\| b and a new line")
        self.assertEqual(_md_cell(None), "—")


if __name__ == "__main__":
    unittest.main()
