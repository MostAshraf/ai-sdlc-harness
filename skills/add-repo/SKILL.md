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
on it. The same hazard reaches a path that is merely **inside** a checkout
some other registered repo already covers (the monorepo shape below): the
branch switch flips the whole shared tree, so check for an in-flight run on
every logical repo sharing that checkout, not only on this exact path.

Present the proposals (language, `test_cmd`, default branch) as
defaults-to-confirm, not facts:

- Confirm `test_cmd` by actually running it — don't accept the proposal
  unconfirmed, and never collapse this repo's own command onto another
  registered repo's.
- A proposal may carry **no `test_cmd`**: no single command covers that
  root (a .NET root with no solution file, or two side by side). Ask the
  user; never synthesise one. `init-verify` gates on invocability only, so
  a command that cannot even locate its project still reports `pass`, and
  the first `verify-red` then seals a red-proof over a build error.
- A `monorepo_split` proposal means this "one repo" is actually several
  logical repos sharing one `.git` at the physical root. **Register each
  proposed root as its own logical repo** — one `/add-repo` pass per root
  (this run registers one; tell the user the rest each need their own run),
  not one registration at the checkout. `monorepo_split` lists the `root` of
  every proposal, checkout-relative in the platform's own separators
  (backslashed on Windows), `.` meaning the checkout itself; the path to
  register is `<checkout>/<root>` (plain
  `<checkout>` for `.`). Suggest `<repo>-<root>` names (`xtream-backend` for
  the `.` root holding the solution, `xtream-frontend` for `frontend/`) and
  let the user overrule the names, not the shape. `init-verify` passes any
  path inside a git work tree, root or subtree, and says `<path> (subtree of
  <checkout>)` when they differ — confirmation, not a warning. Confirm each
  root's `test_cmd` **from that subtree** (`cd <checkout>/frontend` first):
  proposed commands are subtree-relative and the harness runs them with the
  registered path as cwd. Say plainly what the user gets: per-task worktrees
  still isolate (built from the physical checkout, task works in
  `<worktree>/<subtree>`, staging bounded to the subtree), but outside
  worktrees both logical repos sit on the SAME branch — `preflight` cuts it
  in the shared checkout — and a parent root legally contains its child, so
  only review catches a parent task editing the child's files.

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

`--path` may be a **subtree** of a checkout another registered repo already
covers — that's the monorepo shape above, and it is not a collision: the
duplicate-path refusal compares resolved paths exactly, so `<checkout>` and
`<checkout>/frontend` are two distinct registrations while the identical
path twice is still refused. Pass the subtree path verbatim and register the
subtree's own `--test-cmd` with it; nothing downstream rewrites the path back
to the physical checkout root.

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

Going from one repo to several does not reach an in-flight **quick** run: its
`confirm-repo` predicate reads the `repo-ambiguity` artifact recorded once at
`fetch`, so it still walks straight to `preflight` on the only repo it knew
about. Runs fetched after this command see the choice; to move an in-flight
one, abort and re-fetch.
