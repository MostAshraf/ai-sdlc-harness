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
from tests import support

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
            hits = BARE_INVOCATION.findall(f.read_text(encoding="utf-8"))
            if hits:
                offenders[str(f.relative_to(ROOT))] = hits
        self.assertFalse(offenders,
                         f"bare `harness <verb>` reference(s) found (must be "
                         f"'${{CLAUDE_PLUGIN_ROOT}}/bin/harness <verb>'): {offenders}")

    def test_no_shell_variable_alias(self):
        offenders = [str(f.relative_to(ROOT)) for f in self._runtime_md()
                     if SHELL_VAR_ALIAS.search(f.read_text(encoding="utf-8"))]
        self.assertFalse(offenders,
                         f"$HARNESS shell-variable alias found (Bash calls "
                         f"don't persist shell state — inline the wrapper "
                         f"path every time): {offenders}")

    def test_wrapper_script_exists_and_is_executable(self):
        import os
        wrapper = ROOT / "bin" / "harness"
        self.assertTrue(wrapper.is_file())
        # POSIX-only: Windows has no exec bit — there the launch contract is
        # carried by the .cmd sibling instead, asserted for every OS below
        # (skills reference bin/harness, which Git Bash runs fine on Windows;
        # harness.cmd is the cmd.exe-side entry the tests themselves use).
        if os.name != "nt":
            self.assertTrue(wrapper.stat().st_mode & 0o111,
                            "bin/harness not executable")
        self.assertTrue((ROOT / "bin" / "harness.cmd").is_file(),
                        "bin/harness.cmd (Windows sibling) missing")

    def test_wrapper_falls_back_to_system_python_and_still_runs(self):
        import os
        import subprocess
        # NO_COLOR, not just relying on capture_output's non-tty pipe: a
        # FORCE_COLOR in the calling environment (argparse's colorizer
        # honors it even off a tty, Python 3.13+) would otherwise ANSI-wrap
        # this output and break the plain-text assertion below.
        proc = subprocess.run([str(support.HARNESS_BIN), "--help"],
                              capture_output=True, text=True, encoding="utf-8", timeout=30,
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
        wrapper = support.HARNESS_BIN
        with tempfile.TemporaryDirectory() as tmp:
            for args in (["--workspace", tmp, "--run", tmp, "show"],
                        ["show", "--workspace", tmp, "--run", tmp]):
                proc = subprocess.run([str(wrapper), *args], capture_output=True,
                                      text=True, encoding="utf-8", timeout=30,
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
        text = (ROOT / "agents" / "planner.md").read_text(encoding="utf-8")
        self.assertIn("never write `.meta.json` or run `repo-map-stamp`", text)

    def test_plan_contract_points_the_planner_at_the_repo_map(self):
        """Usage-review finding: steps/plan.md owns the map's freshness and
        the README sells the map as what the planner grounds its plans in,
        yet plan-task.md — the plan-mode content contract — never mentioned
        the map at all, so consumption happened only by model initiative
        (some e2e plans cited it, some didn't). The grounding step must
        stay present, must carry the map's path, and must keep freshness/
        stamping on the orchestrator's side of the line. Intake's ask
        carries the same path so the intake planner isn't left guessing
        where "the repo-map" lives."""
        steps = ROOT / "skills" / "dev-workflow" / "steps"
        plan_task = (steps / "plan-task.md").read_text(encoding="utf-8")
        self.assertIn(".claude/context/repo-map/", plan_task)
        self.assertIn("never run `repo-map-check`/`repo-map-stamp`",
                      plan_task)
        intake = (steps / "intake.md").read_text(encoding="utf-8")
        self.assertIn(".claude/context/repo-map/", intake)

    def test_plan_step_zero_checks_base_freshness_and_names_the_remedy(self):
        """adversarial review: 21 tests covered the CLI and the library, and
        NOT ONE pinned the plan-time wiring — deleting step 0a wholesale left
        the whole suite green, while step 0a *is* the half of the fix that
        moves the question before the plan gate. The repo-map bullet above is
        the same pattern for the same reason: the map's grounding half was
        also once documented-only and drifted.

        preflight measures the same staleness, but by then the plan is
        ratified and the branch is already cut from the stale tip — so plan.md
        naming `base-check`, and naming a remedy that terminates, is the
        mechanism, not a nicety."""
        steps = ROOT / "skills" / "dev-workflow" / "steps"
        plan = (steps / "plan.md").read_text(encoding="utf-8")
        self.assertIn("harness base-check", plan)
        self.assertIn("harness update-base", plan)
        # the flag must be clearable, and the clear must be confirmable
        self.assertIn("resolved", plan)
        # …and it must come BEFORE the planner spawn. Re-verification finding:
        # the first version of this test passed with the whole step-0a block
        # moved verbatim to the END of the file — after the spawn and after
        # `cursor --to plan-review` — which pins the words while leaving the
        # actual finding (an ORDERING one: check the base before the planner
        # grounds itself in it) entirely unenforced.
        self.assertLess(plan.index("harness base-check"),
                        plan.index("Spawn `planner`"),
                        "base-check must be documented BEFORE the planner "
                        "spawn — after it, the plan is already grounded")
        preflight = (steps / "preflight.md").read_text(encoding="utf-8")
        self.assertIn("harness update-base", preflight)

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
            text = f.read_text(encoding="utf-8")
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
            (ROOT / "pipeline" / "manifest.yaml").read_text(encoding="utf-8"))
        gate_steps = {sid for sid, s in manifest["steps"].items()
                      if s.get("gate")}  # all gates share steps/gate.md
        referenced = {s for seq in manifest["modes"].values() for s in seq}
        referenced |= {s for g in (manifest.get("groups") or {}).values()
                       for s in g["steps"]}
        missing = [s for s in sorted(referenced - gate_steps)
                   if not (ROOT / "skills" / "dev-workflow" / "steps"
                           / f"{s}.md").is_file()]
        self.assertFalse(missing, f"manifest steps without a step file: {missing}")

    def test_agent_run_suites_resolve_the_test_command_through_the_verb(self):
        """Every step that hands an agent a command to run a repo's suite
        must build it with `resolve-test-cmd`, never by reading
        `language.repos.<name>.test_cmd` out of config.

        adversarial-review, re-verified: the quarantine mechanism applied to
        the suites the HARNESS runs (verify-red/green) while develop,
        review-task, pre-pr-review and harden handed agents a raw config
        value — so the reviewer re-running the suite still hit the
        pre-existing failure and issued CHANGES_REQUESTED, which is the exact
        field loop the quarantine exists to end. Reverting any of those doc
        edits used to leave the whole suite green."""
        steps = ROOT / "skills" / "dev-workflow" / "steps"
        for name in ("develop.md", "harden.md", "pre-pr-review.md",
                     "plan-task.md"):
            text = (steps / name).read_text(encoding="utf-8")
            self.assertIn("resolve-test-cmd", text,
                          f"{name} must resolve the test command through "
                          "`harness resolve-test-cmd` (quarantine-aware)")
        # review-task.md runs the header verbatim rather than resolving it
        self.assertIn("VERBATIM",
                      (steps / "review-task.md").read_text(encoding="utf-8"))

    def test_the_direct_branch_fallback_is_scoped_to_a_quiet_repo(self):
        """The M5 lane policy (one task at a time per repo) made the
        direct-branch worktree fallback safe; round 3's pipelined dispatch
        SUPERSEDED that policy and left the offer unscoped. Executed
        (whole-system review, round 4): with T1 on a direct branch in the
        shared checkout, T2's `merge-task` refuses — HEAD is not the feature
        branch — and both develop.md step 5 and SKILL.md tell the loop that a
        MergePreconditionError naming the feature branch means "a sibling is
        mid-flight, wait and re-run the IDENTICAL command". It never
        succeeds. Prose is the only place this can be scoped, so pin it."""
        text = (ROOT / "skills" / "dev-workflow" / "steps"
                / "develop.md").read_text(encoding="utf-8").replace("\r", "")
        step1 = text.split("## Per task", 1)[1].split("\n2. ", 1)[0]
        self.assertIn("direct-branch fallback", step1)
        for needed in ("no sibling task in that repo is non-terminal",
                       "ready-tasks", "LIVELOCK"):
            self.assertIn(needed, step1,
                          "develop.md must scope the direct-branch fallback "
                          "to a repo with no live sibling, and say why")

    def test_the_wait_vs_stall_triage_asks_an_owned_verb(self):
        """"Which lane is still running" is `show`'s `outstanding_spawns`.
        SKILL.md used to send the orchestrator to the TAIL of events.ndjson
        for it — the hand-derivation owned verbs exist to prevent, and the
        one that cannot attribute a pending to a task at all."""
        skill = (ROOT / "skills" / "dev-workflow"
                 / "SKILL.md").read_text(encoding="utf-8").replace("\r", "")
        develop = (ROOT / "skills" / "dev-workflow" / "steps"
                   / "develop.md").read_text(encoding="utf-8").replace("\r", "")
        for name, text in (("SKILL.md", skill), ("develop.md", develop)):
            self.assertIn("outstanding_spawns", text,
                          f"{name} must point at the owned verb")
        # …and in SKILL.md's stall triage the owned verb comes FIRST; the
        # events tail is only for the two block-shaped kinds after it
        stalls = skill.split("- **Stalls:**", 1)[1].split("\n- ", 1)[0]
        self.assertLess(stalls.index("outstanding_spawns"),
                        stalls.index("events.ndjson`:"),
                        "the ledger read must not precede the owned verb")
        # …and the spawn-launch step points at the SAME verb. It used to say
        # "the events-tail triage below covers spawn-pending", which named a
        # triage that no longer starts there (round-4 review).
        launch = skill.split("do not `stall`", 1)[1].split("Read the verdict",
                                                           1)[0]
        self.assertIn("outstanding_spawns", launch)
        self.assertNotIn("events-tail triage", skill)

    def test_the_stall_triage_covers_the_same_step_and_unclearable_cases(self):
        """Two branches the round-4 triage could not express, both executed.
        (1) `requires_tasks_terminal` pins the cursor at develop while a lane
        is wedged, so a dead pending and a live one BOTH read `step:
        develop` — only `at` separates them, and the old prose had no branch
        for it while the surviving lane's branch said "WAIT" forever.
        (2) repo-map / request-triage pendings match no `stall` key at all
        (`stall_key_spawn_modes`), so the abandon instruction cleared
        nothing and cost the run a stall counter."""
        stalls = ((ROOT / "skills" / "dev-workflow" / "SKILL.md")
                  .read_text(encoding="utf-8").replace("\r", "")
                  .split("- **Stalls:**", 1)[1].split("\n- ", 1)[0])
        # the entry shape the triage reads must name both new fields
        for field in ("at", "clearable", "clearing_key"):
            self.assertIn(field, stalls, f"the triage must read `{field}`")
        # the same-step branch: diagnosed by AGE, not by a fixed threshold
        self.assertIn("OUTLIER", stalls)
        self.assertNotIn("minutes", stalls, "no fixed staleness threshold")
        # the unclearable branch routes to LEAVE IT, never to an override
        unclearable = stalls.split("`clearable: false`", 1)[1].split("- **",
                                                                     1)[0]
        self.assertIn("leave it", unclearable.lower())
        self.assertNotIn("--confirm-no-verdict", unclearable)
        # …and the upgrade-window record has somewhere to be read
        self.assertIn("legacy_spawn_pendings", stalls)

    def test_the_dispatch_picture_prose_matches_the_entry_shape(self):
        """develop.md scopes the direct-branch fallback per REPO and names
        `ready-tasks` as the check; the verb carries `repo` on every entry
        for exactly that, so the prose must describe the shape it will
        actually receive."""
        from harness import workflow
        text = ((ROOT / "skills" / "dev-workflow" / "steps" / "develop.md")
                .read_text(encoding="utf-8").replace("\r", ""))
        picture = workflow.dispatch_picture(
            {"tasks": [{"id": "T1", "status": "pending", "repo": "api"},
                       {"id": "T2", "status": "in-progress", "repo": "web"},
                       {"id": "T3", "status": "done", "repo": "api"},
                       {"id": "T4", "status": "pending", "repo": "web",
                        "depends_on": ["T2"]}]})
        for bucket in ("ready", "in_flight", "blocked", "terminal"):
            self.assertTrue(all("repo" in e for e in picture[bucket]), bucket)
        self.assertEqual(picture["ready"], [{"id": "T1", "repo": "api"}])
        self.assertEqual(picture["terminal"], [{"id": "T3", "repo": "api"}])
        self.assertEqual([e["repo"] for e in picture["in_flight"]], ["web"])
        self.assertEqual([e["repo"] for e in picture["blocked"]], ["web"])
        step1 = text.split("## Per task", 1)[1].split("\n2. ", 1)[0]
        self.assertIn("each entry's own `repo`", step1)

    def test_gate_md_disposition_example_matches_the_manifest(self):
        # gate.md shows the human a numbered security-gate option list; the
        # CLI resolves numbers against the manifest's declared dispositions
        # — the two must agree or the human's "2" means the wrong thing
        import yaml
        manifest = yaml.safe_load(
            (ROOT / "pipeline" / "manifest.yaml").read_text(encoding="utf-8"))
        declared = manifest["steps"]["approve-security"]["dispositions"]
        text = (ROOT / "skills" / "dev-workflow" / "steps" / "gate.md").read_text(encoding="utf-8")
        shown = re.findall(r"\[(\d)\]\s*([a-z-]+)", text)
        self.assertTrue(shown, "gate.md no longer shows the numbered options")
        for num, name in shown:
            self.assertEqual(
                declared[int(num) - 1], name,
                f"gate.md shows [{num}] {name} but manifest dispositions "
                f"are {declared}")


if __name__ == "__main__":
    unittest.main()
