# /story-workflow refine

Interactive, section-by-section restructuring of a story into the canonical
template through a collaborative conversation. Slower than `improve` on purpose.

> For most sessions `improve` is the better choice. Reach for `refine` when a
> story is complex or contentious and you want to work through it one section at
> a time, confirming each before moving on.

## Steps

1. **Fetch** the work item per `shared/provider-io.md` (title, description,
   acceptance criteria, state, links).
2. **Session notes**: if the user passed notes after the id, use them. If not,
   ask once: "Any notes from the refinement session — bullet points, rough
   notes, decisions? If not, I'll work from the work item." Notes capture PO
   clarifications and scope calls that never made it into the item.
3. **Load** `templates/story-template.md` (the target format) and, for
   terminology, `shared/context.md`.
4. **Work the template one section at a time**, proposing content and getting
   approval before moving on:
   - **Context** — draft the business "why"; ask if it's captured correctly.
   - **Description** — one `As a … I want … so that …`. If it spans multiple
     capabilities, flag a possible split before continuing.
   - **Acceptance Criteria** — draft Given/When/Then, happy path first then
     error/edge cases; present them together and ask what's missing.
   - **Out of Scope** — propose explicit exclusions from the notes and context;
     ask what else to exclude.
   - **Open Questions** — collect unresolved items, tag each `[PO]` / `[Tech]` /
     `[Team]`, ask if any remain.
   - **Technical Notes** — leave empty; it's populated by `groom`. If the user
     volunteers technical context, note it but say the full analysis is `groom`.
5. **Assemble** the complete story and present it whole for a final review.
6. **Post** on approval per `shared/provider-io.md` — a comment on remote
   providers; for `local-markdown`, offer the in-place overwrite of the source
   file (this command is one of the two that may rewrite a story in place).

## Notes

- The interactivity is the value — don't rush sections; give the user room to
  correct. Be transparent about inferences: "I'm inferring X from the
  description — right?"
- If the story looks too large mid-refinement (many ACs, broad scope), suggest
  splitting rather than pushing a bloated story through.
