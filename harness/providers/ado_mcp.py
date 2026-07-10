"""Azure DevOps work-item provider, MCP transport (design.md piece 4).

The MCP twin of `ado_cli`: same provider, same normalized contract, different
transport. MCP tools are model-invoked — a script cannot call them — so this
module is the adapter's two script-side parts: the DECLARED MAPPING (which
`mcp__azure-devops__*` tool, which args — the orchestrator executes exactly
this) and NORMALIZE (raw tool result -> the shared contract), the latter
shared with the CLI transport via `ado_common` so switching cli <-> mcp never
duplicates field-mapping. `{project}` is filled from `provider.ado_project`.

Integration checklist (model in the loop — see docs/mcp-integration.md):
Azure DevOps MCP server configured + authed; probe `wit_get_work_item` on a
known id; verify the normalize round-trip on the probe result.
"""
from __future__ import annotations

from .ado_common import STATUS_DEFAULTS, normalize_fetch

NAME = "ado-mcp"
TRANSPORT = "mcp"

MAPPING = {
    "work_item.fetch": {
        "tool": "mcp__azure-devops__wit_get_work_item",
        "args": {"id": "{id}", "project": "{project}", "expand": "relations"}},
    "work_item.transition": {
        "tool": "mcp__azure-devops__wit_update_work_item",
        "args": {"id": "{id}", "project": "{project}",
                 "updates": [{"op": "add", "path": "/fields/System.State",
                              "value": "{to}"}]}},
    "work_item.add_comment": {
        "tool": "mcp__azure-devops__wit_add_work_item_comment",
        "args": {"workItemId": "{id}", "project": "{project}",
                 "text": "{text}"}},
}
SUPPORTS = sorted(MAPPING)

# normalize(config, op, raw) calls with (raw, config); ado_common's shared
# normalizer's second param is the CLI transport's id fallback, not config —
# adapt rather than overload it.
NORMALIZE = {"work_item.fetch": lambda raw, config=None: normalize_fetch(raw)}
