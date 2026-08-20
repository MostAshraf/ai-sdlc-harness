"""Run state: the single live authority (design.md piece 2).

`state.yaml` is loaded/saved ONLY through this module — inside an exclusive
file lock (`flock` on POSIX, an `msvcrt` region lock on Windows; RC4:
parallel `set-state` calls serialize; no lost updates) and sealed by the
integrity chain (RC4: out-of-band writes detected at next read). Contention
is WAITED OUT, not failed: see LOCK_WAIT_BUDGET, whose expiry is the one
case that refuses.

Bootstrap is the FSM's declared from-nothing transition; it REFUSES when a
live run already exists for the work item (coverage B5) — the caller then
offers Resume or Abort, never a silent clobber.
"""
from __future__ import annotations

import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

# How long a lock acquisition may WAIT before giving up. Windows-only in
# effect (POSIX `flock` blocks indefinitely and is left exactly as it was),
# but declared unconditionally so the retry helper below — and the tests
# that inject a tiny budget into it — exist on every platform.
#
# 120s, not the ~10s `msvcrt.LK_LOCK` used to enforce: the critical sections
# this lock protects are NO LONGER all sub-second. `merge-task` now holds it
# across the merge itself (round 3), and a squash on a large repo with a cold
# cache is git, not YAML — measured at 15.6s on a 20k-file checkout, where
# EVERY other run-scoped verb died at ~9.4s with a raw
# `OSError: [Errno 36] Resource deadlock avoided` (adversarial review of that
# change, measured: a `show` issued during a merge, not a pathological state
# at all). A verb must WAIT OUT a sibling lane's merge; the budget exists only
# so a genuinely abandoned lock still ends in a named refusal rather than a
# hang.
LOCK_WAIT_BUDGET = 120.0
# Poll backoff: fast enough that a sub-second holder costs nothing, capped so
# a long merge doesn't spin. Doubling from 10ms to 250ms.
_LOCK_POLL_MIN = 0.01
_LOCK_POLL_MAX = 0.25
# Cross-PROCESS injection point: monkeypatching the constant cannot reach a
# CLI invocation in a subprocess, which is the shape any end-to-end probe of
# the budget path would take. Never set in production; a garbage value falls
# back to the declared default rather than failing the verb that reads it.
_BUDGET_ENV = "HARNESS_LOCK_WAIT_BUDGET"


def _lock_wait_budget() -> float:
    raw = os.environ.get(_BUDGET_ENV)
    if raw:
        try:
            value = float(raw)
        except ValueError:
            pass                       # garbage env → the declared default
        else:
            # `float("inf")`/`float("nan")` parse fine and are the two values
            # that would defeat the budget's whole purpose: inf never expires
            # (the hang the StateError exists to replace) and nan makes every
            # `>=` deadline comparison False, which is the same hang spelled
            # differently. Non-finite is garbage too.
            if math.isfinite(value):
                return value
    return LOCK_WAIT_BUDGET


def _wait_for_lock(attempt, path) -> None:
    """Retry a NON-BLOCKING lock `attempt` until it succeeds or the budget
    runs out — the shape Windows needs and POSIX gets for free.

    The budget expiring is a StateError, never a bare OSError: the raw
    `Resource deadlock avoided` the platform raises names neither the lock,
    nor who is likely holding it, nor what to do — and it reached the CLI's
    JSON error contract verbatim, where a run's transcript recorded a
    concurrency wait as an unexplained system fault."""
    budget = _lock_wait_budget()
    deadline = time.monotonic() + budget
    delay = _LOCK_POLL_MIN
    while True:
        try:
            attempt()
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise StateError(
                    f"gave up after {budget:g}s waiting for the run lock "
                    f"({path}) — another harness verb still holds this run's "
                    "lock, typically a merge (`merge-task` holds it across "
                    "the squash, the longest git operation any verb runs). "
                    "Nothing here read or wrote state: wait for that verb's "
                    "completion, then retry the identical command. Never "
                    "stash, hand-commit, or delete the lock file to get "
                    "past this.") from exc
            time.sleep(delay)
            delay = min(delay * 2, _LOCK_POLL_MAX)


try:
    import fcntl

    def _lock_exclusive(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _lock_shared(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_SH)

    def _unlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
except ImportError:  # Windows — no fcntl; msvcrt region locks (still stdlib)
    import msvcrt

    def _lock_exclusive(fh) -> None:
        # LK_NBLCK in a retry loop, NOT LK_LOCK: LK_LOCK's own retry is
        # ~10s and its expiry is an untranslatable OSError, which made a
        # long merge fatal to every concurrent verb (see LOCK_WAIT_BUDGET).
        # The loop waits far longer and ends in a StateError that names the
        # lock and the remedy.
        def _attempt() -> None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

        _wait_for_lock(_attempt, getattr(fh, "name", "<lock>"))

    # msvcrt has no shared locks: readers serialize behind writers AND each
    # other — correctness-preserving (never a torn read), just less
    # parallel than POSIX's LOCK_SH.
    _lock_shared = _lock_exclusive

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

import yaml

from . import chain
from .ndjson import now_iso

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_id(raw: str) -> str:
    """Character-preserving sanitize for path segments (design.md piece 4).
    Idempotent; no dash-collapsing; raises on empty. Lives here (not
    workflow.py) so bootstrap's own collision check can reuse it without a
    circular import — run-directory naming is this module's domain."""
    if not raw or not raw.strip():
        raise ValueError("empty work-item id")
    return SAFE_RE.sub("-", raw.strip())


def validate_task_id(raw: str) -> str:
    """Task ids flow UNSANITIZED into git branch names (`task/{id}-{uid}`),
    worktree directory names, and red-proof file names — so unlike work-item
    ids (sanitized copies), they are REFUSED outright when unsafe
    (adversarial-review finding: an id with a space or `..` failed later,
    deep inside git, with two confusing errors instead of one clear one at
    registration time)."""
    if not raw or SAFE_RE.search(raw):
        raise StateError(
            f"task id {raw!r} is not usable: ids appear in git branch/"
            "worktree/proof-file names, so only [A-Za-z0-9._-] is allowed")
    return raw


class StateError(Exception):
    pass


class CollisionError(StateError):
    """A live run for this work item already exists (coverage B5)."""


def run_dir(workspace: Path, safe_id: str, date: str) -> Path:
    return workspace / "ai" / f"{date}-{safe_id}"


def state_path(run: Path) -> Path:
    return run / "state.yaml"


def _dump(state: dict) -> bytes:
    return yaml.safe_dump(state, sort_keys=False, allow_unicode=True).encode("utf-8")


@contextmanager
def locked_file(lock_path: Path):
    """Generic exclusive file lock — the same flock/msvcrt primitive the run
    lock uses, for non-run critical sections (config read-merge-write in
    initws.write_section; adversarial-review finding: two concurrent
    `init-section --section overrides` calls raced the read-merge-write
    and one lost its update — atomic replace protects readers, not
    writers)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        _lock_exclusive(fh)
        try:
            yield
        finally:
            _unlock(fh)


@contextmanager
def locked(run: Path):
    """Exclusive run lock. Test execution belongs OUTSIDE this section (RC4);
    only the read-validate-write critical section holds it."""
    run.mkdir(parents=True, exist_ok=True)
    with locked_file(run / ".state.lock"):
        yield


@contextmanager
def locked_read(run: Path):
    """Shared-lock context for read-only callers (`harness show`/`verify`/
    `status`) — serializes against an exclusive writer without `locked()`'s
    unconditional `mkdir` (adversarial-review round 1 finding: that mkdir
    running before `load()` got a chance to refuse a nonexistent run left a
    stray directory on a typo'd `--run`).

    Never mkdirs: if there's no run (no `state.yaml`), there's nothing to
    lock against — `load()` raises its own clean `StateError` immediately.
    If a run exists, `chain.seal()`'s content-then-seal write is two
    SEPARATE atomic replaces, not one transaction (adversarial-review round
    2 finding: an earlier version of this function skipped locking
    entirely, on the mistaken assumption that atomic-replace alone made a
    bare read safe — it doesn't; a reader landing between the two replaces
    sees new content paired with the old seal and raises a spurious
    IntegrityError, indistinguishable from real tampering). A shared lock
    (LOCK_SH) blocks only against a concurrent exclusive writer — multiple
    readers still proceed concurrently — while adding no mkdir side effect."""
    if not state_path(run).exists():
        yield
        return
    lock_file = run / ".state.lock"
    with lock_file.open("a+") as fh:  # "a+": creates if somehow missing, never truncates
        _lock_shared(fh)
        try:
            yield
        finally:
            _unlock(fh)


def load(run: Path, workspace: Path) -> dict:
    path = state_path(run)
    if not path.exists():
        raise StateError(f"no run at {path} — bootstrap first")
    if (run / ".mirror").exists():
        # The published snapshot inside a repo looks exactly like a run dir
        # but carries no seals (they're mirror-excluded) — reading it used
        # to raise "no integrity seal", indistinguishable from tampering
        # (dogfood A2 finding: a relative --run resolved from the repo's
        # cwd instead of the workspace). Name the actual problem.
        raise StateError(
            f"{run} is a published MIRROR snapshot, not the live run — "
            "point --run at the workspace's own ai/<run> directory "
            "(a relative --run resolves against the current directory)")
    key = chain.load_key(workspace)
    return yaml.safe_load(chain.verify(path, key))


def save(run: Path, workspace: Path, state: dict) -> None:
    # strict load: a save with a freshly-minted wrong-workspace key would
    # RE-SEAL real state against garbage — the one shape worse than the
    # phantom mismatch (bootstrap, the legitimate creation moment, mints
    # the key itself before its first save)
    key = chain.load_key(workspace)
    chain.seal(state_path(run), _dump(state), key)


def _terminal(state: dict, manifest: dict) -> bool:
    if state.get("aborted") or state.get("completed"):
        # `harness abort` / `harness complete` are declared terminal
        # outcomes: the run keeps its audit trail but releases its
        # work-item slot (B5 collision check) and contributes no spawn
        # legality. Before abort existed a parked run was live FOREVER —
        # no verb could end it, so its work item could never be
        # re-bootstrapped (adversarial-review finding).
        return True
    # legacy grace: runs finished by a pre-`complete` harness carry no
    # completed marker — cursor sitting on the mode's last step keeps
    # releasing their slot (imprecise: a run still mid-final-step also
    # matches; `complete` is the exact form)
    seq = (manifest.get("modes") or {}).get(state.get("mode")) or []
    return bool(seq) and state.get("cursor", {}).get("current_step") == seq[-1]


def _live_sibling_run(workspace: Path, item_id: str, exclude: Path,
                      manifest: dict) -> Path | None:
    """Any OTHER non-terminal run for the SAME work item, under any date
    (coverage B5, adversarial-review finding: the original check compared
    only the exact `ai/<today>-<id>/` path, so parking a run Monday and
    resuming Tuesday bootstrapped a silent second run under the new date
    instead of refusing). Run-dir names are always `{date}-{safe_id}` with
    an ISO `date` (fixed 10 chars) — sliced, not glob-matched, so a work
    item id that happens to be a SUFFIX of another's (e.g. '1' vs 'TEST-1')
    can't false-match."""
    if not (workspace / "ai").is_dir():
        return None
    target = safe_id(item_id)
    key = chain.load_or_create_key(workspace)
    for candidate in sorted((workspace / "ai").iterdir()):
        tail = candidate.name[11:]
        # exact name, or a same-day slot suffix (`-<n>`, next_run_slot) —
        # the fixed-width slice stays the pre-filter so an unreadable
        # UNRELATED run can't fail-close this item's bootstrap
        if candidate == exclude or not (
                tail == target
                or re.fullmatch(re.escape(target) + r"-\d+", tail)):
            continue
        state_file = state_path(candidate)
        if not state_file.exists():
            continue
        try:
            st = yaml.safe_load(chain.verify(state_file, key))
        except chain.IntegrityError:
            # Fail CLOSED, not skip (adversarial-review round 2 finding):
            # an unreadable sibling might be genuinely live work mid-crash
            # (this module's own documented threat-model ambiguity — it
            # can't tell "crashed" from "tampered" apart), so it must block
            # a fresh bootstrap the same way a confirmed-live sibling
            # would, rather than silently ceding the slot to a second run.
            # `guard_spawn`'s per-run IntegrityError catch is a DIFFERENT
            # seam (which run may spawn subagents right now) — it doesn't
            # cover this one (should a NEW run even start). The human
            # resolves via `harness reseal` first, then retries.
            return candidate
        # the suffix grammar can overlap a DIFFERENT item whose own id
        # literally ends in `-<n>` (item 'X-1' slot 2 names the same dir as
        # item 'X-1-2' exact) — the sealed state's id is the tiebreaker;
        # unreadable states already failed closed above, ambiguity included
        sid = str((st.get("work_item") or {}).get("id") or "")
        if not sid.strip() or safe_id(sid) != target:
            continue
        if not _terminal(st, manifest):
            return candidate
    return None


_LEGACY_TRACKERS = ("tracker.md", "tracker.archived.md", "tracker.aborted.md")


def _legacy_run_dir(run: Path) -> bool:
    """A v2.x run dir: tracker file(s), never a state.yaml. Never vacant."""
    return (not state_path(run).exists()
            and any((run / name).is_file() for name in _LEGACY_TRACKERS))


def next_run_slot(base: Path, workspace: Path, manifest: dict) -> Path:
    """Where fetch should bootstrap when the deterministic `<date>-<id>`
    dir is already taken. Field (validation session D, phase 0): abort
    E2E-4 and re-fetch it the SAME DAY — bootstrap's exact-path collision
    check was existence-only and terminal-blind, so abort's documented
    slot release held for every date EXCEPT today's, and the refusal even
    called the aborted occupant "a live run". A TERMINAL occupant
    (aborted/completed) shifts the new run to `<date>-<id>-2`, `-3`, …; a
    live or unreadable (fail-closed) occupant keeps the base path so
    bootstrap's collision refusal fires exactly as before. Keyed on
    state.yaml, not the dir: a dir holding only work-item.json is
    crash-retry residue bootstrap already resumes into — but a dir holding
    v2.x tracker files is a migrated-workspace ARCHIVE, occupied-terminal
    (adversarial-review finding: state-keyed vacancy saw it as resumable
    residue, so a same-day re-fetch of a migrated item bootstrapped INTO
    its archive, and publish-mirror — which excludes only run-authority
    files — would commit the legacy tracker.md onto the PR branch)."""
    key = chain.load_or_create_key(workspace)
    candidate, n = base, 1
    while True:
        if _legacy_run_dir(candidate):
            n += 1
            candidate = base.parent / f"{base.name}-{n}"
            continue
        if not state_path(candidate).exists():
            return candidate
        try:
            st = yaml.safe_load(chain.verify(state_path(candidate), key))
        except chain.IntegrityError:
            return candidate           # unreadable → collide there, loudly
        if not _terminal(st, manifest):
            return candidate           # genuinely live → collide
        n += 1
        candidate = base.parent / f"{base.name}-{n}"


def bootstrap(run: Path, workspace: Path, *, work_item: dict, mode: str,
              change_type: str, tasks: list[dict], entry_step: str,
              manifest: dict | None = None,
              artifacts: dict | None = None) -> dict:
    """The declared from-nothing transition (RC2). Refuses collision (B5):
    the exact run dir, AND — when `manifest` is given — any other live
    (non-terminal) run for the same work item under a different date.

    `artifacts` seeds the entry step's own declared outputs — the ones that
    are known at bootstrap because bootstrap is what determines them (today:
    `repo-ambiguity`). They must appear here rather than via a later
    `set_artifact` call because a `when` predicate on the very next step
    reads them, and eval_predicate raises on a missing artifact rather than
    treating it as false."""
    # THE one legitimate key-creation moment: every other load/save path is
    # strict (chain.load_key), so a drifted-cwd workspace can never mint a
    # stray key and phantom-fail integrity
    key = chain.load_or_create_key(workspace)
    path = state_path(run)
    # Item-level lock around the sibling-check-and-save (adversarial-review
    # finding: `locked(run)` is a PER-RUN lock, so two concurrent bootstraps
    # of the same work item under DIFFERENT dates take different locks,
    # nothing serializes them, and both pass the "no live sibling" check —
    # reproduced 150/150, breaking the B5 one-live-run invariant abort's
    # slot-release depends on). This workspace-level, item-keyed lock
    # serializes them; same-date bootstraps are additionally protected by
    # the per-run lock below.)
    item_lock = workspace / "ai" / f".bootstrap-{safe_id(work_item['id'])}.lock"
    with locked_file(item_lock), locked(run):
        if path.exists():
            # honest occupant naming (field session D: the old message
            # called an ABORTED occupant "a live run", sending the
            # diagnosis toward abort's slot release instead of this
            # existence-only path check)
            why = (f"a live run already exists at {run} — offer Resume or "
                   "Abort; refusing to clobber (non-interactive callers "
                   "must abort)")
            if manifest is not None:
                try:
                    prev = yaml.safe_load(chain.verify(path, key))
                    if _terminal(prev, manifest):
                        why = (f"a terminal (aborted/completed) run already "
                               f"occupies {run} — fetch allocates a fresh "
                               "same-day slot itself (next_run_slot); a "
                               "direct bootstrap caller must pick a new "
                               "run path")
                except chain.IntegrityError:
                    pass
            raise CollisionError(why)
        if manifest is not None:
            sibling = _live_sibling_run(workspace, work_item["id"], run, manifest)
            if sibling is not None:
                raise CollisionError(
                    f"a live run already exists for work item '{work_item['id']}' "
                    f"at {sibling} — offer Resume or Abort; refusing to bootstrap "
                    "a second run for the same item (non-interactive callers must abort)"
                )
        for t in tasks:
            validate_task_id(t["id"])
        state = {
            "work_item": work_item,
            "mode": mode,
            "change_type": change_type,
            "cursor": {"current_step": entry_step, "completed_steps": []},
            "gates": {},
            "tasks": [
                {"id": t["id"], "repo": t.get("repo", "."), "status": "pending",
                 "depends_on": t.get("depends_on", []), "risk": t.get("risk", "low"),
                 "commit_sha": None, "review_rounds": 0, "stalls": 0,
                 "worktree": None,
                 # A seeded placeholder task (fetch's positional default) carries
                 # this flag so inspectors of state.yaml see it isn't a ratified
                 # plan; plan-register rebuilds tasks fresh and drops it.
                 **({"provisional": True} if t.get("provisional") else {})}
                for t in tasks
            ],
            "contracts": [],
            "artifacts": dict(artifacts or {}),
            "metrics": {entry_step: {"started_at": now_iso(), "ended_at": None}},
        }
        save(run, workspace, state)
    return state
