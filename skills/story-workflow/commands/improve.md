# /story-workflow improve

Single-pass story improvement — assess readiness internally, fill gaps
conversationally, and produce a refined story, all in one adaptive flow. This
is the recommended default: it replaces the separate analyze-then-refine dance.

## Steps

1. **Fetch** the work item per `shared/provider-io.md` (title, description,
   acceptance criteria, state, links). If it isn't found, stop.
2. **Load** `templates/story-template.md` (target format),
   `templates/readiness-report.md` (used only as an internal rubric — do *not*
   present a standalone report), and `shared/context.md` for domain language.
3. **Assess readiness internally** against the rubric, then classify into a tier:
   - **Tier 1 — Solid**: ≤1–2 yellow flags, no red. Draft immediately.
   - **Tier 2 — Some gaps**: 1–3 issues. Specific areas need clarification.
   - **Tier 3 — Rough**: 4+ issues, or a whole section missing (no ACs, no
     description). Needs substantial input.
4. **Fill gaps conversationally**, adapted to the tier:
   - **Tier 1**: skip questions, acknowledge what's good, draft.
   - **Tier 2**: ask 2–5 targeted questions — each references the specific gap
     and *proposes an answer to confirm or correct* (the user validates, not
     drafts). Present them all at once.
   - **Tier 3**: say so honestly, ask up to 5 questions, then (if critical gaps
     remain) one more round of up to 3, then draft.
   - **Session notes** (passed after the id) are gold: mine them before asking
     anything, and don't ask what they already answer. If none and the story is
     Tier 2/3, ask once whether any exist.
5. **Draft the whole story at once** in `templates/story-template.md`: Context,
   Description, Acceptance Criteria (happy path then error/edge), Out of Scope,
   Open Questions (`[PO]`/`[Tech]`/`[Team]`), and an empty Technical Notes
   (that's `groom`'s). For Tier 2/3, prepend a short "What was improved" list;
   skip it for Tier 1.
6. **Review and iterate** — present the full draft, adjust any section the user
   flags. If it's grown too large (>7 ACs, multiple capabilities), suggest a
   split. After minor edits, just confirm the change; don't re-dump the story.
7. **Post** on approval per `shared/provider-io.md` — one comment carrying the
   "What was improved" summary (if any) plus the full story. For
   `local-markdown`, offer the in-place overwrite of the source file (this
   command is one of the two that may rewrite a story in place).

## Notes

- **Adapt, don't interrogate.** If the story is good, say so and draft fast.
- **Suggest answers.** The user should be confirming, not drafting from scratch.
- **One comment, one artifact** — the whole output in a single comment, not one
  per section.
