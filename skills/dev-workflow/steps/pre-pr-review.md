# Instruction: holistic pre-PR review (reviewer shape, mode `pre-pr`)

Review the ENTIRE feature branch (all squashed task commits) as one change:

- Re-run build + full test suite yourself — never trust reported results.
- Check each review-policy rule from config (assertion-depth,
  patch-dto-wrapper, team rules) and cite violations by file:line.
- Check plan-vs-implementation drift: does what shipped match what was
  approved? Are the declared test-intents all realized?
- Hunt the TDD residuals: semantically-empty tests (red for the wrong
  reason), impl overfit to the test, weakened shared fixtures.
- Findings numbered `[R<n>]` with severity CRITICAL | WARNING | SUGGESTION.
- Deliver the full report text in your status block `details` (the
  orchestrator persists it — you cannot write files).
