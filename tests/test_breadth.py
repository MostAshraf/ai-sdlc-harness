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

ROOT = Path(__file__).resolve().parent.parent


class BreadthHarness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.stories = self.workspace / "stories"
        self.stories.mkdir()
        self.repo = make_repo(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace)

    def story(self, sid, title, body="", type_="Bug"):
        (self.stories / f"{sid}.md").write_text(
            f"# {sid}: {title}\nType: {type_}\nStatus: Open\n\n"
            f"## Description\n{body}\n\n## Acceptance Criteria\n- [ ] works\n")

    def cli(self, *args, run=None, expect=0):
        cmd = [sys.executable, "-m", "harness", "--workspace", str(self.workspace)]
        if run:
            cmd += ["--run", str(run)]
        proc = subprocess.run([*cmd, *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
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


class FullWalk(BreadthHarness):
    def test_full_manifest_end_to_end(self):
        self.story("W-10", "Fix parser crash")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-10", "--date", "2026-02-01")["run"])

        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        (run / "plan.md").write_text("# Plan\n## T1\n")
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.gate(run, "approve-plan")
        self.cli("cursor", "--to", "approve-plan", run=run)
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
        self.assertIn("Status: Done", (self.stories / "W-10.md").read_text())
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
        text = report.read_text()
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
                  (run / "events.ndjson").read_text().splitlines()]
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
        self.assertIn("Status: In Progress", (self.stories / "W-51.md").read_text())

    def test_in_review_is_a_noop_by_shipped_default(self):
        self.story("W-52", "thing", type_="Bug")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-52", "--date", "2026-02-13")["run"])
        out = self.cli("write-back", "--milestone", "in_review", run=run)
        self.assertEqual(out["written"], False)
        self.assertNotIn("Status: In Review", (self.stories / "W-52.md").read_text())


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
        original = json.loads((run / "work-item.json").read_text())
        self.assertEqual(original["title"], "Original title")

        self.story("W-70", "Changed title")   # source ticket edited
        out = self.cli("fetch", "--id", "W-70", "--date", "2026-02-17", expect=1)
        self.assertIn("Resume or Abort", out["error"])

        after = json.loads((run / "work-item.json").read_text())
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
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.gate(run, "approve-plan")
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("cursor", "--to", "preflight", run=run)
        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.cli("cursor", "--to", "develop", run=run)

        wt = self.cli("worktree-add", "--repo", str(self.repo),
                      "--task-id", "T1", "--base", branch, run=run)
        shutil.rmtree(wt["path"])   # simulate manual cleanup / crash

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
        (run / "plan.md").write_text("# Plan\n## T1\n")
        self.cli("plan-register",
                 "--tasks-json", json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                 run=run)
        self.gate(run, "approve-plan")
        self.cli("cursor", "--to", "approve-plan", run=run)
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
        report = (run / "reports" / "contracts.md").read_text()
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
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "test_intents": ["test_a", "test_b"]}]),
                 run=run)
        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["tasks"][0]["test_intents"], ["test_a", "test_b"])

    def test_plan_register_accepts_json_files(self):
        # File input avoids shell-quoting large payloads / space-containing
        # workspace paths that inline `$(cat …)` substitution is fragile around.
        self.story("W-23", "thing")
        self.init()
        run = Path(self.cli("fetch", "--id", "W-23", "--date", "2026-02-06")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
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
        self.cli("plan-register",
                 "--tasks-json", json.dumps([
                     {"id": "T1", "repo": str(self.repo)},
                     {"id": "T2", "repo": str(repo_b)}]),
                 run=run)
        self.gate(run, "approve-plan")
        self.cli("cursor", "--to", "approve-plan", run=run)
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
        for step in ("intake", "plan", "approve-plan", "preflight", "develop",
                     "approve-impl", "harden", "security"):
            if st["cursor"]["current_step"] == "security":
                break
            if step == "approve-plan":
                # leaving `plan` requires the registered (non-provisional)
                # task list — the requires_tasks_registered mechanization
                self.cli("plan-register", "--tasks-json",
                         json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                         run=run)
            if step in ("approve-plan", "approve-impl"):
                self.gate(run, step)
            if step == "approve-impl":
                self._force_tasks_done(run)
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
        for step in ("intake", "plan", "approve-plan", "preflight", "develop",
                     "approve-impl", "harden", "security"):
            if st["cursor"]["current_step"] == "security":
                break
            if step == "approve-plan":
                # leaving `plan` requires the registered (non-provisional)
                # task list — the requires_tasks_registered mechanization
                self.cli("plan-register", "--tasks-json",
                         json.dumps([{"id": "T1", "repo": str(self.repo)}]),
                         run=run)
            if step in ("approve-plan", "approve-impl"):
                self.gate(run, step)
            if step == "approve-impl":
                self._force_tasks_done(run)
            self.cli("cursor", "--to", step, run=run)
        sev = self.cli("security-scan", run=run)
        self.assertEqual(sev["max_severity"], "critical")
        report = (run / "reports" / "security.md").read_text()
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
        sf.write_text(sf.read_text() + "# tampered\n")
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
        self._register([{"id": "T1"}, {"id": "T2", "depends_on": ["T1"]}])


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
        self.cli("gate", "--id", "select-comments", "--decide", run=self.run_dir)
        st = state_mod.load(self.run_dir, self.workspace)
        self.assertEqual(st["gates"]["select-comments"]["decision"], ["c2", "c3"])

    def test_non_gate_step_refused(self):
        out = self.cli("gate", "--id", "fetch", "--present", run=self.run_dir,
                       expect=1)
        self.assertIn("not a declared gate step", out["error"])


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
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
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
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("cursor", "--to", "plan", run=run)   # on_reject edge opens


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
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        out = self.cli("gate", "--id", "approve-security", "--decide",
                       run=run)
        self.assertEqual(out["decision"], "defer")
        self.assertIn("work_item.create", out["follow_up"])
        kinds = [json.loads(line)["kind"] for line in
                 (run / "events.ndjson").read_text().splitlines()]
        self.assertIn("deferral-pending", kinds)
        report = Path(self.cli("metrics", run=run)["report"])
        self.assertIn("deferral-pending", report.read_text())
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
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        self.cli("gate", "--id", "approve-security", "--decide", run=run)

        def _status_flagged():
            return next(r for r in self.cli("status")["runs"]
                        if r["run"] == run.name)["flagged_events"]

        def _metrics_flagged():
            text = Path(self.cli("metrics", run=run)["report"]).read_text()
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
        self.cli("gate", "--id", "approve-security", "--present", run=run)
        ndjson.append_record(run / "human-input.ndjson", {"text": "defer"})
        self.cli("gate", "--id", "approve-security", "--decide", run=run)
        flagged = next(r for r in self.cli("status")["runs"]
                       if r["run"] == run.name)["flagged_events"]
        self.assertGreaterEqual(flagged, 1)   # the real pending is still owed


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
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.gate(run, "approve-plan")
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("cursor", "--to", "preflight", run=run)
        self.cli("preflight", "--repo", str(self.repo), run=run)
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertIn(".harness-key", exclude.read_text())

        exclude.write_text("")            # simulate a pre-0.16.12 preflight
        key = self.repo / ".claude" / "context" / ".harness-key"
        key.parent.mkdir(parents=True)
        key.write_text("stray\n")
        (self.repo / "w.txt").write_text("work\n")
        out = self.cli("commit", "--repo", str(self.repo), "--task-id", "T1",
                       "--summary", "sweep attempt", run=run, expect=1)
        self.assertIn("integrity key", out["error"])
        kinds = [json.loads(line)["kind"] for line in
                 (run / "events.ndjson").read_text().splitlines()]
        self.assertIn("secret-sweep-blocked", kinds)
        report = Path(self.cli("metrics", run=run)["report"])
        self.assertIn("secret-sweep-blocked", report.read_text())


class WorkspaceResolution(BreadthHarness):
    def _raw(self, *args, cwd, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "harness", *args], cwd=cwd,
            capture_output=True, text=True, timeout=120,
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
