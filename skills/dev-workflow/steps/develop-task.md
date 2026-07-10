# Instruction: develop one task (developer shape, mode `develop`)

You own ONE task, in YOUR worktree (`harness-repo`), test-first.

**The rule is per-task data, not the mode's name:** if your task declares
`test_intents` (check it in `${CLAUDE_PLUGIN_ROOT}/bin/harness show`'s task
list), the completion transition mechanically requires a red-proof — there
is no path to done that skips it. If it declares none, the red-proof
machinery is inactive and `task --to in-review` skips both verify-red and
verify-green entirely — step 3 below does not apply; write tests and
implementation together, then go straight to step 4. In practice:
plan-registered tasks carry intents unless the plan opted out
(`test_intents: []`, docs/chore); quick mode's fetch-seeded task declares
none — that's why quick runs are relaxed by design, not an oversight.

1. **Read** the task's plan section (description, edge cases, test-intents,
   `[API:]` annotations — verify those signatures before writing call sites,
   pattern hints — start from them). Quick mode has no plan step, so there
   are no declared test-intents to read — go straight to the work item's
   own description.
2. **Write the failing test(s)** implementing the declared test-intents.
   No implementation yet — and this ordering is hook-ENFORCED, not advisory:
   until your red-proof is sealed (step 3), writes to non-test paths in your
   worktree are refused (test paths, test fixtures, and build manifests for
   test dependencies stay writable). **Task declares NO test-intents**
   (docs/chore — the plan-approved opt-out)? Skip steps 2–3 entirely: no
   red-proof is needed or possible (verify-red refuses, the completion
   guard exempts you) — implement, commit, report; review still applies.
   (Quick mode: skip straight to writing test + implementation together —
   there's no red-proof to prove first.)
3. **Prove red (only when your task declares test-intents):**
   `${CLAUDE_PLUGIN_ROOT}/bin/harness verify-red --repo <repo> --task <T> --run <run>`
   (`--test-cmd <cmd>` from your `harness-test-cmd` header; omit it and the
   command auto-resolves from language-config for this task's registered
   repo) — must succeed before any impl. If it reports "not red", your impl
   already exists or the test is vacuous — fix the test.
   From here your test files are LOCKED (blob-SHA): changing one fails the
   completion check. Genuinely wrong test? Use the flagged path:
   `verify-red --revise --reason "<why>"` — reviewer-visible, never silent.
   The output's `missing_intents` (declared names not literally found in
   your test files) is mechanical, not a suggestion — write ALL declared
   tests before this call so it comes back empty; a non-empty result after
   the fact means a declared test was skipped, and adding it now means the
   same flagged `--revise` path as any other post-seal test change, not a
   silent add.
4. **Implement** until the test passes. Checkpoint with
   `${CLAUDE_PLUGIN_ROOT}/bin/harness commit --repo <repo> --task-id <T> --summary "<what>"`
   (working commits; squashed later — never raw `git commit`).
5. **Full-suite check:** run `<test-cmd>`; fix regressions you introduced.
6. Report via the status block: `harness-status: SUCCESS` with
   `verify-red`'s `missing_intents` value quoted verbatim in `details`
   (empty if you closed every gap in step 3), or `PARTIAL` after a
   `--commit-class wip` checkpoint near your turn ceiling, or `FAILED` with
   the blocker.
