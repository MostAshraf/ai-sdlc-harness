"""Regression: the planner's content contract must not drift between the
orchestrator's required-content checklist (steps/plan.md) and the planner's
own instruction file (steps/plan-task.md) — F0/F1/F2 in m8-plan-fidelity.md
were caused by exactly this kind of silent divergence (a stale placeholder
in agents/planner.md standing in for a file that was never written).

Note on strength: `test_required_content_parity` checks keyword PRESENCE in
both files, not semantic agreement — it catches an artifact silently
dropped from one file's checklist, not a requirement reworded into an
optional one in both. That's the same class of check `test_invocation_consistency.py`
already uses (regex/substring, not parsing); a stronger check would need to
parse structure, not just grep for markers."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLANNER_MD = ROOT / "agents" / "planner.md"
PLAN_STEP_MD = ROOT / "skills" / "dev-workflow" / "steps" / "plan.md"
PLAN_TASK_MD = ROOT / "skills" / "dev-workflow" / "steps" / "plan-task.md"
DIAGRAM_STYLING_MD = ROOT / "skills" / "dev-workflow" / "shared" / "diagram-styling.md"

REQUIRED_ARTIFACTS = [
    "test-intent",
    "solution approaches",
    "pattern hint",
    "[api:",
    "self-adversarial",
    "dependency graph",
    "class/type",
    "flowchart",
    "sequence",
]


class PlanContract(unittest.TestCase):
    def test_planner_md_has_no_stale_placeholder(self):
        text = PLANNER_MD.read_text(encoding="utf-8")
        self.assertNotIn("arrives in M5", text,
                         "agents/planner.md still carries the M5 placeholder")

    def test_plan_task_instruction_file_exists(self):
        self.assertTrue(PLAN_TASK_MD.is_file(),
                        "steps/plan-task.md must exist — the planner's content contract")

    def test_diagram_styling_shared_file_exists(self):
        self.assertTrue(DIAGRAM_STYLING_MD.is_file(),
                        "shared/diagram-styling.md must exist (design.md, "
                        "Mermaid validation A3)")

    def test_required_content_parity(self):
        step_text = PLAN_STEP_MD.read_text(encoding="utf-8").lower()
        task_text = PLAN_TASK_MD.read_text(encoding="utf-8").lower()
        missing_from_step = [a for a in REQUIRED_ARTIFACTS if a not in step_text]
        missing_from_task = [a for a in REQUIRED_ARTIFACTS if a not in task_text]
        self.assertFalse(
            missing_from_step,
            f"steps/plan.md missing required-content marker(s): {missing_from_step}")
        self.assertFalse(
            missing_from_task,
            f"steps/plan-task.md missing required-content marker(s): {missing_from_task}")


class AgentToolsPinning(unittest.TestCase):
    """The three agent files carry a union-spelling `tools:` list so the
    same frontmatter grants the right tool set on BOTH Claude Code (grants
    Read/Write/Edit/Bash, ignores the Qwen display names) and Qwen Code
    (grants ReadFile/WriteFile/Edit/Shell/Grep/Glob, warn-drops the Claude
    spellings). Verified empirically on Claude Code (V5) and source-verified
    on Qwen Code v0.20.1 (transformToToolNames). This test pins the union
    so a future edit can't silently break one platform.

    The reviewer is read-only on BOTH platforms — no write spelling of
    either dialect appears in its list."""

    def _tools(self, agent_file: str) -> set[str]:
        text = (ROOT / "agents" / agent_file).read_text(encoding="utf-8")
        # extract the frontmatter block
        fm = text.split("---", 2)[1] if text.startswith("---") else ""
        for line in fm.splitlines():
            if line.strip().startswith("tools:"):
                return {t.strip() for t in line.split(":", 1)[1].split(",")
                        if t.strip()}
        self.fail(f"no tools: frontmatter in {agent_file}")

    def test_developer_has_both_platform_spellings(self):
        tools = self._tools("developer.md")
        # Claude spellings
        for t in ("Read", "Write", "Edit", "Bash"):
            self.assertIn(t, tools, f"developer missing Claude spelling {t}")
        # Qwen display names
        for t in ("ReadFile", "WriteFile", "Shell"):
            self.assertIn(t, tools, f"developer missing Qwen spelling {t}")

    def test_planner_has_both_platform_spellings(self):
        tools = self._tools("planner.md")
        for t in ("Read", "Write", "Edit", "Bash"):
            self.assertIn(t, tools, f"planner missing Claude spelling {t}")
        for t in ("ReadFile", "WriteFile", "Shell"):
            self.assertIn(t, tools, f"planner missing Qwen spelling {t}")

    def test_reviewer_is_read_only_on_both_platforms(self):
        tools = self._tools("reviewer.md")
        # Claude read spellings present
        for t in ("Read", "Bash"):
            self.assertIn(t, tools, f"reviewer missing Claude spelling {t}")
        # Qwen read display names present
        for t in ("ReadFile", "Shell"):
            self.assertIn(t, tools, f"reviewer missing Qwen spelling {t}")
        # NO write spelling of EITHER dialect — read-only contract.
        # Cover Claude display names, Qwen display names, AND canonical/wire
        # names (Qwen's transformToToolNames resolves exact-name matches too,
        # so a lowercase write_file or edit would grant write if it snuck in).
        for write_tool in ("Write", "WriteFile", "write_file",
                           "Edit", "edit", "replace",
                           "NotebookEdit", "notebook_edit"):
            self.assertNotIn(write_tool, tools,
                             f"reviewer grants {write_tool} — breaks the "
                             "read-only contract on one or both platforms")


class VersionTripleSync(unittest.TestCase):
    """qwen-extension.json, plugin.json, and marketplace.json must carry the
    same version — they're a triple now that the repo is dual-native. The
    mgm tooling (WI-4) will enforce this at bump-time; this test is cheap
    defense-in-depth so drift is caught in the harness suite before the
    next /release, regardless of mgm's state."""

    def test_versions_in_sync(self):
        pj = json.loads((ROOT / ".claude-plugin" / "plugin.json")
                        .read_text(encoding="utf-8"))
        qe = json.loads((ROOT / "qwen-extension.json")
                        .read_text(encoding="utf-8"))
        mj = json.loads((ROOT / ".claude-plugin" / "marketplace.json")
                        .read_text(encoding="utf-8"))
        versions = {
            "plugin.json": pj["version"],
            "qwen-extension.json": qe["version"],
            "marketplace.json plugins[0]": mj["plugins"][0]["version"],
        }
        unique = set(versions.values())
        self.assertEqual(len(unique), 1,
                         f"version triple drifted: {versions}")


if __name__ == "__main__":
    unittest.main()
