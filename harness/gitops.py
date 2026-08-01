"""Owned git entry points (RC1) + the TDD proof pair (design.md piece 5A).

Performer and verifier are the same code: commits are constructed here with
the declared commit-class templates (never validated after the fact by parsing
raw git), squash/autosquash re-derive the SHAs they create, the mirror commit
is path-exclusive by construction, and verify-red/green anchor the TDD
guarantee to a chained red-proof sidecar + blob-SHA comparison.
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

from . import chain
from . import state as state_mod
from .ndjson import append_record, now_iso

MIRROR_EXCLUDE = ("human-input.ndjson", ".redproof", ".state.lock")


class GitError(Exception):
    pass


class SecretSweepError(GitError):
    """A commit verb caught a harness integrity key mid-sweep — see
    _refuse_swept_secrets. Distinct type so the CLI can log a flagged
    event: a stray key inside a repo means a wrong---workspace invocation
    happened somewhere, which is worth surfacing beyond one refusal."""


class RedProofError(Exception):
    pass


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    # Explicit UTF-8, never the locale codec: git emits UTF-8 (subjects,
    # paths), and on Windows the locale default is cp1252 — which silently
    # mojibakes every non-ASCII commit subject, breaking the byte-exact
    # subject round-trip find_commit_by_subject depends on after a rewrite.
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        # stderr first, but fall back to stdout — several git failures
        # (merge --squash conflicts, notably) report on stdout only, and
        # the old stderr-only message was literally empty for them
        # (adversarial-review finding: a daily-use debugging tax).
        detail = "\n".join(s for s in (proc.stderr.strip(), proc.stdout.strip())
                           if s)
        raise GitError(f"git {' '.join(args)}: {detail[:600]}")
    return proc.stdout.strip()


def head_sha(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD")


def blob_sha(repo: Path, path: str) -> str:
    return run_git(repo, "hash-object", "--", path)


def changed_files(repo: Path) -> list[str]:
    # `diff --name-only` + `ls-files --others`: clean one-path-per-line output,
    # no status columns to mis-parse (porcelain parsing is exactly the kind of
    # fragile reverse-engineering this module exists to avoid).
    tracked = run_git(repo, "diff", "--name-only", "HEAD")
    untracked = run_git(repo, "ls-files", "--others", "--exclude-standard")
    return [p for p in (tracked + "\n" + untracked).splitlines() if p.strip()]


def _match(path: str, pattern: str) -> bool:
    # fnmatch's `*` crosses `/`, so `a/**` already behaves as a recursive
    # prefix; a leading `**/` additionally needs the anchored-at-root variant.
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(_match(path, p) for p in patterns)


# ------------------------------------------------------------- commit verbs

# Keep the integrity key out of git history. A stray
# `.claude/context/.harness-key` minted inside a repo checkout by a
# pre-0.16.11 wrong---workspace invocation could be swept into a task
# commit by `commit_class`'s own `git add -A`, surfacing only review
# rounds later as a dangling secret-bearing commit needing an
# object-level scrub (reflog expire + gc --prune=now). 0.16.11 killed the
# MINTING (bootstrap is the one creation moment); this pair kills the SWEEP
# for any key that still lands in a checkout by other means (a copied
# workspace, an older harness, a user mistake):
#   - ensure_repo_excludes (preflight): pins the basename into the repo's
#     local info/exclude — shared with every task worktree via the common
#     git dir — so `git add -A` stops seeing an UNTRACKED key at all.
#     info/exclude, not .gitignore: repo-local, never edits the user's
#     tracked files or their own ignore policy.
#   - _refuse_swept_secrets (both commit verbs): backstop for repos
#     preflighted before this existed. STAGED-only by design: a key already
#     tracked in history is the scrub's domain — a refusal there would
#     brick every later commit for a pre-existing condition.
_SECRET_BASENAMES = frozenset({".harness-key"})
_LOCAL_EXCLUDES = (".harness-key",)


def ensure_repo_excludes(repo: Path) -> None:
    out = run_git(repo, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(out) if Path(out).is_absolute() else repo / out
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    missing = [p for p in _LOCAL_EXCLUDES if p not in text.splitlines()]
    if not missing:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    glue = "" if (not text or text.endswith("\n")) else "\n"
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(glue + "\n".join(missing) + "\n")


def _refuse_swept_secrets(repo: Path) -> None:
    staged = run_git(repo, "diff", "--cached", "--name-only")
    hits = [p for p in staged.splitlines()
            if p.rsplit("/", 1)[-1] in _SECRET_BASENAMES]
    if not hits:
        return
    run_git(repo, "reset", "--", *hits, check=False)
    raise SecretSweepError(
        f"refusing to commit a harness integrity key: {', '.join(hits)} was "
        "about to enter git history (now unstaged). The live key lives at "
        "<workspace>/.claude/context/.harness-key, never inside a repo — one "
        "found here is stray litter from a wrong---workspace invocation: "
        "delete the file (`rm`) and retry. Preflight pins `.harness-key` "
        "into .git/info/exclude so `git add -A` skips an untracked one "
        "entirely.")


def render(template: str, **params) -> str:
    try:
        return template.format(**params)
    except KeyError as exc:
        raise GitError(f"commit template needs param {exc}") from exc


def commit_class(repo: Path, config: dict, cls: str, **params) -> str:
    """`harness commit` — stage all worktree changes, commit with the declared
    class template. The naming hook problem disappears: the template is
    *applied* here, not policed after the fact."""
    template = (config["naming"]["commit"] or {}).get(cls)
    if not template:
        raise GitError(f"no declared commit class '{cls}'")
    run_git(repo, "add", "-A")
    _refuse_swept_secrets(repo)
    if not run_git(repo, "diff", "--cached", "--name-only"):
        raise GitError("nothing to commit")
    run_git(repo, "commit", "-m", render(template, **params))
    return head_sha(repo)


def commit_fixup(repo: Path, target_sha: str) -> str:
    """`harness commit --fixup-of` — post-squash fix commits (coverage B10)."""
    run_git(repo, "add", "-A")
    _refuse_swept_secrets(repo)
    if not run_git(repo, "diff", "--cached", "--name-only"):
        raise GitError("nothing to commit")
    run_git(repo, "commit", "--fixup", target_sha)
    return head_sha(repo)


def squash_merge(repo: Path, task_branch: str, message: str) -> str:
    """`harness merge-task` — one integration commit per task.

    On conflict the working tree is RESTORED before raising (adversarial-
    review finding, verified by execution: `merge --squash` creates no
    MERGE_HEAD, so a conflicted one left `<<<<<<<` markers that
    `_in_progress_operation` couldn't see — and the next `harness commit`'s
    `git add -A` committed the conflict markers under a legitimate task
    message). `reset --merge` is the documented cleanup for a failed
    squash merge — it drops the conflicted index/tree changes without
    touching prior local commits; `merge --abort` needs the MERGE_HEAD
    that squash never writes."""
    try:
        run_git(repo, "merge", "--squash", task_branch)
    except GitError as exc:
        run_git(repo, "reset", "--merge", check=False)
        raise GitError(
            f"squash-merge of '{task_branch}' conflicted (working tree "
            f"restored — resolve on the task branch, then retry): {exc}"
        ) from exc
    run_git(repo, "commit", "-m", message)
    return head_sha(repo)


def autosquash(repo: Path, base: str) -> None:
    """Fold `fixup!` commits non-interactively (coverage B10)."""
    import os
    # `true` on EVERY platform: git launches editors through its own sh —
    # which Git for Windows bundles, `/usr/bin/true` included — so the
    # plain POSIX no-op works there too (probe-verified on this exact
    # flow). The previous `cmd /c exit 0` special case, written blind for
    # the Windows lane, was itself the breakage: git's sh-level editor
    # invocation mangled the multi-word command against the todo path
    # ("'epo' is not recognized…" — first Windows triage, 2026-07).
    noop = "true"
    proc = subprocess.run(
        ["git", "-C", str(repo), "rebase", "-i", "--autosquash", base],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "GIT_SEQUENCE_EDITOR": noop, "GIT_EDITOR": noop})
    if proc.returncode != 0:
        run_git(repo, "rebase", "--abort", check=False)
        raise GitError(f"autosquash rebase failed (aborted cleanly): {proc.stderr.strip()}")


def find_commit_by_subject(repo: Path, base: str, subject: str) -> str:
    """SHA re-derivation after a history rewrite (coverage B10)."""
    out = run_git(repo, "log", "--format=%H %s", f"{base}..HEAD")
    for line in out.splitlines():
        sha, _, subj = line.partition(" ")
        if subj == subject:
            return sha
    raise GitError(f"no commit with subject '{subject}' after rewrite")


def _mirror_excluded(rel: Path) -> bool:
    # Prefix match, not exact-name (adversarial-review finding: an editor
    # backup `human-input.ndjson.bak` or any near-name variant slipped past
    # the exact-name carve-out and got mirrored — and pushed).
    return any(part.endswith(".hmac")
               or any(part.startswith(ex) for ex in MIRROR_EXCLUDE)
               for part in rel.parts)


def publish_mirror(repo: Path, run_dir: Path, config: dict, run_name: str) -> str:
    """`harness publish-mirror` — path-exclusive ai/** snapshot. The privacy
    carve-out (human-input.ndjson never mirrored) and exclusivity are
    by construction: only ai/<run> is staged, then verified.

    The mirror PRUNES: a file deleted or renamed in the run dir is deleted
    from the mirror too (adversarial-review finding: copy-only mirroring
    kept both names of a renamed report forever, so the "mirror"
    misrepresented run state) — and a previously-leaked excluded file gets
    cleaned up rather than persisting."""
    dest = repo / "ai" / run_name
    # Refuse to mirror onto the live run itself (adversarial-review HIGH,
    # reproduced): when the repo IS the workspace, dest == run_dir, and the
    # prune below would delete the live run's seals + stamp a `.mirror`
    # marker onto it, bricking it beyond `reseal` recovery. initws refuses
    # registering the workspace-root as a repo, but publish_mirror never
    # re-checked — silent, permanent loss on a hand-edited/pre-0.13 config.
    if dest.resolve() == run_dir.resolve():
        raise GitError(
            f"refusing to publish the mirror onto the live run itself "
            f"({run_dir}) — the repo must not be the workspace root; the "
            "mirror strips seals, which would destroy the run's integrity "
            "chain. Register the actual project checkout as the repo.")
    dest.mkdir(parents=True, exist_ok=True)
    keep: set = set()
    for src in run_dir.rglob("*"):
        rel = src.relative_to(run_dir)
        if _mirror_excluded(rel):
            continue
        keep.add(rel)
        if src.is_dir():
            (dest / rel).mkdir(exist_ok=True)
        else:
            (dest / rel).write_bytes(src.read_bytes())
    for mirrored in sorted(dest.rglob("*"), reverse=True):  # leaves first
        rel = mirrored.relative_to(dest)
        if rel in keep:
            continue
        if mirrored.is_dir():
            try:
                mirrored.rmdir()  # only empties — a kept child keeps it
            except OSError:
                pass
        else:
            mirrored.unlink()
    # Stamped AFTER the prune (which would otherwise delete it): the mirror
    # is a dead ringer for a real run dir except its seals are excluded, so
    # a relative --run resolved from the wrong cwd hits it and reports
    # "no integrity seal" — indistinguishable from tampering (dogfood A2:
    # diagnosed as a transient race; it wasn't). state.load refuses on
    # this marker with the actual explanation instead.
    (dest / ".mirror").write_text(
        "published snapshot — not the live run; the workspace's own "
        f"ai/{run_name}/ is the authority\n", encoding="utf-8")
    run_git(repo, "add", "-A", "--", f"ai/{run_name}")
    staged = run_git(repo, "diff", "--cached", "--name-only").splitlines()
    if not staged:
        return head_sha(repo)  # nothing new — mirror already current
    offenders = [p for p in staged if not p.startswith("ai/")]
    if offenders:
        raise GitError(f"mirror commit would not be path-exclusive: {offenders}")
    message = render(config["naming"]["commit"]["mirror"], run=run_name)
    run_git(repo, "commit", "-m", message)
    return head_sha(repo)


def sync_branch(repo: Path, onto: str) -> None:
    """`harness sync-branch` — the owned update-from-main entry point (RC4)."""
    proc = subprocess.run(["git", "-C", str(repo), "rebase", onto],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        run_git(repo, "rebase", "--abort", check=False)
        raise GitError(f"sync-branch rebase onto {onto} conflicted (aborted cleanly): "
                       f"{proc.stderr.strip()[:300]}")


def _push_remote(repo: Path) -> str:
    """`origin` when it exists, the sole remote otherwise — a repo whose
    remote is named anything else (common on forks: `upstream`+`fork`)
    used to fail with a hardcoded `origin` (adversarial-review finding).
    Multiple remotes and none named origin is genuinely ambiguous:
    refuse rather than guess."""
    remotes = [r for r in run_git(repo, "remote").splitlines() if r.strip()]
    if not remotes:
        raise GitError(f"{repo}: no git remote configured — nothing to push to")
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    raise GitError(
        f"{repo}: multiple remotes ({', '.join(remotes)}) and none named "
        "'origin' — ambiguous push target; add/rename one to 'origin'")


def push_branch(repo: Path, branch: str, force_with_lease: bool = False) -> None:
    """`harness push` — the owned entry point for updating the remote (RC1):
    raw `git push` is blocked the same way commit/merge/rebase/etc. are.
    Plain push after a normal commit; `--force-with-lease` after a history
    rewrite (autosquash, sync-branch) — lease, never bare `--force`, so a
    push that would clobber someone else's intervening remote commit fails
    instead of silently overwriting it."""
    args = ["push", "-u", _push_remote(repo), branch]
    if force_with_lease:
        args.append("--force-with-lease")
    run_git(repo, *args)


def default_branch(repo: Path) -> str:
    """The repo's default branch (origin/HEAD's target), falling back to
    `main` if that can't be resolved (no origin, detached, bare, ...) — a
    display-only BEST GUESS for discover()'s proposal output. Callers that
    actually ACT on a branch name (ensure_default_branch) must not trust
    this without verifying the branch exists — a wrong guess here used to
    be cosmetic; it no longer is.

    Prefix-strip, NOT rsplit('/') (adversarial-review finding: a default
    branch itself containing '/' — `release/2026`, any release-train
    convention — was mangled to its last segment, and if a local branch
    happened to share that name, ensure_default_branch silently switched
    the run onto the wrong branch)."""
    ref = run_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        return ref[len(prefix):]
    return "main"


def _branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch}"], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return proc.returncode == 0


def remote_branch_exists(repo: Path, branch: str) -> bool | None:
    """Does `branch` already exist on the repo's remote? Tri-state:
    True/False when the probe answered, **None when it could not be made** —
    no remote configured, an ambiguous remote set, an offline/auth/timeout
    failure. Callers must degrade on None (warn + flag), never read it as
    "absent": a connectivity blip must not be able to green-light the exact
    collision this exists to catch.

    field: dual-run comparison — `_branch_exists` checks
    `refs/heads/` LOCALLY only, so a fresh clone re-running a story sailed
    through preflight and discovered the prior run's branch hours later, at
    push, as a non-fast-forward — by then with five tasks of work committed
    on it and an open MR already occupying the name in two repos. The branch
    template is deterministic per work item, so a same-item rerun collides
    by construction; the probe has to reach the remote to see it.

    The pattern is the FULL `refs/heads/<branch>`, matching `_branch_exists`'s
    exactness: `ls-remote` matches patterns against the ref TAIL, so a bare
    `main` would also report a hit on `refs/heads/topic/main`. The returned
    ref is re-compared exactly for the same reason."""
    try:
        remote = _push_remote(repo)
    except (GitError, OSError):
        # OSError covers a missing `git` binary (FileNotFoundError), so the
        # tri-state contract holds for every unanswerable case the docstring
        # enumerates rather than leaking a raw exception (adversarial-review)
        return None  # no remote, or ambiguous — nothing to probe against
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", remote,
             f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        # includes TimeoutExpired: an unreachable host can block on auth
        return None
    if proc.returncode != 0:
        return None
    return any(line.split("\t")[-1].strip() == f"refs/heads/{branch}"
               for line in proc.stdout.splitlines() if line.strip())


def _in_progress_operation(repo: Path) -> str | None:
    """A mid-rebase/merge/cherry-pick repo can look clean to changed_files()
    (conflicts already resolved-and-staged, operation just not concluded)
    while still being unsafe to switch out from under. Checked directly
    against git-dir markers, not inferred from working-tree diff.

    The git dir is RESOLVED via rev-parse, never assumed to be `repo/.git`
    (adversarial-review finding, verified by execution: in a linked
    worktree — where every M5 task actually runs — `.git` is a FILE
    pointing at `.git/worktrees/<name>`, so the literal-path check
    returned None for every marker exactly where it was needed most).
    Unmerged index entries are checked too: a conflicted `merge --squash`
    writes no MERGE_HEAD at all, only conflict markers + a dirty index."""
    git_dir_raw = run_git(repo, "rev-parse", "--absolute-git-dir", check=False)
    git_dir = Path(git_dir_raw) if git_dir_raw else repo / ".git"
    for marker, name in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                        ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                        ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if (git_dir / marker).exists():
            return name
    if run_git(repo, "ls-files", "-u", check=False):
        return "unresolved merge (conflicted paths in the index)"
    return None


def ensure_default_branch(repo: Path, branch: str | None = None) -> dict:
    """`harness ensure-default-branch` — the reusable precondition every
    branch-sensitive step (discover, preflight, ...) shares: the repo must
    be clean and on its default branch before that step relies on it.
    Uncommitted changes and unresolved rebase/merge/cherry-pick state STOP
    here — never auto-stashed/committed/discarded/continued; the human
    decides what to do with them (same "surface, never auto-fix" pattern
    as contract drift / security findings). A clean tree on the wrong
    branch is safely switched, no confirmation needed."""
    target = branch or default_branch(repo)
    if not _branch_exists(repo, target):
        raise GitError(
            f"{repo}: branch '{target}' does not exist locally — "
            "could not confirm this is really the default branch "
            "(no resolvable origin/HEAD); pass --branch explicitly")
    in_progress = _in_progress_operation(repo)
    if in_progress:
        raise GitError(
            f"{repo} has a {in_progress} in progress — finish or abort it "
            "yourself before continuing; never auto-resolved")
    dirty = changed_files(repo)
    if dirty:
        shown = ", ".join(dirty[:5]) + ("..." if len(dirty) > 5 else "")
        raise GitError(
            f"{repo} has {len(dirty)} uncommitted change(s) ({shown}) — "
            "resolve, commit, or stash them yourself before continuing; "
            "never auto-discarded")
    current = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current == target:
        return {"switched": False, "branch": target}
    run_git(repo, "checkout", target)
    return {"switched": True, "branch": target, "from_branch": current}


# ------------------------------------------------------------- worktrees

def worktree_add(repo: Path, task_id: str, base_branch: str) -> dict:
    """Per-task worktree with uid8 collision-avoidance (M5 charter). One retry
    with a fresh uid; a second failure raises so the orchestrator can offer
    the documented direct-branch fallback explicitly."""
    import uuid
    last_err = None
    for _ in range(2):
        uid = uuid.uuid4().hex[:8]
        branch = f"task/{task_id}-{uid}"
        path = repo.parent / f"{repo.name}-wt-{task_id}-{uid}"
        try:
            run_git(repo, "worktree", "add", "-b", branch, str(path), base_branch)
            return {"path": str(path), "branch": branch}
        except GitError as exc:
            last_err = exc
    raise GitError(
        f"worktree creation failed twice for task {task_id} ({last_err}) — "
        "offer the direct-branch fallback (task branch in the main checkout, "
        "worktree: null) or fix the repo state; never proceed silently")


def worktree_remove(repo: Path, worktree: dict) -> None:
    run_git(repo, "worktree", "remove", "--force", worktree["path"], check=False)
    run_git(repo, "branch", "-D", worktree["branch"], check=False)
    run_git(repo, "worktree", "prune", check=False)


def diff_paths(repo: Path, base: str) -> list[str]:
    """All paths the branch touches vs base (committed) plus working changes."""
    committed = run_git(repo, "diff", "--name-only", f"{base}...HEAD")
    return sorted({*committed.splitlines(), *changed_files(repo)} - {""})


def diff_line_count(repo: Path, base: str) -> int:
    """Total added+removed lines across the same committed-plus-working
    scope `diff_paths` covers — quick_recheck's `quick_mode.loc_max` check
    (design.md piece 1: the size dimension of "quick", not just the
    disqualify-pattern dimension). Binary files show `-` for both counts in
    `--numstat`; skipped, not counted as a giant integer."""
    committed = run_git(repo, "diff", "--numstat", f"{base}...HEAD")
    working = run_git(repo, "diff", "--numstat")
    total = 0
    for line in (committed + "\n" + working).splitlines():
        if not line.strip():
            continue
        for count in line.split("\t")[:2]:
            if count.isdigit():
                total += int(count)
    return total


# ----------------------------------------------------------------- TDD pair

def _run_tests(repo: Path, test_cmd: str) -> tuple[int, str]:
    try:
        # utf-8 + replace, not the locale codec: test runners routinely
        # emit UTF-8 (check marks, tree glyphs), and Windows' cp1252 has
        # undefined bytes that make a locale decode RAISE mid-run.
        proc = subprocess.run(test_cmd, shell=True, cwd=repo,
                              capture_output=True, text=True, timeout=600,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        # Uncaught, this raised a raw traceback instead of the CLI's JSON
        # error contract (adversarial-review finding) — verify-red/green's
        # callers already handle RedProofError, so route it there instead.
        raise RedProofError(
            f"test command timed out after 600s: {test_cmd!r}") from exc
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-20:])
    return proc.returncode, tail


def _test_set(repo: Path, config: dict, declared: list[str] | None) -> tuple[dict, dict]:
    lang = config.get("language", {})
    test_globs = lang.get("test_paths", ["tests/**"])
    closure_globs = lang.get("test_closure", [])
    if declared:
        tests = [t for t in declared]
    else:
        tests = [f for f in changed_files(repo) if matches_any(f, test_globs)]
    if not tests:
        raise RedProofError("no test files identified — declare with --tests or "
                            "write tests under the configured test paths")
    closure = [f for f in run_git(repo, "ls-files").splitlines()
               if matches_any(f, closure_globs) and f not in tests]
    shas = {t: blob_sha(repo, t) for t in tests}
    closure_shas = {c: blob_sha(repo, c) for c in closure}
    return shas, closure_shas


def _quarantined(config: dict, task_repo, cmd: str,
                 run: Path | None) -> str:
    """Apply the repo's declared test quarantine to a command about to run.

    Resolved against the task's REGISTERED repo path, never the worktree
    `repo` argument: during develop each task runs in a linked worktree
    whose path can never match a `repos.yaml` name (the same reason
    resolve_test_cmd takes `task["repo"]`). A caller that passes neither
    config nor a registered path simply gets no exclusions — which surfaces
    as the quarantined test failing again, loudly, never as a silently
    wrong pass."""
    from . import initws
    if not config or task_repo is None:
        return cmd
    return initws.quarantine_cmd(config, task_repo, cmd, run)


def _quarantine_covers(entry: str, locked_path: str) -> bool:
    """Does one quarantine entry cover this locked test file? Both arguments
    are already normalized and case-folded.

    Exact string equality is NOT enough (whole-branch adversarial review,
    reproduced end to end). The declaration vocabulary refuses backslashes,
    absolutes, `..` and non-canonical spellings — but a DIRECTORY
    (`tests/legacy`) and a GLOB (`tests/**`, `tests/*.spec.ts`) are both
    perfectly canonical, and both are what a real runner wants: pytest's
    `--ignore` takes a directory and vitest's `--exclude` — the template the
    shipped example uses — is glob-native. Either one excludes a task's own
    locked test while a set intersection sees no overlap at all, which is
    precisely the silently-wrong-pass `_refuse_quarantine_overlap` exists to
    make impossible.

    A directory entry is string-indistinguishable from a file path, so this
    could never have been closed by tightening the declaration vocabulary
    alone — the comparison itself has to understand the shapes the runner
    understands.

    Deliberately GREEDY: `fnmatch`'s `*` spans `/`, so `tests/*` is read as
    covering `tests/a/b.spec.ts`. Over-refusal here is loud, immediate and
    fixable by narrowing the entry; under-refusal is the false green. Same
    asymmetry the case-folding rule above already states.
    """
    if entry == locked_path:
        return True
    if locked_path.startswith(entry + "/"):
        return True                              # directory-shaped entry
    # Matched both ways: the entry is the pattern in every realistic config,
    # but a locked path carrying a metacharacter must not become the one
    # spelling that slips through.
    return (fnmatch.fnmatchcase(locked_path, entry)
            or fnmatch.fnmatchcase(entry, locked_path))


def _refuse_quarantine_overlap(config: dict, task_repo, locked: dict) -> None:
    """Refuse when a quarantine entry covers one of the red-proof's OWN
    locked test files.

    adversarial-review: without this, an overlap degrades in two wrong ways
    — verify-red reports "test suite PASSES — not red" (true, but it points
    the developer at a vacuous test rather than at the exclusion), and
    verify-green passes while the task's own test is never EXECUTED at all:
    the blob-SHA check confirms the file is unchanged, and the assertion
    simply never runs. That is the one silently-wrong-pass this mechanism
    must not be able to produce."""
    from . import initws
    if not config or task_repo is None:
        return
    # Case-folded: on a case-insensitive filesystem `Tests/Foo.spec.ts` and
    # `tests/foo.spec.ts` are one file, so a case-only difference must not
    # slip the guard. Over-refusing here is loud and fixable; under-refusing
    # is the silent false green (re-verify finding).
    quarantined = {p.casefold(): p
                   for p in initws.quarantined_paths(config, task_repo)}
    locked_norm = {initws.normalize_test_path(p).casefold(): p for p in locked}
    # Reported as PAIRS: with directory and glob entries in play, naming only
    # the entry leaves a developer guessing which of the task's own files it
    # swallowed (whole-branch review).
    overlap = sorted({(orig, locked_norm[lk])
                      for k, orig in quarantined.items()
                      for lk in locked_norm
                      if _quarantine_covers(k, lk)})
    if overlap:
        pairs = ", ".join(f"{q} covers {l}" for q, l in overlap)
        raise RedProofError(
            f"quarantine overlaps this task's locked test set ({pairs}) — "
            "the task's own test would be excluded from the run and never "
            "execute. A quarantine entry covers a locked file when it names "
            "it exactly, when it is a parent directory of it, or when it is "
            "a glob matching it. Narrow the quarantine entry, or move this "
            "task's test to another file; never both.")


def _declared_test_intents(workspace: Path, run: Path, task_id: str) -> list[str]:
    """The plan's declared test-intent names for this task (empty in quick
    mode, or any mode with no plan step — consistent with its relaxations).
    A task_id absent from state.yaml is a caller error (typo'd --task),
    not "nothing declared" — fail loud, matching transitions.py's identical
    task-lookup sibling functions rather than silently defaulting."""
    with state_mod.locked_read(run):  # torn-read guard, same as show/verify
        st = state_mod.load(run, workspace)
    tasks = st.get("tasks", [])
    task = next((t for t in tasks if t["id"] == task_id), None)
    if task is None:
        raise RedProofError(f"task {task_id}: not found in state.yaml — check --task")
    return task.get("test_intents", [])


def _task_repo(workspace: Path, run: Path, task_id: str):
    """The task's REGISTERED repo path (what `repos.yaml` names), for config
    lookups that must not see a worktree path. Absent task -> None; the
    caller's own lookup raises the loud error."""
    with state_mod.locked_read(run):   # torn-read guard, same as its sibling
        st = state_mod.load(run, workspace)
    task = next((t for t in st.get("tasks", []) if t["id"] == task_id), None)
    return task.get("repo") if task else None


def _missing_intents(repo: Path, tests: dict, closure: dict,
                     declared_intents: list[str]) -> list[str]:
    """Which declared test-intent names don't appear as a whole identifier
    anywhere in the actual test files OR their closure (RC4's same test/
    fixture widening, design.md:398 — a shared base-class test method lives
    in a closure file, not the primary test glob) — presence only (coverage
    B1); whether a present name genuinely tests its declared intent stays
    reviewer judgment (design.md:392). Identifier-boundary matched (`\\b`),
    not a bare substring: a declared `test_api` must not be satisfied by an
    unrelated `test_api_v2`."""
    if not declared_intents:
        return []
    content = ""
    for t in list(tests) + list(closure):
        try:
            content += (repo / t).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return [name for name in declared_intents
            if not re.search(rf"\b{re.escape(name)}\b", content)]


def verify_red(run: Path, workspace: Path, repo: Path, config: dict, task_id: str,
               test_cmd: str, declared: list[str] | None = None,
               intents: list[str] | None = None,
               revise: bool = False, reason: str | None = None) -> dict:
    """Prove the test genuinely fails BEFORE the impl exists; seal the proof.
    Also seals the declared-vs-actual test-intent floor (coverage B1,
    m8-plan-fidelity.md WS-2): `intents` overrides, else the plan's declared
    `test_intents` for this task load from state.yaml automatically."""
    from .transitions import redproof_label, redproof_path  # shared with the set-state guard
    if intents is None and not _declared_test_intents(workspace, run, task_id):
        # A task the plan registered with NO test-intents (docs/chore) has
        # nothing that can ever go red — the completion guard exempts it
        # (same `test_intents: []` opt-out, transitions._guard_red_proof),
        # so refuse loudly HERE instead of the misleading "suite PASSES —
        # not red" (e2e field finding: that message sent the developer
        # chasing a failing test the plan never asked for).
        raise RedProofError(
            f"task {task_id}: the plan declares no test-intents for it — "
            "no red-proof is needed or possible; implement, commit, and "
            "move it to in-review directly (the completion guard exempts "
            "no-intents tasks)")
    key = chain.load_key(workspace)  # strict: never mint from a drifted cwd
    path = redproof_path(run, task_id)
    if path.exists() and not revise:
        raise RedProofError(
            f"task {task_id}: red-proof already exists — revising a locked test "
            "requires --revise --reason (flagged, reviewer-visible; never silent)")
    if revise and not reason:
        raise RedProofError("--revise requires --reason")
    # Quarantine applies to the RED run too, and strengthens it: a suite that
    # is red only because an unrelated pre-existing failure is still in it is
    # not proof that THIS task's test fails. Excluding those first makes the
    # red-proof about the task's own declared intents (field: dual-run
    # comparison — that exact spec aborted three runs of the frontend suite).
    #
    # Overlap refusal BEFORE the run (pre-release review: with the check
    # after the code==0 raise, a quarantine entry covering the task's own
    # test made the excluded suite pass and the misleading "PASSES — not
    # red" fired first — sending the developer to fix a "vacuous" test while
    # the real cause was the exclusion. The check needs nothing from the
    # run, so it goes first and names the actual problem).
    task_repo = _task_repo(workspace, run, task_id)
    tests, closure = _test_set(repo, config, declared)
    _refuse_quarantine_overlap(config, task_repo, tests)
    code, tail = _run_tests(repo, _quarantined(config, task_repo, test_cmd, run))
    if code == 0:
        raise RedProofError(
            f"task {task_id}: test suite PASSES — not red. Test-first means the "
            "failing test exists before the implementation.")
    declared_intents = intents if intents is not None else _declared_test_intents(
        workspace, run, task_id)
    missing_intents = _missing_intents(repo, tests, closure, declared_intents)
    proof = {"task": task_id, "at": now_iso(), "tests": tests, "closure": closure,
             "evidence": {"exit_code": code, "tail": tail},
             "declared_intents": declared_intents, "missing_intents": missing_intents,
             "revision": {"reason": reason, "at": now_iso()} if revise else None}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sealed under the run lock: chain.seal's content-then-seal write is two
    # separate atomic replaces, and an unlocked reader landing between them
    # sees a spurious IntegrityError (the exact torn-read race locked_read
    # documents for state.yaml — the red-proof path just never got the same
    # treatment, adversarial-review finding). The label binds the seal to
    # this task's identity (see transitions.redproof_label).
    with state_mod.locked(run):
        chain.seal(path, json.dumps(proof, sort_keys=True).encode(), key,
                   label=redproof_label(task_id))
    append_record(run / "events.ndjson",
                  {"kind": "test-revision" if revise else "red-proof",
                   "task": task_id, "reason": reason,
                   "tests": sorted(tests)})
    return proof


def verify_green(proof: dict, repo: Path, test_cmd: str | None,
                 run_tests: bool = True, config: dict | None = None,
                 task_repo=None, run: Path | None = None) -> None:
    """The completion checkpoint: test passes AND the locked set is unchanged
    (blob-SHA comparison catches ANY mutation path — Write/Edit/sed/checkout).
    `run_tests=False` re-checks only the SHAs — used inside the state lock
    after the expensive test run already happened outside it (RC4).

    `config`/`task_repo`/`run` carry the declared test quarantine down to the
    actual run (see `_quarantined`); the SHA-only path never runs a command,
    so it needs none of them."""
    for path, sha in {**proof["tests"], **proof["closure"]}.items():
        current = blob_sha(repo, path) if (repo / path).exists() else "<deleted>"
        if current != sha:
            raise RedProofError(
                f"locked test file '{path}' changed since red-proof "
                f"(sha {sha[:8]} -> {current[:8]}) — use the flagged revision "
                "path (verify-red --revise --reason), never a silent edit")
    if run_tests:
        _refuse_quarantine_overlap(config, task_repo, proof["tests"])
        code, tail = _run_tests(repo, _quarantined(config, task_repo,
                                                   test_cmd, run))
        if code != 0:
            raise RedProofError(f"tests still failing (exit {code}) — not green:\n{tail}")
