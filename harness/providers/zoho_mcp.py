"""Zoho work-item provider, MCP transport — declared mapping + normalize
(same split as jira_mcp; see docs/mcp-integration.md for the checklist).
Zoho quirks handled in normalize: binary status model, no dedicated AC field."""
from __future__ import annotations

from ._normalize import acceptance_criteria

NAME = "zoho"
TRANSPORT = "mcp"
STATUS_DEFAULTS = {"in-progress": "Open", "in-review": "Open",
                   "done": "Completed"}

MAPPING = {
    "work_item.fetch": {"tool": "mcp__zoho__ZohoMail_getGroupTask",
                        "args": {"taskId": "{id}"}},
    "work_item.transition": {"tool": "mcp__zoho__ZohoMail_editGroupTask",
                             "args": {"taskId": "{id}", "status": "{to}"}},
}
SUPPORTS = sorted(MAPPING)


def normalize_fetch(raw: dict, config: dict | None = None) -> dict:
    task = raw.get("task", raw)
    description = task.get("description") or ""
    return {"id": str(task.get("id", "")),
            "title": task.get("title") or task.get("subject", ""),
            "type": "Task",
            "state": task.get("status", ""),
            "description": description,
            "acceptance_criteria": acceptance_criteria(description),
            "provider_ref": f"zoho:{task.get('id', '')}"}


NORMALIZE = {"work_item.fetch": normalize_fetch}
