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
from pathlib import Path

from . import chain, ndjson

FORWARD_DEFAULT = ("approved",)


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
        raise TransitionError(
            f"predicate needs artifact '{pred.get('value')}' which was never recorded"
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


def cursor_candidates(state: dict, manifest: dict, config: dict) -> dict[str, str]:
    """Legal next steps from the current cursor: {step_id: reason}."""
    steps, mode = manifest["steps"], state["mode"]
    seq = manifest["modes"][mode]
    current = state["cursor"]["current_step"]
    completed = set(state["cursor"]["completed_steps"])
    artifacts = state.get("artifacts", {})
    candidates: dict[str, str] = {}
    cur_def = steps.get(current, {})

    # requires_tasks_registered gates EVERY exit, not just the sequence edge
    # (adversarial-review finding: the check below only suppressed the
    # sequence candidate, so a future manifest giving this step a
    # returns_to/group/escalation edge would leak past a provisional task
    # list — not reachable on today's manifest, where `plan` has only the
    # sequence edge, but hardened here so it stays true regardless).
    if cur_def.get("requires_tasks_registered") and any(
            t.get("provisional") for t in state.get("tasks", [])):
        return {}  # nothing legal until plan-register replaces the seed

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
        t.get("status") in ("done", "archived") for t in state.get("tasks", [])
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

    return candidates


def advance_cursor(state: dict, manifest: dict, config: dict, target: str,
                   now: str) -> list[dict]:
    """Returns the conditional steps SKIPPED by this move (empty for most
    moves) so the caller can ledger them. Field (e2e E2E-1): approve-
    security self-skipped on a below-threshold severity exactly as
    declared, but nothing recorded that the evaluation ever happened — the
    ledger couldn't distinguish 'gate skipped by predicate' from 'gate
    never considered', and the run report simply didn't know."""
    ensure_live(state, f"cursor --to {target}")
    candidates = cursor_candidates(state, manifest, config)
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
        raise TransitionError(
            f"cursor move '{current}' -> '{target}' is not declared legal; "
            f"legal: {sorted(candidates) or 'none (gate undecided?)'}"
        )
    reason = candidates[target]
    current = state["cursor"]["current_step"]
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
    latest = max(qualifying, key=lambda r: r.get("at", ""))
    if latest.get("verdict") != "APPROVED":
        raise TransitionError(
            f"task {task['id']}: latest reviewer verdict is "
            f"{latest.get('verdict')!r}, not APPROVED — rework via "
            "`task --to in-progress` (round-bounded), then re-review")


def _guard_dependencies_done(state: dict, task: dict) -> None:
    """pending -> in-progress requires every depends_on task done/archived —
    the declared task DAG, enforced (it used to be stored and read by
    nothing). plan_register already refused dangling ids and cycles, so
    blocked here always means "not yet", never "never"."""
    by_id = {t["id"]: t for t in state["tasks"]}
    waiting = sorted({d for d in (task.get("depends_on") or [])
                      if by_id.get(d, {}).get("status")
                      not in ("done", "archived")})
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
        # Full mode already clears this at plan-register (tasks are rebuilt
        # wholesale there, well before any task reaches in-progress) — a
        # no-op there. Quick mode has no plan-register at all (adversarial-
        # review finding), so the fetch-seeded task's `provisional: true`
        # would otherwise never clear: the first in-progress transition is
        # the first point a human/orchestrator has actually acted on it.
        task.pop("provisional", None)
    return task


def record_stall(state: dict, config: dict, task_id: str) -> str:
    """Bounded stalled-agent procedure (coverage B4). Returns the declared
    next action: reinvoke -> recovery -> human."""
    task = next((t for t in state["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise TransitionError(f"unknown task '{task_id}'")
    task["stalls"] = task.get("stalls", 0) + 1
    stall_cfg = config["stall"]
    if task["stalls"] < stall_cfg["recovery_after"]:
        return "reinvoke"
    if task["stalls"] < stall_cfg["human_after"]:
        return "recovery"
    return "human"
