"""Shared Azure DevOps normalize + status defaults (design.md piece 4:
the *normalize part* is one code path per provider, reused across both
transports). `ado_cli` (transport `az boards`) and `ado_mcp` (transport
`mcp__azure-devops__*`) return the same raw `{id, fields}` work-item shape,
so field-mapping / HTML-stripping / AC-extraction live here once — switching
cli <-> mcp never duplicates the mapping and the output contract is identical.
"""
from __future__ import annotations

import re

from ._normalize import acceptance_criteria

# Harness FSM milestone -> ADO state name. ADO has real states, so this is thin.
STATUS_DEFAULTS = {"in-progress": "Active", "in-review": "Active",
                   "done": "Closed"}


def strip_html(text: str) -> str:
    """ADO Description / AcceptanceCriteria fields are stored as HTML."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def normalize_fetch(raw: dict, id: str | None = None) -> dict:
    """Raw ADO work item ({id, fields}) -> the shared work-item contract.
    `id` is the fallback when the raw payload omits it (CLI passes the
    requested id; the MCP result always carries its own)."""
    fields = raw.get("fields", {})
    wid = raw.get("id", id)
    description = strip_html(fields.get("System.Description", ""))
    ac_field = strip_html(
        fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))
    return {"id": str(wid),
            "title": fields.get("System.Title", ""),
            "type": fields.get("System.WorkItemType", "Task"),
            "state": fields.get("System.State", ""),
            "description": description,
            "acceptance_criteria": ([ac_field] if ac_field
                                    else acceptance_criteria(description)),
            "provider_ref": f"ado#{wid}"}
