# /story-workflow analyze

Pre-refinement readiness check. Evaluates a work item against quality criteria
and produces a readiness report with actionable flags. Standalone and
non-destructive — it never rewrites the story.

> For most sessions `improve` is the better choice — it folds readiness analysis
> into refinement in one adaptive pass. Reach for `analyze` when you want a
> standalone report to share with the PO *before* touching the story.

## Steps

1. **Fetch** the work item per `shared/provider-io.md`. Extract title, type,
   state, description, and acceptance criteria. If it isn't found, stop.
2. **Domain context (optional)**: skim `shared/context.md` only if you need the
   repo landscape to judge whether the story's scope is realistic.
3. **Evaluate** the story against every dimension in
   `templates/readiness-report.md`. For each, assign a 🔴/🟡/🟢 flag with a
   *specific* explanation. Rules:
   - **Be specific, not generic.** Not "ACs could be improved" but "AC #2 says
     'handles errors gracefully' — untestable; suggest 'Given an invalid token,
     When login is attempted, Then a 401 with code `AUTH_INVALID_TOKEN` is
     returned'."
   - **Suggest, don't only critique.** Every 🔴/🟡 carries a concrete fix —
     draft the replacement text where you can.
   - **Constructive tone.** The PO and team will read this.
4. **Write the report** in the `templates/readiness-report.md` format: flags
   with explanations, suggested improvements (draft Given/When/Then ACs where
   ACs are missing or vague), and a 2–3 sentence summary assessment.
5. **Present** the full report in the conversation, then ask whether to post it
   as a comment on the item. On yes, post it per `shared/provider-io.md`. For
   `local-markdown` (or any provider), the report is a **comment** — never
   overwrite the story with it; if the user wants it persisted separately,
   offer a sibling file (e.g. `<id>-readiness.md`) via the `Write` tool.

## Notes

- Never modify the item's Description or AC fields — comments only.
- Don't force flags. A report that says "this story is ready" is a good result.
- Flag missing pieces as **questions**, not assertions about what should exist.
