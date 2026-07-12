"""M1 done-criterion: concurrent `set-state` calls do not lose updates.

Eight subprocesses race distinct task transitions against ONE state.yaml
(rewrite-in-full). Without the flock, later writers clobber earlier ones and
some tasks stay pending; with it, every transition lands. This exercises the
real CLI end-to-end, exactly as parallel multi-repo develop lanes would.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
