# Step: pre-pr-fixes (developer shape, mode `fixup`; reached on gate rejection)

Task commits are already squashed — fixes need the fixup path (never new
loose commits, never history edits by hand):

1. Spawn `developer` with `harness-mode: fixup` + the gate's findings.
   It follows `steps/fixup-task.md`.
2. After its fixup commits land:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness merge-task --repo <repo> --autosquash --base <default-branch>
   --run <run>` — folds them into their task commits and re-derives the
   tracker SHAs.
3. Record the declared artifact: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   artifact --name fixup-commits --value "<findings addressed> @ <new HEAD
   sha>" --run <run>`.
4. Return via the declared edge: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to pre-pr --run <run>`
   (the pre-PR review re-runs on the corrected branch).
