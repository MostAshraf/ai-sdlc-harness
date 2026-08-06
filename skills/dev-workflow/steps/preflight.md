# Step: preflight (orchestrator-owned, fully mechanical)

Pass the run's **ratified** repos — plan-register's task list in full/lean,
`confirm-repo`'s in quick — never "whichever repo is first in repos.yaml".
Reaching this step proves a ratification *happened* (the cursor refuses to
leave the owning step otherwise), but `--repo` itself is not checked against
it: this verb cuts a branch wherever you point it. Read the repo off
`show`'s `tasks[*].repo`, don't retype it.

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
a different branch is switched, no confirmation needed.

**On, not at.** That check fetches and reports `behind` — how far the base
trails its remote — but deliberately does NOT pull: the branch is still cut
from the LOCAL tip, because pulling could conflict or absorb upstream code
the human never asked for. A non-zero count logs a flagged
`base-branch-behind` event, and it matters more than it looks: every suite
this run executes (the red-proof, develop, the reviewer's re-run) runs
against that base, so a stale one means green tests that say nothing about
what the change will merge into. **Surface the count to the user** and let
them decide to update the base first — the verb that executes that decision is
`${CLAUDE_PLUGIN_ROOT}/bin/harness update-base --repo <repo-path> [--branch
<name>]`: fetch + **fast-forward only**, refusing a diverged base, a base that
is itself checked out and dirty, or a remote that didn't answer. It never runs
on its own, and it moves the base REF without switching your checkout (raw
`git pull`/`git merge` stays blocked — this is the owned entry point). Run it
only on the user's word. **Do not "re-run preflight" to pick the update up:**
this step is idempotent per repo (below), so a re-run returns the recorded
branch rather than re-cutting it, and the feature branch would stay parked at
the stale tip. By the time you read the count the branch already exists, so
the sequence that actually terminates is `update-base` on the base, then
`${CLAUDE_PLUGIN_ROOT}/bin/harness sync-branch --repo <repo-path> --onto
<base>` from the feature branch to rebase it across the gap. In full/lean the
question should already have been asked at plan step 0a (`base-check`); a
count that still shows here means it wasn't acted on, and quick mode has no
plan step at all, so this is that mode's only asking point. `behind: null` just means the
question was unanswerable (no remote, offline, auth) and is not a signal. Pass `--branch
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
