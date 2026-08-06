"""M1 done-criterion: concurrent `set-state` calls do not lose updates.

Eight subprocesses race distinct task transitions against ONE state.yaml
(rewrite-in-full). Without the flock, later writers clobber earlier ones and
some tasks stay pending; with it, every transition lands. This exercises the
real CLI end-to-end, exactly as parallel multi-repo develop lanes would.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import stat
import unittest
from pathlib import Path

from tests import support

ROOT = Path(__file__).resolve().parent.parent
N_TASKS = 8


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
