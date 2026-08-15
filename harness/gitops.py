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


# ------------------------------------------------- subtree logical repos
#
# A registered repo path is not necessarily a physical checkout root. A
# LOGICAL repo may be any SUBTREE of one — `<checkout>/frontend` alongside
# `<checkout>` itself — which is exactly the shape `initws.discover()`'s
# `monorepo_split` proposes and the real .NET case demands (the `.sln` at the
# physical root is one logical repo, `frontend/` is another, one `.git`
# between them). `repos.yaml` stays `name -> path`; the subtree path IS the
# registered path, so every exact-path resolver (repo_name, resolve_test_cmd,
# quarantine) keeps working untouched.
#
# What that costs this module is PATH RELATIVITY, because git's own answers
# are not uniformly relative to the directory you asked from:
#   - `diff --name-only/--numstat` and `status --porcelain` are TOPLEVEL-
#     relative no matter the cwd — so a subtree repo's file list arrives
#     carrying a `frontend/` prefix that no consumer expects (test_paths
#     globs, blob-SHA locks and gate scopes all read these as repo-relative).
#     `--relative` is git's own fix and does BOTH halves at once: it drops
#     entries outside the cwd's subtree and re-roots the rest. Probe-verified
#     on git 2.55 for --name-only, --numstat and --cached, and a no-op at a
#     toplevel (empty prefix) — so root repos are byte-identical.
#   - `ls-files` (with or without --others) is already CWD-relative AND
#     cwd-scoped, so it needs nothing.
# Anything added later that has no `--relative` (status, notably) must
# translate through `subtree_prefix` by hand; that is what the pair below is
# for, and why they live here rather than inlined at one call site.


def toplevel(repo: Path) -> Path:
    """The PHYSICAL checkout `repo` lives in — `repo` itself for a root
    registration, the enclosing checkout for a subtree one."""
    return Path(run_git(repo, "rev-parse", "--show-toplevel"))


def subtree_prefix(repo: Path) -> str:
    """`repo`'s position INSIDE its physical checkout — `''` for a root
    registration, `'frontend'` for a subtree one. Trailing slash stripped
    (git emits `frontend/`) so callers compose it explicitly."""
    return run_git(repo, "rev-parse", "--show-prefix").strip("/")


def work_tree_root(path) -> Path | None:
    """The checkout `path` belongs to, or None when it is not inside a git
    work tree at all. The registration probe: `(path/".git").exists()` is
    true for a checkout ROOT only, which is precisely the assumption subtree
    logical repos break. Never raises — a missing directory, a bare repo, or
    no `git` on PATH are all "not a work tree", and a verify-time check must
    report that, not traceback."""
    try:
        out = run_git(Path(str(path)), "rev-parse", "--show-toplevel",
                      check=False)
    except OSError:                    # no git binary — unanswerable, not fatal
        return None
    return Path(out) if out else None


def has_tracked_files(path) -> bool:
    """Does `path` hold anything git actually TRACKS? `work_tree_root` only
    answers "inside a work tree", which an UNTRACKED or .gitignored
    directory satisfies exactly as well as a real subtree — probed on git
    2.55: `git -C <checkout>/generated rev-parse --show-toplevel` answers
    `<checkout>` for a `generated/` that `.gitignore` excludes. A
    registration there verifies clean and then breaks at the first task,
    because `git worktree add` materializes only what the BRANCH carries:
    the new worktree appears WITHOUT that directory, `_run_tests(cwd=...)`
    raises on the missing path, and the CLI's resume probe
    (`Path(recorded["path"]).is_dir()`) stays false forever. `ls-files` is
    cwd-scoped and index-backed, so a non-empty answer is precisely "a
    checkout of this branch brings this directory with it".

    Never raises — same contract as `work_tree_root`, since both exist to
    answer a verify-time question about a path that may not be there."""
    try:
        return bool(run_git(Path(str(path)), "ls-files", check=False))
    except OSError:                    # no git binary — unanswerable, not fatal
        return False


def _dirt_subject(repo, top) -> str:
    """Who a "N uncommitted change(s)" refusal is ABOUT. Both refusals
    (`ensure_default_branch`, `update_base`) probe `changed_files(
    toplevel(repo))` deliberately — `checkout` and `merge --ff-only`
    rewrite the whole physical tree — and the paths that come back are
    therefore TOPLEVEL-relative. Naming `repo` as the subject while listing
    those paths sent the user to clean a file in a directory that doesn't
    contain it (reproduced: `...\\ws\\mono\\frontend has 1 uncommitted
    change(s) (.gitignore)` for a `.gitignore` living at `...\\ws\\mono`).
    Subject and paths now name the same tree, and the subject states the
    relationship so "why is it complaining about a sibling's file" is
    answered in the refusal rather than left to the reader.

    A root registration IS its own toplevel, so its message is the exact
    string it has always been."""
    try:
        same = Path(str(top)).resolve() == Path(str(repo)).resolve()
    except OSError:                    # unresolvable — say more, never less
        same = str(top) == str(repo)
    if same:
        return str(repo)
    return f"{top} (the physical checkout holding the registered repo {repo})"


def shares_toplevel(config_repos: dict, repo) -> list[str]:
    """Names of OTHER registered repos resolving into the same physical
    checkout as `repo` — the parent/child overlap a monorepo split creates
    deliberately (root `.sln` repo + `frontend/` repo, one `.git`).

    Context is the DIRECT-BRANCH FALLBACK, the documented escape when
    `worktree_add` fails twice: that fallback cuts the task branch in the
    MAIN checkout, and a checkout switch flips the WHOLE physical tree — so
    where the toplevel is shared it yanks the sibling logical repo's files
    out from under whatever task is running there. Per-task worktrees are
    immune (each is its own tree); the fallback is the one path that isn't.

    This list NAMES colliders; it does not decide the refusal, and must not
    be made to (adversarial-review finding, reproduced): the hazard is a
    property of the physical checkout, not of the registry, so with only
    `frontend` registered this returns `[]` while cutting a task branch in
    the shared checkout still flips `backend/`, `infra/`, and any
    uncommitted human work sitting beside them. `worktree_add` refuses on
    subtree-ness itself and uses this only to say WHO ELSE it knows about.

    Never raises: an unreadable or non-git registration is simply not a
    collision, and message enrichment must not be able to replace the real
    worktree failure with a lookup error."""
    mine = work_tree_root(repo)
    if mine is None:
        return []
    try:
        here = Path(str(repo)).resolve()
        mine = mine.resolve()
    except OSError:
        return []
    out = []
    for name, path in (config_repos or {}).items():
        try:
            if Path(str(path)).resolve() == here:
                continue               # the same registration, not a collision
            other = work_tree_root(path)
            if other is not None and other.resolve() == mine:
                out.append(name)
        except OSError:
            continue
    return sorted(out)


def blob_sha(repo: Path, path: str) -> str:
    # `hash-object` addresses the WORKING TREE relative to cwd (`git -C repo`),
    # not the index, so a subtree-relative path already resolves against the
    # logical repo — no `:<prefix><path>` index rewriting needed here. The
    # locked-set paths reaching this come from `changed_files`/`ls-files`,
    # both subtree-relative by the rules above, so the SHA lock names the
    # blob the developer is actually editing.
    return run_git(repo, "hash-object", "--", path)


def changed_files(repo: Path) -> list[str]:
    # `diff --name-only` + `ls-files --others`: clean one-path-per-line output,
    # no status columns to mis-parse (porcelain parsing is exactly the kind of
    # fragile reverse-engineering this module exists to avoid).
    # `--relative` scopes the diff half to the registered subtree and re-roots
    # it (see the relativity note above); the `ls-files` half already is.
    tracked = run_git(repo, "diff", "--name-only", "--relative", "HEAD")
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
    # Deliberately NOT `--relative` above: the whole index is what `git
    # commit` will write, so a key staged outside the registered subtree
    # still has to be caught. That leaves `hits` TOPLEVEL-relative, which a
    # `reset` issued from a subtree cwd would mis-target — so the unstage is
    # issued from the toplevel, where those paths mean what they say.
    run_git(toplevel(repo), "reset", "--", *hits, check=False)
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
    # `-- .` bounds the sweep to the REGISTERED path. Identical to a bare
    # `add -A` for a root repo (`.` is the whole tree there); for a subtree
    # logical repo it is the guarantee that a `frontend` task can never
    # stage — let alone commit — an edit someone left in `backend/`, which
    # shares the same `.git` and would otherwise ride along silently under a
    # frontend task's message.
    run_git(repo, "add", "-A", "--", ".")
    _refuse_swept_secrets(repo)
    if not run_git(repo, "diff", "--cached", "--name-only"):
        raise GitError("nothing to commit")
    run_git(repo, "commit", "-m", render(template, **params))
    return head_sha(repo)


def commit_fixup(repo: Path, target_sha: str) -> str:
    """`harness commit --fixup-of` — post-squash fix commits (coverage B10)."""
    run_git(repo, "add", "-A", "--", ".")   # subtree-scoped, see commit_class
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
    # Toplevel-relative on purpose (no `--relative`): exclusivity is a claim
    # about the whole commit, so a staged path OUTSIDE the registered subtree
    # must be reported, not filtered away. The expected prefix therefore has
    # to carry the subtree too — `frontend/ai/<run>` for a logical repo
    # registered at `frontend`, plain `ai/<run>` at a checkout root.
    prefix = subtree_prefix(repo)
    wanted = f"{prefix}/ai/" if prefix else "ai/"
    offenders = [p for p in staged if not p.startswith(wanted)]
    if offenders:
        raise GitError(f"mirror commit would not be path-exclusive: {offenders}")
    message = render(config["naming"]["commit"]["mirror"], run=run_name)
    run_git(repo, "commit", "-m", message)
    return head_sha(repo)


def sync_branch(repo: Path, onto: str) -> dict:
    """`harness sync-branch` — the owned update-from-main entry point (RC4).

    FETCHES first, then rebases onto the freshly-fetched tip. It used to
    rebase onto the LOCAL `onto` ref, which nothing in the package had ever
    fetched — so its one documented purpose ("if the base moved upstream,
    sync FIRST", apply-fixes.md) was unachievable: on a stale local base the
    rebase was a no-op that reported success, and the caller pushed believing
    it had caught up. A sync verb that cannot see the thing it syncs to is
    worse than no verb, because it launders the staleness as handled.

    Unlike `ensure_default_branch` — which measures and refuses to move
    anything — moving the branch IS this verb's mandate: the caller invoked
    an update. So it rebases onto FETCH_HEAD (the same explicit-refspec
    reasoning as `base_branch_behind`: the remote-tracking ref is only
    written when the refspec is configured for it).

    An unanswerable remote does NOT block — the PR-comment loop this serves
    must still work offline — but the result says so (`remote_verified`),
    because "rebased onto a local ref I could not confirm" and "rebased onto
    the real upstream tip" are different facts and reporting them
    identically is the original bug in miniature."""
    verified = fetch_base(repo, onto)
    target = "FETCH_HEAD" if verified else onto
    proc = subprocess.run(["git", "-C", str(repo), "rebase", target],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        run_git(repo, "rebase", "--abort", check=False)
        raise GitError(f"sync-branch rebase onto {onto} conflicted (aborted cleanly): "
                       f"{proc.stderr.strip()[:300]}")
    return {"onto": onto, "remote_verified": verified}


def update_base(repo: Path, branch: str | None = None) -> dict:
    """`harness update-base` — the owned entry point that fast-forwards the
    BASE branch onto its remote. The terminating remedy for the staleness
    `base_branch_behind` measures.

    field (US-CHAT-01 lean run): the pipeline could MEASURE a stale base and
    act on it nowhere. `sync_branch` rebases whatever branch is checked out (a
    FEATURE branch catching up to a moved base — apply-fixes.md); nothing
    updated the base itself. So preflight's stated remedy — "surface the count
    and let them decide to update the base first" — named a decision with no
    verb behind it, and the human's only route was the raw `git pull` /
    `git merge` that `GIT_VERB_RE` blocks by design. A documented remedy that
    cannot terminate is the bug class this repo already treats as real.

    **It never switches branches, and never touches the working tree unless
    the base is the branch you are standing on.** The first draft reused
    `ensure_default_branch` for its clean/exists preconditions and inherited
    the branch SWITCH that comes with them — adversarial review found the
    consequence, reproduced end to end: preflight cuts `feat/X-1`, reports
    `behind: 4`, the human runs this verb, and the checkout silently lands on
    `main`. Preflight's idempotent re-run returns the cached `branches` entry
    without switching back, and `merge-task` then squash-commits every task's
    work ONTO THE BASE, with `create-pr` opening a PR whose head branch has
    none of it. A verb whose whole job is "make the base current" must not be
    able to lose the branch the run lives on.

    So the branch ref moves, the HEAD does not. When the target is NOT checked
    out, the fast-forward is a compare-and-swap on the ref itself
    (`update-ref` with the old value) — nothing enters the working tree, which
    also means a dirty tree elsewhere in the repo is irrelevant, exactly the
    plan-time state `workflow.base_check` deliberately tolerates. Both lenses
    landed on that mismatch independently: the remedy must be reachable in the
    state its own trigger is designed to accept.

    Four refusals bound it, because this is the first verb that moves a base
    branch:

    - **No in-progress operation**, and — only when the target IS the current
      branch — **a clean tree**, since that is the one case where a
      fast-forward rewrites files under the human. Never auto-stashed.
    - **Fast-forward only.** `merge --ff-only` when checked out, an
      old-value-checked `update-ref` when not: the base ends up byte-identical
      to what upstream published, never a merge commit and never a rebase.
      Genuine divergence (behind AND ahead) refuses with both counts; the human
      resolves it, the harness never guesses.
    - **The remote must have answered.** Unlike `base_branch_behind` /
      `remote_branch_exists` — measurements, where failing open keeps a
      connectivity blip from bricking a step — this one MOVES a ref, and a
      no-op that reports success is exactly the defect `sync_branch`'s
      docstring documents ("a sync verb that cannot see the thing it syncs to
      is worse than no verb, because it launders the staleness as handled").

    `ahead` is REPORTED, never raised on, when `behind` is 0: there is nothing
    to fast-forward, so refusing "you are already current" would be noise —
    but unpushed commits sitting on a base branch are worth seeing, so the
    number rides out in the result either way.
    """
    target = branch or default_branch(repo)
    if not _branch_exists(repo, target):
        raise GitError(
            f"{repo}: branch '{target}' does not exist locally — could not "
            "confirm this is really the base branch (no resolvable "
            "origin/HEAD); pass --branch explicitly")
    in_progress = _in_progress_operation(repo)
    if in_progress:
        raise GitError(
            f"{repo} has a {in_progress} in progress — finish or abort it "
            "yourself before continuing; never auto-resolved")
    current = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    checked_out = current == target
    elsewhere = _worktree_holding(repo, target) if not checked_out else None
    if elsewhere is not None:
        # `git update-ref` — unlike `git branch -f` — will happily move a
        # branch that another worktree has checked out, and `rev-parse
        # --abbrev-ref HEAD` only ever sees the worktree we were invoked in.
        # Re-verification reproduced the consequence: advancing `main` from a
        # linked worktree left the OTHER checkout holding a staged revert of
        # the upstream commits, which the next `harness commit` there would
        # have committed. Refuse instead — this verb has no mandate over a
        # working tree it cannot see. (`_in_progress_operation` already
        # resolves the git dir through rev-parse for the same reason: linked
        # worktrees are a normal runtime shape here, since every task runs in
        # one.)
        raise GitError(
            f"{repo}: '{target}' is checked out in another worktree "
            f"({elsewhere}) — fast-forwarding the ref from here would leave "
            "that working tree holding a staged reversal of the upstream "
            "commits. Run update-base from that worktree, or detach/switch it "
            "first; never moved out from under a checkout.")
    if checked_out:
        # Toplevel-scoped for the same reason ensure_default_branch is: the
        # `merge --ff-only` below rewrites the whole physical tree, so a
        # sibling logical repo's uncommitted work is just as much at risk as
        # this one's. The SUBJECT follows the scope (`_dirt_subject`) —
        # toplevel-relative paths under a subtree-repo heading named a
        # directory that does not contain them.
        top = toplevel(repo)
        dirty = changed_files(top)
        if dirty:
            shown = ", ".join(dirty[:5]) + ("..." if len(dirty) > 5 else "")
            raise GitError(
                f"{_dirt_subject(repo, top)} has {len(dirty)} uncommitted "
                f"change(s) ({shown}) on "
                f"'{target}' itself — a fast-forward would rewrite them; "
                "commit or stash them yourself first, never auto-discarded. "
                "(Uncommitted work on ANY OTHER branch is fine: this verb "
                "moves the base ref without touching your working tree.)")
    before = run_git(repo, "rev-parse", target)
    remote = _fetch_remote(repo, target)
    if remote is None or not fetch_base(repo, target):
        raise GitError(_unreachable_base_detail(repo, target, remote))
    behind = _rev_count(repo, f"{target}..FETCH_HEAD")
    ahead = _rev_count(repo, f"FETCH_HEAD..{target}")
    if behind is None or ahead is None:
        raise GitError(
            f"{repo}: fetched, but could not count the gap between '{target}' "
            "and its remote — refusing to move the branch on an unreadable "
            "comparison")
    out = {"branch": target, "remote": remote, "before": before,
           "behind": behind, "ahead": ahead, "checked_out": checked_out,
           "current_branch": current}
    if behind and ahead:
        raise GitError(
            f"{repo}: '{target}' has diverged from '{remote}' ({ahead} local-"
            f"only commit(s), {behind} upstream) — not a fast-forward, and "
            "update-base will not rebase or merge a base branch. Resolve it "
            "yourself (push, rebase, or reset those local commits), then "
            "retry.")
    if not behind:
        # Re-read rather than echoing `before`: nothing moved here, but the
        # value was sampled before the fetch, and reporting a sha the branch
        # may no longer be at is the kind of small lie this module doesn't tell.
        return {**out, "after": run_git(repo, "rev-parse", target),
                "advanced": False}
    if checked_out:
        run_git(repo, "merge", "--ff-only", "FETCH_HEAD")
    else:
        # Compare-and-swap on the ref, with the value we measured against as
        # the expected old: a concurrent update between the count and the
        # write fails the swap instead of silently clobbering it. `-m` so the
        # move lands in the reflog like every other branch update.
        run_git(repo, "update-ref", "-m", "harness update-base: fast-forward",
                f"refs/heads/{target}", "FETCH_HEAD", before)
    return {**out, "after": run_git(repo, "rev-parse", target),
            "advanced": True}


def _worktree_holding(repo: Path, branch: str) -> str | None:
    """The path of a linked worktree (or the primary checkout) that currently
    has `branch` checked out, when it is NOT the one we were invoked in —
    None otherwise. `git worktree list --porcelain` is the only input that
    sees past the current worktree's HEAD."""
    out = run_git(repo, "worktree", "list", "--porcelain", check=False)
    try:
        here = Path(run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    except (GitError, OSError):
        here = Path(repo).resolve()
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}" and path:
            try:
                if Path(path).resolve() != here:
                    return path
            except OSError:
                return path
    return None


def _unreachable_base_detail(repo: Path, target: str, remote: str | None) -> str:
    """Why the base could not be refreshed — the remote genuinely being
    unreachable is only one of the reasons `fetch_base` returns False, and
    reporting them all as connectivity sent the human to fix a network that
    was fine (adversarial review, reproduced on a base branch that simply had
    never been pushed — where `base_check` reports `behind: null`, "nothing to
    do", about the very same repo)."""
    if remote is None:
        return (f"{repo}: no usable remote for '{target}' — no remote is "
                "configured, or several are and none is named 'origin' and "
                f"'{target}' has no configured upstream. Set "
                f"`branch.{target}.remote`, or name a remote 'origin'.")
    configured = [r for r in run_git(repo, "remote", check=False).splitlines()
                  if r.strip()]
    if remote not in configured and "://" not in remote and ":" not in remote:
        # A stale `branch.<target>.remote` left behind by a `git remote
        # rename`/removal. Making that config key load-bearing (so a fork
        # layout measures against canonical) is what put this in reach, so the
        # fix that widened the blind spot closes it too — otherwise it reports
        # as connectivity, the exact misdiagnosis this function exists to end.
        # URL-shaped values are legal in that key and are not remote names.
        return (f"{repo}: `branch.{target}.remote` names '{remote}', which is "
                f"not a configured remote ({', '.join(configured) or 'none'}) "
                "— the branch's upstream config is stale, probably after a "
                "`git remote rename`/removal. Repoint it, or unset it to fall "
                "back to the push remote. (Connectivity is fine.)")
    if remote_branch_exists(repo, target, remote=remote) is False:
        return (f"{repo}: '{remote}' has no branch '{target}' — the base "
                "exists only locally, so there is nothing to fast-forward "
                "onto. Push it first, or pass --branch to name the base that "
                "really tracks the remote. (Connectivity is fine; this is not "
                "an offline/auth failure.)")
    return (f"{repo}: could not reach '{remote}' to update '{target}' "
            "(offline, auth, or timeout) — refusing rather than reporting a "
            "fast-forward that did not happen. Fix connectivity and retry; "
            "nothing was moved.")


def _rev_count(repo: Path, range_spec: str) -> int | None:
    """`rev-list --count`, None when git could not answer — the same
    unreadable-comparison degradation `base_branch_behind` uses."""
    count = run_git(repo, "rev-list", "--count", range_spec, check=False)
    return int(count) if count.isdigit() else None


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


def _fetch_remote(repo: Path, branch: str) -> str | None:
    """Which remote is `branch` actually tracking? Its CONFIGURED upstream
    (`branch.<name>.remote`) when it has one, the push remote otherwise, None
    when neither can be resolved.

    adversarial review, reproduced on a fork layout — the standard
    `origin`=my-fork / `upstream`=canonical setup, with `main` tracking
    `upstream`. `_push_remote` prefers `origin` unconditionally, so every
    staleness question was answered against the FORK: `base_branch_behind`
    returned 0 and `update_base` reported "already current" while the base was
    genuinely behind canonical. That is the same laundering `sync_branch`'s
    docstring calls worse than no verb, reached through the remote-resolution
    seam instead of the connectivity one — and the new resolver made it worse
    than a missed measurement, because `base_check` would then write
    `base-branch-current` into the audit ledger: a false clean bill of health.

    Deliberately shared with `fetch_base`, so the measurer, the mover and
    `sync_branch` can never disagree about which remote "the current base"
    means — the same anti-drift reason `fetch_base` itself is one
    implementation with several callers."""
    configured = run_git(repo, "config", "--get", f"branch.{branch}.remote",
                         check=False)
    if configured.strip():
        return configured.strip()
    try:
        return _push_remote(repo)
    except (GitError, OSError):
        return None            # no remote, or ambiguous — structural


def remote_branch_exists(repo: Path, branch: str,
                         remote: str | None = None) -> bool | None:
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
    ref is re-compared exactly for the same reason.

    `remote` overrides the resolution for callers that already know which one
    they mean — `update_base` asks about the remote it actually fetched from,
    which on a fork layout is not the one `_push_remote` would pick."""
    if remote is None:
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


def base_branch_behind(repo: Path, branch: str) -> int | None:
    """How many commits `branch` is behind its remote counterpart — or None
    when that is unanswerable (no remote, ambiguous remotes, offline, auth,
    no such branch upstream). Same tri-state fail-open contract as
    `remote_branch_exists`, and the same reason: a connectivity blip must
    never brick preflight.

    FETCH, never pull (field question, 2026-08-04: "is main pulled to latest
    before preflight?" — it was not, in any mode). `ensure_default_branch`
    guarantees you are ON the default branch, which is not the same as being
    AT it: a local `main` last updated weeks ago passes every one of its
    checks, and preflight then cuts the feature branch from that stale
    commit — so the red-proof, develop's suite and the reviewer's re-run all
    execute against code that is not what the change will merge into.

    Fetch is the honest tool here. It moves only remote-tracking refs —
    never the working tree, never local `main` — so it can answer the
    question without violating this module's "surface, never auto-fix"
    stance (a `pull` could conflict, or absorb upstream code the human never
    asked for, exactly what the dirty-tree and in-progress refusals exist to
    prevent). The caller reports the number; the human decides."""
    if not fetch_base(repo, branch):
        return None
    return _rev_count(repo, f"{branch}..FETCH_HEAD")


def fetch_base(repo: Path, branch: str) -> bool:
    """Refresh `branch` from its remote into FETCH_HEAD. True when the remote
    genuinely answered; False for every unanswerable case (no remote,
    ambiguous remotes, offline, auth, no such branch upstream) — the
    tri-state fail-open contract `remote_branch_exists` established, so a
    connectivity blip never bricks a step.

    One implementation, three callers (`base_branch_behind` measures the gap,
    `sync_branch` rebases across it, `update_base` fast-forwards it) — they
    must never disagree about what "the current base" means, and duplicating
    the remote-resolution and fail-open branches is exactly how that drift
    starts. The remote comes from `_fetch_remote`, which prefers the branch's
    CONFIGURED upstream: on a fork layout every one of them used to answer
    against `origin` (the fork) rather than the canonical remote the base
    actually tracks — see that function.

    Read-only by construction: fetching an explicit refspec writes
    FETCH_HEAD and the remote-tracking ref, never the working tree and never
    the local branch. Callers that then MOVE something (sync_branch,
    update_base) are doing so on their own mandate, not this function's."""
    remote = _fetch_remote(repo, branch)
    if remote is None:
        return False                   # no remote, or ambiguous — structural
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", remote, branch],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return False                   # includes TimeoutExpired (auth prompt)
    return proc.returncode == 0


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
    branch is safely switched, no confirmation needed.

    Reports `behind`: how many commits the target trails its remote (None if
    unanswerable). This function's guarantee is "clean, and ON the default
    branch" — it deliberately does NOT make that branch current, because
    pulling is the kind of auto-mutation everything above refuses to do. It
    now MEASURES the gap instead of leaving it invisible; acting on the
    number is the caller's call."""
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
    # Probed at the PHYSICAL TOPLEVEL, not at the registered subtree —
    # decided and documented here because the two are no longer the same
    # thing. `changed_files(repo)` now honestly reports only the logical
    # repo's own dirt, but the thing this function is about to do is
    # `git checkout`, which flips the ENTIRE shared checkout: uncommitted
    # work in a sibling logical repo (`backend/` while this is `frontend`)
    # would be carried onto the target branch or clobbered outright. So the
    # subtree scoping that protects `add -A` must NOT reach the safety
    # question, and the paths stay toplevel-relative so the refusal says
    # where the dirt actually is rather than hiding it as "somewhere else".
    # (`_in_progress_operation` is already whole-checkout — it reads git-dir
    # markers — which is the same answer for the same reason.)
    # The SUBJECT is scoped to match (`_dirt_subject`): listing toplevel-
    # relative paths under a `<checkout>/frontend` heading told the user to
    # go clean a file in a directory that does not contain it.
    top = toplevel(repo)
    dirty = changed_files(top)
    if dirty:
        shown = ", ".join(dirty[:5]) + ("..." if len(dirty) > 5 else "")
        raise GitError(
            f"{_dirt_subject(repo, top)} has {len(dirty)} uncommitted "
            f"change(s) ({shown}) — "
            "resolve, commit, or stash them yourself before continuing; "
            "never auto-discarded")
    behind = base_branch_behind(repo, target)
    current = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current == target:
        return {"switched": False, "branch": target, "behind": behind}
    run_git(repo, "checkout", target)
    return {"switched": True, "branch": target, "from_branch": current,
            "behind": behind}


# ------------------------------------------------------------- worktrees

def worktree_add(repo: Path, task_id: str, base_branch: str,
                 config_repos: dict | None = None) -> dict:
    """Per-task worktree with uid8 collision-avoidance (M5 charter). One retry
    with a fresh uid; a second failure raises, and the message says whether
    the documented direct-branch fallback is even available here — it is not
    for a subtree registration, whose checkout the fallback would flip
    wholesale (see the refusal at the bottom).

    Created from — and placed beside — the PHYSICAL toplevel, never beside
    the registered path. For a subtree logical repo `repo.parent` is INSIDE
    the checkout, so the old placement dropped the new worktree into the very
    tree it was branching from: a nested checkout sitting in `<repo>/..`,
    which the parent's own `git add`/status then have to reckon with. The
    returned `path` still points at the LOGICAL repo (`<worktree>/<prefix>`)
    because that is the directory the task's developer works in, and `root`
    carries the worktree root that `worktree_remove` needs. A root
    registration has an empty prefix, so `root == path` and both the layout
    and the naming are exactly what they were.

    The returned `path` is VERIFIED to exist before it is returned — see
    the refusal below; a path this function hands back is a directory the
    task can actually be run in.

    `config_repos` (the `repos.yaml` map) is message-only: it lets the
    failure path name the known direct-branch colliders — see
    `shares_toplevel`."""
    import uuid
    prefix = subtree_prefix(repo)
    # A root registration keeps `repo` VERBATIM rather than re-deriving it
    # through rev-parse: git's canonical spelling can differ from the
    # registered one (macOS `/var` -> `/private/var`), and silently relocating
    # every existing deployment's worktrees is not a subtree feature.
    top = toplevel(repo) if prefix else Path(repo)
    last_err = None
    for _ in range(2):
        uid = uuid.uuid4().hex[:8]
        branch = f"task/{task_id}-{uid}"
        root = top.parent / f"{top.name}-wt-{task_id}-{uid}"
        try:
            run_git(top, "worktree", "add", "-b", branch, str(root), base_branch)
        except GitError as exc:
            last_err = exc
            continue
        path = root / prefix if prefix else root
        if path.is_dir():
            return {"path": str(path), "root": str(root), "branch": branch}
        # `git worktree add` SUCCEEDS while materializing only what
        # `base_branch` tracks, so a subtree that does not exist on that
        # base leaves a perfectly healthy worktree with no `<prefix>` in it
        # (reproduced against a base predating the subtree's first commit:
        # returned `...-wt-T1-<uid>\frontend`, exists on disk False, verb
        # reported ok). Returning that path hands the developer a
        # `harness-repo` header pointing at nothing, `_run_tests(cwd=...)`
        # raises, and — worse — the CLI's resume gate
        # (`Path(recorded["path"]).is_dir()`) stays false forever, so every
        # retry adds ANOTHER worktree and ANOTHER `task/<id>-<uid>` branch
        # while `worktree_remove` only ever sees the newest record. Refuse
        # here, and take the tree we just made with us: the leak this lane
        # is documented never to produce must not start with our own call.
        run_git(top, "worktree", "remove", "--force", str(root), check=False)
        run_git(top, "branch", "-D", branch, check=False)
        run_git(top, "worktree", "prune", check=False)
        # The cleanup above is check=False best-effort (a locked file — AV
        # scanner, open handle — can defeat `worktree remove` on Windows,
        # and then `branch -D` fails on the still-checked-out branch), so
        # the message must not state the removal as fact it didn't verify.
        cleaned = ("removed again, no branch left behind"
                   if not root.exists() else
                   f"cleanup attempted but {root} is still on disk — "
                   "remove it and its task branch by hand")
        raise GitError(
            f"worktree for task {task_id} was created from '{base_branch}' "
            f"but the registered subtree '{prefix}' does not exist on that "
            "branch — nothing under it is tracked there, so the worktree "
            f"came up without it ({cleaned}). "
            f"Commit {repo} onto '{base_branch}' (or pass a base that "
            "carries it) and retry; never worked around silently.")
    # The direct-branch fallback cuts the task branch in the MAIN checkout,
    # and a checkout switch flips the WHOLE physical tree. Refused for ANY
    # subtree registration, not only where a sibling happens to be
    # registered too (adversarial-review finding, reproduced with only
    # `frontend` registered): `shares_toplevel` answers about the REGISTRY,
    # the hazard is about the CHECKOUT, so an empty list offered a fallback
    # that would still have flipped `backend/`, `infra/` and any uncommitted
    # human work in them. The list survives as the naming of KNOWN
    # colliders — a refusal that can say who else is in there is worth more
    # than one that can't. A root registration with no sharers is untouched:
    # same offer, same wording as before subtrees existed.
    shared = shares_toplevel(config_repos or {}, repo)
    if prefix or shared:
        colliders = (f" (also registered into it: {', '.join(shared)})"
                     if shared else "")
        fallback = (
            "the direct-branch fallback is NOT available here: this "
            f"registration shares the physical checkout {top}{colliders}, so "
            "cutting the task branch there would switch every other file in "
            "that checkout too — sibling logical repos and unregistered "
            "uncommitted work alike. Fix the repo state instead; never "
            "proceed silently")
    else:
        fallback = (
            "offer the direct-branch fallback (task branch in the main "
            "checkout, worktree: null) or fix the repo state; never proceed "
            "silently")
    raise GitError(
        f"worktree creation failed twice for task {task_id} ({last_err}) — "
        f"{fallback}")


def worktree_remove(repo: Path, worktree: dict) -> None:
    # `git worktree remove` takes the worktree ROOT; `path` is the logical
    # repo inside it, which for a subtree registration is a subdirectory git
    # rejects outright ("is not a working tree"). `.get`, not `[...]`: run
    # state written before subtree support carries only the old
    # `{path, branch}` shape, and a resumed run must still be able to sweep.
    run_git(repo, "worktree", "remove", "--force",
            worktree.get("root") or worktree["path"], check=False)
    run_git(repo, "branch", "-D", worktree["branch"], check=False)
    run_git(repo, "worktree", "prune", check=False)


def diff_paths(repo: Path, base: str) -> list[str]:
    """All paths the branch touches vs base (committed) plus working changes.
    `--relative` keeps the committed half inside the registered subtree, the
    same scope `changed_files` gives the working half — the contract/quick
    gates that consume this compare against repo-relative declarations."""
    committed = run_git(repo, "diff", "--name-only", "--relative",
                        f"{base}...HEAD")
    return sorted({*committed.splitlines(), *changed_files(repo)} - {""})


def diff_line_count(repo: Path, base: str) -> int:
    """Total added+removed lines across the same committed-plus-working
    scope `diff_paths` covers — quick_recheck's `quick_mode.loc_max` check
    (design.md piece 1: the size dimension of "quick", not just the
    disqualify-pattern dimension). Binary files show `-` for both counts in
    `--numstat`; skipped, not counted as a giant integer.

    `--relative` on both halves, so a subtree logical repo's "is this change
    small enough for quick mode" answer counts ITS lines — not a sibling
    logical repo's churn, which it neither owns nor can review."""
    committed = run_git(repo, "diff", "--numstat", "--relative",
                        f"{base}...HEAD")
    working = run_git(repo, "diff", "--numstat", "--relative")
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
