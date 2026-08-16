"""Transition legality — BOTH FSMs, validated from declared data only.

Cursor moves are validated against pipeline/manifest.yaml (mode sequence,
on_reject / returns_to edges, group entry + internal order, escalations,
conditional-gate skip, gate-precedence). Task moves are validated against
pipeline/task-fsm.yaml (+ named guards: red-proof requirement, review-round
bound). Nothing here hardcodes a transition — this module *interprets* the
declarations (design.md pieces 1-2).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import chain, ndjson

FORWARD_DEFAULT = ("approved",)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TERMINAL_CACHE: tuple[str, ...] | None = None


def terminal_statuses() -> tuple[str, ...]:
    """The declared terminal task statuses — `terminal:` in
    pipeline/task-fsm.yaml, never a literal.

    Seven references across six functions asked "is this task finished?" and
    each answered with its own `("done", "archived")` (adversarial-review
    finding): `dispatch_picture` twice (the partition and the unmet-dependency
    scan), the develop sync point's legality half and its refusal half, the
    `dependencies-done` guard, `complete`'s refusal, and `env-check`'s
    scoping. The FSM is data everywhere else in this engine; this was the one
    piece of its vocabulary that lived in code, in seven copies — so adding a
    terminal status meant finding all seven, and missing one would make a
    task finished to the dispatcher and still live to the sync point.

    Cached per process (the file is shipped data, immutable for a run's
    lifetime), which is what keeps it from being a hidden file read inside a
    lock. It used to take an optional already-loaded `fsm` to bypass that
    cache; every call site — the six in this engine plus the schema test —
    passed none (grepped), so the parameter was only ever a second, untested
    way to answer the question this function owns. Removed rather than
    documented."""
    global _TERMINAL_CACHE
    if _TERMINAL_CACHE is None:
        from .schema import load_yaml
        loaded = load_yaml(PLUGIN_ROOT / "pipeline" / "task-fsm.yaml") or {}
        _TERMINAL_CACHE = tuple(loaded.get("terminal") or ())
    return _TERMINAL_CACHE


class TransitionError(Exception):
    pass


def ensure_live(state: dict, verb: str) -> None:
    """Every mutating entry point refuses on a terminal run — terminal by
    declaration either way (`harness abort` / `harness complete`), so
    continuing to walk or mutate one would resurrect a run whose work-item
    slot has already been released to a fresh bootstrap."""
    if state.get("aborted"):
        raise TransitionError(
            f"run was aborted at {state['aborted'].get('at')} "
            f"({state['aborted'].get('reason')!r}) — '{verb}' is illegal on "
            "an aborted run; bootstrap a fresh run for this work item")
    if state.get("completed"):
        raise TransitionError(
            f"run completed at {state['completed'].get('at')} — '{verb}' is "
            "illegal on a completed run; bootstrap a fresh run for new work")


# ------------------------------------------------------------- predicates

def _config_get(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        node = node[part]
    return node


def eval_predicate(pred: dict, artifacts: dict, config: dict) -> bool:
    value = artifacts.get(pred.get("value"))
    if value is None:
        # Naming the remedy here, not only in the step file (adversarial-review
        # B/1): the overwhelmingly likely cause is a run bootstrapped BEFORE
        # the producing step declared this artifact — an upgrade landing
        # mid-run. Such a run has no legal move at all (every sequence walk
        # hits this raise), and the message it wedges on is the only thing the
        # human sees; the step doc that explains it is one they have no reason
        # to re-open.
        raise TransitionError(
            f"predicate needs artifact '{pred.get('value')}' which was never "
            "recorded — if this run was started before an upgrade that added "
            "the artifact, its producing step never ran and no move is legal: "
            "`harness abort --reason 'pre-upgrade run'` then re-fetch (a "
            "same-day re-fetch gets its own run slot)"
        )
    if "equals" in pred:
        return value == pred["equals"]
    if "at_least" in pred:
        threshold = _config_get(config, pred["at_least"]["config"])
        order = config["security"]["severity_order"]
        if value not in order or threshold not in order:
            raise TransitionError(f"severity '{value}'/'{threshold}' not in severity_order")
        return order.index(value) >= order.index(threshold)
    raise TransitionError(f"unknown predicate shape: {pred}")


# ---------------------------------------------------------------- cursor

def _gate_forward(step_def: dict, decision: str | None) -> bool | None:
    """True: forward legal. False: on_reject legal. None: no decision yet."""
    if decision is None:
        return None
    return decision in step_def.get("forward_on", FORWARD_DEFAULT)


def _verdict_bound_filter(cur_def: dict, state: dict, config: dict,
                          run: Path | None,
                          candidates: dict[str, str]) -> dict[str, str]:
    """A `verdict_bound` step's exits are DERIVED from the hook-captured
    reviewer-verdict ledger (reviews.ndjson) — the same trust anchor the
    task FSM's `reviewer-approved` guard uses; an orchestrator claim never
    substitutes. Window: records strictly after the latest human gate
    decision (`decided_at`), so a gate rejection starts a fresh round
    budget for the new cycle instead of inheriting the old one.

      no in-window verdict           -> {}   (fail-closed, like an
                                              undecided gate)
      CHANGES_REQUESTED under bound  -> only the returns_to edge (the
                                              revision loop is forced)
      APPROVED, or bound exhausted   -> only forward (bound exhaustion
                                              reaches the human WITH the
                                              failing report — plan drift
                                              is the human's call, never a
                                              deadlock)
    """
    vb = cur_def["verdict_bound"]
    if run is None:
        return {}  # no ledger access -> fail closed (CLI always passes run)
    latest_verdict, rounds, _at = _verdict_window(state, run, vb["mode"])
    if latest_verdict is None:
        return {}
    bound = _config_get(config, vb["bound"]["config"])
    ret = cur_def.get("returns_to")
    if latest_verdict == "APPROVED" or rounds >= bound:
        return {k: v for k, v in candidates.items() if k != ret}
    return {ret: "returns_to"} if ret in candidates else {}


def _verdict_window(state: dict, run: Path,
                    mode: str) -> tuple[str | None, int, str]:
    """(latest in-window verdict, CHANGES_REQUESTED count, its timestamp) for
    `mode` — THE one computation the exit filter, the outcome-artifact
    recording AND the stall guard read, so the gate a downstream `when`
    predicate consults can never disagree with the exits that were actually
    derived. Window: records strictly after the latest human gate decision.
    Timestamp ties fail closed: two hook processes can stamp identical `at`
    (per-process monotonic clamp, coarse OS clocks), and a plain max() is
    first-wins — which would let an APPROVED beat a same-instant
    CHANGES_REQUESTED.

    The third element is the winning record's `at` (empty string when there
    is no in-window verdict). field: dual-run comparison — the stall
    guard needs to know not just THAT a verdict exists but WHEN, to tell a
    verdict this round already produced from one an earlier round did; it
    reads it from here rather than growing a second reader of the same
    ledger."""
    anchor = max((g.get("decided_at", "") or ""
                  for g in (state.get("gates") or {}).values()), default="")
    try:
        records = ndjson.read_records(run / "reviews.ndjson", strict=True)
    except ndjson.LedgerCorruption as exc:
        raise TransitionError(
            "verdict_bound: reviewer-verdict ledger has a corrupt record — "
            f"refusing to derive from it (fail closed): {exc}") from exc
    qualifying = [r for r in records
                  if r.get("mode") == mode and r.get("at", "") > anchor]
    if not qualifying:
        return None, 0, ""
    latest_at = max(r.get("at", "") for r in qualifying)
    at_tie = [r for r in qualifying if r.get("at", "") == latest_at]
    if any(r.get("verdict") == "CHANGES_REQUESTED" for r in at_tie):
        latest_verdict = "CHANGES_REQUESTED"
    else:
        latest_verdict = at_tie[-1].get("verdict")
    rounds = sum(1 for r in qualifying
                 if r.get("verdict") == "CHANGES_REQUESTED")
    return latest_verdict, rounds, latest_at


def cursor_candidates(state: dict, manifest: dict, config: dict,
                      run: Path | None = None) -> dict[str, str]:
    """Legal next steps from the current cursor: {step_id: reason}."""
    steps, mode = manifest["steps"], state["mode"]
    seq = manifest["modes"][mode]
    current = state["cursor"]["current_step"]
    completed = set(state["cursor"]["completed_steps"])
    artifacts = state.get("artifacts", {})
    candidates: dict[str, str] = {}
    cur_def = steps.get(current, {})

    vb_cur = cur_def.get("verdict_bound") or {}
    if vb_cur.get("outcome_artifact") and run is not None:
        # Refresh the ledger-derived outcome BEFORE the sequence walk: a
        # follower's `when` predicate (lean's exception gate) must see the
        # CURRENT panel state, not the entry stamp — pending while
        # undecided or mid-revision, approved/exhausted once the ledger
        # says so. The caller persists state after advancing, so the value
        # the gate later reads is exactly the one that legalized the move.
        latest_verdict, rounds, _at = _verdict_window(state, run,
                                                      vb_cur["mode"])
        # Exhaustion LATCHES within a window (adversarial-review, lean
        # round: once the bound is hit, returns_to is closed — no
        # legitimate revision can exist in this window — so a later
        # same-window APPROVED is a stall re-spawn or manipulation, and
        # letting it flip the outcome would self-skip lean's exception
        # gate past a plan the panel rejected `bound` times). Only a gate
        # decision re-anchors the window and clears the latch.
        if (latest_verdict is not None
                and rounds >= _config_get(config, vb_cur["bound"]["config"])):
            outcome = "exhausted"
        elif latest_verdict == "APPROVED":
            outcome = "approved"
        else:
            outcome = "pending"
        set_artifact(state, manifest, vb_cur["outcome_artifact"], outcome)

    # requires_tasks_registered gates EVERY exit, not just the sequence edge
    # (adversarial-review finding: the check below only suppressed the
    # sequence candidate, so a future manifest giving this step a
    # returns_to/group/escalation edge would leak past a provisional task
    # list — not reachable on today's manifest, where `plan` has only the
    # sequence edge, but hardened here so it stays true regardless).
    if cur_def.get("requires_tasks_registered") and any(
            t.get("provisional") for t in state.get("tasks", [])):
        return {}  # nothing legal until plan-register replaces the seed

    # Same shape, same reason, different fact: quick mode has no
    # plan-register, so `requires_tasks_registered` can't be what holds its
    # seed repo shut. This gates EVERY exit for the same hardening reason as
    # above — a future returns_to/group/escalation edge into or out of the
    # confirming step must not leak past an unratified repo.
    if (cur_def.get("requires_repo_confirmed")
            and not state.get("repo_confirmed")):
        return {}  # nothing legal until confirm-repo ratifies the seed's repo

    # gate-precedence: advancing PAST a gate needs a recorded forward decision.
    # A `select` gate (e.g. select-comments) picks a subset of items rather
    # than approving/rejecting a single proposal — any parsed selection
    # (including an empty one) is forward-legal, so it skips the
    # forward_on/on_reject binary entirely and falls through to the normal
    # sequence/group logic below once a decision is recorded.
    if cur_def.get("gate") and cur_def.get("select"):
        if (state["gates"].get(current) or {}).get("decision") is None:
            return {}  # nothing legal until a selection is recorded
    elif cur_def.get("gate"):
        decision = (state["gates"].get(current) or {}).get("decision")
        forward = _gate_forward(cur_def, decision)
        if forward is None:
            return {}  # nothing legal until the gate is decided
        if forward is False:
            target = cur_def.get("on_reject")
            return {target: "on_reject"} if target else {}

    # next in sequence (with conditional-gate skip + fail-closed sync point)
    seq_key = None
    tasks_ready = not cur_def.get("requires_tasks_terminal") or all(
        t.get("status") in terminal_statuses() for t in state.get("tasks", [])
    )
    # (requires_tasks_registered is handled by the early return above — it
    # gates every exit edge, not just this sequence one.)
    if current in seq and tasks_ready:
        idx = seq.index(current)
        j = idx + 1
        while j < len(seq):
            nxt = steps[seq[j]]
            if nxt.get("when") is not None and not eval_predicate(
                nxt["when"], artifacts, config
            ):
                j += 1  # predicate false -> gate skipped, keep walking
                continue
            seq_key = seq[j]
            candidates[seq_key] = "sequence"
            break

    # side-step return edge
    if cur_def.get("returns_to"):
        candidates[cur_def["returns_to"]] = "returns_to"

    # group entry / internal order / repeatable re-entry
    for gid, group in (manifest.get("groups") or {}).items():
        gsteps = group["steps"]
        if current in gsteps:
            i = gsteps.index(current)
            if i + 1 < len(gsteps):
                candidates[gsteps[i + 1]] = f"group:{gid}"
            elif group.get("repeatable"):
                candidates[gsteps[0]] = f"group:{gid}:reenter"
        elif group["available_after"] in completed:
            candidates[gsteps[0]] = f"group:{gid}:enter"

    # declared cross-mode escalations — MANDATORY when triggered: a true
    # predicate removes the forward edge (advisory escalation would be the
    # prose-following hole this design closes); an undetermined predicate
    # (artifact not yet recorded) fail-closes forward until the step records
    # its verdict.
    for esc in manifest.get("escalations") or []:
        if esc["from"]["mode"] == mode and esc["from"]["step"] == current:
            try:
                triggered = eval_predicate(esc["when"], artifacts, config)
            except TransitionError:
                triggered = None  # undetermined
            if triggered:
                candidates[esc["to"]["step"]] = f"escalate:{esc['to']['mode']}"
            if triggered is not False and seq_key:
                candidates.pop(seq_key, None)

    # verdict_bound owns ALL exits of its step — applied last so it filters
    # the full computed set (sequence + returns_to), never a partial one.
    if cur_def.get("verdict_bound"):
        candidates = _verdict_bound_filter(cur_def, state, config, run,
                                           candidates)

    return candidates


def advance_cursor(state: dict, manifest: dict, config: dict, target: str,
                   now: str, run: Path | None = None) -> list[dict]:
    """Returns the conditional steps SKIPPED by this move (empty for most
    moves) so the caller can ledger them. Field (e2e E2E-1): approve-
    security self-skipped on a below-threshold severity exactly as
    declared, but nothing recorded that the evaluation ever happened — the
    ledger couldn't distinguish 'gate skipped by predicate' from 'gate
    never considered', and the run report simply didn't know."""
    ensure_live(state, f"cursor --to {target}")
    candidates = cursor_candidates(state, manifest, config, run=run)
    if target not in candidates:
        current = state["cursor"]["current_step"]
        cur_def = manifest["steps"].get(current, {})
        if (cur_def.get("requires_tasks_registered")
                and any(t.get("provisional") for t in state.get("tasks", []))):
            raise TransitionError(
                f"cursor move '{current}' -> '{target}' is blocked: the task "
                "list is still the fetch-seeded provisional placeholder — "
                "run `harness plan-register` with the approved plan's tasks "
                "first")
        if (cur_def.get("requires_repo_confirmed")
                and not state.get("repo_confirmed")):
            seeded = ", ".join(sorted({t.get("repo", ".")
                                       for t in state.get("tasks", [])}))
            raise TransitionError(
                f"cursor move '{current}' -> '{target}' is blocked: this "
                f"run's target repo is still fetch's positional default "
                f"({seeded}) — repos.yaml lists more than one repo and "
                "nothing has ratified which one this work item belongs to. "
                "Propose one from the repo-map evidence, confirm it with the "
                "user, then run `harness confirm-repo --repo <registered "
                "path>`")
        if cur_def.get("requires_tasks_terminal"):
            terminal = terminal_statuses()
            waiting = [(t.get("id"), t.get("status"))
                       for t in state.get("tasks", [])
                       if t.get("status") not in terminal]
            # DECLARED LEGALITY FIRST (adversarial review, round 4): this
            # refusal claims the sync point is what is holding the move —
            # true only for a target the manifest would otherwise allow. A
            # typo'd or unreachable target got it too, and was told to go
            # finish its tasks for a move that would stay refused after they
            # were all done. Probed by asking what the exits WOULD be with
            # every task terminal; anything outside that set falls through to
            # the generic "not declared legal" message, which names the real
            # legal targets.
            if waiting and terminal:
                probe = {**state,
                         "tasks": [{**t, "status": terminal[0]}
                                   for t in state.get("tasks", [])]}
                would_allow = target in cursor_candidates(
                    probe, manifest, config, run=run)
            else:
                would_allow = False
            if waiting and would_allow:
                # Its own refusal, unlike before — this sync point used to
                # fall through to the generic "not declared legal" message,
                # which names the LEGAL targets (none) and says nothing about
                # why. Its two siblings above have always named their cause;
                # this one is the sole reason develop has no exits far more
                # often than they are, and with DAG-pipelined dispatch the
                # answer the orchestrator needs is specifically WHICH task is
                # still moving — "legal: none (gate undecided?)" sent it
                # looking for an undecided gate on a step that has no gate.
                raise TransitionError(
                    f"cursor move '{current}' -> '{target}' is blocked: "
                    f"'{current}' cannot be left until every task is terminal "
                    f"({'/'.join(terminal)}), and these are not — "
                    + ", ".join(f"{tid} is {status}" for tid, status in waiting)
                    + ". The declared sync point, not a gate: finish or "
                    "archive those tasks (`harness ready-tasks` shows where "
                    "each one is); it is never forced.")
        if cur_def.get("verdict_bound") and not candidates:
            raise TransitionError(
                f"cursor move '{current}' -> '{target}' is blocked by "
                "verdict_bound: no in-window reviewer verdict — spawn the "
                f"reviewer (mode: {cur_def['verdict_bound']['mode']}); its "
                "hook-captured verdict derives this step's exits (no "
                "verdict, no exit)")
        raise TransitionError(
            f"cursor move '{current}' -> '{target}' is not declared legal; "
            f"legal: {sorted(candidates) or 'none (gate undecided?)'}"
        )
    reason = candidates[target]
    current = state["cursor"]["current_step"]
    cur_def = manifest["steps"].get(current, {})
    if cur_def.get("gate"):
        entry = state["gates"].get(current)
        if entry and "decision" in entry:
            # Single-use: a decision is consumed by the edge it legalizes
            # (adversarial-review, plan-accuracy round: a stale rejection
            # left in state re-opened its on_reject edge on every later
            # arrival — after ONE human rejection, a humanless
            # plan↔plan-review↔approve-plan cycle was engine-legal and the
            # verdict window's round budget never reset). `decided_at` and
            # `evidence` stay — the window anchor and the audit trail
            # outlive consumption; re-arrival fail-closes until a fresh
            # present + decide. `presented_at` is consumed WITH the
            # decision (re-verification finding: left in place, the
            # capture hook's presented-and-undecided window test stayed
            # open forever — every later prompt captured — and a decide
            # at a re-arrived gate could qualify stray mid-cycle replies
            # against the stale round-1 presentation).
            entry["consumed_decision"] = entry.pop("decision")
            entry.pop("presented_at", None)
    if (reason in ("returns_to", "on_reject")
            and manifest["steps"].get(target, {}).get("requires_tasks_registered")):
        # Re-entering a registration-owning step re-arms its exit condition:
        # the revised plan must plan-register again (idempotent when the
        # decomposition is unchanged). Otherwise a revision round could sail
        # to the gate and into develop on the previous round's task list —
        # the exact class requires_tasks_registered exists to close,
        # reopened by any loop edge for every round after the first.
        for t in state.get("tasks", []):
            t["provisional"] = True
    if (reason in ("returns_to", "on_reject")
            and manifest["steps"].get(target, {}).get("requires_repo_confirmed")):
        # Same re-arming, same reason, for the repo marker (adversarial-review
        # A/F4): cursor_candidates hardens `requires_repo_confirmed` against
        # every exit edge INCLUDING future loop edges, but a marker that never
        # clears would satisfy the re-entered step on the PREVIOUS round's
        # confirmation and release it immediately. Unreachable on today's
        # manifest — quick has no loop edge into confirm-repo — so this keeps
        # the module's own stated bar rather than fixing a live bug.
        state.pop("repo_confirmed", None)
    skipped: list[dict] = []
    if reason == "sequence":
        # a farther-than-adjacent sequence target is only ever legal when
        # cursor_candidates walked over false-predicate steps — name them
        seq = manifest["modes"][state["mode"]]
        for s in seq[seq.index(current) + 1:seq.index(target)]:
            pred = manifest["steps"][s].get("when") or {}
            value = state.get("artifacts", {}).get(pred.get("value"))
            skipped.append({"step": s,
                            "reason": f"declared `when` predicate false: "
                                      f"{pred.get('value')} = {value!r}"})
    if reason.startswith("escalate:"):
        state["mode"] = reason.split(":", 1)[1]
    state["cursor"]["completed_steps"].append(current)
    state["cursor"]["current_step"] = target
    tvb = manifest["steps"].get(target, {}).get("verdict_bound") or {}
    if tvb.get("outcome_artifact"):
        # ENTERING a verdict_bound step (first entry or a revision
        # re-entry): the outcome is undecided — stamp `pending` so a
        # downstream `when` predicate can always evaluate (a missing
        # predicate artifact is a hard TransitionError by design, and the
        # candidates walk evaluates the follower's `when` while the cursor
        # still sits here). The forward exit overwrites it with the real
        # ledger-derived value.
        set_artifact(state, manifest, tvb["outcome_artifact"], "pending")
    state["metrics"].setdefault(current, {})["ended_at"] = now
    state["metrics"].setdefault(target, {})["started_at"] = now
    return skipped


def set_artifact(state: dict, manifest: dict, name: str, value) -> None:
    current = state["cursor"]["current_step"]
    produces = manifest["steps"][current].get("produces", []) or []
    if name not in produces:
        raise TransitionError(
            f"step '{current}' does not declare producing '{name}' — refusing"
        )
    state.setdefault("artifacts", {})[name] = value


# ----------------------------------------------------------------- tasks

def redproof_path(run: Path, task_id: str) -> Path:
    return run / ".redproof" / f"{task_id}.json"


def redproof_label(task_id: str) -> str:
    """The chain-seal identity label for a task's red-proof. Binding the
    task id into the seal digest (adversarial-review finding) means a
    proof file copied to ANOTHER task's proof path fails verification
    outright — the seal proves "T1's proof", not just "an authentic
    proof". The guard below re-asserts `proof["task"]` as belt-and-braces
    for the same replay."""
    return f"redproof:{task_id}"


def _guard_red_proof(state: dict, task: dict, run: Path, key: bytes,
                     verify_ctx: dict | None) -> None:
    """Data-driven activation (design.md piece 5A): active exactly for tasks
    that declare test_intents — the same condition the hook-side pre-red
    write lock keys on, so the two halves of the TDD invariant can never
    disagree. Quick-mode runs are exempt because their fetch-seeded task
    declares no intents, NOT via a mode-name check (composability round,
    2026-07-08: the old literal `mode == "full" and step == "develop"`
    activation meant a new manifest mode containing develop got the write
    lock but silently lost this completion check — a half-enforced
    invariant no data change could repair).
    With a verify_ctx ({repo, test_cmd}) the full checkpoint runs:
    blob-SHA integrity + green test run. Without one (unit-level callers),
    only proof existence + seal are checked — the CLI always builds a ctx."""
    if not task.get("test_intents"):
        # `test_intents: []` is THE TDD opt-out (0.15.8): the plan declared
        # no tests for this task (docs/chore) and the human approved that
        # shape at the plan gate; registration is plan-step-only and
        # chain-sealed, so no downstream shape can grant itself this
        # exemption. 0.15.8 wired the opt-out into the pre-red WRITE lock
        # only — this completion guard still demanded a proof verify-red
        # can never produce (a docs-only change never turns the suite
        # red), deadlocking the task and,
        # since develop requires every task terminal, the whole run. The
        # review requirement (reviewer-approved, in-review -> done) is NOT
        # exempted — docs still get reviewed.
        return
    path = redproof_path(run, task["id"])
    if not path.exists():
        raise TransitionError(
            f"task {task['id']}: no red-proof — run `harness verify-red` before "
            "completing a develop task (no proof, no completion)"
        )
    # IntegrityError on tamper OR on a proof copied from another task's
    # path (the label binds the seal to THIS task's identity).
    proof = json.loads(chain.verify(path, key, label=redproof_label(task["id"])))
    if proof.get("task") != task["id"]:
        raise TransitionError(
            f"task {task['id']}: red-proof declares task "
            f"'{proof.get('task')}' — a proof is never transferable between "
            "tasks (no proof, no completion)")
    if verify_ctx:
        from . import gitops
        gitops.verify_green(proof, verify_ctx["repo"], verify_ctx.get("test_cmd"),
                            run_tests=verify_ctx.get("run_tests", True))


def _guard_reviewer_approved(state: dict, task: dict, run: Path) -> None:
    """in-review -> done needs a hook-captured reviewer verdict
    (adversarial-review finding: this transition had no guard at all, so
    "tight reviewers everywhere" was enforced nowhere — an orchestrator
    could complete a task right after verify-green with no reviewer ever
    spawned, review_rounds 0, nothing flagged).

    The verdict ledger `reviews.ndjson` is written ONLY by the
    PostToolUse(Agent) capture hook when a reviewer-shape subagent replies (its
    `verdict: APPROVED|CHANGES_REQUESTED` status-block line, its task from
    the spawn prompt's `harness-task:` header) — the bash/write guards block
    direct writes (including programmatic `open(...,"a")`-style ones) and no
    CLI verb appends to it, so the record is evidence a reviewer actually
    ran, the same trust anchoring gates get from human-input.ndjson. This
    ledger is NOT chain-sealed (only state.yaml and red-proofs are), so the
    string guard is its sole protection — a corrupt line is treated as
    fail-closed (strict read below), not silently skipped in a way that
    could promote an older, more-permissive record. The record must
    postdate the task's LAST entry into in-review: an approval from a
    previous round must not carry over a rework whose re-review never
    happened."""
    if "in_review_at" not in task:
        # No stamp = no window to anchor the verdict to (a run persisted
        # mid-in-review before the stamp existed, or a hand-edited state):
        # fail closed rather than accept any historical approval
        # (adversarial-review finding). Re-entering in-review stamps it.
        raise TransitionError(
            f"task {task['id']}: no in-review timestamp — cannot anchor the "
            "reviewer verdict window; re-enter in-review (task --to "
            "in-progress then --to in-review) to stamp it, then re-review")
    try:
        records = ndjson.read_records(run / "reviews.ndjson", strict=True)
    except ndjson.LedgerCorruption as exc:
        raise TransitionError(
            f"task {task['id']}: reviewer-verdict ledger has a corrupt "
            f"record — refusing to complete (fail closed): {exc}") from exc
    entered = task["in_review_at"]
    qualifying = [r for r in records
                  if r.get("task") == task["id"] and r.get("mode") == "review"
                  and r.get("at", "") > entered]
    if not qualifying:
        raise TransitionError(
            f"task {task['id']}: no reviewer verdict captured since it "
            "entered in-review — spawn the reviewer (mode: review); its "
            "hook-captured verdict is the completion evidence (no review, "
            "no done)")
    # Same tie rule as _verdict_bound_filter: identical `at` stamps from
    # two hook processes must not let a first-read APPROVED shadow a
    # same-instant rejection — any non-APPROVED at the latest instant wins.
    latest_at = max(r.get("at", "") for r in qualifying)
    at_tie = [r for r in qualifying if r.get("at", "") == latest_at]
    latest = next((r for r in at_tie if r.get("verdict") != "APPROVED"),
                  at_tie[-1])
    if latest.get("verdict") != "APPROVED":
        raise TransitionError(
            f"task {task['id']}: latest reviewer verdict is "
            f"{latest.get('verdict')!r}, not APPROVED — rework via "
            "`task --to in-progress` (round-bounded), then re-review")


def depends_on(task: dict) -> list[str]:
    """A task's `depends_on` as a list of id STRINGS, whatever the stored
    value is. Defensive on purpose: `plan_register` refuses every non-list
    shape and every non-string entry at registration, so a bad one here came
    from a hand edit or a pre-validation state.

    ONE definition, and that IS the finding it closes (re-verification,
    executed): its two readers — `workflow.dispatch_picture`, which shows the
    orchestrator what is ready, and `_guard_dependencies_done` below, which
    decides whether `task --to in-progress` is legal — must answer "what does
    this task depend on?" identically. The guard used to iterate the raw
    value, so a legacy `depends_on: "T1"` string made it refuse per CHARACTER
    ('T' and '1', neither of which any task provides) while `ready-tasks`,
    which coerced, reported the task READY: the exact reader-disagreement
    `dispatch_picture` exists to prevent, arriving inside it. It lives HERE,
    the lower layer, because `harness.workflow` imports the engine and never
    the other way round.

    Non-string ENTRIES are dropped rather than kept, which is what makes the
    coercion total: a nested list is unhashable and a stray int is
    unorderable against ids, so either one takes `ready-tasks`' set-and-sort
    down with a TypeError instead of naming the wedge — and the read-only
    surfaces promise to SHOW a broken plan, not to crash on it."""
    raw = task.get("depends_on")
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, str)]


def _guard_dependencies_done(state: dict, task: dict) -> None:
    """pending -> in-progress requires every depends_on task done/archived —
    the declared task DAG, enforced (it used to be stored and read by
    nothing). plan_register already refused dangling ids and cycles, so
    blocked here always means "not yet", never "never"."""
    by_id = {t["id"]: t for t in state["tasks"]}
    terminal = terminal_statuses()
    waiting = sorted({d for d in depends_on(task)
                      if by_id.get(d, {}).get("status") not in terminal})
    if waiting:
        raise TransitionError(
            f"task {task['id']}: depends_on {', '.join(waiting)} "
            "not yet done — the declared task order is enforced, not advisory")


def _guard_round_bound(task: dict, config: dict) -> None:
    max_rounds = config["review_rounds"]["max"]
    if task["review_rounds"] >= max_rounds:
        raise TransitionError(
            f"task {task['id']}: review round {task['review_rounds'] + 1} exceeds "
            f"bound {max_rounds} — round {max_rounds + 1}+ signals plan drift, not "
            "code drift; escalate to the human"
        )


def transition_task(state: dict, fsm: dict, config: dict, run: Path, key: bytes,
                    task_id: str, to: str, context: str | None = None,
                    verify_ctx: dict | None = None) -> dict:
    ensure_live(state, f"task {task_id} --to {to}")
    task = next((t for t in state["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise TransitionError(f"unknown task '{task_id}'")
    frm = task["status"]
    decl = next((t for t in fsm["transitions"]
                 if t["from"] == frm and t["to"] == to), None)
    if decl is None:
        raise TransitionError(
            f"task {task_id}: '{frm}' -> '{to}' is not a declared transition"
        )
    if decl.get("only_when") and decl["only_when"] != context:
        raise TransitionError(
            f"task {task_id}: '{frm}' -> '{to}' is legal only when '{decl['only_when']}'"
        )
    guard = decl.get("guard")
    if guard == "verify-green-with-red-proof":
        _guard_red_proof(state, task, run, key, verify_ctx)
    elif guard == "review-round-bound":
        _guard_round_bound(task, config)
    elif guard == "reviewer-approved":
        _guard_reviewer_approved(state, task, run)
    elif guard == "dependencies-done":
        _guard_dependencies_done(state, task)
    elif guard is not None:
        raise TransitionError(f"task {task_id}: unknown guard '{guard}' in FSM")
    if decl.get("counter"):
        task[decl["counter"]] = task.get(decl["counter"], 0) + 1
    task["status"] = to
    if to == "in-review":
        # The reviewer-approved guard anchors its verdict window to this
        # stamp: only a verdict captured AFTER the task's latest entry into
        # in-review counts (a round-1 approval must not complete a round-2
        # rework whose re-review never happened).
        task["in_review_at"] = ndjson.now_iso()
    if to == "in-progress":
        # Full mode clears this at plan-register (tasks are rebuilt
        # wholesale there — including after a plan re-entry re-arms the
        # flag — well before any task reaches in-progress), so this is a
        # no-op there. Quick mode has no plan-register at all (adversarial-
        # review finding), so the fetch-seeded task's `provisional: true`
        # would otherwise never clear: the first in-progress transition is
        # the first point a human/orchestrator has actually acted on it.
        task.pop("provisional", None)
    return task


def _stall_round_anchor(state: dict, run: Path, stall_key: str,
                        round_marker: str | None) -> str:
    """The timestamp a captured verdict must POST-DATE before it counts as
    "this round already has its verdict" (field: dual-run comparison).

    A bare "any in-window verdict exists" test would be wrong: round 1's
    CHANGES_REQUESTED stays inside `_verdict_window`'s window (only a human
    gate decision re-anchors that) while round 2's reviewer genuinely
    stalls, so the guard would refuse every subsequent stall forever. The
    anchor is therefore the newest of three round-opening marks:

      (a) the latest human gate decision — `_verdict_window`'s own window
          floor, included so the two never disagree about the window;
      (b) the latest `stall` event for THIS stall key — a recorded stall
          means the orchestrator already re-spawned, so anything captured
          before it belongs to the previous attempt;
      (c) the latest `verdict_bound.round_marker` event, when the step
          declares one — for plan-review that is `plan-registered`: a new
          plan revision opens a new review round, so the previous round's
          verdict must stop suppressing stalls.

    Read leniently (strict=False, unlike the verdict ledger): a torn line
    here can only LOWER the anchor, and a lower anchor makes the guard
    refuse a stall it might have counted — recoverable via
    --confirm-no-verdict. Raising on corruption would instead brick the
    stalled-agent procedure, which is the one path a wedged run depends on.
    """
    marks = [max((g.get("decided_at", "") or ""
                  for g in (state.get("gates") or {}).values()), default="")]
    events = ndjson.read_records(run / "events.ndjson")
    marks += [e.get("at", "") for e in events
              if e.get("kind") == "stall" and e.get("task") == stall_key]
    if round_marker:
        # Deliberately NOT actor-checked, unlike outstanding_flagged's
        # plan-registered consumers: the marker kind is generic declared
        # data, and a forged marker here only WIDENS the anchor — which
        # ALLOWS a stall, burning budget toward the human. That is the
        # design's safe direction, the same reason timestamp ties resolve
        # the same way (pre-release review adjudication).
        marks += [e.get("at", "") for e in events
                  if e.get("kind") == round_marker]
    return max(marks, default="")


def stall_key_spawn_modes(manifest: dict, stall_key: str) -> set[str]:
    """The modes a TASK-LESS `spawn-pending` may carry to belong to this
    stall key — DECLARED data, read off the manifest, never inferred.

    A stall key is either a task id or `step:<step>[:<lens>]`. A task-less
    spawn records no `harness-task` header, so its pending carries task=None
    and cannot be matched by equality; what it CAN be matched by is the step
    that is allowed to spawn it — `steps.<step>.spawns` is exactly the set of
    modes a run at that step launches without a task.

    Two bounds, both executed in adversarial review before they were closed:

    * a PER-TASK key matches no task-less pending at all (the early return).
      This is what keeps the parse total, too: a task id has no step
      component to unpack.
    * a LENS key (`step:plan-review:contradictions`) additionally EXCLUDES
      the step's `verdict_bound` mode. The lens counters exist because a
      panel member stalls independently of the synthesizer, and the
      synthesizer's verdict is the one the FSM reads — so a per-lens
      override that retired the live synthesizer would abandon the whole
      round's real work to recover one advisory lens.

    Consequence, stated: a task-less pending belongs to ITS OWN step's key
    and to no other, so a pending left dangling by a step the run has since
    LEFT no longer refuses (or is swept by) a later step's stall — it clears
    through its own `step:<that step>` key, which is the key the orchestrator
    reads off the pending's own mode. The unclearable set is ANY task-less
    pending whose mode no step declares in its spawns — not only the
    mode-less case (PreToolUse bypass) but also, today, `repo-map` and
    `request-triage`: both are declared OUTSIDE the step spawn-sets
    (out_of_run_spawns / always_legal_spawns), so a pending of theirs that
    lands in a run via the single-run fallback matches no `step:` key
    (adversarial re-verification, executed without any bypass). All of
    these refuse nothing, so they cannot wedge a run, and they self-clear
    on a normal SubagentStop; a stop that never arrives leaves them on the
    flagged gauge until the run ends. Accepted, and cheaper than the
    alternative, which is the cross-step sweep this replaced."""
    if not stall_key.startswith("step:"):
        return set()
    _, step, *lens = stall_key.split(":")
    step_def = (manifest.get("steps") or {}).get(step) or {}
    modes = {s.get("mode") for s in (step_def.get("spawns") or [])}
    if lens:
        modes.discard((step_def.get("verdict_bound") or {}).get("mode"))
    return {m for m in modes if m}


def _read_pairing_events(run: Path) -> list[dict]:
    """events.ndjson for the pending-pairing readers — lenient, and LOUD
    about what it skipped (adversarial review): these readers decide "no
    spawn is in flight" from absence, so a torn `spawn-pending` line
    silently disables the refusal built on it."""
    events, skipped = ndjson.read_records_counting(run / "events.ndjson")
    if skipped:
        print(f"ai-sdlc-harness: {run / 'events.ndjson'} has {skipped} "
              "unparseable line(s) — a torn `spawn-pending` line is invisible "
              "to the stalled-agent guard, which then cannot see a spawn that "
              "is still in flight.", file=sys.stderr)
    return events


def open_spawn_pendings(run: Path, stall_key: str,
                        manifest: dict) -> list[dict]:
    """Every `spawn-pending` for THIS stall key that nothing has closed yet.

    Closed = the agent_id carries a `spawn-captured` (actor "capture": its
    SubagentStop arrived and the reply was captured) or a `spawn-abandoned`
    (actor "stall": a stall override declared that round dead). Both
    resolvers are actor-checked — without it a hand-written `log-event`
    could UNBLOCK a stall by faking either one, which is the forgery
    direction that matters here.

    Keyed the way `_stall_round_anchor` keys its own event scan — the
    record's `task` against the stall key — so the guard only ever refuses a
    stall it can positively attribute to a spawn in flight; a pending under
    a different key is left to that key's own stall, and a TASK-LESS one is
    attributed by mode (below) rather than swept up by any key that happens
    to be asked about.

    The pairing repeats `workflow.outstanding_flagged`'s rule rather than
    importing it: that is the run-wide GAUGE (every open pending, no key),
    while this is a per-key question, and `harness.workflow` imports the
    engine, not the other way round.

    Reads leniently and returns [] on an unreadable ledger: the guard built
    on this SUPPRESSES an action, so it fails OPEN for the same reason the
    verdict check below does — a ledger that cannot be read cannot prove a
    spawn is live, and bricking the stalled-agent procedure is the worse
    failure. The stall verb's abandonment write inherits that: it can only
    ever abandon pendings it could actually read.

    LIST rather than first-match because the override that abandons them
    must reach every one — a plan panel batches its lens spawns, so one
    stall key can legitimately hold several open pendings, and leaving the
    others open would deadlock that key against guard_spawn's
    one-live-spawn rule with no verb left to clear them.

    TASK-LESS spawns match a `step:` key BY MODE. A pending records the
    spawn prompt's `harness-task` header, which a task-less spawn
    (plan-review, pre-pr, a panel lens) does not carry at all — so its
    pending's task is None while its stall is counted per STEP (`stall` with
    no `--task`). Keying on equality alone made those two spellings of one
    spawn miss each other entirely: the plan-review synthesizer — the field
    case this whole guard was written for — could be live in the background
    and still return `reinvoke`, and with backgrounding now legal the miss
    compounds into a DEAD END (the spawn guard refuses the re-spawn as
    already in flight, and the override that would free it never fires
    because nothing refused).
    Bound, stated (and MODE-bound, not `step:`-prefix-bound — the first
    spelling of this widening swept every task-less pending in the run,
    which adversarial review executed three ways: a per-lens override
    abandoned the live synthesizer, a `step:develop` override abandoned
    plan-review and pre-pr pendings from other steps, and one dangling
    cross-step ghost refused every later step-keyed stall in the run): a
    task-less pending matches only the step whose declared spawn-set holds
    its mode, minus the verdict_bound mode when the key names a lens — see
    `stall_key_spawn_modes`, which owns that rule and its consequences. An
    override on the matching key still abandons EVERY pending it matches, a
    batched panel's siblings included: that reach is deliberate (a
    half-abandoned key deadlocks exactly like an un-abandoned one), it only
    ever fires behind an explicit `--confirm-no-verdict`, and no engine-read
    verdict rides on a lens (plan-review.md: lens verdicts are advisory, the
    synthesizer's is the one the FSM reads)."""
    try:
        events = _read_pairing_events(run)
    except OSError:
        return []
    closed = {e.get("agent_id") for e in events
              if (e.get("kind") == "spawn-captured"
                  and e.get("actor") == "capture")
              or (e.get("kind") == "spawn-abandoned"
                  and e.get("actor") == "stall")}
    task_less_modes = stall_key_spawn_modes(manifest, stall_key)
    return [e for e in events
            if e.get("kind") == "spawn-pending"
            and (e.get("task") == stall_key
                 or (e.get("task") is None
                     and e.get("mode") in task_less_modes))
            and e.get("agent_id") not in closed]


def _open_spawn_pending(run: Path, stall_key: str,
                        manifest: dict) -> dict | None:
    """The first still-open `spawn-pending` for this stall key, or None —
    the one-record question `guard_stall_verdict` asks (it needs an agent id
    to name in the refusal, not the whole set)."""
    return next(iter(open_spawn_pendings(run, stall_key, manifest)), None)


def guard_stall_verdict(state: dict, manifest: dict, run: Path,
                        stall_key: str) -> None:
    """Refuse a stall the run's own verdict ledger already answers.

    field: dual-run comparison — a run's orchestrator finished a
    plan-review synthesis, looked for the verdict in `events.ndjson` (where
    verdicts have never lived), found nothing, and called `stall`. The verb
    dutifully said "reinvoke" and a whole lens panel + synthesis re-ran for
    a verdict already on disk. The duplicate CHANGES_REQUESTED burned one of
    five rounds, the four genuine rounds then hit the bound, and the run
    exhausted into a multi-hour human gate.

    Closed HERE, upstream, and nowhere else: round counting deliberately
    lets duplicate captures burn budget toward the human (manifest.yaml's
    anti-manipulation note) and the exhaustion latch depends on that, so
    neither may be softened to compensate. Only steps declaring
    `verdict_bound` have a verdict to check; everything else (every per-task
    spawn) passes straight through.

    …EXCEPT for a spawn that has not finished yet. A background spawn's
    reply reaches no hook until its SubagentStop, so between launch and
    completion the run's ledgers look exactly like a stall: no verdict, no
    status block, nothing. `spawn-pending` is the record that says
    otherwise, and the stall layer was the one layer never taught to read
    it — adversarial review executed the consequence: `stall` returned
    `reinvoke` over a live background reviewer, the reinvoked copy and the
    original both finished, and latest-wins on reviews.ndjson handed the
    run the STALE APPROVED. Checked before the `step:` filter because the
    proven case is a per-task review key, and "the agent is still running"
    is true of any spawn, verdict_bound or not."""
    open_pending = _open_spawn_pending(run, stall_key, manifest)
    if open_pending is not None:
        raise TransitionError(
            f"'{stall_key}': a spawn for this key is still RUNNING in the "
            f"background (spawn-pending, agent {open_pending.get('agent_id')})"
            " — its reply reaches no hook until it finishes, so the empty "
            "ledger is not a stall, it is a spawn in flight. WAIT for that "
            "agent's SubagentStop: verdict and status-block capture happen "
            "there, and the pending clears itself (spawn-captured). "
            "Reinvoking now runs a second agent against the same worktree "
            "and latest-wins can hand the run the STALE verdict. If that "
            "agent genuinely died (its session ended, the CLI crashed), "
            "re-run with --confirm-no-verdict: that records the stall AND "
            "abandons every open pending on this key (`spawn-abandoned`) — "
            "for a task-less key that is every in-flight spawn of a mode "
            "this step declares — which frees the key "
            "for a fresh spawn and makes the dead agent's reply — should it "
            "surface after all — refused rather than captured.")
    if not stall_key.startswith("step:"):
        return  # per-task spawn: no verdict ledger governs it
    step = stall_key[len("step:"):]
    vb = (manifest.get("steps", {}).get(step) or {}).get("verdict_bound") or {}
    if not vb.get("mode"):
        return
    try:
        latest_verdict, _rounds, at = _verdict_window(state, run, vb["mode"])
    except (TransitionError, OSError):
        # A guard whose job is to SUPPRESS an action must fail OPEN, the
        # opposite of the exit filter this shares a reader with (adversarial-
        # review, both lenses independently). _verdict_window reads
        # reviews.ndjson with strict=True and raises on a torn line — and a
        # torn tail there is written by a capture hook killed mid-append,
        # i.e. exactly the event most correlated with the stall being
        # recorded. Refusing then would brick the stalled-agent procedure,
        # the one path a wedged run depends on, with a message about
        # `verdict_bound` that says nothing about stalls. A ledger that
        # cannot be read cannot prove a verdict exists: let the stall
        # through. OSError joins TransitionError (pre-release review): an
        # exists-but-unreadable ledger (EACCES/EIO) is the same "cannot
        # read" — only the corruption spelling was caught, so the wedged-run
        # path failed CLOSED on exactly the I/O failures most likely to
        # accompany a wedge.
        return
    if latest_verdict is None:
        return
    try:
        anchor = _stall_round_anchor(state, run, stall_key,
                                     vb.get("round_marker"))
    except OSError:
        return  # unreadable events ledger: same fail-open reasoning
    if at <= anchor:
        return  # an EARLIER round's verdict — this stall is genuine
    raise TransitionError(
        f"step '{step}': a {latest_verdict} verdict for mode "
        f"'{vb['mode']}' was already captured at {at} in reviews.ndjson — "
        "refusing to record a stall the ledger already answers. Proceed on "
        "the ledger: verdicts live in reviews.ndjson; events.ndjson carries "
        "stall/hook/status-block events, never verdicts. If the spawn "
        "genuinely stalled AFTER that capture, re-run with "
        "--confirm-no-verdict.")


def record_stall(state: dict, config: dict, task_id: str) -> str:
    """Bounded stalled-agent procedure (coverage B4). Returns the declared
    next action: reinvoke -> recovery -> human.

    A `step:<id>` key counts stalls for a TASK-LESS spawn (plan-review,
    pre-pr, …) at run level — the same declared bounds apply
    (adversarial-review, plan-accuracy round: the task-keyed-only verb left
    the plan-review reviewer, whose verdict is exit-blocking, with an
    UNBOUNDED re-spawn loop and no human escalation trigger).

    The ledger check that gates this lives in `guard_stall_verdict`, called
    by the CLI BEFORE this function so a refusal mutates nothing — same
    validate-before-side-effect split preflight uses."""
    if task_id.startswith("step:"):
        counters = state.setdefault("step_stalls", {})
        counters[task_id] = counters.get(task_id, 0) + 1
        count = counters[task_id]
    else:
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if task is None:
            raise TransitionError(f"unknown task '{task_id}'")
        task["stalls"] = task.get("stalls", 0) + 1
        count = task["stalls"]
    stall_cfg = config["stall"]
    if count < stall_cfg["recovery_after"]:
        return "reinvoke"
    if count < stall_cfg["human_after"]:
        return "recovery"
    return "human"
