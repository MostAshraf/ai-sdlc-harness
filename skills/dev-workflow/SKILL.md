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
wrapper script that resolves the plugin venv (created by /init-workspace,
either OS layout) and falls back to system `python3`/`python` pre-setup;
it runs under Git Bash on Windows too. `--workspace <ws>` and
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
   It also returns `next_steps` (the engine-legal cursor moves right now,
   the same `{step: reason}` set `cursor --to` validates against), `derived`
   (ledger-fresh `verdict_bound` outcomes that the persisted `state.artifacts`
   cache hasn't caught up to yet — e.g. a plan-review already APPROVED in the
   ledger shows `{"plan-review.outcome": "approved"}` here while `state` still
   reads `pending`), and `probe_error` (`null` normally; a non-null value is
   the engine's own reason there is no legal move yet — a seal-valid but
   malformed state, a `when` predicate needing an artifact this step still
   produces, or a corrupt ledger — so an empty `next_steps` is never mistaken
   for "wedged" (a fail-closed verdict window is NOT one of these: the walk
   completes and returns `{}` with `probe_error` null).
   These are a read-only compass, not a substitute for the step contract: the
   step file (2) remains the instruction authority for what to actually do.
2. Read the step's file: `${CLAUDE_PLUGIN_ROOT}/skills/dev-workflow/steps/<step>.md`
   — load ONE step file at a time (context economy). Gate steps all use
   `steps/gate.md`.
3. Execute it. Spawning a shape? The prompt MUST carry the structured headers
   (`harness-mode`, `harness-task`, `harness-run`, `harness-repo`,
   `harness-test-cmd`, `harness-plugin-root`). **Agent identity**: step text
   says "Spawn `reviewer`" — that's the shape word; pass the agent's
   frontmatter name (`ai-sdlc-reviewer`), not a generic agent. See
   `shared/spawn-identity.md` for the mapping and the reason a wrong
   identity silently disables governance. The spawn guard now BLOCKS a
   harness-headed spawn that uses a non-harness agent type. Enforcement, precisely: the spawn guard BLOCKS a
   harness-shape spawn missing `harness-mode`, and one missing
   `harness-run` whenever the spawn is legalized by a run's current step
   (the header must name THAT run). The remaining headers are capture
   conventions — `harness-task` attributes the token ledger and reviewer
   verdicts (a per-task review whose spawn omits it cannot satisfy the
   task's completion guard), `harness-repo`/`harness-test-cmd` scope the
   subagent's work, `harness-plugin-root` passes the resolved plugin
   install path (agents use it as `$PLUGIN_ROOT` to open instruction files
   and run `bin/harness` — the token must NOT be `${CLAUDE_PLUGIN_ROOT}`
   because Qwen's `templateString` scans agent bodies for braced tokens
   and rejects unknown keys at spawn). Before
   every spawn, resolve its model: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   resolve-model --shape <shape> --mode <mode>` (per-mode ?? per-shape
   default ?? `inherit`, from `subagent_models`). Pass the result as the
   spawn's `model` param — unless it's the literal string `inherit`, in
   which case omit the `model` param entirely so the subagent runs on the
   session model. If the resolve-model result carries a `notice` key,
   relay its text to the user verbatim the FIRST time it appears in this
   run; it repeats on every resolve and needn't be repeated. **Foreground
   where the tool supports it:** pass `run_in_background: false` — under Qwen
   Code that is REQUIRED (the guard blocks an absent or true value; its
   background format is unmeasured). Where the Agent tool has no such
   parameter, the spawn returns a launch STUB and the agent runs in the
   background: that is expected and captured — WAIT for its completion
   notification, do not proceed on the stub, and do not `stall` (`show`'s
   `outstanding_spawns` names every spawn still in flight — the Stalls
   triage below starts there, not at the events tail). Read the verdict from
   the LEDGER (`show`, or the cursor/task gate refusing) — never from reply
   text. One live spawn per (task, mode): the guard refuses a second while
   the first is unreported; different tasks and modes stay parallel, panel
   lenses (`plan-attack`) are exempt, and batching spawns in ONE message
   still runs them concurrently. `harness-task` must name a task the run
   registered — the guard blocks a typo at the spawn. Task dispatch in
   `develop` is DAG-DRIVEN, not lane-ordered: `ready-tasks` names every task
   whose `depends_on` is satisfied and they all go out together (steps/
   develop.md owns the loop).
4. Advance: `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next> --run <run>`. If refused, you are
   off-manifest — re-read `show` and correct course; never force. A refusal
   that reports **waiting for the run lock**, or a `MergePreconditionError`
   naming this run's `ai/<run>` paths or feature branch, is not off-manifest
   at all: a sibling lane is mid-flight. Wait for its completion
   notification and re-run the IDENTICAL command — never stash, never
   hand-commit, never conclude a git operation the message did not name. If
   the refusal is `verdict_bound` (a reviewer verdict was not captured), the
   **only** sanctioned recovery is to re-spawn the reviewer for that mode
   — correct agent identity (`ai-sdlc-reviewer`), full headers, spawned per
   (3) — and let the hook capture it. Never write `reviews.ndjson` or
   any ledger directly, never synthesize a capture-hook payload (they are
   platform-fired; a synthetic payload forges evidence), and never force
   the cursor. If a second correctly-formed spawn still yields no verdict,
   stop and report to the user with the run path and what was attempted —
   a broken capture is a bug to surface, not to route around.

## Cross-cutting rules

- **Session cwd:** stay at the workspace ROOT — the prompt-capture hook
  defaults its workspace from cwd, so a bare `cd` into a repo silently
  drops gate evidence (field-proven). Use absolute paths / `git -C` /
  `(cd X && …)` subshells for repo work.
- **Gates:** always `gate --present`, show the artifact to the user verbatim,
  wait for their reply, then `gate --decide`. The decision is derived from
  captured human input — you cannot write it, only request the derivation.
  The reply must be a plain typed chat message (never AskUserQuestion —
  its answers bypass the capture hook and can never qualify). Refusals NAME
  their cause, and the remedies DIFFER: a STALE reply, or one typed in
  ANOTHER session, wants one more reply then `--decide` alone; re-present
  only where steps/gate.md says (ad-hoc route, or a confirmed-dead session).
- **Stalls:** a subagent that stops without a status block → `${CLAUDE_PLUGIN_ROOT}/bin/harness stall
  --task <T>` and follow the returned action (`reinvoke` → `recovery` →
  `human`). For a TASK-LESS spawn (plan-review, pre-pr, analyze-comments)
  omit `--task` — the stall counts per step, same bounds — except when the
  step file declares finer keys (plan-review counts each panel lens as
  `--task step:plan-review:<lens>`; the step file is the authority). NEVER
  commit or write on a stalled agent's behalf. **Verdicts live in
  `<run>/reviews.ndjson`; `<run>/events.ndjson` carries stall/hook/
  status-block events, never verdicts** — looking for a verdict in the wrong
  ledger is what makes a captured round read as a stall. Blockless reply?
  Ask `show` before calling `stall`: `outstanding_spawns` names every spawn
  still in flight as `{task, mode, agent_id, step, at, clearable,
  clearing_key}` — the owned answer to "which lane is wedged", so never
  hand-read the events ledger for it. Yours listed there → that subagent is
  still running in the background, WAIT for it; never stall — `stall`
  refuses over an open pending, and forcing it (`--confirm-no-verdict`)
  ABANDONS that spawn: its key is freed for a re-spawn and its reply, if it
  ever lands, is refused rather than captured. Three exceptions, in order:
  - **`clearable: false`** (a `repo-map` / `request-triage` pending — no
    step's spawn-set declares its mode, so no `stall` key matches it) →
    **leave it.** It refuses nothing and cannot wedge a run; it clears when
    that agent stops. Forcing a key here bumps a stall counter, writes no
    override, clears nothing, and degrades the run for a brand-new reason.
  - **`step` is not the cursor's** → the run has LEFT that step and nothing
    is coming. Abandon it with the key the entry hands you:
    `stall --task <clearing_key> --confirm-no-verdict`.
  - **`step` IS the cursor's, and siblings share it** — pipelined `develop`
    holds every lane at one step (the cursor cannot leave while any task is
    non-terminal), so a dead lane and a live one differ only in `at`.
    Compare ages: the OUTLIER — launched with the batch, still open long
    after its siblings reported — is the wedged lane. Age is evidence, not
    proof (a big task is slow, not dead), so name the lane and why, and get
    the user's go-ahead before `stall --task <its clearing_key>
    --confirm-no-verdict`.
  NOT listed → check `legacy_spawn_pendings` first: one listed there was
  launched by a pre-upgrade harness, its deferred capture is impossible, and
  the fix is to re-spawn that agent in the FOREGROUND — never stall it.
  Otherwise read the TAIL of `<run>/events.ndjson`: `status-block-malformed`
  → the verdict WAS captured despite the loose block, proceed on the ledger,
  never stall; `missing-status-block` → genuine stall, procedure above. For
  a task-less **step** key, `stall` refuses outright when that step's ledger
  already holds a verdict for the current round (`--confirm-no-verdict`
  overrides, for a spawn that stalled *after* the capture); per-task and
  per-lens keys are never refused — the engine reads no verdict for those.
- **Ad-hoc human requests mid-run:** spawn `reviewer` with
  `harness-mode: request-triage` (+ plugin-root header, always legal), surface the triage verdict
  to the user; out-of-scope items are never silently merged.
- **Publish:** the mirror snapshots the run's audit trail INTO a registered
  **project repo's** feature branch (so the governance record travels with
  the code in the PR) — never into the workspace, which isn't a git repo and
  isn't a mirror target. So it only makes sense once a repo has a feature
  branch, i.e. **after `preflight`**. Rules:
  - **Before preflight** (`fetch`/`confirm-repo`/`intake`/`plan`/
    `plan-review`/⟨approve-plan⟩/⟨approve-plan-lean⟩): there is no branch yet
    — **skip the mirror entirely**, don't guess a `--repo`.
  - **After preflight** (task completion, and each later gate crossing):
    mirror into **every preflighted repo** (the `branches` artifact in
    `show` lists them) — one call per repo:
    `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --run <run>`.
  - It's best-effort/non-blocking: a repo that can't be committed to (no
    git, detached, etc.) just isn't mirrored — never block the run on it.
- **Status:** render progress with `${CLAUDE_PLUGIN_ROOT}/bin/harness show`; the ledgers
  (`events/tokens.ndjson`) are append-only — read, never write.
