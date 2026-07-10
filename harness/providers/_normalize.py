"""Shared normalization helpers (design.md piece 4): section/checklist
parsing and the AC-embedding heuristic for providers without a dedicated
acceptance-criteria field (GitHub/GitLab/local-markdown: look for an
'## Acceptance Criteria' heading, else task-list items)."""
from __future__ import annotations

import re
import subprocess


def section(text: str, heading: str) -> str:
    m = re.search(rf"^##+\s+{heading}\s*$(.*?)(?=^##+\s|\Z)", text or "",
                  re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def checklist(text: str) -> list[str]:
    return re.findall(r"^\s*[-*]\s*\[[ xX]?\]\s*(.+)$", text or "", re.MULTILINE)


def acceptance_criteria(body: str) -> list[str]:
    ac = section(body, "Acceptance Criteria")
    return checklist(ac) or ([ac] if ac else checklist(body))


def type_from_labels(labels: list[str]) -> str:
    lowered = {l.lower() for l in labels}
    for label, wit in (("bug", "Bug"), ("feature", "Story"),
                       ("enhancement", "Story"), ("chore", "Task"),
                       ("documentation", "Task")):
        if label in lowered:
            return wit
    return "Task"


def run_cli(args: list[str], cwd=None) -> str:
    """All CLI-transport calls go through here — tests put fake `gh`/`glab`/
    `az` executables on PATH and assert the exact argv we construct.

    `cwd` matters for the git-provider axis (`git_providers.py`): `gh`/`glab`
    resolve which forge repo to act on from the git remote of the directory
    they run in, so a git-provider call MUST run inside the target repo, not
    the harness process's own cwd (work-item providers pass an explicit
    `--repo`/`--repo` flag instead, so they don't need this)."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=cwd)
    if proc.returncode != 0:
        from . import ProviderError
        raise ProviderError(
            f"{args[0]} {' '.join(args[1:3])}…: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()
