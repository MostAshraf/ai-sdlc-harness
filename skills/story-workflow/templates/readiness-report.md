# Readiness Report Template

The output format for `analyze`, and the internal rubric `improve` scores
against. A qualitative assessment of story readiness with actionable flags.

```markdown
## Story Readiness Report
**Work Item:** #[ID] — [Title]
**Assessed:** [date]

### Flags

🔴 **[Flag Name]**
[What's missing or problematic, with a specific suggestion for improvement.]

🟡 **[Flag Name]**
[A concern that isn't blocking but should be addressed.]

🟢 **[Flag Name]**
[Something done well — reinforces good practice.]

### Suggested Improvements

[Concrete, story-specific suggestions — draft acceptance criteria, proposed
scope boundaries, or questions to raise in refinement. Not generic advice.]

### Summary

[2–3 sentence overall assessment. Ready for a refinement discussion, or does it
need pre-work from the PO first?]
```

## Flag catalog

The readiness dimensions to evaluate. Not every flag applies to every story —
use judgment.

**Context & Motivation** — 🔴 Missing Context (no "why") · 🟡 Vague Context
(present but not tied to a business need) · 🟢 Clear Context.

**Description Quality** — 🔴 No User-Story Format (no persona/capability/outcome)
· 🟡 Ambiguous Persona ("as a user" is too generic) · 🟢 Well-Formed.

**Acceptance Criteria** — 🔴 Missing ACs · 🔴 Untestable ACs ("should work
well") · 🟡 Incomplete ACs (obvious error/edge cases missing) · 🟡 Too Many ACs
(>7 suggests a split) · 🟢 Solid ACs.

**Scope** — 🔴 Unbounded Scope · 🟡 Implicit Assumptions · 🟡 No Out-of-Scope ·
🟢 Well-Bounded.

**Dependencies & Risks** — 🟡 Implicit Dependencies · 🟡 Cross-Team Dependency ·
🟢 Self-Contained.

## Tone

Constructive and specific. Every 🔴/🟡 carries a concrete fix. 🟢 flags matter
too — they reinforce what the PO and team are doing well.
