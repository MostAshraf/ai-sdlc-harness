# Step: plan-review (adversarial panel — reviewer shape, modes `plan-attack` + `plan-review`)

Independent adversarial review of the plan BEFORE the human sees it — the
planner's self-adversarial pass is the planner grading its own homework;
this step is a panel of hostile perspectives plus a synthesizer with a
hook-captured verdict. The human at ⟨approve-plan⟩ then approves with
independent evidence attached, not just the planner's word.

1. **The lens panel.** Resolve THIS run's panel:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness resolve-lenses --run <run>` —
   change_type-aware declared data (`plan_review.lenses` overlaid per
   `lenses_by_change_type`; chore/docs ship with an empty panel by
   default). Never hand-derive the list from config files.
   FIRST, if any `<run>/reports/plan-attack-*.md` exist from a previous
   round, snapshot them aside (`plan-attack-<lens>-r<n>.md`, same
   convention as plan-r<n>) — a stale lens report left at the live path
   is indistinguishable from a current one. Then, for EACH configured
   lens, spawn `reviewer` with `harness-mode: plan-attack` (+
   `harness-run`, `harness-repo` — NO `harness-task`), naming the lens in
   the ask; each follows `steps/plan-attack-task.md` (the lens vocabulary
   and findings format). Batch ALL lens spawns in ONE message (foreground
   — the standard parallelism rule; hook-CHECKED, not just stated: a lens
   spawn arriving after a sibling completed this round logs a flagged
   `panel-serialized` event). Lenses are read-only: persist each
   report verbatim to `<run>/reports/plan-attack-<lens>.md`. A lens's
   `verdict:` line is its advisory recommendation — the engine never
   reads it (verdict_bound filters on mode `plan-review`), so it can
   neither move the window nor burn the round budget. An EMPTY lens list
   is the declared single-reviewer fallback: skip the panel (this item)
   and go straight to step 2.
2. **The synthesis** — only after EVERY configured lens's report is
   persisted (or that lens's stall has been escalated per the stall rule
   below; name any permanently-absent lens in the ask — a silently
   degraded panel is worse than a declared one). Spawn `reviewer` with
   `harness-mode: plan-review` (+ run/repo headers, NO task) and NAME
   THIS ROUND's exact lens report paths in the ask (zero lenses → say so:
   the synthesizer must never glob the reports directory, where prior
   rounds' files live): it follows `steps/plan-review-task.md` — reads
   exactly the named lens reports, spot-verifies their findings against
   the real code instead of relaying them raw, groups by root cause
   (independent convergence of two lenses on one root cause is a
   confidence signal — say so), runs its own checklist (AC coverage,
   conventions, graph/contract audit, scope containment), and issues THE
   verdict — the one the exit rule below reads. Persist its report to
   `<run>/reports/plan-review.md` verbatim and record the declared
   artifact: `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact
   --name plan-review-report --value reports/plan-review.md --run <run>`.
   Re-persist on every round — the report at the gate must be the LATEST
   round's, and keep prior rounds recoverable as
   `reports/plan-review-r<n>.md` (same snapshot convention as plan-r<n>).
   **Every revision round re-runs the FULL panel** (lenses + synthesis) —
   a revised plan can introduce new contradictions that a fix-focused
   re-check alone would miss; the round budget already bounds the cost.
3. **Exits are derived, not chosen** (the manifest's `verdict_bound`; the
   cursor refuses anything else):
   - Verdict `APPROVED` → forward (`show` + the manifest tell you which
     step that is; a refused advance names the legal candidates): full
     mode → `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to approve-plan
     --run <run>`; lean mode → the exception gate self-skips on an
     approved panel (its `when` reads the ENGINE-recorded
     `plan-review.outcome`; the skip lands in the ledger as a
     gate-skipped event), so the forward target is `preflight` directly.
   - Verdict `CHANGES_REQUESTED`, rounds left → the ONLY legal move is
     back: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to plan --run
     <run>`. Re-enter plan per its step 0b
     (snapshot, revise, re-register — re-entry re-arms registration
     mechanically, so plan-register must run again even for an unchanged
     decomposition), then
     walk forward into this step again and re-run the FULL panel (steps
     1–2). Feed the synthesizer's numbered findings to the planner
     verbatim — they are the revision input.
   - Verdict `CHANGES_REQUESTED`, bound exhausted (`review_rounds.max`
     CHANGES_REQUESTED verdicts this cycle) → forward is legal again:
     advance to the plan gate — ⟨approve-plan⟩ in full mode,
     ⟨approve-plan-lean⟩ in lean (exhaustion is exactly when lean's
     exception gate FIRES) — and present the FAILING report alongside
     the plan — round N+ signals plan drift, and that is the human's call,
     never an auto-approval and never a deadlock. Say explicitly at the
     gate that the review bound was exhausted.
4. At the plan gate (⟨approve-plan⟩ / ⟨approve-plan-lean⟩, `gate.md`): the
   gate presents the plan; ALSO show `<run>/reports/plan-review.md`
   verbatim — approval should be made with the independent review in view
   either way (passing or failing).

Stalls, per panel member — each spawn gets its OWN bounded counter (the
declared bounds are calibrated per spawn; pooling three spawns into one
counter would hit `human_after` on unrelated hiccups):

- **Synthesizer** stalled (no status block, or an uncaptured verdict —
  its verdict is the one the engine needs): `${CLAUDE_PLUGIN_ROOT}/bin/harness
  stall --run <run>` (no `--task` → counted as `step:plan-review`).
  Look for the verdict in `<run>/reviews.ndjson` — that is the ONLY ledger
  verdicts are written to; `<run>/events.ndjson` holds stall/hook/
  status-block events. EXCEPT: a `status-block-malformed` event in
  `<run>/events.ndjson` means the verdict WAS captured despite the loose
  block — proceed on the ledger, never stall (a stall here re-runs the whole
  panel to re-derive a verdict the engine already holds, and the duplicate
  capture burns a review round). The verb refuses this itself when a verdict
  for the current round exists; if the synthesizer genuinely stalled AFTER
  that capture, re-run with `--confirm-no-verdict`.
- **A lens** stalled (no status block / no report — an oddly-formatted
  advisory verdict alone is NOT a stall; the engine never reads lens
  verdicts): `${CLAUDE_PLUGIN_ROOT}/bin/harness stall --run <run>
  --task step:plan-review:<lens>` — any `step:`-prefixed key gets the
  step-counter treatment, and real task ids can never contain `:`.

Follow the returned action (`reinvoke` → `recovery` → `human`); never loop
re-spawns outside these counters.
