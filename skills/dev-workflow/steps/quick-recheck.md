# Step: quick-recheck (orchestrator-owned; quick mode only)

The ex-ante classifier saw only work-item text; this step checks the REAL
diff against the declared disqualify patterns:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness quick-recheck --repo <repo> --base <default-branch> --run <run>
```

- `clean` → advance to `pre-pr`.
- `dirty` → the escalation edge is MANDATORY: `pre-pr` is now illegal;
  advance to `security` (`${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to security`) — the run switches
  to full mode and continues from there. Tell the user why (the hits are in
  the events ledger).
