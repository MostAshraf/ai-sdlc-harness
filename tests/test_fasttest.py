"""fasttest.py done-criteria: the sharded runner runs EXACTLY the suite the
serial discover runs — same classes (a test-less base must vanish, an
import failure must abort loud), same leaf count (the parity guard, proven
in-process against the same loader the workers use), aggregate semantics
(a count mismatch is a failure even when every shard is green), and the
end-to-end CLI on a synthetic package: green run exits 0 with matching
totals, a failing test exits 1 with the traceback surfaced, and the fixture
cross-imports a sibling module the way the real suite imports tests.support
— the shape that broke the runner's first full-suite run (discover inside a
process whose sys.path lacked the top-level root turned every module into
an import failure)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import support

ROOT = Path(__file__).resolve().parent.parent
FASTTEST = ROOT / "tools" / "fasttest.py"
sys.path.insert(0, str(ROOT / "tools"))
import fasttest  # noqa: E402


class RoundRobinChunking(unittest.TestCase):
    def test_chunks_cover_every_id_exactly_once_and_interleave(self):
        ids = [f"tests.test_x.C{i:02d}" for i in range(10)]
        shards = fasttest.chunk_round_robin(ids, 3)
        self.assertEqual(sorted(i for s in shards for i in s), ids)
        # interleaving, not contiguous slicing: C00..C09 split across 3
        # workers puts neighbors in DIFFERENT shards, which is what mixes
        # the slow breadth/gitops classes into separate workers
        self.assertEqual(shards[0], [ids[0], ids[3], ids[6], ids[9]])
        self.assertEqual(shards[1], [ids[1], ids[4], ids[7]])
        self.assertEqual(shards[2], [ids[2], ids[5], ids[8]])

    def test_more_workers_than_ids_yields_empty_shards(self):
        shards = fasttest.chunk_round_robin(["a.A", "b.B"], 4)
        self.assertEqual([len(s) for s in shards], [1, 1, 0, 0])


class RealSuiteCollection(unittest.TestCase):
    """collect() against the repo's actual tests/ — the same walk the
    production runner performs before sharding."""

    @classmethod
    def setUpClass(cls):
        cls.ids, cls.expected = fasttest.collect(ROOT / "tests", "tests", ROOT)

    def test_real_classes_found_and_testless_base_excluded(self):
        # GuardHarness is the shared setUp/tearDown base with no test
        # methods of its own — discover runs no leaves for it, so it must
        # not appear as a shardable id (a worker handed it would run 0)
        self.assertIn("tests.test_guards.BashGuard", self.ids)
        self.assertNotIn("tests.test_guards.GuardHarness", self.ids)

    def test_leaf_count_parity_with_the_serial_runner(self):
        # THE seam this runner lives or dies on: for every sharded id, the
        # loader the `python -m unittest` worker itself uses must resolve
        # the same number of leaves the discover walk counted. Proven here
        # in-process for every class — the e2e below then proves the
        # subprocess side on a synthetic package.
        loader = unittest.TestLoader()
        total = 0
        for cid in self.ids:
            suite = loader.loadTestsFromName(cid)
            count = 0
            stack = [suite]
            while stack:
                node = stack.pop()
                if isinstance(node, unittest.TestSuite):
                    stack.extend(node)
                else:
                    count += 1
            self.assertGreater(count, 0, f"{cid} resolved to no tests")
            total += count
        self.assertEqual(total, self.expected)
        self.assertGreater(self.expected, 1000)  # coarse floor: ~1287 today


class AggregateGuard(unittest.TestCase):
    def _shard(self, ran, rc=0):
        return {"ids": [], "rc": rc, "ran": ran, "seconds": 1.0, "output": ""}

    def test_green_shards_with_matching_total_pass(self):
        ok, problems = fasttest.aggregate([self._shard(3), self._shard(2)], 5)
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_count_mismatch_fails_even_with_all_shards_green(self):
        # "quieter" must not pass for "faster": a dropped class would look
        # exactly like this — every shard rc=0, the sum short by its tests
        ok, problems = fasttest.aggregate([self._shard(3), self._shard(2)], 6)
        self.assertFalse(ok)
        self.assertTrue(any("count guard" in p for p in problems))

    def test_failing_shard_fails_even_with_matching_total(self):
        ok, _ = fasttest.aggregate([self._shard(3), self._shard(2, rc=1)], 5)
        self.assertFalse(ok)


def _write_pkg(root: Path, fail: bool) -> None:
    # mirrors the real tests/ shape: a plain directory of test modules with
    # NO __init__.py, imported by the workers as a namespace package from
    # the top-level root (the exact mechanism the repo's own tests/ uses).
    # helper.py + the cross-import below mirror `from tests import support`:
    # without the top-level root on the parent's sys.path, discover turns
    # BOTH test modules into import failures and collect aborts.
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "helper.py").write_text(
        "SENTINEL = 'helper-reached'\n", encoding="utf-8")
    (pkg / "test_a.py").write_text(
        "import unittest\n"
        "from pkg import helper\n\n\n"
        "class A1(unittest.TestCase):\n"
        "    def test_one(self):\n"
        "        self.assertEqual(helper.SENTINEL, 'helper-reached')\n\n"
        "    def test_two(self):\n        pass\n\n\n"
        "class A2(unittest.TestCase):\n"
        "    def test_three(self):\n        pass\n",
        encoding="utf-8")
    (pkg / "test_b.py").write_text(
        "import unittest\n\n\n"
        "class B1(unittest.TestCase):\n"
        f"    def test_ok(self):\n"
        f"        self.assertFalse({fail}, 'seeded failure')\n"
        "    def test_also_ok(self):\n        pass\n",
        encoding="utf-8")


class EndToEndOnSyntheticPackage(unittest.TestCase):
    """The full CLI against a 5-test synthetic package in a temp root:
    proves the subprocess layer (worker spawn, cwd/import root, Ran-count
    parsing, failing-shard traceback surfacing) without paying for the
    real suite inside the suite."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        support.rmtree(self.root, ignore_errors=True)

    def _run(self, fail: bool, workers: int = 2):
        _write_pkg(self.root, fail)
        return subprocess.run(
            [sys.executable, str(FASTTEST),
             "--start-dir", str(self.root / "pkg"),
             "--top-level", str(self.root),
             "--workers", str(workers)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", timeout=120)

    def test_green_run_exits_zero_with_matching_totals(self):
        proc = self._run(fail=False)
        self.assertEqual(proc.returncode, 0,
                         (proc.stdout + proc.stderr)[-2000:])
        self.assertIn("5 tests in 3 classes", proc.stdout)
        self.assertIn("\nOK — 5 tests", proc.stdout)

    def test_seeded_failure_exits_one_and_surfaces_the_traceback(self):
        proc = self._run(fail=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[FAIL]", proc.stdout)
        self.assertIn("seeded failure", proc.stdout)   # traceback not swallowed
        self.assertIn("FAILED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
