# Technical Notes Template

The output format for the Technical Notes produced by `groom` — a per-repo
breakdown of technical impact.

```markdown
## Technical Notes

### [repo-name]
- **Affected Components:** [specific files, classes, services, or modules]
- **What Changes:** [brief description of the changes needed in this repo]
- **Database/Migration:** [schema changes / migrations, additive vs breaking; "None" if N/A]
- **API Contract:** [new/changed endpoints, request/response models; "None" if N/A]
- **Testing Strategy:** [test types + specific areas to cover]
- **Risk:** [Low / Medium / High — with a brief why]

### [another-repo-name]
- ...

### Cross-Repo Considerations
[Only when the story spans multiple repos: deployment order, shared contracts,
feature flags needed for safe independent rollout.]
```

## Guidelines

**Affected Components** — name the actual classes/services/config files.
`AuthenticationService.cs`, `SamlConfigurationProvider.cs` — not "the auth
module."

**What Changes** — 1–3 sentences on the nature of the change, not the
implementation steps. Developers work out the how; they need the what.

**Database/Migration** — note additive (safe to deploy independently) vs
breaking (needs coordinated deploy), and any data-migration concern.

**API Contract** — new endpoint, modified contract (breaking vs non-breaking),
or deprecation.

**Testing Strategy** — specific, not "add unit tests." e.g. "Unit-test the new
validation; integration-test the full feature flow against the test env."

**Risk** — Low = straightforward, well-understood. Medium = some complexity or
uncertainty. High = significant regression/data/cross-system risk. Always
explain the rating.

**Cross-Repo Considerations** — deployment order, shared models/contracts to
align, feature flags for independent deploys.
