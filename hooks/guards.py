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

If PyYAML is missing, the yaml-needing guards DEGRADE OPEN with one visible
remediation line on stderr (exit 0 — see main()'s YamlMissing handler; the
yaml-free bash/write guards keep blocking); init-workspace verifies the
dependency up front, and the HMAC chain (RC4) still detects authority-file
tampering even with guards down — defense in depth, guard = fast-fail,
chain = guarantee. The same posture covers a missing INTERPRETER: if the
hook launcher pair (hooks/run-guard + run-guard.cmd, registered in
hooks.json) finds no runnable python at all — including
the Windows Store alias that answers to `python`/`python3` but only prints
an install nag — the hook errors non-2 and the platform treats it as
non-blocking. Accepted: pre-venv, nothing harness-y can execute anyway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

# stdlib-only modules at import time. `transitions` qualifies: it imports
# nothing but json/sys/pathlib plus these two, and reaches for PyYAML only
# lazily, inside the declared-data readers this file calls from paths that
# have already loaded surfaces.yaml (i.e. already required it). `gates`
# qualifies outright — hashlib + re, no I/O, no env — and is imported for
# `session_digest` alone, so the capture hook and the CLI that writes the
# stamp cannot drift into two different hashes of the same session id.
from harness import chain, gates, ndjson, qwen_cli_detected, transitions  # noqa: E402


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


_TERMINAL_CACHE: tuple = ()


def _terminal_statuses() -> tuple:
    """The task FSM's declared terminal statuses — the SAME `terminal:` list
    `harness.transitions.terminal_statuses` reads, not a second copy. A guard
    that disagreed with the engine about what "finished" means would refuse a
    spawn the engine considers legal, or wave one through that it doesn't."""
    global _TERMINAL_CACHE
    if not _TERMINAL_CACHE:
        fsm = load_yaml(PLUGIN_ROOT / "pipeline" / "task-fsm.yaml") or {}
        _TERMINAL_CACHE = tuple(fsm.get("terminal") or ())
    return _TERMINAL_CACHE

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
# On Windows the separator class also admits `\`: the Bash tool there is
# Git Bash, whose msys runtime maps a QUOTED backslash path ("ai\r1\
# state.yaml") onto the same file a forward-slash spelling reaches — so a
# backslash-spelled authority write must block identically. POSIX keeps the
# `/`-only form, bit-identical to before (every nt-conditional in this
# module follows that rule: Windows support must not move POSIX behavior).
_SEP = r"[/\\]" if os.name == "nt" else "/"
_NOT_SEP = r"[^/\\\s'\"]" if os.name == "nt" else r"[^/\s'\"]"
AUTHORITY_RE = re.compile(
    r"ai" + _SEP + _NOT_SEP + "+" + _SEP +
    r"(state\.yaml|events\.ndjson|tokens\.ndjson|"
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
# nt additionally captures drive-letter absolutes (`C:/x`, quoted `C:\x`) —
# without them a developer's `sed -i D:/other/repo/x` carried no visible
# absolute target at all (confinement silently fail-open) and a reviewer's
# `rm C:/Temp/x` swept zero absolute tokens (fail-closed false block). The
# quoted forms accept `\` only BEHIND a drive letter: a bare `"\section{x}"`
# (TeX/grep prose) must not become a phantom write target. POSIX keeps the
# original `/`-only pattern so its match set cannot move.
# The quoted forms also admit `\\server\…` UNC (double backslash — still
# structurally distinct from `"\section{x}"` prose): without it, a
# developer's `rm "\\server\share\x"` swept ZERO tokens and the
# confinement never ran — the one fail-open the adversarial review found.
_ABS_TOKEN_RE = re.compile(
    r"\"((?:[A-Za-z]:[/\\]|\\\\|/)[^\"]*)\"|'((?:[A-Za-z]:[/\\]|\\\\|/)[^']*)'"
    r"|(?<![\w/])((?:[A-Za-z]:)?/[^\s;|&<>]+)"
) if os.name == "nt" else re.compile(
    r"\"(/[^\"]*)\"|'(/[^']*)'|(?<![\w/])(/[^\s;|&<>]+)")
_PROG_WRITE_RE = re.compile(_PROG_WRITE)
# targets that are always fine regardless of the allowed roots
_BASH_WRITE_SINK_OK = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")

_QUOTED_SPAN_RE = re.compile(r"\"[^\"]*\"|'[^']*'")


# /x or /x/… where x is one drive letter — Git Bash's drive mounts
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/|$)")


def _bash_path(tgt: str) -> Path:
    """A bash-command path token, as the EXECUTING shell will resolve it.
    On Windows the Bash tool runs under Git Bash, and two of its mount
    spellings must map to where the write actually goes:

    - `/tmp/…` IS the user temp directory — so a literal `/tmp` target
      lands in genuine scratch there, not in `<drive>:\\tmp`. Without the
      translation the reviewer's `tee /tmp/build.log` idiom (the field
      pain the scratch allowance exists for) false-blocks on every
      Windows host, since `Path('/tmp/x')` is not even absolute under
      Windows path semantics.
    - `/c/Users/…` IS `C:\\Users\\…` — the spelling Git Bash's own `pwd`
      emits, so it shows up naturally in developer commands. Untranslated
      it mis-resolves against the cwd drive (`D:\\c\\Users\\…`) and
      false-blocks legitimate in-repo/worktree writes (adversarial-review
      finding on this change).

    Other msys mounts (`/etc`, `/usr`, …) stay untranslated: they live
    inside the Git installation, never inside a registered repo, so the
    rootless-target gate's fail-closed handling is the right answer for
    them. POSIX: identity."""
    if os.name == "nt":
        # casefolded prefix test: msys mounts are case-insensitive —
        # `cygpath -w /TMP/x` lands in the same temp dir (review finding)
        low = tgt.lower()
        if low == "/tmp" or low.startswith("/tmp/"):
            return Path(tempfile.gettempdir(), tgt[5:].lstrip("/"))
        m = _MSYS_DRIVE_RE.match(tgt)
        if m:
            return Path(m.group(1).upper() + ":/", tgt[3:])
    return Path(tgt)


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
        path = _bash_path(tgt)   # nt: /tmp/… is Git Bash's temp mount
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


#: `_CMD_GAP` stops at a newline, but a shell LINE CONTINUATION is still one
#: command — and the step files themselves render long `harness` invocations
#: with trailing `\`. Every alternative using this gap therefore spans it
#: (re-verification finding: `harness artifact \`⏎`  --name …` sailed through
#: while the identical one-line spelling blocked; the three older verbs were
#: exposed to the same evasion and are widened with it).
_CMD_GAP_NL = r"(?:[^|;&\n\r]|\\\r?\n)*"
# Manual invocation of the CAPTURE hook entry points — blocked for every
# actor, orchestrator included (piping a synthetic UserPromptSubmit
# payload into guards.py directly would append a gate-approval record to
# human-input.ndjson indistinguishable from the human typing it — the
# ledgers' sole protection is that ONLY the platform fires these). The
# guard verbs (bash/write/read/spawn/skill) are not listed: invoking them
# manually can only ever BLOCK. Every SPELLING that reaches the dispatcher
# must be anchored here, not just guards.py itself: the run-guard launcher
# pair execs guards.py byte-for-byte, so an unanchored launcher spelling
# is a clean bypass (adversarial-review finding on the launcher change,
# CONFIRMED live — `run-guard user-prompt` allowed where
# `guards.py user-prompt` blocked).
#
# `_CMD_GAP_NL`, not `_CMD_GAP`: the newline-continuation evasion fixed for
# the registration verbs below was still open on THESE verbs (adversarial
# review, executed — the backtick-newline spelling
# `run-guard \`⏎  subagent-stop` exited 0 while the one-line spelling
# blocked). It matters more here than there now that capture writes
# verdicts: a forged `subagent-stop` payload can mint an APPROVED row in
# reviews.ndjson, which is the record the task FSM's `reviewer-approved`
# guard reads — evidence forgery, not merely an unearned registration.
HOOK_FORGE_RE = re.compile(
    r"\b(?:guards\.py|run-guard(?:\.cmd)?)\b" + _CMD_GAP_NL +
    r"\b(?:user-prompt|post-spawn|subagent-stop)\b")
PLANNER_STAMP_RE = re.compile(
    r"\bharness\b" + _CMD_GAP + r"\brepo-map-stamp\b")
# Registration verbs are orchestrator-only: the scope is the HUMAN's
# confirmation recorded by the orchestrator, and the task list is what the
# plan gate ratifies — a subagent shape minting either from inside its own
# spawn would anchor "user-confirmed" to nothing (adversarial-review,
# plan-accuracy round: the intake planner has Bash and is live at exactly
# the cursors where these verbs are legal).
SUBAGENT_REGISTER_RE = re.compile(
    r"\bharness\b" + _CMD_GAP_NL + r"\b(?:scope-register|plan-register|"
    # confirm-repo is the same class of fact as scope-register — it records
    # that a HUMAN was asked which repo a quick run targets, and it is the
    # sole writer of the `repo_confirmed` marker the cursor is gated on. A
    # subagent minting it would answer the question on the user's behalf and
    # unblock the run, which is precisely the failure the step exists to
    # prevent; `always_legal_spawns` keeps a Bash-capable request-triage
    # reviewer spawnable at ANY cursor, so this is a reachable path, not a
    # theoretical one.
    r"confirm-repo|"
    # save-report joins the orchestrator-only set (pre-release adversarial
    # review, both lenses independently): reports/ under a run is
    # GATE-PRESENTED evidence — an exhausted plan-review decision rests on
    # reports/plan-review.md. This comment used to add "and before this verb
    # existed no subagent had any path into it (Write/Edit and bash-write
    # confinement both block the directory)"; the whole-branch adversarial
    # pass reproduced that as FALSE for the planner, whose write confinement
    # admits all of `ai/` — see the reports/ rule in `guard_write`, which
    # closes the Write half the claim was resting on.
    # A reviewer that "helpfully" persisted its own reply would
    # both author the evidence the human reads AND, via the snapshot
    # immutability check, wedge the orchestrator's own documented save.
    r"save-report)\b"
    # `artifact` joins them (adversarial-review, whole-branch pass): blocking
    # the WRITE half of report persistence leaves the REGISTRATION half open,
    # and registration is what a gate actually reads. `set_artifact` validates
    # only that the name is in the live step's `produces` (transitions.py) —
    # and the reviewer is alive exactly while the cursor sits on `plan-review`,
    # whose `produces` includes `plan-review-report`. A flag-only `harness`
    # invocation trips neither the bash-write guard nor AUTHORITY_RE (which
    # names this verb as the SANCTIONED path to state.yaml — sanctioned for
    # the orchestrator). Every documented caller is a step file, i.e. the
    # orchestrator recording what a returned spawn produced.
    #
    # Anchored on the `--name` it always carries rather than on the bare word:
    # the gap spans the whole command, so a lone `\bartifact\b` would also
    # fire on an unrelated path like `--body-file /tmp/artifact.md`. The verb
    # cannot simply be required to follow `harness` directly — global flags
    # legally precede it (`harness --workspace X artifact …`).
    #
    # `--n`/`--na`/`--nam` are matched too: argparse's default `allow_abbrev`
    # accepts all three, so each is a real invocation of this verb and a
    # one-character bypass of a rule whose whole point is being unbypassable
    # (re-verification finding).
    r"|\bharness\b" + _CMD_GAP_NL + r"\bartifact\b" + _CMD_GAP_NL
    + r"--n(?:a(?:m(?:e)?)?)?\b")
# `(?!<)` after the colon: spawn prompts routinely quote
# shared/status-block.md's reply template verbatim as instructions to the
# subagent — including its literal `harness-task: <task-id or ->` example —
# alongside the orchestrator's own real headers. A real header value is
# never a `<placeholder>`, so this keeps `re.search` (first match wins)
# from treating the quoted example as the real header.
MODE_HEADER_RE = re.compile(r"^harness-mode:\s*(?!<)(\S+)", re.MULTILINE)
TASK_HEADER_RE = re.compile(r"^harness-task:\s*(?!<)(\S+)", re.MULTILINE)
# The OTHER side of that `(?!<)`: a `harness-task:` line whose value IS a
# placeholder. Capture-side semantics are unchanged (an unsubstituted
# placeholder must still parse as "no task", or a pending would be filed
# under the literal string `<task-id>`); this pattern exists only so
# `guard_spawn` can BLOCK a prompt that carries nothing but the placeholder.
# It is not hypothetical: skills/dev-workflow/steps/develop.md and
# agents/developer.md both print `harness-task: <task-id>` verbatim as the
# header block to send, so copying that block without substituting produces
# a spawn the whole downstream chain treats as task-LESS — no id validation,
# no (task, mode) serialization, and a verdict captured against None that
# `task --to done` can never read.
TASK_PLACEHOLDER_RE = re.compile(r"^harness-task:[ \t]*(<[^\n]*)",
                                 re.MULTILINE)
# `harness-run`'s value is a filesystem PATH, which CAN contain spaces
# (field report: a workspace under `.../AI Engine/...` truncated at the
# first space with a `\S+` capture, so the resolved run never matched any
# live run and every harness-shape spawn was blocked as "does not match any
# active run"). Capture the REST OF THE LINE (`.` excludes newline) with
# trailing whitespace trimmed; the `(?![ \t<])` still skips the quoted
# `<run-dir>` placeholder AND won't let a leading space satisfy it. The
# other headers carry single-token values (mode/task/status names never
# contain spaces), so `\S+` stays correct there — and rightly refuses to
# swallow trailing prose like `harness-status: SUCCESS — all good`.
RUN_HEADER_RE = re.compile(r"^harness-run:[ \t]*(?![ \t<])(.*\S)", re.MULTILINE)
# The `(?![^\n]*\|)` guard makes the TEMPLATE's own line — `harness-status:
# SUCCESS | PARTIAL | FAILED`, now inlined verbatim in the agent defs —
# regex-invisible when echoed in a reply (same placeholder-invisibility
# convention as the angle-bracket verdict; adversarial-review on this
# change: an echoed template line satisfied the block check and silently
# disabled stall detection for that reply). A real status value is one
# token with no `|` after it on the line.
STATUS_RE = re.compile(r"^harness-status:[ \t]*(\S+)(?![^\n]*\|)",
                       re.MULTILINE)
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
# Optional convergence signal (shared/status-block.md): how many findings the
# reviewer is BLOCKING on. field: dual-run comparison — verdict rows carried
# only mode/verdict/at, so nothing machine-readable recorded whether a review
# panel was converging. That run's own retro then misstated its final round
# ("0 blocking" where the artifacts show 2), and the human gate at
# `exhausted` had no one-glance framing that the real trajectory had been
# 9 → 7 → 2 → 2. Same lenient shape as VERDICT_RE (bold/indent tolerated),
# same placeholder-invisibility guard so the quoted template's own
# `blocking-findings: <N>` line never reads as a real count.
BLOCKING_RE = re.compile(r"^[ \t]*\**blocking-findings:\**[ \t]*(?!<)(\d+)\b",
                         re.MULTILINE)
# The reviewer modes whose captured verdict the ENGINE actually reads:
# `review` → the task FSM's reviewer-approved completion guard;
# `plan-review` → the manifest's verdict_bound exits. Every other reviewer
# mode's verdict is advisory (plan-attack lens verdicts are explicitly
# engine-invisible; pre-pr / analyze-comments / request-triage deliver
# report content, not a verdict) — so for those, a blockless reply is a
# genuine stall even when a verdict token was captured: the deliverable is
# likely missing (adversarial-review on this change, gaps lens).
ENGINE_VERDICT_MODES = ("review", "plan-review")


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


#: Credential shapes scrubbed from a logged `attempt` (see _blocked_context).
#: Deliberately broad — over-redacting an audit string costs nothing, while
#: one leaked token in a ledger that ships with the run costs a rotation.
_SECRET_PATTERNS = (
    re.compile(r"://[^@/\s]*:[^@/\s]*@"),                 # user:pass@host
    re.compile(r"\b(?:glpat|ghp|gho|ghu|ghs|ghr|github_pat|sk|xox[baprs])"
               r"[-_][A-Za-z0-9_\-]{8,}"),                # common token prefixes
    re.compile(r"(?i)\b(?:bearer|token|api[-_]?key|password|passwd|secret)"
               r"\b[=:\s]+\S+"),
)


def _blocked_context(p: dict, workspace: Path, runs: list[Path]) -> dict:
    """What was actually ATTEMPTED, for a `hook-blocked` event.

    field: dual-run comparison — one run logged four reviewer read-only
    violations of the same class, but the event carried only the guard's
    message, so there was nothing to coach against and no way to pattern-
    match recurrence across runs. The guard message says which RULE fired;
    this says what the agent tried to do.

    Path-sanitized like `show`'s probe_error (3.2.0 precedent): these events
    are read in shared reports, and a local filesystem layout has no
    business there. Run prefixes are replaced BEFORE the workspace prefix —
    runs live under the workspace, so scrubbing the shorter path first would
    strand the run-dir tail. Truncated: a blocked heredoc can be enormous
    and the ledger is an audit trail, not a transcript."""
    tool_input = p.get("tool_input") or {}
    attempt = (tool_input.get("command")
               or tool_input.get("file_path") or tool_input.get("path") or "")
    if not attempt and p.get("tool_name") in ("Task", "agent"):
        attempt = str(tool_input.get("subagent_type") or "")
    attempt = str(attempt)
    for raw, tag in ([(r, "<run>") for r in runs] + [(workspace, "<workspace>")]):
        for form in (str(Path(raw).resolve()), str(raw)):
            attempt = attempt.replace(form, tag)
    # Credentials BEFORE truncation (re-verify finding): the raw-git guard is
    # the one most likely to fire on a token-bearing command line
    # (`git push https://oauth2:glpat-…@host/…`), and events.ndjson is an
    # unsealed file that travels with the run and gets pasted into reports.
    for pattern in _SECRET_PATTERNS:
        attempt = pattern.sub("<redacted>", attempt)
    if len(attempt) > 300:
        attempt = attempt[:300] + "…"
    prompt = str(tool_input.get("prompt") or "")
    m = MODE_HEADER_RE.search(prompt)
    return {"tool": p.get("tool_name"),
            "attempt": attempt or None,
            "role": shape_of(p.get("agent_type")) or None,
            "mode": m.group(1) if m else None}


def block(msg: str, cwd: Path | None = None, payload: dict | None = None) -> None:
    """Adversarial-review finding: hook blocks were never logged anywhere,
    despite design.md documenting them and metrics_report/status already
    filtering for a `hook-blocked` event kind that could never occur. Logs
    to the ONE live run when there's exactly one — unambiguous; with zero
    or several, logging would either have nowhere to go or risk attributing
    to the wrong sibling run (the same misattribution class Group 3 fixed
    for subagent-stop), so it's skipped rather than guessed. The block
    itself always happens regardless — logging is best-effort, never a
    precondition for it.

    `payload` adds the attempted command/path + role/mode (see
    `_blocked_context`); omitting it degrades to the message-only record
    this always wrote."""
    if cwd is not None:
        try:
            workspace = _session_workspace(cwd)
            runs = live_runs(workspace)
            if len(runs) == 1:
                record = {"kind": "hook-blocked", "reason": msg}
                if payload is not None:
                    # never let context-building sink the block's own logging
                    try:
                        record.update(_blocked_context(payload, workspace, runs))
                    except Exception:
                        pass
                ndjson.append_record(runs[0] / "events.ndjson", record)
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
                # `update-base` is named explicitly, and the ordering of the
                # two update verbs matters: a human blocked on `git pull`
                # while standing on a stale BASE used to read this list and
                # reach for `sync-branch`, which rebases the CURRENT branch
                # onto a base — the wrong tool, and until update-base existed
                # the only one offered (field: US-CHAT-01 lean run). A block
                # message that cannot name the terminating remedy is half the
                # bug it is trying to prevent.
                block(f"raw `git {m.group(1)}` is blocked (RC1): commits, history "
                      "rewrites, and remote updates go through the owned entry points — "
                      "`harness commit`, `harness merge-task`, `harness push`, "
                      "`harness publish-mirror`, and for updates: "
                      "`harness update-base` (fast-forward a BASE branch onto "
                      "its remote) or `harness sync-branch` (rebase the "
                      "CURRENT branch onto a base that moved).", cwd, p)
        if AUTHORITY_RE.search(target) and WRITE_HINT_RE.search(target):
            block("run-authority files mutate only via the owned entry points — "
                  "`harness cursor` / `harness task` / `harness gate` / "
                  "`harness artifact` / `harness log-event` (RC1) — direct writes "
                  "are blocked. state.yaml and red-proofs are also chain-sealed "
                  "(RC4 detects out-of-band edits); the append-only evidence "
                  "ledgers (human-input.ndjson, reviews.ndjson) are NOT sealed, "
                  "so this guard is their sole protection.", cwd, p)
        if HOOK_FORGE_RE.search(target):
            block("the capture hook entry points (user-prompt / post-spawn / "
                  "subagent-stop) are fired by the platform ONLY — invoking "
                  "them manually can forge captured evidence (a synthetic "
                  "payload would mint a gate approval or reviewer verdict "
                  "indistinguishable from the real one). If a capture seems "
                  "broken, diagnose read-only and report; never execute the "
                  "hook yourself.", cwd, p)
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
                  "on PATH nor allow-listed.", cwd, p)
        if shape_of(p.get("agent_type")) == "reviewer":
            why = _reviewer_bash_write_violation(target, cwd)
            if why:
                block("the reviewer is read-only (design.md piece 3): builds/"
                      "tests may run, and capturing their output under /tmp "
                      "(or > /dev/null) is fine — but "
                      f"{why} mutates outside scratch and is blocked. If "
                      "this fired on a grep/search whose PATTERN contains "
                      "'>' or '<>', QUOTE the pattern — unquoted, the shell "
                      "reads it as a redirect (field: a C# compiler "
                      "closure name '<>c__DisplayClass' tripped exactly "
                      "this).", cwd, p)
        if shape_of(p.get("agent_type")) == "developer":
            # bash-side analogue of the Write/Edit confinement: a write
            # targeting an ABSOLUTE path outside the developer's allowed
            # roots is cross-boundary drift (the Write/Edit guard already
            # blocks it; without this a developer could sed/redirect around
            # that). Relative targets land in cwd (residual, see the
            # _developer_bash_write_targets note).
            for tgt in _developer_bash_write_targets(target):
                # `tpath`, never `p` — `p` is this guard's payload param
                tpath = _bash_path(tgt)  # nt: /tmp/… is Git Bash's temp mount
                # nt: a rootless '/etc/x' target is a Git Bash msys mount —
                # definitely OUTSIDE every drive-lettered repo/worktree
                # root, so it must not slip past the is_absolute() gate as
                # if it were a relative (in-cwd) write
                nt_rootless = (os.name == "nt" and not tpath.is_absolute()
                               and tgt.startswith("/"))
                if tgt in _BASH_WRITE_SINK_OK or not (tpath.is_absolute()
                                                      or nt_rootless):
                    continue
                resolved = tpath.resolve()
                if not _developer_write_ok(resolved,
                                           _session_workspace(cwd).resolve()):
                    block("developer bash writes are confined to a registered "
                          f"repo or its worktree — '{tgt}' is outside it "
                          "(design.md piece 3 path-guard). Author via Write/"
                          "Edit or write inside your `harness-repo` worktree.",
                          cwd, p)
                # same subtree scoping as the Write/Edit surface — otherwise
                # `sed -i <worktree>/package.json` walks straight around it,
                # and a silently-dropped edit is exactly what this refuses
                reason = _worktree_scope_reason(
                    resolved, _session_workspace(cwd).resolve())
                if reason:
                    block(reason, cwd, p)
                # same test-first ordering as the Write/Edit surface —
                # otherwise `sed -i src/main/...` bypasses it on day one
                reason = _tdd_block_reason(
                    resolved, _session_workspace(cwd).resolve())
                if reason:
                    block(reason, cwd, p)
        if shape_of(p.get("agent_type")) == "planner":
            if PLANNER_STAMP_RE.search(target):
                block("the planner never stamps its own repo-map output — "
                      "`repo-map-stamp` is the orchestrator's job, run once "
                      "after the planner's spawn returns (agents/planner.md).",
                      cwd, p)
            # Bash half of guard_write's gate-evidence rule. The reviewer and
            # the developer each have a bash-write confinement above; the
            # planner had none, so the Write/Edit-only first cut of that rule
            # left `echo >`, `tee`, `cp` and `python -c "open(...,'w')"`
            # reaching <run>/reports/ untouched — all four reproduced by the
            # re-verification pass, and all four already blocked for the two
            # shapes that were never the hole.
            #
            # Unlike the developer branch, RELATIVE targets are resolved
            # rather than skipped: a planner's cwd IS the workspace, so
            # `> ai/<run>/reports/x.md` is the natural spelling here, not the
            # cross-boundary absolute path that branch is watching for.
            for tgt in _developer_bash_write_targets(target):
                if tgt in _BASH_WRITE_SINK_OK:
                    continue
                reason = _gate_evidence_write_reason(
                    _resolve_write_path(_bash_path(tgt).as_posix(), cwd),
                    _session_workspace(cwd).resolve())
                if reason:
                    block(reason, cwd, p)
        if shape_of(p.get("agent_type")) and SUBAGENT_REGISTER_RE.search(target):
            block("scope-register, confirm-repo, plan-register, save-report "
                  "and artifact are orchestrator-only: the scope records the "
                  "HUMAN's confirmation, confirm-repo records which repo the "
                  "human said this run targets, the task list is what the "
                  "plan gate ratifies, a run's reports/ are the evidence a "
                  "human gate presents, and `artifact` is what makes that "
                  "evidence the gate's — report your proposal/review in your "
                  "status block; the orchestrator confirms, registers, and "
                  "persists.", cwd, p)


# ------------------------------------------------- Write/Edit path guards

def _tmp_roots() -> tuple[Path, ...]:
    """Scratch roots a confined shape may write to, RESOLVED — on macOS
    `/tmp` is a symlink to `/private/tmp`, and `_resolve_write_path`
    resolves symlinks, so the un-resolved literal `Path("/tmp")` never
    matched anything there (adversarial-review finding: the whole /tmp
    allowance was dead code on Darwin — every scratchpad write by a
    developer/planner blocked). On POSIX deliberately NOT
    `tempfile.gettempdir()`: TMPDIR can be anywhere (and a workspace living
    under it — common in tests — would swallow the whole confinement).

    Windows has no fixed `/tmp` at all, so there `gettempdir()` (%TEMP%) IS
    the platform scratch root — with the same swallowing hazard accepted
    knowingly: a workspace under %TEMP% is the NORM for tests on Windows,
    and `_is_scratch_write`'s workspace/registered-repo exclusions (added
    for exactly the Linux workspace-under-/tmp case) are what keep the
    confinement real there, not the root's location."""
    if os.name == "nt":
        return (Path(tempfile.gettempdir()).resolve(),)
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
    # BOTH directions, not just descendant-ness (adversarial-review finding
    # on the Windows port, but the hole was cross-platform): the workspace
    # commonly lives UNDER the scratch root (Linux mkdtemp, Windows %TEMP%
    # as the norm), so an ANCESTOR target — `rm -rf /tmp` itself — passed
    # the descendant checks and was sanctioned as scratch while the sweep
    # would take the workspace, its ledgers, and any temp-resident repo
    # with it.
    if path.is_relative_to(workspace) or workspace.is_relative_to(path):
        return False
    return not any(path.is_relative_to(r) or r.is_relative_to(path)
                   for r in _registered_repos(workspace))


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


# ---------------------------------- physical checkout of a registered repo

_TOPLEVEL_CACHE: dict[Path, Path] = {}


def _physical_toplevel(repo: Path) -> Path:
    """The checkout `repo` lives in — `repo` itself for an ordinary root
    registration, the enclosing checkout when `repo` is a SUBTREE of one.

    A registered repo path is no longer necessarily a checkout root:
    `initws.discover`'s monorepo split registers subtrees as logical repos of
    their own (`<ws>/mono/frontend` beside `<ws>/mono`, one `.git` between
    them — the .NET case where the `.sln` at the root is one repo and the
    frontend app another). `gitops.worktree_add` names and places the
    per-task worktree after the PHYSICAL TOPLEVEL — `<ws>/mono-wt-<task>-
    <uid>` — with the logical repo at `<worktree>/<prefix>` inside it. So the
    `-wt-` sibling allowance below has to be anchored HERE and not at the
    registered path: for a subtree registration the worktree is neither
    inside `repo` nor a `<repo.name>-wt-` sibling under `repo.parent`, and
    every developer write into its own worktree was blocked (it passed only
    when the enclosing root repo happened to ALSO be registered — luck, not
    correctness).

    Derived by STAT, not by `git rev-parse --show-toplevel`. guards.py runs
    as a fresh process on EVERY tool call and shells out nowhere today (it
    imports no subprocess at all), so asking git would put a process spawn on
    the hot path of every single write — the one cost this file is not
    allowed to add. `<dir>/.git` is the same marker git itself discovers a
    toplevel by (a directory in a normal checkout, a FILE in a linked
    worktree or a submodule — `.exists()` covers both), and the walk stops at
    the first one found. The trade-offs taken knowingly:
      - a root registration carries its own `.git`, so the walk stops on its
        first stat and `top == repo`: root-repo behaviour is byte-identical,
        which is the fail-closed bias this guard is held to (the allowance
        must never widen for the ordinary shape);
      - no marker anywhere up the chain (a registration that is not inside a
        checkout, an unreadable parent) falls back to `repo` — today's
        behaviour verbatim, never a wider one. init-verify already holds
        every registration to being inside a real work tree
        (`gitops.work_tree_root`, rev-parse), so this fallback is the
        already-broken-config case, not a supported shape;
      - the spellings a stat cannot see (GIT_DIR / GIT_CEILING_DIRECTORIES
        aimed elsewhere) degrade to that same fallback rather than to a
        wrong answer.
    Cached per process: one guards run can check several paths (the bash
    guard sweeps every absolute token of a destructive command), and the
    answer for a given registration cannot change mid-process."""
    cached = _TOPLEVEL_CACHE.get(repo)
    if cached is not None:
        return cached
    top = repo
    for cand in (repo, *repo.parents):
        try:
            if (cand / ".git").exists():
                top = cand
                break
        except OSError:      # unreadable ancestor — stop, keep the fallback
            break
    _TOPLEVEL_CACHE[repo] = top
    return top


def _worktree_bases(repo: Path) -> tuple[Path, ...]:
    """Directory names a per-task worktree of `repo` may be a `-wt-` sibling
    of. The registered path stays FIRST and unconditional — old run state and
    any worktree cut before subtree support are named after it — with the
    physical toplevel added only when the two genuinely differ, so a root
    registration produces the exact one-element tuple the pre-subtree code
    checked."""
    top = _physical_toplevel(repo)
    return (repo,) if top == repo else (repo, top)


def _developer_write_ok(path: Path, workspace: Path) -> bool:
    """A developer may write inside a registered repo, inside one of its
    per-task worktree siblings (`worktree_add`: `<toplevel.parent>/<toplevel.
    name>-wt-<task>-<uid>`, where the toplevel is the registered path itself
    for a root repo and the enclosing checkout for a subtree one — see
    `_worktree_bases`), or in /tmp — nothing else. Derived from `repos.yaml`
    under the workspace; a spaced repo path (`.../AI Engine/...`) is
    handled by Path semantics, not a regex.

    ONE of two write gates, not the whole confinement: this is the state-free
    territorial check, and `_worktree_scope_reason` narrows a worktree
    allowance to the logical repo the task actually commits from. Both run,
    in that order, on the Write/Edit and the bash surface alike.

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
        for base in _worktree_bases(repo):
            parent = base.parent
            if path.is_relative_to(parent):
                rel = path.relative_to(parent)
                if rel.parts and rel.parts[0].startswith(base.name + "-wt-"):
                    # The whole worktree passes THIS gate, which is the
                    # state-free territorial question: is the path in a
                    # registered repo or a worktree of one at all. Which
                    # SUBTREE of that worktree the task may write in is a
                    # second, finer question — `_worktree_scope_reason`, run
                    # right after this on both surfaces, and the one that
                    # closes the silent-loss bug that an earlier cut of this
                    # comment wrongly argued away ("an edit dropped in a
                    # sibling logical repo's directory can never reach
                    # history" was true and was precisely the harm: it is
                    # dropped, not rejected). It is kept separate rather than
                    # folded in here because the two fail open on different
                    # things: this one on an undeterminable repo set, that one
                    # on undeterminable run state — and because a bool cannot
                    # carry the reason a developer needs to hear, which is not
                    # "you are outside your worktree" but "you are inside it,
                    # and this file still cannot survive".
                    return True
                near_a_repo = True  # sibling of THIS repo, not its worktree —
                # still check the rest before concluding it's an escape.
                # For a subtree registration the toplevel's parent joins the
                # escape space (`<ws>/x` next to the checkout `<ws>/mono`,
                # not just `<ws>/mono/x` next to the subtree): a TIGHTENING,
                # and only for the new shape — a root registration's two
                # bases are the same directory, so nothing moves there.
    if near_a_repo:
        return False  # sibling of some registered repo, not a legit
        # worktree of ANY of them — never falls through to /tmp scratch
    if _is_scratch_write(path, workspace):
        return True
    return not repos  # can't determine bounds — fail open, don't strand dev


# --------------------------------- the task recorded for a task worktree

_WT_TASK_CACHE: dict[tuple[Path, Path], object] = {}


def _scan_runs_for_worktree(wt_root: Path, workspace: Path):
    """(logical_root, run_dir, task_record) for the live task whose recorded
    worktree ROOT is exactly `wt_root`, or None.

    Aborted and completed runs are skipped (abort sweeps their worktrees; a
    stale dir must not enforce anything), published mirrors are skipped the
    same way `state.load` refuses them, and an unreadable/corrupt sibling run
    is skipped, not raised — mirroring guard_spawn's one-corrupt-run-doesn't-
    brick-the-workspace policy. Every one of those skips is a fail-OPEN for
    both callers below, which is the deliberate posture: neither the ordering
    gate nor the subtree-scope gate is the integrity guarantee, and neither
    may strand a developer over a sibling run it cannot read."""
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
            if not isinstance(wt, dict):
                continue
            # `root` is the post-subtree contract (`gitops.worktree_add`
            # returns `{path, root, branch}`, `root` the physical worktree and
            # `path` the logical repo `<root>/<prefix>` inside it); bare
            # `path` is the pre-subtree shape still sitting in every run's
            # state written before that change, where the two were the same
            # directory — so it IS the root there, and a resumed run must keep
            # matching.
            rec = wt.get("root") or wt.get("path")
            if rec and Path(rec).resolve() == wt_root:
                return Path(wt.get("path") or rec).resolve(), run, t
    return None


def _task_for_worktree_root(wt_root: Path, workspace: Path):
    """`_scan_runs_for_worktree`, memoized for the life of this process.

    The scan is the single most expensive thing guards.py does: it HMAC-chain-
    verifies EVERY live run's state.yaml. Two gates now consume it for the
    same write — the subtree-scope confinement and the TDD ordering — and on
    the bash surface both already ran once per swept absolute token, so a
    destructive command carrying N tokens paid N full scans before this cache
    existed and would have paid 2N after. Memoizing on the worktree root
    collapses all of that to ONE scan per distinct worktree per process, which
    makes the second gate not merely free but a net reduction against today.

    Safe because guards.py is a fresh process per tool call (see
    `_physical_toplevel`, same reasoning, same lifetime): no run's state can
    change between two lookups inside one hook invocation, and nothing here
    outlives it. Keyed on the workspace too — `_session_workspace` is derived
    per call site, and a cache that ignored it would answer a second workspace
    from the first one's runs. `None` is a cached ANSWER, not a miss, so
    membership is tested with `in` rather than `.get()`: "no live task owns
    this worktree" is exactly the verdict worth not re-deriving."""
    key = (wt_root, workspace)
    if key not in _WT_TASK_CACHE:
        _WT_TASK_CACHE[key] = _scan_runs_for_worktree(wt_root, workspace)
    return _WT_TASK_CACHE[key]


def _find_worktree_task(path: Path, workspace: Path):
    """(logical_root, run_dir, task_record) for the recorded task worktree
    containing `path`, or None — where `logical_root` is the LOGICAL repo
    inside that worktree (`worktree["path"]`), the base every repo-relative
    declaration is written against.

    Matched by the EXACT worktree ROOT recorded in state.yaml at
    `worktree-add` (see `_scan_runs_for_worktree` for the two accepted
    spellings of it) — never by parsing the task id out of the directory
    name — so parallel developers and hyphenated task ids can't cross-
    attribute. Comparing against `worktree["path"]` unconditionally, as this
    did, is what made the ordering gate SILENTLY FAIL OPEN for every subtree
    task: `path` is now `<root>/frontend`, the name-derived root is `<root>`,
    the equality never held, no task was ever found and the gate enforced
    nothing while reporting nothing.

    Two consumers, both on the developer write path: `_worktree_scope_reason`
    (which subtree of the worktree this task may write in at all) and
    `_tdd_block_reason` (which files inside it, and when). They share one
    memoized scan — see `_task_for_worktree_root`."""
    for repo in _registered_repos(workspace):
        wt_root = None
        # Same toplevel-anchored naming as the write confinement (see
        # `_worktree_bases`): a subtree repo's worktree is named after the
        # PHYSICAL checkout, so deriving the candidate root from the
        # registered path alone never matched one and the state scan below
        # was never even reached.
        for base in _worktree_bases(repo):
            parent = base.parent
            if not path.is_relative_to(parent):
                continue
            rel = path.relative_to(parent)
            if rel.parts and rel.parts[0].startswith(base.name + "-wt-"):
                wt_root = (parent / rel.parts[0]).resolve()
                break
        if wt_root is None:
            continue
        found = _task_for_worktree_root(wt_root, workspace)
        if found is not None:
            return found
    return None


def _worktree_scope_reason(path: Path, workspace: Path) -> str | None:
    """Why a developer write that landed inside a task worktree is refused
    for being outside the LOGICAL repo that task commits from, or None.

    THE silent-loss bug this closes, reproduced end-to-end with real git on
    the shape `initws.discover` proposes for a JS monorepo (`frontend/`
    registered, `package.json` + `pnpm-workspace.yaml` at the checkout root):
    the developer edits `<wt>/frontend/app.ts` AND `<wt>/package.json`, and
    every downstream mechanism then quietly drops the second one —
    `changed_files`/`diff_paths` are `--relative` to the subtree so the gates
    and the reviewer see ONE file; `commit_class` stages `add -A -- .` from
    the subtree so only `frontend/**` is committed; `merge-task` squashes
    only what was committed; develop.md step 7's `worktree remove --force`
    then deletes the rest. Green run, approved task, a PR that does not
    build — no error, no warning, and (until this gate) no `hook-blocked`
    event either. verify-green cannot catch it: the dependency IS on disk
    while the tests run.

    This is the WRITE half of the same line `commit_class`'s `-- .` pathspec
    draws at commit time. `_developer_write_ok` deliberately allows the whole
    worktree (see its note) because it answers a coarser, state-free
    question; the finer one needs the recorded prefix, which lives in run
    state and is resolved here. Refusing beats warning: the write has no
    destination, so there is nothing to salvage by letting it land.

    Fail-OPEN on every indeterminate — no `repos.yaml`, no PyYAML, no live
    run recording this worktree (the direct-branch fallback records
    `worktree: null`, and an aborted/completed run is skipped), unreadable or
    chain-broken state, or a bug in here. That is a deliberate trade, and it
    is the one the previous cut of this file avoided the run-state read to
    protect: an unreadable state must not strand a developer inside its own
    worktree over a defense-in-depth check. The fallback is TODAY'S exact
    behaviour (the whole worktree allowed), so the change is strictly
    narrowing and only ever where the answer is actually known. The read
    itself costs nothing new: `_tdd_block_reason` already resolved the same
    task from the same state on this same path, and both now share one
    memoized scan (`_task_for_worktree_root`), which nets out cheaper than
    before on the bash surface.

    A ROOT registration is byte-identical to today by construction, not by
    care. Only a path `_find_worktree_task` resolves can be refused here, and
    that requires the path to sit inside a `-wt-` worktree whose recorded
    root matches; for a root registration `worktree_add`'s prefix is empty,
    so `path == root` in the record, the logical root IS that worktree root,
    and every path inside it is therefore relative to it. There is no input
    for which this can return a reason on the ordinary shape — the paths that
    ARE the bug for a subtree task (`<wt>/package.json`, `<wt>/backend/…`)
    are simply the root task's own files, staged by its own `add -A -- .`."""
    try:
        found = _find_worktree_task(path, workspace)
        if found is None:
            return None
        logical_root, _run, task = found
        if path.is_relative_to(logical_root):
            return None
        if logical_root.is_relative_to(path):
            # `path` is an ANCESTOR of the logical repo — the worktree root
            # itself, or an intermediate directory of a nested prefix
            # (`<wt>/apps` for `apps/frontend`). Never a real file write: it
            # reaches here as bash attribution noise, because a destructive
            # verb anywhere in a command makes `_developer_bash_write_targets`
            # sweep every absolute token, including a `cd <worktree>` /
            # `git -C <worktree>` argument — and blocking a legitimate clean-
            # and-build over its own `cd` is the exact false-block
            # `_tdd_block_reason`'s `rel == "."` branch already exists to
            # avoid, one directory further in. A genuine `rm -rf <worktree>`
            # residual is accepted on the same terms it always was: it takes
            # the tests with it, so verify-red/green fail loudly.
            return None
        # `.get`, not `[...]`: a task record missing its id is a malformed-
        # state problem, and letting a KeyError fall into the fail-open
        # handler below would silently switch this confinement OFF for it
        return (f"'{path}' is inside task {task.get('id')}'s worktree but "
                "OUTSIDE the logical repo that task commits from "
                f"({logical_root}) — it belongs to the rest of the physical "
                "checkout the worktree was cut from. A write here is "
                "SILENTLY LOST: `harness "
                "commit` stages `add -A -- .` from your repo only, "
                "`merge-task` squashes only what was committed, and the "
                "worktree (with this edit in it) is force-removed when the "
                "task closes — the change would reach no branch and no PR "
                "while every gate still reported green. If your task really "
                "needs a change out there — a workspace-root manifest, a "
                "sibling package — it is another logical repo's file: say so "
                "in your report so it can be planned as its own task, "
                "instead of editing it here where it cannot survive.")
    except Exception:
        # defense-in-depth confinement, same posture as the ordering gate
        # below: an indeterminate (or a bug here) must never strand a
        # developer — the reviewer, the gates and the HMAC chain are the
        # guarantees, this is the early trip-wire
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
    opt-out, no second flag HERE (plan-register separately demands a
    recorded `no_test_reason` for the opt-out at any risk other than low —
    an upstream registration rule, not a develop-time gate). Red-proof existence is a plain file check: a
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
        logical_root, run, task = found
        if not task.get("test_intents"):
            return None      # no declared intents -> ordering not demanded
        from harness.transitions import redproof_path
        if redproof_path(run, task["id"]).exists():
            return None      # red sealed -> production writes unlocked
        # Relative to the LOGICAL repo (`worktree["path"]`), not to the
        # physical worktree root: the globs this is about to be matched
        # against — `language.test_paths` / `test_closure` / `pre_red_paths`
        # — are repo-relative by construction (`tests/**`, `**/*Tests.cs`),
        # and the same lists are what gitops locks blob-SHAs against using
        # `changed_files`, which is subtree-relative too. Rooting at the
        # worktree instead would hand every subtree task a `frontend/`-
        # prefixed path that no declaration ever mentions: every test write
        # misread as production, the gate refusing the very files it exists
        # to demand. Identical for a root repo, where the two are one
        # directory.
        try:
            rel = path.relative_to(logical_root).as_posix()
        except ValueError:
            # Inside the worktree but OUTSIDE this task's logical repo. Kept
            # as a backstop, and no longer the fail-open it was: a real file
            # write out there is refused upstream by `_worktree_scope_reason`
            # on both surfaces, so this branch is now reached only by the
            # paths that gate deliberately tolerates — the worktree root and
            # the intermediate directories above a nested prefix, which arrive
            # as bash attribution noise (`cd <worktree>`) and are not writes
            # at all. That is the same case, one directory out, as the
            # `rel == "."` branch below, and it fails open for the same
            # reason: the ordering is a claim about THIS task's repo, and
            # `.` / `..` name no file to ask it about.
            return None
        if rel == ".":
            # The repo root itself is never a real file write — it
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


def _gate_evidence_write_reason(path: Path, ws: Path) -> str | None:
    """Why a subagent write to `path` under a run's `reports/` is blocked, or
    None if it may proceed. Both arguments are RESOLVED.

    `<run>/reports/` is GATE-PRESENTED evidence owned by the orchestrator's
    report verb (orchestrator-only, SUBAGENT_REGISTER_RE). The planner's
    artifact-root check admits all of `ai/`, so this directory needs its own
    filename-specific rule inside an otherwise-writable root — exactly like
    the `.meta.json` rule (adversarial-review, whole-branch pass:
    SUBAGENT_REGISTER_RE's own comment claimed "no subagent had any path into
    it", which holds for the reviewer and the developer and was FALSE for the
    planner, live at intake/plan — the two cursors immediately before the gate
    that reads this evidence, and one filename away from pre-seeding the round
    snapshot that wedges the orchestrator's first legitimate save).

    Shared by the Write/Edit AND Bash surfaces. The first cut of this rule
    closed only Write/Edit, leaving `echo >`, `tee`, `cp` and an inline
    `open(...,'w')` open to the same shape — the re-verification pass
    reproduced all four. Closing one surface of two is the very shape of the
    finding this rule exists to fix, so the predicate lives here once and
    both guards call it.

    Casefolded for the reason `_refuse_quarantine_overlap` states about the
    same class of comparison: on a case-insensitive filesystem `Reports/` and
    `reports/` are ONE directory, and a file written through either spelling
    is read back through the other — including by the snapshot-immutability
    check in `save_report` (re-verification finding, reproduced on macOS).
    """
    try:
        rel = path.relative_to(ws / "ai")
    except ValueError:
        return None
    if not any(part.casefold() == "reports" for part in rel.parts):
        return None
    if path.name.casefold() == "plan-revision-log.md":
        # An exemption, not a blanket block: the plan's revision archaeology
        # is the planner's OWN output (agents/planner.md). Casefolded too —
        # on the filesystems this rule is casefolded FOR, a differently-cased
        # spelling is the same file, so it must not block where the exact
        # spelling would not.
        return None
    return ("<run>/reports/ holds gate-presented evidence — it is persisted "
            "by the orchestrator's own report verb after your spawn returns, "
            "and by the harness's report steps, never by a subagent (on any "
            "surface: Write/Edit, a redirect, tee, cp, or an inline "
            "interpreter). Return your report in your reply; the only file "
            "here that is yours is reports/plan-revision-log.md.")


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
              Path(p.get("cwd") or "."), p)


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
              "blocked for every role.", cwd, p)
    shape = shape_of(p.get("agent_type"))
    if shape == "reviewer":
        block("the reviewer is read-only (design.md piece 3) — no Write/Edit.", cwd, p)
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
                  "not the workspace or another repo.", cwd, p)
        # inside the worktree, but is it inside the SUBTREE this task
        # commits from — the second half of the same confinement, and the
        # one that stops a write from being silently dropped at commit time
        reason = _worktree_scope_reason(path, ws.resolve())
        if reason:
            block(reason, cwd, p)
        reason = _tdd_block_reason(path, ws.resolve())
        if reason:
            block(reason, cwd, p)
    if shape == "planner":
        path = _resolve_write_path(fp, cwd)
        # `.qwen/context` is the Qwen install-rewrite spelling of
        # `.claude/context` (the installer rewrites `.claude/`→`.qwen/` in
        # markdown). init-workspace aliases it via a symlink under Qwen,
        # which `path.resolve()` follows into `.claude/context` — but on
        # hosts where symlinks fail the model still writes the literal
        # `.qwen/context/...` path, so accept both prefixes here. The CLI
        # keeps `.claude/context` as the single physical location either
        # way; this is confinement acceptance, not a second data tree.
        artifact_roots = (
            ws.resolve() / "ai",
            (ws / ".claude" / "context").resolve(),
            (ws / ".qwen" / "context").resolve(),
        )
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
                  "(design.md piece 3 path-guard).", cwd, p)
        # Bash half of this same rule lives in guard_bash — see
        # _gate_evidence_write_reason for why it is one shared predicate.
        reason = _gate_evidence_write_reason(path, ws.resolve())
        if reason:
            block(reason, cwd, p)
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
                  "returns (agents/planner.md).", cwd, p)


# --------------------------------------------------------- spawn / skill

def _flag_serialized_panel(run: Path) -> None:
    """A plan-attack spawn arriving AFTER a sibling lens completed THIS
    round was issued serially — batched spawns all clear PreToolUse
    before any PostToolUse fires (field 459226 F-3: the orchestrator
    narrated "spawning both lenses in parallel" and then issued them one
    at a time — twice, ~13 min avoidable wall-clock per serialized
    panel; plan-review.md states the batch rule three times and the
    model violated it in the same breath, so prose can't self-enforce —
    ordering can detect). Round boundary = everything after the LAST
    `plan-registered` marker (a revision round re-registers, re-arming
    the window — same actor-checked boundary the plan-flag supersession
    uses, so a stray `log-event` record can't reset the window). Loud
    and NEVER blocking: the first lens's work is real, and blocking the
    second spawn would waste it. Known benign false positive, named in
    the reason: a stall-recovery re-spawn of ONE lens legitimately
    arrives after its siblings. Premise (field-consistent, not provable
    from this repo): batched spawns all clear PreToolUse before any
    PostToolUse fires — holds for panels under the platform concurrency
    cap (~14), far above any real panel; a panel larger than the cap
    could edge-case a false flag (postmortem F-3's stated bound)."""
    try:
        events = ndjson.read_records(run / "events.ndjson")
    except OSError:
        return
    completed_this_round = False
    for e in events:
        if (e.get("kind") == "plan-registered"
                and e.get("actor") == "plan-register"):
            completed_this_round = False
        elif e.get("kind") == "lens-complete":
            completed_this_round = True
    if completed_this_round:
        try:
            ndjson.append_record(run / "events.ndjson", {
                "kind": "panel-serialized", "actor": "guard-spawn",
                "reason": "this plan-attack spawn arrived after a sibling "
                          "lens had already completed this round — lens "
                          "spawns were issued serially, not batched in ONE "
                          "message (plan-review.md step 1; ~13 min avoidable "
                          "wall-clock per panel). Benign if this is a "
                          "stall-recovery re-spawn of a single lens."})
        except OSError:
            # best-effort telemetry, never a precondition (same pattern as
            # block()'s ledger append): the spawn guard dispatch fails
            # CLOSED, so an unguarded ENOSPC/EACCES here would BLOCK the
            # legal spawn this detector promises never to block
            # (adversarial-review on this change, both lenses — reproduced
            # with a read-only events file).
            pass


def _live_spawn_for(run: Path, task: str | None, mode: str) -> dict | None:
    """The OPEN `spawn-pending` for this exact (task, mode) — a spawn of the
    same shape of work that launched and has not reported back — or None.

    What counts as open is DECLARED, not restated here: `spawn_pairing:` in
    pipeline/task-fsm.yaml names the pending and its two resolvers with the
    actor that owns each, and `transitions.open_pendings` applies it. This is
    one of four readers of that family, split across two layers, and the
    whole-system review's point stands: a hand-written `log-event` must not
    be able to unblock a spawn here while being inert in the other three, nor
    to forge a pending that wedges this key while the gauge shrugs.

    Keyed on (task, mode) EQUALITY and nothing else — never on a stall key.
    Neither the stall layer's `step:<step>[:<lens>]` spelling nor the
    mode-bound matching `transitions.stall_key_spawn_modes` derives from it
    reaches this predicate: a task-less spawn's pending carries task=None and
    this is asked about task=None too, both sides parsing the same absent
    header. Same for `capture_subagent_stop`, which pairs on agent_id — so
    narrowing the stall-key predicate changed neither of them.

    Reads the ledger BEST-EFFORT: an unreadable events.ndjson allows the
    spawn. This predicate exists to refuse, and guard_spawn's fail-closed
    posture is about spawn LEGALITY (the manifest), not about a serialization
    check that would otherwise turn an I/O error into "no agent may run" —
    the same reasoning `_flag_serialized_panel` states for its own ledger
    read. Lenient PARSING has the same shape and a quieter failure: a torn
    `spawn-pending` line is simply skipped, and this predicate reads absence
    as "nothing in flight" — i.e. one corrupt line silently disables the
    refusal. Not fixable by failing closed (that would block every spawn in
    a run whose ledger a crash tore), so it is made VISIBLE instead: the
    skipped-line count goes to stderr (adversarial review)."""
    try:
        events, skipped = ndjson.read_records_counting(run / "events.ndjson")
    except OSError:
        return None
    if skipped:
        print(f"ai-sdlc-harness: {run / 'events.ndjson'} has {skipped} "
              "unparseable line(s) — a torn `spawn-pending` line is invisible "
              "here, so a second spawn for a key whose agent is still in "
              "flight may be allowed through.", file=sys.stderr)
    return next((e for e in transitions.open_pendings(events)
                 if e.get("task") == task and e.get("mode") == mode), None)


def leading_header_block(prompt: str) -> str:
    """The prompt's LEADING run of `harness-*:` lines — where SKILL.md puts
    the orchestrator's own headers, and the only region where an
    angle-bracket value can be an unsubstituted placeholder rather than a
    quoted example.

    This scoping is what keeps the placeholder refusal below from firing on
    the NORMAL shape: shared/status-block.md's reply template carries a
    literal `harness-task: <task-id or ->` line, prompts quote it verbatim as
    instructions to the subagent, and a TASK-LESS spawn (plan-review, pre-pr)
    quoting it has no real task header to be distinguished by. The template
    always sits below prose, never in the opening header run.

    Fails OPEN by construction — a prompt that opens with a sentence has an
    empty leading block, so the new refusal can only ever under-fire."""
    lines: list[str] = []
    for line in prompt.splitlines():
        if not line.strip():
            if lines:
                break          # a blank line ends the block; leading ones skip
            continue
        if not line.startswith("harness-"):
            break
        lines.append(line)
    return "\n".join(lines)


def guard_spawn(p: dict) -> None:
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    manifest = load_yaml(PLUGIN_ROOT / "pipeline" / "manifest.yaml")
    tool_input = p.get("tool_input") or {}
    cwd = Path(p.get("cwd") or ".")
    ws = _session_workspace(cwd)   # cd-drift-immune (field: session D)
    shape = shape_of(tool_input.get("subagent_type"))
    if shape not in surfaces["shapes"]:
        # Not a recognized harness shape. If the prompt carries harness
        # headers, this is a provable mis-typed harness spawn — not a
        # foreign agent. Block it with an actionable message so the
        # orchestrator gets the right agent identity, instead of running
        # the step ungoverned (no gating, no write confinement, no
        # verdict capture). A header-less generic spawn is genuinely
        # unrelated work and passes untouched.
        prompt = tool_input.get("prompt") or ""
        if MODE_HEADER_RE.search(prompt):
            block(
                f"harness-headed spawn used agent type "
                f"'{tool_input.get('subagent_type', '(omitted)')}' — this is "
                f"a harness spawn (carries harness-mode: header), but the "
                f"agent type does not resolve to a harness shape. The three "
                f"harness agents are: ai-sdlc-planner, ai-sdlc-developer, "
                f"ai-sdlc-reviewer (your platform may prefix them, e.g. "
                f"ai-sdlc-harness:ai-sdlc-reviewer). A generic agent "
                f"(general-purpose, Explore, Task) would run this step "
                f"with no spawn gating, no write confinement, no "
                f"verdict capture. See "
                f"skills/dev-workflow/shared/spawn-identity.md.",
                cwd, p)
        return  # genuinely unrelated agent — none of our business
    # WI-3, RESCOPED TO QWEN CODE. The rule's original premise is dead and
    # says so here rather than quietly: "require explicit false — a uniform
    # rule that needs no platform detection and is identical on both
    # platforms". It was uniform because the two platforms failed the same
    # way. They no longer do. On Claude Code the async stub shape is
    # MEASURED (2.1.232: {isAsync, status: async_launched, agentId, …}),
    # capture_post_spawn recognises it, and capture_subagent_stop completes
    # it — a background reply is captured end to end, so the rule now
    # forbids the platform's own default for no benefit and, on the newer
    # Agent schema that dropped the parameter entirely, blocks EVERY
    # harness spawn (the whole pipeline) over a param the caller cannot
    # pass. Under Qwen Code the same stub is UNMEASURED: an unrecognised
    # stub falls to _capture_reply, whose necessarily-absent status block
    # fabricates a missing-status-block stall for a live agent — the exact
    # bug round 1 killed on Claude — so there the rule stays verbatim.
    #
    # STATED RISK, rewritten by measurement: this used to trust `QWEN_CODE`
    # alone to reach hook subprocesses, and on Qwen Code 0.22.2 it does not
    # — hooks receive `QWEN_CODE_CLI` (the cli-entry.js path) while
    # `QWEN_CODE` reaches only shell-tool children — so the block below
    # silently vanished in every real hook run, the exact wrong-way failure
    # (silent permit) the original comment here feared. Detection is now
    # `harness.qwen_cli_detected()`, truthy-presence on EITHER spelling:
    # any value a CLI revision ships keeps the protective block on, and a
    # stray value can only ever over-refuse, which the caller can see and
    # fix. Residual risk, accepted: if a future Qwen drops BOTH spellings
    # from hook environments, the block vanishes again — undetectable from
    # inside the hook, so the measurement note in this repo (project
    # memory, 2026-08-27) is the standing reference for re-checking.
    #
    # The OTHER side of that trade, equally accepted: because Qwen keeps the
    # rule verbatim, Qwen inherits the failure the rescope removed from
    # Claude Code — if Qwen ever adopts the newer Agent schema that drops
    # `run_in_background` entirely, every harness spawn there hard-fails on a
    # parameter the caller cannot pass, i.e. the whole pipeline blocks with
    # no in-band workaround. Held anyway until Qwen's launch-stub shape is
    # MEASURED: a fabricated stalled-agent event for a live agent (the
    # unmeasured-stub failure) corrupts the evidence ledger and races a
    # re-spawn against the original, where this one refuses loudly and
    # visibly. (The stub HAS since been measured — 2026-08-27, live probes
    # on 0.22.2 — and capture_post_spawn now recognises the shape
    # (`returnDisplay.status == "background"`) and records the
    # spawn-pending keyed by the stub's printed task_id, which that
    # agent's SubagentStop carries back as `agent_id`; measured end to
    # end. The rescope of THIS block is the remaining step, deliberately
    # sequenced last so it flips together with the Qwen transcript token
    # attribution and the docs — one atomic enable, never a half-shipped
    # one.)
    if qwen_cli_detected():
        bg = tool_input.get("run_in_background")
        if bg not in (False, "false", "False"):
            block(f"harness-shape spawns ('{shape}') must run in the "
                  "FOREGROUND under Qwen Code — pass run_in_background: "
                  "false explicitly (omitting it DEFAULTS to background for "
                  "top-level spawns). Background capture exists, but it is "
                  "built on Claude Code's MEASURED launch-stub shape; Qwen's "
                  "is unmeasured, and an unrecognised stub reaching verdict "
                  "capture fabricates a stalled-agent event for an agent "
                  "that is still running. For parallelism, batch multiple "
                  "foreground spawns in ONE message — they run concurrently "
                  "and each reply is captured.", cwd, p)
    prompt = tool_input.get("prompt") or ""
    m = MODE_HEADER_RE.search(prompt)
    if not m:
        block(f"harness-shape spawn ('{shape}') requires the structured "
              "`harness-mode: <mode>` header in the spawn prompt (RC4).", cwd, p)
    mode = m.group(1)
    # The other half of the serialization key below. Absent for a task-less
    # spawn (plan-review, pre-pr, …) and None on BOTH sides then — the
    # pending record capture_post_spawn writes carries the same parse of the
    # same header, so a task-less spawn serializes against its own task-less
    # twin and against nothing else.
    task = (TASK_HEADER_RE.search(prompt) or [None, None])[1]
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
                # THE TASK HEADER MUST NAME A REGISTERED TASK. Nothing
                # validated it anywhere before — not here, not at capture —
                # so a typo'd `harness-task: T2x` sailed through, and every
                # downstream consumer keyed off the typo: the capture hook
                # wrote a `spawn-pending` under it, `guard_spawn` (below)
                # then refused the CORRECTLY-spelled re-spawn's sibling key,
                # and `harness stall` refused to count it ("unknown task"),
                # leaving a run DEGRADED with no verb able to move it. Round
                # 2's G4 made that wedge RECOVERABLE (the
                # `--confirm-no-verdict` override retires the pending before
                # the counter can raise); this closes it at the source for
                # every RUN-LEGALIZED spawn, where the cost is one clear
                # refusal instead of a recovery procedure. NOT at every
                # source: an `always_legal_spawns` pair returned far above,
                # before any run was resolved (measured), so a cross-cutting
                # request-triage carrying a bad task header still passes —
                # correctly, since it answers no run-owned question and
                # nothing keys off its header. Pipelined dispatch is what
                # makes closing the run-legalized door worth it: with several
                # tasks in flight, hand-typed ids multiply and a wrong one is
                # no longer obvious from context.
                #
                # A TASK-LESS spawn stays legal and unchecked (plan-review,
                # pre-pr, repo-map): absent is a declared shape here, and
                # both this guard and the capture hook parse the same absent
                # header into the same None.
                known = [t.get("id") for t in (st.get("tasks") or [])]
                if task is None:
                    # UNSUBSTITUTED PLACEHOLDER — `harness-task: <task-id>`,
                    # exactly as develop.md and agents/developer.md print the
                    # header block. TASK_HEADER_RE's `(?!<)` makes it parse
                    # as absent, so without this the spawn ran as a
                    # deliberately task-less one: no id check, no (task,
                    # mode) serialization, and a reviewer verdict filed under
                    # None that no `task --to done` can ever read.
                    #
                    # Two things keep it off the NORMAL shape: it fires only
                    # when no real header accompanies it, and it looks only
                    # in the LEADING header block (see
                    # `leading_header_block`). Prompts routinely quote
                    # shared/status-block.md's reply template —
                    # `harness-task: <task-id or ->` — as instructions to the
                    # subagent, and a task-LESS spawn quoting it has no real
                    # header to be told apart by; the template always sits
                    # below prose, never in the opening header run.
                    ph = TASK_PLACEHOLDER_RE.search(
                        leading_header_block(prompt))
                    if ph:
                        block(
                            f"spawn's `harness-task:` header is still the "
                            f"literal placeholder ({ph.group(1).strip()}) — "
                            "substitute the real task id. It is copied "
                            "verbatim from the header block in develop.md / "
                            "agents/developer.md, and left unsubstituted it "
                            "reads as a TASK-LESS spawn: the id is never "
                            "validated, two dispatches of the same task stop "
                            "serializing, and this agent's verdict is "
                            "captured under no task at all, where `task --to "
                            "done` will never find it. This run's registered "
                            f"task ids are: "
                            f"{', '.join(str(k) for k in known) or 'none'}. A "
                            "genuinely task-less spawn omits the header line "
                            "entirely.", cwd, p)
                if task is not None:
                    if task not in known:
                        block(
                            f"spawn carries `harness-task: {task}`, which is "
                            "not a task in this run — registered task ids "
                            f"are: {', '.join(str(k) for k in known) or 'none'}"
                            ". Everything downstream keys off this header "
                            "(the spawn-pending, the token ledger, the "
                            "reviewer verdict `task --to done` reads), so a "
                            "typo here does not fail loudly later — it files "
                            "a real agent's work under an id no verb can "
                            "reach. Fix the header and re-spawn; a task-less "
                            "spawn omits it entirely.", cwd, p)
                    # …AND IT MUST STILL BE LIVE, for the modes that consume
                    # a task (develop, review). Dispatching a finished task
                    # is a real orchestrator slip under pipelined dispatch —
                    # the loop re-reads `ready-tasks` after every completion
                    # and a stale id from the previous round is one line
                    # away — and it fails LATE and confusingly: the developer
                    # does real work, then `task --to in-review` is refused
                    # because there is no such transition from `done`.
                    #
                    # Scoped to those two modes deliberately: `harden` and
                    # `fixup` run at steps the develop sync point already
                    # required every task to be TERMINAL for, so blocking a
                    # terminal task there would block their only legal shape.
                    # And this refuses nothing else that is legal either —
                    # the FSM's one way back out of a terminal status is the
                    # hotfix re-entry edge (`archived -> in-progress`, and
                    # `done -> archived` before it), which transitions FIRST
                    # and then dispatches, so a legitimate re-entry is
                    # already non-terminal by the time it spawns.
                    #
                    # …but mode-scoping alone was NOT enough (re-verification,
                    # executed): `review` is in the HARDEN step's spawn-set
                    # too (manifest.yaml:279), and harden sits PAST the sync
                    # point — every task is required terminal there, so a
                    # harden-step review spawn can only ever name a terminal
                    # task, and it was refused with advice that dead-ends
                    # ("re-read `harness ready-tasks` and spawn for an id it
                    # lists as ready" — there are none, and never will be).
                    # So the block keys on the same fact its own rationale
                    # rests on: a terminal id is suspicious only while
                    # SIBLINGS ARE STILL LIVE, which is exactly the pipelined
                    # develop loop the stale-id slip happens in. Once every
                    # registered task is terminal (harden and later), naming
                    # one is the normal shape, not a slip.
                    terminal = _terminal_statuses()
                    registered = st.get("tasks") or []
                    all_terminal = all(t.get("status") in terminal
                                       for t in registered)
                    if mode in ("develop", "review") and not all_terminal:
                        status = next((t.get("status") for t in registered
                                       if t.get("id") == task), None)
                        if status in terminal:
                            block(
                                f"spawn carries `harness-task: {task}`, which "
                                f"is already {status} in this run — a "
                                f"terminal task has no '{mode}' work left. "
                                "Dispatching it produces real work the FSM "
                                "then refuses to record (there is no "
                                f"transition out of {status} except the "
                                "hotfix re-entry, which transitions the task "
                                "back to in-progress FIRST). Re-read "
                                "`harness ready-tasks` and spawn for an id it "
                                "lists as ready.", cwd, p)
                # ONE LIVE SPAWN PER (task, mode). The failure this closes
                # was executed in round 1's review: round N's background
                # reviewer was still running when round N+1's was spawned,
                # both finished, and latest-wins on reviews.ndjson crowned
                # the STALE verdict — the same race guard_stall_verdict
                # refuses a stall for, arriving through the other door.
                # Backgrounding is now legal on Claude Code (WI-3 above), so
                # "the previous one is still in flight" stopped being a
                # corner and became the default shape.
                #
                # Keyed on (task, mode) and NOTHING coarser, deliberately: a
                # DIFFERENT task or a DIFFERENT mode is unrelated work, and
                # cross-task parallelism — several tasks' developers or
                # reviewers in flight at once — stays legal, which is the
                # whole point of backgrounding. Only a second agent
                # answering the SAME question is refused.
                #
                # plan-attack is EXEMPT, and that is the one exemption
                # (`always_legal_spawns` returned far above, deliberately
                # unserialized for the same reason: a cross-cutting
                # request-triage answers no run-owned question, so a second
                # one competes with nothing). The refusal is a HARD BLOCK
                # built on the batched-panel premise — every lens clears
                # PreToolUse before any lens's PostToolUse writes a pending —
                # which `_flag_serialized_panel` labels in its own docstring
                # "field-consistent, not provable" and deliberately does NOT
                # block on; and a lens stub returns in milliseconds, so the
                # margin the premise needs is unmeasurably thin. Two tiers of
                # confidence, one rule: the OBSERVER may run on a premise it
                # cannot prove, the BLOCKER may not. If the premise fails,
                # this refuses lenses 2..N of every panel and the only escape
                # (`--confirm-no-verdict` on the panel's key) retires the
                # whole panel including lens 1's real work — and the
                # single-lens stall recovery plan-review.md documents was
                # blocked outright, since a re-spawned lens necessarily
                # arrives while its siblings are in flight. Safe to exempt
                # because NO engine-read verdict rides on a lens (the same
                # justification transitions' docstring gives: verdict_bound
                # filters on mode plan-review, so lens verdicts can neither
                # move the window nor burn the round budget) — two lenses
                # answering one question cost tokens, not a wrong verdict.
                # The non-blocking `panel-serialized` flag below remains the
                # detector for a serially-issued panel.
                live = (None if mode == "plan-attack"
                        else _live_spawn_for(run, task, mode))
                if live is not None:
                    if task:
                        # plural, deliberately: a task key holds one pending
                        # PER MODE, and the CLI abandons every open one on
                        # the key — a singular here promised a narrower
                        # override than the one it prescribed
                        # (adversarial re-verification, executed: two
                        # pendings, one command, two abandonments).
                        exits = (f"run `harness stall --confirm-no-verdict "
                                 f"--task {task}`, which abandons EVERY "
                                 f"open pending on task {task} (all modes) "
                                 "and frees this key")
                    else:
                        exits = ("run `harness stall --confirm-no-verdict` "
                                 "(no --task: the key is `step:<this run's "
                                 "current step>`), which abandons EVERY open "
                                 "pending on that key — every task-less spawn "
                                 "in flight whose mode this step declares, "
                                 "siblings included — and frees it")
                    block(
                        f"a ({shape}, {mode}) spawn for this key is ALREADY "
                        f"in flight — agent {live.get('agent_id')} launched "
                        "in the background and has not reported back "
                        "(`spawn-pending`, still open in this run's "
                        "events.ndjson). Two agents answering the same "
                        "question is how a STALE verdict wins: both write "
                        "reviews.ndjson and the engine reads the latest. "
                        "Two exits, no third: WAIT for that agent's "
                        "completion notification (its verdict/status capture "
                        "happens there and clears the pending), or — if it "
                        f"genuinely died — {exits}. A spawn "
                        "for a DIFFERENT task or mode is unaffected.",
                        cwd, p)
                if mode == "plan-attack":
                    _flag_serialized_panel(run)
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
              "attribute to it (RC4; SKILL.md's mandated headers).", cwd, p)
    if not runs:
        block(f"no active run — harness-shape spawns are fail-closed pre-run; "
              f"({shape}, {mode}) is not a declared out-of-run exception "
              "(invocation control, design.md piece 3).", cwd, p)
    block(f"spawn ({shape}, {mode}) does not match any active run's current "
          "step spawn-set or the always-legal list — the manifest is the "
          "source of truth (design.md piece 1).", cwd, p)


def guard_skill(p: dict) -> None:
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    skill = ((p.get("tool_input") or {}).get("skill") or "").split(":")[-1]
    if skill in surfaces["user_entry"]["skills"] and (
            p.get("agent_id") or p.get("agent_type")):
        block(f"'/{skill}' is a user-entry skill — invocable only from the "
              "main session by a human, never from a subagent "
              "(invocation control, design.md piece 3).",
              Path(p.get("cwd") or "."), p)


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
    wins). Residual: two runs SIMULTANEOUSLY mid-gate in one workspace
    still both capture. The CROSS-SESSION half of that is now handled at
    decide time — the payload DOES carry `session_id` (tagged below) and
    the CLI does read its own, so "typed in some other session" is
    recognizable and `gates.decide` filters it out. What survives is the
    SAME-SESSION half: two runs driven by ONE session tag identically, and
    no field anywhere binds a prompt to a run, so neither gate can tell
    which of them the reply was meant for. There the old mitigation is
    still the only one — the two gates' numbered-option sets differ, so a
    reply meant for the other run usually fails to parse and refuses
    rather than deciding.

    Capture itself is UNCONDITIONAL and lossless within that window: no
    prompt is ever withheld from a run that is awaiting, so the ledger
    stays a complete append-only audit record. What this hook adds is a
    TAG — the digest of the session the prompt was typed in — and the tag
    is what lets `gates.decide` ignore a reply typed in a DIFFERENT session
    from the one driving the run. Field, 2026-08-26: a `/dev-workflow` run
    sat mid-gate while a SECOND Claude Code session in the same workspace
    ran `/story-workflow`; that unrelated prompt was appended here, and
    decide (newest record after the stamp wins) parsed `/story-workflow new
    XD-5` instead of the human's `rejected`.

    Do NOT read this as "capture is scoped by session" — it is not, and
    deliberately so. At capture time "no run matched this prompt's session"
    and "a foreign session typed" are the SAME observation: exactly one run
    was awaiting in the field incident (`/story-workflow` starts no run at
    all), so a rule that appends on no-match re-admits the bug, and a rule
    that drops on no-match destroys the evidence of a session that resumed
    under a new id. Filtering therefore lives at DECIDE time, where the
    deciding session's own identity disambiguates the two — and where a
    wrong guess refuses LOUDLY (a named refusal pointing at `--re-present`)
    instead of silently deleting what the human typed.

    RESIDUAL — a SUBAGENT's session id is not the human's. Measured: the
    `CLAUDE_CODE_SESSION_ID` exported into a subagent is a DIFFERENT string
    from the main session's, and `CLAUDE_CODE_CHILD_SESSION=1` is set in
    BOTH, so it is not a usable discriminator either. This hook only ever
    fires in the main session (UserPromptSubmit has no subagent analogue),
    so every captured record carries the HUMAN's tag — meaning a
    subagent-issued `gate --present` stamps a digest nothing will ever
    match, and a subagent-issued `gate --decide` compares against one, and
    can hard-refuse genuine human evidence. Nothing mechanically blocks
    that today: `harness gate` is not in `SUBAGENT_REGISTER_RE` above, only
    prose says the orchestrator owns gates. It fails CLOSED (a refusal, not
    a forged decision) and is escapable via `--re-present` from the
    orchestrator, which is why it is recorded rather than fixed. If it ever
    bites in the field, the mechanical fix is one line: add `gate` to the
    orchestrator-only verb set in `SUBAGENT_REGISTER_RE`.

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
    # Digested through the shared helper, never hashed inline: the CLI
    # stamps the gate and this hook tags the record, in different
    # processes — a second implementation that drifted (different
    # algorithm, different truncation) would not raise, it would just stop
    # comparing equal, and the symptom is a gate that cannot be decided.
    # The helper also absorbs a non-string `session_id`: this is a hook,
    # the payload is untrusted JSON, and an AttributeError here fails the
    # WHOLE capture open.
    sid = gates.session_digest(p.get("session_id"))
    if sid:
        # Absent, never null: an untagged record means "unknown session",
        # which decide reads as "usable" — the same shape a pre-fix ledger
        # record or a Qwen Code prompt already has.
        record["session"] = sid
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
    usage_source = None
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
        # Qwen Code: the SubagentStop transcript is Gemini-format JSONL — the
        # top-level `type` stays assistant/user, but the message carries
        # `parts: [{"text": <block>}]` and NO `content` (and no message-level
        # usage/model — the per-record usage lives TOP-LEVEL, see
        # `usageMetadata` below). Read `parts` only when `content` is absent,
        # so Claude transcript parsing stays byte-identical.
        parts = msg.get("parts")
        # NEWLINE join, not "" (adversarial-review finding, same class as
        # _response_text): a content block ending without a newline would
        # glue its tail onto the next block's first line, hiding a
        # `verdict:`/`harness-status:` line from the line-anchored regexes.
        # The Qwen parts branch joins the same way for the same reason.
        if isinstance(content, list):
            text = "\n".join(c.get("text", "") for c in content
                             if isinstance(c, dict))
        elif isinstance(content, str):
            text = content
        elif isinstance(parts, list):
            text = "\n".join(pt.get("text", "") for pt in parts
                             if isinstance(pt, dict))
        else:
            text = ""
        key = (role, msg.get("id") or id(entry))
        if key not in by_key:
            by_key[key] = {"role": role, "text": "", "model": msg.get("model"),
                          "usage": {}}
            messages.append(by_key[key])
        by_key[key]["text"] += "\n" + text
        if msg.get("usage"):
            by_key[key]["usage"] = msg["usage"]
        # Qwen/Gemini transcripts carry the per-record usage TOP-LEVEL
        # (`usageMetadata`, Gemini key names) — measured live on 0.22.2,
        # on the agent's own transcript, one block per API round. Translate
        # to the Claude key names every consumer of this parse reads, and
        # FLAG the family: the old all-zero-counts proxy for "Qwen
        # transcript" dies here, because these counts are real. The flag,
        # not a zero signature, is what capture_subagent_stop's double-write
        # guard discriminates on. thoughtsTokenCount is deliberately left
        # out — same stance as the executionSummary branch: the ledger
        # records actual billed input/output, not reasoning tokens folded
        # into either.
        um = entry.get("usageMetadata")
        if isinstance(um, dict):
            by_key[key]["usage"] = {
                "input_tokens": um.get("promptTokenCount") or 0,
                "output_tokens": um.get("candidatesTokenCount") or 0,
                "cache_read_input_tokens":
                    um.get("cachedContentTokenCount") or 0,
            }
            usage_source = "gemini"
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
            "model": last_assistant.get("model"), "usage": total_usage,
            "usage_source": usage_source}


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
    # TOKEN capture keeps the historical fallback chain (agent transcript,
    # else the session's own) — attribution headers and usage counts are all
    # it reads out of it, and both are harmless from either file.
    #
    # The PENDING branch below deliberately does NOT share it: `agent_tx` is
    # tracked separately so a verdict can only ever be minted from the
    # SUBAGENT's own transcript (adversarial review, executed — with
    # `agent_transcript_path` absent, the chain handed the branch the PARENT
    # session transcript, and the ORCHESTRATOR's own restated
    # "verdict: APPROVED" line minted a real reviews.ndjson row: the FSM
    # gate answered by the agent it was meant to check).
    #
    # The parse is wrapped because _parse_transcript reads with
    # encoding="utf-8" and a non-UTF-8 transcript raises UnicodeDecodeError
    # — which FAIL_OPEN turns into "abort the whole hook", so the pending
    # gate never ran at all and a completed background spawn kept its
    # dangling flag forever (adversarial review, executed). Treating a
    # failed parse as NO DATA is the honest outcome: token capture skips
    # gracefully below, and the pending branch falls to
    # `last_assistant_message`.
    agent_tx = p.get("agent_transcript_path")
    transcript = agent_tx or p.get("transcript_path")
    data: dict = {}
    agent_reply = ""
    if transcript and Path(transcript).exists():
        try:
            data = _parse_transcript(Path(transcript))
        except Exception:
            data = {}
        else:
            if agent_tx:
                agent_reply = data.get("text") or ""
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
    role = shape_of(p.get("agent_type"))
    # Completion half of the background-spawn handoff. capture_post_spawn
    # recorded a `spawn-pending` for this agent_id because all PostToolUse
    # ever saw was the launch stub (see there); THIS event is the only one
    # that carries the agent's real final reply, so it is where that
    # deferred verdict/status capture actually happens.
    #
    # Gated on an OPEN pending for this EXACT agent_id, never on the event
    # alone. A foreground spawn's reply is already captured at PostToolUse
    # and re-capturing it here would append a second reviews.ndjson row for
    # one review — and ordering can't be used to tell the two apart, since
    # foreground fires SubagentStop BEFORE PostToolUse while background
    # fires it long after. No pending, no capture: every foreground spawn
    # and every non-harness agent sees byte-identical behaviour to before.
    # The `spawn-captured` check makes a re-delivered stop idempotent for
    # the same reason (one spawn, one verdict row).
    #
    # Placed deliberately BEFORE the Qwen double-write early-return below:
    # that return is about TOKEN rows, and a transcript carrying no counts
    # and no model (Qwen/Gemini, or a degenerate turn) must never cost the
    # run its VERDICT — the FSM-gating record of the two.
    #
    # ASSUMED: the platform's agent_id is unique for the life of a run — it
    # is the only key the two hook processes share. The gate is "was this id
    # EVER captured", not "is there an open pending", so if a platform ever
    # reused an id the second spawn's stop would find the first's
    # `spawn-captured` and skip.
    #
    # What that costs changed in round 4 and the note here did not keep up
    # (round-4 review, executed: `[pending(a-1), captured(a-1), pending(a-1)]`
    # read 1 flagged / DEGRADED at HEAD and 0 / HEALTHY after). This used to
    # be drop-and-FLAG — the gauge required a resolver to FOLLOW its pending,
    # so the second pending stayed open and visible. `open_pendings` is now
    # deliberately order-INDEPENDENT (a resolver closes its agent_id wherever
    # it sits, because the two records are written by hooks whose relative
    # order the platform picks), which makes one `spawn-captured` close BOTH
    # pendings: drop-and-HIDE. The trade is still accepted — a double verdict
    # row on the FSM's own ledger is worse than a dropped one — but the
    # honest statement of it is that a reused id costs a verdict SILENTLY,
    # and the order-independence that buys the four readers one shared rule
    # is what pays for it.
    agent_id = p.get("agent_id")
    try:
        prior, skipped = ndjson.read_records_counting(run / "events.ndjson")
    except OSError:
        prior, skipped = [], 0
    if skipped:
        # Same silence, third reader (adversarial review): a torn
        # `spawn-pending` line makes this stop look like a foreground one and
        # its verdict is never captured, while a torn `spawn-abandoned` line
        # lets an abandoned round's late reply into the ledger after all.
        # Lenient by design — failing closed here would drop verdicts a
        # readable ledger would have captured — so the ledger's damage is
        # reported instead of inferred from a missing row later.
        print(f"ai-sdlc-harness: {run / 'events.ndjson'} has {skipped} "
              "unparseable line(s) — a torn `spawn-pending` / "
              "`spawn-abandoned` line is invisible to this capture, so a "
              "background reply may be dropped (or an abandoned round's "
              "reply captured) on this stop.", file=sys.stderr)
    # Both resolvers are actor-checked, and so is the pending — all four
    # readers of this record family test the same declared, owner-issued
    # values (`spawn_pairing:` in pipeline/task-fsm.yaml, applied by
    # `transitions.open_pendings`), or a forged `spawn-captured` would
    # SUPPRESS a real capture here (verdict lost silently) while being inert
    # in the others.
    #
    # `abandoned` is the one resolver this function asks for BY NAME — hence
    # the declared mapping being keyed rather than a bare list. A stall
    # override (`--confirm-no-verdict`) declared this spawn dead; a stop
    # arriving afterwards is the slow-not-dead agent, and its verdict must
    # NOT enter the ledger — the round it belonged to was closed out, another
    # agent may already be answering the same question, and latest-wins would
    # hand the run whichever finished last. That is exactly the stale-verdict
    # race guard_spawn's one-live-spawn rule closes on the launch side.
    _abandoned_ids = transitions.closed_agent_ids(prior, "abandoned")
    open_pendings = transitions.open_pendings(prior)
    pending = next((e for e in open_pendings
                    if e.get("agent_id") == agent_id), None) if agent_id else None
    if agent_id and agent_id in _abandoned_ids and any(
            transitions.is_open_pending_record(e)
            and e.get("agent_id") == agent_id for e in prior):
        # Loud, like every other capture that deliberately does nothing: a
        # dropped verdict must never be a silent no-op (the undiagnosable
        # failure this file has precedent for). Token accounting below still
        # runs — the agent burned those tokens whatever the ledger decided
        # about its verdict, and the pending is already PAIRED by the
        # abandonment, so the gauge and run health are settled; the visible
        # anomaly record is the `stall-verdict-override` the stall verb wrote.
        print(f"ai-sdlc-harness: background spawn '{agent_id}' stopped, but "
              "its `spawn-pending` was ABANDONED by a stall override — this "
              "reply arrived after abandonment and was NOT captured (no "
              "verdict row, no status-block event). That round was declared "
              "dead; act on the re-spawn that replaced it.", file=sys.stderr)
    if not agent_id and open_pendings:
        # The pairing key is missing on a stop that could plausibly be the
        # one a pending is waiting for. Silent before (the whole gate simply
        # never ran), which is the failure mode the agent_type-absent
        # fallback above was added for: an undiagnosable no-op.
        print(f"ai-sdlc-harness: a subagent-stop payload carried no agent_id "
              f"while {len(open_pendings)} background spawn(s) are still "
              "awaiting capture — this stop could not be paired, so its "
              "verdict/status was NOT captured (HARNESS_HOOK_DEBUG=1 records "
              "raw payloads).", file=sys.stderr)
    if pending is None and agent_id:
        # THE UPGRADE WINDOW, and it must be loud. A pending written by a
        # pre-round-4 harness carries the spawn SHAPE where `actor` now lives
        # ("reviewer", "developer"), so it fails the anti-forgery actor check
        # and is invisible to every reader of this family — this one
        # included. Executed (round-4 review): the pending was absent from
        # the gauge, run health read HEALTHY, `open_spawn_pendings` was
        # empty, the re-spawn and the stall were both ALLOWED over a live
        # agent, and this hook exited 0 with an EMPTY stderr. No reviews row,
        # no `spawn-captured`, no `missing-status-block`: a verdict gone with
        # nothing anywhere saying so — round 3's stale-verdict race, reopened
        # for the length of the upgrade.
        #
        # The fix is NOT to widen the actor bound. Accepting a declared spawn
        # shape as an alternate actor is exactly the forgery round 4 closed
        # (and the schema now refuses such a declaration outright). Deferred
        # capture across the upgrade is genuinely impossible — the two hook
        # processes share nothing but this record and its owner half is
        # gone — so the honest move is to SAY so and name the one recovery,
        # which is the same one every uncapturable spawn gets.
        #
        # `not is_open_pending_record` is the whole test: right kind, wrong
        # (or absent) owner. It also keeps this off the two paths that
        # legitimately reach here with no open pending and a WELL-FORMED one
        # in the ledger — an abandoned round (reported a few lines above) and
        # a re-delivered stop for an already-captured spawn.
        _spec = (transitions.spawn_pairing().get("pending") or {})
        if any(e.get("kind") == _spec.get("kind")
               and e.get("agent_id") == agent_id
               and not transitions.is_open_pending_record(e) for e in prior):
            # Token accounting below still runs, exactly as on the abandoned
            # path: the agent burned those counts whatever the ledger decided
            # about its verdict. The VERDICT is what is lost, so say only
            # that.
            print(f"ai-sdlc-harness: background spawn '{agent_id}' stopped, "
                  "and this run holds a LEGACY `spawn-pending` for it — one "
                  "written by a pre-upgrade harness, whose owner field this "
                  "version cannot verify. Its deferred capture is impossible: "
                  "NO verdict row and NO status-block event were recorded for "
                  "this spawn, and no guard sees it as in flight, so nothing "
                  "else will report it. Re-spawn this agent in the FOREGROUND "
                  "(its reply is captured at PostToolUse, needing no pending) "
                  "and read the verdict from the ledger. Drain in-flight "
                  "spawns before upgrading to avoid this.", file=sys.stderr)
    completed_pending = False
    if pending is not None:
        # task/mode/shape come from the PENDING record, not re-derived: the
        # spawn prompt reached capture_post_spawn whole (mandated headers,
        # tool_input.prompt), while this transcript's first user turn is
        # whatever the platform chose to replay into it — the transcript
        # headers stay as the fallback for a pending that carried none.
        pend_task = pending.get("task") or task
        pend_mode = pending.get("mode") or mode
        # `shape`, never `actor`: since round 4 the pending's actor is the
        # capture-owned "capture" (the anti-forgery bound every record in
        # this family carries), and the spawn shape has its own key — the
        # same split `spawn-captured` below already used. The old
        # `or pending.get("actor")` fallback would now resolve a missing
        # shape to the literal "capture", filing a verdict under a shape no
        # surface declares.
        pend_shape = pending.get("shape") or shape
        # ONLY the subagent's own transcript, else the payload's own copy of
        # its final message — never the parent-session fallback (see the
        # `agent_tx` note above). `last_assistant_message` is documented as
        # a string but has shipped as a content-block dict, so it goes
        # through the same flattener PostToolUse replies use.
        lam = p.get("last_assistant_message")
        text = agent_reply or (lam if isinstance(lam, str)
                               else _response_text(lam))
        if not text:
            # NO capture and NO `spawn-captured`: the pending stays OPEN,
            # because a dangling flag is the honest record of "this spawn's
            # evidence never arrived" and the gauge already exists to show
            # it. The alternative — closing the pending on empty text — ran
            # _capture_reply over "" and FABRICATED a missing-status-block
            # stall while losing a verdict `last_assistant_message` was
            # carrying (adversarial review, executed). Stderr, not the event
            # ledger, matching the agent_type-absent fallback: capture that
            # silently no-ops is the undiagnosable failure.
            print(f"ai-sdlc-harness: background spawn '{agent_id}' stopped "
                  "but neither its own transcript nor last_assistant_message "
                  "carried any reply text — verdict/status NOT captured, the "
                  "spawn-pending flag stays OPEN — and an open pending holds "
                  "this (task, mode) against a re-spawn, so recover by "
                  "abandoning it first: `harness stall --confirm-no-verdict` "
                  "(add `--task` for a per-task spawn), then re-spawn FRESH.",
                  file=sys.stderr)
        else:
            _capture_reply(run, pend_shape, pend_mode, pend_task, text)
            # kind AND actor come from the same declaration the readers
            # match on, so this writer cannot drift from them. `actor` is
            # CAPTURE-owned, like agent-identity's: it is the value every
            # reader tests before letting this record clear a pending.
            # Holding the spawn SHAPE here instead made the pairing forgeable
            # by anything that can write the ledger — executed: a hand-run
            # `harness log-event --json
            # '{"kind":"spawn-captured","agent_id":"a-1"}'` cleared a real
            # pending, and the agent_id is no secret, it is published in the
            # same readable ledger. The shape has its own key.
            captured = (transitions.spawn_pairing()
                        .get("resolvers") or {})["captured"]
            ndjson.append_record(run / "events.ndjson", {
                "kind": captured["kind"], "actor": captured["actor"],
                "agent_id": agent_id, "task": pend_task,
                "shape": pend_shape, "mode": pend_mode})
            # ONE attribution for this stop. Before this, the verdict row
            # took the pending's (task, mode, shape) while the token row a
            # few lines below took the TRANSCRIPT's — so one background
            # spawn wrote a verdict under (review, T1) and its cost under
            # (plan-attack, T-OTHER) whenever the platform replayed a
            # different first user turn (adversarial review, executed).
            # Split-brain attribution on the two ledgers a human reconciles.
            task, mode, role = pend_task, pend_mode, pend_shape
            completed_pending = True
    usage = data.get("usage") or {}
    input_t = usage.get("input_tokens", 0)
    output_t = usage.get("output_tokens", 0)
    cache_r = usage.get("cache_read_input_tokens", 0)
    cache_w = usage.get("cache_creation_input_tokens", 0)
    # Qwen Code double-write guard. Two transcript families meet here, and
    # the skip must fire for exactly one of them:
    #
    # FOREGROUND Qwen: this stop's transcript is the PARENT session's chat
    # file (Gemini JSONL — top-level usageMetadata on every assistant
    # record, counts that belong to the PARENT, not this spawn), and
    # capture_post_spawn has ALREADY written the spawn's real token row
    # from the Task tool's executionSummary. Appending here would duplicate
    # that ledger entry with the parent's counts.
    #
    # BACKGROUND Qwen: the transcript is the AGENT'S OWN (measured live on
    # 0.22.2) and its top-level usageMetadata IS this spawn's cost — the
    # launch stub never carried an executionSummary, so this stop is the
    # ONLY event that can write the row.
    #
    # The discriminators are the pending gate (only a background stop pairs
    # one) and the family flag _parse_transcript now returns
    # (`usage_source: "gemini"`, set by the presence of top-level
    # usageMetadata). The ORIGINAL discriminator — all-zero counts AND no
    # model — died with usageMetadata parsing: a Gemini transcript with
    # real counts and no model would read as "Claude with counts",
    # double-writing the foreground row AND stamping the run "claude-code".
    # The zero-signature keeps its historical secondary role: a degenerate
    # or unparseable transcript (no counts, no model — a failed Qwen spawn
    # pre-executionSummary, or a Claude transcript with no assistant turn)
    # is still dropped rather than placeholder-written, both accepted
    # corners unchanged from before.
    #
    # A stop that COMPLETED a pending is never skipped, either family: the
    # two hooks ARE coordinated for that spawn by the pending record
    # itself, and what capture_post_spawn wrote for it was a launch stub,
    # never a token row. The row is written even at zero/None: "this spawn
    # ran and reported no counts" is a real, reconcilable fact; silence is
    # not.
    gemini_tx = data.get("usage_source") == "gemini"
    zero_sig = (not any((input_t, output_t, cache_r, cache_w))
                and data.get("model") is None)
    if not completed_pending and (gemini_tx or zero_sig):
        return
    ndjson.append_record(run / "tokens.ndjson", {
        "task": task, "mode": mode, "role": role,
        "model": data.get("model"),
        "input": input_t, "output": output_t,
        "cache_read": cache_r, "cache_write": cache_w})
    # Identity, family-keyed: a Gemini transcript that reached this line
    # completed a pending — a measured Qwen BACKGROUND spawn, whose
    # executionSummary sibling never fires for a stub, so the run's driver
    # is qwen-code and this is the one event that can say so. Claude-family
    # rows keep the historical model-keyed stamp, and the degenerate
    # zero-signature corner (a completed pending over a count-less Claude
    # transcript) keeps stamping nothing, exactly as before — stamping that
    # run "claude-code" over a model-less transcript would misattribute
    # the very question this record exists to answer.
    if gemini_tx:
        if completed_pending:
            _record_agent_identity(run, "qwen-code", None)
    elif not zero_sig:
        _record_agent_identity(run, "claude-code", data.get("model"))
    # Reviewer-verdict and missing-status-block capture is NOT unconditional
    # here: it runs above, and ONLY behind the `spawn-pending` gate — i.e.
    # only for a background spawn, whose reply reaches no other hook.
    # Foreground replies are still captured exclusively at PostToolUse
    # (dogfood finding: this event's payload proved unreliable in practice —
    # transcript-path ambiguity, silent no-ops — and the review ledger is
    # FSM-guard-critical, so it anchors to the one payload that carries the
    # spawn prompt and the final reply deterministically). The pending gate
    # is precisely what lets the background path borrow this event without
    # giving up that anchor: the ledger record proves which spawn this stop
    # belongs to and that nothing else captured it. Everything else this
    # event does stays best-effort token accounting.


def _record_agent_identity(run: Path, cli: str, model: str | None) -> None:
    """Once per run per (cli, model): WHICH agent CLI drove this run.

    field: dual-run comparison — one run's token rows all carried
    `model: null` (a documented limitation of that CLI) and nothing anywhere
    recorded which CLI produced them. The user asked mid-run "what model are
    the reviewers using?" and the artifacts could not answer; attribution for
    the whole comparison ended up resting on a retro happening to mention a
    `.qwen` path. The discrimination is free — the token-capture branches
    already tell the two payload shapes apart — it just was never written
    down. Model may legitimately be null (that CLI reports none); the CLI
    name alone still answers the question the artifacts could not.

    ACCEPTED RACE: the dedupe is read-then-append with no lock, and
    plan-review.md mandates batching every lens spawn in ONE message — so N
    concurrent SubagentStop hooks can all read "no record" and all append,
    giving up to N identical rows (re-verify finding). Left as-is
    deliberately: the kind is informational, not flagged, so a duplicate
    inflates no gauge and changes no decision, and taking the run lock in a
    hook on a purely descriptive record would buy consistency nobody reads
    at the cost of serializing the panel's captures.
    """
    try:
        prior = ndjson.read_records(run / "events.ndjson")
    except OSError:
        return
    if any(e.get("kind") == "agent-identity" and e.get("cli") == cli
           and e.get("model") == model for e in prior):
        return
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "agent-identity", "cli": cli,
                          "model": model, "actor": "capture"})


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
        # Qwen Code: the Agent/Task tool_response is
        # {"llmContent": <content>, "returnDisplay": <display>} — llmContent is
        # normally [{"text": <reply>}], a plain string on the ERROR terminate
        # path. `content` stays FIRST (Claude precedence, byte-identical); the
        # list/str branches above then flatten either llmContent encoding.
        if "llmContent" in resp:
            return _response_text(resp["llmContent"])
        return str(resp.get("text") or "")
    return ""


def _qwen_stub_agent_id(tool_response: dict, payload: dict) -> str | None:
    """The pairing id out of Qwen Code's MEASURED background launch stub
    (0.22.2): the stub's `llmContent` is a human-readable TEXT block whose
    one machine-readable line prints `task_id: <id>` — and that same value
    is what the agent's SubagentStop later carries as `agent_id`, the only
    key the two one-shot hook processes share. The stub carries no
    structured id field of its own (unlike Claude's `agentId`), so the
    primary parse is that printed line; the fallback composes the id from
    the structured fields the platform derives it from
    (`returnDisplay.subagentName` + the payload's top-level
    `tool_call_id`) — measured equal to the printed task_id on live
    spawns. Neither present: None, and the caller records the
    unpairable-stub event instead of a pending."""
    text = _response_text(tool_response.get("llmContent"))
    m = re.search(r"^task_id:[ \t]*(\S+)", text, re.MULTILINE)
    if m:
        return m.group(1)
    display = tool_response.get("returnDisplay")
    name = display.get("subagentName") if isinstance(display, dict) else None
    call_id = payload.get("tool_call_id")
    if (isinstance(name, str) and name
            and isinstance(call_id, str) and call_id):
        return f"{name}-{call_id}"
    return None


def _capture_reply(run, shape, mode, task, text) -> None:
    """Verdict / status-block / stall capture over ONE subagent's final
    reply — the whole of it, shared by the TWO events that can carry that
    reply so the two paths cannot drift apart.

    A FOREGROUND spawn's reply arrives on PostToolUse (capture_post_spawn,
    the historical sole caller). A BACKGROUND spawn's PostToolUse carries
    only a launch stub, and its real reply reaches nothing but SubagentStop
    — which is now the common shape, not a corner, since Claude Code
    2.1.232 backgrounds subagent spawns BY DEFAULT. Both callers need
    byte-identical treatment (same kinds, same order, same fields): this
    writes the reviews.ndjson row the task FSM's `reviewer-approved` guard
    reads, so a second, separately-maintained copy of these ninety lines is
    exactly how one path would quietly stop capturing a verdict the other
    still captured."""
    captured = None
    if shape == "reviewer":
        captured = extract_verdict(text)
        if captured:
            # The completion evidence the task FSM's `reviewer-approved`
            # guard requires: which task (spawn-prompt header), which
            # reviewer mode, what verdict. Written only here; scoped to the
            # final status block and conflict-fail-closed (extract_verdict).
            # `blocking-findings` and the plan generation ride ALONGSIDE the
            # verdict, never gating it: the engine's exits read `verdict`
            # only, so a missing or malformed count can never change a
            # transition — it just leaves that round's convergence
            # unrecorded (field: dual-run comparison).
            # Scoped to the FINAL status block exactly like extract_verdict
            # (pre-release review, both lenses: a prose recap of a previous
            # round — "round 1 had blocking-findings: 9" — must not become
            # THIS round's count when the final block omits the optional
            # line); same whole-text fallback for a malformed block.
            _m = list(STATUS_RE.finditer(text))
            _scope = text[_m[-1].start():] if _m else text
            bf = (BLOCKING_RE.findall(_scope) or [None])[-1]
            # Best-effort, and actor-checked like outstanding_flagged (a
            # stray `log-event` record must not move the generation). The
            # try/except is load-bearing: this read sits in front of the
            # FSM-critical verdict append, and an OSError here used to
            # abort the whole capture — verdict lost over an advisory count
            # (pre-release review).
            try:
                plans = sum(1 for e in ndjson.read_records(run / "events.ndjson")
                            if e.get("kind") == "plan-registered"
                            and e.get("actor") == "plan-register")
            except OSError:
                plans = None
            ndjson.append_record(run / "reviews.ndjson", {
                "task": task, "mode": mode, "verdict": captured,
                "blocking_findings": int(bf) if bf is not None else None,
                "plan_generation": plans})
        elif VERDICT_ANYWHERE_RE.search(text):
            # A verdict exists but isn't line-anchored in the final status
            # block — correctly NOT captured (fail-closed), but say so and
            # name the one recovery that works: a whole fresh spawn.
            # SendMessage/resume replies pass through no capture hook, so
            # a restated verdict there can never register (field finding).
            ndjson.append_record(run / "events.ndjson", {
                "kind": "verdict-uncaptured", "task": task, "actor": shape,
                "reason": "a verdict: token appears in the reply but not as "
                          "its own line in the final status block — not "
                          "captured (fail-closed). Recover by re-spawning "
                          "the reviewer FRESH (same headers, a NEW spawn); "
                          "never SendMessage/resume — those "
                          "replies bypass capture entirely"})
    if not STATUS_RE.search(text):
        if captured and mode in ENGINE_VERDICT_MODES:
            # An engine-read captured verdict IS the gate-critical signal,
            # so this is NOT a stall (field run 459226: a reviewer reply
            # carried a line-anchored verdict — captured above via
            # extract_verdict's no-block whole-text fallback — yet this
            # branch still fired missing-status-block at the same
            # timestamp, and the stall procedure re-spawned the whole plan
            # panel to re-derive a verdict the ledger already held, ~1h
            # paid twice). Record a distinct event that the stall
            # procedure must NOT act on — but keep it FLAGGED (it is in
            # FLAGGED_EVENT_KINDS): the no-block capture rode the
            # whole-text fallback, the weakest path, and suppressing the
            # stall also suppressed the re-spawn whose fresh verdict used
            # to supersede a false capture (latest-wins) — visibility to
            # the human is the replacing safeguard (adversarial-review on
            # this change, both lenses independently). The fail-closed
            # floor is untouched — an inline (non-anchored) verdict is
            # still uncaptured and still stalls below.
            ndjson.append_record(run / "events.ndjson", {
                "kind": "status-block-malformed", "task": task,
                "actor": shape, "mode": mode,
                "reason": "reviewer verdict captured, but the reply had no "
                          "well-formed final status block — verdict "
                          "recorded, no stall; end replies ON the block "
                          "(shared/status-block.md)"})
        else:
            ndjson.append_record(run / "events.ndjson", {
                "kind": "missing-status-block", "task": task, "actor": shape,
                "reason": "subagent replied without a status block — "
                          "stalled-agent procedure applies (coverage B4)"})
    if shape == "reviewer" and mode == "plan-attack":
        # Non-flagged completion marker for the panel-serialization
        # detector (guard_spawn): a captured verdict or a reviews.ndjson
        # record is NOT a reliable completion signal (a lens can finish
        # with a well-formed block and no verdict line and leave no trace
        # at all), so completion gets its own event. Round-scoped by the
        # actor-checked plan-registered marker, the same boundary the
        # plan-flag supersession uses.
        ndjson.append_record(run / "events.ndjson", {
            "kind": "lens-complete", "actor": shape, "mode": mode})


def capture_post_spawn(p: dict) -> None:
    """PostToolUse on Agent/Task — the authoritative writer of the
    reviewer-verdict ledger (reviews.ndjson) and the missing-status-block /
    status-block-malformed events. Anchored here, not SubagentStop (dogfood
    finding: an entire
    run's SubagentStop captures silently no-opped — payload shape is
    version-dependent and its transcript_path is documented-ambiguous),
    because THIS payload deterministically carries the spawn prompt
    (tool_input.prompt: the mandated headers) and the subagent's final
    reply (tool_response) — the exact two inputs the capture needs."""
    tool_input = p.get("tool_input") or {}
    shape = shape_of(tool_input.get("subagent_type"))
    surfaces = load_yaml(PLUGIN_ROOT / "pipeline" / "surfaces.yaml")
    if shape not in surfaces["shapes"]:
        # Defense in depth for guard_spawn's WI-2 block (older guard copy,
        # PreToolUse disabled, hook ordering). If this near-miss reaches
        # capture, record what happened instead of returning silently —
        # the ledger must be able to explain WHY a verdict is missing.
        prompt = tool_input.get("prompt") or ""
        if MODE_HEADER_RE.search(prompt):
            cwd_cap = _session_workspace(Path(p.get("cwd") or "."))
            runs_cap = live_runs(cwd_cap)
            # attribute via the harness-run: header (the codebase's own
            # convention), not runs_cap[0] — in a multi-run workspace the
            # first-sorted run silently misattributes every other run's
            # events. Fall back to the single-run case, else stderr.
            run_cap = (_resolve_run(runs_cap, prompt, cwd_cap)
                       if runs_cap else None)
            if run_cap is None and len(runs_cap) == 1:
                run_cap = runs_cap[0]
            if run_cap:
                ndjson.append_record(run_cap / "events.ndjson", {
                    "kind": "spawn-shape-unrecognized",
                    "subagent_type": tool_input.get("subagent_type",
                                                    "(omitted)"),
                    "mode": (MODE_HEADER_RE.search(prompt)
                             or [None, None])[1]})
            elif runs_cap:
                print(f"ai-sdlc-harness: could not attribute a "
                      f"spawn-shape-unrecognized event to one of "
                      f"{len(runs_cap)} live runs (no matching "
                      f"harness-run: header) — event not recorded.",
                      file=sys.stderr)
        return  # not a harness shape — none of our business
    cwd = _session_workspace(Path(p.get("cwd") or "."))
    runs = live_runs(cwd)
    if not runs:
        return
    prompt = tool_input.get("prompt") or ""
    task = (TASK_HEADER_RE.search(prompt) or [None, None])[1]
    mode = (MODE_HEADER_RE.search(prompt) or [None, None])[1]
    run = _resolve_run(runs, prompt, cwd)
    if (run is None and RUN_HEADER_RE.search(prompt) is None
            and {"shape": shape, "mode": mode} in (
                surfaces.get("out_of_run_spawns") or [])):
        # An OUT-OF-RUN spawn belongs to no run by declaration
        # (surfaces.yaml: `/repo-map-refresh` is legal with no run at all),
        # and it carries no `harness-run:` header because there is no run to
        # name. The single-run fallback below would nonetheless file its
        # records into whichever run happens to be live — adversarial review
        # executed it: a backgrounded repo-map spawn wrote a `spawn-pending`
        # {mode: repo-map, task: None} into an unrelated dev-workflow run,
        # which DEGRADED that run's health and (before the mode-bound
        # matching in transitions) wedged its step-keyed stalls behind a
        # pending no orchestrator of that run had ever spawned. Write
        # NOTHING, anywhere: this mode's product is files on disk, not a
        # ledger verdict, and no run is entitled to its records. A
        # header-LESS spawn of an in-run mode still takes the fallback — that
        # one really does belong to a run, and losing its verdict is the
        # worse failure.
        print(f"ai-sdlc-harness: out-of-run spawn ({shape}, {mode}) carries "
              "no harness-run: header (it belongs to no run — "
              "surfaces.yaml's out_of_run_spawns) — nothing recorded for it "
              f"in any of the {len(runs)} live run(s).", file=sys.stderr)
        return
    if run is None and len(runs) == 1:
        run = runs[0]
    if run is None:
        print(f"ai-sdlc-harness: could not attribute a subagent reply to one of "
              f"{len(runs)} live runs (no matching harness-run: header) — "
              "review-verdict/stall capture for this invocation is not "
              "recorded.", file=sys.stderr)
        return
    tool_response = p.get("tool_response")
    # An async LAUNCH STUB, recognised by the RESPONSE's own shape rather
    # than by the run_in_background parameter. Two MEASURED shapes:
    # Claude Code 2.1.232 — {"isAsync": true, "status": "async_launched",
    # "agentId": …, "outputFile": …}; Qwen Code 0.22.2 —
    # {"llmContent": "<text stub printing `task_id: …` and `output_file:
    # …`>", "returnDisplay": {type: "task_execution", subagentName, …,
    # status: "background"}} — i.e. both are everything except the reply,
    # and Qwen's id needs deriving (see _qwen_stub_agent_id). A foreground
    # Qwen completion answers the same returnDisplay with
    # status: "completed" + an executionSummary, which this gate does NOT
    # match — it falls through to reply capture below, byte-identically to
    # today.
    #
    # Shape FIRST because the parameter stopped being evidence: both CLIs
    # can background subagent spawns BY DEFAULT and Qwen's payload schema
    # does not echo `run_in_background` back into tool_input at all, so
    # the param branch below never fired and the stub fell straight
    # through to verdict capture — where its (necessarily) absent status
    # block FABRICATED a missing-status-block stall for an agent that was
    # still happily running, and the stall procedure's reinvoke then raced
    # the live original. Shape-detection holds whatever the parameter says
    # or omits.
    #
    # Truthiness, not `is True`: the identity check missed `isAsync: 1`
    # (and every other truthy spelling a schema revision might use) and
    # fell through to exactly the fabrication above (adversarial review).
    #
    # The gate DECIDES THE BRANCH OUTRIGHT — a recognised stub never reaches
    # _capture_reply, with or without a pairable id. Anything else is the
    # fabrication again: the stub carries no reply by construction, so
    # "capture it anyway" can only invent a stall.
    is_stub = False
    agent_id = None
    if isinstance(tool_response, dict):
        if (tool_response.get("isAsync")
                or tool_response.get("status") == "async_launched"):
            is_stub = True
            agent_id = tool_response.get("agentId")
        elif (isinstance(tool_response.get("returnDisplay"), dict)
              and tool_response["returnDisplay"].get("status")
              == "background"):
            is_stub = True
            agent_id = _qwen_stub_agent_id(tool_response, p)
    if is_stub:
        # The reply is NOT lost, it is merely late: SubagentStop fires for
        # background spawns at completion and carries the transcript. This
        # record is the handoff between the two one-shot hook processes —
        # they share no state but this ledger — and capture_subagent_stop
        # finishes the job against it (`spawn-captured`). A pending left
        # dangling means that stop never came, and it is FLAGGED so the
        # human sees the hole.
        if isinstance(agent_id, str) and agent_id:
            # kind + actor from the declaration every reader matches on
            # (`spawn_pairing.pending`), so the asserting record carries the
            # SAME anti-forgery bound its resolvers always had. It was the one
            # record in the family matched on `kind` alone — executed
            # (whole-system review, round 4): a hand-written `log-event`
            # spawn-pending BLOCKED the next legitimate spawn for that (task,
            # mode), made the stall that would clear it REFUSE, and degraded
            # run health. The spawn SHAPE, which used to sit in `actor`, moves
            # to its own key exactly as `spawn-captured`'s already had.
            pend = transitions.spawn_pairing()["pending"]
            # The SPAWNING STEP, read off the run's own cursor. Health degrades
            # on a pending the run LEFT BEHIND, not on one still in flight
            # where it was launched (workflow.run_health) — without this, a
            # DAG-pipelined develop with N lanes live reads DEGRADED
            # continuously, which is the mid-run signal the dashboard exists to
            # give. Best-effort by design: an unreadable state must never cost
            # the run the pending itself (a lost pending loses a real verdict),
            # so the key is simply absent, and absent reads as degrade —
            # fail-closed, i.e. exactly today's behaviour.
            step = None
            try:
                step = ((read_state(run, cwd).get("cursor")
                         or {}).get("current_step"))
            except Exception as exc:                          # noqa: BLE001
                print(f"ai-sdlc-harness: could not read {run}'s cursor while "
                      f"recording a spawn-pending ({type(exc).__name__}: "
                      f"{exc}) — the pending is recorded WITHOUT its step, so "
                      "it degrades run health until it pairs off.",
                      file=sys.stderr)
            ndjson.append_record(run / "events.ndjson", {
                "kind": pend["kind"], "actor": pend["actor"],
                "task": task, "shape": shape, "step": step,
                "mode": mode, "agent_id": agent_id,
                "reason": "harness-shape spawn launched in the BACKGROUND — "
                          "PostToolUse saw only the launch stub, so verdict/"
                          "status capture is deferred to this agent's "
                          "SubagentStop. Still outstanding means that stop "
                          "never arrived (subagent crashed, or the session "
                          "ended under it) and NOTHING was captured for it — "
                          "abandon it (`harness stall --confirm-no-verdict`, "
                          "`--task` for a per-task spawn) and re-spawn FRESH; "
                          "while open it holds this (task, mode) against a "
                          "second spawn — EXCEPT for the deliberately "
                          "unserialized classes: plan-attack lenses and "
                          "always-legal modes are never refused on it (see "
                          "guard_spawn), and an out-of-run mode writes no "
                          "pending at all"})
            return
        # No agentId: the handoff has no key, so SubagentStop can never pair
        # this spawn's stop to anything and the deferred capture is not
        # merely late, it is impossible. Record the honest uncapturable
        # outcome — which is HEALTH_DEGRADING, unlike a pending — instead of
        # falling through (adversarial review, executed on the real 2.1.232
        # schema, which does NOT echo run_in_background: an id-less stub
        # reached _capture_reply and fabricated a missing-status-block stall
        # for a live agent, the exact bug shape-detection was added to kill).
        ndjson.append_record(run / "events.ndjson", {
            "kind": "background-spawn-uncaptured", "task": task,
            "actor": shape, "mode": mode,
            "reason": "harness-shape spawn launched in the BACKGROUND and "
                      "its launch stub carried NO agentId — unpairable: "
                      "nothing can match this spawn's SubagentStop, so its "
                      "verdict/status can never be captured. Re-spawn FRESH "
                      "(under Qwen Code, in the FOREGROUND — "
                      "run_in_background: false); batch multiple spawns in "
                      "one message for parallelism"})
        return
    text = _response_text(tool_response)
    if (tool_input.get("run_in_background") in (True, "true", "True")
            and not STATUS_RE.search(text)
            and extract_verdict(text) is None):
        # The parameter-keyed fallback for a background launch whose
        # response shape this build does not recognise at all (a future or
        # older CLI whose stub carries neither `isAsync` nor
        # `status: async_launched`): the subagent's real reply may never
        # reach any hook payload, so capture can't be promised here (an
        # APPROVED verdict would be lost, and the stub's missing status
        # block FABRICATED a stall event whose reinvoke then raced the
        # still-live background original). guard_spawn blocks explicit-param
        # backgrounds under Qwen Code, whose stub is the unmeasured one this
        # branch is really about; on Claude Code the shape gate above
        # recognises the stub and this is belt-and-braces for an unknown
        # schema — either way, record what actually happened instead of fake
        # stall evidence.
        #
        # RESPONSE-CONDITIONED, not param-conditioned. When WI-3 was
        # rescoped to Qwen, `run_in_background: true` became LEGAL on Claude
        # Code — and this branch, keyed on the param alone, then fired over
        # responses that carried the agent's whole reply: adversarial review
        # executed it and watched a captured `verdict: APPROVED` be thrown
        # away and the run marked DEGRADED for an uncapturable spawn that
        # had in fact reported in full. The param stopped being proof of a
        # stub the moment it stopped being forbidden; the RESPONSE SHAPE is
        # the evidence. So: only when the reply holds nothing capturable —
        # no status block and no extractable verdict — is this the honest
        # record. Anything else falls through to the normal capture, which
        # already handles every partial shape (blockless reply, malformed
        # block, verdict-without-block) on its own terms.
        ndjson.append_record(run / "events.ndjson", {
            "kind": "background-spawn-uncaptured", "task": task,
            "actor": shape, "mode": mode,
            "reason": "harness-shape spawn ran in the background and its "
                      "launch response was not a recognised stub — only that "
                      "response reaches PostToolUse, so verdict/status "
                      "capture is impossible; re-spawn FRESH (under Qwen "
                      "Code, in the FOREGROUND — run_in_background: false), "
                      "batching multiple spawns in one message for "
                      "parallelism"})
        return
    # Qwen Code, FOREGROUND: a spawn's token counts live in the PostToolUse
    # payload — tool_response.returnDisplay.executionSummary =
    # {inputTokens, outputTokens, thoughtTokens, cachedTokens, totalTokens, …},
    # with no model field — because a foreground stop's transcript is the
    # PARENT session's chat file, whose usageMetadata is the parent's cost.
    # (A BACKGROUND spawn takes the stub return above; its counts arrive
    # later from its OWN transcript's usageMetadata, at SubagentStop.)
    # Same tokens.ndjson schema capture_subagent_stop
    # writes: task/mode from the spawn-prompt headers above, role = the spawn
    # shape, model None (Qwen carries none). cachedTokens → cache_read; there is
    # no cache-creation analogue, so cache_write is 0. thoughtTokens is
    # deliberately left OUT of input/output — the ledger records actual billed
    # input/output, and folding reasoning tokens into either would fabricate a
    # count that was never spent as such. Claude Code payloads carry no
    # executionSummary, so this branch never fires there. When a Qwen spawn
    # FAILS before an executionSummary exists (hard exception / worktree-
    # provisioning failure / subagent-not-found: returnDisplay carries a
    # `status: failed` but no executionSummary), no token row is written here
    # and SubagentStop's count-less transcript is skipped too — an accepted
    # drop (a failed spawn has no billed counts), and the failure is still
    # recorded as the missing-status-block stall event captured below.
    if isinstance(tool_response, dict):
        display = tool_response.get("returnDisplay")
        summary = display.get("executionSummary") if isinstance(display, dict) else None
        if isinstance(summary, dict):
            ndjson.append_record(run / "tokens.ndjson", {
                "task": task, "mode": mode, "role": shape, "model": None,
                "input": summary.get("inputTokens", 0),
                "output": summary.get("outputTokens", 0),
                "cache_read": summary.get("cachedTokens", 0),
                "cache_write": 0})
            # executionSummary is Qwen Code's signature (Claude payloads
            # carry none) — the same discrimination this branch already
            # makes, now written down
            _record_agent_identity(run, "qwen-code", None)
    _capture_reply(run, shape, mode, task, text)


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
    unanswerable after the fact without this).

    DIAGNOSTIC ONLY, and since round 4 that costs something while it is on:
    the target is under `Path.home()`, so `ndjson.ledger_lock` puts its
    sidecar at `~/.ledger.lock` — one MACHINE-WIDE lock every hook of every
    run then serializes on, for a file nothing reads at runtime."""
    import os
    if os.environ.get("HARNESS_HOOK_DEBUG") != "1":
        return
    try:
        ndjson.append_record(Path.home() / ".ai-sdlc-harness-hook-debug.ndjson",
                             {"guard": name, "payload": payload})
    except OSError:
        pass  # debug aid only — never let it affect the guard


def main() -> None:
    # Guard stderr is read by the platform (and by the test suite) as
    # UTF-8; the messages carry em-dashes/arrows, and Windows' default
    # cp1252 pipe encoding would mojibake them — same output contract as
    # harness/__main__.py. errors= must be RESTATED: reconfigure resets it
    # to "strict", and stderr's documented default is "backslashreplace" —
    # under strict, a block() message interpolating an un-encodable payload
    # char (a lone surrogate in a path token) RAISES inside print, and a
    # fail-open guard then exits 0: the write it just decided to block is
    # allowed (adversarial-review finding on this very change, CONFIRMED
    # with an end-to-end repro — the one edit here that had silently moved
    # POSIX behavior).
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    # stdin carries the platform's hook payload, which is ALWAYS UTF-8
    # JSON — but a Windows pipe defaults to cp1252+surrogateescape, which
    # decodes multibyte prompts into mojibake carrying lone surrogates:
    # the capture verbs then either garble the evidence ledger or lose the
    # record entirely when a surrogate hits ndjson's strict utf-8 encode
    # (adversarial-review finding on the launcher change, CONFIRMED by
    # probe). strict errors= is deliberate: genuinely non-UTF-8 bytes
    # should land in the except below as an unparseable payload, taking
    # each verb's declared fail-open/fail-closed posture.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
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
