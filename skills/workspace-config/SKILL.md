---
name: workspace-config
description: >
  Change one config section (overrides / provider / language / repos) on an
  already-bootstrapped workspace, without re-running the full /init-workspace
  interview. USER-ENTRY — invoke only when the user explicitly runs
  /workspace-config; never autonomously, never from a subagent
  (guard-enforced).
---

# workspace-config

Every command below is `${CLAUDE_PLUGIN_ROOT}/bin/harness <verb> …` — run it
yourself via Bash. Never ask the user to type a `harness` command; the user
only answers the questions below.

Not this skill's job: **registering a brand-new repo** (that's `/add-repo` —
discovery, duplicate-name/path refusal, permission-allowlist refresh; this
skill only *changes* an already-registered repo's path/name/`test_cmd`), and
**first-time setup** (`/init-workspace`'s interview).

## 0 · Confirm the workspace is actually bootstrapped

Read `.claude/context/overrides.yaml`. Missing, or no `bootstrap_completed`
key: `/init-workspace` never finished — stop and send the user there.
Proceeding anyway reports "the change is live," and the user only learns
otherwise later, at a confusing `bootstrap incomplete` refusal the next
time `/dev-workflow` runs.

## 1 · Ask which section, and what's changing

- `overrides` — any top-level key in `config/defaults/*.yaml` other than
  `provider`/`repos`/`language` (`status_mapping`, `subagent_models`,
  `quick_mode`, `naming` + `change_types`, `review_rounds`, `stall`,
  `repo_map` staleness N, `review_policy`, `security.scan_cmd`/
  `severity_order`, …). Most single-setting changes land here.
- `provider` — work-item/git provider + specifics (`stories_dir`,
  `github_repo`, `ado_org`, `ado_project`, …).
- `language` — per-repo `test_cmd`, under `language.repos.<name>` (not the
  whole-workspace `test_paths`/`test_closure`).
- `repos` — repointing or renaming an *existing* entry's path.

## 2 · Read the current section before writing it

`overrides` deep-merges — a call only needs the keys actually changing:
`init-section --section overrides --json '{"quick_mode": {"loc_max": 50}}'`.
Exception: a **list**-valued key (`review_policy` is the only shipped one)
still replaces wholesale — dicts recurse, list items don't — so resend the
whole list, including entries you're not changing, or one silently vanishes.

`provider`/`language`/`repos` are full-**replace**: the write IS the whole
file. Read `.claude/context/<section>.yaml` first (Read tool, never a Bash
one-liner), splice in the change, resend the **complete** set — a partial
payload, or a forgotten self-nesting (`{"repos": {...}}` — `overrides` is
the one flat, non-nested exception), silently drops every entry you didn't
restate. For `repos`/`language` this is NOT caught by `init-verify` if it
zeroes the whole map (step 4 covers what verify does check).

**Known gap**: `overrides` only adds/updates keys, never unsets — removing
one needs a direct edit to `overrides.yaml`; say so rather than improvising.

## 3 · Confirm a `test_cmd` change by running it, and mind two risks

Confirm any `test_cmd` change by actually running it in that repo — same
rule `/add-repo`/`/init-workspace` follow, never accept it unconfirmed.

**Known risks**: (1) `init-section --section repos` has none of `/add-repo`'s
collision checks — a duplicate name (case-insensitive: collides on-disk on
a case-insensitive filesystem) or a path under two names (misattributes
`test_cmd`/`scan_cmd`) writes cleanly; check the full map yourself before
sending it. Renaming also orphans `language.repos.<old-name>`,
`security.scan_cmd.<old-name>`, and `.claude/context/repo-map/<old-name>/`
— carry them to the new name or tell the user to. (2) Avoid repointing a
path or changing a `test_cmd` while a `/dev-workflow` run has in-flight
tasks against that repo — resolution is by exact registered path, so a
run mid-flight can start failing with a confusing "no test command" error.

## 4 · Re-verify if the section could affect verify or permissions

`init-verify` covers `provider`, `repos` (≥1 registered, each a real git
checkout), and `language` (`test_cmd` runnability) — **not** `overrides`.

- `provider` only: re-run `init-verify`, fix any failure.
- `repos` (a path) or `language` (a `test_cmd` binary): re-run `init-verify`,
  **then** `init-finalize` too — that's what refreshes the permission
  allowlist (`Read` on the repo path, `Bash` on the `test_cmd` binary) to
  the new value; skip it and the OLD path/binary stays allow-listed while
  the new one doesn't, so a background/`mode:auto` run hits an unprompted
  permission stall the first time it touches the changed repo.
- `overrides` only: no re-verify/re-finalize needed.

## 5 · Tell the user what changed

State the section, the exact keys written, and (if re-verified/re-finalized)
the result.
