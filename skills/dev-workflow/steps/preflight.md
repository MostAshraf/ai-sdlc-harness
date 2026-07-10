# Step: preflight (orchestrator-owned, fully mechanical)

Create the feature branch in each affected repo from the declared naming
template — one owned command per repo:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness preflight --repo <repo-path> --run <run>
```

First ensures the repo is clean and on its default branch (the reusable
precondition `discover` also uses at `/init-workspace` time, standalone as
`${CLAUDE_PLUGIN_ROOT}/bin/harness ensure-default-branch --repo <repo-path>
[--branch <name>]` for any future step that needs it) — a dirty repo, or
one mid-rebase/merge, refuses
(surface to the human, never auto-stash/discard/continue); a clean repo on
a different branch is switched, no confirmation needed. Pass `--branch
<name>` to override the auto-resolved guess. Idempotent on retry, per
repo: a `branches` entry already recorded for *this* repo is returned
directly rather than re-derived — a second repo's preflight is never
satisfied by the first repo's record (each repo gets its own entry). **Known risk:** two runs started concurrently
against the *same* repo path can race here (no repo-level lock exists yet)
— use a separate clone/checkout per concurrent run against the same repo.
Then it renders `naming.branch` (`{type}/{id}-{slug}`) from state, creates
the branch, and records the `branches` artifact. It also pins
`.harness-key` into the repo's `.git/info/exclude` (shared with its task
worktrees), so a stray integrity key can never be swept into git history
by the commit verbs' `git add -A`; the commit verbs refuse-and-unstage as
backstop. Then:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to develop --run <run>
```
