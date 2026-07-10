# Instruction: PR comment analysis (reviewer shape, mode `analyze-comments`)

For each PR comment, classify with evidence from the actual code:

- `VALID` — the comment is right; state the fix it implies and its cost.
- `INVALID` — the comment misunderstands; state the disproof (file:line).
- `PARTIAL` — right concern, wrong specifics; state which half holds.

Never soften a VALID into PARTIAL to avoid work; never argue an INVALID
without a concrete disproof. Numbered list, one verdict per comment, full
analysis in the status block `details`.
