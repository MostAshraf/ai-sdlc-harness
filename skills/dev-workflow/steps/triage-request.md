# Instruction: ad-hoc request triage (reviewer shape, mode `request-triage`)

A human made a request mid-run. Classify it against the APPROVED plan:

- `IN_SCOPE_BUG` — defect in work this run already did → route to the
  develop/fixup loop.
- `IN_SCOPE_AC_MISS` — an approved acceptance criterion isn't met → same.
- `OUT_OF_SCOPE` — new scope → propose a follow-up work item; NEVER
  silently merge it into this run.
- `PLAN_CONFLICT` — contradicts an approved decision → surface both sides
  to the human; never resolve unilaterally.
- `DUPLICATE` / `INVALID` — with the evidence.

Verdict + reasoning in the status block; the orchestrator presents it to the
human — triage never mutates anything.
