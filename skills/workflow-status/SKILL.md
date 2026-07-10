---
name: workflow-status
description: >
  Read-only dashboard of ai-sdlc-harness runs in this workspace. USER-ENTRY —
  invoke only when the user explicitly runs /workflow-status; never
  autonomously, never from a subagent (guard-enforced).
---

# workflow-status (read-only)

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness status` — every run: cursor, mode, work item, task statuses,
   decided gates, flagged-event count. An `aborted` field marks a run ended
   by the abort verb (terminal); an `error` field marks a run whose state
   failed integrity verification (its `remediation` names the reseal
   command) — the rest of the dashboard still renders.
2. Render it as a compact table for the user; for a run they ask about,
   drill in with `${CLAUDE_PLUGIN_ROOT}/bin/harness show --run <run>` plus the ledgers
   (`events.ndjson` — the "what happened" trail; `tokens.ndjson` — spend)
   and compose the per-task timeline: status + review rounds + stalls +
   flagged events + gate evidence.
3. Offer `${CLAUDE_PLUGIN_ROOT}/bin/harness metrics --run <run>` — regenerates
   `reports/metrics.md`, the human-readable table view of the same ledgers
   (timings, tasks, verdicts, aggregated tokens, flagged events). Works at
   any live step, not just the terminal metrics step; it's a deterministic
   projection, so regenerating is always safe.
4. Otherwise STRICTLY read-only: no state changes, no cursor moves, no
   ledger writes — the report above is the one derived, regenerable output;
   everything else only reads what the authority already records.
