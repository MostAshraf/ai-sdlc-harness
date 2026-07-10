---
name: migrate-workspace
description: >
  Adopt a v2.x ai-sdlc-harness workspace into v3.0 — config carries over,
  run history stays archived in place. USER-ENTRY and HUMAN-ONLY — invoke
  only when the user explicitly runs /migrate-workspace; never autonomously,
  never from a subagent (guard-enforced).
---

# migrate-workspace — adopt a v2.x workspace

**The fork seam:** this file and `harness/migrate.py` are the replaceable
unit — forks ship different adoption logic by swapping them and nothing
else. Everything that WRITES goes through the same owned path as
`/init-workspace` (`init-section` → `init-verify` → `init-finalize`);
migrate verbs are read-only proposals.

Human-only, same invocation rules as every skill here: always the full
`${CLAUDE_PLUGIN_ROOT}/bin/harness <verb>` path. First run
`/init-workspace`'s **step 0 environment bootstrap** exactly as written
there (that file owns the procedure — don't re-derive it).

## 1 · Detect (stop conditions first)

```
${CLAUDE_PLUGIN_ROOT}/bin/harness migrate-detect
```

- Surface `warnings` verbatim whenever present — e.g. an unreadable
  `overrides.yaml` reports as bootstrapped *fail-closed*, and the
  remediation lives in the warning, not here.
- `already_bootstrapped: true` → this workspace already runs v3.0. If
  `inventory.legacy_context_files` is non-empty, a prior migration stopped
  before archiving — offer the **archive-only finish** (steps 5–6; the
  report on this path records `inventory` only, since `migrate-extract`
  rightly refuses post-bootstrap — the carried-config record lives in the
  earlier attempt's confirmations); otherwise **stop** and point the user
  at `/workspace-config`.
- `legacy: null` → **stop**: nothing to adopt; point at `/init-workspace`.
- Otherwise show the `evidence` list (why this looks like v2.x), then the
  `inventory`:
  - **`in_flight` runs get a hard boundary, stated plainly:** run history
    is never converted — v3.0 state is sealed evidence (red-proofs,
    ledgers) that v2.x never produced, so migrating a half-done run would
    fabricate its audit trail. **Recommend finishing or abandoning those
    stories on v2.x before migrating**: step 5 archives the very config
    files v2.x resumes from, so proceeding now means a later v2.x session
    must first hand-restore them from `legacy-2.1/`. Proceeding anyway is
    the user's call — record the choice in the step-5 report.
  - Old `ai/` run dirs and `aborted` leftovers stay exactly where they are
    as readable archives; v3.0 run discovery ignores them.

## 2 · Extract & confirm (propose-only)

```
${CLAUDE_PLUGIN_ROOT}/bin/harness migrate-extract
```

Present the proposal per section — `provider`, `repos`, `language` — next
to every `notes` entry (missing repo paths, unverifiable stories_dir) and
the `unmapped` list (what v2.x config has no v3.0 home, and why). The user
confirms or corrects **each section**; anything extract could not fill
falls through to the matching `/init-workspace` interview question — never
guess a value extraction didn't find.

**Re-entry:** if a target section file already exists under
`.claude/context/` (a prior attempt got that far), read it and present its
current value next to the legacy-derived proposal — never silently
re-propose stale legacy data over a correction the user already confirmed.

`optional_overrides.naming` is **opt-in, default NO**: translated v2.1
naming templates are usually the v2.1 defaults the user never chose, and
v3.0's `{type}/{id}-{slug}` handles change-types better than a frozen
`feature/` prefix. Offer it only as "did you deliberately customize
naming in v2.x?".

## 3 · Apply through the owned init path

Write each confirmed section with `init-section` exactly as
`/init-workspace` step 1 documents (self-nested payloads; `overrides`
flat). Then fill the gaps the same way that interview does:

- No `language` proposal for a repo → `discover --repo <path>` and confirm
  (its dirty-repo/default-branch cautions live in `/init-workspace` step 2).
  **`discover` switches a clean repo back to its default branch** — if
  `inventory.in_flight` names a story whose v2.x feature branch may be
  checked out in that repo, say so and get explicit confirmation first.
- MCP-transport providers (`jira`, `zoho`, `ado-mcp`) → the
  model-in-the-loop MCP checklist from the interview applies unchanged.

## 4 · Verify + finalize (the same real gate)

`/init-workspace` step 4, verbatim: `init-verify` must pass (fix failures,
re-run), then `init-finalize`. A migrated workspace ends in exactly the
state a freshly-interviewed one does — there is no "migrated" flavor to
special-case later.

## 5 · Archive the legacy config

List the v2.x context files (`inventory.legacy_context_files`), confirm
with the user, then move them aside so nobody mistakes them for live
config:

```
mkdir -p .claude/context/legacy-2.1 && mv .claude/context/*.md .claude/context/legacy-2.1/
```

Write `.claude/context/legacy-2.1/migration-report.md`: what carried over
(per section), the full `unmapped` and `notes` lists, and any in-flight
v2.x stories with the finish-on-v2.x guidance. The old `ai/` dirs are the
run archive; this report is the config-decision record.

## 6 · Hand over

- `local-markdown` users: existing story files work in place — v2.1
  `> Status:` blockquotes are read tolerantly and upgrade to the v3.0
  `Status:` form on first write-back.
- Remote-provider users: work items live server-side; nothing moved.
- Offer repo-map generation (`/repo-map-refresh` owns the procedure),
  then: `/dev-workflow <work-item-id>` is ready.
