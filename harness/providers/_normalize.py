"""Shared normalization helpers (design.md piece 4): section/checklist
parsing and the AC-embedding heuristic for providers without a dedicated
acceptance-criteria field (GitHub/GitLab/local-markdown: look for an
'## Acceptance Criteria' heading, else task-list items)."""
from __future__ import annotations

import re
import shutil
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
    # `which()` resolution, not a bare exec: Windows' CreateProcess appends
    # only `.exe` to a bare name, so the real Azure CLI (`az.cmd`) — and any
    # non-.exe shim — is unfindable without the PATHEXT walk which() does.
    # It also honors PATH order, which is what lets the test suite's
    # fake-CLI directory shadow a real `gh`/`glab` installed on the host.
    # UTF-8 decode, never the locale codec: forge CLIs print UTF-8 JSON,
    # and Windows' cp1252 both mojibakes it and can raise on undefined bytes.
    exe = shutil.which(args[0]) or args[0]
    proc = subprocess.run([exe, *args[1:]], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=120, cwd=cwd)
    if proc.returncode != 0:
        from . import ProviderError
        raise ProviderError(
            f"{args[0]} {' '.join(args[1:3])}…: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()
