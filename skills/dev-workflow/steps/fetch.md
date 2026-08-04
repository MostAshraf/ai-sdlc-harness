# Step: fetch (orchestrator-owned, fully mechanical)

Already executed by `${CLAUDE_PLUGIN_ROOT}/bin/harness fetch` at startup — it fetched the work item via
the configured provider, normalized it to `<run>/work-item.json`, resolved
`change_type`, ran the ex-ante mode classifier (full/lean/quick — `Mode:`
hints, quick-disqualify keywords, the workspace `default_mode`), and
bootstrapped `state.yaml` with a seeded task.

## MCP-transport providers (ado-mcp / jira / zoho)

A script can't call an MCP tool, so `${CLAUDE_PLUGIN_ROOT}/bin/harness fetch
--id` **refuses** for these, naming the tool + args to invoke (that refusal is
the instruction). Run step-one in the orchestrator instead:

1. Invoke the mapped `work_item.fetch` tool (`${CLAUDE_PLUGIN_ROOT}/bin/harness
   provider --op work_item.fetch --id <id>` prints the exact tool + args;
   `{project}` from `provider.ado_project`). Capture its raw JSON result.
2. Pipe that raw result into the same bootstrap the CLI path runs:

   ```
   printf '%s' '<raw-json>' | ${CLAUDE_PLUGIN_ROOT}/bin/harness fetch --from-raw
   ```

   `--from-raw` runs the identical normalize → classify → bootstrap, writing
   `work-item.json` and `state.yaml`. On success note `run`, `mode`.

## Already-done work items

If `fetch`'s output carries `already_done` / `warning`, the provider still
has this item in a finished state — an earlier run may already have built
and shipped it. This warns rather than blocks (replays and re-plans are
legitimate), and logs a flagged `work-item-already-done` event. **Surface it
to the user and confirm the re-run is intentional before planning**, and
expect `preflight` to refuse outright if a prior run's branch still occupies
the remote.

## Advance

Nothing further to do here. Advance:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next-per-manifest> --run <run>
```

(`intake` in full and lean modes; in quick, `confirm-repo` when the workspace
registers more than one repo, `preflight` when it registers exactly one.)

The seeded task's repo is fetch's **positional default** (`repos[0]`), not a
scope decision — full/lean replace it at plan-register, quick ratifies it at
`confirm-repo`. Don't present it to the user as a chosen repo.

**Upgrading mid-run:** a run bootstrapped before `repo-ambiguity` existed has
no such artifact, so the quick sequence's predicate raises ("predicate needs
artifact 'repo-ambiguity' which was never recorded") rather than guessing. The
artifact is deliberately not orchestrator-settable — abort the run and
re-fetch, which re-bootstraps it correctly into a fresh same-day slot.
