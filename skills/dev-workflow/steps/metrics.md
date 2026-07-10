# Step: metrics (orchestrator-owned, fully mechanical)

```
${CLAUDE_PLUGIN_ROOT}/bin/harness metrics --run <run>
```

Deterministic aggregation — timings from state, tokens from the ledger,
flagged events — into `<run>/reports/metrics.md`. No agent reasoning. Show
the user the summary. Final mirror **once per preflighted repo** (SKILL.md's
Publish rule — never the workspace), `--push` required so this closing
snapshot (metrics report included) actually reaches each PR's remote
branch instead of stranding locally:
`${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --push --run <run>`.
Then declare the run finished — the successful sibling of abort, terminal
by declaration (mutations refuse from here on; the audit trail stays):

```
${CLAUDE_PLUGIN_ROOT}/bin/harness complete --run <run>
```

The run is done.
