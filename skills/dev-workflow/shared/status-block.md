# Shared contract: the status block (every shape, every response)

End EVERY response with exactly this block — a capture hook checks for
it; a missing block triggers the stalled-agent procedure (reinvoke →
recovery → human).

```
harness-status: SUCCESS | PARTIAL | FAILED
harness-task: <task-id or ->
verdict: <APPROVED | CHANGES_REQUESTED>
outcome: <one line — what actually happened, evidence-grounded>
details: <optional: findings list / clarifying questions / blocker>
```

Rules:

- `verdict` is the REVIEWER's line — APPROVED or CHANGES_REQUESTED, alone
  on its own line, in the block position shown (BEFORE the prose fields).
  Non-reviewer shapes omit the line entirely. NEVER fold it into another
  field's prose: `details: No findings. verdict: APPROVED` is
  uncapturable — the hook reads only a line-anchored verdict, fail-closed
  (three field re-reviews were paid for exactly that run-together shape;
  the verdict used to be defined as part of `details`, which taught it).
- `outcome` claims only what a tool result in THIS session proves — report
  failures faithfully ("tests fail with X"), never aspirationally.
- `PARTIAL` means you checkpointed (wip commit) and the work is resumable —
  say exactly where you stopped.
- Reviewer findings go in `details` as a numbered list:
  `[R1] <severity: CRITICAL|WARNING|SUGGESTION> <finding>`.
