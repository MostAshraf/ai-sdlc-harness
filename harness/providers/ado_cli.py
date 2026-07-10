"""Azure DevOps work-item provider, CLI transport (`az boards`). Auth =
`az login` / `az devops login`. ADO has real work-item types and states, so
normalization is thin; org/project come from config or az defaults. The
field-mapping is shared with the MCP transport (`ado_mcp`) via `ado_common`."""
from __future__ import annotations

import json

from .ado_common import STATUS_DEFAULTS, normalize_fetch
from ._normalize import run_cli

NAME = "ado"
TRANSPORT = "cli"


def _scope_args(config: dict) -> list[str]:
    p = config.get("provider") or {}
    args = []
    if p.get("ado_org"):
        args += ["--organization", p["ado_org"]]
    return args


def fetch(config: dict, id: str) -> dict:
    raw = json.loads(run_cli(["az", "boards", "work-item", "show",
                              "--id", str(id), *_scope_args(config),
                              "--output", "json"]))
    return normalize_fetch(raw, id)


def transition(config: dict, id: str, to: str) -> dict:
    run_cli(["az", "boards", "work-item", "update", "--id", str(id),
             *_scope_args(config), "--state", to, "--output", "json"])
    return {"id": str(id), "state": to}


def add_comment(config: dict, id: str, text: str) -> dict:
    run_cli(["az", "boards", "work-item", "update", "--id", str(id),
             *_scope_args(config), "--discussion", text, "--output", "json"])
    return {"id": str(id), "commented": True}


OPS = {"work_item.fetch": fetch,
       "work_item.transition": transition,
       "work_item.add_comment": add_comment}
SUPPORTS = sorted(OPS)
