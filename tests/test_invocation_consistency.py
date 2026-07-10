"""Regression: every skill/agent reference to the harness CLI must go through
the wrapper (`${CLAUDE_PLUGIN_ROOT}/bin/harness`), never a bare `harness`
command (no such binary exists on PATH) and never a shell-variable alias
(Bash tool calls do not persist shell state between invocations — a `$FOO`
defined in one call is gone in the next). Field report: a bare `harness`
reference caused the orchestrator to run `which harness; harness --version`
and fail."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BARE_INVOCATION = re.compile(r'`harness [a-z]|^\s*harness [a-z]|[&;|]\s*harness [a-z]',
                             re.MULTILINE)
SHELL_VAR_ALIAS = re.compile(r'\$HARNESS\b')


class InvocationConsistency(unittest.TestCase):
    def _runtime_md(self):
        for base in ("skills", "agents"):
            yield from (ROOT / base).rglob("*.md")

    def test_no_bare_harness_invocations(self):
        offenders = {}
        for f in self._runtime_md():
            hits = BARE_INVOCATION.findall(f.read_text())
            if hits:
                offenders[str(f.relative_to(ROOT))] = hits
        self.assertFalse(offenders,
                         f"bare `harness <verb>` reference(s) found (must be "
                         f"'${{CLAUDE_PLUGIN_ROOT}}/bin/harness <verb>'): {offenders}")

    def test_no_shell_variable_alias(self):
        offenders = [str(f.relative_to(ROOT)) for f in self._runtime_md()
                     if SHELL_VAR_ALIAS.search(f.read_text())]
        self.assertFalse(offenders,
                         f"$HARNESS shell-variable alias found (Bash calls "
                         f"don't persist shell state — inline the wrapper "
                         f"path every time): {offenders}")

    def test_wrapper_script_exists_and_is_executable(self):
        wrapper = ROOT / "bin" / "harness"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & 0o111, "bin/harness not executable")

    def test_wrapper_falls_back_to_system_python_and_still_runs(self):
        import os
        import subprocess
        # NO_COLOR, not just relying on capture_output's non-tty pipe: a
        # FORCE_COLOR in the calling environment (argparse's colorizer
        # honors it even off a tty, Python 3.13+) would otherwise ANSI-wrap
        # this output and break the plain-text assertion below.
        proc = subprocess.run([str(ROOT / "bin" / "harness"), "--help"],
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NO_COLOR": "1"})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage: harness", proc.stdout)

    def test_global_flags_accepted_before_or_after_the_verb(self):
        """Field report: skill docs place --workspace/--run inconsistently —
        some examples put them before the verb, most put them after. Both
        orderings must reach real dispatch logic (exit 1, a normal refusal)
        rather than dying in argparse (exit 2, 'unrecognized arguments') —
        see harness/cli.py's per-subparser `parents=[common]`."""
        import os
        import subprocess
        import tempfile
        wrapper = ROOT / "bin" / "harness"
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--workspace", tmp, "--run", tmp, "show"],
                        ["show", "--workspace", tmp, "--run", tmp]):
                proc = subprocess.run([str(wrapper), *args], capture_output=True,
                                      text=True, timeout=30,
                                      env={**os.environ, "NO_COLOR": "1"})
                self.assertNotIn("unrecognized arguments", proc.stderr,
                                 f"{args} -> {proc.stderr}")
                self.assertEqual(proc.returncode, 1, f"{args} -> {proc.stderr}")

    def test_planner_repo_map_bullet_says_not_to_stamp(self):
        """Field report: two of three identically-prompted planner agents
        proactively ran repo-map-stamp themselves despite that being the
        orchestrator's job — the negative instruction that fixed it (see
        hooks/guards.py's PLANNER_STAMP_RE for the mechanical backstop on
        the same rule) must stay present; a future edit to this bullet
        could otherwise silently drop it with nothing else noticing."""
        text = (ROOT / "agents" / "planner.md").read_text()
        self.assertIn("never write `.meta.json` or run `repo-map-stamp`", text)

    def test_every_documented_verb_and_flag_exists_in_argparse(self):
        """Adversarial-review finding: the wrapper-only checks above can't
        see a nonexistent verb or flag AFTER the wrapper path — e.g. a
        retired `set-state`, or an example missing a required flag rename —
        the exact drift class that strands a literal-following orchestrator
        mid-run. Every backtick-span/code-fence invocation in skills/ and
        agents/ is validated against the real parser."""
        from harness.cli import build_parser
        _, subs = build_parser()
        global_flags = {"--workspace", "--run", "--help"}
        flags_by_verb = {
            verb: {opt for action in parser._actions
                   for opt in action.option_strings} | global_flags
            for verb, parser in subs.items()}
        span_re = re.compile(r"```(?:\w*\n)?(.*?)```|`([^`]+)`", re.DOTALL)
        for f in self._runtime_md():
            text = f.read_text()
            for m in span_re.finditer(text):
                span = m.group(1) or m.group(2) or ""
                if "bin/harness" not in span:
                    continue
                tokens = span.split()
                for i, tok in enumerate(tokens):
                    if not tok.endswith("bin/harness"):
                        continue
                    rest = tokens[i + 1:]
                    j = 0   # skip global flags (+ values) before the verb
                    while j < len(rest) and rest[j].startswith("--"):
                        j += 2
                    if j >= len(rest) or rest[j].startswith("<"):
                        continue  # prose mention / placeholder verb
                    verb = rest[j].rstrip("`.,;:")
                    self.assertIn(
                        verb, subs,
                        f"{f.relative_to(ROOT)}: unknown verb '{verb}'")
                    for tok2 in rest[j + 1:]:
                        tok2 = tok2.rstrip("`.,;:)")
                        if tok2.startswith("--") and re.fullmatch(
                                r"--[a-z][a-z-]*", tok2):
                            self.assertIn(
                                tok2, flags_by_verb[verb],
                                f"{f.relative_to(ROOT)}: verb '{verb}' has "
                                f"no flag '{tok2}'")

    def test_every_manifest_step_has_a_step_file(self):
        # currently true by hand — this keeps it true by machine (a new
        # manifest step without its instruction file strands the walker)
        import yaml
        manifest = yaml.safe_load(
            (ROOT / "pipeline" / "manifest.yaml").read_text())
        gate_steps = {sid for sid, s in manifest["steps"].items()
                      if s.get("gate")}  # all gates share steps/gate.md
        referenced = {s for seq in manifest["modes"].values() for s in seq}
        referenced |= {s for g in (manifest.get("groups") or {}).values()
                       for s in g["steps"]}
        missing = [s for s in sorted(referenced - gate_steps)
                   if not (ROOT / "skills" / "dev-workflow" / "steps"
                           / f"{s}.md").is_file()]
        self.assertFalse(missing, f"manifest steps without a step file: {missing}")

    def test_gate_md_disposition_example_matches_the_manifest(self):
        # gate.md shows the human a numbered security-gate option list; the
        # CLI resolves numbers against the manifest's declared dispositions
        # — the two must agree or the human's "2" means the wrong thing
        import yaml
        manifest = yaml.safe_load(
            (ROOT / "pipeline" / "manifest.yaml").read_text())
        declared = manifest["steps"]["approve-security"]["dispositions"]
        text = (ROOT / "skills" / "dev-workflow" / "steps" / "gate.md").read_text()
        shown = re.findall(r"\[(\d)\]\s*([a-z-]+)", text)
        self.assertTrue(shown, "gate.md no longer shows the numbered options")
        for num, name in shown:
            self.assertEqual(
                declared[int(num) - 1], name,
                f"gate.md shows [{num}] {name} but manifest dispositions "
                f"are {declared}")


if __name__ == "__main__":
    unittest.main()
