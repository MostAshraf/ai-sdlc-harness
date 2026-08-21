"""Parallel test runner — same tests, same semantics, sharded by TestCase
class across worker processes.

Why: the suite is subprocess-heavy by design (guard tests exec the real
guards.py per payload; breadth/gitops tests build real git fixtures), so a
serial run pays ~100ms per interpreter spawn and ~350ms per fixture
single-filed through one core. Measured on a 24-core host: serial ~16.5 min;
class-sharded across 8 workers ~2 min, with every one of the suite's tests
green — the suite's isolation contract (each test builds its state inside a
per-test tempfile.mkdtemp workspace; nothing chdirs or writes a fixed shared
path) is what makes this safe. That contract is now load-bearing: a test
that shares a fixed path would pass serially and collide in parallel.

Mechanics: enumerate TestCase classes by WALKING the same discover() suite
the serial CI command walks (`discover -s tests`: start dir is its own
top-level, so modules load under bare names like `test_guards` — tests/ has
no __init__.py and is NOT importable under an explicit top_level_dir, which
is why the walk uses the CI shape and the package prefix is restored
afterwards for the workers). Round-robin the sorted class ids across N
workers (deterministic shards, mixed fast/slow classes), run each as
`python -m unittest <ids...>`, then assert the summed `Ran` counts equal
the discovered leaf count — a seam-proof backstop: any future
unittest CLI/discovery disagreement fails loud instead of quietly running
less than the full suite.

CLI:  python tools/fasttest.py [--workers N] [--timeout S]
      [--start-dir DIR] [--top-level DIR]        exit 0 ok / 1 failure
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 8, not cpu_count(): the workers themselves fan out further subprocesses
# (guard probes, git fixtures), so oversubscribing the box adds contention
# without shortening the critical-path shard.
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 900


def _worker_id(cls: type, package: str) -> str:
    """Bare module name from the CI-shaped walk -> worker-spelled dotted
    id (`test_guards.BashGuard` -> `tests.test_guards.BashGuard`), the form
    that imports from the repo root the workers run in."""
    module = cls.__module__
    if "." not in module:
        module = f"{package}.{module}"
    return f"{module}.{cls.__qualname__}"


def collect(start_dir: Path, package: str,
            top_level: Path | None = None) -> tuple[list[str], int]:
    """Class ids and expected leaf-test count, from the same discover walk
    the serial runner performs (`discover -s <start_dir>`).

    top_level (default: start_dir.parent) goes on sys.path FIRST: run as a
    script, sys.path[0] is tools/, not the repo root, and the test modules'
    own `from tests import support` imports then fail inside discover — the
    first full-suite run failed exactly there, every module a _FailedTest.
    The serial command gets this for free because `python -m unittest` puts
    the cwd on sys.path."""
    if top_level is None:
        top_level = start_dir.parent
    if str(top_level) not in sys.path:
        sys.path.insert(0, str(top_level))
    loader = unittest.TestLoader()
    suite = loader.discover(str(start_dir), top_level_dir=str(start_dir))
    ids: dict[str, int] = {}
    failed: list[str] = []
    stack = [suite]
    while stack:
        node = stack.pop()
        if isinstance(node, unittest.TestSuite):
            stack.extend(node)
            continue
        cls = type(node)
        if cls.__module__ == "unittest.loader" and "FailedTest" in cls.__name__:
            failed.append(node.id())   # a module that cannot import must not
            continue                   # be sharded — surface it, not hide it
        cid = _worker_id(cls, package)
        ids[cid] = ids.get(cid, 0) + 1
    if failed:
        raise SystemExit(
            "IMPORT FAILURE in the suite itself (run serially to see it): "
            + ", ".join(failed))
    return sorted(ids), sum(ids.values())


def chunk_round_robin(ids: list[str], workers: int) -> list[list[str]]:
    """Interleave sorted ids: workers receive every Nth class, mixing the
    fast schema/mermaid-style classes with the slow breadth/gitops ones."""
    return [ids[i::workers] for i in range(workers)]


def run_shard(ids: list[str], cwd: Path, timeout: int) -> dict:
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *ids],
            cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        out = (proc.stderr or "") + (proc.stdout or "")
        ran = next((int(ln.split()[1]) for ln in out.splitlines()
                    if ln.startswith("Ran ")), 0)
        return {"ids": ids, "rc": proc.returncode, "ran": ran,
                "seconds": time.perf_counter() - t0, "output": out}
    except subprocess.TimeoutExpired:
        return {"ids": ids, "rc": None, "ran": 0,
                "seconds": time.perf_counter() - t0,
                "output": f"SHARD TIMEOUT after {timeout}s — the classes in "
                          "this shard may be wedged on a subprocess; run "
                          "them singly to localize"}


def aggregate(shards: list[dict], expected: int) -> tuple[bool, list[str]]:
    """All shards green AND their summed `Ran` equals the discovered count —
    the count guard is the difference between 'faster' and 'quieter'."""
    problems = [f"shard rc={s['rc']} ({s['output'].strip()[-200:]})"
                for s in shards if s["rc"] != 0]
    total = sum(s["ran"] for s in shards)
    if total != expected:
        problems.append(
            f"count guard: shards ran {total} tests, discovery found "
            f"{expected} — a class was skipped or double-run; run serially "
            f"to diff")
    return not problems, problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int,
                    default=min(DEFAULT_WORKERS, os.cpu_count() or 1))
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="per-shard seconds (default %(default)s)")
    ap.add_argument("--start-dir", type=Path, default=ROOT / "tests",
                    help="directory of test modules (default: tests/)")
    ap.add_argument("--top-level", type=Path, default=ROOT,
                    help="cwd/import root for workers (default: repo root)")
    args = ap.parse_args(argv)
    workers = max(1, args.workers)

    package = args.start_dir.name
    ids, expected = collect(args.start_dir, package, args.top_level)
    shards = chunk_round_robin(ids, workers)
    print(f"{expected} tests in {len(ids)} classes -> "
          f"{len(shards)} workers (cwd {args.top_level})", flush=True)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(len(shards)) as pool:
        results = list(pool.map(
            lambda s: run_shard(s, args.top_level, args.timeout), shards))
    for s in results:
        mark = "PASS" if s["rc"] == 0 else "FAIL"
        print(f"[{mark}] {len(s['ids']):3d} classes  {s['ran']:5d} tests  "
              f"{s['seconds']:7.1f}s", flush=True)

    ok, problems = aggregate(results, expected)
    print(f"\n{'OK' if ok else 'FAILED'} — {expected} tests, "
          f"{sum(s['seconds'] for s in results):.0f}s worker-time, "
          f"{time.perf_counter() - t0:.0f}s wall", flush=True)
    if not ok:
        for s in results:
            if s["rc"] != 0:
                print(f"\n--- failing shard ({len(s['ids'])} classes) ---\n"
                      f"{s['output'].rstrip()}", flush=True)
        for p in problems:
            print(f"!! {p}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
