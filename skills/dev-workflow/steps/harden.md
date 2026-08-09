# Step: harden (developer shape, mode `harden`)

Coverage top-up after develop's all-tasks-terminal sync point (crossed
via ⟨approve-impl⟩ in full mode; directly in lean, whose impl gate is
deliberately absent) — tests here are green-from-birth (they cover code
that already exists), so NO red-proof machinery applies.

1. Resolve the coverage command: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   resolve-coverage-cmd --repo <repo> --run <run>` (`--run` is what puts any
   quarantine exclusion on the run's flagged-events dashboard — without it
   the exclusions apply invisibly) (per repo, `language.repos.<repo-
   name>.coverage_cmd` — `discover` proposes one at `/init-workspace` time
   where repo evidence supports it: python/go conventions, a node
   `coverage` script or jest/vitest+provider, jacoco in a java pom). Null
   means unconfigured — ask the user, never improvise a command. Their
   answer is either a command (write it to
   `language.repos.<name>.coverage_cmd` the `/workspace-config` way, then
   re-resolve) or an explicit skip — record that with
   `${CLAUDE_PLUGIN_ROOT}/bin/harness log-event --json '{"kind":
   "coverage-skipped", "repo": "<name>", "reason": "<their words>"}'` and
   harden that repo from the tasks' own test gaps instead. The resolved
   command already carries any `language.repos.<name>.quarantine` exclusions
   — if a pre-existing unrelated failure keeps aborting the run, quarantine
   it in config (with a reason + date) rather than hand-narrowing the
   command, so the next run inherits the knowledge. Run the resolved command to find diff-coverage gaps against
   the tasks' touched files.
2. Spawn `developer` with `harness-mode: harden` (+ run/repo/test-cmd/plugin-root
   headers — test-cmd is per repo, resolved with
   `${CLAUDE_PLUGIN_ROOT}/bin/harness resolve-test-cmd --repo <repo> --run
   <run>`, same as `develop`) and the gap list. It follows `steps/harden-task.md`.
3. Spawn `reviewer` (`harness-mode: review`) on the new tests.
4. The DEVELOPER already committed its tests (harden-task.md's own
   commit step — do not commit again from here; a second commit in
   the same repo either fails on "nothing to commit" or sweeps unrelated
   files via `git add -A`). Produce `<run>/reports/coverage.md`
   summarizing before/after coverage and record the declared artifact:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name coverage-report
   --value reports/coverage.md --run <run>`.
5. Advance: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to security --run <run>`.
