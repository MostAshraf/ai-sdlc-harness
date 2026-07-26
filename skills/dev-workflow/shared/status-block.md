# Shared contract: the status block (every shape, every response)

End EVERY response with exactly this block — a capture hook checks for
it; a missing block triggers the stalled-agent procedure (reinvoke →
recovery → human), unless an engine-read reviewer verdict was still
captured — then it's recorded as `status-block-malformed`: flagged for
the human, but not a stall. Don't lean on that: it is a degraded path.

```
harness-status: SUCCESS | PARTIAL | FAILED
harness-task: <task-id or ->
verdict: <APPROVED | CHANGES_REQUESTED>
blocking-findings: <N>
outcome: <one line — what actually happened, evidence-grounded>
details: <optional: findings list / clarifying questions / blocker>
```

Rules:

- The block ENDS the reply — it is the LAST text you output, nothing
  after `details:`. Anything you'd append after it (a sign-off, a caveat,
  one more finding) belongs INSIDE `details:` instead: the capture hook
  reads the FINAL block, and prose trailing it is where verdicts get lost.
- `verdict` is the REVIEWER's line — APPROVED or CHANGES_REQUESTED, alone
  on its own line, in the block position shown (BEFORE the prose fields).
  Non-reviewer shapes omit the line entirely. NEVER fold it into another
  field's prose: `details: No findings. verdict: APPROVED` is
  uncapturable — the hook reads only a line-anchored verdict, fail-closed
  (three field re-reviews were paid for exactly that run-together shape;
  the verdict used to be defined as part of `details`, which taught it).
- `blocking-findings` is the REVIEWER's count of findings that must be
  fixed before approval (CRITICAL, and any WARNING you are actually
  blocking on) — `0` on an APPROVED verdict. Optional, but give it: it is
  the only machine-readable record of whether a review panel is converging,
  and the metrics report turns it into a per-round table the human reads at
  the gate. Non-reviewer shapes omit the line. It never changes the verdict
  — `verdict` alone decides that.
- `outcome` claims only what a tool result in THIS session proves — report
  failures faithfully ("tests fail with X"), never aspirationally.
- `PARTIAL` means you checkpointed (wip commit) and the work is resumable —
  say exactly where you stopped.
- Reviewer findings go in `details` as a numbered list:
  `[R1] <severity: CRITICAL|WARNING|SUGGESTION> <finding>`.
