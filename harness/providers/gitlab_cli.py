"""GitLab work-item provider, CLI transport (`glab`). Auth = `glab auth
login`. Issue ids are project-scoped iids — the adapter takes the iid, as
`glab` does."""
from __future__ import annotations

import json

from . import ProviderError
from ._normalize import acceptance_criteria, run_cli, section, type_from_labels

NAME = "gitlab"
TRANSPORT = "cli"
STATUS_DEFAULTS = {"in-progress": "opened", "in-review": "opened", "done": "closed"}


def _repo_args(config: dict) -> list[str]:
    repo = (config.get("provider") or {}).get("gitlab_repo")
    if not repo:
        # Fail closed, never fall back to cwd resolution (same wrong-issue
        # risk github_cli._repo_args documents).
        raise ProviderError(
            "provider.gitlab_repo is not set — without it, glab would "
            "target whatever repo the current directory happens to be "
            "(wrong-issue risk); set it to the project path hosting the "
            "work items (init-section --section provider)")
    return ["--repo", repo]


def fetch(config: dict, id: str) -> dict:
    raw = json.loads(run_cli(["glab", "issue", "view", str(id),
                              *_repo_args(config), "--output", "json"]))
    body = raw.get("description") or ""
    return {"id": str(raw.get("iid", id)), "title": raw["title"],
            "type": type_from_labels(raw.get("labels") or []),
            # lowercase for the same fetch-vs-transition consistency the
            # github adapter needed (glab already emits lowercase; this
            # pins the contract rather than trusting it)
            "state": (raw.get("state") or "").lower(),
            "description": section(body, "Description") or body,
            "acceptance_criteria": acceptance_criteria(body),
            "provider_ref": f"gitlab#{raw.get('iid', id)}"}


def transition(config: dict, id: str, to: str) -> dict:
    action = "close" if to.lower() in ("closed", "done") else "reopen"
    run_cli(["glab", "issue", action, str(id), *_repo_args(config)])
    return {"id": str(id), "state": "closed" if action == "close" else "opened"}


def add_comment(config: dict, id: str, text: str) -> dict:
    run_cli(["glab", "issue", "note", str(id), *_repo_args(config),
             "--message", text])
    return {"id": str(id), "commented": True}


def create(config: dict, title: str, description: str = "") -> dict:
    """Security-defer follow-up (coverage B9): `glab issue create` prints
    the new issue's URL (plain text, not JSON) — its trailing path segment
    is the issue iid."""
    # --yes: skip the submission confirmation prompt — a captured,
    # non-tty subprocess can never answer it (live-forge follow-up; the
    # harness's `mr create` already passes it)
    url = run_cli(["glab", "issue", "create", "--title", title,
                   "--description", description, "--yes",
                   *_repo_args(config)])
    return {"id": url.rstrip("/").rsplit("/", 1)[-1], "url": url}


OPS = {"work_item.fetch": fetch,
       "work_item.transition": transition,
       "work_item.add_comment": add_comment,
       "work_item.create": create}
SUPPORTS = sorted(OPS)
