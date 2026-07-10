---
name: story-workflow
description: >
  Refine, analyze, improve, and groom user stories / work items for quality —
  readiness reports, template restructuring, adaptive gap-filling, and
  codebase-aware technical grooming. USER-ENTRY and HUMAN-ONLY — invoke only
  when the user explicitly runs /story-workflow <command>; never autonomously
  from conversation, never from a subagent (guard-enforced). Distinct from the
  /dev-workflow pipeline: this shapes the story itself, it does not build it.
---

# story-workflow — story-quality refinement (PO-facing)

Improves work-item quality through four commands. Read-mostly: it fetches a
work item, helps shape it, and — only with the user's consent — posts the
result back as a comment. It never starts a dev run and never touches
`ai/<run>/` run state.

Every harness call is `${CLAUDE_PLUGIN_ROOT}/bin/harness <verb> …` — the full
path, run by you via Bash (a bare `harness` is not on PATH, and shell variables
set in one Bash call do not persist to the next). Non-zero exit = refused; read
the JSON error and act on it.

## Usage

```
/story-workflow <command> <work-item-id> [session-notes…]
```

`<command>` is one of `analyze`, `refine`, `improve`, `groom`. `<work-item-id>`
is whatever the configured provider uses — a number (ADO / GitHub / GitLab), a
key (`PROJ-123`, Jira), or, for `local-markdown`, the story's **id** (the file
stem inside `provider.stories_dir`, e.g. `WORK-7` for `WORK-7.md` — not a
path). Any trailing tokens are session notes, passed through to `improve` and
`refine`.

## Routing

| command | file | one-line |
|---|---|---|
| `improve` | `commands/improve.md` | adaptive single-pass: assess → gap-fill → draft (the recommended default) |
| `analyze` | `commands/analyze.md` | standalone readiness report with flags |
| `refine`  | `commands/refine.md`  | slow, section-by-section interactive restructure |
| `groom`   | `commands/groom.md`   | codebase-aware per-repo technical notes |

Parse `$ARGUMENTS`: the first token is the command, the second is the work-item
id, the rest are notes. If the command is missing or unknown, print the usage
line plus the table above and stop — do not guess. If the id is missing, ask
for it — never proceed without one. Then read the one matching command file and
follow it (context economy: load a single command file, not all four).

## Before routing

1. Confirm the workspace is bootstrapped: `.claude/context/provider.yaml` must
   exist. If it doesn't, `/init-workspace` never ran — send the user there and
   stop. The active provider is resolved from config by the harness; you never
   hand-pick it.
2. The recipe for fetching a work item and posting a result back — for every
   provider and both transports — lives once in `shared/provider-io.md`. Read
   it; the commands cite it instead of repeating it.
3. Domain and convention context (for `improve` and `groom`) lives once in
   `shared/context.md`.

## Guardrails (all four commands)

- On a remote provider, never edit the item's Description or Acceptance-Criteria
  fields — post back as a **comment** only. The single exception is a
  `local-markdown` story the user explicitly asks you to rewrite in place; see
  `shared/provider-io.md`.
- Never invent business requirements. A gap is a question for the human, not an
  assertion of what "should" be there.
- Mirror the user's domain language; don't rename their "platform" to "system".
- Files in `templates/` are read-only references — don't modify them while a
  command runs.
- If a story is already well-formed, say so. "This looks ready" is a valid,
  valuable outcome — don't manufacture flags or questions to look busy.
