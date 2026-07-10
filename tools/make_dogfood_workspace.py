"""Scaffold the first-dogfood workspace (docs/validation-plan.md, session A).

Creates, at the target path: a stories/ dir with DOG-1 (a small, real
feature: add subtract() to a toy calculator), and a `calc` git repo with a
passing stdlib-unittest suite — everything /init-workspace and
/dev-workflow need for a full-mode walk with zero external dependencies.

Usage: python3 tools/make_dogfood_workspace.py <target-dir>
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STORY = """# DOG-1: Add subtraction support to calc
Type: Story
Status: Open

## Description
calc.py currently only adds. Users need `subtract(a, b)` returning a - b,
exported alongside `add`, with the same input validation behavior
(TypeError on non-numeric arguments, matching how add() behaves).

## Acceptance Criteria
- [ ] `subtract(a, b)` returns a - b for ints and floats
- [ ] `subtract` raises TypeError on non-numeric arguments, same as `add`
- [ ] existing `add` behavior is unchanged
"""

CALC = '''"""A deliberately tiny calculator — the dogfood fixture."""


def _check(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("numeric arguments required")


def add(a, b):
    _check(a, b)
    return a + b
'''

TEST = '''import unittest

import calc


class TestAdd(unittest.TestCase):
    def test_adds_numbers(self):
        self.assertEqual(calc.add(2, 3), 5)

    def test_rejects_non_numeric(self):
        with self.assertRaises(TypeError):
            calc.add("2", 3)


if __name__ == "__main__":
    unittest.main()
'''


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    ws = Path(sys.argv[1]).expanduser().resolve()
    if ws.exists() and any(ws.iterdir()):
        print(f"refusing: {ws} exists and is not empty")
        return 1
    stories = ws / "stories"
    stories.mkdir(parents=True)
    (stories / "DOG-1.md").write_text(STORY, encoding="utf-8")
    repo = ws / "calc"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(CALC, encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    for args in (["init", "-b", "main", "."],
                 ["add", "-A"],
                 ["commit", "-m", "calc: initial add() + tests"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    print(f"dogfood workspace ready: {ws}")
    print(f"  story:  stories/DOG-1.md")
    print(f"  repo:   {repo} (test cmd: python3 -m unittest discover -s tests)")
    print("next: start Claude Code IN this directory with the plugin loaded "
          "(see docs/validation-plan.md, session A)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
