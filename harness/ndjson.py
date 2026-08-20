"""Append-only NDJSON ledgers (design.md piece 2, RC3 smalls).

One JSON record per line. Every append runs inside a short-held LEDGER lock
(below) and writes with O_APPEND; readers tolerate a torn final line — a crash
mid-append corrupts at most the tail, never the parse of prior records.

The lock is not belt-and-braces. The original design leaned on O_APPEND alone,
noting it was "atomic for sane record sizes on POSIX" — and that POSIX-only
note stayed the whole story long after these ledgers stopped being telemetry.
On Windows, Python's O_APPEND is the CRT's seek-then-write, which is NOT
atomic, and `append_record` additionally RE-OPENS the file to read back its
tail (torn-tail healing, and the `at` ordering floor) and can issue two
`os.write` calls. The whole-system review measured the result: 6 processes
x 120 appends left 376 of 720 records;
2 barrier-synced processes x 200 left 293 of 400 — 107 records
LOST, none torn, i.e. silently gone rather than visibly damaged. The run lock
gives zero protection (40 appends all landed while another process held it),
because hooks deliberately never take it.

What made that load-bearing: events.ndjson / reviews.ndjson now carry the
verdicts the FSM gates read, written by one-shot hook processes that share no
state but these files — and develop.md MANDATES batching a step's spawns into
ONE message, which is exactly what makes their PostToolUse hooks concurrent. A
lost `spawn-pending` means the agent's SubagentStop finds no pending, captures
nothing, and says nothing on stderr (the repo's own
`test_subagent_stop_without_a_pending_captures_no_verdict` documents that
silence): a real APPROVED vanishes. A lost `spawn-captured` wedges the
(task, mode) key forever.

Reads are deliberately UNLOCKED: a reader racing an append sees at worst a
torn tail, which every reader here already tolerates by construction, and
serializing readers behind writers would put the guards' ledger reads on the
critical path of every hook.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ONE sidecar per ledger DIRECTORY, never the ledger itself: taking a region
# lock on the same file you also append to invites platform-specific surprises
# (and on Windows would interact with the O_APPEND handle this function
# opens). Per-directory rather than per-file is a MEASURED trade, not the free
# one this comment used to claim ("an append is microseconds"): an append's
# own p50 is ~5.7ms on this platform, and at 9 writers x 60 appends the one
# shared sidecar took 3.02s against 1.03s for three separate ones — i.e. the
# shared lock fully serializes work three ledgers could have done in parallel
# (whole-system review, round 4, executed). Kept anyway: two seconds of
# wall-clock across a whole step, spent while the run is already waiting on
# subagents, buys one lock file per run instead of four, and no correctness
# claim here rests on the sharing. If a run's ledgers ever get hot enough for
# that to matter, splitting the sidecar per file is a safe change.
LEDGER_LOCK_NAME = ".ledger.lock"
# Both platforms poll to a deadline (Windows because msvcrt has no
# blocking-with-timeout mode; POSIX because a BLOCKING flock has no deadline
# at all — see below). Short by design: this lock only ever spans one append,
# so at ~5.7ms each a wait this long already means a crashed or hung holder —
# at which point appending unlocked (below) is the right answer, not blocking
# a capture hook.
_LEDGER_LOCK_BUDGET = 20.0
_LEDGER_POLL = 0.002

try:
    import fcntl

    def _take_ledger_lock(fh) -> None:
        # LOCK_NB + poll, NOT a bare blocking LOCK_EX. `ledger_lock`'s
        # fail-open promise names "a crashed holder outliving the budget" —
        # and a blocking flock has no budget to outlive, so on POSIX that
        # sentence was simply false: a holder that hangs rather than dies (a
        # SIGSTOPped hook, a vanished NFS server) blocked a capture hook
        # FOREVER, with Windows already having the 20s escape (whole-system
        # review, round 4). Same budget and poll as the Windows branch, so
        # both platforms now fail open at the same point.
        deadline = time.monotonic() + _LEDGER_LOCK_BUDGET
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LEDGER_POLL)

    def _drop_ledger_lock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
except ImportError:  # Windows — no fcntl; msvcrt region locks (still stdlib)
    import msvcrt

    def _take_ledger_lock(fh) -> None:
        deadline = time.monotonic() + _LEDGER_LOCK_BUDGET
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_LEDGER_POLL)

    def _drop_ledger_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


_FAIL_OPEN_REPORTED = False


@contextmanager
def ledger_lock(path: Path):
    """Exclusive lock over appends to every ledger in `path`'s directory.

    LOCK ORDERING RULE, and it is not optional: this lock is ALWAYS INNERMOST.
    Nothing may hold it while acquiring the run state lock. The engine's
    long critical sections run the other way round — `merge-task` holds the
    state lock across the whole squash and appends a ledger row inside it —
    so a code path that took the ledger lock first and then reached for state
    would deadlock that merge against any hook mid-append. Nothing here reads
    or writes state, which is what keeps the rule true by construction; keep
    it that way, and never call into `harness.state` from inside this block.

    The corollary is the property round 3 established and this must not cost:
    hooks take NO state lock, so a SubagentStop capture still completes in
    ~0.15s while a 15s merge holds the run lock. That property still HOLDS —
    round-4 review measured captures completing in 8-84ms across a 15.6s
    merge — but not for the reason this note used to give ("this lock is
    microseconds"). An append's p50 is ~5.7ms. It holds because this lock is
    held for exactly one append and contended only by other appends, so the
    worst case is a handful of them queued, two orders of magnitude inside
    the budget the property needs.

    FAIL-OPEN, deliberately: if the lock cannot be taken at all (a read-only
    or exotic filesystem, a permissions quirk, a crashed holder outliving the
    budget — a real budget on both platforms now), the append still happens —
    unlocked, with one stderr line. A dropped record is the failure this lock
    exists to prevent; refusing to write one because the lock file misbehaved
    would be the same loss with better paperwork."""
    global _FAIL_OPEN_REPORTED
    lock_path = path.parent / LEDGER_LOCK_NAME
    fh = None
    try:
        # "a+", never "w": a truncating open would race a concurrent holder's
        # own open, and there is nothing in the file to truncate anyway.
        fh = lock_path.open("a+")
        _take_ledger_lock(fh)
    except OSError as exc:
        if fh is not None:
            fh.close()
        # ONCE per process, like guards.main's other one-shot degradations.
        # A degradation this broad is a property of the process, not of the
        # append, and repeating it is actively harmful here: executed in
        # round-4 review, five appends in one hook printed five identical
        # lines, and through a real blocking guard the whole pile arrived
        # PREPENDED to the block reason — the text the model reads as its
        # instruction — burying the refusal it was supposed to act on.
        if not _FAIL_OPEN_REPORTED:
            _FAIL_OPEN_REPORTED = True
            print(f"ai-sdlc-harness: could not take the ledger lock at "
                  f"{lock_path} ({type(exc).__name__}: {exc}) — appending to "
                  f"{path.name} WITHOUT it, and to any further ledger in this "
                  "process without saying so again. A concurrent append may "
                  "be lost (this platform's O_APPEND is not atomic).",
                  file=sys.stderr)
        yield
        return
    try:
        yield
    finally:
        try:
            _drop_ledger_lock(fh)
        finally:
            fh.close()


_now_lock = threading.Lock()
_last_now: datetime | None = None
# One read of the ledger's tail per append (see `_tail_probe`). Generous
# rather than tight: the LARGEST record any call site writes is a few hundred
# bytes (a reviews row is ~150 — verdicts are enums, replies are never
# ledgered; round-5 re-verification measured ~400x headroom), so a window
# that misses the last complete line is unreachable in practice; if a future
# writer ever ledgers a blob past 64 KiB, the miss only costs the
# cross-process clamp below, silently. Reading 64 KiB out of the page cache
# is noise against the ~5.7ms the append itself takes.
_TAIL_WINDOW = 65536


def _parse_at(raw) -> datetime | None:
    """A ledger `at` back into a datetime, or None if it is not one.

    Total by construction: this feeds an ORDERING FLOOR, and a record whose
    `at` was hand-written (`log-event` is unvalidated) or written by some
    future format must degrade to "no floor", never raise inside an append."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def now_iso(after: str = "") -> str:
    # Microsecond precision: gate presentation and the human's reply may land
    # within the same second, and the RC4 record-selection rule is STRICTLY
    #-after (fail closed) — found by the M4 slice, fixed by resolution. That
    # rule further assumes two calls close together never read the SAME OS
    # clock value; a courser clock grain than the microsecond precision we
    # format (observed on a Windows Python 3.10 CI image) can violate that,
    # silently failing the ">" comparison. Clamp to strictly-increasing
    # ourselves rather than trust the OS clock's actual resolution.
    #
    # `after` is the CROSS-process half of that clamp, and the per-process
    # `_last_now` cannot stand in for it — it is exactly what produced the
    # residual round-4 review measured: writer A, having already stamped this
    # tick, is pushed to T+1us, while writer B in another process enters the
    # same tick fresh, stamps raw T, and lands PHYSICALLY LATER. Six
    # inversions, every one of them exactly 1us, which is also why
    # `_verdict_window`'s tie-break could not absorb them (it engages on
    # EQUALITY). `append_record` passes the ledger's own tail here, which is
    # the one floor every writer of that file shares.
    #
    # The two clamps stay SEPARATE, and the order below is the whole reason:
    # `_last_now` records this PROCESS's own clock and must never absorb a
    # per-ledger floor. One ledger carrying a far-future `at` — a caller-
    # supplied one (a test tie-break, a migrated row), a clock-skewed peer, a
    # hand-written `log-event` — would otherwise drag every later timestamp
    # this process mints into that future: other ledgers, `in_review_at`,
    # gate `decided_at`, the lot. Confined this way a poisoned tail costs
    # ordering on its OWN file and nothing else, and that file still comes
    # out non-decreasing, because every append re-reads the same tail.
    global _last_now
    with _now_lock:
        now = datetime.now(timezone.utc)
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        floor = _parse_at(after)
        if floor is not None and now <= floor:
            now = floor + timedelta(microseconds=1)
        return now.isoformat(timespec="microseconds")


def _tail_probe(path: Path) -> tuple[bool, str]:
    """(does the file end in a newline, what `at` its last record carries).

    ONE read of the tail answering both halves, because both are wanted at
    the same instant — inside the append lock, just before the write.
    Ledger-order-preserving by construction: the caller holds the lock, so
    nothing can append between this read and that write.

    Missing file, unreadable file, a window holding no complete line, a tail
    line that is torn: all degrade to ("ends cleanly", "no floor"). Both
    degradations are the pre-round-4 behaviour exactly — heal nothing, clamp
    per-process only — so a probe that cannot answer never makes an append
    worse than it was, and never blocks one."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return True, ""
            fh.seek(max(0, size - _TAIL_WINDOW))
            blob = fh.read()
    except OSError:
        return True, ""
    text = blob.decode("utf-8", errors="replace")
    # Reversed, and stops at the FIRST record that parses: normally that is
    # the last line and the loop runs once. A torn tail, or the partial first
    # line a windowed read can start mid-record on, simply fails json and is
    # stepped over — the same tolerance `read_records` extends.
    for line in reversed(text.split("\n")):
        if not line.strip():
            continue
        try:
            at = json.loads(line).get("at")
        except (json.JSONDecodeError, AttributeError):
            continue
        return text.endswith("\n"), (at if isinstance(at, str) else "")
    return text.endswith("\n"), ""


def append_record(path: Path, record: dict) -> dict:
    """Atomically append one record (adds `at` timestamp if absent).

    Self-heals a torn tail first (adversarial-review finding): a crash
    mid-append leaves a line with no trailing newline; appending straight
    onto it merges two records into one unparseable line — silently
    dropped while it's the tail, but the moment ANOTHER record follows,
    every `read_records` on the ledger raises forever (a gate-evidence
    ledger bricked by one crash). A lone `\\n` first makes the torn
    fragment its own line — still unparseable, but isolated, and only
    ever tolerated/skipped as corruption instead of corrupting neighbours.

    THE chokepoint for the ledger lock (see `ledger_lock`): every writer in
    this codebase — the hooks' event/review/token appends, the CLI's nine
    events.ndjson appends, gitops, initws — goes through this one function, so
    the lock is applied here rather than at each of them. A future writer that
    bypasses it inherits the lost-record failure the whole-system review
    measured, which is reason enough not to add one.

    THE ORDERING GUARANTEE, and it is load-bearing: `at` is non-decreasing in
    FILE ORDER, on one ledger, across processes. Several readers pick by
    max(`at`) over records they then treat as the latest state
    (`transitions`' verdict and red-proof selection, `workflow`'s latest-wins
    pairing), so a record stamped older than a row it physically follows
    hands them the wrong one. Two mechanisms hold it, and round-4 review
    found the property real, unpinned, and only half-implemented:

      * mint INSIDE the lock, never before reaching for it. Mutation M6
        (mint outside) survived all 1234 tests while producing 180
        inversions across 8 processes x 120 appends, against 0 unmutated —
        hence the concurrency test now asserts the property directly.
      * clamp to strictly after the tail record's own `at` (`_tail_probe`,
        the same read that heals a torn tail). The in-lock mint alone still
        left 6 inversions of exactly 1us, because `now_iso`'s clamp is
        per-PROCESS: it pushes an already-stamped writer to T+1us while a
        writer in another process enters the same tick fresh at raw T. The
        ledger's tail is the floor both of them share."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        ends_newline, tail_at = _tail_probe(path)
        record = {"at": now_iso(after=tail_at), **record}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if not ends_newline:
                os.write(fd, b"\n")
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    return record


class LedgerCorruption(ValueError):
    """A non-blank line of a ledger is unparseable, seen by a `strict=True`
    reader — a trust-anchor consumer that must fail closed rather than
    silently skip (which could promote an older, more-permissive record)."""


def read_records(path: Path, strict: bool = False) -> list[dict]:
    """Read all records (see `read_records_counting` for the parse rules —
    this is the same read with the skipped-line count dropped)."""
    return read_records_counting(path, strict)[0]


def read_records_counting(path: Path,
                          strict: bool = False) -> tuple[list[dict], int]:
    """Read all records, plus HOW MANY non-blank lines failed to parse.

    The count exists because a lenient read is silent by construction, and
    for one class of consumer that silence is a disabled guard rather than a
    missing row: the readers of the `spawn-pending` pairing
    (`guards._live_spawn_for`, `transitions.open_spawn_pendings`,
    `capture_subagent_stop`, the flagged gauge — all four now through the one
    declared rule in `transitions.open_pendings`) decide "no spawn is
    in flight" from ABSENCE, so a torn pending line silently turns the
    one-live-spawn refusal, the stall refusal and the late-reply drop into
    no-ops (adversarial review). They keep reading leniently — failing
    closed on a torn line would brick the wedged-run path those guards exist
    to protect — but they now SAY the ledger was partly unreadable.

    `strict=False` (default): skip unparseable lines. A crash-torn fragment
    is a known benign shape (append_record isolates it on its own line), and
    raising on it forever bricked every later read of the ledger over one
    crash (adversarial-review finding). Right for ABSENCE-based consumers
    (status, metrics, "does any qualifying record exist"): a dropped line is
    only ever missing, never a wrong value.

    `strict=True`: a non-blank unparseable line raises `LedgerCorruption`.
    Right for LATEST-WINS trust anchors (gate decisions, reviewer verdicts),
    where silently dropping a torn NEWEST record would promote an older,
    more-permissive one (adversarial-review finding: a torn newest
    CHANGES_REQUESTED let an earlier APPROVED complete a rejected task). The
    caller fails closed; the human/reviewer re-acts, which self-heals the
    torn line on the next append.

    Split on `\\n` (the writer's sole delimiter), NOT str.splitlines()
    (adversarial-review finding: splitlines also breaks on U+2028/U+2029/
    U+0085, which `json.dumps(ensure_ascii=False)` emits literally — a valid
    record containing one, common in pasted text, split into two fragments
    and vanished whole)."""
    if not path.exists():
        return [], 0
    records: list[dict] = []
    skipped = 0
    for line in path.read_bytes().decode("utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if strict:
                raise LedgerCorruption(
                    f"{path}: unparseable ledger line — refusing to derive a "
                    "decision from a ledger with a corrupt record (fail "
                    "closed; re-submit to heal it)")
            skipped += 1
            continue  # torn/corrupt line — isolated, tolerated
    return records, skipped
