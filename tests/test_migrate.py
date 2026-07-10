"""/migrate-workspace seam contract — the fork-replaceable unit.

A fork swaps `harness/migrate.py` (and the skill walker) for its own
adoption logic; THESE assertions are what any replacement must still
satisfy: detect/inventory/extract signatures, read-only behavior, and —
the load-bearing one — `extract()["sections"]` payloads being accepted
VERBATIM by `initws.write_section` and coming back correct through
`cli.load_declared`. Fixture shapes come from tools/make_v21_workspace.py
(copied from a real v2.1 production workspace, one source of truth)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import gitops, initws, migrate
from harness.cli import load_declared

ROOT = Path(__file__).resolve().parent.parent
HARNESS_BIN = ROOT / "bin" / "harness"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "make_v21_workspace", ROOT / "tools" / "make_v21_workspace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _load_generator()


class V21Fixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        GEN.build(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws)

    def cli(self, *args, expect=0):
        proc = subprocess.run(
            [str(HARNESS_BIN), "--workspace", str(self.ws), *args],
            cwd=self.ws, capture_output=True, text=True, timeout=120)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"{args} -> {proc.stdout}\n{proc.stderr}")
        return payload


class Detect(V21Fixture):
    def test_fingerprints_the_v21_workspace(self):
        found = migrate.detect(self.ws)
        self.assertEqual(found["legacy"], "v2.x")
        self.assertFalse(found["already_bootstrapped"])
        self.assertEqual(found["warnings"], [])
        joined = " ".join(found["evidence"])
        self.assertIn("provider-config.md", joined)
        self.assertIn("state.md", joined)
        self.assertIn("tracker", joined)

    def test_empty_workspace_is_not_legacy(self):
        empty = Path(tempfile.mkdtemp())
        try:
            found = migrate.detect(empty)
            self.assertIsNone(found["legacy"])
            self.assertEqual(found["evidence"], [])
            self.assertFalse(found["already_bootstrapped"])
        finally:
            shutil.rmtree(empty)

    def test_reports_a_v3_bootstrap(self):
        initws.mark_bootstrapped(self.ws)
        self.assertTrue(migrate.detect(self.ws)["already_bootstrapped"])

    def test_fails_closed_on_corrupt_overrides(self):
        # Mutation case: an unreadable overrides.yaml cannot PROVE the
        # workspace isn't bootstrapped — the module must refuse-side, with
        # a warning naming the file, not fail open into a migration.
        ctx = self.ws / ".claude" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "overrides.yaml").write_text("{[not yaml", encoding="utf-8")
        found = migrate.detect(self.ws)
        self.assertTrue(found["already_bootstrapped"])
        self.assertTrue(any("overrides.yaml" in w for w in found["warnings"]))


class Inventory(V21Fixture):
    def test_flags_only_genuinely_in_flight_runs(self):
        inv = migrate.inventory(self.ws)
        self.assertEqual(inv["runs"], 3)
        self.assertEqual(inv["aborted"], ["2026-04-01-quick-fix"])
        # US-001 is all-Done — its T2 TITLE contains "In Review", which a
        # whole-row substring scan would false-flag; only US-002 (a real
        # Pending row) may appear, and the state.md active line for the
        # same story must dedupe rather than double-report.
        self.assertEqual([e["id"] for e in inv["in_flight"]], ["US-002"])
        self.assertIn("legacy_context_files", inv)
        self.assertIn("provider-config.md", inv["legacy_context_files"])

    def test_state_md_supplements_a_missing_tracker(self):
        shutil.rmtree(self.ws / "ai" / "2026-06-01-US-002")
        inv = migrate.inventory(self.ws)
        ids = [e["id"] for e in inv["in_flight"]]
        self.assertEqual(ids, ["US-002-add-multiply"])
        self.assertIn("state.md", inv["in_flight"][0]["evidence"])


class Extract(V21Fixture):
    def test_maps_providers_and_carries_settings(self):
        out = migrate.extract(self.ws)
        provider = out["sections"]["provider"]["provider"]
        self.assertEqual(provider["work_item"], "local-markdown")
        self.assertEqual(provider["git"], "gitlab")   # from `glab-cli`
        self.assertEqual(provider["stories_dir"], "./stories")
        self.assertTrue(any("glab-cli" in n for n in out["notes"]))

    def test_registers_repos_and_language(self):
        out = migrate.extract(self.ws)
        repos = out["sections"]["repos"]["repos"]
        self.assertIn("calc", repos)
        self.assertIn("ghost", repos)
        self.assertTrue(any("ghost" in n and "not a git checkout" in n
                            for n in out["notes"]))
        lang = out["sections"]["language"]["language"]["repos"]
        self.assertEqual(lang["calc"]["test_cmd"], "python3 -m unittest")
        self.assertEqual(lang["calc"]["coverage_cmd"],
                         "python3 -m coverage run -m unittest")
        self.assertEqual(lang["ghost"], {"test_cmd": "npm test"})

    def test_naming_is_optional_and_translated(self):
        out = migrate.extract(self.ws)
        self.assertNotIn("naming", out["sections"])   # never auto-applied
        naming = out["optional_overrides"]["naming"]
        self.assertEqual(naming["branch"], "feature/{id}-{slug}")
        self.assertEqual(naming["pr_title"], "[{id}] {summary}")
        joined = " ".join(out["unmapped"])
        self.assertIn("commit_format", joined)
        self.assertIn("tag_format", joined)
        self.assertIn("cost-config.md", joined)

    def test_unknown_provider_falls_through_to_the_interview(self):
        # Mutation case: a spelling this module doesn't know must yield NO
        # proposal (the interview asks) — never a guessed module name that
        # init-verify then chases as "unknown provider".
        (self.ws / ".claude" / "context" / "provider-config.md").write_text(
            "# Provider Configuration\n\n"
            "- **Work Item Provider**: `fancy-tracker`\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertNotIn("work_item",
                         out["sections"].get("provider", {}).get("provider", {}))
        self.assertTrue(any("fancy-tracker" in u for u in out["unmapped"]))

    def test_untranslatable_naming_placeholder_is_dropped(self):
        (self.ws / ".claude" / "context" / "naming-config.md").write_text(
            "# Naming Configuration\n\n"
            "branch_format: ${sprint}/${story_id}\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertEqual(out["optional_overrides"], {})
        self.assertTrue(any("branch_format" in u for u in out["unmapped"]))

    def test_extract_is_read_only(self):
        before = sorted(str(p.relative_to(self.ws))
                        for p in self.ws.rglob("*") if p.is_file())
        migrate.detect(self.ws)
        migrate.inventory(self.ws)
        migrate.extract(self.ws)
        after = sorted(str(p.relative_to(self.ws))
                       for p in self.ws.rglob("*") if p.is_file())
        self.assertEqual(before, after)


class SectionPayloadContract(V21Fixture):
    """THE seam contract: whatever a fork's extract() proposes must be
    write_section-acceptable verbatim and come back intact through
    load_declared — otherwise the skill's apply step breaks silently."""

    def test_sections_round_trip_through_the_owned_init_path(self):
        out = migrate.extract(self.ws)
        target = Path(tempfile.mkdtemp())
        try:
            for section, payload in out["sections"].items():
                initws.write_section(target, section, payload)
            if out["optional_overrides"]:
                initws.write_section(target, "overrides",
                                     out["optional_overrides"])
            _, _, config = load_declared(target)
            self.assertEqual(config["provider"]["work_item"], "local-markdown")
            self.assertEqual(config["repos"]["calc"],
                             str(self.ws / "code" / "calc"))
            self.assertEqual(
                initws.resolve_test_cmd(config, self.ws / "code" / "calc"),
                "python3 -m unittest")
            # translated naming templates must render with the exact field
            # sets the real call sites pass (gitops.render raises on drift)
            self.assertEqual(
                gitops.render(config["naming"]["branch"],
                              type="feature", id="X-1", slug="do-thing"),
                "feature/X-1-do-thing")
            self.assertEqual(
                gitops.render(config["naming"]["pr_title"],
                              type="feature", id="X-1", summary="Do thing"),
                "[X-1] Do thing")
        finally:
            shutil.rmtree(target)


class ReviewHardening(V21Fixture):
    """Regression pins for the adversarial-review findings on this change."""

    def test_legacy_run_dir_is_never_a_vacant_slot(self):
        # A same-day, same-id re-fetch used to bootstrap INTO a v2.x
        # archive dir (state-keyed vacancy), and publish-mirror would then
        # commit the legacy tracker.md onto the PR branch.
        from harness import state as state_mod
        from harness.cli import PLUGIN_ROOT
        from harness.schema import load_yaml
        manifest = load_yaml(PLUGIN_ROOT / "pipeline" / "manifest.yaml")
        base = self.ws / "ai" / "2026-06-01-US-002"   # tracker-only archive
        slot = state_mod.next_run_slot(base, self.ws, manifest)
        self.assertEqual(slot.name, "2026-06-01-US-002-2")

    def test_detect_fails_closed_on_non_mapping_overrides(self):
        (self.ws / ".claude" / "context" / "overrides.yaml").write_text(
            "- just\n- a list\n", encoding="utf-8")
        found = migrate.detect(self.ws)
        self.assertTrue(found["already_bootstrapped"])
        self.assertTrue(any("overrides.yaml" in w for w in found["warnings"]))

    def test_placeholder_settings_never_win(self):
        (self.ws / ".claude" / "context" / "provider-config.md").write_text(
            "# Provider Configuration\n\n"
            "- **Work Item Provider**: `github`\n\n"
            "## Reference\n"
            "- **github_repo**: `<org>/<repo>`\n\n"
            "## Active\n"
            "- **github_repo**: `acme/real`\n"
            "- **stories_dir**: `(not set)`\n", encoding="utf-8")
        provider = migrate.extract(self.ws)["sections"]["provider"]["provider"]
        self.assertEqual(provider["github_repo"], "acme/real")
        self.assertNotIn("stories_dir", provider)

    def test_ambiguous_setting_is_a_note_not_a_guess(self):
        (self.ws / ".claude" / "context" / "provider-config.md").write_text(
            "# Provider Configuration\n\n"
            "- **Work Item Provider**: `github`\n"
            "- **github_repo**: `acme/one`\n"
            "- **github_repo**: `acme/two`\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertNotIn("github_repo",
                         out["sections"]["provider"]["provider"])
        self.assertTrue(any("github_repo" in n for n in out["notes"]))

    def test_backticked_commands_are_unwrapped(self):
        # carried verbatim, backticks reach shell=True as command
        # substitution
        (self.ws / ".claude" / "context" / "language-config.md").write_text(
            "# Language Configuration\n\n### calc\n"
            "- test_command: `python3 -m pytest`\n", encoding="utf-8")
        lang = migrate.extract(
            self.ws)["sections"]["language"]["language"]["repos"]
        self.assertEqual(lang["calc"]["test_cmd"], "python3 -m pytest")

    def test_naming_residue_of_any_spelling_is_unmapped(self):
        # {story_id} and $story_id spellings: .format explodes at preflight
        # or goes silently literal — both must drop to unmapped
        (self.ws / ".claude" / "context" / "naming-config.md").write_text(
            "branch_format: feature/{story_id}-{slug}\n"
            "pr_title_format: [$story_id] $slug\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertEqual(out["optional_overrides"], {})
        joined = " ".join(out["unmapped"])
        self.assertIn("branch_format", joined)
        self.assertIn("pr_title_format", joined)

    def test_fenced_example_tables_are_not_repos(self):
        (self.ws / ".claude" / "context" / "repos-paths.md").write_text(
            "# Repo Paths\n\n"
            "```\n| Repo Name | Local Path |\n|---|---|\n"
            "| my-repo | /path/to/repo |\n```\n\n"
            "| Repo Name | Local Path | Default Branch |\n"
            "|---|---|---|\n"
            f"| calc | {self.ws / 'code' / 'calc'} | main |\n",
            encoding="utf-8")
        repos = migrate.extract(self.ws)["sections"]["repos"]["repos"]
        self.assertEqual(set(repos), {"calc"})

    def test_relative_repo_paths_anchor_at_the_workspace(self):
        (self.ws / ".claude" / "context" / "repos-paths.md").write_text(
            "# Repo Paths\n\n| Repo Name | Local Path |\n|---|---|\n"
            "| calc | code/calc |\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertEqual(out["sections"]["repos"]["repos"]["calc"],
                         str(self.ws / "code" / "calc"))
        self.assertFalse(any("not a git checkout" in n for n in out["notes"]))

    def test_workspace_root_repo_is_dropped_with_the_boundary_note(self):
        (self.ws / ".claude" / "context" / "repos-paths.md").write_text(
            "# Repo Paths\n\n| Repo Name | Local Path |\n|---|---|\n"
            f"| main | {self.ws} |\n", encoding="utf-8")
        out = migrate.extract(self.ws)
        self.assertNotIn("repos", out["sections"])
        self.assertTrue(any("workspace root" in n for n in out["notes"]))

    def test_unclosed_fence_eats_to_eof_not_into_data(self):
        # an unclosed fence must not resurrect its sample rows as repos —
        # absent proposals (interview asks) beat wrong ones
        (self.ws / ".claude" / "context" / "repos-paths.md").write_text(
            "# Repo Paths\n\n```\n| Repo Name | Local Path |\n|---|---|\n"
            "| my-repo | /path/to/repo |\n", encoding="utf-8")
        self.assertNotIn("repos", migrate.extract(self.ws)["sections"])

    def test_dedupe_respects_id_boundaries(self):
        # US-0 is not the same story as US-002 — the prefix must end at a
        # `-` boundary or both belong in the report
        ctx = self.ws / ".claude" / "context"
        (ctx / "state.md").write_text(
            "# Workspace State\n\nBootstrap completed: 2026-05-17\n"
            "Workflow active: US-0\n", encoding="utf-8")
        ids = [e["id"] for e in migrate.inventory(self.ws)["in_flight"]]
        self.assertEqual(ids, ["US-002", "US-0"])

    def test_inventory_cosmetics_hardened(self):
        # `Workflow active: none` is a completed-state spelling, not a
        # story; a drifted `| ID | ... | status |` header still flags
        ctx = self.ws / ".claude" / "context"
        (ctx / "state.md").write_text(
            "# Workspace State\n\nBootstrap completed: 2026-05-17\n"
            "Workflow active: none\n", encoding="utf-8")
        (self.ws / "ai" / "2026-06-01-US-002" / "tracker.md").write_text(
            "# Tracker\n\n| ID | Repo | Title | status |\n|---|---|---|---|\n"
            "| T1 | calc | Implement | ⏳ Pending |\n", encoding="utf-8")
        inv = migrate.inventory(self.ws)
        self.assertEqual([e["id"] for e in inv["in_flight"]], ["US-002"])


class CliSeam(V21Fixture):
    def test_migrate_detect_reports_with_inventory(self):
        payload = self.cli("migrate-detect")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["legacy"], "v2.x")
        self.assertEqual([e["id"] for e in payload["inventory"]["in_flight"]],
                         ["US-002"])

    def test_migrate_detect_is_calm_on_a_fresh_workspace(self):
        empty = Path(tempfile.mkdtemp())
        try:
            proc = subprocess.run(
                [str(HARNESS_BIN), "--workspace", str(empty), "migrate-detect"],
                cwd=empty, capture_output=True, text=True, timeout=120)
            payload = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 0)
            self.assertIsNone(payload["legacy"])
            self.assertNotIn("inventory", payload)
        finally:
            shutil.rmtree(empty)

    def test_migrate_extract_emits_sections(self):
        payload = self.cli("migrate-extract")
        self.assertTrue(payload["ok"])
        self.assertLessEqual({"provider", "repos", "language"},
                             set(payload["sections"]))

    def test_migrate_extract_refuses_a_fresh_workspace(self):
        empty = Path(tempfile.mkdtemp())
        try:
            proc = subprocess.run(
                [str(HARNESS_BIN), "--workspace", str(empty),
                 "migrate-extract"],
                cwd=empty, capture_output=True, text=True, timeout=120)
            payload = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("/init-workspace", payload["error"])
        finally:
            shutil.rmtree(empty)

    def test_migrate_extract_refuses_a_bootstrapped_workspace(self):
        initws.mark_bootstrapped(self.ws)
        payload = self.cli("migrate-extract", expect=1)
        self.assertIn("workspace-config", payload["error"])


if __name__ == "__main__":
    unittest.main()
