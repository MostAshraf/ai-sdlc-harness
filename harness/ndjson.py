"""Append-only NDJSON ledgers (design.md piece 2, RC3 smalls).

One JSON record per line. Every writer appends with a single O_APPEND write
(atomic for sane record sizes on POSIX); readers tolerate a torn final line —
a crash mid-append corrupts at most the tail, never the parse of prior records.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    # Microsecond precision: gate presentation and the human's reply may land
    # within the same second, and the RC4 record-selection rule is STRICTLY
    #-after (fail closed) — found by the M4 slice, fixed by resolution.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def append_record(path: Path, record: dict) -> dict:
    """Atomically append one record (adds `at` timestamp if absent).

    Self-heals a torn tail first (adversarial-review finding): a crash
    mid-append leaves a line with no trailing newline; appending straight
    onto it merges two records into one unparseable line — silently
    dropped while it's the tail, but the moment ANOTHER record follows,
    every `read_records` on the ledger raises forever (a gate-evidence
    ledger bricked by one crash). A lone `\\n` first makes the torn
    fragment its own line — still unparseable, but isolated, and only
    ever tolerated/skipped as corruption instead of corrupting neighbours."""
    record = {"at": now_iso(), **record}
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
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
    """Read all records.

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
        return []
    records: list[dict] = []
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
            continue  # torn/corrupt line — isolated, tolerated
    return records
