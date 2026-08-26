"""Gate decisions anchored to captured human input (RC3 + RC4 spec).

A gate token is written ONLY by deriving the decision from a real record in
human-input.ndjson (captured by the UserPromptSubmit hook) — deterministic
code parses hook-captured human text; the orchestrator can neither fabricate
nor interpret an approval.

Record selection (RC4): only records strictly after the LATEST
`gate_presented_at` stamp qualify; the most recent qualifying record wins;
an ad-hoc/triage interaction re-stamps (caller re-presents). No qualifying
or no parseable record -> refusal (fail closed; `APPROVED but change X`
routes to ad-hoc handling, never a token).
"""
from __future__ import annotations

import hashlib
import re

APPROVED_RE = re.compile(r"^\s*APPROVED\s*\.?\s*$", re.IGNORECASE)
NUMBER_RE = re.compile(r"^\s*\[?([0-9]+)\]?\s*$")


class GateRefusal(Exception):
    pass


def session_digest(session: object) -> str | None:
    """Stable, non-identifying stamp for a platform session id — the ONE
    implementation, used from three places that must all agree byte for
    byte: `present` stamps the presenting session onto the gate entry, the
    UserPromptSubmit hook TAGS each captured record with the prompt's
    `session_id`, and `decide` compares the two (plus the deciding
    session). A second, drifted implementation would not raise; it would
    just stop comparing equal, and the symptom is a refused gate.

    A digest and not the raw id because `state.yaml` is NOT private:
    `publish_mirror` (harness/gitops.py:591-635) snapshots the whole run
    dir into the feature branch, and `_mirror_excluded` (gitops.py:582-588)
    excludes only `.hmac` files and the MIRROR_EXCLUDE prefixes — state.yaml
    is not among them, so it is committed and pushed. Storing the raw UUID
    would therefore publish a live session identifier into shared git
    history for every run, forever, to satisfy a check that only ever needs
    equality. Truncated to 16 hex chars: the comparison is between values
    minted seconds apart on one machine, so collision risk is irrelevant,
    and a short opaque token keeps sealed state readable.

    Anything that is not a non-empty string digests to None — "unknown
    identity", which every consumer reads as "do not filter on this".
    Typed `object`, and isinstance-checked rather than truthiness-checked,
    because one caller is a HOOK: a payload is untrusted JSON from another
    process, and `{"session_id": 12345}` on `str.encode` raises
    AttributeError INSIDE the UserPromptSubmit guard — which fails open
    with a stderr line nobody reads and drops capture for EVERY run in the
    workspace (review finding, reproduced). An id that is only whitespace
    is likewise absent, not an identity that can never match."""
    if not isinstance(session, str):
        return None
    session = session.strip()
    if not session:
        return None
    return hashlib.sha256(session.encode()).hexdigest()[:16]


def present(state: dict, gate_id: str, now: str,
            options: list[str] | None = None,
            session: str | None = None) -> None:
    entry = state["gates"].setdefault(gate_id, {})
    # The digest FIRST, and every subsequent test is on the DIGEST, never on
    # the raw `session`. Truthiness of the raw value disagrees with
    # `session_digest` on exactly the inputs that matter — a whitespace-only
    # `CLAUDE_CODE_SESSION_ID` is truthy but digests to None, and storing
    # `entry["session"] = None` from it would write the null key the rule
    # below forbids while leaving the filter inert (review finding, probed).
    digest = session_digest(session)
    stamp = entry.get("session")
    if digest is None and isinstance(stamp, str) and stamp:
        # REFUSING here rather than popping. A present that cannot supply an
        # identity but is about to CLEAR one silently disarms the gate: the
        # filter arms on this stamp, so afterwards the very next prompt from
        # any session in the workspace decides this gate — the original field
        # bug, reproduced on the exact path the session-mismatch refusal
        # steers the caller onto (`--re-present`). Nothing about the state is
        # mutated before this raise, so a refused present is a no-op.
        #
        # This cannot wedge a session-less platform (Qwen Code, a CI shell):
        # the condition needs an EXISTING string stamp, and a workspace whose
        # presents never carried a session id never has one — every gate
        # there stays presentable exactly as before. It fires only for the
        # genuinely mixed case (stamped by a session-aware process, then
        # re-presented from one that is not), where the honest answer is
        # "run this from the session driving the run", and the escape is to
        # export an id for this process.
        raise GateRefusal(
            f"gate '{gate_id}' is stamped with the session that presented "
            "it, and this process reports no session id "
            "(CLAUDE_CODE_SESSION_ID unset or empty) — re-presenting would "
            "CLEAR that stamp and leave the gate decidable by a prompt "
            "typed in any session in this workspace, which is the bug the "
            "stamp exists to prevent. Run the command from the session "
            "driving this run. Exporting CLAUDE_CODE_SESSION_ID for this "
            "process is a way out ONLY on a platform whose prompt-capture "
            "hook tags nothing: the value must be the SAME string that "
            "platform's UserPromptSubmit payload carries as `session_id`, "
            "because that is what the human's replies are tagged with. An "
            "invented value stamps this gate with an identity no reply can "
            "ever carry, and there is no CLI route back — a no-id present "
            "lands here again, and any other id just writes another stamp "
            "nothing matches")
    entry["presented_at"] = now  # re-presenting re-stamps (RC4 selection spec)
    entry.pop("decision", None)
    # Written UNCONDITIONALLY (never setdefault), exactly like `presented_at`
    # above: `decide` below reads this stamp to recognize which captured
    # replies belong to this gate, so a stale stamp is not merely untidy —
    # it names a session that is no longer the one being answered. The
    # `--re-present` escape hatch (the `gate --present --re-present` branch
    # of `harness/cli.py`'s gate command — cited by NAME, since a line range
    # here rots on the next edit above it) exists precisely for "the human's
    # situation changed", which INCLUDES having moved to a new session (the
    # old terminal closed, the session resumed under a new id). Re-stamping
    # is what makes that escape hatch work: the refusal `decide` raises for
    # a session mismatch points the caller straight at it. The pop is the
    # unstamped direction only (there is nothing to lose): absent, NEVER
    # null — an entry that carries `session: null` asserts an identity it
    # does not have, and this state is audit data.
    if digest:
        entry["session"] = digest
    else:
        entry.pop("session", None)
    if options is not None:
        # Sealed into state at presentation time so decide() replays THIS
        # list — the numbering the human replied to can never be redefined
        # between present and decide (adversarial-review finding).
        entry["options"] = options


def qualifying_records(records: list[dict], entry: dict,
                       session: str | None) -> list[dict]:
    """Which captured records may be PARSED into this gate's decision — the
    session-membership half of it (windowing by `presented_at` is the
    caller's job). ONE call site, `decide` below; it is a named function
    because the rule is intricate enough to deserve its own contract and
    its own tests, NOT because anything else shares it.

    It is deliberately NOT the `--present` waiting guard's predicate. That
    guard asks a different question — "would re-stamping DESTROY a reply?"
    — and re-stamping destroys every record in the window, tagged, untagged
    or foreign, so session membership is irrelevant to it (review finding:
    sharing this filter there let a `--present` issued from a session other
    than the one that replied age out the human's genuine answer silently,
    with no refusal and no event). The guard protects the WINDOW; this
    protects the PARSE. They also differ on axes this helper never touches:
    the guard reads the ledger non-strict and treats corruption as "nothing
    waiting" (fail-open, let the present through) where `decide` reads
    strict and refuses, and the guard deliberately protects a qualified
    "APPROVED but…" that `decide` refuses to parse.

    The filter ARMS on the gate's own stamp and on nothing else. An
    unstamped gate predates this scheme, or was presented by a platform
    that reports no session id; either way it cannot vouch for any tag, so
    it must not filter — a known DECIDING session alone arming the filter
    hard-refused genuine evidence on a path that worked before this rule
    existed (mid-gate upgrade: gate unstamped, human replies in A, the
    session is resumed as C, `--decide` from C discarded the reply). Once
    armed, the deciding session only WIDENS what qualifies: `--decide` is
    invoked BY the session driving the run, so a record tagged with it is
    this human talking to this run right now even when the gate was
    presented under an older id (a resumed session). An UNTAGGED record
    ALWAYS qualifies — unknown means usable, which covers pre-fix ledgers,
    Qwen Code, and any platform whose UserPromptSubmit payload carries no
    `session_id`."""
    stamp = entry.get("session")
    # isinstance, not truthiness: this is the one place a digest is read
    # back out of state, and state.yaml can be hand-edited or migrated (the
    # same reason `cli.py` catches TypeError). A non-string `session` would
    # make `allowed` non-empty and unmatchable, wedging every tagged record
    # into a permanent refusal with no way to reply out of it. Wrong SHAPE
    # means "unknown", which means "do not filter" — never "matches
    # nothing".
    if not isinstance(stamp, str) or not stamp:
        return list(records)
    allowed = {stamp}
    deciding = session_digest(session)
    if deciding:
        allowed.add(deciding)
    return [r for r in records
            if not r.get("session") or r.get("session") in allowed]


def parse_decision(text: str, options: list[str],
                   lenient: frozenset = frozenset()) -> str | None:
    if APPROVED_RE.match(text) and "approved" in options:
        return "approved"
    m = NUMBER_RE.match(text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(options):
            return options[n - 1]
    lowered = text.strip().lower()
    for opt in options:
        if lowered == opt.lower():
            return opt
    # Rejection-side leniency (field, session D: "REJECTED — split the web
    # work" refused, costing a triage spawn + a re-present round-trip for
    # the canonical human reply shape at a plan gate): a reply that LEADS
    # with a non-forward option word may carry notes after it —
    # over-rejecting is the safe direction (one loop at most), the notes
    # are exactly the revision input the on_reject step needs, and the
    # full text is already hash-sealed as the gate evidence. FORWARD
    # decisions stay bare-word/number only: a qualified approval (or
    # waive/defer) must never silently move the pipeline forward. The
    # caller names the non-forward options (manifest forward_on); the
    # library default is empty — strict — so nothing loosens by accident.
    for opt in options:
        if opt in lenient and re.match(rf"^\s*{re.escape(opt)}\b", text,
                                       re.IGNORECASE):
            return opt
    return None


NONE_SELECTED_RE = re.compile(r"^\s*none\s*\.?\s*$", re.IGNORECASE)


def parse_multi_decision(text: str, options: list[str]) -> list[str] | None:
    """Multi-select variant for `select` gates (e.g. select-comments): a
    comma-separated list of numbers/option-names, each resolved the same
    way `parse_decision` resolves a single one. ANY unparseable token
    refuses the whole decision (fail closed, no partial selection guessed).

    `NONE` is the explicit empty-selection sentinel (adversarial-review
    round 2 finding, independently found by both review lenses: without
    it, no input string could ever produce `[]`, even though the manifest
    and step docs document "any selection, including none" as forward-
    legal — a human with nothing to select had no valid way to say so)."""
    if NONE_SELECTED_RE.match(text):
        return []
    tokens = [t.strip() for t in text.split(",")]
    if not tokens or not all(tokens):
        return None
    resolved: list[str] = []
    for tok in tokens:
        m = NUMBER_RE.match(tok)
        if m:
            n = int(m.group(1))
            if not (1 <= n <= len(options)):
                return None
            resolved.append(options[n - 1])
            continue
        lowered = tok.lower()
        match = next((opt for opt in options if lowered == opt.lower()), None)
        if match is None:
            return None
        resolved.append(match)
    seen: set = set()
    return [r for r in resolved if not (r in seen or seen.add(r))]


def decide(state: dict, gate_id: str, human_records: list[dict],
           options: list[str], now: str, multi: bool = False,
           lenient: frozenset = frozenset(),
           deciding_session: str | None = None) -> dict:
    """`deciding_session` is the RAW platform session id of the process
    running `--decide`, supplied by the caller exactly as `now` is (this
    module stays a pure library: no `os`, no I/O, no env). It is digested
    here, because the stamps it is compared against are digests.

    Why filtering lives HERE and not in the capture hook. Capture is
    unconditional and lossless by design — the ledger is an append-only
    audit record — and at capture time "no run matched this prompt's
    session" and "a foreign session typed" are literally the same
    observation, so any capture-time rule either re-admits the bug (append
    on no-match) or destroys a resumed session's evidence (drop on
    no-match). At DECIDE time the ambiguity is gone: `--decide` is invoked
    BY the session currently driving this run, so a record tagged with the
    deciding session is by definition the human talking to this run right
    now — even when the gate was presented by an older session id.

    RESIDUAL — Qwen Code, and untagged precedence in a MIXED ledger. The
    whole rule is opt-in on the platform reporting a session id. Under Qwen
    Code nothing sets `CLAUDE_CODE_SESSION_ID` and the UserPromptSubmit
    payload may carry no `session_id`, so every gate is unstamped, the
    filter never ARMS (see `qualifying_records`) and it is entirely
    INERT: the original field bug (a foreign prompt parsed as the decision)
    reproduces there exactly. And in a ledger that MIXES vintages, "unknown
    means usable" outranks a known-good tag — an untagged record always
    qualifies, then newest-wins, so a newer untagged foreign prompt still
    buries a tagged own-session reply and the refusal then names the wrong
    cause ("does not parse as a decision"). Both are the deliberate price of
    fail-open backward compatibility: the alternative — untagged records
    losing to tagged ones — would silently discard the evidence of every
    pre-fix ledger and every session-less platform, which is the strictly
    worse failure. Stated, not coded around."""
    # `human_records` is read strict by the caller (a torn NEWEST reply must
    # not be silently dropped, promoting an older, more-permissive one —
    # adversarial-review finding, same class as the reviewer-verdict anchor).
    entry = state["gates"].get(gate_id) or {}
    presented_at = entry.get("presented_at")
    if not presented_at:
        raise GateRefusal(f"gate '{gate_id}' was never presented — nothing to decide")
    after = [r for r in human_records if r.get("at", "") > presented_at]
    # The session-membership rule (armed by this gate's stamp, widened by
    # the deciding session) — see `qualifying_records`, and note that the
    # `--present` waiting guard deliberately does NOT share it.
    qualifying = qualifying_records(after, entry, deciding_session)
    if not qualifying:
        if after:
            # Field, 2026-08-26: a `/dev-workflow` run sat mid-gate while a
            # SECOND session in the same workspace ran `/story-workflow`;
            # that prompt was captured into this run's ledger and, being the
            # newest record after the stamp, was parsed as the decision.
            # Ignoring it is the fix — but ignoring it SILENTLY would land
            # in the generic "nothing captured" refusal below, whose remedy
            # (cd back to the workspace root) is actively wrong here. Same
            # precedent as the stale-vs-nothing split just after: two
            # causes, two remedies, reported separately.
            raise GateRefusal(
                f"gate '{gate_id}': {len(after)} human repl(y/ies) captured "
                f"after this presentation, but every one of them was typed "
                f"in a DIFFERENT session than the one that presented this "
                f"gate — a prompt from another session in this workspace is "
                "not this gate's evidence, so it is ignored rather than "
                "parsed. FIRST MOVE, always: ask the human to reply again "
                "IN THE SESSION DRIVING THIS RUN, then `--decide` alone — "
                "no re-present. ONLY if the human CONFIRMS that session is "
                "gone (terminal closed, or it resumed under a new id) use "
                "`--re-present`, which re-stamps the gate to the CURRENT "
                "session: it also ages out anything typed in the old one "
                "while you re-stamp, so re-presenting on a guess is how the "
                "human ends up typing their answer twice."
            )
        # Two very different causes, previously reported identically — and the
        # advice that fits one is actively wrong for the other (field,
        # 2026-08-04): "re-present and have the human reply again" is right
        # when nothing was ever captured, and is what DESTROYS the evidence
        # when a reply exists but predates this presentation. Name which one
        # this is, from the ledger itself.
        stale = [r for r in human_records if r.get("at", "")]
        if stale:
            newest = max(stale, key=lambda r: r["at"])["at"]
            raise GateRefusal(
                f"gate '{gate_id}': {len(stale)} human repl(y/ies) captured, "
                f"but the newest ({newest}) PREDATES this presentation "
                f"({presented_at}) — a re-present after the human replied "
                "re-stamps the window and ages their reply out. Do NOT "
                "present again: ask the human to reply once more, then "
                "`--decide` alone. (Presenting and deciding in one shell "
                "invocation always lands here — no prompt can arrive between "
                "two commands of the same call.)"
            )
        raise GateRefusal(
            f"gate '{gate_id}': no human input captured after presentation — "
            "refusing to write a token. Present, WAIT for a plain typed chat "
            "reply in its own turn, then decide in a separate call. If the "
            "human DID reply, check capture: the UserPromptSubmit hook "
            "scopes to its cwd's workspace, so a session whose shell cwd "
            "drifted away from the workspace root drops evidence silently — "
            "cd back to the workspace root, re-present, and have the human "
            "reply again"
        )
    latest = max(qualifying, key=lambda r: r["at"])
    decision = (parse_multi_decision(latest.get("text", ""), options) if multi
                else parse_decision(latest.get("text", ""), options, lenient))
    if decision is None:
        raise GateRefusal(
            f"gate '{gate_id}': latest human input does not parse as a "
            f"{'selection' if multi else 'decision'} ({options}) — a FORWARD "
            "decision must be the bare option word or number (a qualified "
            "approval routes to ad-hoc handling, never a token); a "
            "rejection-side reply may LEAD with its option word and carry "
            "notes after it"
        )
    entry.update(
        decision=decision,
        decided_at=now,
        evidence=latest.get("hash")
        or hashlib.sha256(latest.get("text", "").encode()).hexdigest(),
    )
    state["gates"][gate_id] = entry
    return entry
