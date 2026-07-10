# Step: plan (planner shape)

0. **Repo-map freshness — this step is its SINGLE owner.** The repo-map is
   first generated at `/init-workspace`; nothing between there and here
   touches it (intake only reads it), so this is the one place in the
   pipeline that confirms it's current. Its status is filesystem-only
   (`.claude/context/repo-map/<name>/.meta.json`) — not in `state.yaml`, so
   the `show` verb won't surface it; check it explicitly, per registered
   repo:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness repo-map-check --repo-name <name>
   --repo <path>`.
   - `fresh` → nothing to do (init already generated + stamped it; do NOT
     re-stamp).
   - `missing`/`stale` → regenerate (spawn `planner` with
     `harness-mode: repo-map`, a declared out-of-run-legal pair) and stamp
     via `${CLAUDE_PLUGIN_ROOT}/bin/harness repo-map-stamp` after it
     returns — a plan grounded in a stale map cites patterns that no longer
     exist.
0b. **Re-entering plan** (gate rejection / approved mid-run amendment)?
   Snapshot the current plan first — copy `<run>/plan.md` to
   `<run>/plan-r<n>.md` — so the previously-approved text stays
   recoverable. Never rewrite it silently.

Spawn `planner` with headers (`harness-mode: plan`, `harness-run`,
`harness-repo`) to produce `<run>/plan.md`. It follows `steps/plan-task.md`
(the content contract). The plan's REQUIRED content (coverage decisions
B1/B2/B7/B8 — the gate presents all of it):

- Tasks keyed by id (`T1`, `T2`…), each with: description, repo, edge-case
  enumeration, risk tier, dependencies.
- **Per-task test-intents** (named tests + one-line intent each) — approved
  at the plan gate; carry the NAMES (not the intent prose) into
  `plan-register`'s `test_intents` field per task — `verify-red` mechanically
  checks each declared name is written, no re-declaration needed later.
- **Approach options, two altitudes**: a top-level `## Solution Approaches`
  section (2–3 whole-solution options + recommendation) only when a genuine
  architectural fork exists, distinct from per-task options — both approved
  at this one gate. Skipped → one line saying why, never silent.
- `[API: <lib> v<X>]` annotations where a task prescribes a library API.
- ≤2 existing-test-file **pattern hints** per task (bounded discovery).
- **Four diagrams** — class/type, runtime flowchart, sequence, task
  dependency graph — per `shared/diagram-styling.md` (full spec in
  `steps/plan-task.md`).
- A **self-adversarial pass**: ambiguities, risks, unchecked assumptions.

When the planner's status block reports the plan ready:

1. Register the tasks it declared (replaces the single fetch-seeded task):
   `${CLAUDE_PLUGIN_ROOT}/bin/harness plan-register --run <run> --tasks-json
   '[{"id":"T1","repo":"<path>","risk":"low","test_intents":["test_name_one",
   "test_name_two"]}, …]'` — `repo` must be the exact path string from this
   workspace's `repos.yaml` (i.e. `config["repos"]`'s VALUE, e.g.
   `/abs/path/to/backend`), never the short registered NAME (`backend`) —
   a name instead of a path resolves to no registered repo and fails
   silently until a later step (`verify-red`/task completion) reports a
   confusing "no test command" error instead of a clear one here.
   `risk` is free-form (defaults to `low` if omitted) — no enum is
   enforced, but use `low`/`medium`/`high` for consistency with what the
   plan itself declares. (`test_intents` is the plan's declared test
   NAMES for that task, empty if none) — declare cross-repo contracts too if
   the plan named any: `--contracts-json '[{"id":"C1","type":"http",
   "producer":"a","consumers":["b"],"signature":["POST /v2/items",
   "field: item_id"]}]'` (`type` is `http | service-bus | dto`, descriptive
   only; `signature` is one string or a list of fragments, and **each must be
   a grep-able code token/signature that appears verbatim in source** —
   `archived`, `filter_notes(notes, tag)`, `POST /v2/items` — NOT an English
   description: reconcile-contracts matches by literal source search, so a
   prose fragment is rejected at plan-register and would false-report drift.
   All fragments must be present; the flat legacy `"repos":["a","b"]` form
   still works in place of `producer`/`consumers`. Legal only at cursor `plan`.
2. Check the diagrams: `${CLAUDE_PLUGIN_ROOT}/bin/harness validate-mermaid
   --file <run>/plan.md`. A non-zero exit names structural rule violations
   (`failures` in the JSON) — send the planner back to fix them, never
   advance past a failure. Purely structural (well-formed Mermaid that
   exists); it doesn't check the four diagrams are actually present — that
   stays plan-task.md's content-contract job.
3. Record the declared artifact: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   artifact --name plan --value plan.md --run <run>` (the gate presents it
   by this name).
4. Advance to the gate: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to approve-plan --run <run>`.
