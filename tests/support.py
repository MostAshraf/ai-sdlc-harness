"""Cross-platform test support (first Windows triage, 2026-07).

Three portability seams the suite shares, so each test file doesn't grow its
own platform branches:

  rmtree          tearDown-safe delete: git marks object files read-only,
                  and on Windows `shutil.rmtree` dies on them with
                  PermissionError (199 of the first Windows run's 290
                  failures were exactly this).
  HARNESS_BIN     the CLI wrapper tests spawn: `bin/harness` is a POSIX sh
                  script Windows cannot exec (`%1 is not a valid Win32
                  application`); its contract-equal sibling is
                  `bin/harness.cmd`.
  write_cli_stub  fake forge CLIs (`gh`/`glab`/`az`) on PATH: POSIX uses a
                  shebang script; Windows cannot exec those, and a `.cmd`
                  shim is NOT faithful either — cmd.exe truncates argv at an
                  embedded newline, and PR bodies are multi-line. So Windows
                  stubs are real PE launchers (pip's vendored distlib
                  launcher + an appended zipapp — the exact mechanism pip
                  uses for console scripts), which preserve argv byte-for-
                  byte (probe-verified, newlines included).

SCRATCH_FIXTURE_DIR mirrors hooks/guards.py `_tmp_roots()`: fixtures that
must deterministically land INSIDE the guard's scratch root use `/tmp` on
POSIX and the platform temp dir on Windows (where %TEMP% *is* the scratch
root and `/tmp` does not exist).
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HARNESS_BIN = ROOT / "bin" / ("harness.cmd" if os.name == "nt" else "harness")

# A runnable-everywhere no-op shell command for test_cmd fixtures: POSIX
# `true` does not exist on Windows, where `cmd /c exit 0` is the
# equivalent. (Fine as a *test command* through `subprocess(shell=True)`;
# NOT fine as a git editor — see gitops.autosquash for that story.)
NOP_CMD = "cmd /c exit 0" if os.name == "nt" else "true"

# A declared `language.repos.<name>.test_cmd` for config fixtures — a PLAIN
# runner invocation on every OS, never executed by the tests that use it.
# Deliberately NOT NOP_CMD: on Windows that is `cmd /c exit 0`, and the
# quarantine's shell-wrapper check correctly refuses to append exclusion
# flags to a `cmd /c` wrapper (they would attach to the wrapper, not the
# runner — the silent-full-suite failure that check exists to prevent). Using
# it as a declared test_cmd made the FIXTURE the thing under test on Windows,
# not the mechanism (first CI run of this branch: two quarantine tests failed
# there on exactly this, while the product behaviour was correct).
NOP_TEST_CMD = "npm test"

# dir= argument for tempfile.mkdtemp when a fixture must sit inside the
# guards' scratch root (None = the platform default temp dir, which on
# Windows is exactly what guards' _tmp_roots() returns there)
SCRATCH_FIXTURE_DIR = None if os.name == "nt" else "/tmp"


def seed_review_verdict(run: Path, mode: str = "plan-review",
                        verdict: str = "APPROVED") -> None:
    """Test-only simulation of the PostToolUse hook's reviewer-verdict
    capture (task-less form) — what a `verdict_bound` step's exits are
    derived from. In production ONLY the hook writes reviews.ndjson
    (AUTHORITY_RE blocks direct writes from agent tool calls)."""
    from harness import ndjson
    ndjson.append_record(run / "reviews.ndjson",
                         {"task": None, "mode": mode, "verdict": verdict})


def scratch_path(*parts: str) -> str:
    """A scratch-root path for guard payloads: `/tmp/...` on POSIX, the
    platform temp dir on Windows — the two spellings guards' _tmp_roots()
    treats as scratch on each OS."""
    base = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    return str(base.joinpath(*parts))


def clear_readonly(func, p, _exc) -> None:
    """`shutil.rmtree`'s error handler for this repo's temp trees: clear the
    read-only bit and retry the removal.

    Module level, not a closure inside `rmtree`, because it is the unit that
    carries the logic and the only way to test it on every Python this repo
    supports. From 3.12 the stdlib absorbs `FileNotFoundError` on unlink
    itself (`except FileNotFoundError: continue`), so no end-to-end rmtree
    test can reach this handler with a vanished path on a modern interpreter —
    while on 3.10, which has no such carve-out, it is reached and mattered.
    """
    # ALREADY GONE is a success, not an error — and it must be checked BEFORE
    # chmod, this handler's own first move, which raises on a missing path.
    # CI, python-3.10 (cutting v3.4.1): git's background auto-maintenance
    # deleted `.git/objects/maintenance.lock` between rmtree's scandir and its
    # unlink, the FileNotFoundError routed here, `os.chmod` raised the same
    # error a second time from INSIDE the handler, and that escaped to fail an
    # otherwise-green 942-test run in tearDown. An error handler that dies on
    # the error it is handling is no handler. Nothing stops git maintaining a
    # repo in the background, so the race is unavoidable: absorb it.
    if not os.path.lexists(p):            # lexists: a broken symlink is real
        return
    try:
        os.chmod(p, stat.S_IWRITE)
    except FileNotFoundError:
        return
    # Don't trust func's arity — POSIX's fd-based rmtree walker can hand
    # us os.open (which needs a `flags` arg, not just a path) when a
    # permission-locked entry blocks the directory scan itself, not just
    # unlink/rmdir. Decide unlink vs. rmdir from the path's own type
    # instead; that's a strict superset of what func(p) covered.
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            os.rmdir(p)
        else:
            os.unlink(p)
    except FileNotFoundError:
        return                            # lost the same race one step later


def rmtree(path, ignore_errors: bool = False) -> None:
    """shutil.rmtree that clears the read-only bit and retries — required
    for any tree holding a .git (git object files are read-only, which
    Windows honors on unlink where POSIX does not)."""
    _clear_readonly = clear_readonly
    kwargs = ({"onexc": _clear_readonly} if sys.version_info >= (3, 12)
              else {"onerror": _clear_readonly})  # onexc is 3.12+
    try:
        shutil.rmtree(path, **kwargs)
    except FileNotFoundError:
        if not ignore_errors:
            raise
    except OSError:
        if not ignore_errors:
            raise


def _launcher_bytes() -> bytes:
    """pip's vendored distlib console launcher for this machine — the PE
    prefix a Windows CLI stub is built from. A hard, named failure beats a
    skip: silently skipping would un-cover the whole provider matrix on the
    one lane it was just made honest for."""
    try:
        from pip._vendor.distlib import scripts as _scripts
    except ImportError as exc:  # pragma: no cover — pip ships everywhere CI runs
        raise RuntimeError(
            "Windows CLI stubs need pip's vendored distlib launcher "
            "(pip._vendor.distlib) — install pip into this interpreter"
        ) from exc
    machine = platform.machine().lower()
    name = {"amd64": "t64.exe", "arm64": "t64-arm.exe",
            "x86": "t32.exe"}.get(machine, "t64.exe")
    return (Path(_scripts.__file__).parent / name).read_bytes()


def write_cli_stub(bin_dir: Path, name: str, script_text: str) -> None:
    """Install `script_text` (a Python program) as a fake CLI named `name`
    inside `bin_dir`, invocable via PATH lookup on this OS. The script's
    `__file__` parent is `bin_dir` on every platform, so stubs may keep
    state in sibling files exactly as the POSIX shebang form always has."""
    if os.name != "nt":
        path = bin_dir / name
        path.write_text(script_text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return
    stub = bin_dir / f"{name}-cli-stub.py"
    stub.write_text(script_text, encoding="utf-8")
    # zipapp bootstrap: exec the sibling stub file with __file__ pointing at
    # it (running the stub from inside the zip would re-root __file__ into
    # the archive and break the sibling-file state convention)
    boot = (
        "import sys\n"
        "from pathlib import Path\n"
        f"stub = Path(sys.argv[0]).resolve().parent / {stub.name!r}\n"
        "code = compile(stub.read_text(encoding='utf-8'), str(stub), 'exec')\n"
        "exec(code, {'__name__': '__main__', '__file__': str(stub)})\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__main__.py", boot)
    shebang = f'#!"{sys.executable}"\r\n'.encode()
    (bin_dir / f"{name}.exe").write_bytes(
        _launcher_bytes() + shebang + buf.getvalue())
