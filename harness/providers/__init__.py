"""Provider adapter layer (design.md piece 4).

Callers name an *operation*, never a provider: `dispatch(config, "work_item.fetch",
id=...)` routes to the configured provider's implementation and returns the
normalized contract. Each provider module exposes:

    OPS: dict[operation-name, callable(config, **kwargs) -> dict]
    SUPPORTS: list[operation-name]     # absent op == declared-unsupported

The shared contract test in tests/test_providers.py is what every provider
must pass — the real version of the original's never-implemented parity tests.
"""
from __future__ import annotations


class ProviderError(Exception):
    pass


class ProviderUnsupported(ProviderError):
    """Operation not supported by the configured provider (declared, clean)."""


_REGISTRY = {
    "local-markdown": "local_markdown",   # file transport, no auth
    "github": "github_cli",               # cli transport (gh)
    "gitlab": "gitlab_cli",               # cli transport (glab)
    "ado": "ado_cli",                     # cli transport (az boards)
    "ado-mcp": "ado_mcp",                 # mcp transport (mcp__azure-devops__*)
    "jira": "jira_mcp",                   # mcp transport (mapping + normalize)
    "zoho": "zoho_mcp",                   # mcp transport (mapping + normalize)
}


def get_module(config: dict):
    name = (config.get("provider") or {}).get("work_item")
    modname = _REGISTRY.get(name)
    if modname is None:
        raise ProviderError(f"unknown work-item provider '{name}' — one of: "
                            f"{', '.join(sorted(_REGISTRY))}")
    import importlib
    return importlib.import_module(f".{modname}", __package__)


def dispatch(config: dict, operation: str, **kwargs) -> dict:
    """CLI/file-transport providers execute here. MCP-transport providers
    cannot be script-called: the model invokes the declared MAPPING tool, then
    (for `work_item.fetch`) pipes the raw result to `harness fetch --from-raw`,
    which runs the shared normalize + bootstrap."""
    mod = get_module(config)
    if getattr(mod, "TRANSPORT", "") == "mcp":
        entry = mod.MAPPING.get(operation)
        if entry is None:
            raise ProviderUnsupported(
                f"provider '{mod.NAME}' declares no support for '{operation}' "
                f"(supports: {', '.join(mod.SUPPORTS)})")
        base = (f"'{mod.NAME}' is MCP-transport: invoke tool '{entry['tool']}' "
                f"with args {entry['args']} (filled from your params)")
        if operation == "work_item.fetch":
            raise ProviderError(
                base + ", then pipe the raw result to `harness fetch "
                "--from-raw` — it normalizes + bootstraps the run "
                "(see steps/fetch.md).")
        raise ProviderError(base + ".")
    fn = mod.OPS.get(operation)
    if fn is None:
        raise ProviderUnsupported(
            f"provider '{mod.NAME}' declares no support for '{operation}' "
            f"(supports: {', '.join(sorted(mod.SUPPORTS))})")
    return fn(config, **kwargs)


def normalize(config: dict, operation: str, raw: dict) -> dict:
    """The shared, scriptable normalize path for MCP-transport results.
    Normalizers take (raw, config): field mapping can be instance-specific
    (e.g. Jira acceptance-criteria custom-field ids are per-instance
    `customfield_NNNNN` values, not a universal name)."""
    mod = get_module(config)
    fn = getattr(mod, "NORMALIZE", {}).get(operation)
    if fn is None:
        raise ProviderUnsupported(
            f"provider '{mod.NAME}' has no normalizer for '{operation}'")
    return fn(raw, config)
