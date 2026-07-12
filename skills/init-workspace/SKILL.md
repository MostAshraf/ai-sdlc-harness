---
name: init-workspace
description: >
  One-time workspace setup for the ai-sdlc-harness pipeline. USER-ENTRY and
  HUMAN-ONLY — invoke only when the user explicitly runs /init-workspace;
  never autonomously, never from a subagent (guard-enforced).
---

# init-workspace — the interview (M7)

Human-only: the user's consent point for the whole workspace. Every command
below is `${CLAUDE_PLUGIN_ROOT}/bin/harness <verb> …` — always the full
path; a bare `harness` is not on PATH, and shell variables set in one Bash
call do not persist to the next. Re-running refreshes **one section at a
time** (`init-section`), never a full-nuke.

## 0 · Environment bootstrap (do this FIRST)

The harness needs PyYAML; system pythons are often externally managed
(PEP 668), so the plugin owns a venv that `bin/harness` resolves
automatically on every future call:

```
PY="${CLAUDE_PLUGIN_ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="${CLAUDE_PLUGIN_ROOT}/.venv/Scripts/python.exe"
"$PY" -c "import yaml" 2>/dev/null || {
  SYS="$(command -v python3 || command -v python)" &&
  "$SYS" -m venv "${CLAUDE_PLUGIN_ROOT}/.venv" &&
  PY="${CLAUDE_PLUGIN_ROOT}/.venv/bin/python" &&
  { [ -x "$PY" ] || PY="${CLAUDE_PLUGIN_ROOT}/.venv/Scripts/python.exe"; } &&
  "$PY" -m pip install --quiet pyyaml; }
```

One snippet for every OS: the Bash tool is Git Bash on Windows, so this
stays POSIX shell there too — the two `.venv` probes cover the `bin/` (POSIX)
vs `Scripts/` (Windows) venv layouts, and the `python3 || python` fallback
covers hosts where only one spelling exists. Until this step runs,
`bin/harness` itself still works (it falls back to the same system
interpreter probe, which is what fails on a PyYAML-less system — that's why
this step exists), and the spawn/skill guards degrade open with a one-line
notice rather than erroring — expected pre-setup behavior, not a bug to chase.

## 1 · Must-provide (no defaults — ask)

- **Work-item provider**: local-markdown / github / gitlab / ado (CLI) /
  ado-mcp / jira / zoho (MCP — walk the user through the model-in-the-loop MCP
  integration probe). ADO is available on either transport (`ado` = `az boards`,
  `ado-mcp` = Azure DevOps MCP server). Plus its specifics (stories dir,
  `github_repo`, `ado_org`, `ado_project`, …).
- **Git provider**: local / github / gitlab / ado (CLI) / ado-mcp (MCP).
  If the user didn't state one, `local` is the sanctioned inference ONLY
  when every registered repo has no remote (`git remote` empty) — say so
  in one line rather than asking; any repo with a remote → ask.
- **Repos**: `name=path` per target repo.

Every `init-section` write is merged straight into the flat config by its
top-level keys, so `provider`, `repos`, and `language` payloads must be
**self-nested** under their own section key:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness init-section --section provider --json \
  '{"provider": {"work_item": "local-markdown", "git": "local", "stories_dir": "stories"}}'
${CLAUDE_PLUGIN_ROOT}/bin/harness init-section --section repos --json \
  '{"repos": {"backend": "/path/to/backend", "frontend": "/path/to/frontend"}}'
```

`overrides` is the one exception on both counts: it's a flat grab-bag of
top-level config keys (`status_mapping`, `subagent_models`, `quick_mode`,
…), never self-nested under an `"overrides"` key, and unlike
`provider`/`repos`/`language` (each write replaces the whole file — always
send the complete current set) its writes **merge**, so separate
`--section overrides` calls for different settings accumulate rather than
clobbering each other. See step 3.

## 2 · Discovered, then confirmed

Run `${CLAUDE_PLUGIN_ROOT}/bin/harness discover --repo <path>` per repo. `discover` first
ensures the repo is clean and on its default branch (`ensure_default_branch`
— the same reusable precondition `preflight` uses later): a dirty repo, or
one mid-rebase/merge, refuses with a clear error — surface it to the user
(never auto-stash/discard/continue) — and a clean repo on a different
branch is switched, reported back in `branch_check` so the interview can
tell the user it happened. If the guessed default branch doesn't exist
locally (no resolvable `origin/HEAD`), pass `--branch <name>` explicitly —
note this only catches a *nonexistent* guess; a repo with no `origin` and
a stray local branch that happens to be named `main` cannot be told apart
from a genuine one, so confirm the branch name with the user for any repo
without a resolvable `origin/HEAD`.
**Known risk:** re-running this against a repo that already has an active
`/dev-workflow` run in progress can switch that run's feature-branch
checkout back to default — avoid re-running discovery for a repo with
in-flight work. Present the proposals (language, `test_cmd`, default
branch) as defaults-to-confirm. A `monorepo_split` proposal means this "one
repo" is actually several logical repos sharing one `.git` at the physical
root — **this isn't yet representable as separate registered repos**
(`init-verify`'s `repo:<name>` check requires a `.git` directly under the
registered path, which no subtree has, so registering subtrees separately
leaves every one of them permanently failing verification with no
available fix). Tell the user this is a known gap; register the repo once,
at its physical root.
**Language-config is per repo**, under `language.repos`, keyed by the same
names used in `--section repos` (a sub-key, not a sibling of the global
`test_paths`/`test_closure` settings, so a repo name can never collide with
those) — confirm each repo's own `test_cmd` by running it, never collapse
differing repos onto one command. **`coverage_cmd` gets the same
treatment**: discover proposes one only on repo evidence (a `coverage`
script, jest/vitest+provider, jacoco in the pom) — confirm it by running
it. No proposal → ask the user for one (the harden step consumes it and
never improvises); an explicit skip is a valid answer, recorded by simply
omitting the key — tell the user harden will re-ask at run time. Write the
whole set in one `--section language` call, e.g. `{"language": {"repos":
{"backend": {"test_cmd": "sh mvnw -q test", "coverage_cmd": "sh mvnw -q
test jacoco:report"}, "frontend": {"test_cmd": "npm test"}}}}`.

## 3 · Choose-or-default (offer "default" explicitly, every time)

Status-mapping override (provider defaults usually suffice), change-types +
naming templates, `subagent_models` (default all `inherit`), quick-mode
thresholds/keywords, repo-map staleness N, review-policy team rules,
`security.scan_cmd` (if a scanner is configured, it's per-repo-keyed the
same way `language` is — no scanner configured stays informational-only).
Only what the user changes goes in `--section overrides` — shipped
defaults cover the rest. Unlike `provider`/`repos`/`language`, this payload
is **flat, not self-nested** (these are top-level config keys in their own
right), e.g. `--section overrides --json '{"quick_mode": {"loc_max":
50}}'` — never `{"overrides": {...}}`. Each call deep-merges into whatever is
already there, so it's fine to write these one setting at a time as the
user decides them; there's no way to *unset* a previously-written override
through this verb though — that needs a direct edit to `overrides.yaml`.

## 4 · Verify (a real gate) + finish

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness init-verify` — every check must pass
   (or be `manual` with the user's explicit acknowledgment for MCP
   providers). Failures show remediation; fix and re-run. **Do not proceed
   on failures.**
2. `${CLAUDE_PLUGIN_ROOT}/bin/harness init-finalize` — writes the permissions
   allowlist and the bootstrap marker (section writes alone do not write
   either of these). It re-runs the same verify gate itself and refuses
   (exit 1) if any check still fails, so it can't mark a half-configured
   workspace bootstrapped even if step 1 above was skipped by mistake.
   Confirm `.claude/settings.json` merged cleanly (non-destructive).
3. **Repo-map**: offer to generate one now per repo, following
   `/repo-map-refresh`'s step 2 exactly (subagent_type-guessing warning,
   `harness-mode: repo-map` header, and stamp-it-yourself rule all live
   there — this file doesn't keep its own copy).
4. Tell the user: `/dev-workflow <work-item-id>` is ready.

## 5 · Adding a repo after the fact

Use the dedicated `/add-repo` skill — it's the one place this procedure
(discover → confirm → register → verify → finalize) is maintained; don't
hand-roll it here too.
