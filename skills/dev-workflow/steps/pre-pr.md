# Step: pre-pr (reviewer shape, mode `pre-pr`)

1. Multi-repo runs first: `${CLAUDE_PLUGIN_ROOT}/bin/harness reconcile-contracts --run <run>` — drift
   goes in front of the human at the gate, never auto-fixed.
2. Spawn `reviewer` with `harness-mode: pre-pr` (+ run/repo headers): a
   holistic review of the full feature branch — cohesion across tasks,
   review-policy rules, plan-vs-implementation drift, the residuals the TDD
   lock can't catch (semantically-empty tests, impl-overfit).
3. The reviewer is read-only: it reports the review in its status block; YOU
   persist it to `<run>/reports/pre-pr.md` verbatim and record the declared
   artifact: `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name pre-pr-report
   --value reports/pre-pr.md --run <run>`.
4. Advance to ⟨approve-pre-pr⟩ (`gate.md`); present the report AND the
   contracts verdict. Rejection routes to `pre-pr-fixes` (declared edge).
