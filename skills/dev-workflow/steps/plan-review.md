# Step: plan-review (reviewer shape, mode `plan-review`)

Independent review of the plan BEFORE the human sees it — the planner's
self-adversarial pass is the planner grading its own homework; this step is
a second pair of eyes with a hook-captured verdict. The human at
⟨approve-plan⟩ then approves with independent evidence attached, not just
the planner's word.

1. Spawn `reviewer` with headers (`harness-mode: plan-review`,
   `harness-run`, `harness-repo` — NO `harness-task`: this reviews the
   run's plan, not a task; the verdict records task-less in
   `reviews.ndjson`, which is exactly what the exit rule below reads). It
   follows `steps/plan-review-task.md` (the content contract): AC
   coverage, conventions consistency, dependency-edge and
   contract-signature audit, scope containment.
2. The reviewer is read-only: it reports the review in its status block;
   YOU persist it to `<run>/reports/plan-review.md` verbatim and record
   the declared artifact: `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact
   --name plan-review-report --value reports/plan-review.md --run <run>`.
   Re-persist on every round — the report at the gate must be the LATEST
   round's, and keep prior rounds recoverable as
   `reports/plan-review-r<n>.md` (same snapshot convention as plan-r<n>).
3. **Exits are derived, not chosen** (the manifest's `verdict_bound`; the
   cursor refuses anything else):
   - Verdict `APPROVED` → forward: `${CLAUDE_PLUGIN_ROOT}/bin/harness
     cursor --to approve-plan --run <run>`.
   - Verdict `CHANGES_REQUESTED`, rounds left → the ONLY legal move is
     back: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to plan --run
     <run>`. Re-enter plan per its step 0b
     (snapshot, revise, re-register — re-entry re-arms registration
     mechanically, so plan-register must run again even for an unchanged
     decomposition), then
     walk forward into this step again and re-spawn the reviewer. Feed the
     reviewer's numbered findings to the planner verbatim — they are the
     revision input.
   - Verdict `CHANGES_REQUESTED`, bound exhausted (`review_rounds.max`
     CHANGES_REQUESTED verdicts this cycle) → forward is legal again:
     advance to ⟨approve-plan⟩ and present the FAILING report alongside
     the plan — round N+ signals plan drift, and that is the human's call,
     never an auto-approval and never a deadlock. Say explicitly at the
     gate that the review bound was exhausted.
4. At ⟨approve-plan⟩ (`gate.md`): the gate presents the plan; ALSO show
   `<run>/reports/plan-review.md` verbatim — approval should be made with
   the independent review in view either way (passing or failing).

A stalled reviewer (no status block, or an uncaptured verdict) follows the
standard bounded stall procedure: run the stall verb WITHOUT `--task`
(`${CLAUDE_PLUGIN_ROOT}/bin/harness stall --run <run>`) — task-less spawns
are counted per step, against the same declared bounds — and follow the
returned action (`reinvoke` → `recovery` → `human`). Never loop re-spawns
outside that counter.
