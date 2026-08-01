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
It then renders `naming.branch` (`{type}/{id}-{slug}`) from state and
**probes the remote for that name before touching anything**. Because the
template is deterministic per work item, a re-run of the same story collides
by construction; a hit refuses with two remedies (branch aside with
`--feature-branch-suffix <s>`, or free the name by merging/deleting the
remote branch and closing its PR/MR) rather than discovering the clash hours
later as a non-fast-forward at push. Resuming the prior work is a third
route but not one *this* run can take: it means continuing in that run's
directory, whose recorded `branches` artifact is what makes preflight skip
the probe. Preflight never adopts a remote branch. A probe that cannot answer — no remote, offline,
auth — continues; a remote that resolved but wouldn't answer logs a flagged
`remote-branch-unverified` event first (a repo with no remote at all is
structural, not a signal, and is skipped silently). Only a confirmed hit
blocks, and it blocks before the clean/default-branch check, so a repo that
is *also* dirty surfaces that on the retry. Note `--branch` is the **base**
override; `--feature-branch-suffix` is the one that changes the feature
branch's own name. In a multi-repo run, **decide the suffix before the first
repo's preflight and pass it to all of them** — a repo this run has already
cut refuses the suffix rather than renaming a branch that may carry commits,
so a mid-run change leaves the repos on differently-named branches (workable
— every artifact is per-repo keyed — but it costs the cross-repo name
correlation a reviewer uses). Then it creates the branch and records the
`branches` artifact. It also pins
`.harness-key` into the repo's `.git/info/exclude` (shared with its task
worktrees), so a stray integrity key can never be swept into git history
by the commit verbs' `git add -A`; the commit verbs refuse-and-unstage as
backstop. Then:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to develop --run <run>
```
