"""local-markdown work-item provider — the no-auth adapter (design.md piece 4).

Work items are markdown files in the configured `provider.stories_dir`:

    # WORK-7: Fix null crash in parser
    Type: Bug
    Status: Open

    ## Description
    ...

    ## Acceptance Criteria
    - [ ] parser returns None on empty input

The normalize part turns that into the generic contract every caller sees
(id / title / type / state / description / acceptance_criteria / provider_ref).
"""
from __future__ import annotations

import glob as globlib
import re
from pathlib import Path

from . import ProviderError

NAME = "local-markdown"
TRANSPORT = "file"
STATUS_DEFAULTS = {"in-progress": "In Progress", "in-review": "In Review",
                   "done": "Done"}
H1_RE = re.compile(r"^#\s+(?:(?P<id>[\w.-]+):\s*)?(?P<title>.+)$", re.MULTILINE)
# The optional `>` / `**` wrappers are v2.x adoption tolerance: legacy
# stories wrote status as a (sometimes bolded) blockquote — `> Status: 📋
# To Do — ...`, `> **Status**: ...` — which a strict match read as absent,
# so every migrated done-story was recorded in work-item.json as "Open" and
# re-offered to the human as open work. Read tolerantly; transition()
# writes back the strict v3.0 `Status:` form, so the file upgrades on first
# write. Both read and write are scoped to the HEADER region (before the
# first `## ` section) via _header(): a quoted `> Status:` inside
# Description is prose, and a whole-file scan used to read it as the item
# state and then REWRITE it (adversarial-review finding).
# the trailing `\**` after the colon covers the colon-inside-bold spelling
# (`**Status:** Done`), which otherwise parsed state as "** Done"
FIELD_RE = {f: re.compile(rf"^(?:>\s*)?\**{f}\**:\**\s*(.+)$",
                          re.MULTILINE | re.IGNORECASE)
            for f in ("Type", "Status")}


def _header(text: str) -> tuple[str, str]:
    """Split at the first `## ` heading — Type/Status live in the header."""
    m = re.search(r"^##\s", text, re.MULTILINE)
    return (text, "") if m is None else (text[:m.start()], text[m.start():])


def _within(path: Path, stories: Path) -> bool:
    """Does `path` resolve INSIDE stories_dir? Resolves symlinks, so a link
    planted in stories_dir that points out of it reads as outside."""
    return path.resolve().is_relative_to(stories.resolve())


def _declared_id(path: Path) -> str | None:
    """The id this file CLAIMS in its H1 (`# US-42: Title` -> 'US-42'), or
    None when the heading declares no id prefix.

    Scoped to the HEADER region, the same way Type/Status already are: an
    `# US-8: ...` line QUOTED inside a `## ` section of a grooming note is
    prose about a story, not a claim to be one, and a whole-file scan read it
    as the latter — re-bricking the story with "matches multiple files"
    (re-verify finding, the same header-scoping lesson FIELD_RE records).

    Never raises: this runs over arbitrary files that merely share a filename
    prefix with a work item. `UnicodeDecodeError` is NOT an OSError, and a
    single cp1252 smart quote in a sibling file was enough to abort the run —
    with `write_back` and (post-merge) `reconcile` dying on an exception
    `_best_effort_transition` does not catch, which is verbatim the field
    failure this whole change exists to prevent (re-verify finding)."""
    try:
        head, _ = _header(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):   # unreadable / directory / dead link / not UTF-8
        return None                 # (UnicodeDecodeError subclasses ValueError)
    m = H1_RE.search(head)
    return m.group("id") if m and m.group("id") else None


def _path(config: dict, item_id: str) -> Path:
    raw = (config.get("provider") or {}).get("stories_dir") or ""
    if not str(raw).strip():
        # Path("") is Path(".") — an unset stories_dir silently hunted for
        # stories in whatever cwd the process had (adversarial-review
        # finding). init-verify also refuses this; double refusal is cheap.
        raise ProviderError(
            "provider.stories_dir is not configured — set it "
            "(init-section --section provider) before using local-markdown")
    stories = Path(raw)
    path = stories / f"{item_id}.md"
    # Confine to stories_dir: an id like '../../x' resolved OUTSIDE it, and
    # transition()/add_comment() would then WRITE there — silent wrong-file
    # I/O, not an error (adversarial-review finding).
    if not _within(path, stories):
        raise ProviderError(
            f"work item id {item_id!r} escapes stories_dir — refusing")
    # field: US-CHAT-00 run. A story file may carry a descriptive slug suffix
    # (`US-CHAT-00-frontend-test-infrastructure.md`) while its H1 declares the
    # short id (`# US-CHAT-00: ...`). fetch() is located by the FULL filename
    # and then hands back that SHORT id (below) — which is what
    # `_bootstrap_from_item` writes to state and what write_back / reconcile
    # later replay into transition(). So the id that resolved going in no
    # longer resolved coming back, and a milestone write-back raised on a
    # fetch that had succeeded moments earlier.
    #
    # Candidates are collected UNIFORMLY — the exact filename plus any slug
    # sibling that DECLARES this id — and >1 refuses. Gating the ambiguity
    # check on "no exact match" instead (the first cut of this fix) left the
    # stated "two files claiming one id is refused" guarantee false in exactly
    # the case where a write is most dangerous: `WORK-7.md` and
    # `WORK-7-add-multiply.md` both declaring `# WORK-7:` silently wrote the
    # former while the run's provider_ref pointed at the latter
    # (adversarial-review, lens A).
    candidates = [path] if path.exists() else []
    # `item_id` is an IDENTIFIER, never a pattern. glob-escape it: unescaped,
    # `US-1[0]` / `SECRE?-9` / `*` each silently resolved a DIFFERENT item's
    # file whenever the pattern happened to hit exactly one — no ambiguity
    # refusal fires on a single match — and `**/../secrets/X` escaped
    # stories_dir outright, because the confinement check above reads the id
    # as a literal path component (where `**` is a directory name and
    # `**/..` collapses back inside) while glob reads it as the recursive
    # wildcard matching zero segments (leaving `..` a real parent hop).
    # CONFIRMED escape, both lenses, reproduced through the shipped CLI.
    for hit in sorted(stories.glob(f"{globlib.escape(item_id)}-*.md")):
        # Confine the path we are actually RETURNING, and do it BEFORE
        # opening it. The check above validated `stories/<id>.md` — a path
        # that by definition does not exist on this branch — so it says
        # nothing about what the glob produced: a symlink planted at
        # `<id>-slug.md` pointing outside stories_dir was refused under its
        # exact name and accepted here (adversarial-review, lens B).
        # Ordered ahead of the read because the first cut checked it AFTER,
        # so every candidate — including out-of-tree symlinks — was opened
        # and slurped whole before anything decided it was in-tree
        # (re-verify finding).
        if not _within(hit, stories):
            raise ProviderError(
                f"work item id {item_id!r} resolves through {hit.name} to a "
                f"path outside stories_dir — refusing")
        # Regular files only. This directory is human-managed, so a candidate
        # can be a directory named `<id>-x.md`, a device node, or a FIFO —
        # and reading a FIFO with no writer blocks FOREVER, wedging every op
        # on that id with no timeout anywhere (re-verify finding).
        if not hit.is_file():
            continue
        # A slug sibling answers to this id only if it SAYS so. The harness's
        # own /story-workflow writes `<id>-readiness.md` / `<id>-technical-
        # notes.md` into this same directory (analyze.md, groom.md); matching
        # on filename shape alone made every one of those brick the story it
        # documents — every op refusing "matches multiple files", telling the
        # human to rename an artifact the harness itself had just created
        # (adversarial-review, lens A). Those reports open `## Story
        # Readiness Report`, declaring no id, so this filter separates a
        # report from a story without either having to know about the other.
        if _declared_id(hit) != item_id:
            continue
        candidates.append(hit)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ProviderError(
            f"work item id {item_id!r} matches multiple files in "
            f"{stories}: {', '.join(p.name for p in candidates)} — "
            f"rename to disambiguate")
    raise ProviderError(f"work item '{item_id}' not found at {path}")


def _section(text: str, heading: str) -> str:
    m = re.search(rf"^##\s+{heading}\s*$(.*?)(?=^##\s|\Z)", text,
                  re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def fetch(config: dict, id: str) -> dict:
    path = _path(config, id)
    text = path.read_text(encoding="utf-8")
    h1 = H1_RE.search(text)
    head, _ = _header(text)
    field = {name: (m.group(1).strip() if (m := rx.search(head)) else "")
             for name, rx in FIELD_RE.items()}
    criteria = re.findall(r"^\s*-\s*\[[ xX]?\]\s*(.+)$",
                          _section(text, "Acceptance Criteria"), re.MULTILINE)
    return {
        "id": (h1.group("id") if h1 and h1.group("id") else path.stem),
        "title": (h1.group("title").strip() if h1 else path.stem),
        "type": field["Type"] or "Task",
        "state": field["Status"] or "Open",
        "description": _section(text, "Description"),
        "acceptance_criteria": criteria,
        "provider_ref": str(path),
    }


def transition(config: dict, id: str, to: str) -> dict:
    path = _path(config, id)
    head, rest = _header(path.read_text(encoding="utf-8"))
    if FIELD_RE["Status"].search(head):
        head = FIELD_RE["Status"].sub(f"Status: {to}", head, count=1)
    else:
        # Insert into the header, never append at the file end — an
        # end-of-file Status would be invisible to the header-scoped read.
        head = head.rstrip() + f"\nStatus: {to}\n" + ("\n" if rest else "")
    path.write_text(head + rest, encoding="utf-8")
    return {"id": id, "state": to}


def add_comment(config: dict, id: str, text: str) -> dict:
    path = _path(config, id)
    body = path.read_text(encoding="utf-8")
    if "## Comments" not in body:
        body = body.rstrip() + "\n\n## Comments\n"
    body = body.rstrip() + f"\n- {text}\n"
    path.write_text(body, encoding="utf-8")
    return {"id": id, "commented": True}


def create(config: dict, title: str, description: str = "") -> dict:
    """Security-defer follow-up (coverage B9), file-transport form: a new
    story file in stories_dir, fetchable by the returned id. Previously
    only the github/gitlab providers implemented create, so the manifest's
    declared `defer -> work_item.create` disposition was a dead end on a
    local-markdown workspace (validation-plan session D would have hit it
    at the approve-security gate). Ids are FU-<n> — a scheme that cannot
    collide with human-authored story names, stays short enough to type
    back into `harness fetch --id`, and never derives from the title (a
    slugged title could escape stories_dir or collide)."""
    raw = (config.get("provider") or {}).get("stories_dir") or ""
    if not str(raw).strip():
        raise ProviderError(
            "provider.stories_dir is not configured — set it "
            "(init-section --section provider) before using local-markdown")
    stories = Path(raw)
    if not stories.is_dir():
        raise ProviderError(f"stories_dir {stories} does not exist")
    # The optional `-<slug>` tail keeps the minter's namespace identical to
    # the RESOLVER's: since `_path` answers `FU-1` with `FU-1-fix-login.md`,
    # a scan that only counted bare `FU-1.md` re-minted `FU-1` and wrote a
    # second file the resolver then preferred — shadowing an existing item
    # behind the id the gate had just recorded (adversarial-review, lens A).
    taken = [int(m.group(1)) for p in stories.glob("FU-*.md")
             if (m := re.fullmatch(r"FU-(\d+)(?:-.*)?", p.stem))]
    item_id = f"FU-{max(taken, default=0) + 1}"
    path = stories / f"{item_id}.md"
    body = (f"# {item_id}: {title}\nType: Task\nStatus: Open\n\n"
            f"## Description\n{description.strip() or title}\n\n"
            f"## Acceptance Criteria\n- [ ] {title}\n")
    path.write_text(body, encoding="utf-8")
    return {"id": item_id, "url": str(path)}


OPS = {"work_item.fetch": fetch,
       "work_item.transition": transition,
       "work_item.add_comment": add_comment,
       "work_item.create": create}
SUPPORTS = sorted(OPS)
