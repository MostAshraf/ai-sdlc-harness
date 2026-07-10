"""M4 done-criterion — THE vertical slice, entirely through the real CLI:

a real work-item file goes fetch -> preflight -> develop (genuine red->green
under the owned entry points) -> human gate (real captured-input approval) ->
squashed task-commit, with the ledgers populated and the chain intact.
This test IS the orchestrator walk the dev-workflow skill renders in prose.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import gitops, ndjson
from tests.test_gitops import FAILING_TEST, TEST_CMD, make_repo

ROOT = Path(__file__).resolve().parent.parent

STORY = """# WORK-7: Fix null crash in parser
Type: Bug
Status: Open

## Description
Empty input makes the parser explode. val() must exist and return 1.

## Acceptance Criteria
- [ ] x.val() returns 1
"""


class VerticalSlice(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.stories = self.workspace / "stories"
        self.stories.mkdir()
        (self.stories / "WORK-7.md").write_text(STORY, encoding="utf-8")
        self.repo = make_repo(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace)

    def cli(self, *args, run=None, expect=0):
        cmd = [sys.executable, "-m", "harness", "--workspace", str(self.workspace)]
        if run:
            cmd += ["--run", str(run)]
        proc = subprocess.run([*cmd, *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=120)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"harness {' '.join(args)} -> {payload} {proc.stderr}")
        return payload

    def human_says(self, run: Path, text: str):
        """What the UserPromptSubmit hook (M3, tested) does in live use."""
        ndjson.append_record(run / "human-input.ndjson", {"text": text})

    def gate(self, run: Path, gate_id: str, reply: str = "APPROVED"):
        self.cli("gate", "--id", gate_id, "--present", run=run)
        self.human_says(run, reply)
        self.cli("gate", "--id", gate_id, "--decide", run=run)

    def _branches(self):
        return sorted(gitops.run_git(
            self.repo, "for-each-ref", "--format=%(refname:short)",
            "refs/heads/").split())

    def _walk_to_preflight(self):
        self.cli("init", "--stories-dir", str(self.stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        run = Path(self.cli("fetch", "--id", "WORK-7",
                            "--date", "2026-01-01")["run"])
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        (run / "plan.md").write_text(
            "# Plan\n## T1: return 1 from x.val()\nTest-intents: test_val\n")
        self.gate(run, "approve-plan")
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("cursor", "--to", "preflight", run=run)
        return run

    def test_preflight_off_step_refuses_without_creating_stray_branch(self):
        """Validation-walk F4: preflight run with the cursor NOT on a
        `branches`-producing step must refuse UP FRONT — creating no stray,
        unrecorded branch. It used to `checkout -b` first and only then fail
        inside set_artifact, orphaning a branch that then blocked the retry
        (its own `checkout -b` re-failing on the branch already existing)."""
        self.cli("init", "--stories-dir", str(self.stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        run = Path(self.cli("fetch", "--id", "WORK-7",
                            "--date", "2026-01-01")["run"])
        before = self._branches()   # cursor is at 'fetch' — not a branches step
        out = self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        self.assertIn("does not declare producing 'branches'", out["error"])
        self.assertEqual(self._branches(), before)   # NO stray branch created

    def test_preflight_adopts_existing_unrecorded_branch(self):
        """Validation-walk F4: a retry over a feature branch left by a crashed
        prior attempt (present in git, not recorded in state) ADOPTS it via a
        plain `checkout` instead of failing on `checkout -b`."""
        run = self._walk_to_preflight()
        branch = "fix/WORK-7-fix-null-crash-in-parser"
        gitops.run_git(self.repo, "checkout", "-b", branch)   # crashed attempt
        gitops.run_git(self.repo, "checkout", "main")         # branch left behind
        entry = self.cli("preflight", "--repo", str(self.repo), run=run)
        self.assertEqual(entry["branch"], branch)             # adopted, not re-cut
        self.assertEqual(gitops.run_git(
            self.repo, "rev-parse", "--abbrev-ref", "HEAD"), branch)

    def test_preflight_refuses_divergent_existing_branch(self):
        """Review finding: adopt must NOT reuse a same-name branch that has
        DIVERGED from base — an aborted/foreign same-id run's leftover whose
        commits would silently ride into this run's PR. Refuse loudly instead
        (as `checkout -b` used to), rather than adopt foreign state."""
        run = self._walk_to_preflight()
        branch = "fix/WORK-7-fix-null-crash-in-parser"
        gitops.run_git(self.repo, "checkout", "-b", branch)
        gitops.run_git(self.repo, "commit", "--allow-empty",
                       "-m", "orphaned leftover commit")   # diverge from main
        gitops.run_git(self.repo, "checkout", "main")
        out = self.cli("preflight", "--repo", str(self.repo), run=run, expect=1)
        self.assertIn("has diverged", out["error"])

    def test_preflight_idempotent_after_cursor_advances_past_preflight(self):
        """Review finding (F4 ordering): a recorded-branch retry must no-op
        even after the cursor has advanced past preflight — the idempotency
        contract holds regardless of the current step, not only while still on
        preflight (the precondition check must not pre-empt the early-return)."""
        run = self._walk_to_preflight()
        first = self.cli("preflight", "--repo", str(self.repo), run=run)
        self.cli("cursor", "--to", "develop", run=run)   # advance past preflight
        again = self.cli("preflight", "--repo", str(self.repo), run=run)  # no raise
        self.assertEqual(again["branch"], first["branch"])

    def test_the_thesis(self):
        # ---- init (bootstrap marker + config) --------------------------------
        self.cli("init", "--stories-dir", str(self.stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")

        # fetch refuses before... (init already ran; prove the gate exists by
        # asserting fetch works only now)
        out = self.cli("fetch", "--id", "WORK-7", "--date", "2026-01-01")
        run = Path(out["run"])
        self.assertEqual(out["mode"], "full")          # Bug, no quick hint
        self.assertEqual(out["change_type"], "fix")    # Bug -> fix via type map
        item = json.loads((run / "work-item.json").read_text())
        self.assertEqual(item["title"], "Fix null crash in parser")

        # second fetch for the same item: collision refusal (B5)
        self.cli("fetch", "--id", "WORK-7", "--date", "2026-01-01", expect=1)

        # ---- walk to develop through real gates ------------------------------
        self.cli("cursor", "--to", "intake", run=run)
        self.cli("cursor", "--to", "plan", run=run)
        (run / "plan.md").write_text(
            "# Plan\n## T1: return 1 from x.val()\nTest-intents: test_val\n")
        self.gate(run, "approve-plan")
        # leaving `plan` with the fetch-seeded provisional task is refused
        # (requires_tasks_registered) — register the approved plan's tasks
        self.cli("cursor", "--to", "approve-plan", run=run, expect=1)
        self.cli("plan-register", "--tasks-json",
                 json.dumps([{"id": "T1", "repo": str(self.repo)}]), run=run)
        self.cli("cursor", "--to", "approve-plan", run=run)
        self.cli("cursor", "--to", "preflight", run=run)

        branch = self.cli("preflight", "--repo", str(self.repo), run=run)["branch"]
        self.assertEqual(branch, "fix/WORK-7-fix-null-crash-in-parser")

        self.cli("cursor", "--to", "develop", run=run)

        # ---- the TDD loop, all owned entry points ----------------------------
        gitops.run_git(self.repo, "checkout", "-b", "task/T1")
        self.cli("task", "--id", "T1", "--to", "in-progress", run=run)

        (self.repo / "tests" / "test_x.py").write_text(FAILING_TEST)
        self.cli("verify-red", "--repo", str(self.repo), "--task", "T1",
                 "--test-cmd", TEST_CMD, "--intents", "test_val", run=run)

        (self.repo / "x.py").write_text("def val():\n    return 1\n")
        self.cli("commit", "--repo", str(self.repo), "--task-id", "T1",
                 "--summary", "return 1 from val", run=run)

        self.cli("task", "--id", "T1", "--to", "in-review",
                 "--repo", str(self.repo), "--test-cmd", TEST_CMD, run=run)

        gitops.run_git(self.repo, "checkout", branch)
        sha = self.cli("merge-task", "--repo", str(self.repo), "--task-id", "T1",
                       "--task-branch", "task/T1",
                       "--summary", "handle empty input", run=run)["sha"]
        # completing out of review needs the hook-captured reviewer verdict
        # (reviewer-approved guard); the hook writes this ledger in production
        self.cli("task", "--id", "T1", "--to", "done", run=run, expect=1)
        ndjson.append_record(run / "reviews.ndjson",
                             {"task": "T1", "mode": "review",
                              "verdict": "APPROVED"})
        self.cli("task", "--id", "T1", "--to", "done", run=run)

        # ---- implementation approval gate (real captured input) --------------
        self.cli("cursor", "--to", "approve-impl", run=run)
        # a decision BEFORE presentation must not count:
        self.cli("gate", "--id", "approve-impl", "--decide", run=run, expect=1)
        self.gate(run, "approve-impl")

        # ---- the thesis assertions -------------------------------------------
        subjects = gitops.run_git(self.repo, "log", "--format=%s",
                                  "main..HEAD").splitlines()
        self.assertEqual(subjects, ["fix: #WORK-7 handle empty input"],
                         "exactly one squashed task-commit, declared template")

        state = self.cli("show", run=run)["state"]
        self.assertEqual(state["cursor"]["current_step"], "approve-impl")
        self.assertEqual(state["tasks"][0]["status"], "done")
        self.assertEqual(state["tasks"][0]["commit_sha"], sha)
        self.assertEqual(state["gates"]["approve-impl"]["decision"], "approved")
        self.assertTrue(state["gates"]["approve-impl"]["evidence"])  # human hash

        kinds = [r.get("kind") for r in ndjson.read_records(run / "events.ndjson")]
        for expected in ("fetched", "red-proof", "gate-decision"):
            self.assertIn(expected, kinds)

        self.cli("verify", run=run)   # integrity chain intact end-to-end

    def test_fetch_refuses_without_bootstrap(self):
        out = self.cli("fetch", "--id", "WORK-7", expect=1)
        self.assertIn("bootstrap incomplete", out["error"])

    def test_quick_hint_with_disqualifier_stays_full(self):
        (self.stories / "Q-1.md").write_text(
            "# Q-1: tiny auth tweak\nType: Task\nStatus: Open\n\n"
            "## Description\nMode: quick\ntouch the auth check\n")
        self.cli("init", "--stories-dir", str(self.stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        out = self.cli("fetch", "--id", "Q-1", "--date", "2026-01-02")
        self.assertEqual(out["mode"], "full")
        self.assertIn("auth", out["classify_reason"])

    def test_quick_hint_clean_goes_quick(self):
        (self.stories / "Q-2.md").write_text(
            "# Q-2: fix typo in README\nType: Task\nStatus: Open\n\n"
            "## Description\nMode: quick\njust a typo\n")
        self.cli("init", "--stories-dir", str(self.stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        out = self.cli("fetch", "--id", "Q-2", "--date", "2026-01-03")
        self.assertEqual(out["mode"], "quick")


if __name__ == "__main__":
    unittest.main()
