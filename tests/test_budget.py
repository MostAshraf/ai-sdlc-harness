"""Budget checker: caps enforced, duplication caught, exemptions respected."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import budget_check  # noqa: E402


def _write(root: Path, rel: str, lines: list[str]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class BudgetCaps(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_hard_cap_is_error(self):
        _write(self.root, "skills/big/SKILL.md", [f"line {i}" for i in range(250)])
        errors, _ = budget_check.check_budgets(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("hard cap", errors[0])

    def test_soft_cap_is_warning_only(self):
        _write(self.root, "skills/mid/SKILL.md", [f"line {i}" for i in range(150)])
        errors, warnings = budget_check.check_budgets(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_docs_are_exempt(self):
        _write(self.root, "docs/design.md", [f"line {i}" for i in range(500)])
        errors, warnings = budget_check.check_budgets(self.root)
        self.assertEqual((errors, warnings), ([], []))

    def test_duplicated_block_across_files_is_error(self):
        block = [f"shared rule line {i}" for i in range(6)]
        _write(self.root, "agents/a.md", ["# A", *block])
        _write(self.root, "agents/b.md", ["# B", *block])
        errors = budget_check.check_duplication(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("agents/a.md", errors[0])
        self.assertIn("agents/b.md", errors[0])

    def test_short_overlap_is_fine(self):
        block = [f"shared rule line {i}" for i in range(3)]  # below window
        _write(self.root, "agents/a.md", ["# A", *block])
        _write(self.root, "agents/b.md", ["# B", *block])
        self.assertEqual(budget_check.check_duplication(self.root), [])


if __name__ == "__main__":
    unittest.main()
