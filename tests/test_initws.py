"""M7 done-criterion: a fresh workspace goes init -> dev-workflow with NO
hand-edited config — plus discovery, verification gates, per-section refresh,
permissions, repo-map staleness, and the status dashboard."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from harness import gitops, initws
from tests.test_gitops import TEST_CMD, make_monorepo, make_repo
from tests import support

ROOT = Path(__file__).resolve().parent.parent
HARNESS_BIN = support.HARNESS_BIN  # bin/harness, or its .cmd sibling on Windows


class M7Harness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.workspace)

    def tearDown(self):
        support.rmtree(self.workspace)

    def cli(self, *args, expect=0):
        """Invokes the real bin/harness launcher, from the workspace's own
        directory — same as a real /init-workspace session, and NOT the
        repo root — so a regression in the launcher's own module
        resolution (it must work from any caller cwd) fails a test here
        instead of shipping unnoticed."""
        proc = subprocess.run(
            [str(HARNESS_BIN), "--workspace", str(self.workspace), *args],
            cwd=self.workspace, capture_output=True, text=True, encoding="utf-8", timeout=300)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"{args} -> {payload} {proc.stderr}")
        return payload


class Discovery(M7Harness):
    def test_python_repo_proposes_pytest(self):
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pyproject")
        out = self.cli("discover", "--repo", str(self.repo))
        langs = {p["language"] for p in out["proposals"]}
        self.assertIn("python", langs)
        self.assertIsNone(out["monorepo_split"])

    def test_python_repo_also_proposes_a_coverage_cmd(self):
        # adversarial-review finding: harden.md told agents to "run the
        # coverage tool (language-config)" but no coverage_cmd key existed
        # anywhere in defaults or discovery — the step was only executable
        # by improvisation.
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pyproject")
        out = self.cli("discover", "--repo", str(self.repo))
        proposal = next(p for p in out["proposals"] if p["language"] == "python")
        self.assertEqual(proposal["coverage_cmd"], "python3 -m pytest --cov")

    def test_rust_repo_proposes_no_coverage_cmd_guess(self):
        # No widely-agreed built-in coverage convention for cargo — the key
        # is absent rather than a guessed, likely-wrong command.
        (self.repo / "Cargo.toml").write_text("[package]\nname='x'\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add cargo")
        out = self.cli("discover", "--repo", str(self.repo))
        proposal = next(p for p in out["proposals"] if p["language"] == "rust")
        self.assertNotIn("coverage_cmd", proposal)

    def _discover_node(self, pkg: dict):
        (self.repo / "package.json").write_text(json.dumps(pkg))
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "node")
        out = self.cli("discover", "--repo", str(self.repo))
        return next(p for p in out["proposals"] if p["language"] == "node")

    def test_node_coverage_script_wins(self):
        # A static `npm run coverage` guess can propose a script the repo
        # doesn't have — proposals are evidence-based. A real coverage
        # script is the strongest evidence.
        p = self._discover_node({"scripts": {
            "test": "vitest run", "coverage": "vitest run --coverage"}})
        self.assertEqual(p["coverage_cmd"], "npm run coverage")

    def test_node_vitest_with_provider_proposes_coverage_flag(self):
        # no coverage script, but vitest + an installed @vitest/coverage-*
        # provider prove `--coverage` will work
        p = self._discover_node({
            "scripts": {"test": "vitest run"},
            "devDependencies": {"vitest": "^4", "@vitest/coverage-v8": "^4"}})
        self.assertEqual(p["coverage_cmd"], "npm test -- --coverage")

    def test_node_without_evidence_proposes_nothing(self):
        # vitest WITHOUT a provider (the flag would just error), and no
        # coverage script: absent key, not a likely-wrong guess
        p = self._discover_node({"scripts": {"test": "vitest run"},
                                 "devDependencies": {"vitest": "^4"}})
        self.assertNotIn("coverage_cmd", p)

    def test_java_jacoco_in_pom_is_detection_not_guessing(self):
        # java stays un-guessed in the static table, but jacoco named in
        # the pom is repo EVIDENCE (field finding: a repo with jacoco
        # configured got no proposal at all)
        (self.repo / "pom.xml").write_text("<project><artifactId>x"
                                           "</artifactId></project>")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "pom")
        out = self.cli("discover", "--repo", str(self.repo))
        p = next(x for x in out["proposals"] if x["language"] == "java")
        self.assertNotIn("coverage_cmd", p)
        (self.repo / "pom.xml").write_text(
            "<project><plugin><groupId>org.jacoco</groupId>"
            "<artifactId>jacoco-maven-plugin</artifactId></plugin></project>")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "jacoco")
        out = self.cli("discover", "--repo", str(self.repo))
        p = next(x for x in out["proposals"] if x["language"] == "java")
        self.assertEqual(p["coverage_cmd"], "mvn -q test jacoco:report")

    def test_monorepo_split_proposed(self):
        (self.repo / "api").mkdir()
        (self.repo / "api" / "pyproject.toml").write_text("[project]\n")
        (self.repo / "web").mkdir()
        (self.repo / "web" / "package.json").write_text("{}")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add markers")
        out = self.cli("discover", "--repo", str(self.repo))
        self.assertEqual(out["monorepo_split"], ["api", "web"])

    def test_build_output_package_json_excluded_from_monorepo_split(self):
        """A generated build-output package.json (e.g. Nuxt/Nitro's
        `.output/server/package.json`) must not be counted as a second
        logical repo alongside the real one at the root."""
        (self.repo / "package.json").write_text("{}")
        (self.repo / ".output" / "server").mkdir(parents=True)
        (self.repo / ".output" / "server" / "package.json").write_text("{}")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add build output")
        out = self.cli("discover", "--repo", str(self.repo))
        self.assertIsNone(out["monorepo_split"])
        roots = {p["root"] for p in out["proposals"]}
        self.assertEqual(roots, {"."})

    def test_maven_wrapper_preferred_over_bare_mvn(self):
        """A repo with its own ./mvnw must get a proposed test_cmd that
        doesn't depend on mvn being installed system-wide."""
        (self.repo / "pom.xml").write_text("<project/>\n")
        mvnw = self.repo / "mvnw"
        mvnw.write_text("#!/bin/sh\nexec true\n")
        mvnw.chmod(0o755)
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pom + wrapper")
        out = self.cli("discover", "--repo", str(self.repo))
        java = next(p for p in out["proposals"] if p["language"] == "java")
        self.assertEqual(java["test_cmd"], "sh mvnw -q test")

    def test_maven_wrapper_preferred_even_without_exec_bit(self):
        """A wrapper committed without +x (common from a non-git checkout)
        is still usable via `sh` — existence is the real signal, not the
        executable bit."""
        (self.repo / "pom.xml").write_text("<project/>\n")
        (self.repo / "mvnw").write_text("#!/bin/sh\nexec true\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pom + non-exec wrapper")
        out = self.cli("discover", "--repo", str(self.repo))
        java = next(p for p in out["proposals"] if p["language"] == "java")
        self.assertEqual(java["test_cmd"], "sh mvnw -q test")

    def test_bare_mvn_proposed_without_wrapper(self):
        (self.repo / "pom.xml").write_text("<project/>\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pom only")
        out = self.cli("discover", "--repo", str(self.repo))
        java = next(p for p in out["proposals"] if p["language"] == "java")
        self.assertEqual(java["test_cmd"], "mvn -q test")

    def _discover_dotnet(self, files: dict):
        """Write a .NET-shaped tree, commit it, and return the discover
        payload. Values are file CONTENT; a `.csproj` needs real text
        because coverage detection reads it."""
        for rel, body in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "dotnet")
        out = self.cli("discover", "--repo", str(self.repo))
        return out, [p for p in out["proposals"] if p["language"] == "dotnet"]

    CSPROJ = "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n"

    def test_dotnet_solution_is_one_repo_not_one_per_project(self):
        """THE reason .NET can't reuse the per-file MARKERS path: an
        ordinary solution has a `.csproj` per project, so matching those
        would propose five logical repos — each with its own `dotnet test`
        — for one buildable unit, leaving the user to undo the split."""
        out, dotnet = self._discover_dotnet({
            "App.sln": "Microsoft Visual Studio Solution File\n",
            "src/App.API/App.API.csproj": self.CSPROJ,
            "src/App.Core/App.Core.csproj": self.CSPROJ,
            "tests/App.Tests/App.Tests.csproj": self.CSPROJ})
        self.assertEqual(len(dotnet), 1)
        self.assertEqual(dotnet[0]["root"], ".")
        # names the solution: a bare `dotnet test` is ambiguous the moment a
        # second solution file appears beside it
        self.assertEqual(dotnet[0]["test_cmd"], "dotnet test App.sln")
        self.assertIsNone(out["monorepo_split"])

    def test_dotnet_nested_solutions_collapse_to_outermost(self):
        """A root solution beside a nested one is still one buildable unit;
        proposing both would nest one logical repo inside another."""
        out, dotnet = self._discover_dotnet({
            "All.sln": "solution\n",
            "tools/Tools.sln": "solution\n",
            "tools/Gen/Gen.csproj": self.CSPROJ})
        self.assertEqual([p["root"] for p in dotnet], ["."])

    def test_dotnet_without_solution_collapses_to_common_ancestor(self):
        """No solution file (SDK-style repos sometimes skip it): sibling
        projects must still collapse rather than fan out. The ancestor holds
        no project file itself, so it gets a root but NO test_cmd — see
        test_dotnet_ambiguous_root_gets_no_test_cmd_guess for why guessing
        one there wedges the task rather than failing at init."""
        out, dotnet = self._discover_dotnet({
            "src/App.API/App.API.csproj": self.CSPROJ,
            "src/App.Tests/App.Tests.csproj": self.CSPROJ})
        self.assertEqual(len(dotnet), 1)
        self.assertEqual(Path(dotnet[0]["root"]).as_posix(), "src")
        self.assertNotIn("test_cmd", dotnet[0])

    def test_dotnet_slnx_recognised_as_a_solution(self):
        """.NET 9's XML solution format — a repo that has migrated to it
        must not fall through to the no-solution csproj path."""
        out, dotnet = self._discover_dotnet({
            "App.slnx": "<Solution />\n",
            "src/App.API/App.API.csproj": self.CSPROJ})
        self.assertEqual([p["root"] for p in dotnet], ["."])

    COVERLET = ("<Project><ItemGroup><PackageReference "
                "Include=\"coverlet.collector\" Version=\"6.0.0\" />"
                "</ItemGroup></Project>\n")

    def test_dotnet_without_coverlet_proposes_no_coverage(self):
        """Same bar as the jacoco and vitest rules: propose a coverage
        command only where the repo proves it works. Kept a separate test
        from the positive case — sharing one repo across both halves left
        `App.sln` on disk for the second, so it re-ran the solution path
        while reading as the no-solution one (adversarial review)."""
        out, dotnet = self._discover_dotnet({
            "App.sln": "solution\n",
            "tests/App.Tests/App.Tests.csproj": self.CSPROJ})
        self.assertNotIn("coverage_cmd", dotnet[0])

    def test_dotnet_coverlet_is_detection_not_guessing(self):
        out, dotnet = self._discover_dotnet({
            "App.sln": "solution\n",
            "tests/App.Tests/App.Tests.csproj": self.COVERLET})
        self.assertEqual(dotnet[0]["coverage_cmd"],
                         'dotnet test App.sln --collect:"XPlat Code Coverage"')

    def test_dotnet_coverage_evidence_confined_to_its_own_root(self):
        """The rejecting direction of _dotnet_coverage's subtree filter,
        which nothing else exercises: one solution's coverlet reference must
        not justify a coverage proposal for a DIFFERENT solution."""
        out, dotnet = self._discover_dotnet({
            "backend/B.sln": "solution\n",
            "backend/src/B/B.csproj": self.CSPROJ,
            "tools/T.sln": "solution\n",
            "tools/Gen/Gen.csproj": self.COVERLET})
        by_root = {p["root"]: p for p in dotnet}
        self.assertEqual(set(by_root), {"backend", "tools"})
        self.assertNotIn("coverage_cmd", by_root["backend"])
        self.assertIn("coverage_cmd", by_root["tools"])

    def test_dotnet_csproj_outside_any_solution_is_swallowed(self):
        """The precedence rule's ACCEPTED COST, pinned. Adversarial review
        mutated _dotnet_roots to also return project dirs outside every
        solution subtree and all other tests stayed green — the losing half
        of the rule was unpinned. `standalone/` is deliberately invisible,
        and its coverlet evidence deliberately discarded."""
        out, dotnet = self._discover_dotnet({
            "backend/B.sln": "solution\n",
            "backend/src/B/B.csproj": self.CSPROJ,
            "standalone/Tool/Tool.csproj": self.COVERLET})
        self.assertEqual([p["root"] for p in dotnet], ["backend"])
        self.assertNotIn("coverage_cmd", dotnet[0])

    def test_dotnet_ambiguous_root_gets_no_test_cmd_guess(self):
        """`dotnet test` resolves a project/solution in its OWN directory
        and refuses when there are none (MSB1003) or several (MSB1011) —
        both exit 1 with `dotnet` on PATH, which init-verify's invocability
        gate reports as PASS. A guessed command therefore survives init and
        detonates at verify-red, which accepts any non-zero exit and seals a
        red-proof over a BUILD error that verify-green can never clear.
        Omit the key instead (adversarial review, both lenses)."""
        # no solution: the common ancestor holds no project file itself
        out, dotnet = self._discover_dotnet({
            "src/App/App.csproj": self.CSPROJ,
            "tests/App.Tests/App.Tests.csproj": self.CSPROJ})
        self.assertEqual(dotnet[0]["root"], ".")
        self.assertNotIn("test_cmd", dotnet[0])
        self.assertNotIn("coverage_cmd", dotnet[0])

    def test_dotnet_sln_beside_slnx_is_ambiguous_not_guessed(self):
        """The .NET 9 migration state this round added `.slnx` for: two
        solution files in one directory is MSB1011, not a free choice."""
        out, dotnet = self._discover_dotnet({
            "App.sln": "solution\n", "App.slnx": "<Solution />\n",
            "src/App/App.csproj": self.CSPROJ})
        self.assertEqual(dotnet[0]["root"], ".")
        self.assertNotIn("test_cmd", dotnet[0])

    def test_dotnet_target_with_spaces_is_quoted(self):
        """The proposal is run through a shell (`shell=True` in
        gitops._run_tests), so an unquoted `dotnet test My App.sln` would
        split into two arguments and fail as MSB1011/MSB1003 — the exact
        class _dotnet_command exists to prevent."""
        out, dotnet = self._discover_dotnet({"My App.sln": "solution\n",
                                             "src/App/App.csproj": self.CSPROJ})
        self.assertEqual(dotnet[0]["test_cmd"], 'dotnet test "My App.sln"')

    def test_dotnet_single_project_repo_names_its_csproj(self):
        """No solution but one project in the root: nameable, so it gets a
        command — naming the file is what removes the MSB1011 class."""
        out, dotnet = self._discover_dotnet({"App.Tests.csproj": self.CSPROJ})
        self.assertEqual(dotnet[0]["test_cmd"], "dotnet test App.Tests.csproj")

    def test_dotnet_msbuild_coverlet_gets_no_proposal(self):
        """`coverlet.msbuild` drives coverage through
        `/p:CollectCoverage=true`, NOT `--collect:"XPlat Code Coverage"` —
        the collector's absence must mean no proposal, not the wrong flag
        (the empty-report failure mode is silent)."""
        out, dotnet = self._discover_dotnet({
            "tests/App.Tests/App.Tests.csproj":
                "<Project><ItemGroup><PackageReference "
                "Include=\"coverlet.msbuild\" Version=\"6.0.0\" />"
                "</ItemGroup></Project>\n"})
        self.assertNotIn("coverage_cmd", dotnet[0])

    def test_dotnet_build_output_excluded_from_discovery(self):
        """`bin`/`obj` hold copies and generated project files; counting
        them would invent roots that are not hand-authored subprojects."""
        out, dotnet = self._discover_dotnet({
            "App.sln": "solution\n",
            "src/App.API/App.API.csproj": self.CSPROJ,
            "src/App.API/bin/Release/App.API.csproj": self.CSPROJ,
            "src/App.API/obj/Stale.sln": "solution\n"})
        self.assertEqual([p["root"] for p in dotnet], ["."])

    def test_dotnet_alongside_node_proposes_monorepo_split(self):
        """The field shape this round exists for: a .NET backend beside a
        Node frontend used to discover as frontend-only, so the backend
        silently got no test command and the pipeline never ran its tests."""
        out, dotnet = self._discover_dotnet({
            "backend/App.sln": "solution\n",
            "backend/src/App.API/App.API.csproj": self.CSPROJ,
            "frontend/package.json": "{}"})
        self.assertEqual(out["monorepo_split"], ["backend", "frontend"])
        self.assertEqual([p["root"] for p in dotnet], ["backend"])
        self.assertEqual({p["language"] for p in out["proposals"]},
                         {"dotnet", "node"})

    def test_switches_to_default_branch_and_reflects_its_state(self):
        """A repo left on a non-default branch must be scanned on the
        DEFAULT branch's state, not whatever branch it was left on."""
        gitops.run_git(self.repo, "checkout", "-b", "experiment")
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "commit", "-m", "add pyproject on experiment")
        out = self.cli("discover", "--repo", str(self.repo))
        langs = {p["language"] for p in out["proposals"]}
        self.assertNotIn("python", langs)          # main never had this file
        self.assertEqual(out["default_branch"], "main")
        self.assertEqual(out["branch_check"],
                         {"switched": True, "branch": "main",
                          "from_branch": "experiment", "behind": None})
        self.assertEqual(
            gitops.run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_refuses_on_uncommitted_changes_not_crash(self):
        (self.repo / "untracked.txt").write_text("dirty\n")
        out = self.cli("discover", "--repo", str(self.repo), expect=1)
        self.assertIn("uncommitted", out["error"])

    def test_branch_override_when_auto_guess_would_be_wrong(self):
        """A repo whose real default is `master` (no origin to resolve it)
        would have the auto-guess ("main") fail closed — the escape hatch
        is an explicit --branch."""
        master_repo = self.workspace / "master-repo"
        gitops.run_git(self.workspace, "init", "-b", "master", "master-repo")
        gitops.run_git(master_repo, "config", "user.email", "t@t")
        gitops.run_git(master_repo, "config", "user.name", "t")
        (master_repo / "README.md").write_text("x\n")
        gitops.run_git(master_repo, "add", "-A")
        gitops.run_git(master_repo, "commit", "-m", "init")
        out = self.cli("discover", "--repo", str(master_repo), expect=1)
        self.assertIn("does not exist locally", out["error"])
        out = self.cli("discover", "--repo", str(master_repo), "--branch", "master")
        self.assertEqual(out["default_branch"], "master")


class VerificationGates(M7Harness):
    def test_fresh_workspace_end_to_end_no_hand_edits(self):
        """THE M7 criterion: init -> verify -> fetch -> walk, config
        entirely tool-written."""
        stories = self.workspace / "stories"
        stories.mkdir()
        (stories / "W-1.md").write_text(
            "# W-1: thing\nType: Task\nStatus: Open\n\n## Description\nd\n")
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        out = self.cli("init-verify")
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["pyyaml"], "pass")
        self.assertEqual(statuses["work-item provider"], "pass")
        self.assertEqual(statuses["repos"], "pass")
        self.assertEqual(statuses["repo:repo"], "pass")
        self.assertEqual(statuses["test_cmd:repo"], "pass")
        # permissions written, mergeable, non-destructive
        settings = json.loads((self.workspace / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("Bash(python3 -m harness:*)",
                      settings["permissions"]["allow"])
        # and the pipeline starts with zero hand edits:
        run = Path(self.cli("fetch", "--id", "W-1", "--date", "2026-03-01")["run"])
        self.cli("--run", str(run), "cursor", "--to", "intake")

    def test_pythonpath_does_not_leak_into_target_repo_test_cmd(self):
        """bin/harness sets PYTHONPATH so `python -m harness` resolves
        regardless of caller cwd — that must not leak into subprocess
        commands this CLI runs IN the target repo (test_cmd here, security
        scans elsewhere), or it silently splices ai-sdlc-harness's own import
        path into commands that have nothing to do with it (e.g. corrupting
        a Python target repo's own pytest run via namespace-package
        collisions)."""
        marker = self.workspace / "pythonpath-marker.txt"
        # python probe, not `printenv … ; true` — runnable on every OS, and
        # it writes the marker itself instead of relying on POSIX redirects
        probe_cmd = (
            f'"{sys.executable}" -c '
            f'"import os, pathlib; pathlib.Path(r\'{marker}\')'
            f".write_text(os.environ.get('PYTHONPATH', ''))\"")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={probe_cmd}")
        self.cli("init-verify")
        self.assertEqual(marker.read_text(encoding="utf-8"), "")

    def test_verify_fails_closed_on_bad_config(self):
        self.cli("init", "--stories-dir", str(self.workspace / "nope"),
                 "--repo", f"repo={self.workspace / 'not-a-repo'}",
                 "--test-cmd", "repo=definitely-not-a-command-xyz")
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        # "nope" gets auto-created by write_section (see AutoCreateStoriesDir)
        # rather than failing this check — no longer a viable bad-config
        # vector for stories_dir specifically.
        self.assertEqual(statuses["work-item provider"], "pass")
        self.assertEqual(statuses["repo:repo"], "fail")
        self.assertEqual(statuses["test_cmd:repo"], "fail")
        self.assertTrue(all(c["remediation"] for c in out["checks"]
                            if c["status"] == "fail"))

    def test_runnable_test_cmd_with_nonzero_exit_passes_without_notfound_remediation(self):
        """Validation-walk F1a: init-verify gates test_cmd on INVOCABILITY
        only (126/127), never the suite's exit code — a suite may legitimately
        be red at init (TDD red state, pre-existing failures). A runnable
        command that exits non-zero is a deliberate PASS, so it must NOT carry
        the misleading `command not found` remediation it used to emit next to
        `exit N`."""
        stories = self.workspace / "stories"
        stories.mkdir()
        # A runnable-everywhere command that exits 2 — not a not-found shape
        # on either the POSIX (126/127) or Windows (exit 1 + unresolvable
        # first token) branch. The interpreter running this suite is the one
        # binary guaranteed present; double quotes parse in cmd.exe AND sh
        # (the old `sh -c 'exit 2'` fixture relied on single-quote handling
        # cmd.exe doesn't have, so on Windows sh got mangled args and the
        # asserted exit code was wrong).
        exit2 = f'"{sys.executable}" -c "raise SystemExit(2)"'
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}",
                 "--test-cmd", f"repo={exit2}")
        out = self.cli("init-verify")
        check = next(c for c in out["checks"] if c["check"] == "test_cmd:repo")
        self.assertEqual(check["status"], "pass")
        self.assertIn("exit 2", check["detail"])
        self.assertIn("not gated at init", check["detail"])
        self.assertEqual(check["remediation"], "")
        self.assertNotIn("command not found", check["remediation"])

    @unittest.skipUnless(os.name == "nt", "cmd.exe first-token resolution")
    def test_repo_local_red_runner_is_runnable_not_notfound(self):
        """Adversarial-review finding on the Windows not-found gate: the
        first-token check anchored to the harness PROCESS cwd, so a
        repo-local runner (`./run-tests.cmd`) that runs and exits 1 — a
        legitimately red suite, this check's own documented pass case —
        was misclassified `command not found` and blocked init-finalize."""
        (self.repo / "run-tests.cmd").write_text("@exit /b 1\r\n",
                                                 encoding="ascii")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}",
                 "--test-cmd", r"repo=.\run-tests.cmd")  # cmd.exe spelling
        out = self.cli("init-verify")
        check = next(c for c in out["checks"] if c["check"] == "test_cmd:repo")
        self.assertEqual(check["status"], "pass")
        self.assertIn("exit 1", check["detail"])   # the runner genuinely ran
        self.assertIn("not gated at init", check["detail"])

    def test_first_token_resolver_units(self):
        # direct units for the Windows invocability probe (the function is
        # platform-neutral even though only the nt branch consults it):
        # cmd builtins resolve; a repo-local runner resolves only via the
        # cwd the command actually ran in; garbage doesn't resolve.
        self.assertTrue(initws._first_token_resolves("pushd sub && npm test"))
        self.assertFalse(
            initws._first_token_resolves("definitely-not-a-command-xyz"))
        local = Path(tempfile.mkdtemp())
        self.addCleanup(support.rmtree, local, ignore_errors=True)
        (local / "runner.cmd").write_text("@exit /b 1\r\n", encoding="ascii")
        self.assertFalse(initws._first_token_resolves("./runner.cmd"))
        self.assertTrue(initws._first_token_resolves("./runner.cmd", local))

    def test_zero_repos_fails_verify_instead_of_emitting_no_checks(self):
        """Adversarial-review finding: an empty `repos` map used to emit
        zero repo:<name>/test_cmd:<name> checks — an absence of failures,
        not a pass — so init-verify silently reported ok:true for a
        workspace /dev-workflow can't do anything with (e.g. after a
        full-replace `init-section --section repos` call wipes every repo
        by mistake)."""
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown",
                                          "stories_dir": str(self.workspace / "s")}}))
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["repos"], "fail")

    def test_unset_stories_dir_fails_verify_not_false_passes(self):
        """Adversarial-review finding: Path("") is Path("."), and
        Path(".").is_dir() is True — a config that FORGOT stories_dir
        passed the provider check and then hunted for stories in whatever
        cwd the process had."""
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown"}}))
        self.cli("init-section", "--section", "repos",
                 "--json", json.dumps({"repos": {"repo": str(self.repo)}}))
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["work-item provider"], "fail")

    def test_github_provider_requires_explicit_repo_target(self):
        # auth alone isn't enough: without provider.github_repo the adapter
        # would resolve the forge repo from cwd (wrong-issue risk) — the
        # runtime now refuses, and verify catches it earlier, where fixing
        # config is cheap.
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "github"}}))
        self.cli("init-section", "--section", "repos",
                 "--json", json.dumps({"repos": {"repo": str(self.repo)}}))
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["github_repo"], "fail")

    def test_github_projects_requires_both_board_coordinates(self):
        # a board is addressed by owner AND number; either half missing is
        # unusable, and the reachability probe is only meaningful once both
        # are present (so it must not run — or report — before then)
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "github-projects"}}))
        self.cli("init-section", "--section", "repos",
                 "--json", json.dumps({"repos": {"repo": str(self.repo)}}))
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["github_project"], "fail")
        self.assertNotIn("github_project reachable", statuses)
        remediation = next(c["remediation"] for c in out["checks"]
                           if c["check"] == "github_project")
        self.assertIn("github_project_owner", remediation)

    def test_mcp_provider_is_manual_check(self):
        self.cli("init-section", "--section", "provider",
                 "--json", '{"provider": {"work_item": "jira"}}')
        self.cli("init-section", "--section", "repos",
                 "--json", json.dumps({"repos": {"repo": str(self.repo)}}))
        out = self.cli("init-verify", expect=1)   # test_cmd:repo still missing
        wi = next(c for c in out["checks"] if c["check"] == "work-item provider")
        self.assertEqual(wi["status"], "manual")
        self.assertIn("MCP integration checklist", wi["detail"])

    def test_per_section_refresh_touches_one_file(self):
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        lang_before = (self.workspace / ".claude/context/language.yaml").read_text(encoding="utf-8")
        self.cli("init-section", "--section", "provider", "--json",
                 '{"provider": {"work_item": "github", "git": "github"}}')
        self.assertEqual((self.workspace / ".claude/context/language.yaml")
                         .read_text(encoding="utf-8"), lang_before)      # untouched
        self.assertIn("github",
                      (self.workspace / ".claude/context/provider.yaml").read_text(encoding="utf-8"))

    def test_init_finalize_writes_permissions_and_marker(self):
        """The interview flow (init-section per piece) does not get
        permissions/bootstrap-marker for free — init-finalize is the
        explicit step that writes them, only once verify has passed."""
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown",
                                          "git": "local",
                                          "stories_dir": str(stories)}}))
        self.cli("init-section", "--section", "repos", "--json",
                 json.dumps({"repos": {"repo": str(self.repo)}}))
        self.cli("init-section", "--section", "language", "--json",
                 json.dumps({"language": {"repos": {"repo": {"test_cmd": TEST_CMD}}}}))
        self.cli("init-verify")

        settings_path = self.workspace / ".claude" / "settings.json"
        overrides_path = self.workspace / ".claude" / "context" / "overrides.yaml"
        self.assertFalse(settings_path.exists())
        self.assertFalse(overrides_path.exists())

        self.cli("init-finalize")

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        self.assertIn("Bash(python3 -m harness:*)", allow)
        self.assertIn(f"Bash({TEST_CMD.split()[0]}:*)", allow)
        # Re-review finding: the rule must be the LITERAL, UNEXPANDED
        # string every skill instructs the model to type — permission
        # matching does no env-var expansion, so a resolved absolute path
        # alone matches nothing a skill-following model actually runs.
        self.assertIn("Bash(${CLAUDE_PLUGIN_ROOT}/bin/harness:*)", allow)
        skill_invocation = "${CLAUDE_PLUGIN_ROOT}/bin/harness fetch --id X-1"
        literal_prefixes = [r[len("Bash("):-len(":*)")] for r in allow
                            if r.startswith("Bash(") and r.endswith(":*)")]
        self.assertTrue(any(skill_invocation.startswith(p)
                            for p in literal_prefixes),
                        "no allow rule prefix-matches the literal command "
                        "shape skill files instruct the model to run")
        overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
        self.assertIn("bootstrap_completed", overrides)

    def test_init_finalize_preserves_prior_overrides(self):
        """mark_bootstrapped must merge into overrides.yaml, not clobber it
        — a user's step-3 `--section overrides` write must survive finalize."""
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"quick_mode": {"loc_threshold": 50}}))
        self.cli("init-finalize")
        overrides = yaml.safe_load(
            (self.workspace / ".claude" / "context" / "overrides.yaml")
            .read_text(encoding="utf-8"))
        self.assertEqual(overrides["quick_mode"], {"loc_threshold": 50})
        self.assertIn("bootstrap_completed", overrides)

    def test_overrides_section_merges_across_calls(self):
        """Two independent --section overrides calls (e.g. status_mapping
        set in one pass, quick_mode in another) must accumulate, not
        clobber each other — the repeatable, one-setting-at-a-time usage
        step 3 of the skill actually documents."""
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"status_mapping": {"default": {"Open": "todo"}}}))
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"quick_mode": {"loc_threshold": 50}}))
        overrides = yaml.safe_load(
            (self.workspace / ".claude" / "context" / "overrides.yaml")
            .read_text(encoding="utf-8"))
        self.assertEqual(overrides["status_mapping"],
                         {"default": {"Open": "todo"}})
        self.assertEqual(overrides["quick_mode"], {"loc_threshold": 50})

    def test_overrides_merge_preserves_sibling_nested_keys(self):
        """Adversarial-review finding (both lenses reproduced it): a
        targeted write to ONE nested key of an already-set top-level
        override (e.g. security.scan_cmd.backend) must not silently drop
        a SIBLING nested key (scan_cmd.frontend) set by an earlier call —
        the shallow {**existing, **data} merge this replaced did exactly
        that. This is the shape /workspace-config's whole pitch relies on:
        repeated, single-setting edits to the same top-level key over time."""
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"security": {"scan_cmd": {"backend": "bandit ."}}}))
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"security": {"scan_cmd": {"frontend": "eslint ."}}}))
        overrides = yaml.safe_load(
            (self.workspace / ".claude" / "context" / "overrides.yaml")
            .read_text(encoding="utf-8"))
        self.assertEqual(overrides["security"]["scan_cmd"],
                         {"backend": "bandit .", "frontend": "eslint ."})

    def test_init_finalize_refuses_when_verify_fails(self):
        """init-finalize must not mark a half-configured workspace
        bootstrapped just because someone skipped straight past init-verify
        — it re-checks itself rather than trusting the skill's prose order.
        Uses an unrecognized provider (not local-markdown) to fail the
        work-item check, since a missing stories_dir no longer fails it —
        write_section now auto-creates it (see AutoCreateStoriesDir)."""
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "not-a-real-provider"}}))
        out = self.cli("init-finalize", expect=1)
        self.assertFalse(out["ok"])
        self.assertFalse((self.workspace / ".claude" / "settings.json").exists())
        self.assertFalse((self.workspace / ".claude" / "context" / "overrides.yaml")
                         .exists())

    def test_init_section_rejects_non_dict_json(self):
        """A bare JSON array/scalar for --json must be rejected up front —
        writing it would land in a section file that _deep_merge later
        calls .items() on, bricking every subsequent CLI call with a raw
        AttributeError instead of a clean error."""
        out = self.cli("init-section", "--section", "overrides",
                       "--json", "[1, 2, 3]", expect=1)
        self.assertFalse(out["ok"])
        self.assertFalse((self.workspace / ".claude" / "context" / "overrides.yaml")
                         .exists())
        # and the CLI is still usable afterward
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"quick_mode": {"loc_threshold": 50}}))


class SubtreeRepoRegistration(M7Harness):
    """init-verify's repo gate probes membership of a git WORK TREE, not
    `(path/".git").exists()`. The old test could only ever pass a checkout
    ROOT — which is exactly the registration `discover()`'s `monorepo_split`
    cannot produce, so every split it proposed verified as a failure."""

    def test_verify_passes_a_subtree_and_names_the_physical_checkout(self):
        mono = make_monorepo(self.workspace)
        stories = self.workspace / "stories"
        stories.mkdir()
        # parent AND child registered together — the legal, required overlap
        # (a .NET `.sln` at the root plus a frontend app under it)
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"front={mono / 'frontend'}",
                 "--repo", f"mono={mono}",
                 "--test-cmd", f"front={support.NOP_CMD}",
                 "--test-cmd", f"mono={support.NOP_CMD}")
        out = self.cli("init-verify")
        checks = {c["check"]: c for c in out["checks"]}
        self.assertEqual(checks["repo:front"]["status"], "pass")
        # the report is honest about the shared checkout: the registered path
        # is not where `.git` is, and that relationship is what the direct-
        # branch refusal and `add -A` scoping both hinge on
        self.assertIn("subtree of", checks["repo:front"]["detail"])
        self.assertIn(str(mono), checks["repo:front"]["detail"])
        self.assertEqual(checks["repo:mono"]["status"], "pass")
        self.assertNotIn("subtree of", checks["repo:mono"]["detail"])
        self.assertTrue(out["ok"])

    def test_a_discovered_monorepo_split_is_registrable_end_to_end(self):
        """The point of the change: `discover()` already PROPOSES subtree
        roots, and until now none of them could be registered — the repo
        gate rejected every one. Proposal -> repos.yaml -> init-verify, with
        each logical repo carrying its own test_cmd."""
        mono = make_monorepo(self.workspace)
        (mono / "frontend" / "package.json").write_text('{"name": "f"}\n')
        (mono / "backend" / "pyproject.toml").write_text("[project]\nname='b'\n")
        gitops.run_git(mono, "add", "-A")
        gitops.run_git(mono, "commit", "-m", "markers")
        out = self.cli("discover", "--repo", str(mono))
        self.assertEqual(out["monorepo_split"], ["backend", "frontend"])
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 *[a for root in out["monorepo_split"]
                   for a in ("--repo", f"{root}={mono / root}")],
                 *[a for root in out["monorepo_split"]
                   for a in ("--test-cmd", f"{root}={support.NOP_CMD}")])
        verified = self.cli("init-verify")
        statuses = {c["check"]: c["status"] for c in verified["checks"]}
        self.assertEqual(statuses["repo:frontend"], "pass")
        self.assertEqual(statuses["repo:backend"], "pass")
        self.assertEqual(statuses["test_cmd:frontend"], "pass")
        self.assertTrue(verified["ok"])

    def test_verify_still_fails_a_path_outside_any_checkout(self):
        plain = self.workspace / "plain"
        plain.mkdir()
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={plain}", "--test-cmd", f"repo={support.NOP_CMD}")
        out = self.cli("init-verify", expect=1)
        check = next(c for c in out["checks"] if c["check"] == "repo:repo")
        self.assertEqual(check["status"], "fail")
        self.assertIn("git checkout", check["remediation"])

    # ------------------------- the checkout must not swallow the workspace

    def _cli_in(self, workspace: Path, *args, expect=0):
        """`M7Harness.cli` pins --workspace to the fixture root; the
        containment hazard needs a workspace NESTED inside a checkout, which
        that root can never be."""
        proc = subprocess.run(
            [str(HARNESS_BIN), "--workspace", str(workspace), *args],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8",
            timeout=300)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"{args} -> {payload} {proc.stderr}")
        return payload

    def test_verify_refuses_a_checkout_that_contains_the_workspace(self):
        """The exact-equality refusal above catches "the workspace IS the
        repo". The gate's new qualifying condition is CONTAINMENT, so the
        hazard widened with it: workspace at `<checkout>/ws`, registration at
        `<checkout>/code/myapp`, one `.git` over both. Pre-fix that verified
        `pass` — and then preflight's `ensure_default_branch` probed dirt at
        the toplevel, which now contains the live run's own `ws/ai/<run>/**`,
        and refused permanently naming the harness's own state files.
        Committing them to clear it is worse: the `git checkout <default>`
        that follows swaps the workspace's sealed state.yaml and
        `.claude/context/**` out from under the run."""
        outer = self.workspace / "outer"
        (outer / "code" / "myapp").mkdir(parents=True)
        gitops.run_git(self.workspace, "init", "-b", "main", "outer")
        gitops.run_git(outer, "config", "user.email", "t@t")
        gitops.run_git(outer, "config", "user.name", "t")
        (outer / "code" / "myapp" / "app.py").write_text("x\n")
        gitops.run_git(outer, "add", "-A")
        gitops.run_git(outer, "commit", "-m", "init")
        ws = outer / "ws"
        stories = ws / "stories"
        stories.mkdir(parents=True)
        self._cli_in(ws, "init", "--stories-dir", str(stories),
                     "--repo", f"myapp={outer / 'code' / 'myapp'}",
                     "--test-cmd", f"myapp={support.NOP_CMD}")
        out = self._cli_in(ws, "init-verify", expect=1)
        check = next(c for c in out["checks"] if c["check"] == "repo:myapp")
        self.assertEqual(check["status"], "fail")
        self.assertIn("lives INSIDE", check["remediation"])
        self.assertIn("state.yaml", check["remediation"])
        self.assertFalse(out["ok"])

    def test_a_subtree_beside_an_outside_workspace_is_still_fine(self):
        """No-regression for the containment check: the ordinary subtree
        registration — workspace somewhere else entirely — is untouched."""
        mono = make_monorepo(self.workspace / "elsewhere")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"front={mono / 'frontend'}",
                 "--test-cmd", f"front={support.NOP_CMD}")
        checks = {c["check"]: c for c in self.cli("init-verify")["checks"]}
        self.assertEqual(checks["repo:front"]["status"], "pass")

    # ------------------------------ a subtree git can actually materialize

    def test_verify_refuses_an_untracked_subtree_registration(self):
        """Membership of a work tree is also true of an IGNORED directory,
        so a registration git can never materialize verified clean: the
        per-task `git worktree add` brings only what the branch tracks, the
        returned repo path does not exist, and every task command runs in a
        missing cwd."""
        mono = make_monorepo(self.workspace)
        (mono / ".gitignore").write_text("generated/\n")
        (mono / "generated").mkdir()
        (mono / "generated" / "app.py").write_text("g\n")
        gitops.run_git(mono, "add", "-A")
        gitops.run_git(mono, "commit", "-m", "ignore generated")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"gen={mono / 'generated'}",
                 "--test-cmd", f"gen={support.NOP_CMD}")
        out = self.cli("init-verify", expect=1)
        check = next(c for c in out["checks"] if c["check"] == "repo:gen")
        self.assertEqual(check["status"], "fail")
        self.assertIn("not in its index", check["remediation"])
        self.assertIn("worktree", check["remediation"])
        # the detail still names the checkout — the reader needs to know
        # WHICH index the path is missing from
        self.assertIn("subtree of", check["detail"])

    def test_an_empty_root_registration_still_passes(self):
        """No-regression for the tracked-subtree probe: a freshly-init'd
        checkout with an empty index is a legitimate root registration and
        must not be caught by a check aimed at subtrees."""
        fresh = self.workspace / "fresh"
        fresh.mkdir()
        gitops.run_git(self.workspace, "init", "-b", "main", "fresh")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"fresh={fresh}", "--test-cmd", f"fresh={support.NOP_CMD}")
        checks = {c["check"]: c for c in self.cli("init-verify")["checks"]}
        self.assertEqual(checks["repo:fresh"]["status"], "pass")
        self.assertEqual(checks["repo:fresh"]["detail"], str(fresh))


class QwenCompatibility(M7Harness):
    """Qwen Code reads permissions and env from `.qwen/settings.json`
    (Claude reads `.claude/settings.json`) and its installer rewrites
    `.claude/`→`.qwen/` in skill markdown. Under `QWEN_CODE=1`,
    init-workspace mirrors the allowlist + exports CLAUDE_PLUGIN_ROOT into
    `.qwen/settings.json` and symlinks `.qwen/context`→`../.claude/context`
    so model writes through the rewritten path land in the single physical
    tree. Claude Code sessions (no QWEN_CODE) are untouched."""

    def test_write_permissions_mirrors_to_qwen_settings_under_qwen(self):
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.write_permissions(self.workspace, {"r": str(self.repo)},
                                     {"r": {"test_cmd": TEST_CMD}})
        qwen_path = self.workspace / ".qwen" / "settings.json"
        self.assertTrue(qwen_path.exists())
        settings = json.loads(qwen_path.read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        # same rules as .claude/settings.json, including the literal token
        self.assertIn("Bash(${CLAUDE_PLUGIN_ROOT}/bin/harness:*)", allow)
        self.assertIn(f"Bash({TEST_CMD.split()[0]}:*)", allow)
        # the env export that makes runtime-generated block-message
        # `${CLAUDE_PLUGIN_ROOT}/...` invocations runnable under Qwen
        self.assertIn("CLAUDE_PLUGIN_ROOT", settings["env"])

    def test_write_permissions_skips_qwen_settings_without_qwen_code(self):
        # Claude Code path is byte-identical: no .qwen/ tree written.
        # Strip QWEN_CODE from the env — the suite itself may run inside a
        # Qwen Code session that sets it in the process env.
        env = {k: v for k, v in os.environ.items() if k != "QWEN_CODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            initws.write_permissions(self.workspace, {"r": str(self.repo)},
                                     {"r": {"test_cmd": TEST_CMD}})
        self.assertFalse((self.workspace / ".qwen").exists())
        self.assertTrue((self.workspace / ".claude" / "settings.json").exists())

    def test_qwen_settings_merge_is_non_destructive(self):
        # a pre-existing .qwen/settings.json (e.g. user env keys) must be
        # merged, not clobbered — same read-modify-write discipline as the
        # .claude path.
        qwen_path = self.workspace / ".qwen" / "settings.json"
        qwen_path.parent.mkdir(parents=True, exist_ok=True)
        qwen_path.write_text(json.dumps({
            "env": {"MY_KEY": "kept"},
            "permissions": {"allow": ["Bash(echo:*)"]}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.write_permissions(self.workspace, {"r": str(self.repo)},
                                     {"r": {"test_cmd": TEST_CMD}})
        settings = json.loads(qwen_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["MY_KEY"], "kept")
        self.assertIn("Bash(echo:*)", settings["permissions"]["allow"])
        self.assertIn(f"Bash({TEST_CMD.split()[0]}:*)",
                      settings["permissions"]["allow"])

    def test_qwen_settings_env_export_preserves_user_pin_pointing_at_real_dir(self):
        # a user (or prior init) who pinned CLAUDE_PLUGIN_ROOT at a path that
        # still exists on disk wins — the self-heal only overwrites a
        # DANGLING value, never a live one. Here the pinned path is the
        # workspace itself (guaranteed to exist), so it's preserved.
        qwen_path = self.workspace / ".qwen" / "settings.json"
        qwen_path.parent.mkdir(parents=True, exist_ok=True)
        qwen_path.write_text(json.dumps(
            {"env": {"CLAUDE_PLUGIN_ROOT": str(self.workspace)}}),
            encoding="utf-8")
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.write_permissions(self.workspace, {}, {})
        settings = json.loads(qwen_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["CLAUDE_PLUGIN_ROOT"],
                         str(self.workspace))

    def test_qwen_settings_env_export_self_heals_when_stored_path_dangles(self):
        # the failure mode Gap 1 exists to prevent: a prior init wrote the
        # plugin root, the plugin was later reinstalled/moved, and the stored
        # path no longer exists. The self-heal overwrites it with the current
        # root so the block-message recovery path stays runnable. Silent on
        # the happy path (a routine reinstall is expected, not a signal).
        qwen_path = self.workspace / ".qwen" / "settings.json"
        qwen_path.parent.mkdir(parents=True, exist_ok=True)
        stale = "/definitely/not/a/real/path/anymore"
        qwen_path.write_text(json.dumps(
            {"env": {"CLAUDE_PLUGIN_ROOT": stale}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.write_permissions(self.workspace, {}, {})
        settings = json.loads(qwen_path.read_text(encoding="utf-8"))
        plugin_root = str(Path(initws.__file__).resolve().parent.parent)
        self.assertEqual(settings["env"]["CLAUDE_PLUGIN_ROOT"], plugin_root)

    @unittest.skipUnless(support.can_symlink(),
                         "host cannot create symlinks (Windows: Developer "
                         "Mode/admin); degrade path tested separately below")
    def test_mark_bootstrapped_symlinks_qwen_context_under_qwen(self):
        # bootstrap the context dir first (write_section creates it)
        initws.write_section(self.workspace, "provider",
                             {"provider": {"work_item": "local-markdown",
                                           "git": "local",
                                           "stories_dir": "stories"}})
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.mark_bootstrapped(self.workspace)
        link = self.workspace / ".qwen" / "context"
        target = self.workspace / ".claude" / "context"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), target.resolve())
        # the link target is RELATIVE (../.claude/context), not absolute —
        # a relative link survives a workspace move/rename where an absolute
        # one would dangle. readlink returns the stored target verbatim.
        self.assertEqual(os.readlink(link), os.path.join("..", ".claude", "context"))
        # a write through the rewritten .qwen/context path lands in the
        # physical .claude/context tree
        (link / "witness.txt").write_text("ok", encoding="utf-8")
        self.assertTrue((target / "witness.txt").exists())

    def test_mark_bootstrapped_no_symlink_without_qwen_code(self):
        initws.write_section(self.workspace, "provider",
                             {"provider": {"work_item": "local-markdown",
                                           "git": "local",
                                           "stories_dir": "stories"}})
        env = {k: v for k, v in os.environ.items() if k != "QWEN_CODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            initws.mark_bootstrapped(self.workspace)
        self.assertFalse((self.workspace / ".qwen" / "context").exists())

    def test_link_qwen_context_does_not_clobber_real_file(self):
        # adversarial-review finding (CONFIRMED): the original `unlink()`
        # succeeded on a regular file at `.qwen/context`, silently deleting
        # user data. Only a symlink (a stale/wrong link) is replaced; a real
        # file or directory the user placed there is left alone.
        # Consistent-visibility hardening: the occupied branch has the SAME
        # broken round-trip as a refused symlink (no link → .qwen/context
        # writes lost), so it warns too — the outcome must not be silent.
        initws.write_section(self.workspace, "provider",
                             {"provider": {"work_item": "local-markdown",
                                           "git": "local",
                                           "stories_dir": "stories"}})
        link = self.workspace / ".qwen" / "context"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("user-owned marker file", encoding="utf-8")
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}), \
                mock.patch("sys.stderr") as stderr:
            initws.mark_bootstrapped(self.workspace)
        # the file survives — not clobbered, not turned into a symlink
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(encoding="utf-8"),
                         "user-owned marker file")
        # and the user is told the round-trip is broken (not silent)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("exists as a real", written)
        self.assertIn(".qwen/context", written)

    @unittest.skipUnless(support.can_symlink(),
                         "host cannot create symlinks (Windows: Developer "
                         "Mode/admin); degrade path tested separately below")
    def test_link_qwen_context_repoints_stale_symlink(self):
        # a stale/wrong symlink IS replaced — that's the documented
        # idempotency contract (re-run after the target moved).
        initws.write_section(self.workspace, "provider",
                             {"provider": {"work_item": "local-markdown",
                                           "git": "local",
                                           "stories_dir": "stories"}})
        link = self.workspace / ".qwen" / "context"
        link.parent.mkdir(parents=True, exist_ok=True)
        elsewhere = self.workspace / "elsewhere"
        elsewhere.mkdir()
        link.symlink_to(elsewhere, target_is_directory=True)
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            initws.mark_bootstrapped(self.workspace)
        target = self.workspace / ".claude" / "context"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), target.resolve())

    def test_link_qwen_context_warns_when_symlinks_unavailable(self):
        # adversarial-review finding (MEDIUM, converged): when the host
        # refuses symlinks, the failure must NOT be silent — without the
        # symlink the guard_write dual-prefix allows a planner context
        # write the CLI can never read (silent data loss). A stderr warning
        # names the requirement. Simulate symlink refusal by patching
        # symlink_to to raise OSError (mock.patch.object restores it).
        initws.write_section(self.workspace, "provider",
                             {"provider": {"work_item": "local-markdown",
                                           "git": "local",
                                           "stories_dir": "stories"}})
        from pathlib import Path
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}), \
                mock.patch.object(Path, "symlink_to",
                                  side_effect=OSError("no privilege")), \
                mock.patch("sys.stderr") as stderr:
            initws.mark_bootstrapped(self.workspace)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("could not symlink .qwen/context", written)
        # and no symlink exists (creation failed)
        self.assertFalse(
            (self.workspace / ".qwen" / "context").is_symlink())


class LauncherExecBitSelfHeal(unittest.TestCase):
    """hooks/run-guard's header + guards.py's docstring: a mode-stripping
    distribution channel (GitHub "Download ZIP", a zip-extraction library
    that drops unix modes, a manual Windows->POSIX copy) can deliver
    hooks/run-guard or bin/harness non-executable — bash's `exec` clause
    then fails 126 with no fallback, the platform reads that non-2 exit as
    a NON-BLOCKING hook error, and every guard (including the fail-closed
    spawn/skill guards) silently stops enforcing. `mark_bootstrapped` (the
    one call both a fresh `init` and an `init-finalize` re-run funnel
    through) self-heals the bit via `_restore_launcher_exec_bits`, which
    takes an explicit `plugin_root` so these tests exercise a disposable
    fixture copy and never chmod the real repo's own launcher files."""

    def _fixture_root(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(support.rmtree, root, ignore_errors=True)
        for rel in initws._LAUNCHER_FILES:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("#!/usr/bin/env bash\necho stub\n", encoding="utf-8")
        return root

    @unittest.skipIf(os.name == "nt",
                     "POSIX executable bit has no Windows meaning — the "
                     "function is a documented no-op there")
    def test_restores_stripped_exec_bit(self):
        root = self._fixture_root()
        for rel in initws._LAUNCHER_FILES:
            (root / rel).chmod(0o644)   # strip the bit, as a mode-stripping channel would
        initws._restore_launcher_exec_bits(plugin_root=root)
        for rel in initws._LAUNCHER_FILES:
            mode = (root / rel).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, f"{rel} not user-executable")
            self.assertTrue(mode & stat.S_IXGRP, f"{rel} not group-executable")
            self.assertTrue(mode & stat.S_IXOTH, f"{rel} not other-executable")

    def test_already_correct_tree_changes_nothing_and_emits_nothing(self):
        root = self._fixture_root()
        if os.name != "nt":
            for rel in initws._LAUNCHER_FILES:
                (root / rel).chmod(0o755)
        before = {rel: (root / rel).stat().st_mode
                 for rel in initws._LAUNCHER_FILES}
        with mock.patch("sys.stderr") as stderr:
            initws._restore_launcher_exec_bits(plugin_root=root)
        for rel in initws._LAUNCHER_FILES:
            self.assertEqual((root / rel).stat().st_mode, before[rel])
        stderr.write.assert_not_called()

    @unittest.skipIf(os.name == "nt",
                     "chmod is never reached on Windows (early return), so "
                     "there is no failure path to warn about there")
    def test_chmod_failure_warns_and_does_not_raise(self):
        root = self._fixture_root()
        for rel in initws._LAUNCHER_FILES:
            (root / rel).chmod(0o644)   # needs a real fix, so chmod is reached
        with mock.patch("os.chmod", side_effect=OSError("permission denied")), \
                mock.patch("sys.stderr") as stderr:
            initws._restore_launcher_exec_bits(plugin_root=root)   # must not raise
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        for rel in initws._LAUNCHER_FILES:
            self.assertIn(rel, written)

    def test_mark_bootstrapped_calls_the_self_heal(self):
        """Wiring check: mark_bootstrapped is the one call both a fresh init
        and an init-finalize re-run funnel through, so the self-heal must
        run from there, unconditionally, on every call — not just on the
        interview's happy path."""
        with mock.patch.object(initws, "_restore_launcher_exec_bits") as heal:
            workspace = Path(tempfile.mkdtemp())
            self.addCleanup(support.rmtree, workspace, ignore_errors=True)
            initws.write_section(workspace, "provider",
                                 {"provider": {"work_item": "local-markdown",
                                               "git": "local",
                                               "stories_dir": "stories"}})
            initws.mark_bootstrapped(workspace)
        heal.assert_called_once_with()


class AutoCreateStoriesDir(M7Harness):
    """A valid-looking provider config naming a stories_dir that doesn't
    exist yet must not need a separate init-verify round-trip to discover
    that — write_section creates it as a side effect of the write, the
    same way it already creates its own .claude/context/ storage dir."""

    def test_init_section_creates_stories_dir(self):
        stories = self.workspace / "stories"
        self.assertFalse(stories.exists())
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown",
                                          "stories_dir": str(stories)}}))
        self.assertTrue(stories.is_dir())

    def test_one_shot_init_creates_stories_dir(self):
        stories = self.workspace / "brand-new-stories-dir"
        self.assertFalse(stories.exists())
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        self.assertTrue(stories.is_dir())
        out = self.cli("init-verify")
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["work-item provider"], "pass")

    def test_non_local_markdown_provider_does_not_auto_create(self):
        """The auto-create is specific to local-markdown's stories_dir
        field — an unrelated provider must not have random paths created
        on its behalf."""
        phantom = self.workspace / "should-not-exist"
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "github",
                                          "stories_dir": str(phantom)}}))
        self.assertFalse(phantom.exists())

    def test_rerun_with_different_stories_dir_creates_new_leaves_old(self):
        first = self.workspace / "first-stories"
        second = self.workspace / "second-stories"
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown",
                                          "stories_dir": str(first)}}))
        self.assertTrue(first.is_dir())
        self.cli("init-section", "--section", "provider", "--json",
                 json.dumps({"provider": {"work_item": "local-markdown",
                                          "stories_dir": str(second)}}))
        self.assertTrue(second.is_dir())
        self.assertTrue(first.is_dir())   # lingers, harmless — nothing re-reads it
        # expect=1: this test never registers a repo, so the "repos" check
        # correctly fails overall verify — the point of this test is only
        # that "work-item provider" itself still passes after the re-run.
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["work-item provider"], "pass")

    def test_refuses_cleanly_on_non_dict_provider_value(self):
        out = self.cli("init-section", "--section", "provider", "--json",
                       '{"provider": "oops"}', expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("not a mapping", out["error"])

    def test_refuses_cleanly_on_non_string_stories_dir(self):
        out = self.cli("init-section", "--section", "provider", "--json",
                       json.dumps({"provider": {"work_item": "local-markdown",
                                                "stories_dir": ["a", "b"]}}),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("must be a string", out["error"])

    def test_refuses_cleanly_when_stories_dir_is_an_existing_file(self):
        """stories_dir naming an existing non-directory (a plausible real
        mistake — local-markdown workspaces are full of .md files) must
        fail with a clean error, not an uncaught OSError."""
        blocker = self.workspace / "stories-but-a-file"
        blocker.write_text("not a directory\n")
        out = self.cli("init-section", "--section", "provider", "--json",
                       json.dumps({"provider": {"work_item": "local-markdown",
                                                "stories_dir": str(blocker)}}),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("could not create stories_dir", out["error"])


class MultiRepoLanguageConfig(M7Harness):
    """Per-repo language-config: repos with different toolchains each get
    their own registered test command, checked and allow-listed independently."""

    def test_two_repos_different_test_cmds_verified_independently(self):
        repo_b = make_repo(self.workspace, "repo-b")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--repo", f"repo-b={repo_b}",
                 "--test-cmd", f"repo={TEST_CMD}",
                 "--test-cmd", f"repo-b={support.NOP_CMD}")
        out = self.cli("init-verify")
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["test_cmd:repo"], "pass")
        self.assertEqual(statuses["test_cmd:repo-b"], "pass")

        lang = yaml.safe_load(
            (self.workspace / ".claude/context/language.yaml").read_text(encoding="utf-8"))
        self.assertEqual(lang["language"]["repos"]["repo"]["test_cmd"], TEST_CMD)
        self.assertEqual(lang["language"]["repos"]["repo-b"]["test_cmd"],
                         support.NOP_CMD)

        # both repos' command heads allow-listed, not just one
        settings = json.loads((self.workspace / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        self.assertIn(f"Bash({TEST_CMD.split()[0]}:*)", allow)
        self.assertIn(f"Bash({support.NOP_CMD.split()[0]}:*)", allow)

    def test_missing_repo_language_entry_fails_closed(self):
        """Mutation: a registered repo with no language entry must fail
        init-verify for THAT repo specifically — the exact scenario a
        multi-repo /init-workspace run can hit if a repo's command is
        never confirmed."""
        repo_b = make_repo(self.workspace, "repo-b")
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--repo", f"repo-b={repo_b}",
                 "--test-cmd", f"repo={TEST_CMD}")   # repo-b never confirmed
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["test_cmd:repo"], "pass")
        self.assertEqual(statuses["test_cmd:repo-b"], "fail")
        remediation = next(c["remediation"] for c in out["checks"]
                           if c["check"] == "test_cmd:repo-b")
        self.assertIn("language.repos.repo-b.test_cmd", remediation)

    def test_repo_named_like_global_language_key_fails_closed_not_crash(self):
        """Regression: a repo registered as `test_paths` (colliding with
        language.yaml's global test_paths/test_closure keys) used to crash
        init-verify with an uncaught AttributeError when its test_cmd was
        never confirmed. Must fail closed through the real CLI, not raise."""
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"test_paths={self.repo}",
                 "--test-cmd", "unused=true")   # test_paths' own cmd never confirmed
        out = self.cli("init-verify", expect=1)
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["test_cmd:test_paths"], "fail")


class AddRepo(M7Harness):
    """Registering a repo after the initial interview must not require
    (or risk) re-supplying every already-registered repo by hand."""

    def _init_one_repo(self):
        stories = self.workspace / "stories"
        stories.mkdir()
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")

    def test_add_repo_preserves_existing_repos(self):
        self._init_one_repo()
        repo_b = make_repo(self.workspace, "repo-b")
        out = self.cli("add-repo", "--name", "repo-b", "--path", str(repo_b),
                       "--test-cmd", "true")
        self.assertEqual(out["added"], {"name": "repo-b", "path": str(repo_b),
                                        "test_cmd": "true"})
        repos = yaml.safe_load(
            (self.workspace / ".claude/context/repos.yaml").read_text(encoding="utf-8"))["repos"]
        self.assertEqual(repos, {"repo": str(self.repo), "repo-b": str(repo_b)})

    def test_add_repo_merges_language_entry_without_disturbing_others(self):
        self._init_one_repo()
        repo_b = make_repo(self.workspace, "repo-b")
        self.cli("add-repo", "--name", "repo-b", "--path", str(repo_b),
                 "--test-cmd", "true")
        lang = yaml.safe_load(
            (self.workspace / ".claude/context/language.yaml").read_text(encoding="utf-8"))
        self.assertEqual(lang["language"]["repos"]["repo"]["test_cmd"], TEST_CMD)
        self.assertEqual(lang["language"]["repos"]["repo-b"]["test_cmd"], "true")

    def test_add_repo_without_test_cmd_leaves_language_untouched(self):
        self._init_one_repo()
        lang_before = (self.workspace / ".claude/context/language.yaml").read_text(encoding="utf-8")
        repo_b = make_repo(self.workspace, "repo-b")
        self.cli("add-repo", "--name", "repo-b", "--path", str(repo_b))
        self.assertEqual(
            (self.workspace / ".claude/context/language.yaml").read_text(encoding="utf-8"),
            lang_before)

    def test_add_repo_refuses_duplicate_name(self):
        self._init_one_repo()
        out = self.cli("add-repo", "--name", "repo", "--path", str(self.repo),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("already registered", out["error"])
        # points at the owned entry point, never at a raw file edit (RC1)
        self.assertIn("init-section --section repos", out["error"])
        self.assertNotIn("edit repos.yaml directly", out["error"])
        repos = yaml.safe_load(
            (self.workspace / ".claude/context/repos.yaml").read_text(encoding="utf-8"))["repos"]
        self.assertEqual(repos, {"repo": str(self.repo)})   # untouched

    def test_repo_name_matches_equivalent_path_spellings(self):
        """Re-review finding: since the per-repo `branches`/`pr` artifact
        keying, repo_name must return a STABLE name across separate CLI
        invocations even when they spell the same repo differently
        (relative vs absolute, `..` segments) — a spelling drift used to
        silently fork the artifact key and drop the recorded base."""
        self._init_one_repo()
        from harness import initws
        from harness.cli import load_declared
        _, _, config = load_declared(self.workspace)
        exact = initws.repo_name(config, str(self.repo))
        self.assertEqual(exact, "repo")
        dotted = self.repo.parent / "." / self.repo.name
        self.assertEqual(initws.repo_name(config, str(dotted)), "repo")
        upped = self.repo / ".." / self.repo.name
        self.assertEqual(initws.repo_name(config, str(upped)), "repo")
        self.assertIsNone(initws.repo_name(config, str(self.workspace / "nope")))

    def test_add_repo_refuses_case_insensitive_duplicate_name(self):
        """Two names differing only by case would collide in repo-map's
        on-disk directories on a case-insensitive filesystem (default
        macOS) — refuse rather than silently corrupting both."""
        self._init_one_repo()
        repo_b = make_repo(self.workspace, "repo-b")
        out = self.cli("add-repo", "--name", "Repo", "--path", str(repo_b),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("already registered", out["error"])

    def test_add_repo_refuses_duplicate_path_under_new_name(self):
        """Registering the same path under a second name would silently
        misattribute config, since name->path resolution elsewhere
        (_repo_name) matches by path and returns the first name found."""
        self._init_one_repo()
        out = self.cli("add-repo", "--name", "repo-alias",
                       "--path", str(self.repo), expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("already registered as 'repo'", out["error"])
        repos = yaml.safe_load(
            (self.workspace / ".claude/context/repos.yaml").read_text(encoding="utf-8"))["repos"]
        self.assertEqual(repos, {"repo": str(self.repo)})   # untouched

    def test_init_verify_catches_a_hand_edited_workspace_root_repo(self):
        # Re-review finding: write_section's write-time refusal doesn't
        # cover a config that PREDATES the fix or was hand-edited past it —
        # init-verify must re-check the invariant, not report ok:true while
        # the `git add -A` authority-file leak is still live.
        self._init_one_repo()
        (self.workspace / ".claude/context/repos.yaml").write_text(
            yaml.safe_dump({"repos": {"evil": str(self.workspace),
                                      "repo": str(self.repo)}}))
        out = self.cli("init-verify", expect=1)
        self.assertFalse(out["ok"])
        bad = next(c for c in out["checks"] if c["check"] == "repo:evil")
        self.assertEqual(bad["status"], "fail")
        self.assertIn("workspace root", bad["remediation"])

    def test_add_repo_refuses_workspace_root_as_a_repo(self):
        # adversarial-review finding: registering the workspace itself as a
        # repo would let `harness commit`'s `git add -A` stage ai/**
        # run-authority files — nothing previously stopped this.
        self._init_one_repo()
        out = self.cli("add-repo", "--name", "self", "--path", str(self.workspace),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("workspace root", out["error"])
        repos = yaml.safe_load(
            (self.workspace / ".claude/context/repos.yaml").read_text(encoding="utf-8"))["repos"]
        self.assertNotIn("self", repos)

    def test_init_section_repos_refuses_workspace_root_as_a_repo(self):
        stories = self.workspace / "stories"
        stories.mkdir()
        out = self.cli("init-section", "--section", "repos",
                       "--json", json.dumps({"repos": {"self": str(self.workspace)}}),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("workspace root", out["error"])

    def test_add_repo_refuses_non_dict_repos_key(self):
        """repos.yaml with a top-level `repos:` key that isn't itself a
        mapping (hand corruption, or a copy-paste mistake) must refuse
        cleanly, not silently discard it as `{}` or crash with a raw
        AttributeError. (A malformed top-level — the file isn't even a
        dict — is a separate, pre-existing gap in `load_declared` shared
        by every CLI verb, not something add-repo's own guard reaches;
        see docs/design.md.)"""
        ctx = self.workspace / ".claude" / "context"
        ctx.mkdir(parents=True)
        (ctx / "repos.yaml").write_text("repos: not-a-mapping\n")
        out = self.cli("add-repo", "--name", "repo", "--path", str(self.repo),
                       expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("not a mapping", out["error"])

    def test_add_repo_refuses_non_dict_language_repos_key(self):
        self._init_one_repo()
        (self.workspace / ".claude/context/language.yaml").write_text(
            "language:\n  repos: not-a-mapping\n")
        repo_b = make_repo(self.workspace, "repo-b")
        out = self.cli("add-repo", "--name", "repo-b", "--path", str(repo_b),
                       "--test-cmd", "true", expect=1)
        self.assertFalse(out["ok"])
        self.assertIn("not a mapping", out["error"])

    def test_add_repo_then_verify_and_finalize_covers_new_repo(self):
        self._init_one_repo()
        self.cli("init-verify")
        self.cli("init-finalize")
        repo_b = make_repo(self.workspace, "repo-b")
        self.cli("add-repo", "--name", "repo-b", "--path", str(repo_b),
                 "--test-cmd", support.NOP_CMD)
        out = self.cli("init-verify")
        statuses = {c["check"]: c["status"] for c in out["checks"]}
        self.assertEqual(statuses["repo:repo-b"], "pass")
        self.assertEqual(statuses["test_cmd:repo-b"], "pass")
        self.cli("init-finalize")
        settings = json.loads((self.workspace / ".claude" / "settings.json")
                              .read_text(encoding="utf-8"))
        allow = settings["permissions"]["allow"]
        self.assertIn(f"Read({repo_b}/**)", allow)
        self.assertIn(f"Bash({support.NOP_CMD.split()[0]}:*)", allow)


class ResolveRepoCommand(unittest.TestCase):
    """Direct unit tests for the shared path->name->command resolver used by
    verify-red / task --to in-review / security-scan."""

    def test_resolve_test_cmd_maps_path_to_named_entry(self):
        config = {"repos": {"backend": "/repos/backend",
                            "frontend": "/repos/frontend"},
                 "language": {"repos": {"backend": {"test_cmd": "mvn -q test"},
                                       "frontend": {"test_cmd": "npm test"}}}}
        self.assertEqual(initws.resolve_test_cmd(config, Path("/repos/backend")),
                         "mvn -q test")
        self.assertEqual(initws.resolve_test_cmd(config, Path("/repos/frontend")),
                         "npm test")

    def test_resolve_test_cmd_unregistered_path_returns_none(self):
        config = {"repos": {"backend": "/repos/backend"}, "language": {}}
        self.assertIsNone(initws.resolve_test_cmd(config, Path("/somewhere/else")))

    def test_resolve_scan_cmd_per_repo_optional(self):
        config = {"repos": {"backend": "/repos/backend",
                            "frontend": "/repos/frontend"},
                 "security": {"scan_cmd": {"backend": "mvn dependency-check:check"}}}
        self.assertEqual(initws.resolve_scan_cmd(config, Path("/repos/backend")),
                         "mvn dependency-check:check")
        self.assertIsNone(initws.resolve_scan_cmd(config, Path("/repos/frontend")))

    def test_resolve_test_cmd_no_collision_with_global_language_keys(self):
        """A repo literally named `test_paths` used to collide with
        language.yaml's existing global keys (test_paths/test_closure) when
        per-repo entries were flat siblings of them. Per-repo entries now
        live under `language.repos`, so the collision can't happen at all —
        the repo's own test_cmd resolves correctly, and the global glob list
        is untouched."""
        config = {"repos": {"test_paths": "/repos/weird"},
                 "language": {"test_paths": ["tests/**"],
                              "repos": {"test_paths": {"test_cmd": "true"}}}}
        self.assertEqual(initws.resolve_test_cmd(config, Path("/repos/weird")), "true")
        self.assertEqual(config["language"]["test_paths"], ["tests/**"])   # untouched

    def test_resolve_test_cmd_stale_flat_shape_fails_closed(self):
        """Pre-nesting `language.yaml` (per-repo entries as flat siblings,
        the old shape) must fail closed, never raise, on the new resolver."""
        config = {"repos": {"backend": "/repos/backend"},
                 "language": {"backend": {"test_cmd": "mvn -q test"}}}
        self.assertIsNone(initws.resolve_test_cmd(config, Path("/repos/backend")))

    def test_resolve_coverage_cmd_per_repo(self):
        config = {"repos": {"backend": "/repos/backend"},
                 "language": {"repos": {"backend": {
                     "test_cmd": "mvn -q test",
                     "coverage_cmd": "mvn -q test jacoco:report"}}}}
        self.assertEqual(initws.resolve_coverage_cmd(config, Path("/repos/backend")),
                         "mvn -q test jacoco:report")

    def test_resolve_coverage_cmd_unconfigured_returns_none(self):
        config = {"repos": {"backend": "/repos/backend"},
                 "language": {"repos": {"backend": {"test_cmd": "mvn -q test"}}}}
        self.assertIsNone(initws.resolve_coverage_cmd(config, Path("/repos/backend")))

    def test_resolve_scan_cmd_ignores_stale_flat_shape(self):
        """A pre-per-repo `security.scan_cmd` flat string must fail closed,
        never raise AttributeError on the old shape."""
        config = {"repos": {"backend": "/repos/backend"},
                 "security": {"scan_cmd": "echo legacy flat scanner"}}
        self.assertIsNone(initws.resolve_scan_cmd(config, Path("/repos/backend")))


class RepoMapAndStatus(M7Harness):
    def seed_map(self, name="repo", rel="index.md"):
        """Write map content the way the planner does — stamping is only
        legal after content exists (repo_map_stamp refuses otherwise)."""
        f = self.workspace / ".claude" / "context" / "repo-map" / name / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# repo map\n", encoding="utf-8")

    def test_repo_map_staleness_lifecycle(self):
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual(out["status"], "missing")
        self.seed_map()
        self.cli("repo-map-stamp", "--repo-name", "repo", "--repo", str(self.repo))
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual((out["status"], out["behind"]), ("fresh", 0))
        for i in range(3):
            (self.repo / f"f{i}.txt").write_text("x")
            gitops.run_git(self.repo, "add", "-A")
            gitops.run_git(self.repo, "commit", "-m", f"chore: c{i}")
        # tighten the threshold via config override -> stale
        (self.workspace / ".claude" / "context").mkdir(parents=True, exist_ok=True)
        (self.workspace / ".claude" / "context" / "rm.yaml").write_text(
            "repo_map:\n  stale_after_commits: 2\n")
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual((out["status"], out["behind"]), ("stale", 3))

    def test_repo_map_check_survives_a_history_rewrite(self):
        """Adversarial-review finding: a stamped SHA absent from the current
        history (force-pushed default branch, re-clone, gc) raised a raw
        `unknown revision` GitError — recovery required knowing to
        hand-delete .meta.json. That IS staleness; answer it as such."""
        self.seed_map()
        self.cli("repo-map-stamp", "--repo-name", "repo", "--repo", str(self.repo))
        meta = (self.workspace / ".claude" / "context" / "repo-map" / "repo"
                / ".meta.json")
        stamped = json.loads(meta.read_text(encoding="utf-8"))
        stamped["sha"] = "deadbeef" * 5   # a SHA this history never had
        meta.write_text(json.dumps(stamped))
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual(out["status"], "stale")
        self.assertIn("not in this history", out["note"])

    def test_repo_map_check_survives_a_corrupt_stamp(self):
        self.seed_map()
        self.cli("repo-map-stamp", "--repo-name", "repo", "--repo", str(self.repo))
        meta = (self.workspace / ".claude" / "context" / "repo-map" / "repo"
                / ".meta.json")
        meta.write_text("{truncated")
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual(out["status"], "missing")
        self.assertIn("corrupt", out["note"])

    def test_repo_map_stamp_refuses_an_empty_map(self):
        """Mutation case for the false-fresh gap: the orchestrator stamps
        unconditionally after the planner spawn returns, so a failed/empty
        spawn used to mint a stamp that repo-map-check would report 'fresh'
        for the next stale_after_commits commits — on a map that doesn't
        exist. Stamping before content is now a refusal, both when the map
        directory is absent and when it exists but holds only the stamp
        path itself."""
        out = self.cli("repo-map-stamp", "--repo-name", "repo",
                       "--repo", str(self.repo), expect=1)
        self.assertIn("no map content", out["error"])
        # an empty-but-existing directory is the same refusal
        d = self.workspace / ".claude" / "context" / "repo-map" / "repo"
        d.mkdir(parents=True)
        out = self.cli("repo-map-stamp", "--repo-name", "repo",
                       "--repo", str(self.repo), expect=1)
        self.assertIn("no map content", out["error"])
        self.assertFalse((d / ".meta.json").exists())
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual(out["status"], "missing")

    def test_repo_map_stamp_accepts_nested_only_content(self):
        """Real maps tier detail files under subdirectories (e.g.
        areas/src.md) — the content check must count recursively, or a
        legitimately generated map with no top-level file would be refused."""
        self.seed_map(rel="areas/src.md")
        out = self.cli("repo-map-stamp", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertTrue(out["ok"])
        out = self.cli("repo-map-check", "--repo-name", "repo",
                       "--repo", str(self.repo))
        self.assertEqual((out["status"], out["behind"]), ("fresh", 0))

    def test_status_dashboard_across_runs(self):
        stories = self.workspace / "stories"
        stories.mkdir()
        for sid in ("W-1", "W-2"):
            (stories / f"{sid}.md").write_text(
                f"# {sid}: t\nType: Task\nStatus: Open\n\n## Description\nd\n")
        self.cli("init", "--stories-dir", str(stories),
                 "--repo", f"repo={self.repo}", "--test-cmd", f"repo={TEST_CMD}")
        self.cli("fetch", "--id", "W-1", "--date", "2026-03-01")
        self.cli("fetch", "--id", "W-2", "--date", "2026-03-02")
        out = self.cli("status")
        self.assertEqual(len(out["runs"]), 2)
        self.assertEqual({r["work_item"] for r in out["runs"]}, {"W-1", "W-2"})
        self.assertTrue(all(r["cursor"] == "fetch" for r in out["runs"]))


class SubagentModelNotice(M7Harness):
    """WI-7: under QWEN_CODE=1, resolve-model translates non-inherit model
    overrides to inherit + notice (Qwen's agent tool has no model param),
    and init-section --section overrides emits a notice when
    subagent_models contains any non-inherit value. Under Claude Code
    (no QWEN_CODE) behavior is byte-identical to before."""

    def _write_overrides(self, sm):
        self.cli("init-section", "--section", "overrides", "--json",
                 json.dumps({"subagent_models": sm}))

    def test_resolve_model_non_inherit_under_qwen(self):
        self._write_overrides({"developer": "sonnet"})
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("resolve-model", "--shape", "developer",
                           "--mode", "develop")
        self.assertEqual(out["model"], "inherit")
        self.assertEqual(out["configured"], "sonnet")
        self.assertIn("notice", out)
        self.assertIn("Qwen Code", out["notice"])

    def test_resolve_model_inherit_under_qwen_no_notice(self):
        self._write_overrides({"developer": "inherit"})
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("resolve-model", "--shape", "developer",
                           "--mode", "develop")
        self.assertEqual(out["model"], "inherit")
        self.assertNotIn("notice", out)
        self.assertNotIn("configured", out)

    def test_resolve_model_non_inherit_under_claude_passthrough(self):
        self._write_overrides({"developer": "sonnet"})
        env = {k: v for k, v in os.environ.items() if k != "QWEN_CODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = self.cli("resolve-model", "--shape", "developer",
                           "--mode", "develop")
        self.assertEqual(out["model"], "sonnet")
        self.assertNotIn("notice", out)

    def test_init_section_overrides_non_inherit_string_under_qwen(self):
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"subagent_models": {"developer": "sonnet"}}))
        self.assertTrue(out["ok"])
        self.assertIn("notice", out)

    def test_init_section_overrides_per_mode_dict_under_qwen(self):
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"subagent_models":
                                      {"developer": {"default": "inherit", "review": "sonnet"}}}))
        self.assertTrue(out["ok"])
        self.assertIn("notice", out)

    def test_init_section_overrides_all_inherit_under_qwen_no_notice(self):
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"subagent_models":
                                      {"developer": "inherit"}}))
        self.assertTrue(out["ok"])
        self.assertNotIn("notice", out)

    def test_init_section_overrides_dict_all_inherit_under_qwen_no_notice(self):
        # the real config shape: {shape: {default: inherit}} — a per-mode
        # dict where every value IS inherit. The recursive helper must
        # see through the nesting; the flat one-level check falsely tripped
        # (a dict value is never == "inherit").
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"subagent_models":
                                      {"developer": {"default": "inherit"},
                                       "reviewer": {"default": "inherit"}}}))
        self.assertTrue(out["ok"])
        self.assertNotIn("notice", out)

    def test_init_section_overrides_no_subagent_models_under_qwen(self):
        with mock.patch.dict(os.environ, {"QWEN_CODE": "1"}):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"quick_mode": {"loc_max": 50}}))
        self.assertTrue(out["ok"])
        self.assertNotIn("notice", out)

    def test_init_section_overrides_non_inherit_under_claude_no_notice(self):
        env = {k: v for k, v in os.environ.items() if k != "QWEN_CODE"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = self.cli("init-section", "--section", "overrides", "--json",
                           json.dumps({"subagent_models": "sonnet"}))
        self.assertTrue(out["ok"])
        self.assertNotIn("notice", out)


GH_BOARD_STUB = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
board = json.loads((Path(__file__).parent / "board.json").read_text(encoding="utf-8"))
verb = " ".join(sys.argv[1:3])
if verb == "auth status":
    print("Logged in to github.com account tester")
elif verb == "project view":
    print(json.dumps({"id": "PVT_x", "number": 4,
                      "closed": board.get("closed", False)}))
elif verb == "project field-list":
    if board.get("field_list_fails"):
        sys.stderr.write("your token has not been granted read:project\n")
        sys.exit(1)
    print(json.dumps({"fields": board["fields"], "totalCount": len(board["fields"])}))
else:
    sys.stderr.write("unexpected: " + verb + "\n")
    sys.exit(1)
'''

BOARD_FIELDS = [{"id": "f1", "name": "Title", "type": "ProjectV2Field"},
                {"id": "f2", "name": "Status",
                 "type": "ProjectV2SingleSelectField",
                 "options": [{"id": "o1", "name": "Todo"},
                             {"id": "o2", "name": "Done"}]}]


class GithubProjectsVerification(unittest.TestCase):
    """The board probe is the one gate between a mistyped board-shaped
    config key and a run that degrades silently three flagged write-back
    events later (adversarial-review, both lenses)."""

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self._path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._path}"
        self.install(fields=BOARD_FIELDS)

    def tearDown(self):
        os.environ["PATH"] = self._path
        support.rmtree(self.bin)

    def install(self, **board):
        (self.bin / "board.json").write_text(json.dumps(board), encoding="utf-8")
        support.write_cli_stub(self.bin, "gh", GH_BOARD_STUB)

    def checks(self, **provider):
        config = {"provider": {"work_item": "github-projects",
                               "github_project": 4,
                               "github_project_owner": "acme", **provider},
                  "repos": {}}
        return {c["check"]: c for c in initws.verify(config)}

    def test_a_reachable_board_with_a_status_field_passes(self):
        got = self.checks()
        self.assertEqual(got["work-item provider"]["status"], "pass")
        self.assertEqual(got["github_project"]["status"], "pass")
        self.assertEqual(got["github_project reachable"]["status"], "pass")
        self.assertEqual(got["github_project status field"]["status"], "pass")

    def test_a_closed_board_is_not_reported_as_usable(self):
        # it reads perfectly and refuses every write
        self.install(fields=BOARD_FIELDS, closed=True)
        got = self.checks()
        self.assertEqual(got["github_project reachable"]["status"], "fail")
        self.assertIn("CLOSED", got["github_project reachable"]["detail"])
        self.assertNotIn("github_project status field", got)

    def test_a_mistyped_status_field_fails_and_names_the_real_fields(self):
        got = self.checks(github_project_status_field="Staus")
        self.assertEqual(got["github_project status field"]["status"], "fail")
        self.assertIn("Title, Status", got["github_project status field"]["detail"])

    def test_a_non_single_select_status_field_fails(self):
        got = self.checks(github_project_status_field="Title")
        self.assertEqual(got["github_project status field"]["status"], "fail")

    def test_a_failed_field_list_probe_is_not_reported_as_a_board_fact(self):
        # discarding the probe's ok flag made a FAILED probe read as
        # "'Status' is not a single-select field", sending the user to
        # change a correctly configured key (re-verification, finding C)
        self.install(fields=BOARD_FIELDS, field_list_fails=True)
        got = self.checks()["github_project status field"]
        self.assertEqual(got["status"], "fail")
        self.assertIn("could not list the board's fields", got["detail"])
        self.assertNotIn("is not a single-select field", got["detail"])
        self.assertIn("read:project", got["remediation"])

    def test_verify_and_the_adapter_agree_on_what_names_the_field(self):
        # one declared value, two readers — they must match it the same way
        got = self.checks(github_project_status_field="status")
        self.assertEqual(got["github_project status field"]["status"], "pass")


class AdoVerification(unittest.TestCase):
    """`ado` work-item provider verify probes `az account show`. The real
    Azure CLI installs as `az.cmd` on Windows, not `az.exe` — a bare
    subprocess exec of "az" (no shell, no PATHEXT walk) reports "not
    installed" even when `az account show` succeeds from an interactive
    shell, because Windows' CreateProcess only auto-appends `.exe` to an
    extensionless name. `write_cli_stub` always produces a real `.exe` on
    Windows, which never exercised this — so the stub here is a `.cmd`
    file, the actual shape of `az`."""

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self._path = os.environ["PATH"]
        os.environ["PATH"] = str(self.bin)  # isolated: no host az can leak in

    def tearDown(self):
        os.environ["PATH"] = self._path
        support.rmtree(self.bin)

    def install_az(self, exit_code=0, output="authenticated"):
        if os.name == "nt":
            (self.bin / "az.cmd").write_text(
                f"@echo {output}\r\n@exit /b {exit_code}\r\n", encoding="utf-8")
        else:
            script = self.bin / "az"
            script.write_text(f"#!/bin/sh\necho {output}\nexit {exit_code}\n",
                              encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def checks(self):
        config = {"provider": {"work_item": "ado"}, "repos": {}}
        return {c["check"]: c for c in initws.verify(config)}

    def test_az_cmd_shim_on_path_is_resolved_not_reported_missing(self):
        self.install_az()
        got = self.checks()["work-item provider"]
        self.assertEqual(got["status"], "pass")
        self.assertEqual(got["detail"], "authenticated")

    def test_az_not_on_path_fails_with_remediation(self):
        got = self.checks()["work-item provider"]
        self.assertEqual(got["status"], "fail")
        self.assertIn("az: not installed", got["detail"])
        self.assertIn("az login", got["remediation"])


if __name__ == "__main__":
    unittest.main()
