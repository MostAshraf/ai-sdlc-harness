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

Per task:

1. **Worktree:** `${CLAUDE_PLUGIN_ROOT}/bin/harness worktree-add --repo <repo> --task-id <T>
   --base <feature-branch> --run <run>` — records `{path, branch}` in state;
   idempotent on resume. If it fails twice it names the direct-branch
   fallback — offer that choice to the user, never improvise.
2. `${CLAUDE_PLUGIN_ROOT}/bin/harness task --id <T> --to in-progress --run <run>`
3. **Spawn `developer`** with headers (`harness-mode: develop`,
   `harness-task: <T>`, `harness-run`, `harness-repo: <worktree-path>`,
   `harness-test-cmd`: the task's registered repo's own `language.repos.<repo-name>.test_cmd`
   — language-config is per repo, not one global command; look up the name
   this task's repo was registered under in `repos.yaml`) + the task's plan
   section. It follows `steps/develop-task.md` (TDD: verify-red, then
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
(⟨approve-impl⟩ in full; quick-recheck in quick).
