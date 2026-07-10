"""Scaffold a v2.x-shaped workspace — the /migrate-workspace fixture.

Reproduces the observable output shape of an ai-sdlc-harness v2.1
workspace (markdown context configs, blockquote-status stories, tracker.md
run dirs): everything `harness migrate-detect` / `migrate-extract` and
tests/test_migrate.py need, with zero external dependencies. Field shapes
are copied from a real v2.1 production workspace, not invented.

Usage: python3 tools/make_v21_workspace.py <target-dir>
Tests import `build(target)` by path — one source of truth for the fixture.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROVIDER_CONFIG = """# Provider Configuration

> Owner: workspace
> Version: 1.0

## Providers

- **Work Item Provider**: `local-markdown`
- **Git Provider**: `glab-cli`

## Work Item Provider: `local-markdown`

### local-markdown Settings
- **stories_dir**: `./stories`
"""

REPOS_PATHS = """# Repo Paths

> Owner: workspace
> Version: 1.0

| Repo Name | Local Path | Default Branch |
|-----------|-----------|----------------|
| calc | {calc_path} | main |
| ghost | {ghost_path} | main |
"""

LANGUAGE_CONFIG = """# Language Configuration

> Owner: workspace
> Version: 1.0

## Per-Repo Details

### calc
- language: python
- runtime_version: 3.12
- test_command: "python3 -m unittest"
- coverage_command: "python3 -m coverage run -m unittest"

### ghost
- language: node
- test_command: "npm test"
"""

NAMING_CONFIG = """# Naming Configuration

branch_format: feature/${story_id}-${slug}
commit_format: #${story_id} #${task_id} ${type}: ${slug}
pr_title_format: [${story_id}] ${slug}
tag_format: v${story_id}
"""

STATE_MD = """# Workspace State

Bootstrap completed: 2026-05-17 07:17 UTC
Workflow active: US-002-add-multiply (Phase 3 — development)
Last metric stamp: Workflow completed 2026-05-20 (US-001-add-subtract)
"""

COST_CONFIG = """# Cost Configuration

currency: USD
"""

STORY_DONE = """# US-001 — Add subtract support

> Status: ✅ Done — 2026-05-01

## Description

calc only adds; users need subtract too.

## Acceptance Criteria

- [x] subtract(a, b) returns a - b
"""

STORY_IN_FLIGHT = """# US-002 — Add multiply support

> Status: 🔧 In Progress — 2026-06-01

## Description

calc needs multiply.

## Acceptance Criteria

- [ ] multiply(a, b) returns a * b
"""

TRACKER_DONE = """# Task Tracker — Add subtract support (US-001)

| Task ID | Repo | Title | Status | Reviewer Verdict | Commit(s) | Notes |
|---------|------|-------|--------|------------------|-----------|-------|
| T1 | calc | Implement subtract | ✅ Done | ✅ Approved | abc1234 | test-required: true |
| T2 | calc | Fix In Review badge color | ✅ Done | ✅ Approved | bcd2345 | — |

Quick legend: ⏳ Pending · 🔧 In Progress · 🔄 In Review · ✅ Done
"""

TRACKER_IN_FLIGHT = """# Task Tracker — Add multiply support (US-002)

| Task ID | Repo | Title | Status | Reviewer Verdict | Commit(s) | Notes |
|---------|------|-------|--------|------------------|-----------|-------|
| T1 | calc | Implement multiply | ✅ Done | ✅ Approved | def5678 | test-required: true |
| T2 | calc | Wire multiply into In Review dashboard | ⏳ Pending | — | — | test-required: true |

Quick legend: ⏳ Pending · 🔧 In Progress · 🔄 In Review · ✅ Done
"""

TRACKER_ABORTED = """# Task Tracker — quick fix (aborted)

| Task ID | Repo | Title | Status | Reviewer Verdict | Commit(s) | Notes |
|---------|------|-------|--------|------------------|-----------|-------|
| T1 | calc | Tweak readme | ⏳ Pending | — | — | — |
"""


def build(target: Path) -> Path:
    """Everything is plain file writes except the one real `git init` —
    extract()'s repo-existence note keys on a real `.git`, and inventing a
    bare `.git` directory would test the fixture, not the code."""
    target = Path(target)
    ctx = target / ".claude" / "context"
    ctx.mkdir(parents=True, exist_ok=True)

    calc = target / "code" / "calc"
    calc.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(calc)], check=True)
    ghost = target / "code" / "ghost"  # deliberately never created

    (ctx / "provider-config.md").write_text(PROVIDER_CONFIG, encoding="utf-8")
    (ctx / "repos-paths.md").write_text(
        REPOS_PATHS.format(calc_path=calc, ghost_path=ghost), encoding="utf-8")
    (ctx / "language-config.md").write_text(LANGUAGE_CONFIG, encoding="utf-8")
    (ctx / "naming-config.md").write_text(NAMING_CONFIG, encoding="utf-8")
    (ctx / "state.md").write_text(STATE_MD, encoding="utf-8")
    (ctx / "cost-config.md").write_text(COST_CONFIG, encoding="utf-8")

    stories = target / "stories"
    stories.mkdir(exist_ok=True)
    (stories / "US-001-add-subtract.md").write_text(STORY_DONE,
                                                    encoding="utf-8")
    (stories / "US-002-add-multiply.md").write_text(STORY_IN_FLIGHT,
                                                    encoding="utf-8")

    runs = {"2026-05-01-US-001": ("tracker.md", TRACKER_DONE),
            "2026-06-01-US-002": ("tracker.md", TRACKER_IN_FLIGHT),
            "2026-04-01-quick-fix": ("tracker.aborted.md", TRACKER_ABORTED)}
    for run_name, (tracker_name, content) in runs.items():
        run_dir = target / "ai" / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / tracker_name).write_text(content, encoding="utf-8")
    return target


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    build(Path(sys.argv[1]))
    print(f"v2.1-shaped workspace scaffolded at {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
