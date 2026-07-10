# Step: reconcile (orchestrator-owned, fully mechanical)

## MCP-transport work-item providers (ado-mcp / jira / zoho)

A script can't call an MCP tool, so plain `${CLAUDE_PLUGIN_ROOT}/bin/harness reconcile` would try to
dispatch `work_item.transition` itself and refuse every time write-back is on
(the default). Run the transition in the orchestrator instead, same pattern
as `fetch.md`:

1. If `write_back.on_done` (or the milestone in play) is enabled, invoke the
   mapped `work_item.transition` tool (`${CLAUDE_PLUGIN_ROOT}/bin/harness
   provider --op work_item.transition --id <id> --to <status>` prints the
   exact tool + args to call).
2. Then run reconcile with the transition skipped (already done above):
   ```
   ${CLAUDE_PLUGIN_ROOT}/bin/harness reconcile --skip-transition --run <run>
   ```

## CLI-transport providers (github / gitlab / ado / local-markdown)

```
${CLAUDE_PLUGIN_ROOT}/bin/harness reconcile --run <run>
```

Provider status write-back (conservative default: done), archives completed
tasks (declared FSM edge), sweeps leftover worktrees. Then advance to
`metrics`.
