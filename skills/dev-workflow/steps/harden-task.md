# Instruction: hardening tests (developer shape, mode `harden`)

You are topping up coverage on code that already exists — tests must be
meaningful, not gaming:

- **Assertion depth (review-policy `assertion-depth`):** assert the full
  observable contract — response body shape, error envelope, side effects.
  Status-code-only or not-throws-only assertions are incomplete.
- Target the diff-coverage gaps you were given, not incidental old code.
- Never modify implementation code in this mode — tests only.
- Commit via `${CLAUDE_PLUGIN_ROOT}/bin/harness commit --commit-class working`; report via the status
  block with the coverage delta you achieved.
