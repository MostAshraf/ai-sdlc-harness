# Instruction: per-task review (reviewer shape, mode `review`)

Review ONE task's diff (its worktree branch vs the feature branch), inside
the develop loop:

- Re-run the task's tests yourself (`harness-test-cmd`) — independent
  verification, never trust the developer's claim. Long output? Capture it
  under /tmp (`… 2>&1 | tee /tmp/review.log`) — /tmp and /dev/null are
  your only legal write targets; any other shell write is blocked. Spell
  the path LITERALLY: a variable-held target (`$SCRATCH/x`, `$(mktemp -d)`)
  can't be verified by the guard and blocks even when it points at /tmp.
- Check the diff against: the task's plan section (scope drift?), the
  config review-policy rules, and the engineering baseline
  (`shared/engineering.md`). For test-intents: run
  `${CLAUDE_PLUGIN_ROOT}/bin/harness show-redproof --task <T> --run <run>`
  (chain-verified; a raw `.redproof/` read skips integrity verification
  and is guard-BLOCKED — and type that full invocation exactly: the bare
  `harness` spelling is neither on PATH nor allow-listed, so it dies as a
  permission denial) and check its `missing_intents` field
  (already mechanically checked, not yours to recompute) — any entry is a
  finding. A present name isn't automatically a pass, though: judge whether
  it genuinely tests its declared intent (design.md:392 — presence is
  mechanical, semantic match stays yours).
- Findings numbered `[R<n>]` with severity CRITICAL | WARNING | SUGGESTION;
  `verdict: APPROVED` only when nothing CRITICAL/WARNING remains. The
  verdict is its OWN block line, before `outcome`/`details` (the template's
  third line — never inside another field's prose): one run into the end
  of a sentence is deliberately not captured (fail-closed) and costs a
  full re-review by a fresh reviewer.
- Full findings in the status block `details` — the orchestrator routes
  them; rejection costs a bounded review round (round N+ escalates to the
  human), so make every finding actionable.
