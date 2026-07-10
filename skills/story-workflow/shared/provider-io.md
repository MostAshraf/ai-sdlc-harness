# shared/provider-io.md — fetch a work item, post a result back

The configured work-item provider is resolved by the harness from
`.claude/context/provider.yaml`; you never name it. There are two transports,
and the recipe differs slightly between them. This file is the single source —
every command cites it.

## Fetch a work item

Run:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness provider --op work_item.fetch --id <id>
```

**CLI / file transport** (`local-markdown`, `github`, `gitlab`, `ado`) prints
the normalized item directly:

```
{"ok": true, "result": {"id": "...", "title": "...", "type": "...",
  "state": "...", "description": "...", "acceptance_criteria": ["…", "…"],
  "provider_ref": "…"}}
```

Work from those fields. For `local-markdown`, `<id>` is the file **stem** inside
`provider.stories_dir` (`WORK-7`, not `stories/WORK-7.md`) — the harness refuses
an id that escapes the stories dir.

**MCP transport** (`ado-mcp`, `jira`, `zoho`): a script cannot call an MCP tool,
so the same command **exits non-zero** and prints the exact MCP tool + args to
invoke — that refusal *is* the instruction. Invoke that tool yourself, capture
its raw JSON, then normalize it to the shape above:

```
printf '%s' '<raw-json>' | ${CLAUDE_PLUGIN_ROOT}/bin/harness provider-normalize --op work_item.fetch
```

⚠️ That refusal message ends by telling you to pipe the raw result into the
`--from-raw` bootstrap. That is the **/dev-workflow** path — it starts a dev run
and seeds `state.yaml`. story-workflow must never start a run, so ignore that
last line and use `provider-normalize` (above), which runs the identical
normalize with no run side-effects.

If the fetch fails or the item isn't found, tell the user plainly and stop.

## Post a result back (only after the user approves)

Story-quality output goes back as a **comment** — never by editing the item's
Description or Acceptance-Criteria fields:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness provider --op work_item.add_comment --id <id> --text '<markdown>'
```

- **CLI / file transport** posts directly (`local-markdown` appends under a
  `## Comments` section in the story file; ADO renders the markdown; GitHub and
  GitLab post native markdown).
- **MCP transport**: as with fetch, the command exits non-zero and names the
  comment tool + args — invoke that MCP tool yourself with the same `<text>`.

Always show the artifact in the conversation and get an explicit yes before
posting. If the user wants changes, iterate first, then post once.

## local-markdown: rewriting a story in place

`add_comment` is non-destructive (a comment bullet), which is exactly what
`analyze` and `groom` want. For `improve` / `refine`, the user may instead want
the refined story to **replace** the source file. That is the one sanctioned
direct write: with the user's explicit go-ahead, use the `Write` tool on the
story's `provider_ref` path (returned by the fetch) to overwrite it with the
refined markdown. If the user declines both a comment and an overwrite, leave
the output in the conversation for them to copy — never overwrite silently, and
never overwrite for `analyze` / `groom` (that would destroy the story).
