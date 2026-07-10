---
name: repo-map-refresh
description: >
  Regenerate the auto-generated repo map the planner grounds its plans in.
  USER-ENTRY — invoke only when the user explicitly runs /repo-map-refresh;
  never autonomously, never from a subagent (guard-enforced).
---

# repo-map-refresh

The repo map is a navigation aid, never hand-maintained — corrections go
through regeneration (design.md piece 5B). `/init-workspace` and
`/add-repo` both point here for the identical generate-or-regenerate
procedure (step 2 below) — this is the one place it's maintained; don't
fork a second copy of it elsewhere.

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness repo-map-check --repo-name <n> --repo <path>` — report
   missing / fresh / stale (+ commits behind) to the user.
2. To regenerate: spawn the planner shape with `harness-mode: repo-map` as
   the prompt's FIRST line (the spawn guard regex-matches this exact
   header — prose that merely mentions repo-map does not satisfy it; this
   is a declared out-of-run spawn, `pipeline/surfaces.yaml`'s
   `out_of_run_spawns`, legal regardless of whether other runs exist in the
   workspace) and the repo path. Do not guess the planner's `subagent_type`
   string: check your currently available subagent types for whichever one
   corresponds to this plugin's planner (`agents/planner.md`'s frontmatter
   `name: planner` is the identifying detail to match against) —
   `hooks/guards.py`'s spawn guard matches on this shape by the LAST
   `:`-segment of whatever string you pass, and a wrong guess doesn't
   error, it just silently skips the guard's enforcement (fail-closed
   spawn gating, planner's write-confinement to `ai/`/`.claude/context/`),
   which is worse than the guess looking right. The planner can only write
   under `ai/<run>/` and `.claude/context/` (guard-enforced — never repo
   source), so point it at `.claude/context/repo-map/<name>/`. It writes
   the tiered map there: a short top-level index (directories/modules by
   purpose) plus per-area detail files, each loadable alone.
3. Stamp it yourself, not the planner:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness repo-map-stamp --repo-name <n> --repo <path>`
   — stamping is the orchestrator's job, never the planner's own.
4. Remind the user: the planner still reads real code for areas it plans to
   touch — the map speeds targeting, it doesn't replace reading.
