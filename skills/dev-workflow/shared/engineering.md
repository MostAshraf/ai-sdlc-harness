# Shared baseline: engineering principles (cited by developer + reviewer)

- Match the surrounding code: its idioms, naming, error handling, comment
  density. Consistency beats personal preference.
- Do the simplest thing that works well: no speculative abstractions, no
  feature flags or compat shims where changing the code suffices, no error
  handling for scenarios that cannot happen. Validate at system boundaries.
- Optional fields on partial-update DTOs use nullable/wrapper types
  (review-policy `patch-dto-wrapper`) — a bare primitive cannot distinguish
  "sent default" from "omitted".
- Tests assert the full observable contract (review-policy
  `assertion-depth`), not just status codes.
- Every claim in a status block is evidence-grounded: cite the tool result
  that proves it. Failures are reported verbatim, never aspirationally.
