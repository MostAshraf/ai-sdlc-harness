#!/usr/bin/env python3
"""Plugin guard layer (design.md piece 3 + RC1/RC4 + invocation control).

One dispatcher, selected by argv[1] — registered in hooks/hooks.json.
Exit 0 = allow · exit 2 = block (stderr is the redirect-to-`harness` message).

Per-guard policy (declared, a "keeping" from the original):
  bash            fail-open on unparseable payload — the HMAC chain (RC4) is
                  the guarantee for authority files; this guard is fast-fail.
                  Its raw-commit/merge/rebase/.../push block (GIT_VERB_RE) is
                  a STANDING, workspace-scoped invocation rule, not a
                  run-state guard: it applies for the life of a harness
                  workspace — from the moment `/init-workspace` completes,
                  regardless of whether any `ai/<run>/` currently exists —
                  unlike `spawn` below, there is no "no run yet" carve-out
                  for it (adversarial-review finding: previously
                  undocumented, easy to mistake for a run-scoped check like
                  the others in this list). It is NOT session- or repo-wide
                  beyond that: `_is_harness_workspace` gates it on the
                  `/init-workspace` bootstrap marker, so a session touching
                  an unrelated, never-initialized repo sees raw git
                  untouched — see `_is_harness_workspace` for the one
                  documented residual this leaves open.
  write           fail-open on unparseable payload; fail-closed on authority
                  paths (they are never legal via tools).
  spawn           FAIL-CLOSED: no run -> no harness-shape spawns beyond the
                  declared out_of_run exceptions; integrity failure blocks
                  spawn-legality from JUST the corrupt run, not the rest of
                  the workspace (harness reseal is the recovery verb).
  skill           fail-closed for user-entry skills from subagent context.
  user-prompt     never blocks (capture only).
  subagent-stop   never blocks (capture + stall detection feed events ledger).

If PyYAML is missing, guards exit non-zero with a visible remediation error
(they do not silently disable); init-workspace verifies the dependency up
front, and the HMAC chain (RC4) still detects authority-file tampering even
with guards down — defense in depth, guard = fast-fail, chain = guarantee.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

from harness import chain, ndjson  # noqa: E402  (stdlib-only modules)


class YamlMissing(Exception):
    pass


def load_yaml(path: Path):
    """Lazy YAML — only the spawn/skill guards need it. The bash/write/capture
    guards are pure regex+payload and must keep working (and blocking!) on a
    Python without PyYAML, e.g. macOS system python3 before setup."""
    try:
        import yaml
    except ImportError:
        raise YamlMissing(
            "ai-sdlc-harness: PyYAML missing for this hook's interpreter — "
            "/init-workspace bootstraps the plugin venv; until then this "
            "guard degrades open.") from None
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)

# A single "word" a flag's value can take — a whole quoted string (its
# space(s) included) counts as ONE token, not just up to the first
# whitespace (adversarial-review round 3 finding: plain `\S+` matched only
# `-C "my` out of `-C "my repo" commit`, leaving `repo" commit` unable to
# reach the verb — silently reopening the bypass for any quoted,
# space-containing flag value). Round 4 finding: a token can also be a MIX
# of bare and quoted segments the way the shell itself tokenizes —
# `-c user.name="My Name"` is ONE shell word, but the round-3 alternation
# (whole-quoted OR \S+) consumed only `user.name="My` and the parse died
# before the verb, reopening the same bypass one level down. One-or-more
# runs of (quoted segment | bare segment) matches shell word semantics.
_GIT_TOKEN = r"""(?:"[^"]*"|'[^']*'|[^\s"'|;&])+"""
# `git` not immediately preceded by a quote char (adversarial-review round
# 3 finding): without this, `grep -rn 'git reset --hard' .` — a pure read,
# searching for the LITERAL PHRASE — blocked, because "git reset --hard"
# appears verbatim inside the quotes with nothing distinguishing it from a
# real invocation by position alone. This doesn't solve quote-context
# detection in general (a real invocation whose own commit message quotes
# a git command, e.g. `git commit -m "run: git reset --hard"`, still
# blocks correctly on the OUTER real "commit" — that's not a false
# positive — but does nothing to stop the awkward case of a git command
# quoted as an argument's VALUE elsewhere); it closes the specific,
# reproduced gap where the guarded phrase is quoted as a literal, not
# invoked.
_GIT_ANCHOR = r"(?<!['\"])"
GIT_VERB_RE = re.compile(
    # The verb must be the actual subcommand — immediately after `git`, or
    # after any number of global flags — NOT anywhere later in the command.
    # Adversarial-review finding (round 1): the prior `[^|;&]*?` gap let the
    # verb match as a plain SUBSTRING anywhere after `git`, so `git log
    # --grep "merge"` (a pure read) false-positived on the word "merge"
    # appearing in the grep pattern.
    #
    # Global flags split into two shapes: a handful (-C, -c, --git-dir,
    # --work-tree, --namespace, --super-prefix, --exec-path) take a
    # SEPARATE value token when not written with `=`; every other dash-
    # prefixed flag (--no-pager, --paginate/-p, --bare, --literal-pathspecs,
    # ...) is self-contained. Adversarial-review finding (round 2): the
    # first attempt at this fix recognized only -C/-c/--git-dir explicitly
    # and required the verb IMMEDIATELY after — so `git --no-pager commit`
    # (or `push`) failed to match at all, silently REOPENING the raw-git
    # bypass hole for every verb, common flag, real usage. Any OTHER
    # self-contained flag is now accepted generically instead of requiring
    # an exact enumeration.
    # `[ \t]+` between tokens, NOT `\s+` (round-5 finding): a newline
    # separates commands in a multi-line Bash payload exactly like `;`
    # does, so `git --version\nrebase-helper.sh` is TWO commands — `\s+`
    # let the verb match across the line break as if it were one.
    # `pull` (adversarial-review round 6 finding): a pull IS a merge (or a
    # rebase, with pull.rebase) — leaving it out let raw history-mutating
    # merges through the front door while `git merge` itself was blocked.
    _GIT_ANCHOR + r"\bgit\b(?:[ \t]+(?:"
    r"(?:-C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--exec-path)"
    r"(?:=" + _GIT_TOKEN + r"|[ \t]+" + _GIT_TOKEN + r")?"
    r"|-{1,2}[A-Za-z][\w-]*(?:=" + _GIT_TOKEN + r")?"
    r"))*[ \t]+(commit|merge|rebase|cherry-pick|revert|am|pull|(?<!stash )push)\b")
# A quoted `sh -c "<payload>"` runs its payload as a full shell command —
# GIT_VERB_RE's quote anchor (correctly) refuses to match inside quotes, so
# without extracting these payloads `bash -c "git commit -m x"` sailed
# through the raw-git block entirely (adversarial-review round 6 finding).
SHELL_C_RE = re.compile(
    r"\b(?:sh|bash|zsh|dash|ksh)\b[^|;&\n\r]*?-c[ \t]+(?:\"([^\"]*)\"|'([^']*)')")
AUTHORITY_RE = re.compile(
    r"ai/[^/\s'\"]+/(state\.yaml|events\.ndjson|tokens\.ndjson|"
    r"human-input\.ndjson|reviews\.ndjson|\.redproof|\.state\.lock)|\.hmac\b")
# Programmatic file writes an inline interpreter can perform without any
# shell redirect (adversarial-review CRITICAL: WRITE_HINT_RE below caught
# `>`/`tee`/`sed -i`/… but NOT `python -c 'open(p,"a").write(x)'`,
# `node -e fs.appendFileSync`, `ruby File.write` — so the all-shape
# authority-file guard was bypassable by any shape, forging a reviewer
# verdict OR a gate approval into human-input.ndjson. REVIEWER_WRITE_RE
# already caught the python `"w"` case but not append, and not node/ruby;
# this shared fragment closes both). Write MODES only (`w`/`a`/`x`/`+`), so
# a read `open(p)` / `open(p,"r")` / `File.read` stays allowed.
_PROG_WRITE = (
    r"open\s*\([^)]*,[^)]*[\"'][^\"')]*[wax+]"    # python/ruby open(...,<write-mode>)
    r"|\.write_(?:text|bytes)\s*\("               # pathlib Path.write_text/bytes
    # node fs writes — match the distinctive METHOD name on any receiver
    # (`require("fs").appendFileSync` / `fs.writeFile` / `fsPromises.writeFile`
    # all lead here; anchoring on `fs.` missed the require(...) idiom)
    r"|\.(?:append|write)File(?:Sync)?\s*\(|\.createWriteStream\s*\("
    r"|\bFile\.(?:write|open)\b|\bIO\.write\b"    # ruby
)
WRITE_HINT_RE = re.compile(
    r"(?<![0-9])>(?!&)|\btee\b|\bsed\s+(-\w+\s+)*-i|\brm\b|\bmv\b|\bcp\b|"
    r"\btruncate\b|\bdd\b|yq\s+.*-i|--in-place|" + _PROG_WRITE)
# Destructive-but-not-matched-by-the-above git verbs (adversarial-review
# finding): `checkout -- </path>` and bare `checkout .` discard working-tree
# changes; restore/stash/clean mutate or delete the working tree outright —
# none of these are a raw commit/merge/etc. GIT_VERB_RE already blocks, and
# none redirect/tee/sed/rm/mv/cp/touch either. `_GIT_ANCHOR` prefix (round
# 3 finding, same class as GIT_VERB_RE's): without it, a pure read quoting
# one of these phrases verbatim (e.g. `grep -rn 'git reset --hard' .`)
# false-positived as if it were a real invocation. `git reset --hard`
# (round 2 finding: the first pass at this fix omitted it) discards
# working-tree AND committed changes just as destructively.
# Round 4 findings, same class: `checkout <tree-ish> -- <path>` restores
# from ANY ref (the round-3 pattern required `--` immediately after
# `checkout`, missing `git checkout HEAD -- src/`); `checkout ./` and
# `checkout ..` are the bare-`.` discard with a path spelling the
# `\.(?:\s|$)` pattern didn't cover; `checkout -f`/`--force` and
# `switch --discard-changes` throw away local modifications outright.
# One-command gap (round-5 finding, caught by re-review of round 4's own
# fix): the gaps here must stop at LINE BREAKS too, not just `|;&` — a
# newline separates commands in a multi-line Bash payload exactly like `;`
# does, and `[^|;&]*` happily crossed it, so `git checkout main\nnpm test
# -- --watch=false` (checkout, then an unrelated test run) false-positived
# on the later line's bare `--`. Same one-line intent applied to every
# pattern below and to PLANNER_STAMP_RE.
_CMD_GAP = r"[^|;&\n\r]*"
_REVIEWER_GIT_RE = (
    _GIT_ANCHOR + r"\bgit[ \t]+checkout\b" + _CMD_GAP + r"[ \t]--[ \t]|"
    + _GIT_ANCHOR + r"\bgit[ \t]+checkout[ \t]+\.{1,2}(?:[/\s]|$)|"
    + _GIT_ANCHOR + r"\bgit[ \t]+checkout\b" + _CMD_GAP + r"[ \t]-(?:f\b|-force\b)|"
    + _GIT_ANCHOR + r"\bgit[ \t]+switch\b" + _CMD_GAP + r"--discard-changes\b|"
    + _GIT_ANCHOR + r"\bgit[ \t]+restore\b|"
    + _GIT_ANCHOR + r"\bgit[ \t]+stash\b|"
    + _GIT_ANCHOR + r"\bgit[ \t]+clean\b" + _CMD_GAP + r"-f|"
    + _GIT_ANCHOR + r"\bgit[ \t]+reset\b" + _CMD_GAP + r"--hard\b"
)
# The reviewer's git-mutating forms, compiled standalone: they mutate the
# repo regardless of any path argument, so they stay blunt-blocked while
# the file-write side of the old REVIEWER_WRITE_RE became target-aware
# (field runs: 11 blocks across two stories were reviewers managing huge
# test-suite output — `tee /tmp/build.log`, `>> /tmp/out.log`, a QUOTED
# `> "/tmp/log"`, `rm /tmp/out.log` — every one a scratch write the old
# lookahead couldn't see past; each cost a blocked retry per review).
# The target policy lives in _reviewer_bash_write_violation below.
_REVIEWER_GIT_ONLY_RE = re.compile(_REVIEWER_GIT_RE)
# Bash-side developer write-confinement (the analogue of the Write/Edit
# path-guard): a developer may run builds/tests and edit files inside its
# worktree, but a bash WRITE to an ABSOLUTE path outside its allowed roots
# is the same cross-boundary drift the Write/Edit guard blocks — otherwise
# a developer blocked there could just `sed -i /other/repo/x` or `> /etc/x`.
# We extract the WRITE TARGET of the common idioms (not every path token —
# an absolute READ source like `cat /etc/x > ./local` must NOT block), then
# check each absolute target against the allowed roots. Residuals (accepted,
# documented — the Write/Edit path is the confined primary authoring channel
# and authority files are blocked regardless): a RELATIVE target (lands in
# the workspace cwd, not the worktree — odd but low-risk, and blocking every
# `> build.log` is worse), heredocs, and exotic idioms.
_REDIR_TARGET_RE = re.compile(
    r"(?<![0-9])>>?[ \t]*(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)")
_TEE_TARGET_RE = re.compile(r"\btee\b(?:[ \t]+-\S+)*[ \t]+(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)")
# verbs whose path ARGUMENTS are themselves the write/delete targets
# (`touch` added with the reviewer's target-aware policy — for a developer
# it now gets the same confinement as rm/mv/cp, previously unmatched)
_DESTRUCTIVE_VERB_RE = re.compile(
    r"\b(?:rm|mv|cp|touch|truncate|dd|install)\b|\bsed\b[^\n;|&]*?[ \t]-\w*i")
_ABS_TOKEN_RE = re.compile(r"\"(/[^\"]*)\"|'(/[^']*)'|(?<![\w/])(/[^\s;|&<>]+)")
_PROG_WRITE_RE = re.compile(_PROG_WRITE)
# targets that are always fine regardless of the allowed roots
_BASH_WRITE_SINK_OK = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")

_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'")


def _mask_quoted(cmd: str) -> str:
    """Blank the INSIDE of quoted spans — length-preserving, quotes kept —
    so write-idiom SHAPE matching can't fire on quoted data. Field (e2e
    E2E-1): a `>` inside a quoted awk/python/jq program handed
    _REDIR_TARGET_RE garbage targets ('{', 'should', ':'), each a blocked
    reviewer retry. A real redirect/tee/destructive-verb never sits inside
    quotes, and a quoted `sh -c` payload that IS a command gets re-scanned
    by _scan_targets. Length preservation means match offsets on the
    masked text are valid in the original — targets are read back from
    the original at the same span, since a TARGET is legitimately quoted
    (`> "/tmp/a b.log"`). Inline-interpreter writes must keep matching the
    UNmasked text: those live inside the quotes by nature."""
    return _QUOTED_SPAN_RE.sub(
        lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1],
        cmd)


def _developer_bash_write_targets(cmd: str) -> list[str]:
    masked = _mask_quoted(cmd)   # shapes on masked, targets from original
    targets: list[str] = []
    for m in _REDIR_TARGET_RE.finditer(masked):
        targets.append(cmd[m.start(1):m.end(1)].strip("\"'"))
    for m in _TEE_TARGET_RE.finditer(masked):
        targets.append(cmd[m.start(1):m.end(1)].strip("\"'"))
    # for destructive verbs / inline-interpreter writes, every absolute path
    # token in the command is a plausible target (their args are the objects
    # they act on)
    if _DESTRUCTIVE_VERB_RE.search(masked) or _PROG_WRITE_RE.search(cmd):
        for m in _ABS_TOKEN_RE.finditer(cmd):
            targets.append(m.group(1) or m.group(2) or m.group(3))
    return targets


def _reviewer_bash_write_violation(cmd: str, cwd: Path) -> str | None:
    """Read-only-with-scratch: the reviewer never mutates a repo, the
    workspace, or run state — but it MUST re-run test suites (review-task
    .md), and managing their output needs somewhere to write. /tmp and the
    /dev sinks are that somewhere; every other write target is a
    violation. Field runs (11 blocks across two stories): `tee /tmp/…`,
    `>> /tmp/…`, quoted `> "/tmp/…"`, and `rm /tmp/…` were all blocked by
    the old blunt regex, costing a blocked retry per review while blocking
    zero actual mutations. Git-mutating forms and inline-interpreter
    writes stay blunt-blocked (the former mutate regardless of arguments;
    the latter are nowhere near the natural test-output idiom). For
    rm/mv/cp/touch/…, ALL absolute path tokens must be scratch and at
    least one must exist — a relative arg is invisible to the sweep, so
    "no absolute token" fails closed. Residual (accepted, documented): a
    mixed `cp /tmp/x rel/dst` shape slips it — repo influence still ends
    at the verdict, since the reviewer has no Write/Edit at all.

    Shape-matching runs on a quote-masked view of the command (see
    _mask_quoted — field e2e E2E-1: `>` inside quoted awk/python/jq
    programs, and git verbs quoted in grep'd prose, false-blocked ~4
    reviews); targets are read back from the original text. A `$VAR`-held
    target stays BLOCKED (the guard can't expand variables), but the
    message now names the fix: literal /tmp paths."""
    masked = _mask_quoted(cmd)
    if _REVIEWER_GIT_ONLY_RE.search(masked):
        return "a git-mutating form"
    if _PROG_WRITE_RE.search(cmd):   # interpreter writes live INSIDE quotes
        return "an inline-interpreter file write"

    workspace = _session_workspace(cwd).resolve()

    def scratch(tgt: str) -> bool:
        if tgt in _BASH_WRITE_SINK_OK:
            return True
        path = Path(tgt)
        if not path.is_absolute():
            path = cwd / path      # a relative redirect lands in the
            # workspace — a real write, unlike the developer's accepted
            # relative-target residual (its cwd is inside its own lane)
        try:
            path = path.resolve()
        except OSError:
            return False
        # `_is_scratch_write`, not a bare /tmp check: a relative redirect
        # resolves under the workspace, and on Linux the workspace itself
        # commonly sits under /tmp — a bare check would wave through
        # `tee build.log`-shaped in-workspace writes as if they were
        # `tee /tmp/build.log` scratch (adversarial-review finding).
        return _is_scratch_write(path, workspace)

    def described(idiom: str, raw: str) -> str:
        if raw.strip("\"'").startswith("$"):
            return (f"{idiom} '{raw}' — a variable-held target the guard "
                    "cannot expand; use a literal /tmp path instead")
        return f"{idiom} '{raw}'"

    for m in _REDIR_TARGET_RE.finditer(masked):
        raw = cmd[m.start(1):m.end(1)]
        if not scratch(raw.strip("\"'")):
            return described("a redirect to", raw)
    for m in _TEE_TARGET_RE.finditer(masked):
        raw = cmd[m.start(1):m.end(1)]
        if not scratch(raw.strip("\"'")):
            return described("tee to", raw)
    if _DESTRUCTIVE_VERB_RE.search(masked):
        toks = [a or b or c for a, b, c in _ABS_TOKEN_RE.findall(cmd)]
        if not toks or not all(scratch(t) for t in toks):
            return "rm/mv/cp/touch/sed -i on a non-scratch path"
    return None


# Manual invocation of the CAPTURE hook entry points — blocked for every
# actor, orchestrator included (piping a synthetic UserPromptSubmit
# payload into guards.py directly would append a gate-approval record to
# human-input.ndjson indistinguishable from the human typing it — the
# ledgers' sole protection is that ONLY the platform fires these). The
# guard verbs (bash/write/read/spawn/skill) are not listed: invoking them
# manually can only ever BLOCK.
HOOK_FORGE_RE = re.compile(
    r"\bguards\.py\b" + _CMD_GAP + r"\b(?:user-prompt|post-spawn|subagent-stop)\b")
PLANNER_STAMP_RE = re.compile(
    r"\bharness\b" + _CMD_GAP + r"\brepo-map-stamp\b")
# `(?!<)` after the colon: spawn prompts routinely quote
# shared/status-block.md's reply template verbatim as instructions to the
# subagent — including its literal `harness-task: <task-id or ->` example —
# alongside the orchestrator's own real headers. A real header value is
# never a `<placeholder>`, so this keeps `re.search` (first match wins)
# from treating the quoted example as the real header.
MODE_HEADER_RE = re.compile(r"^harness-mode:\s*(?!<)(\S+)", re.MULTILINE)
TASK_HEADER_RE = re.compile(r"^harness-task:\s*(?!<)(\S+)", re.MULTILINE)
# `harness-run`'s value is a filesystem PATH, which CAN contain spaces
# (field report: a workspace under `.../HEX AI Engine/...` truncated at the
# first space with a `\S+` capture, so the resolved run never matched any
# live run and every harness-shape spawn was blocked as "does not match any
# active run"). Capture the REST OF THE LINE (`.` excludes newline) with
# trailing whitespace trimmed; the `(?![ \t<])` still skips the quoted
# `<run-dir>` placeholder AND won't let a leading space satisfy it. The
# other headers carry single-token values (mode/task/status names never
# contain spaces), so `\S+` stays correct there — and rightly refuses to
# swallow trailing prose like `harness-status: SUCCESS — all good`.
RUN_HEADER_RE = re.compile(r"^harness-run:[ \t]*(?![ \t<])(.*\S)", re.MULTILINE)
STATUS_RE = re.compile(r"^harness-status:\s*(\S+)", re.MULTILINE)
# The reviewer's verdict line (shared/status-block.md), captured by the
# PostToolUse hook into reviews.ndjson. Lenient token: leading whitespace/
# markdown bold (dogfood A2: an indented `details: |` block scalar hid a
# real APPROVED), and trailing punctuation/prose after the verdict word
# (adversarial-review: `verdict: APPROVED.` / `**verdict: APPROVED**` /
# `verdict: APPROVED — LGTM` all dropped a genuine approval, forcing a
# needless re-review). `\b` after the word bounds it without requiring a
# bare line.
VERDICT_RE = re.compile(
    r"^[ \t]*\**verdict:\**\s*(APPROVED|CHANGES_REQUESTED)\b", re.MULTILINE)
# The near-miss detector (NOT a capture rule): a verdict token that appears
# anywhere — mid-sentence, glued to prose — while the anchored rule above
# found nothing. Deliberately not folded into VERDICT_RE: the line anchor
# is the fail-closed floor (a false APPROVED completes a task unreviewed),
# so a run-together verdict must stay UNcaptured — but silently so is a
# trap: the orchestrator would hand-search the ledger, then try to
# recover via SendMessage-resume, a channel NO capture hook sees.
# This powers a signpost event naming the one sanctioned recovery.
VERDICT_ANYWHERE_RE = re.compile(r"verdict:\**\s*(APPROVED|CHANGES_REQUESTED)\b")


def extract_verdict(text: str) -> str | None:
    """The reviewer's verdict, resolved SAFELY (adversarial-review findings).

    Scope to the FINAL status block: the verdict lives in the trailing
    `harness-status:` block, so a verdict quoted in earlier prose (an
    example, a quoted prior round) is ignored — search only the text after
    the last `harness-status:`. If no status block is present (malformed
    reply), search the whole text.

    Within that scope, FAIL CLOSED on conflict: if BOTH verdicts appear,
    return CHANGES_REQUESTED. A false CHANGES_REQUESTED costs one re-review;
    a false APPROVED completes a task unreviewed — the asymmetry decides it.
    This subsumes both the last-match rule (which could let a quoted
    APPROVED after a real rejection win) and first-match (the inverse)."""
    m = list(STATUS_RE.finditer(text))
    scope = text[m[-1].start():] if m else text
    found = set(VERDICT_RE.findall(scope))
    if not found:
        return None
    if "CHANGES_REQUESTED" in found:
        return "CHANGES_REQUESTED"   # includes the both-present conflict case
    return "APPROVED"


def block(msg: str, cwd: Path | None = None) -> None:
    """Adversarial-review finding: hook blocks were never logged anywhere,
    despite design.md documenting them and metrics_report/status already
    filtering for a `hook-blocked` event kind that could never occur. Logs
    to the ONE live run when there's exactly one — unambiguous; with zero
    or several, logging would either have nowhere to go or risk attributing
    to the wrong sibling run (the same misattribution class Group 3 fixed
    for subagent-stop), so it's skipped rather than guessed. The block
    itself always happens regardless — logging is best-effort, never a
    precondition for it."""
    if cwd is not None:
        try:
            runs = live_runs(_session_workspace(cwd))
            if len(runs) == 1:
                ndjson.append_record(runs[0] / "events.ndjson",
                                     {"kind": "hook-blocked", "reason": msg})
        except OSError:
            pass
    print(msg, file=sys.stderr)
    sys.exit(2)


def shape_of(agent_type: str | None) -> str:
    # Agents follow the ai-sdlc-harness convention (name = ai-sdlc-<role>);
    # the pipeline's shape vocabulary is the bare role, so strip the prefix.
    return (agent_type or "").split(":")[-1].strip().lower().removeprefix("ai-sdlc-")


def live_runs(cwd: Path) -> list[Path]:
    """Runs under cwd — SKIPPING published mirror snapshots. A repo's
    `ai/<run>/` mirror is a dead ringer for a real run dir (state.yaml
    included) except for its `.mirror` marker; field (session D,
    transcript-proven): with the session shell drifted into a repo, the
    up-walk's probe matched the MIRROR, resolved the repo as 'the
    workspace', and capture wrote the human's gate reply into the mirror
    copy inside the repo working tree — dropped from the real ledger, and
    only kept out of git history by publish_mirror's prune. The marker is
    the designed discriminator; every resolution path funnels through
    here (capture, block()'s event logging, spawn legality)."""
    return sorted(p.parent for p in (cwd / "ai").glob("*/state.yaml")
                  if not (p.parent / ".mirror").exists())


def read_state(run: Path, workspace: Path) -> dict:
    try:
        import yaml
    except ImportError:
        raise YamlMissing("PyYAML missing — cannot read run state") from None
    key = chain.load_key(workspace)  # strict: a hook must never mint a key
    return yaml.safe_load(chain.verify(run / "state.yaml", key))


# ------------------------------------------------------------- Bash guards

def _scan_targets(cmd: str) -> list[str]:
    """The command itself, plus every quoted `sh -c` payload it carries —
    each payload is a full shell command in its own right, and the quote
    anchor (correct for grep'd literals) would otherwise hide it."""
    targets = [cmd]
    for m in SHELL_C_RE.finditer(cmd):
        targets.append(m.group(1) or m.group(2) or "")
    return targets


def guard_bash(p: dict) -> None:
    cmd = (p.get("tool_input") or {}).get("command") or ""
    cwd = Path(p.get("cwd") or ".")
    is_harness_ws = None  # computed lazily — most bash calls carry no git verb
    for target in _scan_targets(cmd):
        m = GIT_VERB_RE.search(target)
        if m:
            if is_harness_ws is None:
                is_harness_ws = _is_harness_workspace(cwd)
            if is_harness_ws:
                block(f"raw `git {m.group(1)}` is blocked (RC1): commits, history "
                      "rewrites, and remote updates go through the owned entry points — "
                      "`harness commit`, `harness merge-task`, `harness sync-branch`, "
                      "`harness push`, `harness publish-mirror`.", cwd)
        if AUTHORITY_RE.search(target) and WRITE_HINT_RE.search(target):
            block("run-authority files mutate only via the owned entry points — "
                  "`harness cursor` / `harness task` / `harness gate` / "
                  "`harness artifact` / `harness log-event` (RC1) — direct writes "
                  "are blocked. state.yaml and red-proofs are also chain-sealed "
                  "(RC4 detects out-of-band edits); the append-only evidence "
                  "ledgers (human-input.ndjson, reviews.ndjson) are NOT sealed, "
                  "so this guard is their sole protection.", cwd)
        if HOOK_FORGE_RE.search(target):
            block("the capture hook entry points (user-prompt / post-spawn / "
                  "subagent-stop) are fired by the platform ONLY — invoking "
                  "them manually can forge captured evidence (a synthetic "
                  "payload would mint a gate approval or reviewer verdict "
                  "indistinguishable from the real one). If a capture seems "
                  "broken, diagnose read-only and report; never execute the "
                  "hook yourself.", cwd)
        if (shape_of(p.get("agent_type")) in ("planner", "developer", "reviewer")
                and ".redproof" in target):
            # READ side of the red-proof (the write side is AUTHORITY_RE
            # above): a raw read skips chain verification, and prose alone
            # didn't hold — field: a permission-denied reviewer
            # "compensated manually" via `python3 -c` on the proof file
            # (allowed by the python3 permission), treating unverified
            # bytes as its intent-floor evidence. `show-redproof` IS the
            # read path; note the dot — the verb name `show-redproof`
            # itself must not trip this.
            block("red-proofs are read ONLY via `harness show-redproof` "
                  "(chain-verified) — a raw `.redproof/` read skips "
                  "integrity verification and is blocked for every shape. "
                  "Invoke it as `${CLAUDE_PLUGIN_ROOT}/bin/harness "
                  "show-redproof` — the bare `harness` spelling is neither "
                  "on PATH nor allow-listed.", cwd)
        if shape_of(p.get("agent_type")) == "reviewer":
            why = _reviewer_bash_write_violation(target, cwd)
            if why:
                block("the reviewer is read-only (design.md piece 3): builds/"
                      "tests may run, and capturing their output under /tmp "
                      "(or > /dev/null) is fine — but "
                      f"{why} mutates outside scratch and is blocked.", cwd)
        if shape_of(p.get("agent_type")) == "developer":
            # bash-side analogue of the Write/Edit confinement: a write
            # targeting an ABSOLUTE path outside the developer's allowed
            # roots is cross-boundary drift (the Write/Edit guard already
            # blocks it; without this a developer could sed/redirect around
            # that). Relative targets land in cwd (residual, see the
            # _developer_bash_write_targets note).
            for tgt in _developer_bash_write_targets(target):
                if tgt in _BASH_WRITE_SINK_OK or not Path(tgt).is_absolute():
                    continue
                resolved = Path(tgt).resolve()
                if not _developer_write_ok(resolved,
                                           _session_workspace(cwd).resolve()):
                    block("developer bash writes are confined to a registered "
                          f"repo or its worktree — '{tgt}' is outside it "
                          "(design.md piece 3 path-guard). Author via Write/"
                          "Edit or write inside your `harness-repo` worktree.",
                          cwd)
                # same test-first ordering as the Write/Edit surface —
                # otherwise `sed -i src/main/...` bypasses it on day one
                reason = _tdd_block_reason(
                    resolved, _session_workspace(cwd).resolve())
                if reason:
                    block(reason, cwd)
        if shape_of(p.get("agent_type")) == "planner" and PLANNER_STAMP_RE.search(target):
            block("the planner never stamps its own repo-map output — "
                  "`repo-map-stamp` is the orchestrator's job, run once after "
                  "the planner's spawn returns (agents/planner.md).", cwd)


# ------------------------------------------------- Write/Edit path guards

def _tmp_roots() -> tuple[Path, ...]:
    """Scratch roots a confined shape may write to, RESOLVED — on macOS
    `/tmp` is a symlink to `/private/tmp`, and `_resolve_write_path`
    resolves symlinks, so the un-resolved literal `Path("/tmp")` never
    matched anything there (adversarial-review finding: the whole /tmp
    allowance was dead code on Darwin — every scratchpad write by a
    developer/planner blocked). Deliberately NOT `tempfile.gettempdir()`:
    TMPDIR can be anywhere (and a workspace living under it — common in
    tests — would swallow the whole confinement)."""
    return (Path("/tmp").resolve(),)


def _is_scratch_write(path: Path, workspace: Path) -> bool:
    """A /tmp write is genuine SCRATCH only when it falls OUTSIDE the
    confined workspace's own tree AND outside every registered repo. On
    Linux (unlike macOS, where the per-test tempdir lands under
    /var/folders/…), `tempfile.mkdtemp()` — and CI/container workspaces
    generally — commonly land the workspace itself under /tmp, and a
    registered repo can independently be checked out under /tmp too (its
    own checkout dir, not necessarily nested under the workspace at all —
    `_registered_repos` finds repos VIA the workspace's repos.yaml, never
    assumes they live under it). Without both exclusions, a relative
    write that lands inside the workspace, or an absolute one that lands
    inside a repo's checkout sitting outside the workspace, would ALSO
    match the blanket /tmp allowance purely because of where it happens
    to sit — silently defeating every stricter per-shape confinement this
    scratch check sits behind (adversarial-review finding: 9 confinement
    tests passed on macOS only — the exact inverse of the Darwin-symlink
    bug the /tmp resolve() above was written to fix — and failed on Linux
    CI, where `pytest > build.log`-shaped relative writes and `rm -rf
    <workspace>/Code/other`-shaped sibling writes both resolved under
    /tmp and were nodded through as scratch. A second adversarial-review
    pass on the first fix then found the repo-checkout gap: without the
    repo exclusion here, a reviewer/planner write into a sibling repo
    checkout that ALSO happens to sit under /tmp — a plausible CI/sandbox
    layout — would be waved through as scratch too, defeating the
    reviewer's read-only guarantee and the planner's repo-source
    immunity; the developer's own confinement doesn't depend on this
    exclusion since `_developer_write_ok` checks repo/worktree membership
    before ever calling this function, but reviewer and planner have no
    such prior check)."""
    if not any(path.is_relative_to(r) for r in _tmp_roots()):
        return False
    if path.is_relative_to(workspace):
        return False
    return not any(path.is_relative_to(r) for r in _registered_repos(workspace))


def _registered_repos(workspace: Path) -> list[Path]:
    """Resolved paths of every repo registered in this workspace's
    `repos.yaml`. The developer write-confinement is built from these, NOT
    from the payload `cwd` (field report + adversarial-review prediction:
    a spawned developer's `cwd` is the SESSION WORKSPACE it launched from,
    never its worktree — so confining to `cwd` blocked every legitimate
    worktree write, since worktrees live as siblings of the repo, outside
    the workspace). `cwd` IS reliably the workspace, so it's used to FIND
    the repos, not as the write root itself."""
    try:
        import yaml
    except ImportError:
        return []
    f = workspace / ".claude" / "context" / "repos.yaml"
    if not f.exists():
        return []
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except Exception:
        return []
    repos = (data or {}).get("repos") if isinstance(data, dict) else None
    out: list[Path] = []
    for p in (repos or {}).values():
        try:
            out.append(Path(str(p)).resolve())
        except Exception:
            pass
    return out


def _developer_write_ok(path: Path, workspace: Path) -> bool:
    """A developer may write inside a registered repo, inside one of its
    per-task worktree siblings (`worktree_add`: `repo.parent/<repo.name>-wt-
    <task>-<uid>`), or in /tmp — nothing else. Derived from `repos.yaml`
    under the workspace; a spaced repo path (`.../HEX AI Engine/...`) is
    handled by Path semantics, not a regex.

    Fail-OPEN when the repo set can't be determined (no `repos.yaml`, no
    PyYAML): this confinement is defense-in-depth, not the integrity
    guarantee (authority files are blocked separately, raw git is blocked
    in bash, and the reviewer + HMAC chain are the real backstops), so a
    guard that can't compute its bounds must not strand a developer —
    consistent with guard_write's documented fail-open stance.

    Registered-repo membership is checked BEFORE the blanket /tmp scratch
    allowance, not after: a path inside some repo's own parent directory
    that ISN'T a legit worktree sibling of ANY registered repo is a
    deliberate escape (the field case this guard exists for) and must
    stay blocked even when that parent happens to sit under /tmp too —
    falling through to `_is_scratch_write` for it would silently readmit
    exactly the sibling-repo escape the worktree-prefix check just
    refused (adversarial-review finding, same class as the Linux-vs-macOS
    tempdir gap `_is_scratch_write` documents).

    The loop tries EVERY registered repo before giving up — not a
    return-on-first-non-match — because a multi-repo workspace commonly
    registers repos as siblings under one shared parent (`ws/Code/alpha`,
    `ws/Code/beta`, the exact layout `/add-repo` produces): a path inside
    `beta` fails `alpha`'s direct-membership check AND lands inside
    `alpha.parent`, so returning False on that first near-miss would deny
    a perfectly legitimate write into `beta` before `beta` ever got a
    turn — order-dependent on `repos.yaml` iteration order, and wrong for
    every repo but whichever happened to be listed first (adversarial-
    review finding, second pass: confirmed as a regression this fix's
    first draft introduced, independent of the /tmp collision above)."""
    repos = _registered_repos(workspace)
    near_a_repo = False
    for repo in repos:
        if path.is_relative_to(repo):
            return True
        parent = repo.parent
        if path.is_relative_to(parent):
            rel = path.relative_to(parent)
            if rel.parts and rel.parts[0].startswith(repo.name + "-wt-"):
                return True
            near_a_repo = True  # sibling of THIS repo, not its worktree —
            # still check the rest before concluding it's an escape
    if near_a_repo:
        return False  # sibling of some registered repo, not a legit
        # worktree of ANY of them — never falls through to /tmp scratch
    if _is_scratch_write(path, workspace):
        return True
    return not repos  # can't determine bounds — fail open, don't strand dev


# --------------------------------------- developer TDD-ordering guard

def _find_worktree_task(path: Path, workspace: Path):
    """(worktree_root, run_dir, task_record) for the recorded task worktree
    containing `path`, or None. Matched by the EXACT worktree path recorded
    in state.yaml at `worktree-add` (task["worktree"]["path"]) — never by
    parsing the task id out of the directory name — so parallel developers
    and hyphenated task ids can't cross-attribute. Aborted runs are skipped
    (abort sweeps their worktrees; a stale dir must not enforce anything),
    published mirrors are skipped the same way `state.load` refuses them,
    and an unreadable/corrupt sibling run is skipped, not raised — mirroring
    guard_spawn's one-corrupt-run-doesn't-brick-the-workspace policy."""
    for repo in _registered_repos(workspace):
        parent = repo.parent
        if not path.is_relative_to(parent):
            continue
        rel = path.relative_to(parent)
        if not (rel.parts and rel.parts[0].startswith(repo.name + "-wt-")):
            continue
        wt_root = (parent / rel.parts[0]).resolve()
        for sf in sorted((workspace / "ai").glob("*/state.yaml")):
            run = sf.parent
            if (run / ".mirror").exists():
                continue
            try:
                st = read_state(run, workspace)
            except Exception:
                continue
            if st.get("aborted") or st.get("completed"):
                continue
            for t in st.get("tasks") or []:
                wt = t.get("worktree")
                rec = wt.get("path") if isinstance(wt, dict) else None
                if rec and Path(rec).resolve() == wt_root:
                    return wt_root, run, t
    return None


def _tdd_block_reason(path: Path, workspace: Path) -> str | None:
    """Test-first ordering for developer writes (design.md piece 5A). Field
    report: 2 of 8 declared test-intents had zero test code while their
    production signatures were already changed — the prompt-only "no
    implementation yet" had no mechanical form, and design.md:395 recorded
    that as accepted (blob-SHA at verify-green covers the TEST files, but
    is retrospective and says nothing about production edited pre-red).
    Reversed on that evidence: a write to a NON-test path inside a task's
    worktree is refused while that task declares test-intents and its
    red-proof is not yet sealed.

    The exemption is the plan itself: a task with no declared intents
    (docs/config/chore, quick mode) is never subject to the ordering — the
    human approved that shape at the plan gate; `test_intents: []` IS the
    opt-out, no second flag. Red-proof existence is a plain file check: a
    developer cannot fabricate it (AUTHORITY_RE + the write-confinement
    block the run dir on both surfaces), and the authoritative seal +
    blob-SHA verification stays at set-state. Fail-OPEN on every
    indeterminate (direct-branch fallback — `worktree: null` leaves nothing
    to match — unreadable state/config, missing PyYAML): defense-in-depth
    against a drifting agent; verify-red's intent floor + verify-green's
    blob-SHA comparison remain the guarantee."""
    try:
        found = _find_worktree_task(path, workspace)
        if found is None:
            return None
        wt_root, run, task = found
        if not task.get("test_intents"):
            return None      # no declared intents -> ordering not demanded
        from harness.transitions import redproof_path
        if redproof_path(run, task["id"]).exists():
            return None      # red sealed -> production writes unlocked
        rel = path.relative_to(wt_root).as_posix()
        if rel == ".":
            # The worktree ROOT itself is never a real file write — it
            # reaches here as bash attribution noise: a destructive verb
            # anywhere in the command makes _developer_bash_write_targets
            # sweep every absolute token, including a `cd <worktree>` /
            # `-C <worktree>` argument (which would otherwise block a
            # legitimate clean-and-build as "'.' is not a test path"). A
            # genuine `rm -rf <worktree>` residual is accepted:
            # it nukes the tests too, so verify-red/green fail loudly.
            return None
        from harness.cli import load_declared
        from harness.gitops import matches_any
        lang = load_declared(workspace)[2].get("language") or {}
        allowed = [*(lang.get("test_paths") or []),
                   *(lang.get("test_closure") or []),
                   *(lang.get("pre_red_paths") or [])]
        if matches_any(rel, allowed):
            return None
        return (f"task {task['id']} declares test-intents but its red-proof "
                f"is not sealed yet, and '{rel}' is not a test path — "
                "test-first ordering (design.md piece 5A): write the declared "
                "failing tests first (paths matching language.test_paths / "
                "test_closure / pre_red_paths are writable now), run "
                f"`harness verify-red --task {task['id']}`, and production "
                "writes unlock. Tasks with no declared test-intents are "
                "exempt from this ordering.")
    except Exception:
        # advisory ordering guard: an indeterminate (or a bug here) must
        # never strand a developer — the chain-verified checkpoint at task
        # completion is the guarantee, this is the early trip-wire
        return None


def _resolve_write_path(fp: str, cwd: Path) -> Path:
    """Resolve `fp` the way the tool actually would (relative to the
    agent's own `cwd`, or absolute), collapsing `..` components and
    symlinks. `Path.resolve()` on a bare relative path resolves against
    THIS PROCESS's os.getcwd() — not the payload's `cwd` — so the join
    happens first (adversarial-review finding: the prior lexical
    `is_relative_to` checks never resolved `..` at all, so e.g.
    `ai/../src/x.py` lexically prefix-matched an allowed `ai/` root while
    actually escaping it once resolved)."""
    path = Path(fp)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def guard_read(p: dict) -> None:
    """Raw red-proof reads bypass the chain (Read/Grep-tool side; the Bash
    side lives in guard_bash): `harness show-redproof` is the ONE verified
    read path — review-task.md said so in prose, and a permission-denied
    reviewer could walk straight past it. Harness shapes only; the
    orchestrator/human stay free for debugging."""
    if shape_of(p.get("agent_type")) not in ("planner", "developer", "reviewer"):
        return
    tool_input = p.get("tool_input") or {}
    fp = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if ".redproof" in Path(fp).as_posix():
        block("red-proofs are read ONLY via `harness show-redproof` "
              "(chain-verified) — a raw `.redproof/` read skips integrity "
              "verification. Invoke it as `${CLAUDE_PLUGIN_ROOT}/bin/"
              "harness show-redproof --task <T> --run <run>`.",
              Path(p.get("cwd") or "."))


def guard_write(p: dict) -> None:
    tool_input = p.get("tool_input") or {}
    fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not fp:
        return
    cwd = Path(p.get("cwd") or ".")
    ws = _session_workspace(cwd)   # cd-drift-immune confinement roots
    posix = Path(fp).as_posix()
    if AUTHORITY_RE.search(posix):
        block("run-authority files mutate only via the owned entry points — "
              "`harness cursor` / `harness task` / `harness gate` / "
              "`harness artifact` / `harness log-event` (RC1); this write is "
              "blocked for every role.", cwd)
    shape = shape_of(p.get("agent_type"))
    if shape == "reviewer":
        block("the reviewer is read-only (design.md piece 3) — no Write/Edit.", cwd)
    if shape == "developer":
        # `cwd` is the workspace the developer was spawned from (NOT its
        # worktree — that lives outside the workspace); use it to find the
        # registered repos, and confine writes to those repos + their
        # worktree siblings + /tmp (field report: the old `is_relative_to
        # (cwd)` confined to the workspace and blocked every worktree
        # write; it also let a developer write anywhere IN the workspace).
        path = _resolve_write_path(fp, cwd)
        if not _developer_write_ok(path, ws.resolve()):
            block(f"developer writes are confined to a registered repo or its "
                  f"per-task worktree (design.md piece 3 path-guard) — '{fp}' "
                  "is under neither. Write inside your `harness-repo` worktree, "
                  "not the workspace or another repo.", cwd)
        reason = _tdd_block_reason(path, ws.resolve())
        if reason:
            block(reason, cwd)
    if shape == "planner":
        path = _resolve_write_path(fp, cwd)
        artifact_roots = (ws.resolve() / "ai", (ws / ".claude" / "context").resolve())
        # scratch checked via `_is_scratch_write`, not a bare /tmp
        # membership test: the workspace root itself commonly sits under
        # /tmp (Linux `tempfile.mkdtemp()`, CI/containers), and a bare
        # check would wave through any workspace-internal path — e.g.
        # `<workspace>/src/x.py` — as if it were unrelated /tmp scratch
        # (adversarial-review finding).
        if not (any(path.is_relative_to(a) for a in artifact_roots)
                or _is_scratch_write(path, ws.resolve())):
            block("planner writes are confined to run artifacts (ai/<run>/) and "
                  ".claude/context/ — it never touches repo source "
                  "(design.md piece 3 path-guard).", cwd)
        if path.name == ".meta.json":
            # Otherwise legal by the path check above — repo-map/<name>/ is
            # squarely inside .claude/context/ — so this needs its own,
            # filename-specific check, the same way AUTHORITY_RE blocks
            # specific run-authority filenames within an otherwise-writable
            # directory. Mirrors PLANNER_STAMP_RE's Bash-side check on the
            # same rule (guard_bash) — the CLI verb is one way to produce
            # this file; hand-authoring it directly is the other.
            block("the planner never stamps its own repo-map output — "
                  "`.meta.json` is written only by `harness repo-map-stamp`, "
                  "run by the orchestrator after the planner's spawn "
                  "returns (agents/planner.md).", cwd)


# --------------------------------------------------------- spawn / skill

def guard_spawn(p: dict) -> None:
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    manifest = load_yaml(PLUGIN_ROOT / "pipeline" / "manifest.yaml")
    tool_input = p.get("tool_input") or {}
    cwd = Path(p.get("cwd") or ".")
    ws = _session_workspace(cwd)   # cd-drift-immune (field: session D)
    shape = shape_of(tool_input.get("subagent_type"))
    if shape not in surfaces["shapes"]:
        return  # not a harness shape — none of our business
    if tool_input.get("run_in_background") in (True, "true", "True"):
        # Backgrounding a reviewer or developer spawn is dangerous: a
        # background spawn's tool_response is only the launch stub — the
        # real reply never passes through ANY hook payload — so the
        # reviewer's verdict would be unrecoverable, the stub would
        # fabricate a missing-status-block stall event, and the stall
        # reinvoke would race the still-live background original in the
        # same worktree. Verdict/stall capture is ANCHORED to the
        # foreground tool_response (capture_post_spawn); background
        # harness spawns break it structurally.
        block(f"harness-shape spawns ('{shape}') must run in the FOREGROUND "
              "— verdict/status capture reads the spawn's own tool_response, "
              "and a background spawn returns only a launch stub, so a "
              "reviewer verdict would be unrecoverable and a spurious stall "
              "event fabricated. For parallelism, batch multiple foreground "
              "spawns in ONE message — they run concurrently and each reply "
              "is captured.", cwd)
    prompt = tool_input.get("prompt") or ""
    m = MODE_HEADER_RE.search(prompt)
    if not m:
        block(f"harness-shape spawn ('{shape}') requires the structured "
              "`harness-mode: <mode>` header in the spawn prompt (RC4).", cwd)
    mode = m.group(1)
    pair = {"shape": shape, "mode": mode}
    if pair in (manifest.get("always_legal_spawns") or []):
        return
    # A spawn legalized by a RUN's current step must name that run
    # (adversarial-review finding: SKILL.md claimed the guard enforced the
    # `harness-run:` header, but only `harness-mode:` was ever checked — so
    # a headerless spawn passed, and capture_subagent_stop then silently
    # dropped its token/stall attribution in any multi-run workspace).
    m_run = RUN_HEADER_RE.search(prompt)
    header_run = None
    if m_run:
        candidate = Path(m_run.group(1))
        if not candidate.is_absolute():
            candidate = ws / candidate
        header_run = candidate.resolve()
    runs = live_runs(ws)
    step_would_match = False
    for run in runs:
        try:
            st = read_state(run, ws)
        except chain.IntegrityError:
            # A corrupt/tampered run must not veto every OTHER run's legal
            # spawns in the same workspace (adversarial-review finding: this
            # used to propagate uncaught, failing closed for the whole
            # workspace). It still contributes no spawn-set of its own —
            # skip it, loudly, rather than either blocking everything or
            # silently pretending it's fine; `harness reseal` is the
            # human-invoked recovery if this is a crash, not tampering.
            print(f"ai-sdlc-harness: run at {run} failed integrity verification "
                  "— skipped for spawn-legality, not blocking other runs "
                  "(see `harness reseal` if this is crash recovery, not "
                  "tampering).", file=sys.stderr)
            continue
        if st.get("aborted") or st.get("completed"):
            continue  # terminal by declaration — legalizes nothing
        step = manifest["steps"].get(st["cursor"]["current_step"]) or {}
        if pair in (step.get("spawns") or []):
            if header_run is None:
                step_would_match = True  # legal step, but unattributable
                continue
            if run.resolve() == header_run:
                return
    # Declared out-of-run exceptions are a standing allowance, not one
    # conditioned on the workspace having zero run directories: `ai/*/`
    # never gets cleaned up after a run reaches its terminal step, so
    # "no runs" would otherwise almost never be true in a workspace that's
    # ever run `/dev-workflow` once — exactly the state `/add-repo` and
    # `/repo-map-refresh` are normally invoked in.
    if pair in (surfaces.get("out_of_run_spawns") or []):
        return
    if step_would_match:
        block(f"spawn ({shape}, {mode}) matches a live run's current step, "
              "but the prompt carries no `harness-run: <run-dir>` header — "
              "every in-run spawn must name its run so its tokens/stalls "
              "attribute to it (RC4; SKILL.md's mandated headers).", cwd)
    if not runs:
        block(f"no active run — harness-shape spawns are fail-closed pre-run; "
              f"({shape}, {mode}) is not a declared out-of-run exception "
              "(invocation control, design.md piece 3).", cwd)
    block(f"spawn ({shape}, {mode}) does not match any active run's current "
          "step spawn-set or the always-legal list — the manifest is the "
          "source of truth (design.md piece 1).", cwd)


def guard_skill(p: dict) -> None:
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    skill = ((p.get("tool_input") or {}).get("skill") or "").split(":")[-1]
    if skill in surfaces["user_entry"]["skills"] and (
            p.get("agent_id") or p.get("agent_type")):
        block(f"'/{skill}' is a user-entry skill — invocable only from the "
              "main session by a human, never from a subagent "
              "(invocation control, design.md piece 3).",
              Path(p.get("cwd") or "."))


# --------------------------------------------------------------- capture

def _awaiting_gate_decision(st: dict) -> bool:
    """A gate is presented and not yet decided — the ONLY window in which a
    captured record can ever qualify as gate evidence (`gates.decide`
    requires a record strictly after `presented_at`; anything captured
    outside the window is unreadable by design, RC3/RC4)."""
    return any(isinstance(g, dict) and g.get("presented_at")
               and g.get("decision") is None
               for g in (st.get("gates") or {}).values())


def _nearest_workspace(cwd: Path) -> Path:
    """The gate-evidence workspace for THIS session: cwd itself when it
    holds live runs, else the nearest ancestor that does. If the
    orchestrator cd's into a child repo, the user's APPROVED fires this
    hook with cwd=<ws>/web; live_runs would find nothing, and genuine
    gate evidence would be dropped silently. Bounded walk; a registered
    repo that is NOT under the workspace (sibling
    layouts) remains a documented residual — nothing in the payload names
    the workspace then, and the gate-decide refusal message carries the
    diagnostic breadcrumb for that case."""
    probe = cwd
    for _ in range(8):
        try:
            # live_runs, not a raw glob: a repo's published mirror carries
            # ai/<run>/state.yaml too, and matching it here resolved the
            # REPO as the workspace (field: session D's dropped `waive`)
            if live_runs(probe):
                return probe
        except OSError:
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    return cwd


def _session_workspace(cwd: Path) -> Path:
    """Env-first workspace resolution for EVERY hook (capture, spawn
    legality, block()'s event logging, write/bash confinement roots). The
    platform sets CLAUDE_PROJECT_DIR (the session's project root) for
    every hook invocation — and unlike the payload's cwd, it is immune to
    shell `cd` drift for the whole session. Field, three bites of the
    same class: two dropped gate replies (E2E-1, session D), then a
    pre-pr reviewer spawn refused "no active run" because guard_spawn
    resolved runs from a drifted cwd (session D again). For the
    mainstream session shape (claude started IN the workspace) the env
    var closes the class completely. Validated — it must actually hold
    live, non-mirror runs — so a session started elsewhere (workspace
    opened as a subdirectory, tests firing the hook directly) falls back
    to the cwd up-walk unchanged."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        root = Path(proj)
        try:
            if live_runs(root):   # mirror-filtered, same as every scan
                return root
        except OSError:
            pass
    return _nearest_workspace(cwd)


def _has_bootstrap_marker(path: Path) -> bool:
    """True if `path` is a workspace root that has completed
    `/init-workspace` — `harness init-finalize` is the one call that writes
    `bootstrap_completed` into `.claude/context/overrides.yaml`
    (harness/initws.py `mark_bootstrapped`). A plain substring check, not a
    YAML parse: guard_bash is a "pure regex+payload" guard that must keep
    working — and blocking — without PyYAML (module docstring); overrides.yaml
    is a flat top-level mapping, so a raw-text check for the key is safe and
    avoids putting a hard dependency on this guard's git-verb path.

    Presence, not truthiness (unlike `migrate.detect`/`workflow.bootstrap_gate`,
    which parse the YAML and check `bool(...)`): the sole writer,
    `mark_bootstrapped`, only ever writes a non-empty ISO timestamp, so the
    two checks agree today. If a future un-bootstrap path ever sets the key
    falsy instead of removing it, this would need to switch to a value check
    too — flagged here so that change doesn't silently drift from the other
    two consumers (adversarial-review finding).

    `except Exception`, matching `_registered_repos`' own stance on this
    file's un-chain-sealed, hand-editable config: a decode error (invalid
    UTF-8) is a `ValueError`, not an `OSError` — adversarial-review finding:
    an `OSError`-only catch let a corrupt `overrides.yaml` raise uncaught out
    of `guard_bash`, which is a FAIL_OPEN guard — so the exception aborted
    the ENTIRE bash guard invocation mid-loop, silently skipping every other
    check for that same command (AUTHORITY_RE included), not just this one."""
    f = path / ".claude" / "context" / "overrides.yaml"
    try:
        return "bootstrap_completed" in f.read_text(encoding="utf-8")
    except Exception:
        return False


def _is_harness_workspace(cwd: Path) -> bool:
    """Whether `cwd`'s session belongs to a workspace that has ever
    completed `/init-workspace` — the gate for guard_bash's raw-git block,
    RC1's one deliberately STANDING (not run-scoped) rule, narrowed here to
    sessions that actually opted into the harness rather than every session
    the plugin happens to be enabled for (README FAQ). Mirrors
    `_session_workspace`'s CLAUDE_PROJECT_DIR-first, cd-drift-immune
    resolution: the env var is the session's ACTUAL project root and wins
    when it is itself bootstrapped, before falling back to a bounded
    up-walk from the payload's (driftable) cwd — same 8-level bound as
    `_nearest_workspace`, same rationale.

    Two known, accepted residuals (not bugs to chase):

    1. A session whose CLAUDE_PROJECT_DIR is rooted directly inside a repo
       REGISTERED to a SIBLING workspace — rather than the workspace itself
       — finds no marker walking its own ancestors, since nothing today
       points from a registered repo back to the workspace that owns it
       (`_registered_repos` only resolves workspace -> repos, never the
       reverse). Raw git stays unprotected in that one layout, same as
       today's pre-existing "disable the plugin for sessions where you want
       raw git back" workaround for it — just narrower in scope than before.

    2. The bootstrap marker itself (`.claude/context/overrides.yaml`) is an
       ordinary, non-chain-sealed config file — unlike `state.yaml`/the
       ledgers, it isn't HMAC-sealed (RC4) or `AUTHORITY_RE`-protected, and
       is legally writable by the orchestrator and the planner shape via
       Write/Edit. A direct edit stripping `bootstrap_completed` (not
       reachable through any owned CLI verb, but not blocked by any guard
       either) silently turns the raw-git block back off for the rest of
       the session, even mid-run. Accepted deliberately, adversarial-review
       finding: matches this module's existing two-layer stance elsewhere
       (guard = fast-fail defense-in-depth, HMAC chain = the actual
       integrity guarantee) rather than a gap unique to this change — but
       it IS a new capability the unconditional pre-change block never had
       (there was no file to edit before). Revisit if it turns out to
       matter in practice, e.g. by chain-protecting the marker or adding it
       to `AUTHORITY_RE`."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj and _has_bootstrap_marker(Path(proj)):
        return True
    probe = cwd
    for _ in range(8):
        if _has_bootstrap_marker(probe):
            return True
        if probe.parent == probe:
            break
        probe = probe.parent
    return False


def capture_user_prompt(p: dict) -> None:
    """Capture gate evidence — scoped to runs actually AWAITING a gate
    decision, not fanned out to every live run (adversarial-review finding:
    the fan-out meant (a) terminal runs accumulated the user's raw text
    forever, against the human-input privacy posture, and (b) in a
    workspace with two live runs, an `APPROVED` typed for run B also landed
    in run A's ledger and could satisfy run A's presented gate — defeating
    the RC3 trust anchor across runs). Scoping is semantics-preserving:
    records outside a presented-undecided window can never qualify in
    `gates.decide` anyway (strictly-after `presented_at`, most-recent
    wins). Residual, documented in design.md: two runs SIMULTANEOUSLY
    mid-gate in one workspace still both capture — irreducible here
    because neither the hook payload nor the CLI carries a session↔run
    binding; the gate texts' numbered-option sets differing is the
    practical mitigation.

    Fail-stance: capture-only, fails TOWARD capturing — a run whose state
    can't be read (missing PyYAML, integrity failure mid-crash) gets the
    record anyway; losing genuine gate evidence is the greater harm, and
    unreadable-state capture matches the pre-scoping behavior."""
    text = p.get("prompt") or p.get("user_prompt") or ""
    if not text:
        return
    cwd = _session_workspace(Path(p.get("cwd") or "."))
    record = {"text": text,
              "hash": hashlib.sha256(text.encode()).hexdigest()}
    for run in live_runs(cwd):
        try:
            st = read_state(run, cwd)
        except Exception:
            ndjson.append_record(run / "human-input.ndjson", record)
            continue
        if st.get("aborted") or st.get("completed"):
            continue  # terminal — no gate of its can ever be decided again
        if _awaiting_gate_decision(st):
            ndjson.append_record(run / "human-input.ndjson", record)


def _parse_transcript(path: Path) -> dict:
    """Each JSONL line is ONE content block of a turn, not the whole
    message — a turn that thinks/calls-a-tool/answers spans several lines
    sharing the same `message.id`. Treating the last line seen as if it
    were the complete message (the original approach) loses the actual
    reply text whenever a trailing tool_use/thinking block-line follows
    the text block-line for that same id — exactly the shape of a normal
    "let me check that, <tool call>" turn, and observed in practice
    wiping out genuine `harness-status:` replies. Group by id instead."""
    messages: list[dict] = []
    by_key: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        msg = entry.get("message") or {}
        content = msg.get("content")
        # NEWLINE join, not "" (adversarial-review finding, same class as
        # _response_text): a content block ending without a newline would
        # glue its tail onto the next block's first line, hiding a
        # `verdict:`/`harness-status:` line from the line-anchored regexes.
        text = "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict)) if isinstance(content, list) \
            else (content if isinstance(content, str) else "")
        key = (role, msg.get("id") or id(entry))
        if key not in by_key:
            by_key[key] = {"role": role, "text": "", "model": msg.get("model"),
                          "usage": {}}
            messages.append(by_key[key])
        by_key[key]["text"] += "\n" + text
        if msg.get("usage"):
            by_key[key]["usage"] = msg["usage"]
    first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
    last_assistant = next((m for m in reversed(messages) if m["role"] == "assistant"), {})
    # Sum usage across EVERY assistant turn, not just the last one
    # (adversarial-review finding: each turn is its own API call with its
    # own token cost — a multi-turn subagent's total was undercounted to
    # just its final reply's usage, contradicting design.md's "actual
    # numbers" claim for the token ledger).
    total_usage: dict = {}
    for m in messages:
        if m["role"] != "assistant":
            continue
        for k, v in (m.get("usage") or {}).items():
            # scalars only (dogfood A2 finding, deterministic on every
            # spawn: real usage blocks carry a NESTED `cache_creation:
            # {ephemeral_5m_input_tokens, …}` dict alongside the flat
            # fields — blind `int + dict` summation raised TypeError,
            # which FAIL_OPEN then swallowed, so no token record was ever
            # written and nothing said why). The four consumed fields are
            # all flat scalars; nested breakdowns are skipped.
            if isinstance(v, (int, float)):
                total_usage[k] = total_usage.get(k, 0) + v
    return {"first_user": first_user, "text": last_assistant.get("text", ""),
            "model": last_assistant.get("model"), "usage": total_usage}


def _resolve_run(runs: list[Path], header_src: str, cwd: Path) -> Path | None:
    """Which live run a subagent's records belong to — from its OWN spawn
    prompt's `harness-run:` header (mandated in every harness-shape spawn),
    never `runs[0]` (adversarial-review finding: with terminal runs never
    cleaned up, `ai/*/` almost always holds more than one run past the
    first story, so "the first one" silently misattributes every second+
    run's tokens/stalls to whichever run happens to sort first)."""
    m = RUN_HEADER_RE.search(header_src)
    if not m:
        return None
    candidate = Path(m.group(1))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve()
    for run in runs:
        if run.resolve() == candidate:
            return run
    return None


def capture_subagent_stop(p: dict) -> None:
    cwd = _session_workspace(Path(p.get("cwd") or "."))
    runs = live_runs(cwd)
    if not runs:
        return
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    transcript = p.get("agent_transcript_path") or p.get("transcript_path")
    data = _parse_transcript(Path(transcript)) if transcript and Path(transcript).exists() else {}
    header_src = data.get("first_user", "")
    shape = shape_of(p.get("agent_type"))
    if shape not in surfaces["shapes"]:
        if p.get("agent_type"):
            return  # a real, non-harness agent — none of our business
        # agent_type ABSENT is a payload-contract anomaly (dogfood finding:
        # capture silently no-opped for a whole session, undiagnosable
        # after the fact) — fall back to the spawn prompt's own mandated
        # `harness-mode:` header (modes are unique per shape). Anomalies
        # go to stderr + HARNESS_HOOK_DEBUG, never the event ledger:
        # builtin-agent stops are frequent and must not spam it.
        m = MODE_HEADER_RE.search(header_src)
        mode_hint = m.group(1) if m else None
        shape = next((s for s, d in surfaces["shapes"].items()
                      if mode_hint in (d.get("modes") or [])), "")
        if not shape:
            print("ai-sdlc-harness: subagent-stop payload carried no agent_type "
                  "and its transcript has no harness headers — token capture "
                  "skipped (HARNESS_HOOK_DEBUG=1 records raw payloads).",
                  file=sys.stderr)
            return
    run = _resolve_run(runs, header_src, cwd) or (runs[0] if len(runs) == 1 else None)
    if run is None:
        # Can't attribute to a specific run among several — never guess.
        # Adversarial-review round 2 finding: this silently dropped the
        # subagent's tokens/stall-detection with no visible trace at all,
        # asymmetric with guard_spawn's printed warning for its own
        # analogous skip (a corrupt sibling run).
        print(f"ai-sdlc-harness: could not attribute a subagent-stop event to "
              f"one of {len(runs)} live runs (no matching harness-run: "
              "header) — tokens/stall-detection for this invocation are "
              "not recorded.", file=sys.stderr)
        return
    task = (TASK_HEADER_RE.search(header_src) or [None, None])[1]
    mode = (MODE_HEADER_RE.search(header_src) or [None, None])[1]
    usage = data.get("usage") or {}
    ndjson.append_record(run / "tokens.ndjson", {
        "task": task, "mode": mode, "role": shape_of(p.get("agent_type")),
        "model": data.get("model"),
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0)})
    # Reviewer-verdict and missing-status-block capture live in
    # capture_post_spawn (PostToolUse on Agent/Task), NOT here (dogfood
    # finding: this event's payload proved unreliable in practice —
    # transcript-path ambiguity, silent no-ops — and the review ledger is
    # FSM-guard-critical, so it anchors to the one payload that carries
    # the spawn prompt and the final reply deterministically). This event
    # keeps only the best-effort token accounting above.


def _response_text(resp) -> str:
    """The Agent tool's PostToolUse `tool_response`, flattened to text —
    tolerant of every plausible encoding (plain string, content-block
    list, {content: …} wrapper) since the exact shape is undocumented.

    Content blocks join with NEWLINE, not "" (adversarial-review finding:
    a block ending without a trailing newline glued `verdict: APPROVED`
    onto the previous line, silently dropping a real approval from the
    line-anchored VERDICT_RE)."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return "\n".join(_response_text(x) for x in resp)
    if isinstance(resp, dict):
        if "content" in resp:
            return _response_text(resp["content"])
        return str(resp.get("text") or "")
    return ""


def capture_post_spawn(p: dict) -> None:
    """PostToolUse on Agent/Task — the authoritative writer of the
    reviewer-verdict ledger (reviews.ndjson) and missing-status-block
    events. Anchored here, not SubagentStop (dogfood finding: an entire
    run's SubagentStop captures silently no-opped — payload shape is
    version-dependent and its transcript_path is documented-ambiguous),
    because THIS payload deterministically carries the spawn prompt
    (tool_input.prompt: the mandated headers) and the subagent's final
    reply (tool_response) — the exact two inputs the capture needs."""
    tool_input = p.get("tool_input") or {}
    shape = shape_of(tool_input.get("subagent_type"))
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    if shape not in surfaces["shapes"]:
        return  # not a harness shape — none of our business
    cwd = _session_workspace(Path(p.get("cwd") or "."))
    runs = live_runs(cwd)
    if not runs:
        return
    prompt = tool_input.get("prompt") or ""
    run = _resolve_run(runs, prompt, cwd) or (runs[0] if len(runs) == 1 else None)
    if run is None:
        print(f"ai-sdlc-harness: could not attribute a subagent reply to one of "
              f"{len(runs)} live runs (no matching harness-run: header) — "
              "review-verdict/stall capture for this invocation is not "
              "recorded.", file=sys.stderr)
        return
    task = (TASK_HEADER_RE.search(prompt) or [None, None])[1]
    mode = (MODE_HEADER_RE.search(prompt) or [None, None])[1]
    if tool_input.get("run_in_background") in (True, "true", "True"):
        # A background spawn's tool_response is only the launch stub — the
        # subagent's real reply never reaches any hook payload, so capture
        # is impossible here (an APPROVED verdict would be lost, and the
        # stub's missing status block FABRICATED a stall event whose
        # reinvoke then raced the still-live background original).
        # guard_spawn now blocks these up front; this branch is
        # belt-and-braces for an older guard copy — record what actually
        # happened instead of fake stall evidence.
        ndjson.append_record(run / "events.ndjson", {
            "kind": "background-spawn-uncaptured", "task": task,
            "actor": shape, "mode": mode,
            "reason": "harness-shape spawn ran in the background — only the "
                      "launch stub reaches PostToolUse, so verdict/status "
                      "capture is impossible; re-spawn in the FOREGROUND "
                      "(batch multiple foreground spawns in one message for "
                      "parallelism)"})
        return
    text = _response_text(p.get("tool_response"))
    if shape == "reviewer":
        verdict = extract_verdict(text)
        if verdict:
            # The completion evidence the task FSM's `reviewer-approved`
            # guard requires: which task (spawn-prompt header), which
            # reviewer mode, what verdict. Written only here; scoped to the
            # final status block and conflict-fail-closed (extract_verdict).
            ndjson.append_record(run / "reviews.ndjson", {
                "task": task, "mode": mode, "verdict": verdict})
        elif VERDICT_ANYWHERE_RE.search(text):
            # A verdict exists but isn't line-anchored in the final status
            # block — correctly NOT captured (fail-closed), but say so and
            # name the one recovery that works: a fresh foreground spawn.
            # SendMessage/resume replies pass through no capture hook, so
            # a restated verdict there can never register (field finding).
            ndjson.append_record(run / "events.ndjson", {
                "kind": "verdict-uncaptured", "task": task, "actor": shape,
                "reason": "a verdict: token appears in the reply but not as "
                          "its own line in the final status block — not "
                          "captured (fail-closed). Recover by re-spawning "
                          "the reviewer FRESH in the foreground (same "
                          "headers); never SendMessage/resume — those "
                          "replies bypass capture entirely"})
    if not STATUS_RE.search(text):
        ndjson.append_record(run / "events.ndjson", {
            "kind": "missing-status-block", "task": task, "actor": shape,
            "reason": "subagent replied without a status block — "
                      "stalled-agent procedure applies (coverage B4)"})


GUARDS = {"bash": guard_bash, "write": guard_write, "read": guard_read,
          "spawn": guard_spawn,
          "skill": guard_skill, "user-prompt": capture_user_prompt,
          "subagent-stop": capture_subagent_stop,
          "post-spawn": capture_post_spawn}
FAIL_OPEN = {"bash", "write", "read", "user-prompt", "subagent-stop",
             "post-spawn"}


def _debug_dump(name: str, payload: dict) -> None:
    """HARNESS_HOOK_DEBUG=1 (set when launching Claude Code) appends every
    hook invocation's raw payload to ~/.ai-sdlc-harness-hook-debug.ndjson —
    the one-flag diagnosis path for "a hook isn't doing what I expect"
    (dogfood-run finding: capture_subagent_stop returned silently for an
    entire session and nothing recorded WHY; payload-shape questions are
    unanswerable after the fact without this)."""
    import os
    if os.environ.get("HARNESS_HOOK_DEBUG") != "1":
        return
    try:
        ndjson.append_record(Path.home() / ".ai-sdlc-harness-hook-debug.ndjson",
                             {"guard": name, "payload": payload})
    except OSError:
        pass  # debug aid only — never let it affect the guard


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    guard = GUARDS.get(name)
    if guard is None:
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0 if name in FAIL_OPEN else 2)
    _debug_dump(name, payload)
    try:
        guard(payload)
    except SystemExit:
        raise
    except YamlMissing as exc:
        # Pre-setup degradation: without PyYAML nothing harness-y can execute
        # anyway (the harness CLI needs it too), so degrading these guards
        # open cannot enable a harness action — one quiet line, no traceback,
        # never a per-prompt error storm.
        print(exc, file=sys.stderr)
        sys.exit(0)
    except Exception as exc:
        if name in FAIL_OPEN:
            # Open, but never SILENT (dogfood A2 finding: a deterministic
            # TypeError in the token capture was swallowed here on every
            # single spawn — no stderr, no ledger trace; the failure was
            # only findable by replaying a captured payload by hand).
            print(f"ai-sdlc-harness: guard '{name}' errored (fail-open): "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(0)
        block(f"guard '{name}' failed closed: {exc}")
    sys.exit(0)


if __name__ == "__main__":
    main()
