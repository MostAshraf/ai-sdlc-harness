# Step: intake (planner shape)

Spawn the planner to turn the normalized work item into a requirements
summary. It reads `<run>/work-item.json` — it has no provider access.

Spawn `planner` with prompt headers:

```
harness-mode: intake
harness-run: <run>
harness-repo: <primary-repo-path>
```

…plus the ask: read `work-item.json` (+ the repo-map at
`.claude/context/repo-map/<repo-name>/` if present — EVERY registered
repo's `index.md` first, then only the area files the work item touches),
produce `<run>/requirements.md` — restated requirements, acceptance
criteria, ambiguities/clarifying questions for the human, and a Target
Repos proposal. Two shape requirements downstream steps key on:

- **Acceptance criteria are NUMBERED** (`AC1`, `AC2`, …) — the plan's
  traceability table and plan-review's coverage check reference these ids;
  an unnumbered criterion is invisible to both.
- **`## Target Repos`**: which registered repos this story touches, each
  with a one-line evidence-based reason grounded in the repo-map indexes
  (or code reading if no map) — and name any registered repo it
  deliberately does NOT touch when a human might expect it to.

The repo-map is generated at `/init-workspace` and its freshness is
(re)checked and stamped at **plan step 0** (the single owner). Here, just
READ what's present — do NOT generate it, run `repo-map-check`, or stamp
`.meta.json`; a missing or stale map is plan's job to refresh, not
intake's. (If it's absent, intake still proceeds on `work-item.json`
alone.) Don't investigate its status — plan handles it.

If the status block reports clarifying questions, surface them to the user
and relay answers (append to `requirements.md` via a re-spawn if substantial).
When surfacing them, **separate the two kinds** rather than presenting one
undifferentiated list:

- **confirm-a-default** — a question with a safe, stated default (you can
  proceed if the human doesn't object). Batch these together and present the
  default you'll take; a non-answer resolves cleanly.
- **resolve-a-real-fork** — a genuine either/or with no grounded default
  (both branches are live). Present each on its own and wait for an explicit
  pick; never bundle one of these behind a default, and never imply a fork is
  settled when it isn't.

Mixing the two produces partial-answer states (the human ratifies the easy
defaults and the real fork silently rides along unanswered).

Note the run's seeded task list (`T1`) is a **provisional placeholder** —
fetch's positional default (`repos[0]`), flagged `provisional: true` in
state, not a scope decision. Do not present it to the human as one; the real
task list is set at plan-register and replaces it wholesale.

Record the declared artifact once requirements.md is final:
`${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name requirements-summary
--value requirements.md --run <run>`.

**Confirm the target repos, then register the scope.** Present the Target
Repos proposal to the user with its per-repo evidence — this is a
confirm-a-default when the proposal is well-grounded (state the set you'll
take), a real fork if the map gave genuinely ambiguous signals. Once
confirmed, record it:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness scope-register --run <run> \
  --repos-json '["/abs/path/to/backend", "/abs/path/to/frontend"]'
```

Entries are the exact registered repo PATH strings (`repos.yaml` VALUES,
same rule as plan-register's `repo` field), never the short names. This is
mechanical scope: `plan-register` refuses any task whose repo is outside
it, and the plan step freshness-checks only these repos. Skipping it
doesn't defer the question — plan-register fails closed without a scope.

Then advance: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to plan --run <run>`.
