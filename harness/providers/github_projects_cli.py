"""GitHub Projects (v2) work-item provider, CLI transport (`gh project`).

Distinct from the `github` provider on purpose. GitHub *issues* carry only
open/closed, so `github_cli` collapses every milestone into that binary. A
Project board carries a real single-select **Status** field, so this adapter
writes milestones into actual board columns — the richer projection the
collapse exists to approximate. Use `github` when the tracker is the issue
list; use `github-projects` when it is the board.

Auth is wholly `gh auth login`'s concern. Note the scope asymmetry: reads
need `read:project`, but `item-edit` (status write-back) needs the wider
`project` scope — `gh auth refresh -s project`. Write-back is best-effort
harness-wide, so a read-only token degrades to a flagged
`write-back-failed` event rather than a broken run. Everything this module
raises must therefore be a `ProviderError`: it is the only class the
best-effort path catches, and this is the first provider to put JSON
parsing on the write-back path at all, so `_json()` exists to keep a
malformed `gh` response inside that contract.

Shape provenance: `item-list`, `field-list`, and `project view` were
captured from `gh` 2.98 against a live board
(tests/fixtures/forge/github-projects-*.json). `item-create` and
`item-edit` are NOT captured — they need the wider `project` scope and
would mutate a live board — so their response handling here is defensive
by assumption rather than by observation, which is why `create` validates
the id it is handed instead of trusting it. `add_comment` shells to `gh
issue comment`, whose capture lives with the `github` provider's fixtures
(github-work_item.add_comment.json) since it is the identical verb.
"""
from __future__ import annotations

import json
import re
import unicodedata

from . import ProviderError
from ._normalize import acceptance_criteria, run_cli, section, type_from_labels

NAME = "github-projects"
TRANSPORT = "cli"

#: Board-native milestone targets. Unlike the issue providers these are real
#: column names, not a two-state collapse. GitHub's stock board template
#: ships only Todo / In Progress / Done, so `in-review` is the one that
#: routinely has no literal home — `_ALIASES` handles that rather than
#: failing a write-back on every default board.
STATUS_DEFAULTS = {"in-progress": "In Progress", "in-review": "In Review",
                   "done": "Done"}

#: Ordered fallbacks tried *after* the requested name itself, keyed by
#: `_key()`. Emulation stays inside the adapter (design.md piece 4): callers
#: ask for a milestone, not for a column that may not exist.
#:
#: `inreview` ends by re-entering `inprogress`'s OWN chain rather than
#: naming one spelling of it (adversarial-review, both lenses): a flat
#: `("Review", "In Progress")` tail stranded `in-review` on a
#: Todo/Doing/Done board — the exact "Doing" rename `inprogress` already
#: anticipates — so the two chains disagreed about the same board.
#: Degrading `in-review` to in-progress is deliberate: the work genuinely
#: is still in flight, and a board with no review column should not strand
#: the item. It is a fallback, not an override — `status_mapping` chooses
#: which name is *requested*; this chain still applies to that name.
_ALIASES = {
    "inprogress": ("Doing", "Started"),
    "inreview": ("Review", "In Progress", "Doing", "Started"),
    "done": ("Complete", "Completed", "Closed"),
    "todo": ("Backlog", "New"),
}


def _key(name: str) -> str:
    """Comparison key for a field or option name.

    `gh` keys an item's field values by the field's display name, and
    whether it lowercases or camelCases a multi-word name is an
    implementation detail we must not depend on ("Status" -> "status";
    "Linked pull requests" -> "linked pull requests"). Folding away case
    and punctuation matches both spellings of the same field, and unifies
    "To Do" with "Todo".

    Unicode-aware, deliberately (adversarial-review, lens B): an ASCII-only
    `[^a-z0-9]` filter reduced EVERY option on a Cyrillic or CJK board to
    the empty string, so `_resolve_option` matched the first option in the
    list and silently moved finished items to the To Do column. NFKD-then-
    strip-marks also folds "Terminé" onto "Termine", which is the same
    spelling-tolerance the ASCII path had for punctuation. `keys_match`
    is what actually compares — an empty key never matches anything.
    """
    folded = unicodedata.normalize("NFKD", str(name or "")).casefold()
    return re.sub(r"[\W_]", "", folded, flags=re.UNICODE)


def keys_match(a: str, b: str) -> bool:
    """Whether two field/option names are the same name.

    Public because `initws.verify` matches the configured status field the
    same way this module does at runtime — two readers of one declared
    value must not hold different beliefs about it. Fails closed on an
    unkeyable name rather than collapsing onto "".
    """
    ka, kb = _key(a), _key(b)
    return bool(ka) and ka == kb


def _as_list(value, what: str) -> list:
    """A JSON value that must be a list, inside the error contract.

    `_json` closed the not-JSON hole, but well-formed JSON of the WRONG
    SHAPE still escaped as a bare `TypeError` from iterating a non-list
    (re-verification, finding B) — and `_resolve_option` runs on the
    write-back path, which catches `ProviderError` alone."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProviderError(
            f"{what} is {type(value).__name__}, expected a list")
    return value


def _text(value) -> str:
    """Same idea for a field the normalizers will regex over."""
    return value if isinstance(value, str) else ("" if value is None
                                                 else str(value))


def _json(raw: str, what: str) -> dict:
    """`json.loads` inside the provider error contract.

    `JSONDecodeError` is a `ValueError`, not a `ProviderError`, so an
    unparseable `gh` response on the write-back path escaped the
    best-effort catch and aborted the step (adversarial-review, lens A) —
    the precise failure `_best_effort_transition` exists to prevent."""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ProviderError(
            f"{what} returned output that is not JSON ({exc}); first 200 "
            f"characters: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(
            f"{what} returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def _project(config: dict) -> tuple[str, str, list[str]]:
    """(project number, owner login, owner argv). Fails closed on either half.

    Same reasoning as `github_cli._repo_args`: `gh project` has no
    cwd-resolution fallback to guess wrong with, but a *missing* value would
    make `gh` treat the next argv token as the project number, so an unset
    key must be an error here rather than a confusing `gh` usage failure.
    """
    p = config.get("provider") or {}
    number = str(p.get("github_project") or "").strip()
    owner = str(p.get("github_project_owner") or "").strip()
    missing = [k for k, v in (("github_project", number),
                              ("github_project_owner", owner)) if not v]
    if missing:
        raise ProviderError(
            f"provider.{' and provider.'.join(missing)} is not set — "
            "github-projects needs the board's number and owner login "
            "(the two path segments of its URL, e.g. .../orgs/acme/"
            "projects/7 -> owner 'acme', project 7; '@me' is valid for a "
            "user-owned board); set them with "
            "init-section --section provider")
    return number, owner, ["--owner", owner]


def _home_repo(config: dict) -> str:
    """`provider.github_project_repo` — the repo whose issue numbers this
    workspace's work-item ids refer to. Optional, and the difference it
    makes is the id SHAPE (see `_emit_id`), so it is worth setting."""
    return str((config.get("provider") or {}).get(
        "github_project_repo") or "").strip()


def _status_field(config: dict) -> str:
    return str((config.get("provider") or {}).get(
        "github_project_status_field") or "Status").strip() or "Status"


#: A harness-side sanity ceiling, NOT a `gh` one: probed against gh 2.98,
#: `gh project item-list` does no client-side `--limit` validation at all —
#: it forwards the value and pages. `_find`'s remediation tells the user to
#: RAISE this key, so a bound is what keeps that advice from walking them
#: into an absurdly long GraphQL walk. (Re-verification finding D: the
#: comment here previously asserted a `gh` behaviour `gh` does not have.)
_MAX_LIMIT = 1000


def _limit(config: dict) -> int:
    """How deep to page the board. There is no server-side get-one-item call
    (`gh project` exposes `item-list` only), so every op reads a page and
    filters locally — the limit is the honest bound on what we can see, and
    a miss reports it rather than claiming the item does not exist."""
    raw = (config.get("provider") or {}).get("github_project_limit")
    try:
        value = int(raw) if raw not in (None, "") else 200
    except (TypeError, ValueError):
        raise ProviderError(
            f"provider.github_project_limit must be a positive integer "
            f"(got {raw!r})")
    if value < 1 or value > _MAX_LIMIT:
        # Upper bound too: `_find`'s remediation tells the user to RAISE
        # this, and walking them into a raw `gh` usage error would make
        # that remediation actively wrong (adversarial-review, lens B).
        raise ProviderError(
            f"provider.github_project_limit must be between 1 and "
            f"{_MAX_LIMIT} (got {value})")
    return value


def _items(config: dict) -> tuple[list[dict], int]:
    """(the page we can see, the board's real item count).

    `totalCount` is what lets a miss distinguish "not on this board" from
    "past the page we read" — reporting the second as the first sent users
    to raise a limit that was never the problem."""
    number, _, owner = _project(config)
    raw = _json(run_cli(["gh", "project", "item-list", number, *owner,
                         "--format", "json",
                         "--limit", str(_limit(config))]),
                "gh project item-list")
    items = [i for i in _as_list(raw.get("items"), "item-list `items`")
             if isinstance(i, dict)]
    total = raw.get("totalCount")
    return items, total if isinstance(total, int) else len(items)


def _content(item: dict) -> dict:
    content = item.get("content")
    return content if isinstance(content, dict) else {}


def _is_pull_request(item: dict) -> bool:
    return str(_content(item).get("type") or "") == "PullRequest"


def item_ref(item: dict) -> str:
    """The item's cross-repo-unambiguous name: `owner/repo#N`, or the
    project item node id for a draft that has no issue behind it."""
    content = _content(item)
    if content.get("number") and content.get("repository"):
        return f"{content['repository']}#{content['number']}"
    return str(item.get("id") or "")


def _emit_id(config: dict, item: dict) -> str:
    """The id the harness carries for this item — chosen so that replaying
    it through `_find` always resolves to THIS item again.

    A bare issue number only satisfies that when something pins which
    repo's numbering it refers to, which is what `github_project_repo`
    does. Emitting a bare number unconditionally was the adversarial
    review's highest finding, converged on by both lenses: on a board
    spanning repos, the qualified spelling was the only one that could
    fetch an item and the bare number it handed back then made every
    subsequent transition and comment refuse as ambiguous for the life of
    the run — and two genuinely different items collided on one run
    directory and one bootstrap lock.

    So: bare when pinned (short ids, clean branch names, and a `Closes #N`
    that means what it says), qualified otherwise. Drafts have no number
    at all and answer to their `PVTI_…` item id."""
    content = _content(item)
    number = content.get("number")
    if not number:
        return str(item.get("id") or "")
    home = _home_repo(config)
    if home and str(content.get("repository") or "").lower() == home.lower():
        return str(number)
    return item_ref(item)


def _candidates(config: dict, items: list[dict], wanted: str) -> list[dict]:
    """Items matching `wanted`, which may be a project node id, an issue
    URL, `owner/repo#N`, `#N`, or a bare number."""
    text = str(wanted).strip()
    exact = [i for i in items
             if i.get("id") == text or _content(i).get("url") == text]
    if exact:
        return exact
    repo, _, number = text.rpartition("#")
    if not number.isdigit():
        return []
    if not repo:
        # A bare number is interpreted in the pinned repo's numbering when
        # there is one; that is what makes the id `_emit_id` hands back
        # re-resolve without ambiguity.
        repo = _home_repo(config)
    hits = []
    for i in items:
        content = _content(i)
        if str(content.get("number") or "") != number:
            continue
        if repo and str(content.get("repository") or "").lower() != repo.lower():
            continue
        # Issues and PRs share one numbering space per repo, so a PR here
        # is never a same-repo collision — but ACROSS repos an unrelated
        # PR #7 would make a real issue #7 read as ambiguous, and a PR
        # matched alone would be planned and built as if it were a work
        # item (adversarial-review, both lenses). Number lookup is for
        # work items; an explicit id or URL still resolves a PR, so
        # `_find` can name it rather than say "not found".
        if _is_pull_request(i):
            continue
        hits.append(i)
    return hits


def _find(config: dict, id: str) -> dict:
    number, login, _ = _project(config)
    board = f"{login}/{number}"
    limit = _limit(config)
    items, total = _items(config)
    hits = _candidates(config, items, id)
    if not hits:
        if total > len(items):
            where = (f"the first {limit} of {total} items — raise "
                     "provider.github_project_limit to cover the rest")
        else:
            where = (f"all {total} of the board's items; note that "
                     "`gh project item-list` never returns ARCHIVED items, "
                     "so an archived item reads as absent")
        raise ProviderError(
            f"work item '{id}' not found on project {board} — searched "
            f"{where}")
    if len(hits) > 1:
        # A board can span repos, and issue numbers are per-repo — silently
        # picking one would transition a DIFFERENT team's item (the
        # wrong-issue class `github_cli._repo_args` fails closed on).
        refs = ", ".join(sorted(item_ref(h) for h in hits))
        raise ProviderError(
            f"work item '{id}' is ambiguous on project {board} — it matches "
            f"{refs}; set provider.github_project_repo to the repo whose "
            "issue numbers this workspace's ids refer to, or address the "
            "item by its owner/repo#N form or project item id (PVTI_…)")
    item = hits[0]
    if _is_pull_request(item):
        raise ProviderError(
            f"'{id}' is a pull request on project {board} "
            f"({item_ref(item)}), not a work item — boards carry PRs "
            "alongside issues; address the issue the PR closes instead")
    return item


def _status_of(config: dict, item: dict) -> str:
    wanted = _status_field(config)
    for key, value in item.items():
        if isinstance(value, str) and keys_match(key, wanted):
            return value
    return ""


def _resolve_option(config: dict, to: str) -> tuple[str, str, str]:
    """(field id, option id, option name) for the requested status.

    Node ids rather than `--field`/`--value` names: the id form is `gh`'s
    documented scripting path, it is the only form that works for draft
    items, and resolving it here is what lets a miss report the board's
    real options instead of a bare `gh` failure."""
    number, login, owner = _project(config)
    board = f"{login}/{number}"
    raw = _json(run_cli(["gh", "project", "field-list", number, *owner,
                         "--format", "json", "--limit", "100"]),
                "gh project field-list")
    # isinstance-guarded like `_items` is: an unexpected member shape used
    # to escape as a bare AttributeError/KeyError, outside the CLI's JSON
    # error contract entirely (adversarial-review, lens B).
    fields = [f for f in _as_list(raw.get("fields"), "field-list `fields`")
              if isinstance(f, dict)]
    name = _status_field(config)
    field = next((f for f in fields if keys_match(f.get("name"), name)), None)
    if field is None:
        known = ", ".join(str(f.get("name")) for f in fields) or "(none)"
        raise ProviderError(
            f"project {board} has no field '{name}' — fields: {known}; set "
            "provider.github_project_status_field to the one holding "
            "workflow status")
    field_id = str(field.get("id") or "")
    options = [o for o in _as_list(field.get("options"),
                                   f"field '{name}' `options`")
               if isinstance(o, dict) and o.get("id")]
    if not field_id or not options:
        raise ProviderError(
            f"field '{name}' on project {board} is not a usable "
            f"single-select field (type {field.get('type')!r}, "
            f"{len(options)} option(s)) — status write-back needs one; "
            "point provider.github_project_status_field at a single-select "
            "field")
    for candidate in (to, *_ALIASES.get(_key(to), ())):
        hits = [o for o in options if keys_match(o.get("name"), candidate)]
        if len(hits) > 1:
            # Name folding is deliberately tolerant, so two genuinely
            # distinct columns can still fold together (Indic vowel signs,
            # circled vs plain digits). Taking the first in board order is
            # the silent wrong-column write the Unicode fix existed to
            # remove — refuse instead (re-verification, finding E).
            names = ", ".join(str(o.get("name")) for o in hits)
            raise ProviderError(
                f"'{candidate}' matches more than one '{name}' option on "
                f"project {board} ({names}) — their names differ only by "
                "characters the match folds away; rename one, or name the "
                "exact column with config status_mapping")
        if hits:
            return field_id, str(hits[0]["id"]), str(hits[0].get("name"))
    names = ", ".join(str(o.get("name")) for o in options)
    raise ProviderError(
        f"no '{name}' option matches '{to}' on project {board} — options: "
        f"{names}; remap the milestone with config status_mapping")


def _project_node_id(config: dict) -> str:
    number, login, owner = _project(config)
    raw = _json(run_cli(["gh", "project", "view", number, *owner,
                         "--format", "json"]), "gh project view")
    node = str(raw.get("id") or "")
    if not node:
        raise ProviderError(
            f"gh project view {login}/{number} returned no project id")
    return node


def fetch(config: dict, id: str) -> dict:
    item = _find(config, id)
    content = _content(item)
    body = _text(content.get("body"))
    return {"id": _emit_id(config, item),
            "title": _text(item.get("title")) or _text(content.get("title")),
            "type": type_from_labels(
                [l for l in _as_list(item.get("labels"), "item `labels`")
                 if isinstance(l, str)]),
            # The board column verbatim — `done_state_match` casefolds and
            # tokenizes, so "In Progress" needs no lowercasing here, and
            # keeping it verbatim is what makes transition()'s returned
            # state and a later fetch agree exactly (the shared contract).
            # Empty when the item has no value in that field: `gh` emits a
            # field key only when it is set, so a brand-new item has none.
            "state": _status_of(config, item),
            "description": section(body, "Description") or body,
            "acceptance_criteria": acceptance_criteria(body),
            "provider_ref": f"github-projects:{item_ref(item)}"}


def transition(config: dict, id: str, to: str) -> dict:
    number, _, owner = _project(config)
    item = _find(config, id)
    node = str(item.get("id") or "")
    if not node:
        # Same reasoning as `_project`'s fail-closed: an empty flag value
        # makes `gh` read the NEXT argv token as the item id.
        raise ProviderError(
            f"work item '{id}' resolved to a board item with no item id — "
            "cannot address it for a status write")
    field_id, option_id, option_name = _resolve_option(config, to)
    run_cli(["gh", "project", "item-edit", number, *owner,
             "--id", node,
             "--project-id", _project_node_id(config),
             "--field-id", field_id,
             "--single-select-option-id", option_id])
    return {"id": _emit_id(config, item), "state": option_name}


def add_comment(config: dict, id: str, text: str) -> dict:
    """Comments belong to the backing issue, not the board — a project item
    has no comment stream of its own. Draft items therefore genuinely have
    nowhere to put one; that is an error, not a silent drop."""
    item = _find(config, id)
    content = _content(item)
    if not (content.get("number") and content.get("repository")):
        raise ProviderError(
            f"work item '{id}' is a draft item on the board ("
            f"{content.get('type') or 'DraftIssue'}) — draft items have no "
            "comment stream; convert it to a real issue to record comments")
    run_cli(["gh", "issue", "comment", str(content["number"]),
             "--repo", str(content["repository"]), "--body", text])
    return {"id": _emit_id(config, item), "commented": True}


def create(config: dict, title: str, description: str = "") -> dict:
    """Security-defer follow-up (coverage B9): a draft item on the board.

    Draft rather than a real issue because the board is the configured
    tracker and a draft needs no second repo target to land in — but it
    does mean the follow-up cannot be commented on until someone converts
    it, which `add_comment` says out loud. `item-create`'s response shape
    is assumed rather than captured (see the module docstring), so the id
    is validated here instead of trusted."""
    number, _, owner = _project(config)
    raw = _json(run_cli(["gh", "project", "item-create", number, *owner,
                         "--title", title, "--body", description,
                         "--format", "json"]), "gh project item-create")
    new_id = str(raw.get("id") or "")
    if not new_id:
        raise ProviderError(
            "gh project item-create returned no item id — the follow-up may "
            "or may not exist; check the board before retrying")
    # A draft item has no URL of its own; the board is where it lives.
    return {"id": new_id, "url": str(raw.get("url") or "")}


OPS = {"work_item.fetch": fetch,
       "work_item.transition": transition,
       "work_item.add_comment": add_comment,
       "work_item.create": create}
SUPPORTS = sorted(OPS)
