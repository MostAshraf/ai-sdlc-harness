---
name: dev-workflow
description: >
  Run the governed SDLC pipeline for a work item. USER-ENTRY — invoke only
  when the user explicitly runs /dev-workflow <work-item-id>; never trigger
  autonomously from conversation, and never from a subagent (guard-enforced).
---

# dev-workflow — the thin orchestrator walker

You are the **orchestrator**: a coordinator, not an implementer. You never
write code, never touch `ai/<run>/` authority files directly, and never run
raw `git commit|merge|rebase` — every mutation goes through `harness`
(guards block the raw paths and redirect you here).

Every command below runs through `${CLAUDE_PLUGIN_ROOT}/bin/harness` — a
wrapper script that resolves the plugin venv (created by /init-workspace)
and falls back to system `python3` pre-setup. `--workspace <ws>` and
`--run <run>` may go before or after the verb, in any mix — e.g. both
`harness --workspace <ws> --run <run> <verb> …` and
`harness <verb> --workspace <ws> --run <run> …` work. Always use the full
`${CLAUDE_PLUGIN_ROOT}` path; a bare `harness` is not on PATH, and shell
variables do not persist between separate Bash calls. Non-zero exit =
refused; read the JSON error.

## Startup

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness fetch --id <work-item-id>` — refuses if bootstrap is incomplete
   (run `/init-workspace` first) or a live run already exists (offer the user
   **Resume or Abort** — never clobber). Abort is a real verb:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness abort --run <run> --reason "<why>"` —
   terminal (mutations refuse from then on), sweeps worktrees, keeps the
   audit trail, and releases the work-item slot so a fresh fetch works.
   On success note `run`, `mode`.
2. The pipeline manifest (`${CLAUDE_PLUGIN_ROOT}/pipeline/manifest.yaml`) is
   the single source of truth for step order. Do not improvise steps.

## The walk

Loop until the mode's sequence is exhausted, then close the run:
`${CLAUDE_PLUGIN_ROOT}/bin/harness complete --run <run>` (terminal, the
successful sibling of abort — the final step's file says exactly when).

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness show --run <run>` → current step, mode, tasks, gates.
2. Read the step's file: `${CLAUDE_PLUGIN_ROOT}/skills/dev-workflow/steps/<step>.md`
   — load ONE step file at a time (context economy). Gate steps all use
   `steps/gate.md`.
3. Execute it. Spawning a shape? The prompt MUST carry the structured headers
   (`harness-mode`, `harness-task`, `harness-run`, `harness-repo`,
   `harness-test-cmd`). Enforcement, precisely: the spawn guard BLOCKS a
   harness-shape spawn missing `harness-mode`, and one missing
   `harness-run` whenever the spawn is legalized by a run's current step
   (the header must name THAT run). The remaining headers are capture
   conventions — `harness-task` attributes the token ledger and reviewer
   verdicts (a per-task review whose spawn omits it cannot satisfy the
   task's completion guard), `harness-repo`/`harness-test-cmd` scope the
   subagent's work. Before
   every spawn, resolve its model: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   resolve-model --shape <shape> --mode <mode>` (per-mode ?? per-shape
   default ?? `inherit`, from `subagent_models`). Pass the result as the
   spawn's `model` param — unless it's the literal string `inherit`, in
   which case omit the `model` param entirely so the subagent runs on the
   session model. Every harness-shape spawn runs FOREGROUND — pass
   `run_in_background: false` explicitly (newer platforms default to
   background, and capture reads the spawn's own tool_response; a
   background spawn returns only a launch stub — verdict lost, stall
   event fabricated; the guard blocks an explicit `true`). Parallelism =
   batch multiple foreground spawns in ONE message, never backgrounding.
4. Advance: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next> --run <run>`. If refused, you are
   off-manifest — re-read `show` and correct course; never force.

## Cross-cutting rules

- **Session cwd:** stay at the workspace ROOT — the prompt-capture hook
  defaults its workspace from cwd, so a bare `cd` into a repo silently
  drops gate evidence (field-proven). Use absolute paths / `git -C` /
  `(cd X && …)` subshells for repo work.
- **Gates:** always `gate --present`, show the artifact to the user verbatim,
  wait for their reply, then `gate --decide`. The decision is derived from
  captured human input — you cannot write it, only request the derivation.
  The reply must be a plain typed chat message (never AskUserQuestion —
  its answers bypass the capture hook and can never qualify). A refusal
  means no qualifying reply: re-present or route to ad-hoc.
- **Stalls:** a subagent that stops without a status block → `${CLAUDE_PLUGIN_ROOT}/bin/harness stall
  --task <T>` and follow the returned action (`reinvoke` → `recovery` →
  `human`). NEVER commit or write on a stalled agent's behalf.
- **Ad-hoc human requests mid-run:** spawn `reviewer` with
  `harness-mode: request-triage` (always legal), surface the triage verdict
  to the user; out-of-scope items are never silently merged.
- **Publish:** the mirror snapshots the run's audit trail INTO a registered
  **project repo's** feature branch (so the governance record travels with
  the code in the PR) — never into the workspace, which isn't a git repo and
  isn't a mirror target. So it only makes sense once a repo has a feature
  branch, i.e. **after `preflight`**. Rules:
  - **Before preflight** (`fetch`/`intake`/`plan`/⟨approve-plan⟩): there is
    no branch yet — **skip the mirror entirely**, don't guess a `--repo`.
  - **After preflight** (task completion, and each later gate crossing):
    mirror into **every preflighted repo** (the `branches` artifact in
    `show` lists them) — one call per repo:
    `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --run <run>`.
    In a single-repo run that's one call; in a multi-repo run, one per repo.
  - It's best-effort/non-blocking: a repo that can't be committed to (no
    git, detached, etc.) just isn't mirrored — never block the run on it.
- **Status:** render progress with `${CLAUDE_PLUGIN_ROOT}/bin/harness show`; the ledgers
  (`events/tokens.ndjson`) are append-only — read, never write.
