---
name: add-repo
description: >
  Register one new repo into an already-bootstrapped workspace, without
  disturbing repos already registered or re-running the full interview.
  USER-ENTRY — invoke only when the user explicitly runs /add-repo; never
  autonomously, never from a subagent (guard-enforced).
---

# add-repo

Every command below is `${CLAUDE_PLUGIN_ROOT}/bin/harness <verb> …` — run it
yourself via Bash. Never ask the user to type a `harness` command; the user
only answers the questions below.

## 1 · Ask

- Repo name (must be new — case-insensitively distinct from every
  already-registered name) and its local path.

## 2 · Discover, then confirm

`${CLAUDE_PLUGIN_ROOT}/bin/harness discover --repo <path>` — same discovery
`/init-workspace` uses. It first ensures the repo is clean and on its
default branch: a dirty repo, or one mid-rebase/merge, refuses with a clear
error — surface that to the user verbatim, never auto-stash/discard. If the
guessed default branch doesn't resolve (no `origin/HEAD`), ask the user to
name it explicitly, then pass `--branch <name>` yourself; a repo with no
`origin` and a stray local branch that happens to be named `main` can't be
told apart from a genuine one, so confirm rather than assume.
**Known risk:** running `discover` against a path that's actually already
registered, with a `/dev-workflow` run in progress against it, can switch
that run's feature-branch checkout back to default — if there's any chance
the path is already registered, check first rather than running `discover`
on it.

Present the proposals (language, `test_cmd`, default branch) as
defaults-to-confirm, not facts:

- Confirm `test_cmd` by actually running it — don't accept the proposal
  unconfirmed, and never collapse this repo's own command onto another
  registered repo's.
- A `monorepo_split` proposal means this "one repo" is actually several
  logical repos sharing one `.git` at the physical root. **This isn't yet
  representable as separate registered repos** — `init-verify`'s
  `repo:<name>` check requires a `.git` directly under the registered path,
  which no subtree has, so registering subtrees separately leaves every one
  of them permanently failing verification with no available fix. Tell the
  user this is a known gap; register the repo once, at its physical root.

## 3 · Register

```
${CLAUDE_PLUGIN_ROOT}/bin/harness add-repo --name <n> --path <path> --test-cmd '<confirmed cmd>'
```

This merges into the existing repo/language config — every already-
registered repo survives untouched. It refuses (never renames/overwrites/
aliases) on:

- `--name` already registered, compared case-insensitively — surface this
  verbatim and ask the user for a different name.
- `--path` already registered under a different name — surface this
  verbatim; the repo is very likely already set up, so confirm with the
  user rather than retrying with a new name.

`--test-cmd` is optional — omitting it registers the repo but leaves
`init-verify`'s `test_cmd:<name>` check failing until a command is set via
`init-section --section language` (merge the new repo's entry into the
existing `language.repos` map — that section is still full-replace, so
resupply the whole map, not just this repo's entry).

## 4 · Verify + finish

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness init-verify` — every check must pass
   (or be `manual` with the user's explicit acknowledgment, for MCP-
   transport work-item providers). **Do not proceed on failures** — show
   the remediation, fix, re-run.
2. `${CLAUDE_PLUGIN_ROOT}/bin/harness init-finalize` — refreshes the
   permission allowlist to cover the new repo (its `test_cmd` binary,
   `Read` on its path). Confirm `.claude/settings.json` merged cleanly.
3. **Repo-map**: offer to generate now — run `/repo-map-refresh`'s step 2
   procedure for this repo (that skill owns the exact subagent_type-
   guessing warning and the `harness-mode: repo-map` spawn/stamp sequence;
   don't restate it here), pointed at
   `.claude/context/repo-map/<repo-name>/`.
4. Tell the user the new repo is ready for `/dev-workflow`.

## Known risk

`security-scan` scans every registered repo in one call, regardless of
which repo a given `/dev-workflow` run's tasks touch — avoid running
`/add-repo` while another run is active for an unrelated repo, in case the
newly-added one isn't a valid checkout yet.
