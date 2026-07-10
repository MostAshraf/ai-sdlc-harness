"""GitHub work-item provider, CLI transport (`gh`) — design.md piece 4.

Auth is wholly `gh auth login`'s concern; the harness never sees a token.
Emulation hides inside the adapter: GitHub issues have only open/closed, so
the status projection collapses (STATUS_DEFAULTS) and richer states are
emulated as labels-free comments. Live verification: `gh auth status` +
one fetch against a real repo (init-workspace M7 probes this).
"""
from __future__ import annotations

import json

from . import ProviderError
from ._normalize import acceptance_criteria, run_cli, section, type_from_labels

NAME = "github"
TRANSPORT = "cli"
STATUS_DEFAULTS = {"in-progress": "open", "in-review": "open", "done": "closed"}


def _repo_args(config: dict) -> list[str]:
    repo = (config.get("provider") or {}).get("github_repo")
    if not repo:
        # Fail closed, never fall back to cwd resolution (adversarial-review
        # finding: without --repo, `gh` targets whatever repo the harness
        # process's directory happens to be — fetching a DIFFERENT project's
        # issue #7 is a silent wrong-result, not an error).
        raise ProviderError(
            "provider.github_repo is not set — without it, gh would target "
            "whatever repo the current directory happens to be (wrong-issue "
            "risk); set it to the owner/name hosting the work items "
            "(init-section --section provider)")
    return ["--repo", repo]


def fetch(config: dict, id: str) -> dict:
    raw = json.loads(run_cli(["gh", "issue", "view", str(id),
                              *_repo_args(config),
                              "--json", "number,title,body,state,labels"]))
    body = raw.get("body") or ""
    return {"id": str(raw["number"]), "title": raw["title"],
            "type": type_from_labels([l["name"] for l in raw.get("labels", [])]),
            # normalized to lowercase (live-forge finding: real gh returns
            # "OPEN"/"CLOSED" uppercase while transition() reports
            # lowercase — the shared contract's fetch-vs-transition
            # self-consistency broke against a real forge; the stub hid it
            # by faking lowercase)
            "state": (raw.get("state") or "").lower(),
            "description": section(body, "Description") or body,
            "acceptance_criteria": acceptance_criteria(body),
            "provider_ref": f"github#{raw['number']}"}


def transition(config: dict, id: str, to: str) -> dict:
    action = "close" if to.lower() in ("closed", "done") else "reopen"
    run_cli(["gh", "issue", action, str(id), *_repo_args(config)])
    return {"id": str(id), "state": "closed" if action == "close" else "open"}


def add_comment(config: dict, id: str, text: str) -> dict:
    run_cli(["gh", "issue", "comment", str(id), *_repo_args(config),
             "--body", text])
    return {"id": str(id), "commented": True}


def create(config: dict, title: str, description: str = "") -> dict:
    """Security-defer follow-up (coverage B9): `gh issue create` prints the
    new issue's URL (plain text, not JSON) — its trailing path segment is
    the issue number."""
    url = run_cli(["gh", "issue", "create", "--title", title,
                   "--body", description, *_repo_args(config)])
    return {"id": url.rstrip("/").rsplit("/", 1)[-1], "url": url}


OPS = {"work_item.fetch": fetch,
       "work_item.transition": transition,
       "work_item.add_comment": add_comment,
       "work_item.create": create}
SUPPORTS = sorted(OPS)
