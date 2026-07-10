# Story Template

The canonical format for user stories. All four commands produce output that
follows this structure.

```markdown
## Context
[Why does this story exist? What business problem or user need does it address?
Background that helps developers understand the motivation.]

## Description
As a [persona], I want [capability], so that [outcome].

## Acceptance Criteria
- [ ] Given [precondition], When [action], Then [expected result]
- [ ] Given [precondition], When [action], Then [expected result]
- [ ] ...

## Out of Scope
[Explicitly what this story does NOT cover. Prevents scope creep and sets clear
boundaries for the implementation.]

## Open Questions
[Anything unresolved, each prefixed with who answers it. Cleared during
refinement or grooming — remove items as they get answered.]

## Technical Notes
[Added during or after grooming. Per-repo breakdown of affected components,
migration concerns, testing strategy, and risk flags. See technical-notes.md.]
```

## Section guidelines

**Context** — 2–4 sentences on the "why," not the "what." A developer reading
only this should understand the business motivation. No implementation detail.

**Description** — one sentence in user-story format. Needing multiple `As a…`
statements usually means the story is too large and should be split.

**Acceptance Criteria** — each AC independently testable, in Given/When/Then.
Aim for 3–7. Fewer than 3 usually means missing edge cases; more than 7 usually
means the story is too large.

**Out of Scope** — 1–3 items someone might reasonably assume are included but
aren't. If nothing is ambiguous, write "No explicit exclusions identified."

**Open Questions** — prefix each with `[PO]`, `[Tech]`, or `[Team]`. Example:
`[PO] Should existing users be migrated automatically or opt-in?`

**Technical Notes** — only populated during grooming; left empty or absent
before. See `technical-notes.md` for the per-repo format.
