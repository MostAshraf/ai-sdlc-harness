"""M1 done-criterion: concurrent `set-state` calls do not lose updates.

Eight subprocesses race distinct task transitions against ONE state.yaml
(rewrite-in-full). Without the flock, later writers clobber earlier ones and
some tasks stay pending; with it, every transition lands. This exercises the
real CLI end-to-end, exactly as parallel multi-repo develop lanes would.

…and the same question one layer down, for the LEDGERS: concurrent hook
processes appending to one events.ndjson (ConcurrentLedgerAppends below).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import stat
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from harness import ndjson, state as state_mod
from tests import support

ROOT = Path(__file__).resolve().parent.parent
N_TASKS = 8

# One writer process: wait on the barrier, then hammer the ledger through the
# REAL `append_record` — the same entry point every hook and CLI verb uses.
_APPEND_WRITER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from harness import ndjson
ledger, go = Path(sys.argv[2]), Path(sys.argv[3])
tag, count = sys.argv[4], int(sys.argv[5])
while not go.exists():
    time.sleep(0.005)
for i in range(count):
    ndjson.append_record(ledger, {"tag": tag, "i": i})
"""


class ConcurrentSetState(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.run = self.workspace / "ai" / "2026-01-01-RACE-1"

    def tearDown(self):
        support.rmtree(self.workspace)

    def _cli(self, *args) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "harness",
             "--workspace", str(self.workspace), "--run", str(self.run), *args],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def test_no_lost_updates_under_parallel_writers(self):
        tasks = [f"T{i}" for i in range(1, N_TASKS + 1)]
        boot = self._cli("bootstrap", "--work-item-id", "RACE-1", "--title", "r",
                         "--mode", "quick", "--change-type", "fix",
                         *[a for t in tasks for a in ("--task", t)])
        out, err = boot.communicate(timeout=60)
        self.assertEqual(boot.returncode, 0, err)

        procs = [self._cli("task", "--id", t, "--to", "in-progress") for t in tasks]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, f"stdout={out} stderr={err}")

        show = self._cli("show")
        out, _ = show.communicate(timeout=30)
        state = json.loads(out)["state"]
        statuses = {t["id"]: t["status"] for t in state["tasks"]}
        self.assertEqual(set(statuses.values()), {"in-progress"},
                         f"lost update detected: {statuses}")

    def test_collision_refused_via_cli(self):
        boot = self._cli("bootstrap", "--work-item-id", "RACE-1", "--title", "r",
                         "--mode", "quick", "--change-type", "fix")
        boot.communicate(timeout=60); self.assertEqual(boot.returncode, 0)
        again = self._cli("bootstrap", "--work-item-id", "RACE-1", "--title", "r",
                          "--mode", "quick", "--change-type", "fix")
        out, _ = again.communicate(timeout=30)
        self.assertEqual(again.returncode, 1)
        self.assertIn("Resume or Abort", json.loads(out)["error"])

    def test_out_of_band_edit_yields_integrity_exit(self):
        boot = self._cli("bootstrap", "--work-item-id", "RACE-1", "--title", "r",
                         "--mode", "quick", "--change-type", "fix")
        boot.communicate(timeout=60); self.assertEqual(boot.returncode, 0)
        state_file = self.run / "state.yaml"
        state_file.write_text(state_file.read_text(encoding="utf-8") + "# tampered\n")
        show = self._cli("show")
        out, _ = show.communicate(timeout=30)
        self.assertEqual(show.returncode, 3)  # 3 = integrity (2 is argparse usage)
        self.assertIn("integrity", json.loads(out)["error"].lower())


class ConcurrentLedgerAppends(unittest.TestCase):
    """The ledgers carry the FSM's evidence and are written by one-shot hook
    processes that share no state but the files themselves — and develop.md
    mandates batching a step's spawns into ONE message, which is exactly what
    makes their PostToolUse hooks concurrent.

    O_APPEND alone was the whole protection, and it is not atomic on Windows
    (Python's is the CRT's seek-then-write), while `append_record` also
    re-opens the file to heal a torn tail and can issue two writes. The
    whole-system review measured the result on this platform: 6 processes x
    120 appends left 376 of 720 records, 2 barrier-synced processes x 200 left
    293 of 400 — 107 LOST, 0 torn. Silently gone, not visibly damaged, which
    is what makes it dangerous: a lost `spawn-pending` means the agent's
    SubagentStop finds nothing to pair, captures no verdict, and prints
    nothing.

    The run lock is no help — it was measured giving zero protection (40
    appends all landed while another process held it) — and must not become
    the answer: hooks deliberately never take it, so that a capture completes
    in ~0.15s while a 15s merge is in progress. Hence a SEPARATE, short-held
    ledger lock (`ndjson.ledger_lock`), which the two tests after this one
    pin as independent of the state lock in both directions."""

    WRITERS = 6
    PER_WRITER = 150

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.run = self.workspace / "ai" / "2026-01-01-LEDGER-1"
        self.run.mkdir(parents=True)
        self.ledger = self.run / "events.ndjson"
        self.go = self.workspace / "go.flag"

    def tearDown(self):
        support.rmtree(self.workspace)

    def _writer(self, tag, count=None) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", _APPEND_WRITER, str(ROOT), str(self.ledger),
             str(self.go), tag, str(count if count is not None
                                    else self.PER_WRITER)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")

    def test_every_concurrent_append_survives(self):
        procs = [self._writer(f"w{i}") for i in range(self.WRITERS)]
        self.go.write_text("go")            # barrier: release them together
        for proc in procs:
            _, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, err)
        records, skipped = ndjson.read_records_counting(self.ledger)
        # not one torn line…
        self.assertEqual(skipped, 0, "a torn line survived the lock")
        # …and not one LOST record, which is the failure that was measured
        expected = {(f"w{i}", n) for i in range(self.WRITERS)
                    for n in range(self.PER_WRITER)}
        self.assertEqual({(r["tag"], r["i"]) for r in records}, expected)
        self.assertEqual(len(records), len(expected))   # no duplicates either
        # …and `at` is NON-DECREASING IN FILE ORDER — the second property
        # `append_record` promises, load-bearing (several readers pick by
        # max(`at`) over records they then treat as the latest state) and
        # untested until round-4 review: mutation M6, minting `at` outside
        # the lock, survived all 1234 tests while producing 180 inversions
        # across 8 processes x 120 appends. Asserted on the SAME run as the
        # survival check because both are properties of one concurrent
        # append, and re-racing them separately only halves the coverage.
        stamps = [r["at"] for r in records]
        inversions = [(a, b) for a, b in zip(stamps, stamps[1:]) if a > b]
        self.assertEqual(inversions, [],
                         f"{len(inversions)} record(s) stamped older than a "
                         "row they physically follow — latest-wins readers "
                         "would pick the wrong one")

    def test_a_fresh_writer_in_one_clock_tick_cannot_stamp_older_than_the_tail(self):
        """The residual the in-lock mint alone leaves — round-4 review
        measured 6 of them, every one exactly 1us. `now_iso`'s clamp is
        per-PROCESS: it pushes a writer that already stamped this tick to
        T+1us, while a writer in ANOTHER process enters the same tick fresh,
        stamps raw T, and lands physically later. 1us is also why
        `_verdict_window`'s tie-break cannot absorb them — that engages on
        EQUALITY. The floor has to be the LEDGER's own tail, which every
        writer of the file shares.

        Deterministic rather than raced, and the two substitutions are the
        only things a second PROCESS contributes to this mechanism: a frozen
        clock (the same tick both writers read) and a fresh `_last_now` (the
        per-process state a new process starts empty). The genuine
        multi-process path is raced above; a same-tick collision cannot be
        scheduled on demand, and a test that waits for one to happen pins
        nothing on the run where it does not."""
        tick = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        frozen = types.SimpleNamespace(
            now=lambda tz=None: tick, fromisoformat=datetime.fromisoformat)
        with mock.patch.object(ndjson, "datetime", frozen), \
                mock.patch.object(ndjson, "_last_now", None):
            first = ndjson.append_record(self.ledger, {"tag": "A", "i": 0})
            # same process, same tick: the per-process clamp lifts it 1us
            second = ndjson.append_record(self.ledger, {"tag": "A", "i": 1})
            self.assertGreater(second["at"], first["at"])
            # …and now the OTHER process arrives in that same tick with no
            # `_last_now` of its own. Unclamped it stamps raw T — older than
            # the row it lands after, which is the inversion.
            ndjson._last_now = None
            third = ndjson.append_record(self.ledger, {"tag": "B", "i": 0})
        stamps = [r["at"] for r in ndjson.read_records(self.ledger)]
        self.assertEqual(stamps, [first["at"], second["at"], third["at"]])
        self.assertEqual(sorted(stamps), stamps)
        # strictly increasing, in fact: two records comparing EQUAL are the
        # shape the tie-break cannot separate either
        self.assertEqual(len(set(stamps)), 3)

    def test_a_capture_still_completes_while_the_state_lock_is_held(self):
        """The property round 3 established and this lock must not cost: a
        capture hook takes NO state lock, so it completes in ~0.15s even
        while `merge-task` holds the run lock across a 15s squash. Reusing
        the state lock for ledger writes would have queued every capture
        behind every merge — and the state lock's own wait budget is 120s."""
        with state_mod.locked(self.run):
            started = time.monotonic()
            proc = self._writer("under-lock", count=5)
            self.go.write_text("go")
            _, err = proc.communicate(timeout=60)
            elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, err)
        self.assertLess(elapsed, 30, "the append queued behind the state lock")
        self.assertEqual(len(ndjson.read_records(self.ledger)), 5)

    def test_a_state_locked_caller_can_append_while_hooks_are_writing(self):
        """The deadlock ordering, from the other side. `merge-task` holds the
        state lock and appends a ledger row inside it; a hook holds the ledger
        lock and never reaches for state. That ordering — ledger INNERMOST,
        always — is what keeps the two from meeting head-on, and it is a
        property of the code (nothing in `ndjson` touches `harness.state`),
        so this test is the executable reminder rather than the proof."""
        procs = [self._writer(f"hook{i}", count=40) for i in range(3)]
        self.go.write_text("go")
        started = time.monotonic()
        with state_mod.locked(self.run):
            for i in range(40):
                ndjson.append_record(self.ledger, {"tag": "merge", "i": i})
        elapsed = time.monotonic() - started
        for proc in procs:
            _, err = proc.communicate(timeout=300)
            self.assertEqual(proc.returncode, 0, err)
        self.assertLess(elapsed, 60, "state-lock-holder blocked on the ledger")
        records, skipped = ndjson.read_records_counting(self.ledger)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(records), 40 + 3 * 40)


class RmtreeAgainstConcurrentDeletion(unittest.TestCase):
    """CI (cutting v3.4.1): a fully green 942-test run failed in `tearDown`.
    Git's background auto-maintenance deleted `.git/objects/maintenance.lock`
    between `shutil.rmtree`'s scandir and its unlink; the FileNotFoundError
    routed into `support.rmtree`'s read-only handler, whose own first move —
    `os.chmod` — raised the same error again from INSIDE the handler and
    escaped.

    It surfaced on the macos-3.10 lane, but it is not an OS bug: **3.10 is the
    variable**. From 3.12 `shutil` absorbs FileNotFoundError on unlink itself
    (`except FileNotFoundError: continue`) and never calls the handler, so the
    same race is invisible there — macos-3.14 passed on identical code, and
    the two 3.10 lanes that passed simply did not hit the race. Reading the
    lane names alone would have blamed macOS.

    Nothing stops git maintaining a repo in the background, so the race is
    unavoidable and the handler has to absorb it. An error handler that dies
    on the error it is handling is no handler at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        support.rmtree(self.tmp, ignore_errors=True)

    def test_the_handler_absorbs_a_path_that_vanished_under_it(self):
        """The reported failure, exactly: rmtree's handler invoked for an
        entry git already deleted. Pre-fix, `os.chmod` raised FileNotFoundError
        a second time from inside the handler and escaped.

        Called DIRECTLY rather than through `shutil.rmtree`, and that is
        forced, not lazy: from 3.12 the stdlib absorbs FileNotFoundError on
        unlink itself, so no end-to-end rmtree test can reach this handler
        with a vanished path on 3.12+ — an end-to-end version passed with the
        bug fully present on this interpreter, and mutation testing is what
        exposed it. 3.10, where CI actually failed, has no such carve-out."""
        gone = self.tmp / ".git" / "objects" / "maintenance.lock"
        gone.parent.mkdir(parents=True)
        support.clear_readonly(os.unlink, gone,
                               FileNotFoundError(2, "No such file", str(gone)))

    def test_the_handler_still_removes_a_read_only_file(self):
        """The absorption must not cost the handler its actual job — a
        read-only git object still has to be cleared and unlinked."""
        obj = self.tmp / "read-only-object"
        obj.write_text("x")
        os.chmod(obj, stat.S_IREAD)
        support.clear_readonly(os.unlink, obj, PermissionError())
        self.assertFalse(obj.exists())

    def test_the_handler_removes_a_directory_too(self):
        """`func`'s arity is deliberately not trusted; the path's own type
        decides rmdir vs unlink."""
        d = self.tmp / "a-dir"
        d.mkdir()
        support.clear_readonly(os.open, d, PermissionError())
        self.assertFalse(d.exists())

    def test_a_genuinely_undeletable_tree_still_raises(self):
        """The absorption must not become a blanket swallow — a real failure
        (not a vanished path) still has to surface, or a leaked temp tree goes
        silent and the next run inherits it."""
        if os.name == "nt":
            self.skipTest("POSIX directory-permission semantics")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = self.tmp / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner" / "f.txt").write_text("x")
        os.chmod(locked / "inner", 0o500)            # no write => no unlink
        try:
            with self.assertRaises(OSError):
                support.rmtree(locked)
        finally:
            os.chmod(locked / "inner", 0o700)


if __name__ == "__main__":
    unittest.main()
