"""init-workspace's SKILL.md venv-bootstrap instruction (step 0) must stay
parseable by BOTH shells a platform may pick when a model runs it through
its shell tool: Claude Code's Bash tool always fires bash (Git Bash on
Windows), but Qwen Code's `run_shell_command` tool shares hooks'
getShellConfiguration() — on Windows outside an MSYS-flavored terminal it
falls back to cmd.exe, where the old inline POSIX probe
(`SYS="$(command -v python3 || command -v python)"`) was unparseable: cmd
treats `=` as an argument delimiter. Same root cause, same fix shape as
hooks/run-guard: a committed launcher pair invoked through ONE dual-clause
command,

    exec "<root>/bin/setup-venv" || <root>/bin/setup-venv

so the shell that ends up running it self-selects the right half without
the model (or a human reading the doc) needing to predict which shell a
given launch will use."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import support

ROOT = Path(__file__).resolve().parent.parent
SETUP_VENV = ROOT / "bin" / "setup-venv"
SETUP_VENV_CMD = ROOT / "bin" / "setup-venv.cmd"
SKILL_MD = ROOT / "skills" / "init-workspace" / "SKILL.md"

COMMAND_RE = re.compile(
    r'exec "\$\{CLAUDE_PLUGIN_ROOT\}/bin/setup-venv" '
    r'\|\| \$\{CLAUDE_PLUGIN_ROOT\}/bin/setup-venv\b')

BASH = shutil.which("bash")


def _launcher_command(root: Path) -> str:
    return f'exec "{root}/bin/setup-venv" || {root}/bin/setup-venv'


def _path_without_any_python() -> str:
    """The real PATH with every directory holding a `python`/`python3`
    executable removed — everything else setup-venv's own internals need
    (`dirname`, bash's own resolution, coreutils in general) stays
    resolvable exactly as normal; only the interpreter search comes up
    genuinely empty. FILTERING the real PATH, rather than isolating a
    copy or symlink of one tool in a scratch directory, was chosen after
    both alternatives broke on this host: a copied/symlinked bash or
    dirname failed to find ITS OWN shared-library dependencies (an
    MSYS/Windows DLL-search quirk, not a stand-in for how ubuntu-latest/
    macos-latest's real dynamic linker resolves libraries — but reason
    enough not to trust the technique further without seeing it pass).
    An entirely empty PATH was tried first and rejected for a different
    reason: `dirname` is an external coreutils binary setup-venv's own
    `HERE=` line depends on, not a shell builtin, so a fully empty PATH
    fails the script before it ever reaches the python search — a
    different, unintended failure than the one under test."""
    keep = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        p = Path(d)
        if any((p / name).exists() for name in
              ("python3", "python3.exe", "python", "python.exe")):
            continue
        keep.append(d)
    return os.pathsep.join(keep)


def _install_launcher_pair(root: Path) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SETUP_VENV, bin_dir / "setup-venv")
    (bin_dir / "setup-venv").chmod(0o755)
    shutil.copy2(SETUP_VENV_CMD, bin_dir / "setup-venv.cmd")


def _stub_already_satisfied_venv(root: Path) -> None:
    """A fake `.venv` whose python already answers `import yaml` with
    success — exercises the launcher's fast idempotent path without a
    real venv create + pip install (hermetic, no network, no multi-second
    cost). setup-venv invokes this file DIRECTLY as `"$PY" -c
    "import yaml"` (the interpreter itself, not code handed to one), so
    the stub must be a self-executing program that exits 0 regardless of
    its arguments — not just any file named python(.exe): batch bytes in
    a file named python.exe fail CreateProcess outright (Windows), and
    raw Python SOURCE with no shebang gets kernel-ENOEXEC-retried as a
    sh script on POSIX (`raise SystemExit(0)` is a syntax error there) —
    both silently escaped into the FULL bootstrap path (a real venv, a
    network pip install, inside a "fast path" test) rather than failing
    loud, and both invisible from a Windows dev machine, where only the
    Windows branch below ever runs locally: CI's Linux/macOS lanes are
    what caught the POSIX case. A plain shebang script covers POSIX;
    support.write_cli_stub's faithful PE wrapper covers Windows."""
    venv_bin = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    if os.name == "nt":
        support.write_cli_stub(venv_bin, "python", "raise SystemExit(0)\n")
    else:
        stub = venv_bin / "python"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


class VenvBootstrapLauncherContract(unittest.TestCase):
    """Static content checks — always run, no subprocess needed."""

    def test_skill_invokes_the_dual_clause_launcher(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertRegex(
            text, COMMAND_RE,
            "init-workspace SKILL.md must bootstrap the venv through the "
            "shell-agnostic launcher pair, not an inline POSIX-only probe")

    def test_old_inline_probe_is_gone(self):
        # the whole point of the fix: a model must never be handed a
        # `command -v python3 || command -v python` one-liner to run
        # directly through a shell that might turn out to be cmd.exe
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertNotIn("command -v python3", text)

    def test_launcher_pair_files_exist_with_correct_line_endings(self):
        sh = SETUP_VENV.read_bytes()
        self.assertTrue(sh.startswith(b"#!/usr/bin/env bash"))
        self.assertNotIn(b"\r", sh,
                         "bin/setup-venv must stay LF-only: a stock Linux "
                         "bash rejects a CRLF shebang script")
        for needle in (b".venv/bin/python", b".venv/Scripts/python.exe",
                       b"command -v python3", b"import yaml"):
            self.assertIn(needle, sh)
        bat = SETUP_VENV_CMD.read_bytes()
        self.assertIn(b"\r\n", bat,
                      "bin/setup-venv.cmd must stay CRLF: cmd.exe parsing "
                      "has LF-only edge cases")
        for needle in (rb".venv\Scripts\python.exe", b"import yaml",
                       rb"exit /b %ERRORLEVEL%"):
            self.assertIn(needle, bat)

    def test_wrapper_is_executable_on_posix(self):
        if os.name != "nt":
            self.assertTrue(SETUP_VENV.stat().st_mode & 0o111,
                            "bin/setup-venv not executable")


class VenvBootstrapDispatchSmoke(unittest.TestCase):
    """Functional smoke tests over the launcher pair itself: dispatch
    correctness only (fast idempotent path, missing-interpreter error
    path) — never a real venv create + pip install, no network, matching
    HookLauncherContract's own scope (tests/test_guards.py)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        _install_launcher_pair(self.root)

    def tearDown(self):
        support.rmtree(self.root, ignore_errors=True)

    def _run(self, argv, env_extra=None):
        env = {**os.environ, **(env_extra or {})}
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", timeout=60, env=env)
        return proc.returncode, proc.stderr

    def _assert_stayed_hermetic(self):
        # a REAL `python -m venv` always writes pyvenv.cfg at the venv
        # root — its absence proves the fast path never bootstrapped
        self.assertFalse(
            (self.root / ".venv" / "pyvenv.cfg").exists(),
            "fast-path test escaped the stub and built a real venv "
            "(non-hermetic: network pip install inside a unit test)")

    @unittest.skipUnless(os.name == "posix" and BASH,
                         "bash launch path needs a POSIX bash")
    def test_bash_fast_path_when_already_satisfied(self):
        _stub_already_satisfied_venv(self.root)
        code, err = self._run([BASH, "-c", _launcher_command(self.root)])
        self.assertEqual(code, 0, err)
        self._assert_stayed_hermetic()

    @unittest.skipUnless(os.name == "posix" and BASH,
                         "bash launch path needs a POSIX bash")
    def test_bash_errors_clearly_when_no_interpreter_found(self):
        # Invoke bash directly ON THE FILE (`bash script`, not the
        # dual-clause `exec "script" || ...` launcher command): in this
        # invocation style bash reads the script's own content, and its
        # `#!/usr/bin/env bash` line is just a comment needing no `env`/
        # PATH resolution to launch AT ALL — isolating this test to
        # exactly what it means to exercise, setup-venv's OWN internal
        # `command -v python3 || command -v python` fallback correctly
        # reporting failure, not the outer launcher's shebang
        # resolution (covered by the fast-path test above and by
        # HookLauncherContract's equivalents for the hook launchers).
        script = self.root / "bin" / "setup-venv"
        code, err = self._run([BASH, str(script)],
                              {"PATH": _path_without_any_python()})
        self.assertEqual(code, 1, err)
        self.assertIn("no system python3/python found", err)

    @unittest.skipUnless(os.name == "nt", "cmd.exe fallback is Windows-only")
    def test_cmd_fallback_fast_path_when_already_satisfied(self):
        # The EXACT spawn shape Qwen Code uses on Windows outside an MSYS
        # terminal: spawn('cmd.exe', ['/d','/s','/c', cmd], {shell:false}).
        _stub_already_satisfied_venv(self.root)
        code, err = self._run(
            ["cmd.exe", "/d", "/s", "/c", _launcher_command(self.root)])
        self.assertEqual(code, 0, err)
        self.assertIn("not recognized", err)  # clause 1 ('exec') did fail
        self._assert_stayed_hermetic()

    @unittest.skipUnless(os.name == "nt", "cmd.exe fallback is Windows-only")
    def test_cmd_fallback_errors_clearly_when_no_interpreter_found(self):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        code, err = self._run(
            ["cmd.exe", "/d", "/s", "/c", _launcher_command(self.root)],
            {"PATH": f"{windir}\\System32;{windir}"})
        self.assertEqual(code, 1)
        self.assertIn("no system python3/python found", err)


if __name__ == "__main__":
    unittest.main()
