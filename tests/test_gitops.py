"""M2 done-criteria against real fixture repos: red->green happy path,
skipped-red refusal, SHA-mismatch detection (edit / git-checkout / fixture),
the flagged revision path, squash + autosquash correctness, mirror
path-exclusivity, sync-branch, commit classes."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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
        sha = gitops.squash_merge(self.repo, "task/T1", "fix: #GIT-1 do the thing")
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
        gitops.autosquash(self.repo, base)
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
            gitops.squash_merge(self.repo, "task/T1", "fix: #X collide")
        self.assertIn("conflicted", str(ctx.exception))
        # tree restored: no markers, no unmerged index, nothing staged
        self.assertEqual((self.repo / "c.txt").read_text(encoding="utf-8"), "main version\n")
        self.assertFalse(gitops.run_git(self.repo, "ls-files", "-u"))
        with self.assertRaises(gitops.GitError):   # nothing to commit
            gitops.commit_class(self.repo, self.config, "working",
                                task="T1", summary="post-conflict")

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
        entry = {"test_cmd": support.NOP_CMD}
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
            f"{support.NOP_CMD} --exclude tests/harden-fe010.spec.ts")

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
        self.assertEqual(result, {"switched": True, "branch": "main",
                                  "from_branch": "feature"})
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_noop_when_already_on_default(self):
        result = gitops.ensure_default_branch(self.repo)
        self.assertEqual(result, {"switched": False, "branch": "main"})

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
                                  "from_branch": "other"})

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


if __name__ == "__main__":
    unittest.main()
