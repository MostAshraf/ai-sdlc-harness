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


if __name__ == "__main__":
    unittest.main()
