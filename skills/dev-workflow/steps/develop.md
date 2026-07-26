# Step: develop (orchestrator loop — spawns developer + reviewer per task)

Lane policy (M5 charter): tasks run **sequentially within a repo, in
parallel across repos** (one developer per lane, spawned together). A lane's
stall pauses that lane only (fail-soft); gates and pre-pr require ALL lanes
complete (fail-closed sync points — mechanically enforced: the cursor cannot
leave `develop` while any task is still pending/in-progress/in-review).

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

Per task:

1. **Worktree:** `${CLAUDE_PLUGIN_ROOT}/bin/harness worktree-add --repo <repo> --task-id <T>
   --base <feature-branch> --run <run>` — records `{path, branch}` in state;
   idempotent on resume. If it fails twice it names the direct-branch
   fallback — offer that choice to the user, never improvise.
2. `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-progress --run <run>`
3. **Spawn `developer`** with headers (`harness-mode: develop`,
   `harness-task: <T>`, `harness-run`, `harness-repo: <worktree-path>`,
   `harness-test-cmd`: resolve it with `${CLAUDE_PLUGIN_ROOT}/bin/harness
   resolve-test-cmd --repo <the task's REGISTERED repo path> --run <run>` —
   never by reading `language.repos.<name>.test_cmd` by hand; the verb
   applies that repo's declared quarantine exclusions, so an agent-run suite
   skips the same known-failing specs the harness-run one does, and `--run`
   is what puts the exclusion on the run's flagged-events dashboard. Null
   `test_cmd` means unconfigured — ask the user, never improvise, exactly as
   with `resolve-coverage-cmd`. Language-config is per repo, not one global
   command) + the task's plan section. It follows `steps/develop-task.md` (TDD: verify-red, then
   impl, then a harness commit).
4. **Completion:** `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-review --repo <worktree>
   --run <run>` (`--test-cmd <cmd>` optional — omitted, it auto-resolves from
   language-config for this task's registered repo) — runs verify-green +
   the red-proof check; a refusal means the TDD contract wasn't met (send
   the developer back; a locked-test change needs the flagged revision path).
5. **Spawn `reviewer`** (`harness-mode: review`, same headers — the
   `harness-task: <T>` header is load-bearing here: a capture hook
   captures the reviewer's `verdict:` line into `reviews.ndjson` keyed by
   it, and step 7's `task --to done` REFUSES without a captured APPROVED
   for this task) on the task diff. `CHANGES_REQUESTED` →
   `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-progress --run <run>`
   (round-bounded; a refusal = escalate to the human) and re-spawn the
   developer with the findings. `APPROVED` → continue.
   **Verdict not captured** (a `verdict-uncaptured` event, or step 7
   refused with reviews.ndjson missing this task)? Re-spawn the reviewer
   FRESH — foreground, same headers. NEVER SendMessage/resume the finished
   one: continuation replies pass through no capture hook, so a restated
   verdict there can never register, however clean (field finding).
6. **Squash:** from the feature-branch checkout:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness merge-task --repo <repo> --task-id <T> --task-branch
   <worktree-branch> --summary "<task summary>" --run <run>`
7. `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to done --run <run>`
   (refused unless the hook captured this task's reviewer APPROVED — spawn
   the reviewer, don't restate its verdict) → `${CLAUDE_PLUGIN_ROOT}/bin/harness worktree-remove --repo
   <repo> --task-id <T> --run <run>` → `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <repo> --run <run>`.

All tasks done → record the declared artifact the ⟨approve-impl⟩ gate
presents: `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name task-commits
--value "<T1>: <sha1>; <T2>: <sha2>; …" --run <run>` (the SHAs are in
`show`'s tasks) → `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next> --run <run>`
(⟨approve-impl⟩ in full; harden in lean — its impl gate is deliberately
absent, the per-task hook-captured verdicts + pre-PR gate carry the
guarantees; quick-recheck in quick).
