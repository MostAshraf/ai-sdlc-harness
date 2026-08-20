# Step: develop (orchestrator loop — spawns developer + reviewer per task)

Dispatch policy: **DAG-driven and pipelined**. `depends_on` is the ONLY
ordering authority, enforced at both ends — plan-register refuses cycles and
dangling ids, the task FSM refuses `pending -> in-progress` while a
dependency is unfinished, and the plan contract forbids declaring a
dependency that isn't real. Everything not so declared runs CONCURRENTLY,
across repos and within one repo alike. (This SUPERSEDES the M5 lane policy,
"sequential within a repo": that rule was a proxy for merge contention on the
shared feature-branch checkout, which `merge-task` now owns directly — it
holds the run's exclusive lock across the merge and refuses an unfinished git
operation, a dirty tree, or a HEAD that is not this run's feature branch, in
that order.) The exit stays fail-closed: the
cursor cannot leave `develop` while any task is pending/in-progress/in-review.

**The residual, stated plainly:** a worktree is cut from the feature branch
as it stands, so a co-dispatched sibling's tree does NOT contain the other's
code until that other merges and a fresh worktree is cut. Every lane
therefore builds and tests against a partial branch; `reconcile-contracts`
and the pre-PR review, both run on the MERGED feature branch after every
lane lands, are where integration is actually checked. Co-dispatch freely
across repos (separate checkouts, nothing shared). Within one repo the same
holds, plus the file-overlap stagger in step 2 below.

Once, before the first task: `${CLAUDE_PLUGIN_ROOT}/bin/harness write-back
--milestone develop_start --run <run>` (no-ops cleanly if
`write_back.on_develop_start` is off or the provider/type resolve no target;
for an MCP-transport work-item provider it also no-ops, returning
`mcp_guidance` — invoke the named tool yourself if you want live status
sync, otherwise nothing further to do).

Also once, before the first spawn: `${CLAUDE_PLUGIN_ROOT}/bin/harness
env-check --run <run>` — probes every prerequisite the plan declared
(`env_requires`). Exit 0 means go. **A non-zero exit is a STOP, not a
warning**: surface the `missing` list with each entry's `hint` to the user,
wait for them to make it available, then re-run `env-check`. Never start a
service yourself and never spawn the developer anyway — a task whose
integration test can't run either burns an hour mid-develop or ships
unverified. No task declaring `env_requires` → nothing probed, exit 0.

## The dispatch loop

1. **Ask** `${CLAUDE_PLUGIN_ROOT}/bin/harness ready-tasks --run <run>` →
   `{ready, in_flight, blocked, terminal, conflicts}`. Every entry of the
   first four is `{id, repo}` (+ `status` on `in_flight`, `waiting_on` on
   `blocked`), so "is any sibling task in this repo still non-terminal?" —
   which recipe step 1 asks — is answered from this verb alone.
   The owned derivation: never re-derive readiness from `show` yourself — a
   dependency merely `in-review` is NOT satisfied, and a task already in
   flight must not be dispatched a second time.
2. **Dispatch every entry in `ready`** — all of them. Run recipe steps 1-2 per
   task (worktree, in-progress: quick CLI calls), then put their step-3
   developer spawns in ONE message, which is what runs them concurrently.
   **Except a `conflicts` pair:** two tasks in the same repo whose declared
   `files` overlap, where at least one is ready — the other is either ready
   too or already IN FLIGHT, and the ready one is named first. Hold that
   ready task: let the other's merge land, THEN cut its worktree and dispatch
   it. A worktree cut now cannot contain a sibling's not-yet-merged code, so
   both would edit the same file from the same starting point. Advisory, not
   enforced: `depends_on` is still the
   only ordering authority, and everything else in `ready` still goes out
   together. Across repos there is never a conflict. An EMPTY `conflicts` is
   the planner's word, not a verified fact — the manifests it is derived from
   are plan-declared and nothing checks them for completeness — so an overlap
   that surfaces mid-develop as a merge conflict is that gap, and the same
   stagger is the answer.
3. **Wait, don't poll.** A backgrounded spawn returns a launch STUB, not a
   reply: proceed on its completion notification only, never `stall` a live
   spawn, and never stop waiting on task B because task A reported — an
   un-awaited lane is a lane that never finishes. **Which lane is still in
   flight** is an owned answer, never a ledger read: `show`'s
   `outstanding_spawns` lists every open spawn as `{task, mode, agent_id,
   step, at, clearable, clearing_key}`. A lane listed there is running; a
   lane you expected there and don't see is the one to diagnose. Every lane
   here shares one `step` (the cursor cannot leave `develop` until the tasks
   are terminal), so `at` — not `step` — is what tells a wedged lane from a
   slow one; SKILL.md's Stalls triage owns that call.
4. **On each completion notification** advance THAT task through the recipe,
   then re-run `ready-tasks`: a task reaching `done` is what unblocks its
   dependents, and they dispatch the same moment under the same rules.
5. **Merges serialize** (`merge-task` takes the run lock): call it as each
   task reaches recipe step 6, never batched at the end — a queued merge
   holds the whole DAG's tail. A stalled lane pauses THAT lane only.
   A verb that reports a **lock wait** (`gave up … waiting for the run
   lock`) or a **MergePreconditionError naming this run's `ai/<run>` paths
   or feature branch** means a sibling lane is mid-flight, nothing more:
   wait for its completion notification, then re-run the IDENTICAL command.
   Never stash, never hand-commit, never `git` your way past it.

## Per task

1. **Worktree:** `${CLAUDE_PLUGIN_ROOT}/bin/harness worktree-add --repo <repo> --task-id <T>
   --base <feature-branch> --run <run>` — records `{path, root, branch}` in
   state; idempotent on resume. For a repo registered as a **subtree** of a
   shared checkout, `root` is the worktree and `path` the logical repo
   inside it (`<worktree>/<subtree>`): `path` is what the developer and
   every later `--repo <worktree>` argument take, never `root` (which exists
   so step 7 can remove the worktree at all). Root registrations are
   unchanged — the two are one directory. If creation fails twice, read what
   the failure actually says: it either names the direct-branch fallback
   **or REFUSES it, naming the shared physical checkout and any other repos
   registered into it** — a task branch cut in the main checkout switches
   every file there, registered or not. On the refusal there is no choice to
   offer: surface it verbatim, stop the lane, and let the user fix the repo
   state. Where it IS named, offer it to the user (never improvise it) **only
   while no sibling task in that repo is non-terminal** — check `ready-tasks`
   and match on each entry's own `repo`, across `ready`, `in_flight` and
   `blocked`.
   It parks a non-feature branch in the shared checkout, so every live
   sibling's `merge-task` precondition (HEAD must be this run's feature
   branch) then refuses — and step 5 tells the loop that refusal means "wait
   and re-run the identical command", which here never succeeds: a LIVELOCK.
   With siblings live there is no choice to offer either: stop this lane, let
   them land, retry.
2. `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-progress --run <run>`
3. **Spawn `developer`** with headers (`harness-mode: develop`,
   `harness-task: <T>`, `harness-run`, `harness-repo: <worktree-path>`,
   `harness-test-cmd`, `harness-plugin-root`): resolve test-cmd with `${CLAUDE_PLUGIN_ROOT}/bin/harness
   resolve-test-cmd --repo <the task's REGISTERED repo path> --run <run>` —
   never by reading `language.repos.<name>.test_cmd` by hand; the verb
   applies that repo's declared quarantine exclusions, so an agent-run suite
   skips the same known-failing specs the harness-run one does, and `--run`
   is what puts the exclusion on the run's flagged-events dashboard. Null
   `test_cmd` means unconfigured — ask the user, never improvise, exactly as
   with `resolve-coverage-cmd`. Language-config is per repo, not one global
   command) + the task's plan section. It follows `steps/develop-task.md` (TDD: verify-red, then
   impl, then a harness commit). `harness-task` must name a REGISTERED id —
   the guard blocks a typo rather than filing real work under an unreachable id.
4. **Completion:** `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-review --repo <worktree>
   --run <run>` (`--test-cmd <cmd>` optional — omitted, it auto-resolves from
   language-config for this task's registered repo) — runs verify-green +
   the red-proof check; a refusal means the TDD contract wasn't met (send
   the developer back; a locked-test change needs the flagged revision path).
5. **Spawn `reviewer`** (`harness-mode: review`, same headers inc.
   `harness-plugin-root` — the
   `harness-task: <T>` header is load-bearing here: a capture hook
   captures the reviewer's `verdict:` line into `reviews.ndjson` keyed by
   it, and step 7's `task --to done` REFUSES without a captured APPROVED
   for this task) on the task diff. `CHANGES_REQUESTED` →
   `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-progress --run <run>`
   (round-bounded; a refusal = escalate to the human) and re-spawn the
   developer with the findings. `APPROVED` → continue.
   **Verdict not captured** (a `verdict-uncaptured` event, or step 7
   refused with reviews.ndjson missing this task)? Re-spawn the reviewer
   FRESH — same headers, spawned per SKILL.md step 3 (and only once the
   previous one has reported; the guard refuses a second live spawn for the
   same task and mode). NEVER SendMessage/resume the finished one: continuation replies pass through no capture hook, so a restated
   verdict there can never register, however clean (field finding).
6. **Squash:** from the feature-branch checkout:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness merge-task --repo <repo> --task-id <T> --task-branch
   <worktree-branch> --summary "<task summary>" --run <run>`
7. `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to done --run <run>`
   (refused unless the hook captured this task's reviewer APPROVED — spawn
   the reviewer, don't restate its verdict) → `${CLAUDE_PLUGIN_ROOT}/bin/harness worktree-remove --repo
   <repo> --task-id <T> --run <run>` → `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <repo> --run <run>`.

`ready-tasks` reporting every task terminal → record the declared artifact
the ⟨approve-impl⟩ gate presents: `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name task-commits
--value "<T1>: <sha1>; <T2>: <sha2>; …" --run <run>` (the SHAs are in
`show`'s tasks; assembled ONCE, after the last task, never per task) →
`${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next> --run <run>`
(⟨approve-impl⟩ in full; harden in lean — its impl gate is deliberately
absent, the per-task hook-captured verdicts + pre-PR gate carry the
guarantees; quick-recheck in quick).
