"""Jira work-item provider, MCP transport (design.md piece 4).

MCP tools are model-invoked — a script cannot call them. So this module is
the adapter's two script-side parts: the DECLARED MAPPING (which tool, which
args — the model executes exactly this, nothing more) and the NORMALIZE
functions (raw tool result -> the shared contract), runnable via
`harness provider-normalize` so the code path stays testable.

Integration checklist (model in the loop — see docs/mcp-integration.md):
Jira MCP server configured + authed; probe `getJiraIssue` on a known key;
verify the normalize round-trip on the probe result.
"""
from __future__ import annotations

NAME = "jira"
TRANSPORT = "mcp"
STATUS_DEFAULTS = {"in-progress": "In Progress", "in-review": "In Review",
                   "done": "Done"}

MAPPING = {
    "work_item.fetch": {"tool": "mcp__jira__get_issue",
                        "args": {"issueKey": "{id}"}},
    "work_item.transition": {"tool": "mcp__jira__transition_issue",
                             "args": {"issueKey": "{id}", "to": "{to}"}},
    "work_item.add_comment": {"tool": "mcp__jira__add_comment",
                              "args": {"issueKey": "{id}", "body": "{text}"}},
}
SUPPORTS = sorted(MAPPING)


def _adf_text(node) -> str:
    """Flatten Atlassian Document Format to plain text (minimal, lossless
    enough for planning input)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if isinstance(node, dict):
        own = node.get("text", "")
        sep = "\n" if node.get("type") in ("paragraph", "listItem",
                                           "heading") else ""
        return own + _adf_text(node.get("content", [])) + sep
    return ""


def normalize_fetch(raw: dict, config: dict | None = None) -> dict:
    fields = raw.get("fields", {})
    description = _adf_text(fields.get("description")) .strip()
    from ._normalize import acceptance_criteria
    # A real Jira AC field is an instance-specific `customfield_NNNNN` id —
    # the old hardcoded `customfield_ac` matched no real instance ever, so
    # AC extraction always silently fell back to description heuristics
    # (adversarial-review finding). `provider.jira_ac_field` declares it.
    ac_field = ((config or {}).get("provider") or {}).get("jira_ac_field")
    ac = (fields.get(ac_field) if ac_field else None) \
        or acceptance_criteria(description)
    if not isinstance(ac, list):
        ac = [ac]
    # entries must be strings for every downstream consumer; an ADF/dict
    # custom-field value flattens through the same ADF walker
    ac = [a if isinstance(a, str) else _adf_text(a) for a in ac]
    return {"id": raw.get("key", ""),
            "title": fields.get("summary", ""),
            "type": (fields.get("issuetype") or {}).get("name", "Task"),
            "state": (fields.get("status") or {}).get("name", ""),
            "description": description,
            "acceptance_criteria": ac,
            "provider_ref": f"jira:{raw.get('key', '')}"}


NORMALIZE = {"work_item.fetch": normalize_fetch}
