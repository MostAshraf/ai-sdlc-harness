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


def present(state: dict, gate_id: str, now: str,
            options: list[str] | None = None) -> None:
    entry = state["gates"].setdefault(gate_id, {})
    entry["presented_at"] = now  # re-presenting re-stamps (RC4 selection spec)
    entry.pop("decision", None)
    if options is not None:
        # Sealed into state at presentation time so decide() replays THIS
        # list — the numbering the human replied to can never be redefined
        # between present and decide (adversarial-review finding).
        entry["options"] = options


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
           lenient: frozenset = frozenset()) -> dict:
    # `human_records` is read strict by the caller (a torn NEWEST reply must
    # not be silently dropped, promoting an older, more-permissive one —
    # adversarial-review finding, same class as the reviewer-verdict anchor).
    entry = state["gates"].get(gate_id) or {}
    presented_at = entry.get("presented_at")
    if not presented_at:
        raise GateRefusal(f"gate '{gate_id}' was never presented — nothing to decide")
    qualifying = [r for r in human_records if r.get("at", "") > presented_at]
    if not qualifying:
        raise GateRefusal(
            f"gate '{gate_id}': no human input after presentation — refusing to "
            "write a token (re-present after any interleaved interaction). If "
            "the human DID reply, check capture: the UserPromptSubmit hook "
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
