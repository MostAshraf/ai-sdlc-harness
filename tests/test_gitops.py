"""M2 done-criteria against real fixture repos: red->green happy path,
skipped-red refusal, SHA-mismatch detection (edit / git-checkout / fixture),
the flagged revision path, squash + autosquash correctness, mirror
path-exclusivity, sync-branch, commit classes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path

from unittest import mock

from harness import (chain, gitops, initws, ndjson, state as state_mod,
                     transitions, workflow)
from harness.cli import load_declared
from harness.providers import ProviderError
from tests import support

TEST_CMD = f'"{sys.executable}" -m unittest discover -s tests -t .'

FAILING_TEST = """import unittest
import x

class T(unittest.TestCase):
    def test_val(self):
        self.assertEqual(x.val(), 1)
"""


def make_repo(base: Path, name: str = "repo", with_impl: bool = False) -> Path:
    repo = base / name
    (repo / "tests").mkdir(parents=True)
    gitops.run_git(base, "init", "-b", "main", name)
    gitops.run_git(repo, "config", "user.email", "t@t")
    gitops.run_git(repo, "config", "user.name", "t")
    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "conftest.py").write_text("# shared fixture\nSTRICT = True\n")
    (repo / "README.md").write_text("fixture\n")
    if with_impl:
        (repo / "x.py").write_text("def val():\n    return 1\n")
    gitops.run_git(repo, "add", "-A")
    gitops.run_git(repo, "commit", "-m", "init")
    return repo


class GitopsHarness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.run = self.workspace / "ai" / "2026-01-01-GIT-1"
        self.manifest, self.fsm, self.config = load_declared(self.workspace)
        self.key = chain.load_or_create_key(self.workspace)
        self.repo = make_repo(self.workspace)
        state_mod.bootstrap(
            self.run, self.workspace,
            work_item={"id": "GIT-1", "title": "t", "provider_ref": ""},
            mode="full", change_type="fix",
            tasks=[{"id": "T1", "repo": str(self.repo)}], entry_step="fetch")
        # a full-mode TDD task always carries plan-declared intents
        # (`test_intents: []` is the docs/chore opt-out, which verify-red
        # REFUSES — matches FAILING_TEST's method name so the intent floor
        # stays clean in the happy paths)
        self._set_declared_test_intents(["test_val"])

    def tearDown(self):
        support.rmtree(self.workspace)

    def _write_test(self):
        (self.repo / "tests" / "test_x.py").write_text(FAILING_TEST)

    def _write_impl(self):
        (self.repo / "x.py").write_text("def val():\n    return 1\n")

    def _red(self, **kw):
        return gitops.verify_red(self.run, self.workspace, self.repo, self.config,
                                 "T1", TEST_CMD, **kw)

    def _set_declared_test_intents(self, names):
        st = state_mod.load(self.run, self.workspace)
        st["tasks"][0]["test_intents"] = names
        state_mod.save(self.run, self.workspace, st)

    def _task_branch(self, name="task/T1", filename="a.txt", back_to="main"):
        """A task branch with one commit, leaving the checkout back on
        `back_to` — the shape every merge/autosquash precondition test needs."""
        gitops.run_git(self.repo, "checkout", "-b", name)
        (self.repo / filename).write_text("task work\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "task work")
        gitops.run_git(self.repo, "checkout", back_to)

    def _stall_a_rebase(self, repo=None):
        """Leave a REAL rebase mid-flight (`edit` on the first todo line), the
        way an interrupted history rewrite does. Real, not a fabricated
        marker directory: the tests below assert the refusal does not ABORT
        it, and only a real rebase can actually be destroyed."""
        repo = repo or self.repo
        for text in ("one\n", "two\n"):
            (repo / "r.txt").write_text(text)
            gitops.run_git(repo, "add", "-A")
            gitops.run_git(repo, "commit", "-m", f"r {text.strip()}")
        subprocess.run(["git", "-C", str(repo), "rebase", "-i", "HEAD~2"],
                       capture_output=True,
                       env={**os.environ,
                            "GIT_SEQUENCE_EDITOR": "sed -i.bak '1s/pick/edit/'"})
        self.assertEqual(gitops._in_progress_operation(repo), "rebase",
                         "fixture failed to leave a rebase in progress")
        self.addCleanup(gitops.run_git, repo, "rebase", "--abort", check=False)


class TddProofPair(GitopsHarness):
    def test_test_command_timeout_raises_redprooferror_not_a_raw_traceback(self):
        # adversarial-review finding: subprocess.TimeoutExpired was uncaught
        # here, crashing with a raw Python traceback instead of the CLI's
        # JSON error contract. The mock times out only the TEST command —
        # patching every subprocess.run also killed the git calls _test_set
        # makes, which the old blanket mock got away with purely because the
        # test run happened to come first (pre-release review reordered
        # verify_red so the overlap check precedes the run).
        self._write_test()
        real = subprocess.run

        def only_test_cmd_times_out(args, **kwargs):
            if isinstance(args, str):   # shell=True: the test command itself
                raise subprocess.TimeoutExpired(TEST_CMD, 600)
            return real(args, **kwargs)

        with mock.patch("harness.gitops.subprocess.run",
                        side_effect=only_test_cmd_times_out):
            with self.assertRaises(gitops.RedProofError) as ctx:
                self._red()
        self.assertIn("timed out", str(ctx.exception))

    def test_red_green_happy_path(self):
        self._write_test()
        proof = self._red()
        self.assertNotEqual(proof["evidence"]["exit_code"], 0)
        self.assertIn("tests/test_x.py", proof["tests"])
        self.assertIn("tests/conftest.py", proof["closure"])   # RC4 widening
        sealed = json.loads(chain.verify(
            transitions.redproof_path(self.run, "T1"), self.key,
            label=transitions.redproof_label("T1")))
        self.assertEqual(sealed["tests"], proof["tests"])
        self._write_impl()
        gitops.verify_green(proof, self.repo, TEST_CMD)   # green + SHAs intact

    def test_skipped_red_refused(self):
        self._write_impl()
        self._write_test()
        with self.assertRaises(gitops.RedProofError) as ctx:
            self._red()
        self.assertIn("not red", str(ctx.exception))

    def test_green_refused_while_still_red(self):
        self._write_test()
        proof = self._red()
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_green(proof, self.repo, TEST_CMD)
        self.assertIn("still failing", str(ctx.exception))

    def test_sha_mismatch_via_direct_edit(self):
        self._write_test()
        proof = self._red()
        self._write_impl()
        weakened = FAILING_TEST.replace("assertEqual(x.val(), 1)",
                                        "assertTrue(True)")
        (self.repo / "tests" / "test_x.py").write_text(weakened)   # sed-style
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_green(proof, self.repo, TEST_CMD)
        self.assertIn("changed since red-proof", str(ctx.exception))

    def test_sha_mismatch_via_git_checkout(self):
        # v1 committed; verify-red on edited v2; `git checkout` restores v1.
        (self.repo / "tests" / "test_x.py").write_text("# placeholder v1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "placeholder test")
        self._write_test()   # v2, uncommitted
        proof = self._red()
        self._write_impl()
        gitops.run_git(self.repo, "checkout", "--", "tests/test_x.py")
        with self.assertRaises(gitops.RedProofError):
            gitops.verify_green(proof, self.repo, TEST_CMD)

    def test_fixture_weakening_is_caught(self):
        self._write_test()
        proof = self._red()
        self._write_impl()
        (self.repo / "tests" / "conftest.py").write_text("STRICT = False\n")
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_green(proof, self.repo, TEST_CMD)
        self.assertIn("conftest", str(ctx.exception))

    def test_revision_path_is_flagged_never_silent(self):
        self._write_test()
        self._red()
        with self.assertRaises(gitops.RedProofError) as ctx:
            self._red()   # silent re-baseline refused
        self.assertIn("--revise", str(ctx.exception))
        with self.assertRaises(gitops.RedProofError):
            self._red(revise=True)   # reason mandatory
        self._red(revise=True, reason="assertion checked the wrong field")
        kinds = [r["kind"] for r in ndjson.read_records(self.run / "events.ndjson")]
        self.assertIn("test-revision", kinds)

    def test_empty_test_set_refused(self):
        with self.assertRaises(gitops.RedProofError):
            gitops.verify_red(self.run, self.workspace, self.repo, self.config,
                              "T1", "false")  # cmd fails but no test files changed


class TestIntentFloor(GitopsHarness):
    """WS-2 (m8-plan-fidelity.md): declared-vs-actual test-intent floor —
    coverage B1's presence check, mechanized at verify-red time."""

    def test_full_match_yields_no_missing(self):
        self._write_test()  # FAILING_TEST defines test_val
        proof = self._red(intents=["test_val"])
        self.assertEqual(proof["declared_intents"], ["test_val"])
        self.assertEqual(proof["missing_intents"], [])

    def test_declared_name_absent_yields_missing(self):
        self._write_test()
        proof = self._red(intents=["test_val", "test_edge_case"])
        self.assertEqual(proof["missing_intents"], ["test_edge_case"])

    def test_explicit_empty_intents_override_yields_empty_missing(self):
        # an explicit `--intents` (even empty) is the caller's declaration
        # and bypasses the state lookup: nothing declared, nothing missing
        self._write_test()
        proof = self._red(intents=[])
        self.assertEqual(proof["declared_intents"], [])
        self.assertEqual(proof["missing_intents"], [])

    def test_no_intents_task_refused_with_the_exemption_message(self):
        """A docs-only task (test_intents: []) can never go red, and
        verify-red's old "suite PASSES — not red" sent the developer
        chasing a failing test the plan never asked for. Refuse loudly
        with the exemption instead."""
        self._set_declared_test_intents([])
        self._write_test()
        with self.assertRaises(gitops.RedProofError) as ctx:
            self._red()
        self.assertIn("no test-intents", str(ctx.exception))
        self.assertIn("in-review directly", str(ctx.exception))

    def test_auto_loads_declared_intents_from_state_when_not_given(self):
        self._set_declared_test_intents(["test_val", "test_ghost"])
        self._write_test()
        proof = self._red()  # no --intents: must load from state.yaml
        self.assertEqual(proof["declared_intents"], ["test_val", "test_ghost"])
        self.assertEqual(proof["missing_intents"], ["test_ghost"])

    def test_explicit_intents_override_state(self):
        self._set_declared_test_intents(["test_ghost"])
        self._write_test()
        proof = self._red(intents=["test_val"])  # explicit wins over state.yaml
        self.assertEqual(proof["declared_intents"], ["test_val"])
        self.assertEqual(proof["missing_intents"], [])

    def test_missing_intents_sealed_on_redproof_and_readable_by_reviewer(self):
        self._write_test()
        self._red(intents=["test_val", "test_edge_case"])
        sealed = json.loads(chain.verify(
            transitions.redproof_path(self.run, "T1"), self.key,
            label=transitions.redproof_label("T1")))
        self.assertEqual(sealed["missing_intents"], ["test_edge_case"])

    def test_word_boundary_prevents_prefix_false_negative(self):
        # test_val is written; a DIFFERENT declared name that's merely a
        # prefix of it must not be satisfied by test_val's presence.
        self._write_test()
        proof = self._red(intents=["test_va"])
        self.assertEqual(proof["missing_intents"], ["test_va"])

    def test_closure_file_counts_as_written(self):
        # a shared base-class test method lives in conftest.py (test_closure
        # glob), not the primary test_paths glob (RC4 widening, design.md:398)
        self._write_test()
        (self.repo / "tests" / "conftest.py").write_text(
            "# shared fixture\nSTRICT = True\ndef test_shared_case(): pass\n")
        proof = self._red(intents=["test_shared_case"])
        self.assertEqual(proof["missing_intents"], [])

    def test_unknown_task_id_raises_instead_of_silently_defaulting(self):
        self._write_test()
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_red(self.run, self.workspace, self.repo, self.config,
                              "NO-SUCH-TASK", TEST_CMD)
        self.assertIn("not found in state.yaml", str(ctx.exception))


class CommitAndSquash(GitopsHarness):
    def test_commit_classes_render_declared_templates(self):
        (self.repo / "w.txt").write_text("work\n")
        gitops.commit_class(self.repo, self.config, "working",
                            task="T1", summary="wire up x")
        self.assertEqual(gitops.run_git(self.repo, "log", "-1", "--format=%s"),
                         "task(T1): wire up x")
        (self.repo / "w2.txt").write_text("partial\n")
        gitops.commit_class(self.repo, self.config, "wip",
                            task="T1", summary="soft-cap checkpoint")
        self.assertTrue(gitops.run_git(self.repo, "log", "-1", "--format=%s")
                        .startswith("[WIP] task(T1):"))

    def test_nothing_to_commit_refused(self):
        with self.assertRaises(gitops.GitError):
            gitops.commit_class(self.repo, self.config, "working",
                                task="T1", summary="empty")

    def test_squash_merge_single_integration_commit(self):
        base = gitops.head_sha(self.repo)
        gitops.run_git(self.repo, "checkout", "-b", "task/T1")
        (self.repo / "a.txt").write_text("a\n")
        gitops.commit_class(self.repo, self.config, "working", task="T1", summary="a")
        (self.repo / "b.txt").write_text("b\n")
        gitops.commit_class(self.repo, self.config, "working", task="T1", summary="b")
        gitops.run_git(self.repo, "checkout", "main")
        sha = gitops.squash_merge(self.repo, "task/T1",
                                  "fix: #GIT-1 do the thing", "main")
        subjects = gitops.run_git(self.repo, "log", "--format=%s",
                                  f"{base}..HEAD").splitlines()
        self.assertEqual(subjects, ["fix: #GIT-1 do the thing"])   # ONE commit
        self.assertTrue((self.repo / "a.txt").exists() and (self.repo / "b.txt").exists())
        self.assertEqual(sha, gitops.head_sha(self.repo))

    def test_autosquash_folds_fixup_and_rederives_sha(self):
        base = gitops.head_sha(self.repo)
        (self.repo / "a.txt").write_text("v1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "fix: #GIT-1 task one")
        task_sha = gitops.head_sha(self.repo)
        (self.repo / "a.txt").write_text("v2 fixed\n")
        gitops.commit_fixup(self.repo, task_sha)
        gitops.autosquash(self.repo, base, "main")
        subjects = gitops.run_git(self.repo, "log", "--format=%s",
                                  f"{base}..HEAD").splitlines()
        self.assertEqual(subjects, ["fix: #GIT-1 task one"])   # fixup folded
        new_sha = gitops.find_commit_by_subject(self.repo, base, "fix: #GIT-1 task one")
        self.assertNotEqual(new_sha, task_sha)                  # SHA re-derived
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "v2 fixed\n")


class SecretSweepGuard(GitopsHarness):
    """0.16.12 field class (e2e E2E-1): a stray integrity key inside a repo
    checkout must never enter git history — pre-0.16.11 a wrong---workspace
    invocation minted one, and `commit_class`'s own `git add -A` swept it
    into a task commit that later needed an object-level scrub."""

    def _plant_key(self, root: Path) -> Path:
        key = root / ".claude" / "context" / ".harness-key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("stray-secret\n")
        return key

    def test_commit_refuses_and_unstages_stray_key(self):
        self._plant_key(self.repo)
        (self.repo / "w.txt").write_text("work\n")
        with self.assertRaises(gitops.SecretSweepError) as ctx:
            gitops.commit_class(self.repo, self.config, "working",
                                task="T1", summary="sweep attempt")
        self.assertIn(".harness-key", str(ctx.exception))
        staged = gitops.run_git(self.repo, "diff", "--cached", "--name-only")
        self.assertNotIn(".harness-key", staged)     # unstaged on refusal
        # the named remediation works: delete the stray, retry cleanly
        (self.repo / ".claude" / "context" / ".harness-key").unlink()
        gitops.commit_class(self.repo, self.config, "working",
                            task="T1", summary="clean retry")
        tracked = gitops.run_git(self.repo, "ls-files")
        self.assertNotIn(".harness-key", tracked)
        self.assertIn("w.txt", tracked)

    def test_commit_fixup_refuses_stray_key(self):
        (self.repo / "a.txt").write_text("v1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "fix: #GIT-1 base")
        self._plant_key(self.repo)
        (self.repo / "a.txt").write_text("v2\n")
        with self.assertRaises(gitops.SecretSweepError):
            gitops.commit_fixup(self.repo, gitops.head_sha(self.repo))

    def test_exclude_keeps_untracked_key_out_of_add_A(self):
        gitops.ensure_repo_excludes(self.repo)
        gitops.ensure_repo_excludes(self.repo)   # idempotent — no duplicates
        exclude = self.repo / ".git" / "info" / "exclude"
        self.assertEqual(
            exclude.read_text(encoding="utf-8").splitlines().count(".harness-key"), 1)
        self._plant_key(self.repo)
        (self.repo / "w.txt").write_text("work\n")
        # no refusal needed: add -A never sees the excluded key
        gitops.commit_class(self.repo, self.config, "working",
                            task="T1", summary="excluded key untouched")
        self.assertNotIn(".harness-key", gitops.run_git(self.repo, "ls-files"))
        # the file itself is untouched — exclusion, not deletion
        self.assertTrue(
            (self.repo / ".claude" / "context" / ".harness-key").exists())

    def test_exclude_covers_task_worktrees_via_common_git_dir(self):
        gitops.ensure_repo_excludes(self.repo)
        wt_rec = gitops.worktree_add(self.repo, "T1", "main")
        self.addCleanup(gitops.worktree_remove, self.repo, wt_rec)
        wt = Path(wt_rec["path"])
        self._plant_key(wt)
        (wt / "w.txt").write_text("work\n")
        gitops.commit_class(wt, self.config, "working",
                            task="T1", summary="worktree sweep-proof")
        self.assertNotIn(".harness-key", gitops.run_git(wt, "ls-files"))


class SquashConflictCleanup(GitopsHarness):
    def _conflicting_branch(self):
        (self.repo / "c.txt").write_text("main version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main side")
        gitops.run_git(self.repo, "checkout", "-b", "task/T1", "HEAD~1")
        (self.repo / "c.txt").write_text("task version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "task side")
        gitops.run_git(self.repo, "checkout", "main")

    def test_conflicted_squash_merge_restores_the_tree(self):
        """Adversarial-review finding (verified by execution): a conflicted
        `merge --squash` left `<<<<<<<` markers with NO MERGE_HEAD, so the
        in-progress check saw nothing and the next `harness commit`'s
        `git add -A` committed the conflict markers under a legitimate
        task message."""
        self._conflicting_branch()
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #X collide", "main")
        self.assertIn("conflicted", str(ctx.exception))
        # tree restored: no markers, no unmerged index, nothing staged
        self.assertEqual((self.repo / "c.txt").read_text(encoding="utf-8"), "main version\n")
        self.assertFalse(gitops.run_git(self.repo, "ls-files", "-u"))
        with self.assertRaises(gitops.GitError):   # nothing to commit
            gitops.commit_class(self.repo, self.config, "working",
                                task="T1", summary="post-conflict")

    def test_a_conflicted_squash_leaves_no_dirt_the_preconditions_would_see(self):
        """The two new preconditions and the old conflict cleanup have to
        agree: `reset --merge` restores the tree, so a task whose merge
        conflicted does not poison the NEXT task's precondition check. If it
        did, one conflict would freeze the whole pipelined tail."""
        self._conflicting_branch()
        with self.assertRaises(gitops.GitError):
            gitops.squash_merge(self.repo, "task/T1", "fix: #X collide", "main")
        gitops.run_git(self.repo, "checkout", "-b", "task/T2", "main")
        (self.repo / "unrelated.txt").write_text("sibling work\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "sibling")
        gitops.run_git(self.repo, "checkout", "main")
        sha = gitops.squash_merge(self.repo, "task/T2", "fix: #X sibling", "main")
        self.assertEqual(sha, gitops.head_sha(self.repo))

    def test_unresolved_merge_blocks_ensure_default_branch(self):
        # a plain (non-squash) conflicted state must also be seen via the
        # unmerged-index check even where no MERGE_HEAD marker survives
        self._conflicting_branch()
        proc = subprocess.run(["git", "-C", str(self.repo), "merge",
                               "--squash", "task/T1"], capture_output=True)
        self.assertNotEqual(proc.returncode, 0)   # conflicted, NOT cleaned up
        self.assertIn("unresolved merge",
                      gitops._in_progress_operation(self.repo) or "")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("in progress", str(ctx.exception))

    def test_in_progress_detection_works_inside_a_linked_worktree(self):
        """Adversarial-review finding (verified by execution): `.git` is a
        FILE in a linked worktree, so the literal `repo/.git/<marker>`
        checks returned None exactly where every M5 task actually runs."""
        wt = gitops.worktree_add(self.repo, "T9", "main")
        wt_path = Path(wt["path"])
        self.addCleanup(gitops.worktree_remove, self.repo, wt)
        (wt_path / "r.txt").write_text("a\n")
        gitops.run_git(wt_path, "add", "-A")
        gitops.run_git(wt_path, "commit", "-m", "one")
        (wt_path / "r.txt").write_text("b\n")
        gitops.run_git(wt_path, "add", "-A")
        gitops.run_git(wt_path, "commit", "-m", "two")
        # rebase with edit stops mid-flight, leaving rebase-merge markers
        import os
        env = {**os.environ,
               "GIT_SEQUENCE_EDITOR": "sed -i.bak '1s/pick/edit/'"}
        subprocess.run(["git", "-C", str(wt_path), "rebase", "-i", "HEAD~2"],
                       capture_output=True, env=env)
        self.assertEqual(gitops._in_progress_operation(wt_path), "rebase")
        gitops.run_git(wt_path, "rebase", "--abort", check=False)


class SquashMergePreconditions(GitopsHarness):
    """Round 3 (DAG-pipelined dispatch): several tasks are in flight at once
    and every one of them squash-merges into the SAME feature-branch
    checkout. The two states that used to corrupt silently — a merge issued
    on the wrong HEAD, and a merge stacked on a sibling's half-finished tree
    — now refuse BEFORE the index is touched."""

    def test_a_head_that_is_not_the_feature_branch_is_refused(self):
        """The silent corruption: the whole task diff lands on whatever is
        checked out — classically `main`, from a stray checkout or an
        aborted sibling — and create-pr then opens a PR whose head branch
        has none of it."""
        gitops.run_git(self.repo, "checkout", "-b", "feat/W-1")
        gitops.run_git(self.repo, "checkout", "main")
        self._task_branch()
        before = gitops.head_sha(self.repo)
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 land it",
                                "feat/W-1")
        msg = str(ctx.exception)
        self.assertIn("'main'", msg)          # where HEAD actually is
        self.assertIn("feat/W-1", msg)        # where it was expected
        self.assertEqual(gitops.head_sha(self.repo), before)   # nothing moved
        self.assertFalse(gitops.changed_files(self.repo))      # nothing staged
        # …and on the right branch the identical call goes through
        gitops.run_git(self.repo, "checkout", "feat/W-1")
        sha = gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 land it",
                                  "feat/W-1")
        self.assertEqual(sha, gitops.head_sha(self.repo))

    def test_a_detached_head_says_so(self):
        # `rev-parse --abbrev-ref` answers the literal string "HEAD" here,
        # which as a branch name in a refusal reads like nonsense
        self._task_branch()
        gitops.run_git(self.repo, "checkout", "--detach", "main")
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        self.assertIn("DETACHED", str(ctx.exception))

    def test_a_dirty_tree_is_refused_and_names_the_paths(self):
        """A dirty tree here is a sibling task's merge caught mid-flight;
        squashing over it folds that task's changes into THIS task's
        integration commit, so the per-task commit_sha map stops describing
        what each commit contains."""
        self._task_branch()
        (self.repo / "README.md").write_text("a sibling's half-finished work\n")
        before = gitops.head_sha(self.repo)
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        msg = str(ctx.exception)
        self.assertIn("README.md", msg)
        self.assertIn("1 uncommitted change", msg)
        self.assertEqual(gitops.head_sha(self.repo), before)
        # cleaning it is all it takes — the refusal is about tree state, and
        # the same command then succeeds unchanged
        gitops.run_git(self.repo, "checkout", "--", "README.md")
        gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        self.assertTrue((self.repo / "a.txt").exists())

    def test_a_staged_change_is_dirt_too(self):
        # `diff HEAD` covers both halves: a merge stacked on someone else's
        # staged-but-uncommitted index is the same misattribution
        self._task_branch()
        (self.repo / "staged.txt").write_text("staged\n")
        gitops.run_git(self.repo, "add", "-A")
        with self.assertRaises(gitops.MergePreconditionError):
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")

    def test_untracked_files_are_deliberately_not_dirt(self):
        """Scoped on purpose: an untracked scratch file is the normal state
        of a working checkout, it is not in the index, and `merge --squash`
        refuses on its own if it would clobber one. Refusing here would make
        every task's merge hostage to a stray editor file."""
        self._task_branch()
        (self.repo / "scratch.log").write_text("not tracked\n")
        gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        self.assertTrue((self.repo / "scratch.log").exists())   # left alone
        self.assertIn("a.txt", gitops.run_git(self.repo, "ls-files"))

    def test_an_unfinished_rebase_is_refused_first_and_left_running(self):
        """Round 4: the in-progress probe now runs BEFORE the branch check,
        and that order is the fix. Mid-rebase, HEAD is detached — so the
        branch check used to answer first and told the operator to "check
        the feature branch out first", which ORPHANS the live rebase. The
        refusal must name the rebase's OWN abort, and must not perform it."""
        self._task_branch()
        self._stall_a_rebase()
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        msg = str(ctx.exception)
        self.assertIn("rebase", msg)
        self.assertIn("git rebase --abort", msg)
        self.assertNotIn("check the feature branch out", msg.lower())
        # the human's rebase is still theirs — untouched, not concluded
        self.assertEqual(gitops._in_progress_operation(self.repo), "rebase")


class InterruptedSquashRecovery(GitopsHarness):
    """Round 4, verified by execution: `squash_merge`'s commit sat OUTSIDE
    the try, so a death between the squash and its commit (Ctrl-C, a killed
    lane, a crashed shell) left the FULL squash staged with no commit.
    Nothing could see that state — no MERGE_HEAD, no unmerged entries — so
    `ready-tasks` kept reporting the task READY while every retry was
    refused by its own leftovers, described as the operator's uncommitted
    changes."""

    def _die_mid_merge(self):
        """The interrupted state itself: the squash lands, the commit never
        happens. Reproduced with the raw git call the verb makes."""
        self._task_branch()
        gitops.run_git(self.repo, "merge", "--squash", "task/T1")
        self.assertTrue(gitops.run_git(self.repo, "diff", "--cached",
                                       "--name-only"), "nothing staged")

    def test_a_staged_squash_is_detected_as_an_operation_in_progress(self):
        self._die_mid_merge()
        self.assertEqual(gitops._in_progress_operation(self.repo),
                         "staged squash merge (never committed)")

    def test_the_retry_is_refused_naming_reset_merge_not_operator_dirt(self):
        """The message is the whole point: a machine's abandoned index is not
        something to go commit or stash, and `rebase --abort` would be the
        wrong tool. One command clears it."""
        self._die_mid_merge()
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        msg = str(ctx.exception)
        self.assertIn("git reset --merge", msg)
        self.assertIn("staged squash merge", msg)
        self.assertNotIn("Commit, stash", msg)    # not the dirt refusal

    def test_the_named_cleanup_works_and_the_identical_command_then_lands(self):
        self._die_mid_merge()
        gitops.run_git(self.repo, "reset", "--merge")   # what the refusal says
        sha = gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        self.assertEqual(sha, gitops.head_sha(self.repo))
        self.assertEqual(gitops.run_git(self.repo, "log", "-1", "--format=%s"),
                         "fix: #W-1 x")
        self.assertIsNone(gitops._in_progress_operation(self.repo))

    def test_a_failing_commit_no_longer_strands_the_squash(self):
        """The commit is inside the try now, so its failure gets the same
        `reset --merge` cleanup the merge's does — otherwise one bad commit
        (a rejected hook, a full disk) leaves exactly the state above."""
        self._task_branch()
        real = gitops.run_git

        def fail_the_commit(repo, *args, **kw):
            if args[:1] == ("commit",):
                raise gitops.GitError("git commit: simulated failure")
            return real(repo, *args, **kw)

        with mock.patch("harness.gitops.run_git", side_effect=fail_the_commit):
            with self.assertRaises(gitops.GitError):
                gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        self.assertIsNone(gitops._in_progress_operation(self.repo))
        self.assertFalse(gitops.run_git(self.repo, "diff", "--cached",
                                        "--name-only"))


class SquashFailureIsNotAlwaysConflict(GitopsHarness):
    """Round 4, measured on an `index.lock` collision: the blanket
    `except GitError` relabelled ANY git failure as "conflicted (working
    tree restored — resolve on the task branch, then retry)". All three
    claims were false — it was not a conflict, there was nothing to resolve
    on the task branch, and the `reset --merge` cleanup ran `check=False`
    against the same lock and could not have restored anything."""

    def test_an_index_lock_collision_reports_gits_own_error_verbatim(self):
        self._task_branch()
        lock = self.repo / ".git" / "index.lock"
        lock.write_text("")
        self.addCleanup(lambda: lock.exists() and lock.unlink())
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #W-1 x", "main")
        msg = str(ctx.exception)
        self.assertIn("NOT on a conflict", msg)
        self.assertIn("index.lock", msg)            # git's words, kept
        self.assertNotIn("resolve on the task branch", msg)
        # …and the restoration claim is honest: the cleanup hit the same lock
        self.assertIn("cleanup ALSO failed", msg)
        self.assertNotIn("working tree restored — ", msg)

    def test_a_real_conflict_still_reads_as_one(self):
        """The narrowing must not cost the conflict path its message: an
        actual conflict leaves unmerged entries, which is what distinguishes
        the two."""
        (self.repo / "c.txt").write_text("main version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main side")
        gitops.run_git(self.repo, "checkout", "-b", "task/T1", "HEAD~1")
        (self.repo / "c.txt").write_text("task version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "task side")
        gitops.run_git(self.repo, "checkout", "main")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.squash_merge(self.repo, "task/T1", "fix: #X collide", "main")
        msg = str(ctx.exception)
        self.assertIn("conflicted", msg)
        self.assertIn("working tree restored", msg)


class AutosquashPreconditions(GitopsHarness):
    """Round 4, measured — survivor M5. `autosquash` had NO preconditions and
    the CLI returned before the recorded-branch lookup, so a `--autosquash`
    issued while the shared checkout sat on `main` rebased MAIN itself; the
    SHA re-derivation then matched a same-subject commit that was not the
    task's and wrote it into state.yaml. Nothing failed — the run simply
    started lying about what it had built."""

    def _fixup_history(self, branch="feat/W-1"):
        """A branch carrying one task commit plus its `fixup!` — what
        autosquash exists to fold. Returns the base SHA."""
        base = gitops.head_sha(self.repo)
        gitops.run_git(self.repo, "checkout", "-b", branch)
        (self.repo / "a.txt").write_text("v1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "fix: #GIT-1 task one")
        task_sha = gitops.head_sha(self.repo)
        (self.repo / "a.txt").write_text("v2 fixed\n")
        gitops.commit_fixup(self.repo, task_sha)
        return base

    def test_a_wrong_checkout_is_refused_and_rewrites_nothing(self):
        base = self._fixup_history()
        gitops.run_git(self.repo, "checkout", "main")
        main_before = gitops.head_sha(self.repo)
        feat_before = gitops.run_git(self.repo, "rev-parse", "feat/W-1")
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.autosquash(self.repo, base, "feat/W-1")
        msg = str(ctx.exception)
        self.assertIn("'main'", msg)         # where HEAD actually is
        self.assertIn("feat/W-1", msg)       # where the run's work lives
        self.assertEqual(gitops.head_sha(self.repo), main_before)
        self.assertEqual(gitops.run_git(self.repo, "rev-parse", "feat/W-1"),
                         feat_before)
        # …and on the right branch the identical call goes through
        gitops.run_git(self.repo, "checkout", "feat/W-1")
        gitops.autosquash(self.repo, base, "feat/W-1")
        self.assertEqual(
            gitops.run_git(self.repo, "log", "--format=%s",
                           f"{base}..HEAD").splitlines(),
            ["fix: #GIT-1 task one"])

    def test_a_pre_existing_rebase_is_refused_without_being_aborted(self):
        """The cleanup path aborts only a rebase THIS call started — so one
        it merely FINDS has to be refused, naming `git rebase --abort` as the
        operator's action. Aborting someone else's in-flight rewrite would
        destroy real work."""
        base = self._fixup_history()
        self._stall_a_rebase()
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.autosquash(self.repo, base, "feat/W-1")
        self.assertIn("git rebase --abort", str(ctx.exception))
        self.assertEqual(gitops._in_progress_operation(self.repo), "rebase",
                         "the pre-existing rebase was aborted for the caller")

    def test_a_dirty_tree_is_refused_too(self):
        base = self._fixup_history()
        (self.repo / "README.md").write_text("a sibling lane, mid-merge\n")
        with self.assertRaises(gitops.MergePreconditionError) as ctx:
            gitops.autosquash(self.repo, base, "feat/W-1")
        self.assertIn("README.md", str(ctx.exception))

    def test_a_rebase_that_never_started_says_so_instead_of_claiming_a_cleanup(self):
        self._fixup_history()
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.autosquash(self.repo, "no-such-base", "feat/W-1")
        msg = str(ctx.exception)
        self.assertIn("never started", msg)
        self.assertNotIn("aborted cleanly", msg)


class ConcurrentTaskMerges(unittest.TestCase):
    """The seam DAG-pipelined dispatch created, exercised through the REAL
    CLI: two tasks, two worktrees, one shared feature-branch checkout — and
    both `merge-task` calls issued at the same instant.

    One index, one HEAD, two `git merge --squash` + `git commit` pairs. With
    the merge outside the run lock (where it lived until round 3, with only
    the SHA write-back re-taking it) the two interleave: git's own
    `index.lock` fails one of them outright, or the loser's commit sweeps
    the winner's staged tree into its own integration commit. Either way a
    task's work is misfiled or missing, and nothing in the run says so.

    Deliberately NOT a unit test of the lock: the whole point is that the
    real verb takes it, from two OS processes, exactly as two dispatched
    lanes would.

    Two measurements shaped this test, because the obvious version of it
    CANNOT LOSE — and a race test that cannot lose is evidence of nothing:
      - Launching both processes from a barrier is not enough. Interpreter
        startup jitter is ~0.5s and unequal between two simultaneously
        launched processes; the two merges landed 1.2s apart and never
        touched. So the RUN LOCK ITSELF is the starting gate: the test holds
        it while both `merge-task` processes launch and queue on it.
      - Even then they stagger by ~1s on Windows, where `msvcrt.locking`
        retries at ONE-SECOND intervals — so the loser of the state read
        arrives a full second late. The merge therefore has to stay open
        longer than that stagger, which is what `N_FILES` buys (measured on
        this repo's fixture: 2000 files ≈ 1.9s inside git, 120 ≈ 0.1s —
        and at 120 the mutation testing survived 3/3 runs)."""

    N_FILES = 2000         # ≈1.9s inside git — wider than the ~1s stagger
    GATE_HOLD = 1.0        # seconds — long enough for both to reach the lock
    # For the deterministic queue probes below: comfortably wider than
    # interpreter startup (~0.5s, measured on this repo), so "hasn't finished
    # yet" means "queued on the lock" rather than "still importing".
    QUEUE_HOLD = 2.5

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        (self.workspace / "stories").mkdir()
        self.repo = make_repo(self.workspace)
        self.run = self.workspace / "ai" / "2026-01-01-RACE-1"
        self._cli("init", "--stories-dir", str(self.workspace / "stories"),
                  "--repo", f"repo={self.repo}",
                  "--test-cmd", f"repo={support.NOP_TEST_CMD}", run=False)
        state_mod.bootstrap(
            self.run, self.workspace,
            work_item={"id": "RACE-1", "title": "t", "provider_ref": ""},
            mode="full", change_type="fix",
            tasks=[{"id": "T1", "repo": str(self.repo)},
                   {"id": "T2", "repo": str(self.repo)}],
            entry_step="fetch")
        self.branch = "feat/RACE-1"
        gitops.run_git(self.repo, "checkout", "-b", self.branch)
        self.base = gitops.head_sha(self.repo)
        # Test-only stand-in for preflight, which records exactly this and
        # nothing else that matters here: merge-task reads the run's own
        # `branches` artifact to know which branch a task integrates onto.
        st = state_mod.load(self.run, self.workspace)
        st["artifacts"]["branches"] = {"repo": {"branch": self.branch,
                                                "base": "main"}}
        state_mod.save(self.run, self.workspace, st)

    def tearDown(self):
        support.rmtree(self.workspace, ignore_errors=True)

    def _argv(self, *args, run=True):
        cmd = [sys.executable, "-m", "harness", "--workspace",
               str(self.workspace)]
        if run:
            cmd += ["--run", str(self.run)]
        return [*cmd, *args]

    def _cli(self, *args, run=True):
        return subprocess.run(self._argv(*args, run=run), cwd=support.ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8", timeout=300)

    def _flow(self, task_id: str, wt: dict, gate, results: dict) -> None:
        """One task's full commit+merge flow, as its lane would run it. The
        merge is LAUNCHED before the gate opens and blocks on the run lock
        the test is holding — that is what makes the two merges simultaneous
        rather than merely concurrent."""
        path = Path(wt["path"])
        for i in range(self.N_FILES):
            (path / f"{task_id}-{i}.txt").write_text(f"{task_id} file {i}\n")
        commit = self._cli("commit", "--repo", str(path), "--task-id", task_id,
                           "--summary", f"{task_id} work")
        merge = subprocess.Popen(
            self._argv("merge-task", "--repo", str(self.repo),
                       "--task-id", task_id, "--task-branch", wt["branch"],
                       "--summary", f"{task_id} landed"),
            cwd=support.ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        gate.wait(timeout=120)             # launched — the gate may open
        out, err = merge.communicate(timeout=300)
        results[task_id] = (commit, merge.returncode, out, err)

    def test_two_task_merges_race_and_both_land(self):
        worktrees = {t: gitops.worktree_add(self.repo, t, self.branch)
                     for t in ("T1", "T2")}
        for wt in worktrees.values():
            self.addCleanup(gitops.worktree_remove, self.repo, wt)
        results: dict = {}
        gate = threading.Barrier(len(worktrees) + 1)      # lanes + this thread
        threads = [threading.Thread(target=self._flow,
                                    args=(t, wt, gate, results))
                   for t, wt in worktrees.items()]
        with state_mod.locked(self.run):
            for th in threads:
                th.start()
            gate.wait(timeout=300)         # every merge process has launched
            time.sleep(self.GATE_HOLD)     # …and is now queued on this lock
        # released: both merges enter git within milliseconds of each other
        for th in threads:
            th.join(timeout=600)
        self.assertEqual(sorted(results), ["T1", "T2"], "a lane never finished")
        for task_id, (commit, code, out, err) in results.items():
            self.assertEqual(commit.returncode, 0,
                             f"{task_id} commit: {commit.stdout}{commit.stderr}")
            self.assertEqual(code, 0, f"{task_id} merge: {out}{err}")

        # BOTH integration commits are on the branch — no lost merge, and no
        # third commit from a half-swept index
        subjects = gitops.run_git(self.repo, "log", "--format=%s",
                                  f"{self.base}..{self.branch}").splitlines()
        self.assertEqual(sorted(subjects),
                         ["fix: #RACE-1 T1 landed", "fix: #RACE-1 T2 landed"])
        # …carrying BOTH tasks' files, every one of them
        tracked = set(gitops.run_git(self.repo, "ls-files").splitlines())
        for task_id in ("T1", "T2"):
            missing = [f"{task_id}-{i}.txt" for i in range(self.N_FILES)
                       if f"{task_id}-{i}.txt" not in tracked]
            self.assertFalse(missing, f"{task_id} lost files: {missing[:5]}")
        # …and the checkout is intact: no unmerged entries, nothing left
        # staged or modified by an interleaved merge
        self.assertFalse(gitops.run_git(self.repo, "ls-files", "-u"))
        self.assertFalse(gitops.run_git(self.repo, "status", "--porcelain"))
        # state agrees, with one distinct SHA per task (the write-back that
        # used to be the only locked part of this verb)
        st = state_mod.load(self.run, self.workspace)
        shas = {t["id"]: t["commit_sha"] for t in st["tasks"]}
        self.assertEqual(len(set(shas.values())), 2, shas)
        landed = set(gitops.run_git(self.repo, "log", "--format=%H",
                                    f"{self.base}..{self.branch}").splitlines())
        self.assertEqual(set(shas.values()), landed)

    def _assert_queues_on_the_run_lock(self, *args) -> str:
        """Launch a verb while THIS test holds the run lock and prove it
        QUEUED: still running when the hold ends, successful once released.

        Deterministic where a two-process race is not — the lock is the same
        object either way, so proving each verb takes it proves the pair
        serializes in both directions, without depending on which of two
        launches wins."""
        proc = subprocess.Popen(
            self._argv(*args), cwd=support.ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            with state_mod.locked(self.run):
                time.sleep(self.QUEUE_HOLD)
                queued = proc.poll() is None
            out, err = proc.communicate(timeout=300)
        finally:
            if proc.poll() is None:                # never leak a hung child
                proc.kill()
        self.assertTrue(queued, "the verb ran to completion while the run "
                                f"lock was held: {out}{err}")
        self.assertEqual(proc.returncode, 0, f"{out}{err}")
        return out

    def test_publish_mirror_queues_on_the_run_lock_like_a_merge_does(self):
        """Round 4: `publish-mirror` ran with NO lock at all. It walks the
        live run directory, copies and PRUNES it into the repo, then stages
        and commits — while a sibling lane may be rewriting that repo's index
        (merge) or mid-`chain.seal` on state.yaml (two separate atomic
        replaces). Both verbs take the run lock now, so the pair serializes
        whichever arrives first."""
        out = self._assert_queues_on_the_run_lock(
            "publish-mirror", "--repo", str(self.repo))
        self.assertTrue(json.loads(out)["ok"])
        self.assertTrue((self.repo / "ai" / self.run.name / "state.yaml").exists())

    def test_a_typoed_run_is_refused_before_publish_mirror_takes_the_lock(self):
        """`locked()` mkdirs unconditionally, so the phantom-run check has to
        come first — the same stray-directory class `show`/`merge-task`
        already guard against."""
        bogus = self.workspace / "ai" / "2026-01-01-TYPO"
        proc = subprocess.run(
            [*self._argv("publish-mirror", "--repo", str(self.repo), run=False),
             "--run", str(bogus)],
            cwd=support.ROOT, capture_output=True, text=True,
            encoding="utf-8", timeout=300)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not a run", json.loads(proc.stdout)["error"])
        self.assertFalse(bogus.exists(), "a typo'd --run must not be created")

    def test_autosquash_queues_on_the_run_lock_too(self):
        """Survivor M5: the autosquash form REWRITES every commit on the
        branch, so a sibling merge landing mid-rebase is silently dropped
        from the rewritten history — and the SHA re-derivation then records a
        subject-matched commit for a task whose real work is gone. It is the
        longest lock hold this verb has, and the one that most needs it."""
        out = self._assert_queues_on_the_run_lock(
            "merge-task", "--repo", str(self.repo), "--autosquash",
            "--base", "main")
        self.assertTrue(json.loads(out)["autosquashed"])

    def test_a_merge_task_on_a_typoed_run_manufactures_nothing(self):
        bogus = self.workspace / "ai" / "2026-01-01-NOPE"
        proc = subprocess.run(
            [*self._argv("merge-task", "--repo", str(self.repo), "--task-id",
                         "T1", "--task-branch", "task/T1", "--summary", "x",
                         run=False), "--run", str(bogus)],
            cwd=support.ROOT, capture_output=True, text=True,
            encoding="utf-8", timeout=300)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        err = json.loads(proc.stdout)["error"]
        self.assertIn("StateError", err)
        self.assertIn("not a run", err)
        self.assertFalse(bogus.exists(), "a typo'd --run must not be created")


class PushRemoteResolution(GitopsHarness):
    def test_single_nonorigin_remote_used_multiple_refused(self):
        gitops.run_git(self.repo, "remote", "add", "upstream", "u://x")
        self.assertEqual(gitops._push_remote(self.repo), "upstream")
        gitops.run_git(self.repo, "remote", "add", "fork", "u://y")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops._push_remote(self.repo)
        self.assertIn("ambiguous", str(ctx.exception))
        gitops.run_git(self.repo, "remote", "add", "origin", "u://z")
        self.assertEqual(gitops._push_remote(self.repo), "origin")

    def test_no_remote_refused(self):
        with self.assertRaises(gitops.GitError):
            gitops._push_remote(self.repo)


class MirrorAndSync(GitopsHarness):
    def test_mirror_is_path_exclusive_and_private(self):
        ndjson.append_record(self.run / "events.ndjson", {"kind": "x"})
        ndjson.append_record(self.run / "human-input.ndjson", {"text": "SECRET"})
        (self.run / ".redproof").mkdir()
        (self.run / ".redproof" / "T1.json").write_text("{}")
        (self.repo / "unrelated.txt").write_text("dirty working tree\n")
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        committed = gitops.run_git(self.repo, "diff-tree", "--no-commit-id",
                                   "--name-only", "-r", "HEAD").splitlines()
        self.assertTrue(committed, "mirror commit is empty")
        self.assertTrue(all(p.startswith("ai/") for p in committed), committed)
        joined = "\n".join(committed)
        self.assertNotIn("human-input", joined)     # privacy carve-out
        self.assertNotIn(".redproof", joined)       # wrapper-owned scratch
        self.assertNotIn(".hmac", joined)           # seals are workspace-local
        self.assertIn("unrelated.txt", gitops.changed_files(self.repo))

    def test_mirror_prunes_deletions_and_near_name_private_variants(self):
        """Adversarial-review findings: (a) copy-only mirroring kept both
        names of a renamed report forever; (b) the privacy carve-out was
        exact-name, so `human-input.ndjson.bak` (editor backup) mirrored
        — and pushed."""
        (self.run / "reports").mkdir()
        (self.run / "reports" / "old-name.md").write_text("v1\n")
        (self.run / "human-input.ndjson.bak").write_text("SECRET\n")
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        first = gitops.run_git(self.repo, "ls-files", f"ai/{self.run.name}")
        self.assertIn("old-name.md", first)
        self.assertNotIn(".bak", first)                 # prefix carve-out
        # rename in the run dir -> mirror must not keep both
        (self.run / "reports" / "old-name.md").rename(
            self.run / "reports" / "new-name.md")
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        second = gitops.run_git(self.repo, "ls-files", f"ai/{self.run.name}")
        self.assertIn("new-name.md", second)
        self.assertNotIn("old-name.md", second)         # pruned

    def test_mirror_onto_the_live_run_is_refused(self):
        """Adversarial-review HIGH (reproduced): when the repo IS the
        workspace, dest == run_dir and the prune would delete the live
        run's seals + stamp .mirror, bricking it beyond reseal. Refuse."""
        # self.repo is a repo inside self.workspace; make a run whose dir
        # sits under self.repo/ai so dest == run_dir
        run_in_repo = self.repo / "ai" / "2026-01-01-Z"
        state_mod.bootstrap(run_in_repo, self.workspace,
                            work_item={"id": "Z", "title": "t", "provider_ref": ""},
                            mode="quick", change_type="fix",
                            tasks=[{"id": "T1"}], entry_step="fetch")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.publish_mirror(self.repo, run_in_repo, self.config,
                                  run_in_repo.name)
        self.assertIn("live run itself", str(ctx.exception))
        # the run survives untouched
        self.assertTrue((run_in_repo / "state.yaml.hmac").exists())
        self.assertFalse((run_in_repo / ".mirror").exists())
        self.assertTrue(state_mod.load(run_in_repo, self.workspace))

    def test_mirror_is_marked_and_refuses_to_load_as_a_run(self):
        """Dogfood A2 finding: the mirror is a dead ringer for a run dir
        minus its seals, so a relative --run resolved from the repo's cwd
        read it and reported "no integrity seal" — indistinguishable from
        tampering. The marker names the actual problem."""
        (self.run / "plan.md").write_text("# plan\n")
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        mirrored = self.repo / "ai" / self.run.name
        self.assertTrue((mirrored / ".mirror").is_file())
        with self.assertRaises(state_mod.StateError) as ctx:
            state_mod.load(mirrored, self.workspace)
        self.assertIn("MIRROR snapshot", str(ctx.exception))
        # republish keeps the marker (the prune sweep must not eat it)
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        self.assertTrue((mirrored / ".mirror").is_file())

    def test_mirror_message_uses_declared_class(self):
        (self.run / "plan.md").write_text("# plan\n")
        gitops.publish_mirror(self.repo, self.run, self.config, self.run.name)
        subject = gitops.run_git(self.repo, "log", "-1", "--format=%s")
        self.assertEqual(subject,
                         f"chore(harness): publish run snapshot {self.run.name}")

    def test_sync_branch_rebases_cleanly(self):
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "feat.txt").write_text("f\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat")
        gitops.run_git(self.repo, "checkout", "main")
        (self.repo / "main.txt").write_text("m\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main moved")
        gitops.run_git(self.repo, "checkout", "feature")
        gitops.sync_branch(self.repo, "main")
        self.assertTrue((self.repo / "main.txt").exists())

    def test_sync_branch_picks_up_commits_only_on_the_REMOTE_base(self):
        """The defect this verb existed to fix but couldn't: it rebased onto
        the LOCAL base, which nothing in the package ever fetched. Its one
        documented use is "if the base moved upstream, sync FIRST" — and a
        base that moved ONLY upstream was invisible, so the rebase was a
        no-op that reported success and the caller pushed believing it had
        caught up. Local main is deliberately left stale here: if the fetch
        were removed, the rebase would still "succeed" and upstream.txt would
        not exist."""
        origin = make_repo(self.workspace, "origin-sync")
        gitops.run_git(self.workspace, "clone", str(origin), "sync-clone")
        clone = self.workspace / "sync-clone"
        gitops.run_git(clone, "config", "user.email", "t@t")
        gitops.run_git(clone, "config", "user.name", "t")
        gitops.run_git(clone, "checkout", "-b", "feature")
        (clone / "feat.txt").write_text("f\n")
        gitops.run_git(clone, "add", "-A")
        gitops.run_git(clone, "commit", "-m", "feat")
        (origin / "upstream.txt").write_text("u\n")      # base moves UPSTREAM only
        gitops.run_git(origin, "add", "-A")
        gitops.run_git(origin, "commit", "-m", "upstream work")

        out = gitops.sync_branch(clone, "main")
        self.assertTrue(out["remote_verified"])
        self.assertTrue((clone / "upstream.txt").exists())
        self.assertTrue((clone / "feat.txt").exists())   # own work preserved

    def test_sync_branch_without_a_remote_says_it_could_not_verify(self):
        # offline / no remote must not block the PR-comment loop, but must
        # not be reported as a real sync either
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "feat.txt").write_text("f\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat")
        out = gitops.sync_branch(self.repo, "main")
        self.assertFalse(out["remote_verified"])

    def test_sync_branch_conflict_aborts_cleanly(self):
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "README.md").write_text("feature version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat readme")
        gitops.run_git(self.repo, "checkout", "main")
        (self.repo / "README.md").write_text("main version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main readme")
        gitops.run_git(self.repo, "checkout", "feature")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.sync_branch(self.repo, "main")
        self.assertIn("aborted cleanly", str(ctx.exception))
        status = gitops.run_git(self.repo, "status")
        self.assertNotIn("rebase in progress", status)

    def _add_bare_origin(self) -> Path:
        bare = self.workspace / "origin.git"
        gitops.run_git(self.workspace, "init", "--bare", str(bare))
        gitops.run_git(self.repo, "remote", "add", "origin", str(bare))
        return bare

    def test_push_publishes_branch_to_origin(self):
        # adversarial-review finding: nothing anywhere ever pushed — sync-branch
        # is a rebase, not a push. harness push is the owned entry point that
        # closes that gap (RC1: never a raw `git push`).
        bare = self._add_bare_origin()
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "feat.txt").write_text("f\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat")
        gitops.push_branch(self.repo, "feature")
        self.assertIn("feature", gitops.run_git(bare, "branch", "--list", "feature"))

    def test_push_force_with_lease_after_history_rewrite(self):
        bare = self._add_bare_origin()
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "feat.txt").write_text("f\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat")
        gitops.push_branch(self.repo, "feature")
        first_sha = gitops.head_sha(self.repo)
        gitops.run_git(self.repo, "commit", "--amend", "-m", "feat (amended)")
        self.assertNotEqual(gitops.head_sha(self.repo), first_sha)
        gitops.push_branch(self.repo, "feature", force_with_lease=True)
        remote_sha = gitops.run_git(bare, "rev-parse", "feature")
        self.assertEqual(remote_sha, gitops.head_sha(self.repo))

    def test_push_without_lease_after_rewrite_is_rejected(self):
        bare = self._add_bare_origin()
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "feat.txt").write_text("f\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feat")
        gitops.push_branch(self.repo, "feature")
        gitops.run_git(self.repo, "commit", "--amend", "-m", "feat (amended)")
        with self.assertRaises(gitops.GitError):
            gitops.push_branch(self.repo, "feature")   # plain push, non-fast-forward


class SubagentModelResolution(GitopsHarness):
    """adversarial-review finding: subagent_models was declared, validated,
    and documented as design.md piece 3's "single control point" — but no
    spawn instruction anywhere ever resolved it, so a per-mode override was
    silently inert."""

    def test_default_inherit_when_unconfigured(self):
        self.assertEqual(
            workflow.resolve_subagent_model(self.config, "reviewer", "pre-pr"),
            "inherit")

    def test_scalar_form_applies_to_every_mode(self):
        config = {**self.config, "subagent_models": {"developer": "claude-opus-4-8"}}
        self.assertEqual(workflow.resolve_subagent_model(config, "developer", "develop"),
                         "claude-opus-4-8")
        self.assertEqual(workflow.resolve_subagent_model(config, "developer", "harden"),
                         "claude-opus-4-8")

    def test_object_form_per_mode_wins_over_default(self):
        config = {**self.config, "subagent_models": {
            "reviewer": {"default": "inherit", "pre-pr": "claude-opus-4-8"}}}
        self.assertEqual(workflow.resolve_subagent_model(config, "reviewer", "pre-pr"),
                         "claude-opus-4-8")
        self.assertEqual(workflow.resolve_subagent_model(config, "reviewer", "review"),
                         "inherit")

    def test_unconfigured_shape_falls_back_to_inherit(self):
        config = {**self.config, "subagent_models": {"developer": "claude-opus-4-8"}}
        self.assertEqual(workflow.resolve_subagent_model(config, "planner", "plan"),
                         "inherit")


class WriteBackResolution(GitopsHarness):
    """adversarial-review finding: write_back.on_develop_start/on_in_review
    were declared, defaulted, and documented — only on_done was ever
    consulted anywhere. Per-work-item-type status_mapping (also documented,
    e.g. `Incident: {done: Mitigated}`) only ever read the 'default' key."""

    def setUp(self):
        super().setUp()
        self.config["provider"] = {"work_item": "github"}

    def test_default_flags_match_shipped_config(self):
        # shipped default: on_develop_start true, on_in_review false, on_done true
        self.assertEqual(
            workflow.resolve_write_back_status(self.config, "develop_start", None),
            "open")
        self.assertIsNone(
            workflow.resolve_write_back_status(self.config, "in_review", None))
        self.assertEqual(
            workflow.resolve_write_back_status(self.config, "done", None),
            "closed")

    def test_flag_off_returns_none_even_with_a_type_override(self):
        config = {**self.config, "write_back": {"on_in_review": False}}
        config["status_mapping"] = {"default": {"in-review": "Custom"}}
        self.assertIsNone(workflow.resolve_write_back_status(config, "in_review", None))

    def test_per_type_status_mapping_overrides_default(self):
        config = {**self.config, "status_mapping": {
            "default": {"done": "Released"},
            "Incident": {"done": "Mitigated"}}}
        self.assertEqual(workflow.resolve_write_back_status(config, "done", "Incident"),
                         "Mitigated")
        self.assertEqual(workflow.resolve_write_back_status(config, "done", "Bug"),
                         "Released")   # unmapped type falls back to 'default'


class WriteBackMcpCarveOut(GitopsHarness):
    """adversarial-review round 2 finding: write_back() had no MCP-transport
    carve-out, unlike reconcile_flow which got exactly this fix in the same
    diff for the identical problem — dispatch() always raises for MCP
    transport, and develop.md calls write-back --milestone develop_start
    UNCONDITIONALLY at the very start of every full-mode run (on_develop_start
    defaults true), with no prior step that could have handled the
    transition itself first."""

    def test_develop_start_does_not_raise_for_mcp_provider(self):
        self.config["provider"] = {"work_item": "jira"}
        result = workflow.write_back(self.workspace, self.run, self.config,
                                     "develop_start")
        self.assertEqual(result["written"], False)
        self.assertIn("mcp_guidance", result)
        self.assertIn("work_item.transition", result["mcp_guidance"])

    def test_cli_transport_provider_still_writes_back(self):
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch") as mock_dispatch:
            result = workflow.write_back(self.workspace, self.run, self.config,
                                         "develop_start")
        self.assertEqual(result["written"], True)
        mock_dispatch.assert_called_once()


class WriteBackIsBestEffort(GitopsHarness):
    """field: US-CHAT-00 run. Both milestone write-back call sites dispatched
    the provider transition bare, so a provider refusal propagated out of a
    step whose own contract calls it "never a blocking requirement" — a real
    run aborted `develop` at its very first verb because the story file
    carried a slug-suffixed filename. The refusal must be RECORDED and
    reported, never raised — and never silently swallowed either."""

    def _events(self):
        return ndjson.read_records(self.run / "events.ndjson")

    def test_provider_refusal_is_flagged_and_reported_not_raised(self):
        self.config["provider"] = {"work_item": "github"}
        boom = ProviderError("work item 'GIT-1' not found at /s/GIT-1.md")
        with mock.patch("harness.providers.dispatch", side_effect=boom):
            result = workflow.write_back(self.workspace, self.run, self.config,
                                         "develop_start")
        self.assertEqual(result["written"], False)
        self.assertIn("not found", result["error"])       # reported, not lost
        flagged = [e for e in self._events() if e["kind"] == "write-back-failed"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["actor"], "write-back")
        self.assertEqual(flagged[0]["item"], "GIT-1")

    def test_the_failure_reaches_the_flagged_events_gauge(self):
        # a swallow nothing surfaced would be worse than the raise it replaced
        self.assertIn("write-back-failed", workflow.FLAGGED_EVENT_KINDS)
        events = [{"kind": "write-back-failed", "actor": "write-back"}]
        self.assertEqual(len(workflow.outstanding_flagged(events)), 1)
        # ...but the run's own machinery is intact — only the tracker is stale
        self.assertEqual(workflow.run_health(events)[0], "HEALTHY")

    def test_the_failure_carries_a_detail_the_metrics_report_renders(self):
        # adversarial-review, both lenses: the payload key was `error`, which
        # metrics' _detail() does not read — so the flagged row a human sees
        # rendered a bare kind name with an empty Detail cell, carrying none of
        # the information needed to act on it.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            workflow.write_back(self.workspace, self.run, self.config,
                                "develop_start")
        flagged = [e for e in self._events() if e["kind"] == "write-back-failed"]
        self.assertIn("tracker down", flagged[0]["reason"])
        report = workflow.metrics_report(self.workspace, self.run, self.manifest)
        row = [ln for ln in report.read_text(encoding="utf-8").splitlines()
               if "write-back-failed" in ln]
        self.assertTrue(row)
        self.assertIn("tracker down", row[0])

    def test_a_later_success_clears_the_earlier_miss(self):
        # adversarial-review, both lenses: filed permanent, the miss outlived
        # the condition that caused it — a run whose `done` write-back landed
        # has a correct tracker and nothing outstanding, but the gauge kept
        # reporting 1 forever. Same resolver shape as env-prereq-satisfied.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            workflow.write_back(self.workspace, self.run, self.config,
                                "develop_start")
        self.assertEqual(len(workflow.outstanding_flagged(self._events())), 1)
        with mock.patch("harness.providers.dispatch"):      # human fixed it
            self.assertEqual(                               # the `done` milestone
                workflow.write_back(self.workspace, self.run, self.config,
                                    "done")["written"], True)
        self.assertEqual(workflow.outstanding_flagged(self._events()), [])

    def test_a_stray_log_event_cannot_clear_a_genuine_miss(self):
        # re-verify finding: `log-event` is unvalidated, and this resolver
        # CLEARS an audit gauge — verified going 1 -> 0 on a hand-appended
        # kind. Actor-checked now, exactly like `plan-registered`.
        events = [{"kind": "write-back-failed", "actor": "write-back"},
                  {"kind": "write-back-succeeded"}]                 # no actor
        self.assertEqual(len(workflow.outstanding_flagged(events)), 1)
        events[1]["actor"] = "reconcile"                            # the real one
        self.assertEqual(workflow.outstanding_flagged(events), [])

    def test_the_success_marker_fires_once_not_on_every_later_run(self):
        # re-verify finding: gated on "a miss appears anywhere in the ledger",
        # which stays true forever once resolved — so every clean write-back
        # after the first miss appended another marker. Same bug
        # `_has_open_env_miss` exists to prevent for env-prereq-satisfied.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            workflow.write_back(self.workspace, self.run, self.config,
                                "develop_start")
        with mock.patch("harness.providers.dispatch"):
            for _ in range(3):
                workflow.write_back(self.workspace, self.run, self.config,
                                    "done")
        self.assertEqual(
            len([e for e in self._events()
                 if e["kind"] == "write-back-succeeded"]), 1)

    def test_an_unwritable_ledger_does_not_break_a_successful_write_back(self):
        # re-verify finding: the success branch's ledger read/append were left
        # bare while the failure branch was guarded — so a full or read-only
        # run dir raised out of a call that had already succeeded, post-merge
        # from reconcile.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            workflow.write_back(self.workspace, self.run, self.config,
                                "develop_start")
        with mock.patch("harness.providers.dispatch"), \
             mock.patch("harness.ndjson.read_records",
                        side_effect=OSError(28, "No space left on device")):
            result = workflow.write_back(self.workspace, self.run, self.config,
                                         "done")
        self.assertEqual(result["written"], True)

    def test_a_clean_write_back_emits_no_success_marker(self):
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch"):
            workflow.write_back(self.workspace, self.run, self.config,
                                "develop_start")
        self.assertEqual([e for e in self._events()
                          if e["kind"] == "write-back-succeeded"], [])

    def test_reconcile_reports_the_refusal_it_no_longer_raises(self):
        # adversarial-review, both lenses: reconcile dropped the helper's
        # result, so `harness reconcile` returned a bare {"reconciled": true}
        # — a refused transition was indistinguishable from a clean sync at
        # the one decision point the orchestrator actually reads.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            result = workflow.reconcile_flow(self.workspace, self.run,
                                             self.config, self.fsm)
        self.assertEqual(result["reconciled"], True)
        self.assertEqual(result["write_back"]["written"], False)
        self.assertIn("tracker down", result["write_back"]["error"])

    def test_declared_unsupported_still_refuses_rather_than_flagging(self):
        # adversarial-review, lens B: ProviderUnsupported subclasses
        # ProviderError, so the bare catch swallowed it too — turning a
        # provider that DECLARES no transition support into a flagged event on
        # every milestone of every run, instead of the config-time refusal it
        # is. Declared-unsupported is a statement about the provider, not a
        # runtime "no".
        from harness.providers import ProviderUnsupported
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderUnsupported("declares no support")):
            with self.assertRaises(ProviderUnsupported):
                workflow.write_back(self.workspace, self.run, self.config,
                                    "develop_start")
        self.assertEqual(
            [e for e in self._events() if e["kind"] == "write-back-failed"], [])

    def test_an_unwritable_ledger_does_not_replace_the_suppressed_error(self):
        # adversarial-review, lens B: the append lives INSIDE the except, so a
        # full or read-only run dir converted a suppressed ProviderError into a
        # different, raised OSError. "Never raises" is stated unconditionally.
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")), \
             mock.patch("harness.ndjson.append_record",
                        side_effect=OSError(28, "No space left on device")):
            result = workflow.write_back(self.workspace, self.run, self.config,
                                         "develop_start")
        self.assertEqual(result["written"], False)
        self.assertIn("tracker down", result["error"])

    def test_reconcile_completes_its_ledger_work_despite_a_refusal(self):
        # reconcile runs POST-merge: raising here fails a run whose work is
        # already landed, and leaves worktrees swept but the ledger unreconciled
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=ProviderError("tracker down")):
            result = workflow.reconcile_flow(self.workspace, self.run,
                                             self.config, self.fsm)
        self.assertEqual(result["reconciled"], True)
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("reconciled", kinds)                # the step still closed
        flagged = [e for e in self._events() if e["kind"] == "write-back-failed"]
        self.assertEqual([f["actor"] for f in flagged], ["reconcile"])

    def test_mcp_transport_refusal_is_not_demoted_to_best_effort(self):
        # caught building this change: the first cut of the swallow also ate
        # reconcile's MCP carve-out, whose raise is the MECHANISM telling the
        # orchestrator to invoke the mapped tool itself and pass
        # --skip-transition. Best-effort covers "the provider said no", never
        # "this transport is not script-callable" — demoting it would let a
        # run reconcile with its tracker silently never synced.
        self.config["provider"] = {"work_item": "jira"}
        with self.assertRaises(ProviderError):
            workflow.reconcile_flow(self.workspace, self.run, self.config,
                                    self.fsm)
        self.assertEqual(
            [e for e in self._events() if e["kind"] == "write-back-failed"], [])

    def test_a_non_provider_error_still_raises(self):
        # best-effort covers "the provider said no", not a bug in our own
        # dispatch layer — swallowing that would hide a real defect
        self.config["provider"] = {"work_item": "github"}
        with mock.patch("harness.providers.dispatch",
                        side_effect=RuntimeError("bug in dispatch")):
            with self.assertRaises(RuntimeError):
                workflow.write_back(self.workspace, self.run, self.config,
                                    "develop_start")


class SecurityScanTimeout(GitopsHarness):
    def test_scanner_timeout_is_surfaced_as_worst_severity_not_a_crash(self):
        # adversarial-review finding: subprocess.TimeoutExpired was uncaught
        # in security_scan's per-repo scan — a raw traceback for the WHOLE
        # step, and (had it been silently treated as "no finding") a clean
        # verdict would be exactly the wrong default for a security gate.
        st = state_mod.load(self.run, self.workspace)
        st["cursor"]["current_step"] = "security"
        state_mod.save(self.run, self.workspace, st)
        config = {**self.config, "repos": {"repo": str(self.repo)},
                 "security": {**self.config["security"],
                             "scan_cmd": {"repo": "some-slow-scanner"}}}
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("some-slow-scanner", 900)):
            sev = workflow.security_scan(self.workspace, self.run, config, self.manifest)
        self.assertEqual(sev, "critical")   # order[-1] — worst, forces review
        report = (self.run / "reports" / "security.md").read_text(encoding="utf-8")
        self.assertIn("timed out", report)


class ReconcileMcpCarveOut(GitopsHarness):
    """adversarial-review finding: reconcile_flow had no MCP-transport carve-
    out (unlike fetch, which already has one) — `harness reconcile` refused
    every time for an MCP-transport work-item provider, since dispatch()
    always raises for that transport by construction (no script-callable
    path exists). --skip-transition lets the orchestrator do the transition
    itself first, same pattern as fetch.md's --from-raw."""

    def test_reconcile_raises_for_mcp_provider_without_skip(self):
        self.config["provider"] = {"work_item": "jira"}
        with self.assertRaises(ProviderError):
            workflow.reconcile_flow(self.workspace, self.run, self.config, self.fsm)

    def test_reconcile_with_skip_transition_completes_archiving(self):
        self.config["provider"] = {"work_item": "jira"}
        st = state_mod.load(self.run, self.workspace)
        st["tasks"][0]["status"] = "done"
        state_mod.save(self.run, self.workspace, st)
        result = workflow.reconcile_flow(self.workspace, self.run, self.config,
                                         self.fsm, skip_transition=True)
        self.assertEqual(result, {"reconciled": True})
        st = state_mod.load(self.run, self.workspace)
        self.assertEqual(st["tasks"][0]["status"], "archived")


class TestQuarantine(GitopsHarness):
    """field: dual-run comparison — one pre-existing, unrelated
    failing spec was rediscovered and routed around FOUR times across two
    runs of the same story (it blocked a task's completion in one and
    aborted the frontend coverage run three times in the other). The
    per-call `--test-cmd` override could express the workaround; nothing
    carried the knowledge between runs. Loud by construction: required
    reason+since, a refusal when the runner flag is missing, and a flagged
    event on every exclusion."""

    def _config(self, quarantine=None, coverage_cmd=None):
        cfg = dict(self.config)
        cfg["repos"] = {"repo": str(self.repo)}
        entry = {"test_cmd": support.NOP_TEST_CMD}
        if coverage_cmd:
            entry["coverage_cmd"] = coverage_cmd
        if quarantine is not None:
            entry["quarantine"] = quarantine
        cfg["language"] = {**(cfg.get("language") or {}),
                           "repos": {"repo": entry}}
        return cfg

    ONE = {"exclude_template": "--exclude {test}",
           "tests": [{"test": "tests/harden-fe010.spec.ts",
                      "reason": "pre-existing appVersion mismatch on main",
                      "since": "2026-07-22"}]}

    def test_renders_exclusions_and_flags_them(self):
        cfg = self._config(self.ONE)
        out = initws.quarantine_cmd(cfg, self.repo, "npm test", self.run)
        self.assertEqual(out, "npm test --exclude tests/harden-fe010.spec.ts")
        flagged = [e for e in ndjson.read_records(self.run / "events.ndjson")
                   if e["kind"] == "tests-quarantined"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["tests"], ["tests/harden-fe010.spec.ts"])
        self.assertIn("appVersion", flagged[0]["reasons"]
                      ["tests/harden-fe010.spec.ts"])
        # and it is on the shared flagged-events surface, not a private list
        self.assertIn("tests-quarantined", workflow.FLAGGED_EVENT_KINDS)

    def test_multiple_entries_each_render(self):
        cfg = self._config({
            "exclude_template": "--deselect {test}",
            "tests": [{"test": "a_test.py", "reason": "flaky", "since": "2026-07-01"},
                      {"test": "b_test.py", "reason": "flaky", "since": "2026-07-02"}]})
        self.assertEqual(initws.quarantine_cmd(cfg, self.repo, "pytest"),
                         "pytest --deselect a_test.py --deselect b_test.py")

    def test_missing_exclude_template_refuses(self):
        cfg = self._config({"tests": [{"test": "a", "reason": "r",
                                       "since": "2026-07-01"}]})
        with self.assertRaises(initws.QuarantineError) as ctx:
            initws.quarantine_cmd(cfg, self.repo, "npm test")
        msg = str(ctx.exception)
        self.assertIn("exclude_template", msg)
        self.assertIn("--exclude {test}", msg)     # names the runner flags
        self.assertIn("Refusing", msg)             # never a silent full suite

    def test_entry_without_reason_or_since_refuses(self):
        for bad in ({"test": "a", "since": "2026-07-01"},
                    {"test": "a", "reason": "r"},
                    {"test": "a", "reason": "  ", "since": "2026-07-01"},
                    {"reason": "r", "since": "2026-07-01"}):
            cfg = self._config({"exclude_template": "-x {test}", "tests": [bad]})
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, "npm test")

    def test_no_quarantine_leaves_the_command_byte_identical(self):
        for cfg in (self._config(), self._config({}),
                    self._config({"exclude_template": "-x {test}", "tests": []})):
            self.assertEqual(
                initws.quarantine_cmd(cfg, self.repo, "npm test"), "npm test")
        # an unregistered repo path resolves to no name and is left alone too
        self.assertEqual(
            initws.quarantine_cmd(self._config(self.ONE), self.workspace / "nope",
                                  "npm test"), "npm test")
        self.assertEqual(ndjson.read_records(self.run / "events.ndjson"), [])

    def test_coverage_command_gets_the_same_exclusions(self):
        # the coverage run is the OTHER path the quarantined spec kept killing
        cfg = self._config(self.ONE, coverage_cmd="npm run coverage")
        self.assertEqual(
            initws.quarantine_cmd(cfg, self.repo,
                                  initws.resolve_coverage_cmd(cfg, self.repo)),
            "npm run coverage --exclude tests/harden-fe010.spec.ts")

    def test_malformed_block_refuses_instead_of_reading_as_empty(self):
        # adversarial-review, both lenses: these are the shapes a user
        # actually writes. Collapsing them to "nothing quarantined" left the
        # user believing a config file fixed a failure it never touched.
        for bad in ("tests/foo.spec.ts",
                    [{"test": "a", "reason": "r", "since": "2026-07-01"}],
                    {"exclude_template": "-x {test}",     # typo'd `tests`
                     "test": [{"test": "a", "reason": "r", "since": "d"}]}):
            cfg = self._config(bad)
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, "npm test")

    def test_shell_composed_command_refuses(self):
        # flags are APPENDED, so they would land on `tee`, not the runner
        cfg = self._config(self.ONE)
        for cmd in ("cd fe && npm test", "npm test | tee log", "a; b"):
            with self.assertRaises(initws.QuarantineError) as ctx:
                initws.quarantine_cmd(cfg, self.repo, cmd)
            self.assertIn("shell-composed", str(ctx.exception))

    def test_coverage_uses_its_own_template_when_declared(self):
        # a coverage_cmd that is a DIFFERENT tool must not get the test
        # runner's flag appended (adversarial-review)
        cfg = self._config({**self.ONE,
                            "coverage_exclude_template": "--ignore={test}"})
        self.assertEqual(
            initws.quarantine_cmd(cfg, self.repo, "nyc report", coverage=True),
            "nyc report --ignore=tests/harden-fe010.spec.ts")
        # …and falls back to exclude_template when it has none of its own
        self.assertEqual(
            initws.quarantine_cmd(self._config(self.ONE), self.repo,
                                  "nyc report", coverage=True),
            "nyc report --exclude tests/harden-fe010.spec.ts")

    def test_event_is_emitted_once_per_run_not_once_per_call(self):
        # adversarial-review, both lenses: per-application emission put ~12
        # identical records in the gauge a human reads to triage a run
        cfg = self._config(self.ONE)
        for _ in range(4):
            initws.quarantine_cmd(cfg, self.repo, "npm test", self.run)
        flagged = [e for e in ndjson.read_records(self.run / "events.ndjson")
                   if e["kind"] == "tests-quarantined"]
        self.assertEqual(len(flagged), 1)
        # SINGULAR `reason` is what the metrics flagged table renders —
        # `reasons` (the per-entry map) is not it, so the row that was
        # supposed to name the exclusions came out blank in the one surface
        # a human reads (whole-branch adversarial review)
        self.assertIn("harden-fe010", flagged[0].get("reason", ""))

    def test_overlap_with_the_locked_test_set_refuses(self):
        # the one silently-wrong pass this mechanism must not produce: the
        # task's own test excluded, so verify-green never executes it while
        # the SHA check still confirms the file is unchanged
        self._write_test()
        cfg = self._config({"exclude_template": "--ignore={test}",
                            "tests": [{"test": "tests/test_x.py",
                                       "reason": "pre-existing",
                                       "since": "2026-07-22"}]})
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_red(self.run, self.workspace, self.repo, cfg, "T1",
                              TEST_CMD, declared=["tests/test_x.py"],
                              intents=["test_val"])
        self.assertIn("locked test set", str(ctx.exception))

    def test_init_verify_gates_a_malformed_block(self):
        # caught where fixing config is cheap, not mid-develop at verify-red
        cfg = self._config({"tests": [{"test": "a", "reason": "r",
                                       "since": "2026-07-01"}]})  # no template
        checks = initws.verify(cfg)
        bad = next(c for c in checks if c["check"] == "quarantine:repo")
        self.assertEqual(bad["status"], "fail")
        self.assertIn("exclude_template", bad["detail"])
        # a well-formed block passes
        ok = next(c for c in initws.verify(self._config(self.ONE))
                  if c["check"] == "quarantine:repo")
        self.assertEqual(ok["status"], "pass")

    def test_resolve_test_cmd_verb_carries_the_exclusions(self):
        # the owned entry point agent-run suites build their header from —
        # without it, develop/review/pre-pr/harden ran the raw command and
        # re-hit the quarantined failure (adversarial-review, blocking)
        cfg = self._config(self.ONE)
        self.assertEqual(
            initws.quarantine_cmd(cfg, self.repo,
                                  initws.resolve_test_cmd(cfg, self.repo)),
            f"{support.NOP_TEST_CMD} --exclude tests/harden-fe010.spec.ts")

    def test_verify_green_applies_it_and_refuses_an_overlap(self):
        # re-verify finding: BOTH halves of verify-green's quarantine wiring
        # (the exclusion and the overlap guard) could be deleted with the
        # whole suite still green — the silent-false-green guard was unpinned.
        self._write_test()
        cfg = self._config({"exclude_template": "--ignore={test}",
                            "tests": [{"test": "tests/quarantined_test.py",
                                       "reason": "pre-existing",
                                       "since": "2026-07-22"}]})
        proof = {"tests": {"tests/test_x.py": "sha"}, "closure": {}}
        seen = []
        with mock.patch("harness.gitops.blob_sha", return_value="sha"), \
                mock.patch("harness.gitops._run_tests",
                           side_effect=lambda r, c: (seen.append(c), (0, ""))[1]):
            gitops.verify_green(proof, self.repo, "pytest", config=cfg,
                                task_repo=self.repo, run=self.run)
        self.assertEqual(seen, ["pytest --ignore=tests/quarantined_test.py"])

        overlapping = self._config({"exclude_template": "--ignore={test}",
                                    "tests": [{"test": "tests/test_x.py",
                                               "reason": "r",
                                               "since": "2026-07-22"}]})
        with mock.patch("harness.gitops.blob_sha", return_value="sha"):
            with self.assertRaises(gitops.RedProofError) as ctx:
                gitops.verify_green(proof, self.repo, "pytest",
                                    config=overlapping, task_repo=self.repo,
                                    run=self.run)
        self.assertIn("locked test set", str(ctx.exception))

        # …and the shape-aware half at THIS call site too, not only at
        # verify_red's (re-verification: the new coverage exercised verify_red
        # only, and verify-green is the call that produces the silent pass)
        by_dir = self._config({"exclude_template": "--ignore={test}",
                               "tests": [{"test": "tests", "reason": "r",
                                          "since": "2026-07-22"}]})
        with mock.patch("harness.gitops.blob_sha", return_value="sha"):
            with self.assertRaises(gitops.RedProofError):
                gitops.verify_green(proof, self.repo, "pytest", config=by_dir,
                                    task_repo=self.repo, run=self.run)

    def test_overlap_check_is_spelling_insensitive(self):
        """Every spelling a RUNNER treats as the same file must either be
        refused at declaration or be caught by the overlap guard — never
        excluded-but-unmatched, which is the silent false green.

        re-verify finding: the first version validated the STRIPPED value but
        rendered the RAW one, so `"tests/x.py "` (a quoted YAML scalar keeps
        the space) sailed through both, excluded the task's own test, and
        left verify-green passing with the assertion never executed."""
        for spelling in ("./tests/test_x.py", "tests//test_x.py",
                         "tests/./test_x.py", "tests/sub/../test_x.py",
                         " tests/test_x.py", "tests/test_x.py ",
                         str(self.repo / "tests/test_x.py"),
                         "tests\\test_x.py"):
            cfg = self._config({"exclude_template": "--ignore={test}",
                                "tests": [{"test": spelling, "reason": "r",
                                           "since": "2026-07-22"}]})
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, "pytest")

    def test_case_only_difference_still_trips_the_overlap_guard(self):
        # on a case-insensitive filesystem these are one file, so a
        # case-only difference must not slip the guard (fail toward refusing)
        self._write_test()
        cfg = self._config({"exclude_template": "--ignore={test}",
                            "tests": [{"test": "Tests/Test_X.py",
                                       "reason": "r", "since": "2026-07-22"}]})
        with self.assertRaises(gitops.RedProofError):
            gitops.verify_red(self.run, self.workspace, self.repo, cfg, "T1",
                              TEST_CMD, declared=["tests/test_x.py"],
                              intents=["test_val"])

    def test_a_typod_run_refuses_instead_of_manufacturing_one(self):
        """Whole-branch review, reproduced: `--run` is optional on these two
        verbs, so they sit in NO_RUN and skip the required-run check — and
        `ndjson.append_record`'s mkdir(parents=True) then built an entire
        phantom run directory from a typo, returned ok:true, and left the
        REAL run without its `tests-quarantined` event while the exclusions
        applied invisibly. `save_report` closed exactly this and called
        itself "the one run-scoped verb" that had skipped the check."""
        ghost = self.workspace / "ai" / "2026-07-25-TYPO"
        for verb in ("resolve-test-cmd", "resolve-coverage-cmd"):
            proc = subprocess.run([sys.executable, "-m", "harness", "--workspace",
                           str(self.workspace), verb, "--repo", str(self.repo),
                           "--run", str(ghost)],
                          cwd=Path(__file__).resolve().parent.parent,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
            out = json.loads(proc.stdout)
            self.assertFalse(out["ok"], out)
            self.assertIn("not a run", out["error"])
            self.assertFalse(ghost.exists())

    def test_directory_and_glob_entries_trip_the_overlap_guard(self):
        """Whole-branch review, reproduced end to end: the guard was an exact
        set intersection, so a DIRECTORY (`tests/legacy`) or a GLOB
        (`tests/**`) excluded the task's own locked test with no overlap
        seen at all — verify-green passing while the assertion never ran.

        Neither shape is exotic or malformed: pytest's `--ignore` takes a
        directory and vitest's `--exclude` (the shipped example template) is
        glob-native, and both spellings are perfectly canonical, so the
        declaration vocabulary could never have caught them."""
        self._write_test()          # tests/test_x.py
        for entry in ("tests", "tests/**", "tests/*.py", "tests/test_?.py",
                      "tests/test_[wxy].py", "tests/*"):
            cfg = self._config({"exclude_template": "--ignore={test}",
                                "tests": [{"test": entry, "reason": "r",
                                           "since": "2026-07-22"}]})
            with self.assertRaises(gitops.RedProofError) as ctx:
                gitops.verify_red(self.run, self.workspace, self.repo, cfg,
                                  "T1", TEST_CMD,
                                  declared=["tests/test_x.py"],
                                  intents=["test_val"])
            self.assertIn("locked test set", str(ctx.exception))
            # the pair is named — with a directory or glob entry, naming only
            # the entry leaves the developer guessing which file it swallowed
            self.assertIn("tests/test_x.py", str(ctx.exception))

    def test_a_non_covering_directory_or_glob_still_runs(self):
        # the other half of the shape-aware comparison: over-refusal is the
        # safe direction, but it must not swallow a legitimate quarantine of
        # a NEIGHBOURING directory or spec family (regression guard)
        self._write_test()
        for entry in ("tests/legacy", "tests/*.spec.ts", "e2e/**"):
            cfg = self._config({"exclude_template": "--ignore={test}",
                                "tests": [{"test": entry, "reason": "r",
                                           "since": "2026-07-22"}]})
            gitops._refuse_quarantine_overlap(cfg, self.repo,
                                              {"tests/test_x.py": "sha"})

    def test_overlap_refusal_fires_before_the_misleading_passes_message(self):
        """pre-release review: with the overlap check AFTER the code==0
        raise, quarantining the task's own test made the excluded suite pass
        and 'PASSES — not red' fired first — sending the developer to fix a
        'vacuous' test while the real cause was the exclusion (the exact
        field pain the guard's docstring cites)."""
        self._write_test()
        self._write_impl()      # suite would genuinely pass when excluded
        cfg = self._config({"exclude_template": "--ignore={test}",
                            "tests": [{"test": "tests/test_x.py",
                                       "reason": "misattributed",
                                       "since": "2026-07-22"}]})
        with self.assertRaises(gitops.RedProofError) as ctx:
            gitops.verify_red(self.run, self.workspace, self.repo, cfg, "T1",
                              TEST_CMD, declared=["tests/test_x.py"],
                              intents=["test_val"])
        self.assertIn("locked test set", str(ctx.exception))
        self.assertNotIn("not red", str(ctx.exception))

    def test_windows_wrapper_spellings_refuse_too(self):
        # pre-release review: `sh.exe -c`, `cmd /c` and `powershell
        # -Command` slipped the POSIX-only wrapper pattern — reviving the
        # silent-full-suite false negative on the platform whose toolchains
        # wrap commands most
        cfg = self._config(self.ONE)
        for bad in ('sh.exe -c "npm test"', 'bash.exe -lc "npm test"',
                    'cmd /c "npm test"', 'cmd.exe /d /c "npm test"',
                    'powershell -Command "npm test"',
                    'pwsh.exe -NoProfile -c "npm test"'):
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, bad)

    def test_wrapped_and_multiline_commands_refuse(self):
        """re-verify finding: the quote-aware scan let `sh -c "cd fe && …"`
        through — the flags became the wrapper's arguments and never reached
        the runner, so the full suite ran while init-verify said `pass`. A
        false negative strictly worse than the false positive it replaced."""
        cfg = self._config(self.ONE)
        for bad in ('sh -c "cd frontend && npx vitest run"',
                    'bash -lc "npm test | tee log"',
                    "docker compose run --rm test sh -c 'pytest && flake8'",
                    "npm test\nnpm run coverage",
                    "npm test & npm run lint"):
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, bad)
        # …while a quoted regex alternation is still a normal single command
        for ok in ('go test ./... -run "TestA|TestB"',
                   'npx vitest run -t "auth\\"quoted|token"'):
            self.assertTrue(
                initws.quarantine_cmd(cfg, self.repo, ok).startswith(ok))

    def test_reapplying_is_idempotent(self):
        # the documented develop path applies twice: resolve-test-cmd builds
        # the header, develop-task.md passes it back as --test-cmd to
        # verify-red (re-verify finding: rendered `--exclude x --exclude x`)
        cfg = self._config(self.ONE)
        once = initws.quarantine_cmd(cfg, self.repo, "npm test")
        self.assertEqual(initws.quarantine_cmd(cfg, self.repo, once), once)

    def test_template_without_the_placeholder_refuses(self):
        cfg = self._config({"exclude_template": "--exclude",
                            "tests": [{"test": "a", "reason": "r",
                                       "since": "2026-07-01"}]})
        with self.assertRaises(initws.QuarantineError) as ctx:
            initws.quarantine_cmd(cfg, self.repo, "npm test")
        self.assertIn("{test}", str(ctx.exception))

    def test_quoted_shell_metacharacters_are_not_composition(self):
        # re-verify finding: a bare substring scan refused every quoted regex
        # alternation — normal single commands — with irrelevant advice
        cfg = self._config(self.ONE)
        for ok in ('go test ./... -run "TestA|TestB"',
                   'npm test -- --testPathPattern "src/(a|b)"',
                   "pytest -k 'a or b;c'"):
            self.assertTrue(initws.quarantine_cmd(cfg, self.repo, ok)
                            .startswith(ok))
        for bad in ("cd fe && npm test", "npm test | tee log", "a; b"):
            with self.assertRaises(initws.QuarantineError):
                initws.quarantine_cmd(cfg, self.repo, bad)

    def test_init_verify_gates_the_coverage_template_too(self):
        # re-verify finding: verify() only exercised the test path, so a bad
        # coverage template passed init-verify and died at harden
        cfg = self._config({**self.ONE,
                            "coverage_exclude_template": "--ignore"},
                           coverage_cmd="npm run coverage")
        bad = next(c for c in initws.verify(cfg)
                   if c["check"] == "quarantine:repo")
        self.assertEqual(bad["status"], "fail")

    def test_verify_red_applies_it_through_the_real_run(self):
        # end-to-end through the choke point: the exclusion reaches the
        # executed command, and the event lands on the run's ledger
        self._write_test()
        cfg = self._config({"exclude_template": "--ignore={test}",
                            "tests": [{"test": "tests/quarantined_test.py",
                                       "reason": "pre-existing failure",
                                       "since": "2026-07-22"}]})
        seen = []

        def spy(repo, cmd):
            seen.append(cmd)
            return 1, "boom"

        with mock.patch("harness.gitops._run_tests", side_effect=spy):
            gitops.verify_red(self.run, self.workspace, self.repo, cfg,
                              "T1", "pytest", declared=["tests/test_x.py"],
                              intents=["test_val"])
        self.assertEqual(seen, ["pytest --ignore=tests/quarantined_test.py"])
        self.assertIn("tests-quarantined",
                      [e["kind"] for e in
                       ndjson.read_records(self.run / "events.ndjson")])


class RemoteBranchProbe(GitopsHarness):
    """gitops.remote_branch_exists — the remote half of the branch check
    `_branch_exists` only ever did locally (field: dual-run
    comparison). Tri-state on purpose: None means UNANSWERED, never absent."""

    def _bare_origin(self) -> Path:
        bare = self.workspace / "origin.git"
        gitops.run_git(self.workspace, "init", "--bare", str(bare))
        gitops.run_git(self.repo, "remote", "add", "origin", str(bare))
        gitops.run_git(self.repo, "push", "origin", "main")
        return bare

    def test_detects_and_denies_correctly(self):
        bare = self._bare_origin()
        self.assertFalse(gitops.remote_branch_exists(self.repo, "fix/absent"))
        gitops.run_git(bare, "branch", "fix/GIT-1-t", "main")
        self.assertTrue(gitops.remote_branch_exists(self.repo, "fix/GIT-1-t"))

    def test_matches_the_exact_ref_not_the_tail(self):
        # ls-remote matches patterns against the ref TAIL, so a bare `main`
        # would also hit `refs/heads/topic/main` — the full refs/heads/ form
        # plus the exact re-compare is what keeps this precise.
        bare = self._bare_origin()
        gitops.run_git(bare, "branch", "topic/release", "main")
        self.assertFalse(gitops.remote_branch_exists(self.repo, "release"))
        self.assertTrue(gitops.remote_branch_exists(self.repo, "topic/release"))

    def test_no_remote_is_unanswered_not_absent(self):
        self.assertIsNone(gitops.remote_branch_exists(self.repo, "anything"))

    def test_ambiguous_remotes_are_unanswered(self):
        gitops.run_git(self.repo, "remote", "add", "upstream", "u://x")
        gitops.run_git(self.repo, "remote", "add", "fork", "u://y")
        self.assertIsNone(gitops.remote_branch_exists(self.repo, "anything"))

    def test_unreachable_remote_is_unanswered_not_absent(self):
        # Offline/auth failure must NOT green-light the collision it exists
        # to catch: callers degrade to a warning on None.
        gitops.run_git(self.repo, "remote", "add", "origin",
                       str(self.workspace / "no-such-repo.git"))
        self.assertIsNone(gitops.remote_branch_exists(self.repo, "anything"))

    def test_probe_timeout_is_unanswered_not_a_crash(self):
        # An unreachable host can block on auth until the timeout fires; that
        # must surface as "unanswered", not as a traceback out of preflight.
        self._bare_origin()
        real = subprocess.run

        def only_ls_remote_times_out(args, **kwargs):
            if "ls-remote" in args:
                raise subprocess.TimeoutExpired("ls-remote", 30)
            return real(args, **kwargs)

        with mock.patch("harness.gitops.subprocess.run",
                        side_effect=only_ls_remote_times_out):
            self.assertIsNone(gitops.remote_branch_exists(self.repo, "main"))


class DefaultBranch(GitopsHarness):
    """gitops.ensure_default_branch — the reusable precondition shared by
    discover() and preflight()."""

    def test_switches_from_other_branch_when_clean(self):
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        result = gitops.ensure_default_branch(self.repo)
        # `behind` is None here: this fixture has no remote, so the staleness
        # question is unanswerable rather than answered "current"
        self.assertEqual(result, {"switched": True, "branch": "main",
                                  "from_branch": "feature", "behind": None})
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_noop_when_already_on_default(self):
        result = gitops.ensure_default_branch(self.repo)
        self.assertEqual(result, {"switched": False, "branch": "main",
                                  "behind": None})

    def test_refuses_on_uncommitted_changes_without_switching(self):
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        gitops.run_git(self.repo, "add", "-A")   # staged, not committed
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("uncommitted", str(ctx.exception))
        # never auto-stashed/discarded, never switched away
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "feature")
        self.assertIn("dirty.txt", gitops.changed_files(self.repo))

    def test_refuses_on_untracked_files_too(self):
        (self.repo / "untracked.txt").write_text("new\n")
        with self.assertRaises(gitops.GitError):
            gitops.ensure_default_branch(self.repo)

    def test_explicit_branch_override(self):
        gitops.run_git(self.repo, "checkout", "-b", "release")
        gitops.run_git(self.repo, "checkout", "main")
        gitops.run_git(self.repo, "checkout", "-b", "other")
        result = gitops.ensure_default_branch(self.repo, branch="release")
        self.assertEqual(result, {"switched": True, "branch": "release",
                                  "from_branch": "other", "behind": None})

    def test_slashed_default_branch_parsed_whole(self):
        """Adversarial-review finding: `rsplit('/')` mangled a default
        branch itself containing '/' (release-train convention) to its
        last segment — and a same-named local branch then got silently
        checked out instead."""
        remote = make_repo(self.workspace, "origin-repo")
        gitops.run_git(remote, "checkout", "-b", "release/2026")
        clone = self.workspace / "clone"
        gitops.run_git(self.workspace, "clone", str(remote), "clone")
        gitops.run_git(clone, "config", "user.email", "t@t")
        gitops.run_git(clone, "config", "user.name", "t")
        gitops.run_git(clone, "remote", "set-head", "origin", "release/2026")
        self.assertEqual(gitops.default_branch(clone), "release/2026")
        result = gitops.ensure_default_branch(clone)
        self.assertEqual(result["branch"], "release/2026")

    def _clone_with_upstream(self, name="clone"):
        remote = make_repo(self.workspace, f"origin-{name}")
        clone = self.workspace / name
        gitops.run_git(self.workspace, "clone", str(remote), name)
        gitops.run_git(clone, "config", "user.email", "t@t")
        gitops.run_git(clone, "config", "user.name", "t")
        return remote, clone

    def test_reports_how_far_the_base_trails_its_remote(self):
        """Field question, 2026-08-04: "is main pulled to latest before
        preflight?" It was not, in any mode — and being ON the default branch
        passed every check while being weeks behind it. The count is measured
        (fetch, read-only) and reported; the branch is NOT moved."""
        remote, clone = self._clone_with_upstream()
        self.assertEqual(gitops.ensure_default_branch(clone)["behind"], 0)
        before = gitops.run_git(clone, "rev-parse", "HEAD")
        for n in range(2):              # upstream moves on
            (remote / f"up{n}.txt").write_text("x\n")
            gitops.run_git(remote, "add", "-A")
            gitops.run_git(remote, "commit", "-m", f"upstream {n}")
        result = gitops.ensure_default_branch(clone)
        self.assertEqual(result["behind"], 2)
        # measured, never auto-fixed: the local branch has NOT been moved
        self.assertEqual(gitops.run_git(clone, "rev-parse", "HEAD"), before)
        self.assertEqual(gitops.changed_files(clone), [])

    def test_behind_is_none_when_unanswerable(self):
        # no remote at all — structural, not a signal; must not raise
        self.assertIsNone(gitops.base_branch_behind(self.repo, "main"))
        self.assertIsNone(gitops.ensure_default_branch(self.repo)["behind"])

    def test_refuses_when_target_branch_does_not_exist(self):
        """A guessed/passed branch that isn't real must fail closed with a
        clear message, not attempt the checkout and produce a raw pathspec
        error (the repo has no origin, so `default_branch` is a guess)."""
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo, branch="does-not-exist")
        self.assertIn("does not exist locally", str(ctx.exception))

    def test_refuses_during_bisect(self):
        shas = []
        for i in range(4):
            (self.repo / "README.md").write_text(f"v{i}\n")
            gitops.run_git(self.repo, "add", "-A")
            gitops.run_git(self.repo, "commit", "-m", f"v{i}")
            shas.append(gitops.head_sha(self.repo))
        gitops.run_git(self.repo, "bisect", "start")
        gitops.run_git(self.repo, "bisect", "bad", shas[-1])
        gitops.run_git(self.repo, "bisect", "good", shas[0])
        self.assertTrue((self.repo / ".git" / "BISECT_LOG").exists())
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("bisect", str(ctx.exception))

    def test_refuses_during_unresolved_merge(self):
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "README.md").write_text("feature version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feature readme")
        gitops.run_git(self.repo, "checkout", "main")
        (self.repo / "README.md").write_text("main version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main readme")
        gitops.run_git(self.repo, "merge", "feature", check=False)  # conflicts
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").exists())
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("merge", str(ctx.exception))
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").exists())  # untouched

    def test_refuses_during_unresolved_revert(self):
        (self.repo / "README.md").write_text("v1\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "v1")
        v1_sha = gitops.head_sha(self.repo)
        (self.repo / "README.md").write_text("v2\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "v2")
        gitops.run_git(self.repo, "revert", "--no-edit", v1_sha, check=False)  # conflicts
        self.assertTrue((self.repo / ".git" / "REVERT_HEAD").exists())
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("revert", str(ctx.exception))

    def test_refuses_during_unresolved_rebase_even_if_tree_looks_clean(self):
        """A conflict resolved via `checkout --ours` + `add` (a legitimate
        strategy) leaves the working tree looking clean to changed_files()
        while .git/rebase-merge is still present — must still refuse."""
        gitops.run_git(self.repo, "checkout", "-b", "feature")
        (self.repo / "README.md").write_text("feature version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "feature readme")
        gitops.run_git(self.repo, "checkout", "main")
        (self.repo / "README.md").write_text("main version\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "main readme")
        gitops.run_git(self.repo, "checkout", "feature")
        gitops.run_git(self.repo, "rebase", "main", check=False)  # conflicts
        gitops.run_git(self.repo, "checkout", "--ours", "README.md")
        gitops.run_git(self.repo, "add", "README.md")
        self.assertEqual(gitops.changed_files(self.repo), [])  # looks clean
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.repo)
        self.assertIn("rebase", str(ctx.exception))


class UpdateBase(GitopsHarness):
    """gitops.update_base — the owned fast-forward of the BASE branch.

    field (US-CHAT-01 lean run): staleness was measurable in two places and
    actionable in none — preflight's "let them decide to update the base
    first" named a remedy with no verb behind it, and raw `git pull`/`git
    merge` is guard-blocked. These assert the remedy terminates AND that the
    three refusals bounding it hold."""

    def _clone_with_upstream(self, name="clone"):
        remote = make_repo(self.workspace, f"origin-{name}")
        clone = self.workspace / name
        gitops.run_git(self.workspace, "clone", str(remote), name)
        gitops.run_git(clone, "config", "user.email", "t@t")
        gitops.run_git(clone, "config", "user.name", "t")
        return remote, clone

    def _advance_remote(self, remote: Path, n: int = 1) -> None:
        for i in range(n):
            (remote / f"up{i}.txt").write_text("x\n")
            gitops.run_git(remote, "add", "-A")
            gitops.run_git(remote, "commit", "-m", f"upstream {i}")

    def test_fast_forwards_a_behind_base_and_says_so(self):
        remote, clone = self._clone_with_upstream()
        before = gitops.head_sha(clone)
        self._advance_remote(remote, 4)          # the field case, exactly
        self.assertEqual(gitops.base_branch_behind(clone, "main"), 4)
        result = gitops.update_base(clone)
        self.assertTrue(result["advanced"])
        self.assertEqual(result["behind"], 4)
        self.assertEqual(result["ahead"], 0)
        self.assertEqual(result["before"], before)
        self.assertEqual(result["after"], gitops.head_sha(clone))
        self.assertNotEqual(result["after"], before)
        # the whole point: the gap is CLOSED, not merely reported
        self.assertEqual(gitops.base_branch_behind(clone, "main"), 0)
        self.assertTrue((clone / "up3.txt").exists())

    def test_fast_forward_only_no_merge_commit(self):
        """The base must end up byte-identical to what upstream published —
        a merge commit would make the local base a shape no reviewer of the
        upstream branch has ever seen."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 2)
        gitops.update_base(clone)
        self.assertEqual(gitops.head_sha(clone),
                         gitops.run_git(remote, "rev-parse", "HEAD"))
        # linear: HEAD's parent count is 1, never 2
        parents = gitops.run_git(clone, "rev-list", "--parents", "-n", "1", "HEAD")
        self.assertEqual(len(parents.split()), 2)

    def test_already_current_is_a_no_op_not_an_error(self):
        """Idempotent: the orchestrator re-runs this after a refusal, and
        'you are already up to date' is a success, not a failure."""
        _remote, clone = self._clone_with_upstream()
        before = gitops.head_sha(clone)
        result = gitops.update_base(clone)
        self.assertFalse(result["advanced"])
        self.assertEqual(result["behind"], 0)
        self.assertEqual(result["before"], before)
        self.assertEqual(result["after"], before)
        self.assertEqual(gitops.head_sha(clone), before)

    def test_refuses_a_diverged_base_without_touching_it(self):
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 2)
        (clone / "local.txt").write_text("local only\n")   # local-only commit
        gitops.run_git(clone, "add", "-A")
        gitops.run_git(clone, "commit", "-m", "local only")
        before = gitops.head_sha(clone)
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone)
        msg = str(ctx.exception)
        self.assertIn("diverged", msg)
        self.assertIn("1 local-only commit(s), 2 upstream", msg)
        self.assertIn("not a fast-forward", msg)
        # never rebased, never merged, never reset
        self.assertEqual(gitops.head_sha(clone), before)
        self.assertTrue((clone / "local.txt").exists())

    def test_ahead_only_is_reported_not_raised(self):
        """Local commits with nothing upstream is not a blocked
        fast-forward — there is nothing to fast-forward. Refusing "already
        current" would be noise; the count still rides out in the result."""
        _remote, clone = self._clone_with_upstream()
        (clone / "local.txt").write_text("local only\n")
        gitops.run_git(clone, "add", "-A")
        gitops.run_git(clone, "commit", "-m", "local only")
        result = gitops.update_base(clone)
        self.assertFalse(result["advanced"])
        self.assertEqual(result["behind"], 0)
        self.assertEqual(result["ahead"], 1)

    def test_refuses_when_the_remote_cannot_answer(self):
        """The measuring helpers fail OPEN (a blip must not brick a step);
        this one MOVES a ref, so it must fail CLOSED — a no-op reporting
        success is the exact defect sync_branch's docstring documents."""
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(self.repo)        # fixture has no remote
        self.assertIn("no usable remote", str(ctx.exception))

    def test_a_local_only_base_is_not_reported_as_a_connectivity_failure(self):
        """adversarial review: `fetch_base` returns False for "no such branch
        upstream" too, so a base that was simply never pushed sent the human
        off to fix a network that was fine — while `base_check` called the
        very same repo `behind: null`, "nothing to do". Two verbs, one repo,
        contradictory accounts."""
        _remote, clone = self._clone_with_upstream()
        gitops.run_git(clone, "branch", "integration")     # never pushed
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone, branch="integration")
        msg = str(ctx.exception)
        self.assertIn("has no branch 'integration'", msg)
        self.assertIn("Connectivity is fine", msg)

    def test_refuses_a_dirty_base_that_is_actually_checked_out(self):
        """Only when the base IS current: that is the one case where a
        fast-forward rewrites files under the human."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 1)
        (clone / "README.md").write_text("uncommitted work\n")
        before = gitops.head_sha(clone)
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone)
        self.assertIn("uncommitted", str(ctx.exception))
        self.assertEqual(gitops.head_sha(clone), before)
        self.assertEqual((clone / "README.md").read_text(), "uncommitted work\n")

    def test_a_dirty_tree_on_another_branch_does_not_block_the_base(self):
        """Both adversarial lenses, independently: `base_check` deliberately
        tolerates a dirty tree at plan time ("the human may well have work in
        progress"), so its own advertised remedy must be reachable in exactly
        that state. The first draft refused it — a remedy that cannot
        terminate, the bug class this change exists to close, one seam over."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 3)
        gitops.run_git(clone, "checkout", "-b", "feat/wip")
        (clone / "README.md").write_text("work in progress\n")
        (clone / "scratch.txt").write_text("untracked too\n")
        result = gitops.update_base(clone)
        self.assertTrue(result["advanced"])
        self.assertEqual(result["behind"], 3)
        self.assertFalse(result["checked_out"])
        # the base moved; the human's working tree did not
        self.assertEqual(gitops.base_branch_behind(clone, "main"), 0)
        self.assertEqual((clone / "README.md").read_text(), "work in progress\n")
        self.assertTrue((clone / "scratch.txt").exists())
        self.assertFalse((clone / "up0.txt").exists())   # not checked out

    def test_never_switches_the_checkout(self):
        """THE regression test (adversarial review, high, reproduced end to
        end). The first draft reused `ensure_default_branch` and inherited its
        branch SWITCH, so the documented flow — preflight reports behind,
        human runs update-base, re-run preflight — silently left the repo on
        `main`. Preflight's idempotent re-run returns the cached `branches`
        entry without switching back, so `merge-task` then squash-committed
        every task onto the BASE and `create-pr` opened a PR whose head branch
        had none of the work."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 2)
        gitops.run_git(clone, "checkout", "-b", "feat/X-1-thing")
        result = gitops.update_base(clone)
        self.assertTrue(result["advanced"])
        self.assertEqual(result["current_branch"], "feat/X-1-thing")
        self.assertEqual(
            gitops.run_git(clone, "rev-parse", "--abbrev-ref", "HEAD"),
            "feat/X-1-thing")

    def test_does_not_switch_on_a_refusal_either(self):
        """The refusal messages claim nothing was moved. That has to be true
        of the WORKING TREE, not just the ref — the first draft had already
        switched the checkout before it ever reached a refusal."""
        _remote, clone = self._clone_with_upstream()
        gitops.run_git(clone, "checkout", "-b", "feat/X-1-thing")
        gitops.run_git(clone, "remote", "remove", "origin")
        with self.assertRaises(gitops.GitError):
            gitops.update_base(clone)
        self.assertEqual(
            gitops.run_git(clone, "rev-parse", "--abbrev-ref", "HEAD"),
            "feat/X-1-thing")

    def test_fetches_the_branchs_configured_upstream_not_origin(self):
        """adversarial review, high, reproduced on the standard fork layout:
        `origin` = my fork, `upstream` = canonical, `main` tracking upstream.
        `_push_remote` prefers `origin` unconditionally, so every staleness
        question was answered against the FORK — "already current" while the
        base was genuinely behind canonical. Same laundering as the defect
        this verb was written to end, reached through remote resolution
        instead of connectivity."""
        canonical = make_repo(self.workspace, "canonical")
        fork = self.workspace / "fork"
        gitops.run_git(self.workspace, "clone", str(canonical), "fork")
        clone = self.workspace / "forkclone"
        gitops.run_git(self.workspace, "clone", str(fork), "forkclone")
        gitops.run_git(clone, "config", "user.email", "t@t")
        gitops.run_git(clone, "config", "user.name", "t")
        gitops.run_git(clone, "remote", "add", "upstream", str(canonical))
        gitops.run_git(clone, "config", "branch.main.remote", "upstream")
        (canonical / "canon.txt").write_text("canonical work\n")
        gitops.run_git(canonical, "add", "-A")
        gitops.run_git(canonical, "commit", "-m", "canonical work")
        # the fork (origin) is untouched and would answer "current"
        self.assertEqual(gitops.base_branch_behind(clone, "main"), 1)
        result = gitops.update_base(clone)
        self.assertEqual(result["remote"], "upstream")
        self.assertTrue(result["advanced"])
        self.assertTrue((clone / "canon.txt").exists())

    def test_refuses_mid_rebase(self):
        _remote, clone = self._clone_with_upstream()
        gitops.run_git(clone, "checkout", "-b", "feature")
        (clone / "README.md").write_text("feature version\n")
        gitops.run_git(clone, "add", "-A")
        gitops.run_git(clone, "commit", "-m", "feature readme")
        gitops.run_git(clone, "checkout", "main")
        (clone / "README.md").write_text("main version\n")
        gitops.run_git(clone, "add", "-A")
        gitops.run_git(clone, "commit", "-m", "main readme")
        gitops.run_git(clone, "checkout", "feature")
        gitops.run_git(clone, "rebase", "main", check=False)   # conflicts
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone)
        self.assertIn("rebase", str(ctx.exception))

    def test_explicit_branch_override_updates_that_base_by_ref(self):
        """A `release/2026`-shaped base, updated while `main` stays checked
        out: the REF moves, the working tree does not."""
        remote, clone = self._clone_with_upstream()
        gitops.run_git(remote, "checkout", "-b", "release/2026")
        (remote / "rel.txt").write_text("release work\n")
        gitops.run_git(remote, "add", "-A")
        gitops.run_git(remote, "commit", "-m", "release commit")
        gitops.run_git(clone, "fetch", "origin", "release/2026")
        gitops.run_git(clone, "checkout", "-b", "release/2026", "FETCH_HEAD~1")
        gitops.run_git(clone, "checkout", "main")
        result = gitops.update_base(clone, branch="release/2026")
        self.assertEqual(result["branch"], "release/2026")
        self.assertTrue(result["advanced"])
        self.assertFalse(result["checked_out"])
        self.assertEqual(
            gitops.run_git(clone, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertFalse((clone / "rel.txt").exists())      # tree untouched
        # …but the branch itself now carries the commit
        self.assertIn("rel.txt", gitops.run_git(
            clone, "ls-tree", "--name-only", "release/2026"))

    def test_refuses_when_the_named_base_does_not_exist_locally(self):
        _remote, clone = self._clone_with_upstream()
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone, branch="does-not-exist")
        self.assertIn("does not exist locally", str(ctx.exception))

    def test_refuses_a_base_checked_out_in_another_worktree(self):
        """Re-verification finding, reproduced: `update-ref` — unlike `branch
        -f` — will move a branch another worktree has checked out, and
        `rev-parse --abbrev-ref HEAD` cannot see past the current one. The
        other checkout was left holding a STAGED REVERSAL of the upstream
        commits, which its next `harness commit` would have committed. Linked
        worktrees are the normal runtime shape here — every task runs in one."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 2)
        wt = self.workspace / "linked-wt"
        gitops.run_git(clone, "worktree", "add", "-b", "task/T1", str(wt))
        before = gitops.run_git(clone, "rev-parse", "main")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(wt, branch="main")     # main is held by `clone`
        msg = str(ctx.exception)
        self.assertIn("checked out in another worktree", msg)
        self.assertIn("staged reversal", msg)
        self.assertEqual(gitops.run_git(clone, "rev-parse", "main"), before)
        self.assertEqual(gitops.changed_files(clone), [])   # no staged revert

    def test_a_stale_upstream_config_is_not_reported_as_connectivity(self):
        """Re-verification finding: making `branch.<n>.remote` load-bearing
        (so a fork layout measures against canonical) is exactly what put a
        STALE value in reach — after a `git remote rename`, the fetch fails
        and `remote_branch_exists` answers None, so it fell through to
        "offline, auth, or timeout". The fix that widened the blind spot
        closes it."""
        _remote, clone = self._clone_with_upstream()
        gitops.run_git(clone, "config", "branch.main.remote", "ghost")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(clone)
        msg = str(ctx.exception)
        self.assertIn("names 'ghost', which is not a configured remote", msg)
        self.assertIn("Connectivity is fine", msg)

    def test_the_fast_forward_is_a_compare_and_swap(self):
        """The off-checkout path writes with the value it measured against as
        the expected old, so a ref that moved in between fails the swap
        instead of being clobbered."""
        remote, clone = self._clone_with_upstream()
        self._advance_remote(remote, 1)
        gitops.run_git(clone, "checkout", "-b", "feat/x")
        real_rev_count = gitops._rev_count

        def moving_rev_count(repo, spec):
            out = real_rev_count(repo, spec)
            if spec.startswith("FETCH_HEAD.."):      # after both counts read
                (clone / "sneak.txt").write_text("concurrent\n")
                gitops.run_git(clone, "add", "-A")
                gitops.run_git(clone, "commit", "-m", "concurrent writer")
                gitops.run_git(clone, "branch", "-f", "main", "HEAD")
            return out

        with mock.patch.object(gitops, "_rev_count", moving_rev_count):
            with self.assertRaises(gitops.GitError) as ctx:
                gitops.update_base(clone)
        self.assertIn("update-ref", str(ctx.exception))


class CliEndToEnd(GitopsHarness):
    ROOT = Path(__file__).resolve().parent.parent

    def _cli(self, *args) -> tuple[int, dict]:
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "harness", "--workspace", str(self.workspace),
             "--run", str(self.run), *args],
            cwd=self.ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc.returncode, payload

    def _walk_to_develop(self):
        from harness import gates as gates_mod
        st = state_mod.load(self.run, self.workspace)
        for _ in range(10):
            current = st["cursor"]["current_step"]
            if current == "develop":
                break
            step_def = self.manifest["steps"][current]
            if step_def.get("gate"):
                gates_mod.present(st, current, "2026-01-01T00:00:00+00:00")
                st["gates"][current]["decision"] = "approved"
            if step_def.get("verdict_bound"):
                support.seed_review_verdict(
                    self.run, mode=step_def["verdict_bound"]["mode"])
            nxt = next(iter(transitions.cursor_candidates(
                st, self.manifest, self.config, run=self.run)))
            transitions.advance_cursor(st, self.manifest, self.config, nxt,
                                       "2026-01-01T00:00:00+00:00",
                                       run=self.run)
        state_mod.save(self.run, self.workspace, st)

    def test_full_mode_tdd_flow_through_the_cli(self):
        self._walk_to_develop()
        code, _ = self._cli("task", "--id", "T1", "--to", "in-progress")
        self.assertEqual(code, 0)

        # completing without any red-proof: refused (exit 1, fail closed)
        code, out = self._cli("task", "--id", "T1", "--to", "in-review",
                              "--repo", str(self.repo), "--test-cmd", TEST_CMD)
        self.assertEqual(code, 1)
        self.assertIn("no red-proof", out["error"])

        self._set_declared_test_intents(["test_val", "test_ghost"])
        self._write_test()
        code, out = self._cli("verify-red", "--repo", str(self.repo),
                              "--task", "T1", "--test-cmd", TEST_CMD)
        self.assertEqual(code, 0, out)
        self.assertIn("tests/test_x.py", out["tests"])
        self.assertEqual(out["declared_intents"], ["test_ghost", "test_val"])
        self.assertEqual(out["missing_intents"], ["test_ghost"])

        code, out = self._cli("show-redproof", "--task", "T1")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["missing_intents"], ["test_ghost"])

        # still red -> completion refused by the green run
        code, out = self._cli("task", "--id", "T1", "--to", "in-review",
                              "--repo", str(self.repo), "--test-cmd", TEST_CMD)
        self.assertEqual(code, 1)
        self.assertIn("still failing", out["error"])

        self._write_impl()
        code, out = self._cli("task", "--id", "T1", "--to", "in-review",
                              "--repo", str(self.repo), "--test-cmd", TEST_CMD)
        self.assertEqual(code, 0, out)


def make_monorepo(base: Path, name: str = "mono") -> Path:
    """ONE physical checkout holding TWO logical repos: `frontend/` and
    `backend/`, `.git` at the root. The shape `discover()`'s
    `monorepo_split` proposes, and the shape the .NET case forces (a `.sln`
    at the physical root registered as one logical repo, `frontend/` as
    another) — so `repos.yaml` maps two names into one checkout and every
    path-producing git call has to know which half it is answering for."""
    repo = base / name
    for area in ("frontend", "backend"):
        (repo / area / "tests").mkdir(parents=True)
    gitops.run_git(base, "init", "-b", "main", name)
    gitops.run_git(repo, "config", "user.email", "t@t")
    gitops.run_git(repo, "config", "user.name", "t")
    for area in ("frontend", "backend"):
        (repo / area / "tests" / "__init__.py").write_text("")
        (repo / area / "app.py").write_text("def val():\n    return 1\n")
    (repo / "README.md").write_text("mono\n")
    gitops.run_git(repo, "add", "-A")
    gitops.run_git(repo, "commit", "-m", "init")
    return repo


class SubtreeLogicalRepos(unittest.TestCase):
    """A logical repo registered by a SUBTREE of a physical checkout.

    `repos.yaml` still maps name -> path; the subtree path IS the registered
    path, so the exact-path resolvers are untouched. What changes is that
    `repo` and `git rev-parse --show-toplevel` are no longer the same
    directory — and git's own answers are not uniformly relative to either
    one. Every test here pins one half of that: the prefix is stripped, and
    the sibling logical repo's paths are filtered out."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.mono = make_monorepo(self.workspace)
        self.frontend = self.mono / "frontend"
        self.backend = self.mono / "backend"
        _, _, self.config = load_declared(self.workspace)

    def tearDown(self):
        support.rmtree(self.workspace)

    def _dirty_both(self):
        (self.frontend / "app.py").write_text("def val():\n    return 2\n")
        (self.backend / "app.py").write_text("def val():\n    return 3\n")
        (self.frontend / "new.py").write_text("f\n")
        (self.backend / "new.py").write_text("b\n")

    # ------------------------------------------------- the canonical pair

    def test_toplevel_and_prefix_locate_the_registration(self):
        self.assertEqual(gitops.toplevel(self.frontend).resolve(),
                         self.mono.resolve())
        self.assertEqual(gitops.subtree_prefix(self.frontend), "frontend")
        # a root registration is its own toplevel with an EMPTY prefix —
        # the property every "byte-identical for root repos" claim rests on
        self.assertEqual(gitops.toplevel(self.mono).resolve(),
                         self.mono.resolve())
        self.assertEqual(gitops.subtree_prefix(self.mono), "")

    def test_work_tree_root_accepts_a_subtree_and_answers_none_off_tree(self):
        self.assertEqual(gitops.work_tree_root(self.frontend).resolve(),
                         self.mono.resolve())
        outside = self.workspace / "plain"
        outside.mkdir()
        self.assertIsNone(gitops.work_tree_root(outside))
        self.assertIsNone(gitops.work_tree_root(self.workspace / "missing"))

    def test_has_tracked_files_separates_a_real_subtree_from_an_ignored_one(self):
        """`work_tree_root` says "inside a work tree", which an IGNORED
        directory satisfies just as well — and `git worktree add` will not
        materialize one, so a registration there is un-runnable. This is the
        second half of the question the repo gate has to ask."""
        (self.mono / ".gitignore").write_text("generated/\n")
        (self.mono / "generated").mkdir()
        (self.mono / "generated" / "app.py").write_text("g\n")
        gitops.run_git(self.mono, "add", "-A")
        gitops.run_git(self.mono, "commit", "-m", "ignore generated")
        # both are inside the same work tree...
        self.assertEqual(gitops.work_tree_root(self.mono / "generated").resolve(),
                         self.mono.resolve())
        # ...only one of them is in the index
        self.assertTrue(gitops.has_tracked_files(self.frontend))
        self.assertFalse(gitops.has_tracked_files(self.mono / "generated"))
        self.assertFalse(gitops.has_tracked_files(self.workspace / "missing"))

    # ------------------------------------------------------ path relativity

    def test_changed_files_strips_the_prefix_and_drops_the_sibling(self):
        self._dirty_both()
        self.assertEqual(sorted(gitops.changed_files(self.frontend)),
                         ["app.py", "new.py"])
        # the same physical dirt, asked at the root: unfiltered, unstripped
        self.assertEqual(sorted(gitops.changed_files(self.mono)),
                         ["backend/app.py", "backend/new.py",
                          "frontend/app.py", "frontend/new.py"])

    def test_diff_paths_strips_the_prefix_and_drops_the_sibling(self):
        gitops.run_git(self.mono, "checkout", "-b", "task/T1")
        (self.frontend / "app.py").write_text("def val():\n    return 2\n")
        (self.backend / "app.py").write_text("def val():\n    return 3\n")
        gitops.run_git(self.mono, "add", "-A")
        gitops.run_git(self.mono, "commit", "-m", "both")
        (self.frontend / "later.py").write_text("l\n")   # working-tree half
        (self.backend / "later.py").write_text("l\n")
        self.assertEqual(gitops.diff_paths(self.frontend, "main"),
                         ["app.py", "later.py"])
        self.assertEqual(gitops.diff_paths(self.backend, "main"),
                         ["app.py", "later.py"])

    def test_diff_line_count_counts_only_the_registered_subtree(self):
        gitops.run_git(self.mono, "checkout", "-b", "task/T1")
        (self.backend / "app.py").write_text("x\n" * 40)
        gitops.run_git(self.mono, "add", "-A")
        gitops.run_git(self.mono, "commit", "-m", "backend churn")
        (self.frontend / "app.py").write_text("def val():\n    return 2\n")
        # frontend touched 1 line + 1 line; backend's 40+ must not inflate
        # the quick-mode size gate for a repo that does not own them
        self.assertEqual(gitops.diff_line_count(self.frontend, "main"), 2)
        self.assertGreater(gitops.diff_line_count(self.backend, "main"), 40)

    def test_blob_sha_and_test_set_lock_the_subtree_file(self):
        (self.frontend / "tests" / "test_app.py").write_text("def test_val(): pass\n")
        (self.backend / "tests" / "test_app.py").write_text("def test_val(): pass\n")
        tests, closure = gitops._test_set(self.frontend, self.config, None)
        # subtree-relative keys, and the sibling's identically-named test is
        # not in the locked set at all
        self.assertEqual(sorted(tests), ["tests/test_app.py"])
        self.assertEqual(
            tests["tests/test_app.py"],
            gitops.run_git(self.mono, "hash-object", "--",
                           "frontend/tests/test_app.py"))
        self.assertTrue(all(not c.startswith("backend") for c in closure), closure)
        self.assertIn("tests/__init__.py", closure)

    # ---------------------------------------------------------- staging scope

    def test_commit_cannot_stage_or_commit_a_sibling_subtree(self):
        """The mutation test for `add -A -- .`: one `.git`, two logical
        repos, so an unrelated edit left in `backend/` used to be swept into
        a `frontend` task's commit under that task's message."""
        self._dirty_both()
        gitops.commit_class(self.frontend, self.config, "working",
                            task="T1", summary="frontend only")
        committed = gitops.run_git(self.mono, "diff-tree", "--no-commit-id",
                                   "--name-only", "-r", "HEAD").splitlines()
        self.assertEqual(sorted(committed),
                         ["frontend/app.py", "frontend/new.py"])
        # and the backend edits are still sitting in the working tree,
        # unstaged — surfaced to their own repo, not silently absorbed
        self.assertEqual(sorted(gitops.changed_files(self.backend)),
                         ["app.py", "new.py"])

    def test_commit_fixup_is_subtree_scoped_too(self):
        (self.frontend / "app.py").write_text("def val():\n    return 2\n")
        target = gitops.commit_class(self.frontend, self.config, "working",
                                     task="T1", summary="first")
        (self.frontend / "app.py").write_text("def val():\n    return 4\n")
        (self.backend / "app.py").write_text("def val():\n    return 5\n")
        gitops.commit_fixup(self.frontend, target)
        committed = gitops.run_git(self.mono, "diff-tree", "--no-commit-id",
                                   "--name-only", "-r", "HEAD").splitlines()
        self.assertEqual(committed, ["frontend/app.py"])

    def test_mirror_stays_path_exclusive_from_a_subtree_repo(self):
        run = self.workspace / "ai" / "2026-01-01-SUB-1"
        (run / "reports").mkdir(parents=True)
        (run / "reports" / "r.md").write_text("report\n")
        gitops.publish_mirror(self.frontend, run, self.config, run.name)
        committed = gitops.run_git(self.mono, "diff-tree", "--no-commit-id",
                                   "--name-only", "-r", "HEAD").splitlines()
        # exclusivity is judged against `<prefix>/ai/<run>`: the mirror lands
        # inside the LOGICAL repo, so the staged paths carry the prefix and a
        # bare `ai/` expectation would have called every one of them an
        # offender
        self.assertTrue(committed)
        self.assertTrue(all(p.startswith(f"frontend/ai/{run.name}/")
                            for p in committed), committed)

    # -------------------------------------------------------------- worktrees

    def test_worktree_lands_beside_the_toplevel_and_returns_the_subtree(self):
        wt = gitops.worktree_add(self.frontend, "T1", "main")
        root, path = Path(wt["root"]), Path(wt["path"])
        self.addCleanup(gitops.worktree_remove, self.frontend, wt)
        # beside the PHYSICAL checkout, named after it — not beside
        # `<checkout>/frontend`, which would nest a whole second checkout
        # inside the tree it was cut from
        self.assertEqual(root.parent.resolve(), self.mono.parent.resolve())
        self.assertTrue(root.name.startswith(f"{self.mono.name}-wt-T1-"))
        self.assertNotIn(self.mono.resolve(), root.resolve().parents)
        # the task works in the LOGICAL repo inside that worktree
        self.assertEqual(path, root / "frontend")
        self.assertTrue((path / "app.py").is_file())
        self.assertEqual(gitops.subtree_prefix(path), "frontend")

    def test_worktree_remove_cleans_up_a_subtree_worktree(self):
        wt = gitops.worktree_add(self.frontend, "T1", "main")
        root = Path(wt["root"])
        self.assertTrue(root.is_dir())
        gitops.worktree_remove(self.frontend, wt)
        # `git worktree remove` takes the ROOT — handed `path` (the subtree)
        # it refuses with "is not a working tree" and the tree leaks
        self.assertFalse(root.exists())
        self.assertNotIn("task/", gitops.run_git(self.mono, "branch", "--list"))

    def test_root_registration_worktree_layout_is_unchanged(self):
        wt = gitops.worktree_add(self.mono, "T2", "main")
        self.addCleanup(gitops.worktree_remove, self.mono, wt)
        self.assertEqual(wt["path"], wt["root"])      # empty prefix
        self.assertEqual(Path(wt["path"]).parent.resolve(),
                         self.workspace.resolve())
        self.assertTrue(Path(wt["path"]).name.startswith("mono-wt-T2-"))

    def test_worktree_remove_tolerates_the_pre_subtree_dict_shape(self):
        """Run state written before this change records only
        `{path, branch}`; a resumed/reconciled run must still sweep."""
        wt = gitops.worktree_add(self.mono, "T3", "main")
        legacy = {"path": wt["path"], "branch": wt["branch"]}
        gitops.worktree_remove(self.mono, legacy)
        self.assertFalse(Path(wt["path"]).exists())

    # ------------------------------- the returned path has to actually exist

    def _base_without_frontend(self) -> str:
        """A base branch that predates the subtree — the ordinary case for a
        long-lived `main` and a newly split-out logical repo, and equally
        what an ignored/untracked subtree looks like to `worktree add`."""
        gitops.run_git(self.mono, "checkout", "-q", "-b", "old", "main")
        gitops.run_git(self.mono, "rm", "-r", "-q", "frontend")
        gitops.run_git(self.mono, "commit", "-m", "no frontend on this base")
        gitops.run_git(self.mono, "checkout", "-q", "main")
        return "old"

    def test_worktree_add_refuses_a_base_that_lacks_the_subtree(self):
        """`git worktree add` SUCCEEDS on such a base and simply does not
        materialize the subtree, so the pre-fix verb returned ok with a
        `path` that was never on disk: the developer's `harness-repo` header
        pointed at nothing and `_run_tests(cwd=...)` raised."""
        base = self._base_without_frontend()
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.worktree_add(self.frontend, "T1", base)
        msg = str(ctx.exception)
        self.assertIn("does not exist on that branch", msg)
        self.assertIn(base, msg)

    def test_the_refused_worktree_is_not_left_behind(self):
        """cli.py's resume gate is `Path(recorded["path"]).is_dir()` — false
        forever for a subtree that never materialized — so a returned-but-
        missing path made every retry cut ANOTHER worktree and ANOTHER
        `task/<id>-<uid>` branch, with `worktree_remove` only ever seeing the
        newest record. The refusal takes its own tree with it."""
        base = self._base_without_frontend()
        with self.assertRaises(gitops.GitError):
            gitops.worktree_add(self.frontend, "T1", base)
        self.assertEqual(
            gitops.run_git(self.mono, "worktree", "list").count("\n"), 0)
        self.assertNotIn("task/", gitops.run_git(self.mono, "branch", "--list"))
        self.assertEqual(
            [p.name for p in self.workspace.iterdir()
             if p.name.startswith("mono-wt-")], [])

    def test_a_materialized_subtree_worktree_still_returns_its_path(self):
        """The no-regression half: the check only refuses the missing case."""
        self._base_without_frontend()                 # the branch merely exists
        wt = gitops.worktree_add(self.frontend, "T1", "main")
        self.addCleanup(gitops.worktree_remove, self.frontend, wt)
        self.assertTrue(Path(wt["path"]).is_dir())
        self.assertTrue((Path(wt["path"]) / "app.py").is_file())

    def test_root_registration_path_check_is_a_no_op(self):
        """A root registration's `path` IS the worktree root, which `git
        worktree add` always creates — the verification can never fire, and
        the verb's result shape is unchanged."""
        wt = gitops.worktree_add(self.mono, "T4", "main")
        self.addCleanup(gitops.worktree_remove, self.mono, wt)
        self.assertEqual(sorted(wt), ["branch", "path", "root"])
        self.assertTrue(Path(wt["path"]).is_dir())

    # ------------------------------------------ shared-checkout collisions

    def test_shares_toplevel_names_only_the_sibling_logical_repos(self):
        other = make_repo(self.workspace, "solo")
        repos = {"frontend": str(self.frontend), "backend": str(self.backend),
                 "mono": str(self.mono), "solo": str(other)}
        self.assertEqual(gitops.shares_toplevel(repos, self.frontend),
                         ["backend", "mono"])
        self.assertEqual(gitops.shares_toplevel(repos, other), [])
        # a dangling registration is not a collision, and must not raise
        self.assertEqual(
            gitops.shares_toplevel({"ghost": str(self.workspace / "gone")},
                                   other), [])

    def test_worktree_failure_refuses_the_direct_branch_fallback_on_a_share(self):
        repos = {"frontend": str(self.frontend), "mono": str(self.mono)}
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.worktree_add(self.frontend, "T1", "no-such-base", repos)
        msg = str(ctx.exception)
        self.assertIn("direct-branch fallback is NOT available", msg)
        self.assertIn("mono", msg)

    def test_worktree_failure_refuses_the_fallback_for_an_UNREGISTERED_share(self):
        """The hazard is the physical CHECKOUT, not the registry. With only
        `frontend` registered, `shares_toplevel` is empty — and the pre-fix
        message therefore OFFERED the direct-branch fallback, which cuts the
        task branch in the checkout that also holds `backend/` and whatever
        uncommitted human work sits beside it."""
        self.assertEqual(
            gitops.shares_toplevel({"frontend": str(self.frontend)},
                                   self.frontend), [])
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.worktree_add(self.frontend, "T1", "no-such-base",
                                {"frontend": str(self.frontend)})
        msg = str(ctx.exception)
        self.assertIn("direct-branch fallback is NOT available", msg)
        self.assertNotIn("offer the direct-branch fallback", msg)
        self.assertIn(self.mono.name, msg)          # names the shared checkout
        self.assertNotIn("also registered into it", msg)   # nothing to name

    def test_worktree_failure_still_offers_the_fallback_without_a_share(self):
        solo = make_repo(self.workspace, "solo")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.worktree_add(solo, "T1", "no-such-base",
                                {"solo": str(solo)})
        self.assertIn("offer the direct-branch fallback", str(ctx.exception))

    def test_a_root_registration_that_shares_a_checkout_still_refuses(self):
        """The parent half of a split (`mono` registered at the checkout
        root, `frontend` under it) has an EMPTY prefix but the same hazard:
        a task branch cut there switches `frontend/` too."""
        repos = {"frontend": str(self.frontend), "mono": str(self.mono)}
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.worktree_add(self.mono, "T1", "no-such-base", repos)
        msg = str(ctx.exception)
        self.assertIn("direct-branch fallback is NOT available", msg)
        self.assertIn("also registered into it: frontend", msg)

    # --------------------------------------------------- shared-checkout dirt

    def test_ensure_default_branch_refuses_on_out_of_subtree_dirt(self):
        """`changed_files` is subtree-scoped now, but `git checkout` still
        flips the WHOLE physical tree — so the safety question is asked at
        the toplevel, and the refusal names where the dirt actually is."""
        (self.backend / "app.py").write_text("def val():\n    return 9\n")
        self.assertEqual(gitops.changed_files(self.frontend), [])   # not ITS dirt
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.frontend, "main")
        msg = str(ctx.exception)
        self.assertIn("uncommitted", msg)
        self.assertIn("backend/app.py", msg)

    def test_the_dirt_refusal_names_the_tree_the_dirt_is_actually_in(self):
        """Subject and paths have to name the SAME directory. They didn't:
        the paths are toplevel-relative (deliberately — see above) while the
        subject stayed the registered path, so the refusal read
        `...\\mono\\frontend has 1 uncommitted change(s) (.gitignore)` for a
        `.gitignore` that lives at `...\\mono` and cannot be found, let
        alone cleaned, where the message points."""
        (self.mono / ".gitignore").write_text("*.tmp\n")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.frontend, "main")
        msg = str(ctx.exception)
        self.assertIn(".gitignore", msg)
        subject = Path(msg.split(" has ")[0].split(" (")[0])
        self.assertEqual(subject.resolve(), self.mono.resolve())
        # and the relationship is stated, so "why is it talking about a
        # directory I didn't register" is answered in the refusal itself
        self.assertIn(
            f"the physical checkout holding the registered repo {self.frontend}",
            msg)

    def test_update_base_dirt_refusal_names_the_shared_checkout_too(self):
        """The twin refusal, same widening, same fix — it refuses before any
        remote is contacted, so no upstream fixture is needed."""
        (self.mono / ".gitignore").write_text("*.tmp\n")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(self.frontend, "main")
        msg = str(ctx.exception)
        self.assertIn(".gitignore", msg)
        self.assertIn("on 'main' itself", msg)
        subject = Path(msg.split(" has ")[0].split(" (")[0])
        self.assertEqual(subject.resolve(), self.mono.resolve())

    def test_root_registration_dirt_messages_are_byte_identical(self):
        """No-regression, stated as the literal strings: a single-repo
        registration is its own toplevel, so neither refusal may gain a
        single character."""
        (self.mono / "README.md").write_text("dirty\n")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.ensure_default_branch(self.mono, "main")
        self.assertEqual(
            str(ctx.exception),
            f"{self.mono} has 1 uncommitted change(s) (README.md) — resolve, "
            "commit, or stash them yourself before continuing; never "
            "auto-discarded")
        with self.assertRaises(gitops.GitError) as ctx:
            gitops.update_base(self.mono, "main")
        self.assertEqual(
            str(ctx.exception),
            f"{self.mono} has 1 uncommitted change(s) (README.md) on 'main' "
            "itself — a fast-forward would rewrite them; commit or stash them "
            "yourself first, never auto-discarded. (Uncommitted work on ANY "
            "OTHER branch is fine: this verb moves the base ref without "
            "touching your working tree.)")

    def test_ensure_default_branch_still_passes_on_a_clean_shared_checkout(self):
        gitops.run_git(self.mono, "checkout", "-b", "side")
        out = gitops.ensure_default_branch(self.frontend, "main")
        self.assertTrue(out["switched"])
        self.assertEqual(out["branch"], "main")

    def test_cli_worktree_lane_round_trips_the_root_through_state(self):
        """`root` has to survive state.yaml, or the sweep at task-done hands
        `git worktree remove` the subtree path and leaks the whole tree."""
        chain.load_or_create_key(self.workspace)
        run = self.workspace / "ai" / "2026-01-01-SUB-1"
        state_mod.bootstrap(
            run, self.workspace,
            work_item={"id": "SUB-1", "title": "t", "provider_ref": ""},
            mode="full", change_type="fix",
            tasks=[{"id": "T1", "repo": str(self.frontend)}], entry_step="fetch")

        def cli(*args):
            proc = subprocess.run(
                [sys.executable, "-m", "harness", "--workspace",
                 str(self.workspace), "--run", str(run), *args],
                cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                text=True, encoding="utf-8", timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        wt = cli("worktree-add", "--repo", str(self.frontend),
                 "--task-id", "T1", "--base", "main")
        root, path = Path(wt["root"]), Path(wt["path"])
        self.assertEqual(path, root / "frontend")
        self.assertTrue((path / "app.py").is_file())
        resumed = cli("worktree-add", "--repo", str(self.frontend),
                      "--task-id", "T1", "--base", "main")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["root"], wt["root"])
        cli("worktree-remove", "--repo", str(self.frontend), "--task-id", "T1")
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
