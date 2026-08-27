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

**Resolve the plugin root.** Under native Qwen Code (no Claude-plugin
conversion), `${CLAUDE_PLUGIN_ROOT}` is not exported until step 6 writes
`.qwen/settings.json`, and even then only next session. Each Bash call is a
fresh subprocess (an `export` won't persist), so the probe **prints** the
resolved path for you to substitute textually; if the var is already set
(Claude Code, converted install) the probe passes it through unchanged:

**Folder-trust caveat (Qwen Code):** project-level `.qwen/settings.json` is
silently ignored unless the folder is trusted (trusted-folders is OFF by
default, measured on 0.22.2) — the `CLAUDE_PLUGIN_ROOT` export and the
permission mirror don't apply, so subagents hit the prompts the allowlist
was meant to pre-clear. The pipeline's hooks are unaffected (they ship with
the extension). Remedy: trust the folder (Qwen `/permissions` → Trust this
folder, then restart) or export `CLAUDE_PLUGIN_ROOT` in the launching shell.

```
# Resolve the plugin root and print it (native Qwen first-run fallback)
R="${CLAUDE_PLUGIN_ROOT:-}"
[ -z "$R" ] && for d in "$HOME/.qwen/extensions/ai-sdlc-harness/bin/harness" \
  "$HOME/.qwen/extensions/ai-sdlc-harness/.qwen-extension-install.json"; do
  [ -e "$d" ] && { case "$d" in *install.json) R=$(python3 -c \
    "import json,sys;print(json.load(open(sys.argv[1])).get('source',''))" "$d");; \
    *) R=$(dirname "$(dirname "$d")");; esac; [ -x "$R/bin/harness" ] && break; }; done
[ -x "$R/bin/harness" ] || { echo "ERROR: set CLAUDE_PLUGIN_ROOT to the dir with bin/harness" >&2; exit 1; }
echo "PLUGIN_ROOT=$R"
```

**Use the printed `PLUGIN_ROOT=<path>` in place of every
`${CLAUDE_PLUGIN_ROOT}` below** for this skill run.

The harness needs PyYAML; system pythons are often externally managed (PEP
668), so the plugin owns a venv that `bin/harness` resolves automatically
thereafter. Bootstrap it through ONE dual-clause command — same shape as
`hooks/run-guard`'s launcher pair, and for the same reason: the platform
picks the interpreting shell, not us. Claude Code's Bash tool always runs
bash (Git Bash on Windows); Qwen Code's `run_shell_command` shares hooks'
shell-selection logic and falls back to cmd.exe on Windows outside an
MSYS-flavored terminal, where a `NAME="$(command …)"`-shaped POSIX
assignment is unparseable (cmd treats `=` as an argument delimiter):

```
exec "${CLAUDE_PLUGIN_ROOT}/bin/setup-venv" || ${CLAUDE_PLUGIN_ROOT}/bin/setup-venv
```

`exec` replaces bash with the launcher, so the second clause only ever runs
under cmd.exe (where `exec` fails fast and `||` falls through to the unquoted
path, PATHEXT-resolved to `setup-venv.cmd`). Both halves probe both `.venv`
layouts (`bin/` POSIX, `Scripts/` Windows) and the same `python3` →
`python` fallback, then exit once PyYAML is importable. Until this step runs,
`bin/harness` still works (falling back to the same system-interpreter probe,
which is what fails on a PyYAML-less system — hence this step), and the
spawn/skill guards degrade open with a one-line notice: expected pre-setup
behavior, not a bug to chase.

## 1 · Must-provide (no defaults — ask)

- **Work-item provider**: local-markdown / github / github-projects / gitlab /
  ado (CLI) / ado-mcp / jira / zoho (MCP — walk the user through the
  model-in-the-loop probe). Two come in pairs: `ado` = `az boards` vs
  `ado-mcp`; `github` = repo issues vs `github-projects` = a Projects v2 board.
  Plus specifics (stories dir, `github_repo`, `github_project`/`_owner`, …).
- **Git provider**: local / github / gitlab / ado (CLI) / ado-mcp (MCP).
  If the user didn't state one, `local` is the sanctioned inference ONLY
  when every registered repo has no remote (`git remote` empty) — say so
  in one line rather than asking; any repo with a remote → ask.
- **Repos**: `name=path` per target repo.

Every `init-section` write is merged straight into the flat config by its
top-level keys, so `provider`, `repos`, and `language` payloads must be
**self-nested** under their own section key. If any `init-section` result
carries a `notice` key, relay its text to the user verbatim:

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
send the complete current set) its writes **merge**, so separate `--section
overrides` calls accumulate rather than clobbering each other. See step 3.

## 2 · Discovered, then confirmed

Run `${CLAUDE_PLUGIN_ROOT}/bin/harness discover --repo <path>` per repo. It first
ensures the repo is clean and on its default branch (`ensure_default_branch` — the
precondition `preflight` reuses later): a dirty or mid-rebase/merge repo refuses
with a clear error — surface it to the user (never auto-stash/discard/continue)
— and a clean repo on a different branch is switched, reported in `branch_check`
so you can tell the user it happened. If the guessed default branch doesn't exist
locally (no resolvable `origin/HEAD`), pass `--branch <name>` explicitly — but
that only catches a *nonexistent* guess: a repo with no `origin` and a stray local
branch named `main` is indistinguishable from a genuine one, so confirm the branch
name with the user for any repo without one.
**Known risk:** re-running this against a repo with an active `/dev-workflow`
run switches that run's feature-branch checkout back to default — and the
switch flips the whole checkout, so avoid discovery on ANY path inside a
checkout with in-flight work, not just the exact registered path. Present the proposals (language, `test_cmd`, default
branch) as defaults-to-confirm. A `monorepo_split` proposal means this "one repo" is several
logical repos sharing one `.git` — **register each proposed root as its own repo**: path
`<checkout>/<root>` (checkout-relative; `.` = the checkout), name `<repo>-<root>`, and confirm
each `test_cmd` FROM that subtree — commands run with the registered path as cwd. `init-verify`
passes any path inside a git work tree, naming the enclosing checkout: a pass, not a warning.
What the shared `.git` costs: tasks still isolate (worktrees cut from the physical checkout,
staging subtree-bounded, direct-branch fallback refused), but outside them both repos ride one
branch, and a parent root holds its child — only review keeps a parent's task out of it.
**Language-config is per repo**, under `language.repos`, keyed by the same
names used in `--section repos` (a sub-key, not a sibling of the global
`test_paths`/`test_closure` settings, so a repo name can never collide with
those) — confirm each repo's own `test_cmd` by running it, never collapse
differing repos onto one command. **A proposal may carry no `test_cmd`** —
no single command covers that root (a .NET root with no solution file, or
two side by side). Ask the user; never synthesise one. Nothing catches a bad
guess later: `init-verify` gates on invocability only, so a command that
cannot even locate its project still reports `pass`, and the first
`verify-red` then seals a red-proof over a build error. **`coverage_cmd` gets the same
treatment**: discover proposes one only on repo evidence (a `coverage`
script, jest/vitest+provider, jacoco in the pom) — confirm it by running
it. No proposal → ask the user for one (the harden step consumes it and
never improvises); an explicit skip is a valid answer, recorded by simply
omitting the key — tell the user harden will re-ask at run time. Write the
whole set in one `--section language` call, e.g. `{"language": {"repos":
{"backend": {"test_cmd": "sh mvnw -q test", "coverage_cmd": "sh mvnw -q
test jacoco:report"}, "frontend": {"test_cmd": "npm test"}}}}`.
A repo carrying a **known-failing spec unrelated to any run** can declare a
`quarantine` sibling — `{"exclude_template": "--exclude {test}", "tests":
[{"test": "…", "reason": "…", "since": "YYYY-MM-DD"}]}` — applied to
the test command *and* to coverage (give coverage its own
`coverage_exclude_template` when it's a different tool). `reason` and `since`
are required; the template is the FLAG only, must contain `{test}`, and is
runner-specific — never guessed. Flags are APPENDED, so the command must be
a single one (no unquoted `&&`/`|`/`;`) and the flag must reach the runner:
an `npm test` wrapper swallows `--exclude` unless the template passes it
through (`-- --exclude {test}`), so prefer invoking the runner directly. Test
paths are repo-relative, forward-slashed, no `./` prefix. Each run logs one
flagged event naming the exclusions. Don't offer this proactively at setup;
it's for a failure the user already has.

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
