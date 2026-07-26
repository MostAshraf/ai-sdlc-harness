"""Git-provider axis (design.md piece 4): pr.create per forge, CLI transport.

Emulation hides inside the adapter: GitHub/GitLab link the work item via a
`Closes #N` body line; ADO passes `--work-items` (native link). `local` is
the records-only provider so the pipeline completes without a forge.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import ProviderError, ProviderUnsupported
from ._normalize import run_cli


def _pr_body(work_item_id: str, summary: str, link_style: str) -> str:
    link = {"closes": f"Closes #{work_item_id}",
            "relates": f"Relates to {work_item_id}"}[link_style]
    return f"{summary}\n\n{link}\n"


def create_local(config, repo: Path, branch, base, title, work_item_id, summary):
    return {"provider": "local", "branch": branch, "title": title,
            "url": f"file://{repo}#{branch}"}


def create_github(config, repo: Path, branch, base, title, work_item_id, summary):
    url = run_cli(["gh", "pr", "create", "--title", title,
                   "--body", _pr_body(work_item_id, summary, "closes"),
                   "--base", base, "--head", branch], cwd=repo)
    return {"provider": "github", "branch": branch, "title": title, "url": url}


#: `glab mr create` failure text that means "the project could not be
#: resolved", as opposed to a real API/permission/validation error. Matched
#: case-insensitively against the CLI's combined output.
#: Matched case-insensitively, and each tell is PROJECT-adjacent on purpose
#: (re-verify finding: a bare "404"/"not found" also matches glab's echo of
#: the source branch, so a story about a 404 page — branch `fix/US-1-404-page`
#: — routed every failure through the fallback and surfaced the API's error
#: instead of glab's own).
_GITLAB_PROJECT_RESOLUTION = (
    "project not found", "404 project", "could not find project",
    "failed to get project", "project does not exist",
)


def _remote_project_path(repo: Path) -> str:
    """`group/subgroup/project` from origin's URL, for any of the spellings
    git accepts: `git@host:group/proj.git`, `https://host/group/proj.git`,
    `https://user@host/group/proj.git`, `ssh://git@host:22/group/proj.git`.
    Empty string when origin is absent or unparseable."""
    try:
        url = run_cli(["git", "-C", str(repo), "remote", "get-url",
                       "origin"]).strip()
    except ProviderError:
        return ""
    had_scheme = False
    for scheme in ("ssh://", "https://", "http://", "git://"):
        if url.startswith(scheme):
            url, had_scheme = url.removeprefix(scheme), True
    url = url.split("@", 1)[-1]          # drop any user@ prefix
    if had_scheme:
        # Ports are legal only in URL form. In scp form (`host:group/proj`)
        # the segment after the colon is a PATH, and stripping a numeric one
        # silently drops a group literally named `2024` (re-verify finding).
        url = re.sub(r":\d+(?=/)", "", url)
    # scp-style `host:group/proj` and url-style `host/group/proj` both leave
    # the host as the first segment once the separator is normalized
    if "/" not in url.split(":", 1)[0]:
        url = url.replace(":", "/", 1)
    parts = [p for p in url.split("/") if p]
    return "/".join(parts[1:]).removesuffix(".git") if len(parts) > 1 else ""


def _gitlab_project_id(repo: Path) -> str | None:
    """The numeric project id for this checkout, via search.

    field: dual-run comparison — both runs ended with `pr-recorded-manually`
    twice. The instance 404s PATH-ENCODED project lookups (`group/sub/proj`)
    while numeric-id access works, so `glab mr create` — which resolves by
    path — could never create the MR, and the human/manual hatch was the only
    route. Resolved by matching the remote's path suffix against the search
    results, never by taking the first hit: a substring search for `backend`
    will happily return someone else's `backend`."""
    import json as _json
    path = _remote_project_path(repo)
    slug = path.rsplit("/", 1)[-1] if path else ""
    if not slug:
        return None
    try:
        raw = run_cli(["glab", "api",
                       f"projects?search={slug}&per_page=100"], cwd=repo)
        for proj in _json.loads(raw):
            if str(proj.get("path_with_namespace", "")).casefold() == path.casefold():
                return str(proj.get("id"))
    except Exception:      # any search failure -> no fallback, manual hatch
        return None
    return None


def create_gitlab(config, repo: Path, branch, base, title, work_item_id, summary):
    body = _pr_body(work_item_id, summary, "closes")
    try:
        url = run_cli(["glab", "mr", "create", "--title", title,
                       "--description", body,
                       "--source-branch", branch, "--target-branch", base,
                       "--yes"], cwd=repo)
        return {"provider": "gitlab", "branch": branch, "title": title,
                "url": url}
    except ProviderError as exc:
        # Only the project-resolution class falls back. A validation error
        # (branch missing, MR already open, no permission) must surface as
        # itself — retrying it through a second transport would just produce
        # a second, more confusing failure.
        if not any(tell in str(exc).casefold()
                   for tell in _GITLAB_PROJECT_RESOLUTION):
            raise
        pid = _gitlab_project_id(repo)
        if pid is None:
            raise
        raw = run_cli(["glab", "api", "--method", "POST",
                       f"projects/{pid}/merge_requests",
                       "-f", f"source_branch={branch}",
                       "-f", f"target_branch={base}",
                       "-f", f"title={title}",
                       "-f", f"description={body}"], cwd=repo)
        import json as _json
        try:
            url = str(_json.loads(raw).get("web_url") or "").strip()
        except ValueError:
            url = ""
        if not url:
            raise ProviderError(
                "gitlab: path-encoded project lookup 404'd and the numeric-id "
                f"fallback (project {pid}) returned no web_url — record the "
                "MR manually (`harness create-pr --manual-url <url>`)") from exc
        return {"provider": "gitlab", "branch": branch, "title": title,
                "url": url, "resolved_by": "numeric-project-id"}


def create_ado(config, repo: Path, branch, base, title, work_item_id, summary):
    import json
    raw = json.loads(run_cli(["az", "repos", "pr", "create", "--title", title,
                              "--description", summary,
                              "--source-branch", branch,
                              "--target-branch", base,
                              "--work-items", str(work_item_id),   # native link
                              "--output", "json"], cwd=repo))
    return {"provider": "ado", "branch": branch, "title": title,
            "url": raw.get("url") or raw.get("repository", {}).get("webUrl", "")}


# ADO PR creation over MCP is model-invoked (design.md piece 4) — declared, not
# scripted. `repositoryId`/`project` come from provider-config; ADO requires the
# `refs/heads/` branch prefix and links the work item natively (no `Closes #N`).
ADO_MCP_PR = {
    "create": {"tool": "mcp__azure-devops__repo_create_pull_request",
               "args": {"repositoryId": "{repositoryId}", "project": "{project}",
                        "sourceRefName": "refs/heads/{branch}",
                        "targetRefName": "refs/heads/{base}",
                        "title": "{title}", "description": "{summary}"}},
    "link": {"tool": "mcp__azure-devops__wit_link_work_item_to_pull_request",
             "args": {"workItemId": "{work_item_id}",
                      "pullRequestUrl": "{pr_url}", "project": "{project}"}},
}


def create_ado_mcp(config, repo: Path, branch, base, title, work_item_id, summary):
    """MCP-transport twin of `create_ado`: refuse with the declared mapping the
    orchestrator executes (create PR, then natively link the work item),
    mirroring the MCP work-item dispatch refusal."""
    raise ProviderError(
        f"'ado-mcp' is MCP-transport: invoke '{ADO_MCP_PR['create']['tool']}' "
        f"with args {ADO_MCP_PR['create']['args']} "
        f"(branch={branch!r} base={base!r} title={title!r}; refs/heads/ prefix "
        f"required, repositoryId/project from provider-config), then link via "
        f"'{ADO_MCP_PR['link']['tool']}' (workItemId={work_item_id}, "
        "pullRequestUrl=<url returned by create>).")


_GIT = {"local": create_local, "github": create_github,
        "gitlab": create_gitlab, "ado": create_ado, "ado-mcp": create_ado_mcp}


def create_pr(config: dict, **kwargs) -> dict:
    name = (config.get("provider") or {}).get("git", "local")
    fn = _GIT.get(name)
    if fn is None:
        raise ProviderError(f"unknown git provider '{name}' "
                            f"(available: {', '.join(sorted(_GIT))})")
    return fn(config, **kwargs)


def _pr_number(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def fetch_comments_local(config, repo: Path, pr: dict) -> list[dict]:
    """`local` is records-only (no forge) — the human pastes comments
    (analyze-comments.md), so there is nothing to fetch."""
    return []


def fetch_comments_github(config, repo: Path, pr: dict) -> list[dict]:
    """Three GitHub surfaces hold review feedback (re-review finding: the
    first version fetched only the first — top-level conversation comments
    — while inline diff comments, the dominant form of real code-review
    feedback, and review-summary bodies both sat unfetched, so
    analyze-comments triaged a near-empty list on a real 'Request changes'
    review):
      1. conversation comments (`gh pr view --json comments`)
      2. review summary bodies (`--json reviews`; blank bodies — a bare
         approve/request-changes click — are skipped)
      3. inline diff comments (`gh api repos/{owner}/{repo}/pulls/N/comments`
         — gh substitutes {owner}/{repo} from the cwd repo's remote)"""
    import json
    n = _pr_number(pr["url"])
    raw = json.loads(run_cli(["gh", "pr", "view", n,
                              "--json", "comments,reviews"], cwd=repo))
    items: list[dict] = []
    for c in raw.get("comments") or []:
        items.append({"author": (c.get("author") or {}).get("login", ""),
                      "body": c.get("body", "")})
    for r in raw.get("reviews") or []:
        if (r.get("body") or "").strip():
            items.append({"author": (r.get("author") or {}).get("login", ""),
                          "body": r["body"],
                          "review_state": r.get("state", "")})
    inline = json.loads(run_cli(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{n}/comments"], cwd=repo))
    for c in inline if isinstance(inline, list) else []:
        items.append({"author": (c.get("user") or {}).get("login", ""),
                      "body": c.get("body", ""), "path": c.get("path", ""),
                      "line": c.get("line") or c.get("original_line")})
    return [{"id": str(i + 1), **item} for i, item in enumerate(items)]


def fetch_comments_gitlab(config, repo: Path, pr: dict) -> list[dict]:
    import json
    # Via `glab api` (adversarial-review finding: the first version ran
    # `glab mr note list`, a subcommand that does not exist — `glab mr note`
    # only CREATES a note, glab has no note-listing subcommand at all, and
    # the stub-driven test asserted the harness's own invented argv, so it
    # shipped green). `:id` is glab api's documented placeholder for the
    # current directory's project — which is why cwd=repo matters here.
    # System notes (state-change events GitLab stores as notes) are
    # filtered out.
    raw = json.loads(run_cli(
        ["glab", "api",
         f"projects/:id/merge_requests/{_pr_number(pr['url'])}/notes"
         "?per_page=100"], cwd=repo))
    # Filter BEFORE numbering (live-forge finding: real GitLab returns
    # notes newest-first, so system notes precede the human ones and
    # enumerate-then-filter produced ids like "4" for the first visible
    # comment — position-dependent on filtered-out content, and
    # inconsistent with the github adapter's contiguous numbering).
    notes = [c for c in (raw if isinstance(raw, list) else [])
             if not c.get("system")]
    return [{"id": str(i + 1), "author": (c.get("author") or {}).get("username", ""),
             "body": c.get("body", "")}
            for i, c in enumerate(notes)]


def fetch_comments_ado(config, repo: Path, pr: dict) -> list[dict]:
    raise ProviderUnsupported(
        "ado PR-comment fetch is not implemented — use the ADO web UI "
        "or `az repos pr` directly for this round and paste the comments")


def fetch_comments_ado_mcp(config, repo: Path, pr: dict) -> list[dict]:
    raise ProviderError(
        "'ado-mcp' is MCP-transport: invoke "
        "'mcp__azure-devops__repo_get_pull_request_threads' "
        f"(pullRequestId from {pr.get('url', '')!r}), then pass the raw "
        "result to the reviewer for analysis — a script cannot call an MCP tool.")


_GIT_COMMENTS = {"local": fetch_comments_local, "github": fetch_comments_github,
                 "gitlab": fetch_comments_gitlab, "ado": fetch_comments_ado,
                 "ado-mcp": fetch_comments_ado_mcp}


def fetch_pr_comments(config: dict, **kwargs) -> list[dict]:
    name = (config.get("provider") or {}).get("git", "local")
    fn = _GIT_COMMENTS.get(name)
    if fn is None:
        raise ProviderError(f"unknown git provider '{name}' "
                            f"(available: {', '.join(sorted(_GIT_COMMENTS))})")
    return fn(config, **kwargs)
