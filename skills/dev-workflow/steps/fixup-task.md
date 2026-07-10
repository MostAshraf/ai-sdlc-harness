# Instruction: post-squash fixes (developer shape, mode `fixup`)

You are fixing findings against ALREADY-SQUASHED task commits:

1. For each finding, make the fix in the feature-branch checkout.
2. Commit with `${CLAUDE_PLUGIN_ROOT}/bin/harness commit --repo <repo> --fixup-of <task-id>` — the
   ${CLAUDE_PLUGIN_ROOT}/bin/harness locates the task's integration commit and formats the `fixup!`
   subject; autosquash folds it later.
3. TDD-locked test files remain locked: a test change still requires the
   flagged revision path (`verify-red --revise --reason`).
4. Run the full suite before reporting. Status block lists finding → fix
   mapping, evidence-grounded.
