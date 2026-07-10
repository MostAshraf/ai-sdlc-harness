# Instruction: produce the plan (planner shape, mode `plan`)

Produce `<run>/plan.md`: the human-facing design for this story. Both
altitudes of approach decision below are approved at the single
`approve-plan` gate (design.md:495 — one gate, two altitudes).

## 1. Decomposition

Tasks keyed by id (`T1`, `T2`…), each with: description, repo, edge-case
enumeration, risk tier, dependencies. One task = one reviewable unit.

**`depends_on` is HARD technical blockers ONLY — it is mechanically
enforced and gates cross-repo parallelism.** A task can't start until every
task it `depends_on` is done (the `dependencies-done` guard refuses), and
you can't loosen the graph past the plan gate (registration is plan-step-
only) — so an over-declared edge permanently *serializes work that could
run concurrently*. Declare an edge only when B literally cannot begin until
A completes (B compiles against a symbol A introduces, B's tests need code
A writes first, B mutates state A sets up). Do NOT encode as `depends_on`:
a cross-repo consumer whose contract is already ratified in
`requirements.md` (it doesn't depend on the producer's *implementation* —
build it in parallel against the fixed contract, stub the response; the
**ratified contract is the sync point, model it as a Cross-repo contract
below, not an edge** — e.g. a frontend task consuming a ratified backend
API is `depends_on: []`, not `[<backend tasks>]`); or mere merge/read order.
If your self-adversarial pass flags an edge as "soft / for clarity / not a
hard blocker," **remove it** (note sequencing in prose if useful) — the
enforced graph doesn't read your annotation. Keep the dependency diagram
faithful to the hard edges only: the graph the human approves is the one
that runs.

## 2. Solution Approaches (top-level altitude — only when a fork exists)

When a genuine architectural fork exists for the **whole story** (not a
per-task implementation detail), write a `## Solution Approaches` section:
2–3 whole-solution options (name, summary, trade-offs) + a recommendation.
**Skip this section when no real fork exists** — a single obvious approach
forces no filler comparison; over-triggering is worse than omitting it. Skip
silently, though, and the human at the gate can't tell "no fork" from
"missed one" — so when you skip it, add one line instead: `No architectural
fork identified: <why>` under the task table, visible either way.

## 3. Per-task detail

- **Test-intents**: named tests + one-line intent each, per task, as a
  literal block under that task's section:
  ```
  Test-intents:
  - test_name_one — one-line intent
  - test_name_two — one-line intent
  ```
  Name each test exactly as the developer should write it (function/method
  name) — `verify-red` mechanically checks these literal names appear in
  the actual test files (coverage B1); the orchestrator carries the name
  list, not the intent prose, into `plan-register`'s `test_intents` field.
- **`[API: <lib> v<X>]`** annotations where a task prescribes a library API
  — the developer verifies the real signature before writing call sites.
- **≤2 pattern hints** per task: existing test files via bounded globbing
  (≤5 globs), ranked by mtime, filename match only (no reading contents).
  "No match" is a valid answer — don't widen the search.
- **Per-task approach options** where a single task (not the whole story)
  has more than one reasonable implementation — distinct from the
  story-level altitude in step 2.
- **Cross-repo contracts**, multi-repo stories only: name the producer repo,
  consumer repo(s), and signature fragment(s) for anything one repo's task
  emits and another's depends on — the enriched shape in `steps/plan.md`'s
  registration example.

## 4. Diagrams

Four Mermaid diagrams, styled per `shared/diagram-styling.md`:

1. **Class/type diagram** (`classDiagram`) — new/modified types,
   relationships, key methods. Mark scope with `:::new` / `:::modified` on a
   dedicated `class <Name>:::<style>` statement — NOT on a relationship
   endpoint or bare reference (classDiagram grammar rejects those; see
   `shared/diagram-styling.md`).
2. **Runtime flowchart** (`flowchart TD`) — the feature's runtime flow,
   decision points, external interactions, error paths.
3. **Sequence diagram** (`sequenceDiagram`, `autonumber`) — actor
   interactions, call order, sync vs async, key payloads.
4. **Task dependency graph** (`flowchart LR`) — one node per task id, edges
   from `depends_on`. Required for every story, even single-task ones.

## 5. Self-adversarial pass

Before finalizing: list ambiguities, risks, and unchecked assumptions in the
draft. Revise or flag them — don't ship a plan you haven't tried to break.

## 6. Register + advance

Mechanics (the `plan-register` call, gate advance) live in
`steps/plan.md` — this file is the content contract, that file is the
registration procedure. Don't duplicate one into the other.
