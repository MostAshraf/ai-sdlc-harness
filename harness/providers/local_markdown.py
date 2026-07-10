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
    if not path.resolve().is_relative_to(stories.resolve()):
        raise ProviderError(
            f"work item id {item_id!r} escapes stories_dir — refusing")
    if not path.exists():
        raise ProviderError(f"work item '{item_id}' not found at {path}")
    return path


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
    taken = [int(m.group(1)) for p in stories.glob("FU-*.md")
             if (m := re.fullmatch(r"FU-(\d+)", p.stem))]
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
