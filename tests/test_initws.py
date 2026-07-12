"""M7 done-criterion: a fresh workspace goes init -> dev-workflow with NO
hand-edited config — plus discovery, verification gates, per-section refresh,
permissions, repo-map staleness, and the status dashboard."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from harness import gitops, initws
from tests.test_gitops import TEST_CMD, make_repo
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
                          "from_branch": "experiment"})
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


if __name__ == "__main__":
    unittest.main()
