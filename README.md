# ai-sdlc-harness · v3.0

[![Available on CodeGuilds](https://img.shields.io/badge/Available_on-CodeGuilds-6366f1)](https://codeguilds.dev/packages/ai-sdlc-harness)

**A governed multi-agent SDLC pipeline for Claude Code** — a ground-up rewrite of [ai-sdlc-harness](https://github.com/MostAshraf/ai-sdlc-harness). Drives a real engineering workflow — fetch → scope-confirmed plan → independent plan review → proven-red TDD → review → security → PR → comment rounds → reconcile → metrics — across one or many repos. No application code lives here: only the pipeline manifest, the Python core that enforces it, and the agents, skills, and hooks that run it.

| Command | Purpose |
|---|---|
| `/init-workspace` | One-time setup interview: provider, repos, discovered toolchain, verification gate |
| `/dev-workflow <work-item-id>` | Take a work item from requirements to merged PR end-to-end |
| `/story-workflow <command> <work-item-id>` | Shape a story's quality before it's built: `analyze` · `refine` · `improve` · `groom` |
| `/workflow-status` | Read-only dashboard: cursor, tasks, gates, flagged events, run-health verdict per run |
| `/workspace-config` | Change one config section without re-running the interview |
| `/add-repo` | Register one new repo into an already-bootstrapped workspace |
| `/migrate-workspace` | Adopt a v2.x workspace: config carries over, run history stays archived in place |
| `/repo-map-refresh` | Regenerate the auto-generated codebase map the planner grounds its plans in |

## The rewrite, in one table

The original harness works, but almost all of its accumulated complexity compensates for one root cause: *orchestration logic lived in markdown prose an LLM had to faithfully execute, with hooks bolted on to catch the cases where it didn't.* The rewrite moves every mechanical rule into code that just runs, and reserves the model for judgment.

| Concern | ai-sdlc-harness (v2.x) | ai-sdlc-harness (v3.0) |
|---|---|---|
| Pipeline definition | Prose phase files the orchestrator re-derives every run; a separate hardcoded copy in guard scripts | One declared manifest ([pipeline/manifest.yaml](pipeline/manifest.yaml)) read by **both** the orchestrator and the enforcement layer — no second copy to drift |
| Git operations | Raw `git` calls reverse-engineered after the fact by `shlex`-parsing hooks | Owned entry points (`harness commit` / `merge-task` / `sync-branch` / `push` …); the raw verbs are blocked outright |
| Workflow state | `tracker.md`, honor-system updates | HMAC-chain-sealed `state.yaml` + append-only ndjson evidence ledgers — tamper-evident, `exit 3` on out-of-band edits |
| Human gates | The model reads your reply and acts on it | A hook captures your reply verbatim; deterministic code parses the decision — the model cannot approve on your behalf |
| TDD enforcement | Separate Tester and Developer subagents per task (token/latency tax) | One developer writes test + implementation; `harness verify-red` *proves* the failure and blob-SHA-locks the test set until completion |
| Providers | Markdown capability docs an agent must correctly read | Code modules behind one interface, each held to a shared contract test |
| Agents | 7 role files fusing permissions with procedure | 3 fixed tool-grant *shapes* (planner / developer / reviewer); procedure lives in shared step files |
| File-size budgets | Retrofitted after files passed 400 lines | ~100/200-line budget enforced from day one (`tools/budget_check.py`) |

## Install

This repo is **dual-native**: a native Claude Code plugin (`.claude-plugin/`) and a native Qwen Code extension (`qwen-extension.json`) in one repository. Install it under either CLI — no conversion step is needed.

### Claude Code

```
/plugin marketplace add MostAshraf/ai-sdlc-harness
/plugin install ai-sdlc-harness@ai-sdlc-harness
```

The repeated name isn't a typo — it's `plugin-name@marketplace-name`, and here they match. Restart Claude Code (or `/reload-plugins`) so the skills and hooks load. There's a non-interactive equivalent too, if you'd rather script it:

```sh
claude plugin marketplace add MostAshraf/ai-sdlc-harness
claude plugin install ai-sdlc-harness@ai-sdlc-harness   # --scope user|project|local
```

### Qwen Code

```
qwen extensions install MostAshraf/ai-sdlc-harness
```

For development with a live link to your working copy:

```
qwen extensions link /path/to/ai-sdlc-harness
```

Restart Qwen Code so the skills, agents, and hooks load.

### After install

Then, inside Claude Code or Qwen Code:

```
/init-workspace
```

The interview asks only what it must (provider, repos), *discovers* your toolchain (proposing the test command it found), and offers **default** for everything else. It bootstraps a plugin-owned Python venv (PEP 668-safe — no system-python changes), ends with a verification gate — every check passes or you don't proceed — and writes per-section config under `.claude/context/`, a permission allowlist into `.claude/settings.json` (no manual `settings.json` editing needed), and the bootstrap marker.

## Prerequisites

| Dependency | Why |
|---|---|
| **Claude Code** or **Qwen Code** | The CLI that runs this harness. Install Claude Code from [claude.ai/code](https://claude.ai/code) or Qwen Code from [qwen-code](https://github.com/QwenLM/qwen-code). |
| **Git** | Branch management, per-task worktree isolation, owned commits. |
| **Python 3.10+** | The entire core is Python. `/init-workspace` creates the plugin's own venv with PyYAML as its first step; until then the guards print a one-line notice and stand down. |
| **Provider CLI, authed** *(optional)* | `gh auth login` / `glab auth login` / `az login`, if using that provider. MCP providers need their server connected. The `local-markdown` provider needs nothing at all. |

Target repos must be **cloned locally**, clean, and on their default branch when registered — the harness does not clone them. No language prerequisites; toolchains are discovered.

---

## Running under Qwen Code

This repo carries a `qwen-extension.json` at root, making it a **native Qwen Code extension** — no Claude-plugin conversion is needed. Install via `qwen extensions install` or `qwen extensions link` (see [Install](#install) above), then run `/init-workspace` inside a Qwen Code session.

### What happens under Qwen Code

When `/init-workspace` runs under Qwen Code (`QWEN_CODE=1` is set in that session):

- **Permissions are mirrored to `.qwen/settings.json`.** Qwen Code reads its allowlist from `.qwen/settings.json` rather than `.claude/settings.json`; the init step writes both so background agents run unprompted under either CLI. A workspace bootstrapped under Claude Code and later opened under Qwen Code has no `.qwen/settings.json` yet — re-run `/workspace-config` (or the permission-write step) once under Qwen Code to create it.
- **`CLAUDE_PLUGIN_ROOT` is exported via the `.qwen/settings.json` `env` block** (self-healing on reinstall). Native Qwen does not export this variable or substitute it in markdown, so the init step's bootstrap probe resolves the plugin root on first run and prints it for the model to substitute textually in subsequent commands (each Bash call is a fresh subprocess, so a shell export doesn't persist). The env export written at step 6 takes over from the **next session**. A stale value from a prior install self-heals when the stored path no longer exists on disk; a deliberate user pin pointing at a real directory is preserved.
- **`.qwen/context` is symlinked to `../.claude/context`** (relative, so a workspace move doesn't dangle it). This is an affordance for the **converted-install** path only: Qwen's Claude-plugin converter rewrites `.claude/` → `.qwen/` in installed markdown, so skills point the model at `.qwen/context/…` while the CLI reads `.claude/context/`. The symlink makes both land in the single physical tree. Under **native** installs, markdown keeps literal `.claude/context/` paths that already match the physical tree, so the symlink is harmless when unused. The write guard additionally accepts the literal `.qwen/context/` prefix as a confined root. **Windows needs Developer Mode or an admin shell to create symlinks**; if creation fails or a real file occupies the path, init prints a visible warning (no silent data loss).
- **Agents carry a union-spelling `tools:` list.** Each agent's frontmatter carries both Claude names (`Read`, `Write`, `Edit`, `Bash`) and Qwen display names (`ReadFile`, `WriteFile`, `Shell`). Claude Code grants the names it recognizes and silently ignores the rest; Qwen Code does the same in reverse. The reviewer stays read-only on both platforms (no write spelling of either dialect).
- **`subagent_models` overrides are honored in Claude Code sessions but cannot be applied at spawn under Qwen Code** — its agent tool has no model parameter, so subagents run on the session model. The harness surfaces this at spawn time (via `resolve-model`) and at config time (via `init-section --section overrides`), relaying a notice to the user the first time it fires in each run. The advanced manual escape hatch is a project-level `.qwen/agents/<name>.md` shadow file with a `model:` line — a full copy that replaces the plugin's agent wholesale and must be manually re-synced after plugin upgrades.

**Folder-trust note:** if you enable Qwen Code's `security.folderTrust.enabled` and the workspace is untrusted, Qwen drops workspace settings **entirely** — both the env export and the permission allowlist go inert, surfacing as permission prompts reappearing. Trust enforcement is off by default; trust the workspace (or keep it off) to keep the dual-write live.

---

## Workflow at a Glance

```mermaid
flowchart LR
    classDef orch fill:#dde6fa,stroke:#446,stroke-width:1.5px,color:#013
    classDef agent fill:#fff7d8,stroke:#b80,stroke-width:1px,color:#530
    classDef human fill:#fff1de,stroke:#a60,stroke-width:1px,color:#420
    classDef output fill:#eaf3ff,stroke:#36b,stroke-width:1px,color:#024

    F([fetch + classify]):::orch
    I[intake + confirm target repos]:::agent
    P[plan]:::agent
    PR[plan-review — adversarial panel]:::agent
    G1{approve-plan}:::human
    G1L{approve-plan-lean, only when the panel exhausted its rounds}:::human
    PF[preflight]:::orch
    D[develop — proven-red TDD per task]:::agent
    G2{approve-impl}:::human
    H[harden]:::agent
    S[security scan]:::orch
    G25{approve-security, when findings meet the threshold}:::human
    CR[confirm-repo — only when several repos are registered]:::orch
    QR[quick-recheck]:::orch
    PP[pre-pr review]:::agent
    G3{approve-pre-pr}:::human
    FX[pre-pr-fixes]:::agent
    CP[create-pr]:::output
    AC[analyze-comments]:::agent
    G4{select-comments}:::human
    AF[apply-fixes]:::agent
    RC[reconcile]:::output
    M[metrics]:::output

    F -->|full / lean| I --> P --> PR
    PR -->|full: panel APPROVED, or round budget exhausted| G1
    PR -->|CHANGES_REQUESTED — forced revision loop| P
    PR -.->|lean: panel APPROVED — gate self-skips| PF
    PR -->|lean: rounds exhausted| G1L
    G1 -->|approved| PF
    G1 -->|rejected| P
    G1L -->|approved| PF
    G1L -->|rejected| P
    F -->|quick, one repo registered| PF
    F -->|quick, several repos| CR --> PF
    PF --> D
    D -->|full| G2
    D -.->|lean — no impl gate| H
    G2 -->|approved| H --> S
    G2 -->|rejected| D
    S -->|below threshold| PP
    S -->|findings meet the threshold| G25
    G25 -->|waive / defer| PP
    G25 -->|fix-now| D
    D -->|quick| QR
    QR -->|clean| PP
    QR -->|dirty — escalate to full| S
    PP --> G3
    G3 -->|approved| CP
    G3 -->|rejected| FX --> PP
    CP --> RC --> M
    CP -.->|on demand, repeatable| AC --> G4 --> AF -.-> RC
    AF -.->|new comments| AC
```

**Full mode** has three unconditional human gates (plan, implementation, pre-PR), one conditional gate (security — fires only when the aggregate finding severity meets the configured threshold, default `medium`), and one multi-pick gate (comment selection, inside the on-demand PR-comments group). Before the plan ever reaches you, an **adversarial plan-review panel** runs: one lens reviewer per resolved lens (`plan_review.lenses` overlaid per change type by `lenses_by_change_type` — default *contradictions & collisions* + *gaps & completeness* for feature/fix/refactor; chore/docs default to the single-reviewer fallback; empty list = single reviewer) attacks the plan in parallel, and a synthesizer verifies their findings against the real code — never relaying them raw — groups them by root cause, checks the numbered acceptance criteria, the codebase's observed conventions, and the confirmed repo scope, and issues the one verdict the engine reads. A `CHANGES_REQUESTED` verdict mechanically forces a revision loop (bounded by `review_rounds.max`; exhaustion escalates to you *with* the failing report attached; every revision round re-runs the full panel), and that hook-captured verdict is what legalizes the step's exits, never the orchestrator's claim. **Quick mode** — trivial changes classified at fetch — keeps only the pre-PR gate. Everything between gates runs hands-off.

**Lean mode** keeps full's entire rigor but gates by exception — the plan gate fires only if the panel exhausts its rounds, there's no implementation gate, and the pre-PR gate stays: one human stop on the happy path. Add `Mode: lean` to a work item, or set `default_mode: lean` for the workspace — see [Choosing a mode](#choosing-a-mode) and [Lean mode](#lean-mode--gating-by-exception).

At **any** cursor position, an ad-hoc human request is legal: it spawns the reviewer in `request-triage` mode (a declared always-legal spawn), which classifies the request against the approved plan. Out-of-scope requests surface back to you with explicit options — never silently merged.

## The Per-Task TDD Loop

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant C as harness CLI
    participant D as Developer
    participant R as Reviewer

    Note over O: develop step — one task at a time, in a dedicated worktree
    O->>C: worktree-add --task T1
    O->>D: spawn with harness-mode develop headers
    D->>D: write the failing tests from the plan's declared test-intents
    Note over D: writes to non-test paths are hook-refused until the red-proof exists
    D->>C: verify-red --task T1
    Note over C: runs the test itself — must genuinely fail.<br/>Seals a chained red-proof + SHA-locks the test set
    D->>D: implement until green, checkpoint via harness commit
    D-->>O: status block SUCCESS
    O->>C: task --id T1 --to in-review
    Note over C: runs verify-green + red-proof check —<br/>a silently weakened test refuses the transition
    O->>R: spawn with harness-mode review
    R->>R: re-run build + tests independently, review the diff
    R-->>O: verdict — hook-captured into reviews.ndjson
    alt APPROVED
        O->>C: task --to done, then merge-task (squash) + worktree-remove
    else CHANGES_REQUESTED
        O->>D: relay numbered findings — same worktree, bounded rounds
    end
```

`task --to done` mechanically **refuses** without a captured `APPROVED` verdict in the ledger — the orchestrator can't paraphrase a review into an approval. Rejection rounds are bounded (`review_rounds.max`, default 5); beyond that the rework transition is refused and you are escalated, on the theory that round N+ signals plan drift, not code drift.

---

## The Agent Shapes

Instead of one file per role fusing "who may do what" with "what procedure to follow", there are three fixed tool-grant **shapes**; the procedure lives in per-mode step files under [skills/dev-workflow/steps/](skills/dev-workflow/steps/) that the shape reads at spawn time.

| Shape | Modes | Tool grant | Guard-enforced restriction |
|---|---|---|---|
| **planner** | `intake` · `plan` · `repo-map` | Read/Grep/Glob/Write/Edit/Bash | Writes only under `ai/<run>/` and `.claude/context/` — never repo source |
| **developer** | `develop` · `harden` · `fixup` | Read/Grep/Glob/Write/Edit/Bash | Works only inside its task worktree; non-test writes refused until the task's red-proof is sealed |
| **reviewer** | `review` · `plan-review` · `plan-attack` · `pre-pr` · `analyze-comments` · `request-triage` | Read/Grep/Glob/Bash | Strictly read-only — no Write/Edit granted, shell writes blocked (a literal `/tmp` scratch path is the one exception); builds and test runs allowed, so it verifies independently instead of trusting another agent's claim |

The **orchestrator** (the main Claude Code conversation running `/dev-workflow`) is deliberately thin: a coordinator that walks the manifest, spawns shapes with structured `harness-mode:` headers, and calls `harness` verbs. It never writes code, never touches run-authority files directly, and never runs raw git — the guards block those paths and point it back to the owned verbs.

## Key Concepts

### The run directory

Everything for one work item lives under a single directory keyed by date + work-item ID (same-day re-runs after an abort get a `-2`, `-3` suffix slot):

```
ai/2026-07-08-PROJ-123/
├── state.yaml            # THE authority: cursor, tasks, artifacts, gate decisions — HMAC-chain-sealed
├── work-item.json        # fetched + provider-normalized work item
├── requirements.md       # intake output
├── plan.md               # edge cases, risk tiers, test-intents, file-touch manifests, AC traceability, diagrams
├── events.ndjson         # every deviation: test revisions, rejections, blocks, stalls, skipped gates
├── tokens.ndjson         # real per-invocation token spend
├── reviews.ndjson        # hook-captured reviewer verdicts (what "done" transitions check)
├── human-input.ndjson    # your gate replies, captured verbatim — never leaves the workspace
├── .redproof/            # sealed per-task red-proofs (read only via `harness show-redproof`)
└── reports/              # security.md, pre-pr.md, metrics.md, coverage
```

### Sealed state and evidence ledgers

`state.yaml` is HMAC-chained (key at `.claude/context/.harness-key`, pinned into git's exclude list and refused by the commit verbs if ever staged). Any out-of-band edit fails verification: every CLI verb exits `3` and refuses to proceed until you inspect and run `harness reseal`. The ndjson ledgers are append-only and chained the same way. At gate crossings, `publish-mirror` commits a **path-exclusive** snapshot of `ai/<run>/` onto your feature branch — minus `human-input.ndjson`, the red-proofs, and lockfiles — so the audit trail travels with the PR while your raw chat text stays local.

### Human gates

Your reply at a gate is captured verbatim by a `UserPromptSubmit` hook into `human-input.ndjson`; `harness gate --decide` then derives the decision from that evidence with a deterministic parser. The grammar:

- **`APPROVED`** — exactly. A qualified approval ("approved, but also…") is *not* an approval; it routes to ad-hoc request triage.
- **A numbered option** or the option's exact word (e.g. `2`, `waive`).
- **Rejection-side options may lead the reply and carry notes** (`rejected — split T2 into…`); forward decisions stay bare by design.
- The comment-selection gate additionally takes a comma-separated pick (`1,3`) or the literal `NONE`.

Gate presentations, skips (a conditional gate whose predicate didn't fire), and rejections are all ledgered; `/workflow-status` surfaces the flagged events.

### Proof-anchored TDD

The property that matters — *the test genuinely failed before the fix existed* — without the original's two-agent tax:

1. The developer writes the failing tests first. This ordering is **hook-enforced**, not advisory: until the red-proof exists, writes to non-test paths in the worktree are refused (test paths, fixtures, and build manifests for test dependencies stay writable — patterns configurable per language in [config/defaults/workflow.yaml](config/defaults/workflow.yaml)).
2. `harness verify-red` runs the test itself — it must fail — then seals a chained red-proof and blob-SHA-locks the test files plus their declared closure (shared fixtures, `conftest.py`, …).
3. The completion transition runs `verify-green` **and** re-checks the locked SHAs: a quietly weakened assertion refuses the transition.
4. A genuinely wrong test is revised via `verify-red --revise --reason "…"` — an explicit, reviewer-visible flagged event, never a silent edit.
5. Tasks a plan explicitly marks with no test-intents (docs, chores) are the approved opt-out: verify-red refuses, the completion guard exempts, review still applies. At any risk other than `low`, the opt-out must carry a recorded `no_test_reason` — registration refuses the silent form and flags the recorded one for review. The mirror is enforced too: a task WITH test-intents must register its file-touch manifest (`files`) naming at least one non-test path — coverage backfill can never satisfy the red-proof, so registration refuses it up front unless a `test_only_reason` records the test-infrastructure exception (flagged `tests-without-production`).

### Owned git entry points

Once a workspace has completed `/init-workspace`, raw commit-creating / history-rewriting git verbs (`commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `am`, `pull`, `push`) are blocked for Claude for the life of that workspace — the guard cannot know a "harness" commit from any other, so it blocks the whole verb rather than trying to tell one commit's intent from another's (your own terminal outside Claude Code is unaffected, and a session that has never run `/init-workspace` sees ordinary git — see [Guardrail Hooks](#guardrail-hooks)). Mutations go through owned verbs that validate, execute, and ledger in one place: `commit` (declared classes `working`/`wip`, `--fixup-of`), `merge-task` (squash / `--autosquash` fold), `worktree-add`/`worktree-remove`, `update-base` (fast-forward a *base* branch onto its remote — fetch + `--ff-only`, refusing any divergence), `sync-branch` (owned rebase of the *current* branch onto a base that moved), `push` (`--force-with-lease`), and `publish-mirror`. Branch and commit naming come from [config/defaults/naming.yaml](config/defaults/naming.yaml).

### Choosing a mode

Three modes, selected at fetch from declared data — never at the model's discretion:

| Mode | Pipeline | Human gates |
|---|---|---|
| **full** | Everything: scoped plan, adversarial panel, proven-red TDD, harden, security | Plan, implementation, pre-PR (+ security when findings meet the threshold) |
| **lean** | Identical rigor to full | **Pre-PR only** on the happy path — the plan gate fires only if the panel exhausts its rounds; no implementation gate |
| **quick** | Short path: no plan step, no red-proof machinery; confirms the target repo first in a multi-repo workspace | Pre-PR only |

Pick one per work item with a **`Mode:` hint on its own line in the work item's description** (the story file for `local-markdown`, the issue body for github/gitlab/…):

```
## Description
Mode: lean
Refactor the notification service onto the new queue client.
```

The hint is matched case-insensitively (`Mode: lean`, `mode: lean`). Or set the workspace-wide fallback for items with no hint — in [config/defaults/workflow.yaml](config/defaults/workflow.yaml), overridable via `/workspace-config`:

```yaml
default_mode: lean    # shipped default: full
```

Precedence is **full > quick > lean > default**, resolved by the classifier and mapped to a mode by the manifest alone:

- **`Mode: full`** always wins — the per-item escape *upward* out of a `default_mode: lean` workspace. An item asking for more gating always gets it.
- **`Mode: quick`** needs eligibility: the hint *and* no risk keyword. A keyword-disqualified quick hint falls to lean/default — never past lean, because the keywords guard *skipping the plan machinery*, which lean keeps.
- **`default_mode: quick` is not a thing** — quick requires per-item eligibility, never a standing workspace choice.

`harness fetch` reports the resolved `mode`, and the `fetched` event records the `mode_verdict` plus why (`explicitly hinted` vs `workspace default_mode`), so mode selection is auditable rather than inferred.

### Lean mode — gating by exception

Lean keeps **all** of full's rigor — confirmed repo scope, the adversarial plan panel, proven-red TDD, harden, the security scan — and loosens only *when a human is interrupted*. On the happy path you are asked exactly once, right before the PR is opened:

- **The plan gate self-skips** when the plan-review panel approved the plan within its round budget, and **fires only on bound exhaustion** — the case where the machines couldn't converge and the decision is genuinely yours. The skip is ledgered as a `gate-skipped` event, so "skipped by predicate" is never confused with "never evaluated".
- **The implementation gate is absent** — deliberately: task completion already refuses without a hook-captured reviewer `APPROVED` per task, and the holistic pre-PR review plus its gate cover the aggregate.
- **`approve-pre-pr` remains unconditional** — the one guaranteed stop.

The skip predicate reads an outcome the **engine** records from the same verdict ledger that legalizes the step's exits — the orchestrator never writes it (the `artifact` verb refuses the name), so a drifting agent cannot skip you past a plan the panel rejected. Exhaustion also *latches*: once the panel burns its budget, a later approval can't retroactively re-open the skip.

The trade to accept knowingly: on a lean happy path, the panel's approval is the plan's effective ratification — your scope confirmation and the plan's test-intents are never gate-ratified. That's the point of choosing it per item (or per workspace), and `Mode: full` buys the gates back on anything risky.

### Quick mode — with a mechanical escape hatch

Trivial changes (explicit `Mode: quick` hint in the work item, no risk keywords) run the short pipeline: no plan step, no red-proof machinery, one gate. Because eligibility was classified *before code existed*, `quick-recheck` re-examines the **real diff** after develop: touching disqualifying patterns (security/auth/migration/API paths) or exceeding size caps (80 changed lines / 5 files, configurable) triggers the declared escalation edge into full mode's security step — forcibly, not at the model's discretion.

Skipping the plan step means skipping the step that decides **which repo** the work belongs in. `fetch` seeds its single task with `repos[0]` — a positional default, no content analysis — and in full/lean `plan-register` replaces that wholesale. Quick has no plan-register, so in a **multi-repo workspace** it stops at `confirm-repo`: the orchestrator proposes one target repo from the repo-map indexes and the work item's own content, the human confirms, and `harness confirm-repo` ratifies it before `preflight` cuts a branch anywhere. The cursor has no legal move until it does. A single-repo workspace has nothing to ratify, so the step is skipped by its declared predicate and quick stays zero-ceremony.

### The security step

`security-scan` runs each repo's configured `scan_cmd`, parses severities via the configured regex, and records the aggregate maximum. The gate fires only at or above `security.gate_threshold` (default `medium`), with three dispositions: **fix-now** (routes back to develop), **waive** (recorded, pipeline continues), **defer** (pipeline continues *and* a follow-up work item is created via the provider, with paired ledger events so an in-flight, completed, or dropped deferral are distinguishable states).

### Multi-repo runs

A work item spanning repos gets per-repo task lanes; cross-repo API contracts are declared in the plan and mechanically re-checked at reconcile time (`reconcile-contracts` greps each declared fragment across the other repos' sources, excluding test paths and the committed `ai/**` mirrors; `{param}` tokens in `http` route fragments match any one path segment, so each repo may name a route parameter its own way without false drift). Sync points fail closed: the cursor cannot leave `develop` while any task in any lane is non-terminal.

### The repo map

`/init-workspace` (optionally) and `/repo-map-refresh` generate a tiered codebase map under `.claude/context/repo-map/` following a declared content contract: per repo, an `index.md` (purpose, stack, module inventory, cross-repo edges — the tier intake proposes target repos from), `areas/*` detail files (each loadable alone), and a `conventions.md` (observed naming/layering/error-handling/test patterns, each with a cited example — the tier plan-review checks plans against). Stamped with the SHA it was generated at and flagged stale after 50 commits (configurable); a map with no content cannot be stamped, so an empty generation can never be certified fresh. The planner's intake and plan instructions point it at the map directly (index first, then only the areas the story touches) instead of re-deriving the codebase from scratch every run. Auto-generated only, never hand-maintained: corrections go through regeneration.

### Scoped planning

Intake ends by proposing the story's **target repos** with evidence from the map indexes; you confirm the set, and the orchestrator records it via `harness scope-register` — mechanical scope, not a convention: `plan-register` refuses any task outside it (and refuses outright when no scope was ever confirmed), re-registering a scope that would strand already-registered tasks is refused, the registration verbs are guard-blocked from subagent shapes, and widening mid-plan goes back through you. The plan itself must carry per-task file-touch manifests, verify commands, size budgets, an AC-traceability table over intake's numbered acceptance criteria, and an explicit out-of-scope section — the material both the independent plan-review and the developer run on.

## Guardrail Hooks

One Python entry point ([hooks/guards.py](hooks/guards.py)) handles every event, registered in [hooks/hooks.json](hooks/hooks.json). Guards scope themselves to workspaces with a live harness run (resolved from `CLAUDE_PROJECT_DIR` first, so a drifted shell `cwd` can't dodge them) — with one exception: the raw-git block is standing rather than run-scoped, active for the life of any workspace that has completed `/init-workspace` (same `CLAUDE_PROJECT_DIR`-first resolution, checking for the bootstrap marker instead of a live run) regardless of whether a run currently exists. It is still not global — a session that has never run `/init-workspace` sees ordinary git. Two documented residuals: a session rooted directly in a repo registered to a *sibling* workspace, rather than the workspace itself, isn't recognized as belonging to it (nothing today points from a registered repo back to the workspace that owns it); and the bootstrap marker itself (`.claude/context/overrides.yaml`) is an ordinary, non-chain-sealed config file — a direct edit stripping it can silently turn the block back off, a capability the pre-change unconditional block never had. Both accepted deliberately rather than closed in this pass — see `_is_harness_workspace`'s docstring.

| Guard | Event · Matcher | What it enforces |
|---|---|---|
| bash | PreToolUse · Bash | Blocks raw history-mutating git inside any workspace that has completed `/init-workspace` and points to the owned verbs. Role-aware shell-write analysis (quote-masked shape matching on redirects, `tee`, `cp`/`mv`, in-place editors): reviewer writes only to literal `/tmp` paths; developer confined to its worktree; secret/evidence files unreadable. |
| write | PreToolUse · Write/Edit/MultiEdit/NotebookEdit | Path confinement per shape (planner → `ai/<run>/` + `.claude/context/`; developer → its worktree with the pre-red test-first lock; reviewer → nothing) plus sensitive-file patterns. |
| spawn | PreToolUse · Agent/Task | Only the spawn-set the manifest declares for the current cursor is legal — shape *and* `harness-mode:` header both checked; out-of-run spawns (e.g. repo-map generation) must be declared in [pipeline/surfaces.yaml](pipeline/surfaces.yaml). Fail-closed. |
| skill | PreToolUse · Skill | USER-ENTRY skills (`/dev-workflow`, `/init-workspace`, …) refuse invocation from subagents or autonomous triggering — they run only when you ran them. |
| read | PreToolUse · Read/Grep | Red-proofs are readable by harness shapes only via `harness show-redproof` (chain-verified) — a raw `.redproof/` read skips integrity verification and is blocked. |
| prompt capture | UserPromptSubmit | Verbatim capture of your replies into `human-input.ndjson` — the only evidence `gate --decide` accepts. |
| verdict capture | PostToolUse · Agent/Task | The authoritative writer of `reviews.ndjson` (reviewer verdicts) and the missing-status-block / status-block-malformed events — anchored here because this payload deterministically carries both the spawn prompt and the agent's final reply. |
| stop capture | SubagentStop | Per-invocation token accounting into `tokens.ndjson`; secondary status-block capture. |

Every guard's fail-open/fail-closed policy is chosen deliberately and tested: recognised violations always block; the spawn guard is fail-closed even on ambiguity.

## The `harness` CLI

All ~50 owned verbs run through the wrapper `${CLAUDE_PLUGIN_ROOT}/bin/harness` (resolves the plugin venv in either OS layout, falls back to system `python3`/`python`). It runs on macOS, Linux, and Windows — on Windows it executes under Git Bash, with `bin/harness.cmd` as the cmd.exe sibling. Agents call it; you rarely need to — except `abort`.

| Group | Verbs |
|---|---|
| Workspace setup | `init` · `discover` · `ensure-default-branch` · `init-verify` · `init-section` · `init-finalize` · `add-repo` · `migrate-detect` · `migrate-extract` · `resolve-model` · `resolve-coverage-cmd` |
| Pipeline steps | `fetch` · `scope-register` · `confirm-repo` · `base-check` · `preflight` · `plan-register` · `env-check` · `quick-recheck` · `security-scan` · `reconcile-contracts` · `create-pr` · `fetch-pr-comments` · `reconcile` · `write-back` · `metrics` |
| State & evidence | `bootstrap` · `cursor` · `task` · `artifact` · `gate` · `stall` · `log-event` · `verify` · `show` · `status` · `abort` · `complete` · `reseal` |
| TDD proof | `verify-red` (and `--revise`) · `show-redproof` |
| Git (owned) | `worktree-add` · `worktree-remove` · `commit` · `merge-task` · `update-base` · `sync-branch` · `push` · `publish-mirror` |
| Providers & misc | `provider` · `provider-normalize` · `validate-mermaid` · `repo-map-check` · `repo-map-stamp` |

Exit codes: `0` ok · `1` refused (read the JSON `error`) · `2` usage error · `3` integrity violation (a sealed file changed out-of-band — recover with `harness reseal` after review).

## Providers

Work-item integrations are code modules behind one interface — callers name an *operation* (`work_item.fetch`, `work_item.create`, `create-pr`, …), never a provider — and every module must pass the shared contract test in [tests/test_providers.py](tests/test_providers.py). Adding a provider = implementing the interface.

| Provider | Transport | Needs |
|---|---|---|
| `local-markdown` | files | Nothing — work items are `.md` files in a configured stories directory |
| `github` | CLI (`gh`) | `gh auth login` |
| `gitlab` | CLI (`glab`) | `glab auth login` |
| `ado` | CLI (`az boards`) | `az login` + DevOps extension |
| `ado-mcp` | MCP | Azure DevOps MCP server connected |
| `jira` | MCP | Jira MCP server connected |
| `zoho` | MCP | Zoho MCP server connected |

`local-markdown` story files may be named `<id>.md` or `<id>-<descriptive-slug>.md` (`US-42.md`, `US-42-add-multiply.md`), and both spellings resolve for every operation. A slug-named file claims its id in its `# <id>: <title>` heading — that heading is what makes `US-42` resolve `US-42-add-multiply.md`, so keep it when you edit the file. Sibling notes that declare no id of their own (`US-42-readiness.md` and the rest of `/story-workflow`'s output) sit in the same directory harmlessly. Two files *claiming the same id* is refused rather than guessed at, whichever is named which: a status write-back would otherwise land in an arbitrary one of them.

CLI/file-transport providers execute inside the harness process. MCP-transport providers can't be script-called: the module declares a tool **mapping**, the model invokes the MCP tool, and pipes the raw result to `harness fetch --from-raw` for the same shared normalize + bootstrap path.

Milestone status write-back is **best-effort** on script-callable transports: a provider that refuses the transition is recorded as a flagged `write-back-failed` event and the run continues, rather than failing a step whose work already landed. A later milestone that does land clears the flag (`write-back-succeeded`), so a tracker that was briefly down doesn't leave a permanent mark on a run that ended in sync. Two things are deliberately *not* best-effort: the MCP carve-out, where the refusal is the signal to invoke the mapped tool yourself and pass `reconcile --skip-transition`, and a provider that declares no transition support at all, which is a capability gap rather than a runtime refusal.

## Project Structure

```
ai-sdlc-harness/
├── .claude-plugin/plugin.json   # plugin manifest
├── pipeline/
│   ├── manifest.yaml            # THE pipeline source of truth: steps, modes, gates, escalations
│   ├── task-fsm.yaml            # legal task-status transitions
│   └── surfaces.yaml            # subagent shapes, write surfaces, out-of-run spawns
├── config/defaults/             # shipped knobs (workflow, naming, quick-mode, review-policy,
│                                #   status-mapping, subagent-models) — overrides live in the
│                                #   workspace's .claude/context/
├── harness/                     # the Python core behind every owned verb
│   ├── cli.py                   # verb surface
│   ├── workflow.py              # step implementations
│   ├── state.py · transitions.py · gates.py · chain.py   # sealed state + FSM + gate parser
│   ├── gitops.py                # owned git machinery
│   ├── migrate.py               # v2.x workspace adoption (the fork seam)
│   └── providers/               # code-modular provider adapters
├── hooks/
│   ├── hooks.json               # hook registrations
│   └── guards.py                # all guard + capture logic (one entry point)
├── agents/                      # the 3 shapes: planner.md, developer.md, reviewer.md
├── skills/
│   ├── dev-workflow/            # thin orchestrator walker + per-step instruction files
│   └── init-workspace/ · add-repo/ · migrate-workspace/ · workspace-config/ · workflow-status/ · repo-map-refresh/
├── bin/harness                  # wrapper script resolving the plugin venv (+ harness.cmd for Windows)
├── tools/                       # meta-tooling: line-budget checker, sandbox workspace generators
└── tests/                       # 878 stdlib-unittest tests
```

Workspace artifacts — `ai/<date>-<id>/` and `.claude/context/` — are generated inside *your* working directory by `/init-workspace` and the pipeline. They never live inside this plugin repo.

## Development

Working on the harness itself? Run it from a clone instead of the installed copy — `--plugin-dir` is per-session, so it never disturbs an installed version:

```sh
git clone https://github.com/MostAshraf/ai-sdlc-harness.git   # or git@github.com:MostAshraf/ai-sdlc-harness.git
claude --plugin-dir ./ai-sdlc-harness
```

Requires Python 3.10+ and PyYAML. CI runs the suite on Linux, macOS, and Windows — all three lanes enforcing.

```sh
python3 -m venv .venv && .venv/bin/pip install pyyaml
.venv/bin/python -m harness.schema          # validate all declared data against the fixed vocabulary
.venv/bin/python tools/budget_check.py      # line budget + duplication sweep
.venv/bin/python -m unittest discover -s tests
```

On Windows the venv lands its interpreter under `Scripts\` instead of `bin/`:

```powershell
python -m venv .venv; .venv\Scripts\pip install pyyaml
.venv\Scripts\python -m unittest discover -s tests
```

The test suite (989 tests) covers the state engine, gate grammar, guard behavior (via subprocess against real payloads), provider contracts, git machinery against real temp repos, breadth walks of the pipeline modes, composability probes (a scratch mode and scratch step must validate and walk with zero Python changes), Windows-only guard path shapes, and meta-checks (invocation consistency, declared-data schema, line budgets). See [CHANGELOG.md](CHANGELOG.md) for release history.

## FAQ

**Can I resume after closing the terminal?** Yes — `state.yaml` *is* the resume point. Start a new session in the same workspace, run `/workflow-status` to see where you are, then `/dev-workflow <id>`: it detects the live run and offers **Resume or Abort** — never clobbers.

**How do I abandon a run?** `harness abort --run <run> --reason "<why>"` (via the plugin's `bin/harness`). Terminal: sweeps worktrees, keeps the full audit trail, frees the work item. A same-day re-run gets a fresh `-2` suffix slot; nothing is deleted.

**What does exit code 3 mean?** A sealed file (`state.yaml`, a ledger, a red-proof) changed outside the owned verbs. Every verb refuses until you inspect the diff and run `harness reseal` — deliberately loud, because silent tolerance would make the audit trail worthless.

**Why can't Claude run `git commit` in my harness workspace?** The guard can't distinguish a harness commit from any other, so it blocks the whole verb for the life of any workspace that has completed `/init-workspace` — regardless of whether a run is currently active. Your own terminal outside Claude Code is unaffected, and a project that has never run `/init-workspace` is unaffected too. If you need raw git inside a bootstrapped workspace, disable the plugin for that session.

**How do I stop it interrupting me at every gate?** Run the item in **lean mode**: put `Mode: lean` on its own line in the work item's description, or set `default_mode: lean` in your workspace config to make it the standing choice for unhinted items. Lean keeps every guarantee full has (scoped plan, change_type-scaled adversarial panel, proven-red TDD, security) and gates by exception instead — the plan gate fires only if the plan-review panel exhausts its round budget, there's no implementation gate, and the pre-PR gate stays. Happy path: one stop, right before the PR. `Mode: full` on a single item buys the full gates back — see [Choosing a mode](#choosing-a-mode).

**Which mode will my work item run in?** Precedence is **full > quick > lean > default**: an explicit `Mode: full` hint always wins, `Mode: quick` needs the hint *and* no risk keyword, `Mode: lean` (or `default_mode: lean`) is next, and anything else lands on full. `harness fetch` reports the resolved `mode`, and the `fetched` event in `events.ndjson` records the `mode_verdict` and the reason it was chosen — so it's auditable, never a guess.

**What if the reviewer keeps rejecting?** Rounds are bounded (`review_rounds.max`, default 5). Beyond that the rework transition is refused and you're escalated — persistent rejection signals plan drift, not code drift.

**What if the plan-review panel keeps rejecting?** Same bound (`review_rounds.max`), applied per plan cycle: each `CHANGES_REQUESTED` verdict forces a revision loop back to the planner. Once the budget is exhausted, the plan reaches you at the plan gate **with the failing review attached** — in lean mode, that's exactly when its otherwise-skipped gate fires. Never an auto-approval, never a deadlock.

**What if an agent stalls or returns garbage?** A missing/invalid status block is detected mechanically; the orchestrator re-invokes with a continuation prompt (bounded, default 2), then escalates to you (default 3). It never acts on the agent's behalf. One carve-out: a reviewer reply whose engine-read verdict was captured despite a missing block is recorded (`status-block-malformed`, flagged) and the run proceeds on the ledger — no re-spawn to re-derive a verdict it already holds.

**What if a test is genuinely wrong after it was proven red?** `harness verify-red --revise --reason "<why>"` — the revision is sealed and flagged in the events ledger, reviewer-visible. There is no silent path.

**What happens on a security finding?** Below the threshold: recorded, pipeline continues. At/above: the gate fires with **fix-now** (back to develop), **waive** (recorded), or **defer** — defer also creates a follow-up work item through your provider, with paired events so a dropped deferral is detectable.

**How do I trust what a run did?** Read the ledgers. `events.ndjson` records every deviation (test revisions, gate rejections, blocked actions, stalls, skipped gates), `tokens.ndjson` the real spend, `reviews.ndjson` the verdicts, and the chained `state.yaml` the decisions — all mirrored onto the feature branch at gate crossings (minus your raw replies). `/workflow-status` and `harness metrics` are deterministic projections of the same files.

**I'm coming from ai-sdlc-harness v2.x — can I migrate my workspace?** Yes — run `/migrate-workspace` in it. Config carries over as per-section proposals you confirm (provider, repos, test commands, stories directory), applied through the same verification gate as a fresh setup; the old markdown configs are archived to `.claude/context/legacy-2.1/` with a migration report. Run history is never converted — v3.0 state is sealed evidence v2.x never produced — so old `ai/` run dirs stay in place as readable archives, and in-flight v2.x stories finish on v2.x (or are abandoned) before you switch. Existing `local-markdown` stories work in place, including their `> Status:` blockquotes.
