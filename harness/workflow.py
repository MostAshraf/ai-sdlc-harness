"""Mechanical workflow steps as code (design.md principle 3).

`fetch` and `preflight` are orchestrator-owned steps whose logic is entirely
mechanical — so they ARE code, invoked by one-line step files. The minimal
`init` writes just enough workspace config for a run (the interactive
interview is M7).
"""
from __future__ import annotations

import datetime as _dt
import json
import posixpath
import re
import subprocess
from pathlib import Path

import yaml

from . import gitops, ndjson, state as state_mod
from .state import safe_id
from .transitions import set_artifact

# The ONE definition of which event kinds a human should be shown — read by
# both `status` (count) and `metrics_report` (table). These used to be two
# hand-maintained lists that drifted (field e2e E2E-1: status said 18
# flagged, metrics.md said 23 — same run, same ledger, different filters).
# `status-block-malformed` is flagged (shown) but is NOT a stall: it
# records a reviewer reply whose engine-read verdict WAS captured despite
# a missing status block, so the stalled-agent procedure must not re-spawn
# (capture_post_spawn holds the emission rule). It stays IN this list
# because that capture rode extract_verdict's no-block whole-text
# fallback — the weakest path — and suppressing the stall also removed the
# re-spawn whose fresh verdict used to supersede a false capture; showing
# the event to the human is the replacing safeguard (adversarial-review
# on this change, both lenses independently).
FLAGGED_EVENT_KINDS = (
    "test-revision", "reviewer-rejected", "hook-blocked",
    "missing-status-block", "status-block-malformed", "quick-recheck",
    "contracts-check", "verdict-uncaptured", "background-spawn-uncaptured",
    "coverage-skipped", "risk-without-tests", "tests-without-production",
    "pr-recorded-manually", "secret-sweep-blocked", "gate-skipped",
    "deferral-pending", "panel-serialized",
    # field: dual-run comparison. Six kinds, in three resolution classes —
    # each stated explicitly, because getting this wrong is what makes the
    # flagged-events gauge either over- or under-report (see
    # outstanding_flagged below).
    #
    # (a) PERMANENT OCCURRENCES, no resolver (like hook-blocked/gate-skipped):
    #     a remote branch that collided, a probe that resolved a remote but
    #     could not answer, a re-run of an already-done item, and a run's
    #     quarantined exclusions. `tests-quarantined` had to EARN this — it
    #     is emitted once per run per (repo, set) rather than once per test
    #     invocation, which is what makes it an occurrence rather than an
    #     O(tasks) repeated assertion (adversarial-review, both lenses).
    "remote-branch-exists", "remote-branch-unverified",
    "work-item-already-done", "tests-quarantined",
    # (b) LIVE PLAN STATE, superseded by the next `plan-registered`: a weak
    #     contract fragment describes the currently-registered plan, exactly
    #     like risk-without-tests.
    "contract-fragment-weak",
    # (c) RESOLVABLE, paired off by `env-prereq-satisfied`: the human starts
    #     the service and re-runs. Permanent here would leave every such run
    #     reading DEGRADED forever (re-verify finding).
    "env-prereq-missing",
    # field: US-CHAT-00 run. Class (c), RESOLVABLE — paired off by
    # `write-back-succeeded`. The milestone write-back is declared best-effort
    # ("never a blocking requirement", write_back below), so a provider that
    # refuses the transition must not abort the run; but swallowing it
    # silently would leave the live tracker stale with nothing anywhere
    # saying so. Surface it on the same gauge a human already reads, and
    # never auto-fix — the house stance.
    #
    # Class (c) rather than (a) for exactly the reason `env-prereq-missing`
    # is: the trigger is an external dependency the human restores (tracker
    # down, credential expired), and the harness then RETRIES by construction
    # — up to three milestones per run (develop_start, in_review, done). A
    # run whose `done` write-back succeeded has a correct tracker and nothing
    # outstanding; leaving the earlier miss permanent made that run report a
    # stale flag forever, the same false reading the re-verify finding
    # rejected one class above (adversarial-review, both lenses
    # independently). Not HEALTH_DEGRADING either: no evidence was lost and
    # the run's own machinery is intact — only the external tracker is behind.
    "write-back-failed")
# None of the seven are HEALTH_DEGRADING (below): each means "a human should
# look", not "this run's machinery degraded / evidence was lost". The closest
# call is `remote-branch-unverified` — evidence genuinely not obtained — but
# preflight continuing without it is the declared, safe behaviour (a
# connectivity blip must not brick a run), not a degraded one.


def outstanding_flagged(events: list[dict]) -> list[dict]:
    """Flagged events with RESOLVED deferrals paired off (validation-walk F5).
    A `deferral-pending` is flagged, but a matching `deferral-recorded`
    resolves it — so `status` and `metrics` report only OUTSTANDING items (a
    live "still owed" gauge, not a permanent tally; gate.md step 6's "stays on
    the dashboard until you pair it" now actually holds). ONE shared filter so
    status.flagged_events and metrics' "## Flagged events (N)" never drift (the
    same reason FLAGGED_EVENT_KINDS itself is shared, above).

    The two events share no key, so pair by ORDER: a `deferral-recorded`
    resolves the EARLIEST still-open `deferral-pending` that PRECEDED it (FIFO).
    A spurious, duplicate, or out-of-order `deferral-recorded` with no open
    pending ahead of it resolves nothing — fail-CLOSED, so a stray record
    (`log-event` is unvalidated) can never silently hide an unrelated
    outstanding deferral (review finding: an audit gauge must not under-count).

    `risk-without-tests` and `tests-without-production` get the same
    live-gauge treatment with a different resolver: each `plan-registered`
    marker supersedes EVERY earlier event of both kinds, because
    plan-register replaces the task list wholesale — only the latest
    registration's batch describes the current plan (adversarial-review on
    the zero-test change, both lenses independently: the append-only batch
    survived the revision round that withdrew the opt-out, misreporting the
    approved plan at every later gate). Both events assert live plan STATE,
    unlike gate-skipped/hook-blocked, which record occurrences and stay
    permanent correctly."""
    flagged = [e for e in events if e.get("kind") in FLAGGED_EVENT_KINDS]
    open_pending: list[dict] = []
    open_plan: list[dict] = []
    open_env: list[dict] = []
    open_wb: list[dict] = []
    resolved: set[int] = set()
    for e in events:
        kind = e.get("kind")
        if kind == "deferral-pending":
            open_pending.append(e)
        elif kind == "deferral-recorded" and open_pending:
            resolved.add(id(open_pending.pop(0)))   # resolve earliest open pending
        elif kind == "env-prereq-missing":
            open_env.append(e)
        elif kind == "env-prereq-satisfied":
            # A prerequisite the human then made available is RESOLVED, not a
            # permanent occurrence — every earlier miss is superseded by one
            # clean probe (the whole set is re-probed each time).
            resolved.update(id(x) for x in open_env)
            open_env.clear()
        elif kind == "write-back-failed":
            open_wb.append(e)
        elif (kind == "write-back-succeeded"
                and e.get("actor") in ("write-back", "reconcile")):
            # Same resolver shape as env-prereq: a later milestone that DID
            # land proves the tracker is no longer behind, so every earlier
            # miss on this run is superseded. Each milestone pushes the one
            # current status, so one success supersedes the whole batch
            # rather than pairing 1:1.
            #
            # actor-checked, exactly like `plan-registered` below: this
            # CLEARS an audit gauge, and `log-event` is unvalidated, so
            # without the check a stray record cleared a genuine outstanding
            # miss — `status.flagged_events` verified going 1 -> 0 on a
            # hand-appended kind (re-verify finding). The real emitter always
            # writes the actor.
            resolved.update(id(x) for x in open_wb)
            open_wb.clear()
        elif kind in ("risk-without-tests", "tests-without-production",
                      "contract-fragment-weak"):
            open_plan.append(e)
        elif (kind == "plan-registered"
                and e.get("actor") == "plan-register"):
            # actor-checked (adversarial-review on the backfill change): a
            # stray `log-event` record with this kind must not silently
            # clear the live gauge — the same fail-closed stance the
            # deferral resolver states above; real markers always carry
            # the actor (emitted in plan_register, nowhere else)
            resolved.update(id(x) for x in open_plan)  # superseded batch
            open_plan.clear()
    return [e for e in flagged if id(e) not in resolved]


# Process-health filter over the same ledger: the kinds that mean the run
# MACHINERY degraded — evidence lost or the stalled-agent procedure
# engaged — as opposed to flagged-but-healthy signals a human reviews
# (field 459226: a run "completed green" over 11 flagged events and 2
# stalls, and the degradation surfaced only in a manual post-mortem).
# Deliberately EXCLUDED: `status-block-malformed` (the verdict WAS
# captured — loose formatting, run healthy; including it made every fork
# field run read DEGRADED and the verdict uninformative), `gate-skipped`
# (declared predicate self-skips are the mode working as designed),
# `risk-without-tests` / `tests-without-production` (recorded, reviewed
# decisions), `contracts-check`
# and `hook-blocked` (content findings / guards doing their job), and
# `panel-serialized` (an efficiency miss — wall-clock wasted, nothing
# lost or stalled).
HEALTH_DEGRADING_KINDS = (
    "missing-status-block", "verdict-uncaptured",
    "background-spawn-uncaptured")


def stall_count(st: dict) -> int:
    """Total stalls recorded by the stalled-agent procedure — per-task
    counters plus the step-keyed counters for task-less spawns. Counted
    from STATE (the authority `record_stall` writes), not from a
    self-reported event kind: the procedure can engage on paths that
    write no capture event at all (hook attribution failure across
    several live runs, a hung spawn with no PostToolUse payload —
    adversarial-review on this change, both lenses), and those stalls
    must still degrade the run."""
    return (sum(t.get("stalls", 0) for t in st.get("tasks", []))
            + sum((st.get("step_stalls") or {}).values()))


def run_health(events: list[dict], stalls: int = 0) -> tuple[str, dict[str, int]]:
    """(verdict, per-kind counts): HEALTHY unless a degrading event was
    recorded or the stalled-agent procedure engaged (`stalls` — pass
    stall_count(st)). ONE definition read by both `status` and
    `metrics_report` — the same anti-drift rule as FLAGGED_EVENT_KINDS
    above. Unlike outstanding_flagged, health is HISTORY, not a live
    gauge: a stall the run later recovered from still degraded it, so
    nothing pairs off."""
    counts: dict[str, int] = {}
    for e in events:
        k = e.get("kind")
        if k in HEALTH_DEGRADING_KINDS:
            counts[k] = counts.get(k, 0) + 1
    return ("DEGRADED" if counts or stalls else "HEALTHY", counts)


def slug(title: str, limit: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:limit].rstrip("-") or "change"


def classify(item: dict, config: dict) -> tuple[str, str]:
    """Ex-ante mode classification (design.md piece 1). Returns
    (mode_verdict, reason) — a VERDICT from this function's vocabulary
    (`quick-eligible` | `lean-requested` | `default`), never a mode NAME:
    the verdict-to-mode mapping is the entry step's declared
    `selects_mode`, resolved by select_mode() below (composability round,
    2026-07-08: this function used to mint the literals "full"/"quick"
    itself, leaving the manifest's selects_mode declaration decorative).
    Precedence: an explicit `Mode: full` hint outranks everything (an item
    asking for MORE gating always gets it — the per-item escape from a
    `default_mode: lean` workspace); then quick (explicitly hinted AND no
    disqualifying keyword — quick-recheck is the real-diff backstop); then
    lean (work-item `Mode: lean` hint, or the workspace's
    `default_mode: lean`). A keyword-disqualified quick hint falls through
    to the lean/default tiers, never straight to full — the keywords guard
    SKIPPING the plan machinery, which lean keeps."""
    qm = config.get("quick_mode", {})
    default_mode = config.get("default_mode", "full")
    if default_mode not in ("full", "lean"):
        # fail loud at fetch, not silently-full for the life of the
        # workspace (a typo'd override would otherwise never be noticed)
        raise state_mod.StateError(
            f"config: default_mode must be 'full' or 'lean' (got "
            f"{default_mode!r}) — fix the workspace override")
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    desc = item.get("description", "")

    def hinted(mode: str) -> bool:
        return bool(re.search(rf"^mode:\s*{mode}", desc,
                              re.MULTILINE | re.IGNORECASE))

    quick_hint = hinted("quick")
    disqualified = next((k for k in qm.get("disqualify_keywords", [])
                         if k.lower() in text), None)
    note = (f" (quick disqualified by keyword '{disqualified}')"
            if quick_hint and disqualified else "")
    if hinted("full"):
        return "full-requested", "explicitly hinted" + note
    if quick_hint and not disqualified:
        return "quick-eligible", "explicitly hinted"
    if hinted("lean"):
        return "lean-requested", "explicitly hinted" + note
    if default_mode == "lean":
        return "lean-requested", "workspace default_mode" + note
    if quick_hint:
        return "default", f"quick disqualified by keyword '{disqualified}'"
    return "default", "default"


def select_mode(manifest: dict, verdict: str) -> str:
    """Resolve classify()'s verdict to a mode name via the entry step's
    declared `selects_mode` mapping — the single place a run's entry mode
    is minted, reading the manifest rather than repeating it."""
    sel = (manifest["steps"][manifest["entry"]] or {}).get("selects_mode") or {}
    mode = sel.get(verdict)
    if mode not in (manifest.get("modes") or {}):
        raise state_mod.StateError(
            f"entry step '{manifest['entry']}' selects_mode maps "
            f"verdict {verdict!r} to {mode!r}, which is not a "
            "declared mode — fix pipeline/manifest.yaml")
    return mode


def resolve_change_type(item: dict, config: dict) -> str:
    mapped = (config.get("work_item_type_map") or {}).get(item.get("type"))
    if mapped:
        return mapped
    change_types = config.get("change_types") or ["chore"]
    return "chore" if "chore" in change_types else change_types[0]


def init_minimal(workspace: Path, stories_dir: Path, repos: dict[str, str],
                 test_cmds: dict[str, str]) -> Path:
    """Per-section config + permissions + the bootstrap marker (M7: each
    section independently refreshable; the interview drives richer values).
    `test_cmds` is keyed by repo name (same keys as `repos`) — language-config
    is per repo, since different repos may use different toolchains."""
    from . import initws
    initws.write_section(workspace, "provider",
                         {"provider": {"work_item": "local-markdown",
                                       "git": "local",
                                       "stories_dir": str(stories_dir)}})
    initws.write_section(workspace, "repos", {"repos": repos})
    language = {name: {"test_cmd": cmd} for name, cmd in test_cmds.items()}
    initws.write_section(workspace, "language", {"language": {"repos": language}})
    initws.write_permissions(workspace, repos, language)
    initws.mark_bootstrapped(workspace)
    return workspace / ".claude" / "context"


def resolve_subagent_model(config: dict, shape: str, mode: str) -> str:
    """Per-shape/per-mode model override resolution (design.md piece 3):
    `per-mode ?? per-shape default ?? 'inherit'`. `inherit` means the caller
    passes NO model at spawn time — the subagent runs on the session model.
    The orchestrator calls this before every harness-shape spawn (the
    single control point `subagent_models` exists to be)."""
    entry = (config.get("subagent_models") or {}).get(shape, "inherit")
    if isinstance(entry, dict):
        return entry.get(mode) or entry.get("default", "inherit")
    return entry


def resolve_lenses(config: dict, change_type: str | None) -> list[str]:
    """The plan-review lens panel for THIS run's change_type (field 459226
    rec #3: lean ran the full adversarial panel, full round budget, for an
    all-low-risk chore). `plan_review.lenses` is the default panel;
    `lenses_by_change_type` overrides it per change_type. An explicitly
    mapped EMPTY list is the declared single-reviewer fallback (the
    synthesizer reviews the plan directly — plan-review.md step 1); an
    UNMAPPED change_type gets the full default panel — fail toward MORE
    review, never less. The orchestrator resolves this via
    `harness resolve-lenses`, the single control point (mirror of
    resolve_subagent_model above)."""
    pr = config.get("plan_review") or {}
    by_ct = pr.get("lenses_by_change_type") or {}
    if change_type in by_ct:
        return list(by_ct[change_type])
    default = pr.get("lenses")
    return list(default) if default is not None else ["contradictions", "gaps"]


def bootstrap_gate(config: dict) -> None:
    if not config.get("bootstrap_completed"):
        raise state_mod.StateError(
            "bootstrap incomplete — run /init-workspace before /dev-workflow")


#: Provider-independent "this item is already finished" vocabulary. Each
#: adapter ships its own STATUS_DEFAULTS (github `closed`, ado `Closed`,
#: jira `Done`, …) and users remap via `status_mapping`, so both are
#: consulted first — this set only catches the rest.
_DONE_VOCABULARY = frozenset(
    {"done", "closed", "resolved", "completed", "complete", "shipped"})


def done_state_match(config: dict, item: dict) -> str | None:
    """The item's provider state, if it reads as ALREADY DONE — else None.

    field: dual-run comparison — a story still marked `Done` from a
    run three days earlier was re-fetched and rebuilt end-to-end; the intake
    noticed the stale state and flagged it as an ambiguity, but nothing
    mechanical acted on it, and the run only collided at push.

    Matching is deliberately TOLERANT and deliberately WARN-ONLY. Provider
    status vocabularies differ and adapters pass decorated values through
    verbatim (`local-markdown` yields whole header lines like
    `✅ Done — 2026-07-22`), so the state is casefolded, split into word
    tokens, and tested for any done token. That can over-match in principle
    (`Not Done`); over-matching costs one warning line, whereas refusing
    would block the legitimate replay/re-plan cases — which is exactly why
    this warns instead of blocking."""
    raw = str(item.get("state") or "").strip()
    if not raw:
        return None
    known = set()
    try:
        from .providers import get_module
        provider_done = getattr(get_module(config), "STATUS_DEFAULTS", {}
                                ).get("done")
        if provider_done:
            known.add(str(provider_done).casefold())
    except Exception:      # unknown/unset provider: fall back to the vocabulary
        pass
    mapping = config.get("status_mapping") or {}
    if not isinstance(mapping, dict):
        # Unvalidated config key, and this is the FIRST verb a run touches —
        # a list-shaped status_mapping used to escape as a raw AttributeError
        # traceback instead of the JSON error contract (pre-release review;
        # the identical class this branch fixed for env_requirements).
        raise state_mod.StateError(
            "config `status_mapping` must be a mapping of work-item type -> "
            f"{{milestone: status}} (got {type(mapping).__name__})")
    # Type-specific SHADOWS default — the same precedence
    # resolve_write_back_status uses, so the two readers of this declared
    # data cannot hold different beliefs about it (adversarial-review).
    # Note this only decides the CONFIGURED name: the universal vocabulary
    # below still matches, by design, so a `default: {done: Shipped}` that a
    # type overrides can still be recognized on its own English merits
    # (re-verify: worth stating, since the shadow alone doesn't suppress it).
    item_type = item.get("type")
    section = (mapping.get(item_type) if item_type else None) or \
        mapping.get("default", {})
    configured = section.get("done") if isinstance(section, dict) else None
    if configured:
        known.add(str(configured).casefold())
    tokens = {t for t in re.split(r"[^0-9a-z]+", raw.casefold()) if t}
    if known & ({raw.casefold()} | tokens) or tokens & _DONE_VOCABULARY:
        return raw
    return None


def _bootstrap_from_item(workspace: Path, config: dict, manifest: dict,
                         item: dict, date: str | None) -> dict:
    """The shared step-one *tail* (RC2), transport-independent: classify ->
    resolve change_type -> bootstrap state (from-nothing transition, collision-
    refusing) -> seed a single task -> persist work-item.json. Both transports
    converge here — the CLI path after `dispatch`, the MCP path after
    `normalize` — so a run is bootstrapped identically whichever was used."""
    mode_verdict, reason = classify(item, config)
    mode = select_mode(manifest, mode_verdict)
    change_type = resolve_change_type(item, config)
    date = date or _dt.date.today().isoformat()
    # same-day re-runs (abort → re-fetch) land in a `-<n>` slot instead of
    # colliding with the terminal occupant of the deterministic name
    run = state_mod.next_run_slot(
        workspace / "ai" / f"{date}-{safe_id(item['id'])}", workspace, manifest)
    repos = list((config.get("repos") or {"." : "."}).values())
    # The single seeded task is a PLACEHOLDER, not a scope decision: `repos[0]`
    # is just whichever repo is listed first in repos.yaml (a positional
    # default, no content analysis). It is flagged `provisional` so anyone
    # inspecting state.yaml / `show` sees it isn't a ratified plan — the real
    # task list is set at plan-register, which replaces this wholesale.
    seed_repo = repos[0]
    # Written BEFORE bootstrap (adversarial-review round 1 finding): a crash
    # between a successful bootstrap and this write used to leave a run with
    # sealed state.yaml but no work-item.json — and no way to retry, since
    # the collision check refuses on the exact run path existing. A plain
    # JSON write has no such collision semantics, so writing it first makes
    # a crash-then-retry just re-fetch and overwrite it harmlessly before
    # bootstrap runs.
    #
    # Guarded on state.yaml NOT existing yet (adversarial-review round 2
    # finding: the crash-recovery reordering above, applied unconditionally,
    # let a same-day re-fetch collision overwrite the EXISTING live run's
    # work-item.json with the new fetch's content BEFORE bootstrap's own
    # collision check ever ran — reproduced directly: re-fetching a
    # same-day work item after its source ticket's title changed left
    # work-item.json permanently mismatched against the original run's
    # state.yaml/tasks/plan, even though bootstrap correctly refused right
    # after). When state.yaml already exists, skip straight to bootstrap()
    # and let its own collision check raise untouched — never overwrite a
    # live run's work-item.json.
    if not state_mod.state_path(run).exists():
        run.mkdir(parents=True, exist_ok=True)
        (run / "work-item.json").write_text(
            json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
    state_mod.bootstrap(
        run, workspace,
        work_item={"id": item["id"], "title": item["title"],
                   "provider_ref": item["provider_ref"],
                   "type": item.get("type")},
        mode=mode, change_type=change_type,
        tasks=[{"id": "T1", "repo": seed_repo, "provisional": True}],
        entry_step=manifest["entry"], manifest=manifest)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "fetched", "item": item["id"], "mode": mode,
                          "mode_verdict": mode_verdict,
                          "classify_reason": reason,
                          "change_type": change_type,
                          "seed_task": {
                              "id": "T1", "repo": seed_repo,
                              "basis": "positional-default (repos[0]); "
                                       "provisional until plan-register"},
                          "actor": "fetch"})
    result = {"run": str(run), "mode": mode, "change_type": change_type,
              "classify_reason": reason}
    already_done = done_state_match(config, item)
    if already_done:
        # Warn at minute zero rather than blocking: replays and re-plans of a
        # closed item are legitimate. The flagged event is what makes the
        # decision auditable — and what preflight's remote-branch probe then
        # corroborates if a prior run really did ship this
        # (field: dual-run comparison).
        ndjson.append_record(run / "events.ndjson",
                             {"kind": "work-item-already-done",
                              "item": item["id"], "state": already_done,
                              "actor": "fetch"})
        result["already_done"] = already_done
        result["warning"] = (
            f"work item {item['id']} is already in state '{already_done}' — "
            "it may have been built by an earlier run. Confirm this is an "
            "intentional replay before planning; preflight will refuse if a "
            "prior run's branch still occupies the remote.")
    return result


def fetch_flow(workspace: Path, config: dict, manifest: dict, item_id: str,
               date: str | None = None) -> dict:
    """CLI/file-transport step-one: `dispatch` executes the fetch, then the
    shared tail bootstraps the run. MCP-transport providers `dispatch`-refuse
    here (a script cannot call their tools) — the orchestrator invokes the
    mapped tool and pipes the raw result to `fetch_from_raw` instead."""
    from .providers import dispatch
    bootstrap_gate(config)
    item = dispatch(config, "work_item.fetch", id=item_id)
    return _bootstrap_from_item(workspace, config, manifest, item, date)


def fetch_from_raw(workspace: Path, config: dict, manifest: dict, raw: dict,
                   date: str | None = None) -> dict:
    """MCP-transport step-one: the orchestrator invoked the mapped MCP tool (a
    script cannot) and pipes the raw result here. We run the SAME scriptable
    `normalize` the CLI path's adapter runs internally, then the shared tail —
    so the run bootstraps identically regardless of transport."""
    from .providers import normalize
    bootstrap_gate(config)
    item = normalize(config, "work_item.fetch", raw)
    return _bootstrap_from_item(workspace, config, manifest, item, date)


CONTRACT_TYPES = {"http", "service-bus", "dto"}
#: Leading tokens that mark an http fragment as a method+path DESCRIPTION
#: rather than a grep-able route substring (see _contract_advisories).
_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
               "TRACE", "CONNECT"}


def _contract_advisories(c: dict) -> list[str]:
    """Weak-but-legal contract shapes, surfaced as flags rather than
    refusals.

    field: dual-run comparison — one run registered
    `GET /api/v1/admin/workflows/discovery` as its http fragment. The
    method+path form appears verbatim in neither client nor controller
    source (a client calls `get("/api/v1/…")`; a controller carries the verb
    as a decorator), so reconcile-contracts reported `drift` on a correctly
    implemented contract and pre-PR had to adjudicate it away. The other run
    registered the bare route and got `clean`. The verb adds nothing to
    matching and reliably breaks it.

    FLAGGED, not refused: the method+path form is a documented, supported
    shape and it matches fine where source really does carry it verbatim
    (route tables, generated clients). Refusing would break working setups
    to prevent a false POSITIVE — the wrong trade. The flag reaches
    plan-review and the human, the same treatment `risk-without-tests`
    gets."""
    if c.get("type") != "http":
        return []
    sig = c.get("signature")
    fragments = sig if isinstance(sig, list) else [sig]
    out = []
    for f in fragments:
        head = str(f or "").strip().split(None, 1)
        if head and head[0].upper() in _HTTP_VERBS:
            route = head[1] if len(head) > 1 else "the route alone"
            out.append(
                f"contract {c.get('id')}: http fragment {f!r} leads with an "
                "HTTP verb. reconcile-contracts matches fragments as literal "
                "source substrings, and method+path rarely appears verbatim "
                f"in either side's code — prefer {route!r} (or a distinctive "
                "tail of it) unless your source really does carry the verb.")
    return out


def _validate_contract(c: dict) -> None:
    """A contract needs id + signature, plus EITHER a flat `repos` list
    (legacy shape, still fully supported) OR a directional `producer` +
    `consumers` pair — not both (ambiguous: a stale `repos` alongside a
    directional pair would never be checked or surfaced as an error).
    `signature` may be a string or a list of fragments, each non-empty —
    reconciliation requires all fragments present. `type` is optional,
    surfaced in the report alongside producer/consumer roles."""
    if not c.get("id") or not c.get("signature"):
        raise state_mod.StateError("contract needs id and signature")
    fragments = c["signature"] if isinstance(c["signature"], list) else [c["signature"]]
    if not fragments or any(not f for f in fragments):
        raise state_mod.StateError("signature fragments must be non-empty")
    # F3 (validation-walk): reconcile-contracts matches each fragment by a
    # LITERAL `git grep -F` in every named repo — except a `type: http`
    # fragment carrying a `{param}` token, whose params elide to one path
    # segment each (_http_route_regex) while the surrounding route text
    # still matches literally. So a fragment must be a grep-able code
    # substring — a bare symbol, signature, or route template that appears
    # in source (`archived`, `filter_notes(notes, tag)`,
    # `{id}/authorization`), never an English description. A prose fragment
    # matches nothing and false-reports drift on correctly-implemented code
    # (E2E-1 false positive). An em/en-dash is a reliable prose tell (a code
    # token never carries one), so reject it at declaration — fail-fast —
    # rather than surfacing a phantom drift at pre-pr. (True semantic
    # matching is the future upgrade noted in reconcile_contracts.)
    for f in fragments:
        if "—" in f or "–" in f:
            raise state_mod.StateError(
                f"contract signature fragment {f!r} reads as prose (dash "
                "separator) — reconcile-contracts matches fragments by literal "
                "source search (route-structural only for http {param} "
                "templates); declare a grep-able code token or signature "
                "that appears verbatim in the repo (e.g. `archived`, "
                "`filter_notes(notes, tag)`), not an English description")
    has_repos, has_directional = bool(c.get("repos")), bool(c.get("producer")) and bool(c.get("consumers"))
    if has_repos and has_directional:
        raise state_mod.StateError(
            "contract must declare repos OR producer+consumers, not both")
    if not has_repos and not has_directional:
        raise state_mod.StateError(
            "contract needs repos, or producer + consumers")
    if c.get("type") is not None and c["type"] not in CONTRACT_TYPES:
        raise state_mod.StateError(
            f"contract type must be one of {sorted(CONTRACT_TYPES)}")
    if c.get("type") == "http":
        # Same fail-fast slot as the em-dash prose tell above: an http
        # fragment that is route-parameters ONLY (`{id}`, `{a}/{b}`)
        # elides to an anchorless pattern that would report "present"
        # everywhere — a vacuous check whose failure mode is invisible
        # false CLEAN, strictly worse than the prose fragment's visible
        # false drift (adversarial-review on this change, both lenses
        # independently). Reject at declaration; _http_route_regex also
        # refuses the shape at match time for pre-existing state.
        for f in fragments:
            parts = _HTTP_TOKEN_RE.split(f)
            if len(parts) > 1 and not any(
                    ch.isalnum() for p in parts[::2] for ch in p):
                raise state_mod.StateError(
                    f"http contract fragment {f!r} is route-parameters "
                    "only — each {param} matches ANY one path segment, so "
                    "this fragment would report present everywhere; anchor "
                    "the template with a literal segment (`users/{id}`, "
                    "not `{id}`)")


def scope_register(workspace: Path, run: Path, manifest: dict, config: dict,
                   repos: list) -> dict:
    """Record the human-confirmed target-repo set for this run — the scope
    the planner decomposes within (`plan_register` refuses tasks outside
    it). Legal at `intake` (first registration, right after the user
    confirms intake's Target Repos proposal) and at `plan` (re-registration
    when a revision round legitimately widens/narrows the set — still
    user-confirmed, never silent). Entries are the exact registered repo
    PATHS (config repos VALUES), matching what plan-register's tasks carry."""
    from .transitions import ensure_live
    if not isinstance(repos, list) or not repos or not all(
            isinstance(r, str) and r for r in repos):
        raise state_mod.StateError(
            "scope-register: --repos-json must be a non-empty JSON array of "
            "registered repo path strings")
    registered = set((config.get("repos") or {}).values())
    unknown = sorted(set(repos) - registered)
    if unknown:
        raise state_mod.StateError(
            "scope-register: not registered in this workspace's repos: "
            f"{', '.join(unknown)} — entries must be the exact registered "
            "repo PATH strings (config repos VALUES, e.g. /abs/path/to/"
            "backend), never the short NAMES")
    scoped = sorted(set(repos))
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "scope-register")
        cursor = st["cursor"]["current_step"]
        # Legality is derived from the manifest, not a hardcoded step list
        # (adversarial-review: a second copy of a fact `produces: scope`
        # already declares would silently refuse a future mode's scope-
        # producing step that validates clean).
        producers = sorted(sid for sid, s in (manifest.get("steps") or {}).items()
                           if "scope" in (s.get("produces") or []))
        if cursor not in producers:
            raise state_mod.StateError(
                "scope-register is legal only at a step the manifest "
                f"declares producing 'scope' ({', '.join(producers)}) — "
                f"cursor: {cursor}")
        # Containment is an invariant, not a point-in-time check: a
        # re-registration must not strand already-registered tasks outside
        # the new set (adversarial-review: narrowing after plan-register
        # silently broke tasks ⊆ scope with nothing left to notice it).
        stranded = sorted({t.get("repo", ".") for t in st.get("tasks", [])
                           if not t.get("provisional")
                           and t.get("repo", ".") not in scoped})
        if stranded:
            raise state_mod.StateError(
                "scope-register: registered task repo(s) would fall outside "
                f"the new scope: {', '.join(stranded)} — re-run plan-register "
                "with a task list inside the new set first, or include those "
                "repos")
        set_artifact(st, manifest, "scope", scoped)
        st["scope"] = {"repos": scoped, "at": ndjson.now_iso()}
        state_mod.save(run, workspace, st)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "scope-registered", "repos": scoped,
                          "actor": "scope-register"})
    return {"scope": scoped}


def plan_register(workspace: Path, run: Path, manifest: dict,
                  tasks: list[dict], contracts: list[dict] | None = None,
                  config: dict | None = None) -> dict:
    """Replace the fetch-seeded task list with the approved plan's tasks
    (+ declared cross-repo contracts). Legal only while the cursor is at
    `plan` — the plan is what the gate will approve. `config` feeds the
    coverage-backfill policy's `language.test_paths`/`test_closure`
    vocabulary; absent, the same `["tests/**"]` default verify-red's
    test-set falls back to (config-less callers only exist in unit
    harnesses — the CLI always threads the merged config)."""
    from .transitions import ensure_live
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "plan-register")
        if st["cursor"]["current_step"] != "plan":
            raise state_mod.StateError(
                "plan-register is legal only at the plan step "
                f"(cursor: {st['cursor']['current_step']})")
        ids = [t["id"] for t in tasks]
        if len(ids) != len(set(ids)) or not ids:
            raise state_mod.StateError("task ids must be non-empty and unique")
        for t in tasks:
            state_mod.validate_task_id(t["id"])
        # depends_on was stored but never validated (adversarial-review
        # finding): a dangling id could never be satisfied, and a cycle
        # deadlocked every involved task — both only surfacing mid-develop.
        # Enforcement of the declared order lives in the task FSM's
        # `dependencies-done` guard; registration just refuses bad shapes.
        id_set = set(ids)
        deps = {t["id"]: list(t.get("depends_on") or []) for t in tasks}
        for tid, dlist in deps.items():
            dangling = sorted(set(dlist) - id_set)
            if dangling:
                raise state_mod.StateError(
                    f"task {tid}: depends_on names unknown task(s) "
                    f"{', '.join(dangling)}")
        remaining = {tid: set(dlist) for tid, dlist in deps.items()}
        while remaining:
            free = [tid for tid, dl in remaining.items() if not dl]
            if not free:  # Kahn's algorithm: no dependency-free task left
                raise state_mod.StateError(
                    "depends_on contains a cycle among: "
                    + ", ".join(sorted(remaining)))
            for tid in free:
                remaining.pop(tid)
            for dl in remaining.values():
                dl.difference_update(free)
        # Scope containment (after the payload-shape checks — a malformed
        # list gets its shape error, not a scope one): the task list must
        # stay inside the human-confirmed target-repo set (scope-register).
        # Fail-closed on a missing scope — an unconfirmed decomposition is
        # exactly what the confirmation step exists to prevent.
        scope = (st.get("scope") or {}).get("repos") or []
        if not scope:
            raise state_mod.StateError(
                "plan-register: no confirmed target-repo scope — record the "
                "user-confirmed set first (`harness scope-register`, from "
                "intake's Target Repos proposal), then register tasks")
        off_scope = sorted({t.get("repo", ".") for t in tasks} - set(scope))
        if off_scope:
            raise state_mod.StateError(
                "plan-register: task repo(s) outside the confirmed scope: "
                f"{', '.join(off_scope)} — widen the scope first (user-"
                "confirmed `harness scope-register` at the plan step); an "
                "unconfirmed repo never enters the task list silently")
        # Shape checks for the two fields the zero-test policy below makes
        # load-bearing (first change to read them for policy): garbage must
        # refuse loudly at the owned entry point — a dict stringifies into
        # a truthy "recorded decision", and a string test_intents reads as
        # per-character intents at verify-red, dead-ending the task at
        # develop (adversarial-review on this change).
        for t in tasks:
            if not isinstance(t.get("test_intents", []), list):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: test_intents must be "
                    "a LIST of test names (got "
                    f"{type(t.get('test_intents')).__name__})")
            if t.get("no_test_reason") is not None and not isinstance(
                    t["no_test_reason"], str):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: no_test_reason must "
                    f"be a string (got {type(t['no_test_reason']).__name__})")
            if t.get("files") is not None and not isinstance(
                    t["files"], list):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: files must be a LIST "
                    "of path strings (the plan's file-touch manifest; got "
                    f"{type(t['files']).__name__})")
            for f in t.get("files") or []:
                if not isinstance(f, str) or not f.strip():
                    raise state_mod.StateError(
                        f"plan-register: task {t['id']}: files must be a "
                        f"LIST of non-empty path strings ({f!r} isn't)")
                # repo-relative FILE paths only: an absolute, escaping, or
                # directory-shaped entry can't be honestly classified
                # against the repo's test globs and would satisfy (or
                # defeat) the backfill policy below vacuously. Checked on
                # the raw slash-normalized form — `_norm_file` collapses
                # `./` after this, and normpath never introduces `..`.
                raw = f.strip().replace("\\", "/")
                if (raw.startswith("/") or re.match(r"^[A-Za-z]:", raw)
                        or ".." in raw.split("/")):
                    raise state_mod.StateError(
                        f"plan-register: task {t['id']}: files entry "
                        f"{f!r} must be a repo-relative path (no absolute "
                        "paths, drive letters, or '..' segments)")
                if raw.endswith("/") or posixpath.normpath(raw) == ".":
                    raise state_mod.StateError(
                        f"plan-register: task {t['id']}: files entry "
                        f"{f!r} is a directory, not a file — manifest "
                        "entries name the specific files the task "
                        "creates or modifies")
            if t.get("test_only_reason") is not None and not isinstance(
                    t["test_only_reason"], str):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: test_only_reason must "
                    "be a string (got "
                    f"{type(t['test_only_reason']).__name__})")
            # env_requires: declared at plan time, probed before the spawn.
            # The vocabulary is `env_requirements` in config — an unknown
            # name REFUSES here rather than reading as a checked requirement
            # `env-check` would silently skip (half-enforced-vocabulary bar).
            # Vocabulary is only checkable when a config was threaded; the
            # CLI always threads it, unit harnesses may not (same stance the
            # language/test_paths default below takes).
            reqs = t.get("env_requires", [])
            if not isinstance(reqs, list):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: env_requires must be a "
                    f"LIST of requirement names (got {type(reqs).__name__})")
            _envcfg = (config or {}).get("env_requirements") or {}
            if not isinstance(_envcfg, dict):
                raise state_mod.StateError(
                    "config `env_requirements` must be a mapping of name -> "
                    f"{{probe, hint}} (got {type(_envcfg).__name__})")
            declared = set(_envcfg)
            for r in reqs:
                if not isinstance(r, str) or not r.strip():
                    raise state_mod.StateError(
                        f"plan-register: task {t['id']}: env_requires must "
                        f"be a LIST of non-empty names ({r!r} isn't)")
                # normalized BEFORE the vocabulary check, so validation and
                # the persisted form below agree on what the name is
                if config is not None and r.strip() not in declared:
                    raise state_mod.StateError(
                        f"plan-register: task {t['id']}: env_requires names "
                        f"'{r.strip()}', which has no probe declared. Known: "
                        f"{', '.join(sorted(declared)) or '(none)'}. Add it "
                        "to `env_requirements` in workspace config (name -> "
                        "{probe, hint}) so env-check can actually verify it "
                        "— an unprobeable requirement reads as checked.")
        # Zero-test policy (field 459226): `test_intents: []` stays the
        # plan-approved opt-out, but at any risk OTHER THAN "low" the
        # opt-out must carry a RECORDED reason — one medium-risk task
        # shipped with no tests and no red-proof, justified only by an
        # unrecorded repo coverage convention, and the gap surfaced only
        # in a manual post-mortem. Fail-closed over the free-form risk
        # vocabulary: any value except the exact string "low" demands the
        # reason (a custom or typo'd tier must not silently duck the
        # policy). Tests are never forced — the reason is stored on the
        # task and flagged (`risk-without-tests`, below) so plan-review
        # judges it and status/metrics show it: a recorded, reviewed
        # decision, never a silent gap.
        for t in tasks:
            if (t.get("risk", "low") != "low" and not t.get("test_intents")
                    and not (t.get("no_test_reason") or "").strip()):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']} declares risk "
                    f"{t.get('risk')!r} with no test_intents and no "
                    "no_test_reason — declare the tests, or record WHY "
                    "none apply (e.g. a repo coverage-exclusion "
                    "convention); the reason is flagged for plan-review "
                    "and the human, never assumed")
        # Coverage-backfill policy (field 459226 postmortem F-2 — the
        # mechanical half; the prompt half shipped in plan-task.md rule 2):
        # a task WITH test intents must touch at least one file OUTSIDE
        # the repo's test set, or it dead-ends at develop — a test
        # proving already-correct behavior never goes red, one documenting
        # an unfixed bug never goes green — past every plan gate, at
        # maximum cost (the field run ended in a full run abort).
        # Classification is `language.test_paths` ∪ `test_closure` — the
        # full set verify-red SHA-locks with the red proof — so a file
        # whose post-red edit develop would refuse (a locked shared
        # fixture, root `conftest.py`) can never pose as the production
        # entry at plan time (adversarial-review on this change: with
        # test_paths alone, the fixture-layer exception under-fired by
        # directory layout). The gate's reach equals this vocabulary's
        # coverage — a layout it doesn't name (e.g. rspec `spec/`) needs
        # the workspace `language.test_paths` override, the same
        # precondition the whole TDD apparatus already has. Fail-closed:
        # a test-carrying task with NO recorded manifest refuses too (an
        # absent manifest must not duck the policy — the custom-risk-tier
        # stance). The judged exception — a task whose PRODUCT is test
        # infrastructure — is a recorded `test_only_reason`, stored on the
        # task and flagged (`tests-without-production`) for plan-review
        # and the human, mirroring `no_test_reason`. The gate proves the
        # manifest's STRUCTURE, not its honesty — a fabricated production
        # entry passes here; manifest honesty stays plan-review's
        # spot-check job.
        lang = (config or {}).get("language", {})
        test_globs = (lang.get("test_paths", ["tests/**"])
                      + (lang.get("test_closure") or []))

        def _norm_file(f: str) -> str:
            # ONE normal form for classification AND persistence: slashes
            # forward, `./` collapsed (adversarial-review: a literal
            # `./tests/…` entry classified as production — two characters
            # re-opened the dead-end this policy closes)
            return posixpath.normpath(f.strip().replace("\\", "/"))

        def _prod_files(t: dict) -> list[str]:
            return [f for f in (t.get("files") or [])
                    if not gitops.matches_any(_norm_file(f), test_globs)]

        for t in tasks:
            if not t.get("test_intents"):
                continue
            if not t.get("files"):
                raise state_mod.StateError(
                    f"plan-register: task {t['id']} declares test_intents "
                    "but no `files` manifest — carry the plan's file-touch "
                    "manifest (repo-relative paths) so registration can "
                    "prove a production change exists alongside the tests")
            if not _prod_files(t) and not (
                    t.get("test_only_reason") or "").strip():
                raise state_mod.StateError(
                    f"plan-register: task {t['id']}: every files entry "
                    "matches language.test_paths/test_closure — a "
                    "coverage-backfill task can never satisfy the "
                    "red-proof (a test proving already-correct behavior "
                    "never goes red). Fold the tests into the task that "
                    "changes production code, defer the backfill, or — if "
                    "this task's PRODUCT is test infrastructure — record "
                    "test_only_reason; the reason is flagged for "
                    "plan-review and the human, never assumed. (A "
                    "PRODUCTION file merely NAMED like a test — e.g. "
                    "src/load_test.py under **/*_test.* — means the glob "
                    "overmatches: narrow the workspace "
                    "language.test_paths override instead of recording a "
                    "reason that isn't true)")
        st["tasks"] = [
            {"id": t["id"], "repo": t.get("repo", "."), "status": "pending",
             "depends_on": t.get("depends_on", []), "risk": t.get("risk", "low"),
             "test_intents": t.get("test_intents", []),
             # the SAME normal form the policy judged — a stored
             # backslashed/`./` spelling would hand the first future
             # consumer a classification the policy already solved
             "files": [_norm_file(f) for f in (t.get("files") or [])],
             # a reason only means something for a zero-test task — one
             # riding alongside declared intents (planner confusion, or a
             # revision that added tests without dropping the stale field)
             # is normalized away, never stored as a self-contradictory
             # record no surface would ever render
             "no_test_reason": ((t.get("no_test_reason") or "").strip() or None)
                               if not t.get("test_intents") else None,
             # the same stale-reason rule for the backfill mirror: the
             # reason only means something for a test-carrying task whose
             # manifest has NO production entry — any other combination
             # normalizes away
             "test_only_reason": ((t.get("test_only_reason") or "").strip()
                                  or None)
                                 if (t.get("test_intents")
                                     and not _prod_files(t)) else None,
             # normalized + de-duplicated, order preserved: env-check probes
             # each distinct requirement once per invocation
             "env_requires": list(dict.fromkeys(
                 r.strip() for r in (t.get("env_requires") or []))),
             "commit_sha": None, "review_rounds": 0, "stalls": 0,
             "worktree": None}
            for t in tasks]
        for c in contracts or []:
            _validate_contract(c)
        st["contracts"] = contracts or []
        state_mod.save(run, workspace, st)
        # Events AFTER the save, matching every sibling verb — a failed
        # save must not leave phantom ledger records (the module-wide
        # trade: append-may-fail under-counts, never over-counts). The
        # `plan-registered` marker lands FIRST: registration replaces the
        # task list wholesale, so the marker supersedes every EARLIER
        # `risk-without-tests` batch in outstanding_flagged and the gauge
        # tracks the latest registration only; the fresh batch follows it.
        ndjson.append_record(run / "events.ndjson", {
            "kind": "plan-registered", "actor": "plan-register",
            "count": len(ids)})
        for t in st["tasks"]:
            if (t["risk"] != "low" and not t["test_intents"]
                    and t["no_test_reason"]):
                ndjson.append_record(run / "events.ndjson", {
                    "kind": "risk-without-tests", "task": t["id"],
                    "actor": "plan-register", "reason": t["no_test_reason"]})
            # normalization above guarantees: stored reason ⟺ test-carrying
            # task whose whole manifest is test paths — flag exactly those
            if t["test_only_reason"]:
                ndjson.append_record(run / "events.ndjson", {
                    "kind": "tests-without-production", "task": t["id"],
                    "actor": "plan-register",
                    "reason": t["test_only_reason"]})
        # Weak-but-legal contract shapes: flagged for plan-review and the
        # human, never refused (see _contract_advisories). Superseded by the
        # next `plan-registered` exactly like the two batches above — a
        # revision that fixes a fragment must clear its flag.
        for c in contracts or []:
            for advisory in _contract_advisories(c):
                ndjson.append_record(run / "events.ndjson", {
                    "kind": "contract-fragment-weak", "contract": c.get("id"),
                    "actor": "plan-register", "reason": advisory})
    return {"tasks": ids, "contracts": [c["id"] for c in contracts or []]}


def quick_recheck(workspace: Path, run: Path, config: dict, manifest: dict,
                  repo: Path, base: str) -> str:
    """Post-develop diff-pattern re-check (RC3 small): the ex-ante classifier
    saw only work-item text; this sees the REAL diff. Any disqualify-pattern
    hit dirties the verdict, which the declared escalation edge consumes.
    Also checks the SIZE dimension of "quick" (adversarial-review finding:
    `quick_mode.loc_max`/`files_max` were schema-validated but never
    consumed anywhere — a 5,000-line quick diff passed recheck as long as
    it avoided the disqualify paths)."""
    touched = gitops.diff_paths(repo, base)
    qm = config.get("quick_mode", {})
    patterns = [p for group in (qm.get("disqualify_patterns") or {}).values()
                for p in group]
    hits = sorted({t for t in touched if gitops.matches_any(t, patterns)})
    loc = gitops.diff_line_count(repo, base)
    oversized = (len(touched) > qm.get("files_max", len(touched))
                or loc > qm.get("loc_max", loc))
    verdict = "dirty" if hits or oversized else "clean"
    from .transitions import ensure_live
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "quick-recheck")
        set_artifact(st, manifest, "recheck-verdict", verdict)
        state_mod.save(run, workspace, st)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "quick-recheck", "verdict": verdict,
                          "hits": hits, "files_touched": len(touched),
                          "loc_changed": loc, "actor": "quick-recheck"})
    return verdict


def security_scan(workspace: Path, run: Path, config: dict, manifest: dict) -> str:
    """Owned security step: runs every registered repo's own configured
    scanner (language-config convention: `security.scan_cmd` is per repo,
    since different repos may need different scanners) concurrently, then
    aggregates to ONE true max severity across all repos — a clean scan of
    one repo must never silently overwrite a critical finding in another,
    since the ⟨approve-security⟩ gate's `when` predicate reads this single
    aggregate artifact. Mirrors reconcile_contracts' all-repos-in-one-call
    pattern rather than one CLI invocation per repo."""
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from . import initws
    from .transitions import ensure_live
    with state_mod.locked_read(run):
        ensure_live(state_mod.load(run, workspace), "security-scan")
    sec = config.get("security", {})
    order = sec["severity_order"]
    regex = sec.get("severity_regex", r"(?i)\b(critical|high|medium|low)\b")

    def scan_one(item):
        name, path = item
        cmd = initws.resolve_scan_cmd(config, path)
        if not cmd:
            return (name, order[0],
                    f"## {name}\n\nNo scanner configured "
                    f"(`security.scan_cmd.{name}`) — informational.\n")
        try:
            proc = subprocess.run(cmd, shell=True, cwd=path, capture_output=True,
                                  text=True, timeout=900,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            # Uncaught, this raised a raw traceback for the WHOLE step
            # instead of the CLI's JSON error contract (adversarial-review
            # finding) — and silently treating a timeout as "clean" would
            # be exactly the wrong default for a security gate. Surfaced
            # as the WORST severity instead: forces human review rather
            # than either crashing every other repo's scan or hiding an
            # unknown result behind a clean verdict.
            return (name, order[-1],
                    f"## {name}\n\ncommand: `{cmd}` **timed out after 900s** — "
                    f"treated as {order[-1]} pending investigation, not clean.\n")
        output = (proc.stdout + "\n" + proc.stderr).strip()
        found = re.findall(regex, output)
        sev = max((s.lower() for s in found), key=order.index,
                 default=order[0]) if found else order[0]
        return (name, sev, f"## {name}\n\ncommand: `{cmd}` (exit {proc.returncode})\n"
                           f"severity: **{sev}**\n\n```\n{output[-4000:]}\n```\n")

    repos = sorted((config.get("repos") or {}).items())
    if repos:
        with ThreadPoolExecutor(max_workers=len(repos)) as pool:
            results = sorted(pool.map(scan_one, repos))
    else:
        results = []
    max_sev = max((sev for _, sev, _ in results), key=order.index, default=order[0])
    sections = [body for _, _, body in results] or ["No repos registered.\n"]
    body = ("# Security scan\n\n" + "\n".join(sections) +
            f"\n**overall max severity: {max_sev}**\n")
    reports = run / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "security.md").write_text(body, encoding="utf-8")
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        set_artifact(st, manifest, "security-report", "reports/security.md")
        set_artifact(st, manifest, "security.max_severity", max_sev)
        state_mod.save(run, workspace, st)
    return max_sev


# ERE metacharacters to escape when a route fragment's LITERAL segments
# are spliced into a regex. git grep -E is POSIX ERE — `-P` (PCRE) is not
# guaranteed compiled in, so nothing below may use PCRE-only constructs.
# Braces are handled separately in _ere_escape: a bare `\{` is not
# portably a literal brace in ERE, so literal braces match via single-char
# bracket classes (`[{]` / `[}]`).
_ERE_META = re.compile(r"[.^$*+?()\[\]|\\]")
# One elided route parameter: a still-braced segment (`{id}`, or a
# consumer's inline interpolation like `{Uri.EscapeDataString(userId)}`)
# OR a bare interpolated value — anything up to the next path separator,
# closing brace, or string quote (quotes bound the match to one string
# literal so it can't bleed across unrelated code). Plain capturing
# group: ERE has no non-capturing `(?:…)`; under `grep -q` the group is
# harmless.
_HTTP_PARAM_SEG = "([{][^/}\"']*[}]|[^/}\"']+)"
# A {param} token: brace-delimited, no separator and no QUOTE inside — the
# same charset _HTTP_PARAM_SEG's braced alternative accepts, so a token the
# split captures is always one the elided segment can re-match (adversarial-
# review: the old split admitted quote-bearing tokens like `{"ok":true}`
# that the seg then couldn't match — a JSON shape under an http contract
# must stay a literal fragment, not become a route param).
_HTTP_TOKEN_RE = re.compile(r"(\{[^/}\"']+\})")


def _ere_escape(seg: str) -> str:
    return (_ERE_META.sub(lambda m: "\\" + m.group(0), seg)
            .replace("{", "[{]").replace("}", "[}]"))


def _http_route_regex(frag: str) -> str | None:
    """POSIX ERE matching `frag` with every `{param}` elided to one path
    segment — or None when the caller must keep the exact literal `-F`
    match (no `{param}` token, or a degenerate all-param template). Field
    run 459226 (downstream fork report): a directional http contract
    declared the producer's route template (`{id}/authorization`); the
    consumer built the same path by interpolating a differently-named
    variable, so the literal `{id}` never appeared there and a
    byte-identical wire shape was reported as drift — both pre-PR
    reviewers independently investigated and dismissed the same tooling
    false positive. Only the param may vary: the literal route text
    around it is ERE-escaped, so `/authz` vs `/authorization` still
    drifts and a route `.` matches only a literal dot (`v2.1` never
    matches `v2x1`).

    Residual (documented): a `{param}` position is satisfied by ANY one
    path segment, so `{id}/items` also matches prose like `docs/items` in
    a non-test file — prefer templates carrying a literal segment before
    the first param (`users/{id}/items`), which bounds the widening; the
    fully anchorless shape is rejected at declaration below."""
    parts = _HTTP_TOKEN_RE.split(frag)
    if len(parts) == 1:
        return None
    # Degenerate template guard: no alphanumeric literal outside the
    # {param} tokens (`{id}`, `{a}/{b}`) → the built ERE would have no
    # anchor and match nearly any line — a guaranteed false CLEAN, the one
    # forbidden direction (this checker may only ever fail toward visible
    # drift). Keep the literal -F match instead; _validate_contract
    # rejects the shape at declaration, this is belt-and-braces for
    # contracts registered before that rule existed.
    if not any(ch.isalnum() for p in parts[::2] for ch in p):
        return None
    # Captured tokens sit at ODD indices by re.split construction —
    # classify by parity, never by re-testing the shape (adversarial-
    # review: a brace-wrapped LITERAL part like `{a/b}` — never captured,
    # it carries a separator — shape-matched and was wrongly elided).
    return "".join(_HTTP_PARAM_SEG if i % 2 else _ere_escape(p)
                   for i, p in enumerate(parts))


def reconcile_contracts(workspace: Path, run: Path, config: dict,
                        repos: dict[str, str]) -> str:
    """Cross-repo contract check (M5 charter / coverage B6): every declared
    signature (or, for a multi-fragment signature, every fragment) must
    appear in every repo the contract names — either its flat `repos` list
    or, when declared directionally, `producer` + `consumers`. Test paths
    (the same `language.test_paths` convention verify-red reads) are
    excluded to cut false positives from a signature merely mentioned in a
    test — known residual: a repo whose ONLY correct representation of a
    signature is a consumer-driven contract test would false-negative here;
    accepted trade-off, same class as RC4's shared-fixture residual. Drift
    is REPORTED for the human at ⟨approve-pre-pr⟩ — never auto-fixed.
    Fragments are validated grep-able at declaration (`_validate_contract`
    rejects prose — validation-walk F3), closing the common false-positive
    cheaply. For `type: http` contracts only, a fragment carrying a
    `{param}` token matches route-STRUCTURALLY (each param elides to one
    path segment — `_http_route_regex`; field 459226: producer `{id}` vs
    consumer `userId` false-drifted a byte-identical route); every other
    fragment is an exact literal match. True semantic/AST comparison
    remains a documented future upgrade, not attempted here: it would need
    structured fragments (the symbol + its kind, not a free string) plus a
    per-language matcher — stdlib `ast` for Python, a parser or heuristic
    for JS, tree-sitter for universal coverage — which trades the
    language-agnostic, near-zero-dependency stance `git grep` was chosen
    for; hence deferred (the http-param elision is the cheap, targeted
    slice of it that keeps that stance)."""
    st = state_mod.load(run, workspace)
    test_globs = config.get("language", {}).get("test_paths", ["tests/**"])
    # `glob` pathspec magic, not plain `:(exclude)`: git's non-glob pathspec
    # interpretation of a `**/`-prefixed pattern only matches past at least
    # one real directory, silently failing to exclude a root-level file —
    # `gitops._match` already special-cases this same gap for the identical
    # `test_paths` convention; `glob` magic gets the same coverage here.
    excludes = [f":(exclude,glob){g}" for g in test_globs]
    # The published mirror must NOT be in scope (field, session D — agent-
    # diagnosed): every preflighted repo carries the run's own ai/<run>/
    # mirror, whose state.yaml holds the contract declarations verbatim —
    # so fragments were "matching" their own declaration. Two failure
    # modes, both real: prose-annotated fragments that can never appear in
    # source passed as CLEAN (E2E-1's clean verdicts partially vacuous),
    # while PyYAML's ~80-col line-wrapping of longer fragments broke their
    # mirror match and flagged genuinely-implemented code as MISSING. The
    # checker must only ever see real source. Residual (documented): a
    # repo whose own product code lives under a root-level ai/ dir loses
    # contract coverage there — same harness-owns-ai/ convention as
    # publish_mirror.
    excludes.append(":(exclude,glob)ai/**")
    lines, drift = ["# Cross-repo contracts\n"], False
    for c in st.get("contracts", []):
        fragments = c["signature"] if isinstance(c["signature"], list) else [c["signature"]]
        if c.get("producer") and c.get("consumers"):
            repo_names = list(dict.fromkeys([c["producer"], *c["consumers"]]))
        else:
            repo_names = list(dict.fromkeys(c["repos"]))
        if c.get("producer") and c.get("consumers"):
            consumers = list(dict.fromkeys(c["consumers"]))
            role = f" ({c['type']}, " if c.get("type") else " ("
            role += f"{c['producer']} → {', '.join(consumers)})"
        elif c.get("type"):
            role = f" ({c['type']})"
        else:
            role = ""
        is_http = c.get("type") == "http"
        for repo_name in repo_names:
            repo = Path(repos.get(repo_name, repo_name))
            missing = []
            for frag in fragments:
                # http fragments carrying a {param} token match
                # route-structurally via -E (each param elides to one path
                # segment — _http_route_regex); everything else stays an
                # exact literal -F match. A malformed ERE exits non-zero
                # and reads as MISSING — the safe direction: false drift
                # for the human to dismiss, never false clean.
                rx = _http_route_regex(frag) if is_http else None
                try:
                    if rx is not None:
                        gitops.run_git(repo, "grep", "-E", "-q", rx,
                                       "--", ".", *excludes)
                    else:
                        gitops.run_git(repo, "grep", "-F", "-q", frag,
                                       "--", ".", *excludes)
                except gitops.GitError:
                    missing.append(frag)
            if missing:
                lines.append(f"- {c['id']}{role} @ {repo_name}: **MISSING** ("
                             + ", ".join(f"`{m}`" for m in missing) + ")")
                drift = True
            else:
                lines.append(f"- {c['id']}{role} @ {repo_name}: present")
    verdict = "drift" if drift else "clean"
    lines.append(f"\nverdict: **{verdict}**\n")
    reports = run / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "contracts.md").write_text("\n".join(lines), encoding="utf-8")
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "contracts-check", "verdict": verdict,
                          "actor": "reconcile-contracts"})
    return verdict


def create_pr(workspace: Path, run: Path, config: dict, manifest: dict,
              repo: Path, manual_url: str | None = None) -> dict:
    """Create the PR via the configured git provider. M5 ships `local`
    (a records-only provider so the pipeline completes without a forge);
    real forges arrive in M6 through the same seam. One PR per repo in
    multi-repo runs (create-pr.md) — the `pr` artifact is keyed by the
    repo's registered name, same shape as `branches`, so a second repo's
    call never overwrites the first's record.

    `manual_url` is the provider-outage escape hatch (field: a reverse
    proxy in front of a self-hosted GitLab 404'd every path-encoded
    project lookup, so `glab mr create` couldn't resolve the project while
    pushes and numeric-ID reads worked fine — the human created the MR by
    hand, and the run still needs its artifact). Recording stays an owned
    entry point: same lock, ensure_live, per-repo keying — plus a distinct
    event kind so the audit trail shows no provider call was made."""
    from . import initws
    from .providers.git_providers import create_pr as git_create_pr
    from .transitions import ensure_live
    name = initws.repo_name(config, repo) or str(repo)
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "create-pr")
        title = gitops.render(config["naming"]["pr_title"],
                              type=st["change_type"], id=st["work_item"]["id"],
                              summary=st["work_item"]["title"])
        if manual_url:
            pr_id = manual_url.rstrip("/").rsplit("/", 1)[-1]
            if not pr_id.isdigit():
                # fetch-pr-comments derives the PR/MR id from the URL tail
                # (git_providers._pr_number) — a URL that doesn't end in the
                # number breaks the comment loop later; refuse loudly NOW.
                raise gitops.GitError(
                    "manual PR record needs the PR/MR's own URL, ending in "
                    f"its number (…/merge_requests/7, …/pull/7) — got "
                    f"'{manual_url}'")
            pr = {"id": pr_id, "url": manual_url.rstrip("/"), "title": title,
                  "manual": True}
        else:
            branch = gitops.run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            # The real default branch was already resolved once per repo at
            # preflight (gitops.ensure_default_branch) and persisted there —
            # reuse it instead of a hardcoded 'main' fallback that's wrong
            # for any repo whose default branch is something else.
            recorded = ((st.get("artifacts") or {}).get("branches") or {}).get(name) or {}
            base = recorded.get("base")
            if not base:
                # Fail closed, not guess: every mode's sequence runs
                # preflight (which records the per-repo resolved base)
                # before create-pr — a missing record means this repo was
                # never preflighted under this run, and a guessed 'main'
                # would silently target the wrong base on any repo whose
                # default branch differs (adversarial-review finding: the
                # fallback guess reintroduced the exact bug the per-repo
                # record exists to fix).
                raise gitops.GitError(
                    f"no recorded base branch for repo '{name}' in this run — "
                    "run `harness preflight --repo <path>` for it first")
            pr = git_create_pr(config, repo=repo, branch=branch, base=base,
                               title=title, work_item_id=st["work_item"]["id"],
                               summary=st["work_item"]["title"])
        prs = dict((st.get("artifacts") or {}).get("pr") or {})
        prs[name] = pr
        set_artifact(st, manifest, "pr", prs)
        state_mod.save(run, workspace, st)
    ndjson.append_record(run / "events.ndjson", {
        "kind": "pr-recorded-manually" if manual_url else "pr-created",
        "title": title, "repo": name, "actor": "create-pr",
        **({"url": manual_url} if manual_url else {})})
    return pr


#: milestone name -> (write_back config flag, STATUS_DEFAULTS/status_mapping key)
WRITE_BACK_MILESTONES = {
    "develop_start": ("on_develop_start", "in-progress"),
    "in_review": ("on_in_review", "in-review"),
    "done": ("on_done", "done"),
}
_MILESTONE_FALLBACK = {"done": "Done"}  # preserves the original done-only behavior


def resolve_write_back_status(config: dict, milestone: str,
                              item_type: str | None) -> str | None:
    """Which provider status (if any) to write back at a pipeline milestone
    (design.md piece 4). Returns None when that milestone's `write_back`
    flag is off — adversarial-review finding: `on_develop_start`/
    `on_in_review` were declared, defaulted `true`, and documented, but only
    `on_done` was ever consulted anywhere. Per-work-item-type
    `status_mapping` (also declared/documented, e.g. `Incident: {done:
    Mitigated}`) resolves by `item_type`, falling back to `default` — the
    original only ever read the `default` key regardless of the item's
    actual type."""
    flag, key = WRITE_BACK_MILESTONES[milestone]
    if not (config.get("write_back") or {}).get(flag, True):
        return None
    from .providers import get_module
    provider_defaults = getattr(get_module(config), "STATUS_DEFAULTS", {})
    mapping = config.get("status_mapping") or {}
    override = (mapping.get(item_type) if item_type else None) or mapping.get("default", {})
    return {**provider_defaults, **override}.get(key, _MILESTONE_FALLBACK.get(milestone))


def _best_effort_transition(run: Path, config: dict, item_id: str, target: str,
                            actor: str) -> dict:
    """Push one provider status, best-effort: a `ProviderError` is recorded as
    a flagged `write-back-failed` and reported, never raised.

    field: US-CHAT-00 run. Both milestone write-back call sites (write_back
    and reconcile_flow) dispatched the transition bare, so a provider refusal
    propagated out of a step whose own contract calls it "never a blocking
    requirement" — a run whose story file carried a slug-suffixed filename
    aborted `develop` at its very first verb. The resolution bug behind that
    specific case is fixed in the local-markdown provider, but the contract
    gap is separate and outlives it: ANY provider refusal here (a tracker
    down, a credential expired, a status name the tracker rejects) should
    leave the run walking and the staleness visible, not halt it.

    ONE definition for both call sites — the same anti-drift rule
    FLAGGED_EVENT_KINDS and outstanding_flagged state above; a swallow that
    logged on one path and not the other would make the gauge lie about which
    milestones actually landed. Only `ProviderError` is caught: it is the
    declared "the provider said no" channel. A bug in our own dispatch layer
    still raises, because that is not a best-effort condition.

    MCP transport is deliberately NOT best-effort and propagates: `dispatch`
    raises there before reaching any provider, and that refusal is the
    MECHANISM telling the orchestrator to invoke the mapped tool itself (then
    pass `reconcile --skip-transition`). Demoting it to a flagged event would
    let a run reconcile with its tracker never synced and nothing forcing the
    question — the trust-the-prose shape this codebase refuses everywhere.
    `write_back` short-circuits MCP earlier with its own guidance return, so
    only `reconcile_flow` reaches this branch."""
    from .providers import (ProviderError, ProviderUnsupported, dispatch,
                            get_module)
    # resolved OUTSIDE the try: an unknown provider name is a config error,
    # not "the provider said no", and must keep raising like it always has
    mcp = getattr(get_module(config), "TRANSPORT", "") == "mcp"
    try:
        dispatch(config, "work_item.transition", id=item_id, to=target)
    except ProviderUnsupported:
        # `ProviderUnsupported` subclasses ProviderError, so the bare catch
        # below swallowed it too — turning a provider that DECLARES no
        # transition support into a flagged event on every milestone of every
        # run. Declared-unsupported is a statement about the PROVIDER, not a
        # runtime "the tracker said no", and flagging it would report the same
        # non-news on every run forever (adversarial-review, lens B).
        #
        # Honest limit (re-verify finding): no config-time check refuses this
        # earlier — nothing outside providers/ reads `SUPPORTS`, and
        # init-verify does not probe it. All seven shipped providers implement
        # work_item.transition, so this is unreachable on stock config; a FORK
        # provider that omits it would abort develop's first verb and
        # reconcile post-merge. Raising is still right — silently flagging a
        # permanent capability gap is worse — but it is a refusal at first
        # use, not at configuration time.
        raise
    except ProviderError as exc:
        if mcp:
            raise
        try:
            ndjson.append_record(
                run / "events.ndjson",
                {"kind": "write-back-failed", "item": item_id, "to": target,
                 # `reason` is the key metrics' _detail() renders — named
                 # `error` in the first cut, the flagged row came out blank,
                 # so the surfacing this whole path exists for showed a human
                 # only the kind (adversarial-review, both lenses)
                 "reason": f"{target!r}: {exc}", "actor": actor})
        except OSError:
            # a full or read-only run dir must not convert a suppressed
            # ProviderError into a DIFFERENT raised exception — the contract
            # is "never raises", unqualified (adversarial-review, lens B)
            pass
        return {"written": False, "to": target, "error": str(exc)}
    # Emitted only when there is an OUTSTANDING miss to supersede: the
    # resolver above needs a marker, but a success marker on every clean run
    # would be three ledger lines per run recording that nothing happened.
    # Gating on "a miss appears anywhere in the ledger" is not the same test —
    # it stays true forever once one has been resolved, so three clean
    # write-backs after one miss appended three markers, only the first
    # superseding anything. `_has_open_env_miss` exists because this exact
    # bug was found for env-prereq-satisfied; same lesson, same shape
    # (re-verify finding).
    #
    # Wrapped like the failure branch above, and for the same reason: the
    # contract is "never raises", unqualified, and this branch is reached
    # post-merge from reconcile. Leaving the success-path ledger I/O bare
    # meant a full or read-only run dir raised out of a call that had already
    # succeeded (re-verify finding).
    try:
        if any(e.get("kind") == "write-back-failed"
               for e in outstanding_flagged(
                   ndjson.read_records(run / "events.ndjson"))):
            ndjson.append_record(run / "events.ndjson",
                                 {"kind": "write-back-succeeded",
                                  "item": item_id, "to": target,
                                  "actor": actor})
    except OSError:
        pass
    return {"written": True, "to": target}


def write_back(workspace: Path, run: Path, config: dict, milestone: str) -> dict:
    """`harness write-back --milestone <develop_start|in_review|done>` —
    the orchestrator-owned call for the two milestones that used to have NO
    call site at all (develop_start, in_review; `done` still also fires
    from `reconcile`). No-ops cleanly, never raises, when the milestone's
    flag is off or no target status resolves.

    MCP-transport carve-out (adversarial-review round 2 finding): unlike
    `reconcile_flow`, this is called UNCONDITIONALLY at the very start of
    `develop` (write_back.on_develop_start defaults true) with no prior
    orchestrator step that could have already handled an MCP-transport
    provider's transition itself — `dispatch()` always raises for MCP
    transport by construction, so without this check every MCP-transport
    work item would fail at the first step of every full-mode run. Detects
    transport directly (same check `dispatch()` makes internally) and
    returns guidance instead of raising, mirroring fetch.md's pattern: the
    orchestrator invokes the mapped tool itself if it cares about live
    status sync; write-back is best-effort, never a blocking requirement.

    That best-effort promise is enforced, not just stated: a provider that
    refuses the transition is recorded as a flagged `write-back-failed` and
    reported in the result, never raised (see `_best_effort_transition`)."""
    from .transitions import ensure_live
    with state_mod.locked_read(run):  # torn-read guard, same as show/verify
        st = state_mod.load(run, workspace)
    ensure_live(st, "write-back")  # never push a live tracker status for a dead run
    target = resolve_write_back_status(config, milestone, st["work_item"].get("type"))
    if target is None:
        return {"written": False}
    from .providers import get_module
    if getattr(get_module(config), "TRANSPORT", "") == "mcp":
        return {"written": False, "mcp_target": target,
               "mcp_guidance": f"MCP-transport provider — a script can't call "
                               f"an MCP tool; invoke the mapped work_item."
                               f"transition tool yourself if you want live "
                               f"status sync (to={target!r})."}
    return _best_effort_transition(run, config, st["work_item"]["id"], target,
                                   "write-back")


def reconcile_flow(workspace: Path, run: Path, config: dict, fsm: dict,
                   manifest: dict | None = None,
                   skip_transition: bool = False) -> dict:
    """Post-merge reconciliation: provider status write-back (conservative
    default), archive done tasks, sweep worktrees.

    `skip_transition` (reconcile.md's MCP-transport carve-out, mirroring
    fetch.md's existing one): MCP-transport work-item providers can't be
    script-dispatched at all — `dispatch()` always raises for them, so
    without this flag `harness reconcile` refused with every write_back
    default on. The orchestrator invokes the mapped MCP tool itself first,
    then passes this flag so archiving/worktree-sweep still run normally."""
    from . import chain as chain_mod
    from .transitions import transition_task
    key = chain_mod.load_key(workspace)  # strict: never mint from a drifted cwd
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        from .transitions import ensure_live
        ensure_live(st, "reconcile")
        for task in st["tasks"]:
            if task.get("worktree"):
                gitops.worktree_remove(Path(task["repo"]), task["worktree"])
                task["worktree"] = None
            if task["status"] == "done":
                transition_task(st, fsm, config, run, key, task["id"], "archived")
        if manifest is not None and st["cursor"]["current_step"] == "reconcile":
            # the step's one declared output, recorded by the owner
            # (adversarial-review finding: `produces: [reconciled]` was
            # declared and recorded by nothing)
            set_artifact(st, manifest, "reconciled", True)
        state_mod.save(run, workspace, st)
    written: dict | None = None
    if not skip_transition:
        target = resolve_write_back_status(config, "done", st["work_item"].get("type"))
        if target is not None:
            # best-effort, same contract as write_back's — reconcile runs
            # POST-merge, so raising here would fail a run whose work is
            # already landed and leave its worktrees swept but its ledger
            # unreconciled (field: US-CHAT-00 run). The result is REPORTED,
            # not dropped: `harness reconcile` returning a bare
            # {"reconciled": true} made a refused transition indistinguishable
            # from a clean sync at the one decision point where the
            # orchestrator reads it, leaving the staleness discoverable only
            # by separately running `status` (adversarial-review, both lenses)
            written = _best_effort_transition(run, config,
                                              st["work_item"]["id"], target,
                                              "reconcile")
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "reconciled", "actor": "reconcile"})
    # only present when a transition was actually attempted — an unchanged
    # shape for the flag-off and --skip-transition paths
    return ({"reconciled": True} if written is None
            else {"reconciled": True, "write_back": written})


def abort_run(workspace: Path, run: Path, reason: str) -> dict:
    """`harness abort` — the declared way to END a run before its terminal
    step (previously promised by every "offer Resume or Abort" message and
    implemented nowhere). Marks the run aborted (terminal: releases the
    work-item slot for a fresh bootstrap, stops legalizing spawns), sweeps
    task worktrees, and logs the reason. The run directory and its ledgers
    stay — an abort is an audit event, never a deletion."""
    from .transitions import ensure_live
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "abort")  # aborting twice would clobber the record
        for task in st["tasks"]:
            if task.get("worktree"):
                gitops.worktree_remove(Path(task["repo"]), task["worktree"])
                task["worktree"] = None
        st["aborted"] = {"at": ndjson.now_iso(), "reason": reason}
        state_mod.save(run, workspace, st)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "aborted", "reason": reason, "actor": "abort"})
    return {"aborted": True, "reason": reason}


def complete_run(workspace: Path, run: Path, manifest: dict) -> dict:
    """`harness complete` — the declared way to END a run that finished its
    walk (the successful sibling of `harness abort`). Field (e2e E2E-1): a
    run that exhausted its sequence parked at the final step as "live"
    forever — the final step never got an `ended_at`, `status` listed the
    run indefinitely, and "finished successfully" had no first-class
    representation anywhere (state._terminal's cursor-at-last-step
    heuristic covered the collision check only, and would even treat a run
    still mid-final-step as terminal). Refuses unless the cursor sits ON
    the mode's final step with every task terminal; stamps the final
    step's `ended_at`, appends it to completed_steps, and marks the run
    completed (terminal: mutations refuse via ensure_live, spawns stop
    being legalized, the work-item slot is released). The run directory
    and its ledgers stay — completion is an audit event, never a
    deletion."""
    from .transitions import TransitionError, ensure_live
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        ensure_live(st, "complete")  # completing twice would clobber the record
        seq = manifest["modes"][st["mode"]]
        current = st["cursor"]["current_step"]
        if current != seq[-1]:
            raise TransitionError(
                f"complete is legal only from the mode's final step "
                f"('{seq[-1]}') — cursor is at '{current}'; walk the manifest "
                "to the end first, or `harness abort` to end the run early")
        not_terminal = [t["id"] for t in st["tasks"]
                        if t.get("status") not in ("done", "archived")]
        if not_terminal:
            raise TransitionError(
                f"complete refused: task(s) {', '.join(not_terminal)} are not "
                "terminal — a finished run has no live tasks")
        now = ndjson.now_iso()
        st["cursor"]["completed_steps"].append(current)
        st["metrics"].setdefault(current, {})["ended_at"] = now
        st["completed"] = {"at": now}
        state_mod.save(run, workspace, st)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "completed", "actor": "complete"})
    return {"completed": True}


def _md_cell(v) -> str:
    """One GFM table cell: newline-flattened (a hook-blocked reason is a
    whole paragraph), pipe-escaped so a reason can't break the row, None
    rendered as an em-dash."""
    if v is None:
        return "—"
    return " ".join(str(v).split()).replace("|", "\\|")


def _md_table(headers: list, rows: list) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |"
              for row in rows]
    return lines


def _fmt_when(iso: str | None) -> str:
    """'2026-07-06T13:29:37.451185+00:00' → '2026-07-06 13:29:37'."""
    return iso[:19].replace("T", " ") if iso else "—"


def _fmt_duration(start: str | None, end: str | None) -> str:
    if not start:
        return "—"
    if not end:
        return "running"
    try:
        delta = _dt.datetime.fromisoformat(end) - _dt.datetime.fromisoformat(start)
    except ValueError:
        return "—"
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def metrics_report(workspace: Path, run: Path,
                   manifest: dict | None = None) -> Path:
    """Deterministic aggregation rendered as human-readable tables: run
    health (events + stall counters) leading, timings from state, tokens
    from the ledger (aggregated per task × role),
    verdicts from reviews.ndjson, exceptions from events — no agent
    reasoning (a 'keeping'). The ndjson ledgers stay the machine-readable
    source of truth; this file is a regenerable VIEW, never parsed back and
    never hand-edited. Runnable at any live step — mid-run it's a
    dashboard, at the metrics step it's the closing artifact (and it rides
    publish-mirror into each repo's feature branch either way)."""
    from .transitions import ensure_live
    with state_mod.locked_read(run):  # torn-read guard
        st = state_mod.load(run, workspace)
    ensure_live(st, "metrics")
    tokens = ndjson.read_records(run / "tokens.ndjson")
    events = ndjson.read_records(run / "events.ndjson")
    reviews = ndjson.read_records(run / "reviews.ndjson")
    lines = [f"# Metrics — {st['work_item']['id']}", "",
             f"{st['work_item'].get('title') or ''} · mode `{st['mode']}` · "
             f"cursor `{st['cursor']['current_step']}` · generated "
             f"{_fmt_when(ndjson.now_iso())} UTC", ""]
    # Run health leads the report — the executive signal (field 459226: a
    # run "completed green" over 11 flagged events and 2 stalls, visible
    # only via manual post-mortem). The verdict flips on the declared
    # HEALTH_DEGRADING_KINDS or an engaged stall procedure; the
    # non-degrading malformed-block count rides as context only.
    stalls = stall_count(st)
    health, degrading = run_health(events, stalls)
    malformed = sum(1 for e in events
                    if e.get("kind") == "status-block-malformed")
    parts = [f"{k}: {n}" for k, n in sorted(degrading.items())]
    if stalls:
        parts.append(f"stalls: {stalls}")
    if malformed:
        parts.append(f"status-block-malformed (non-degrading): {malformed}")
    lines += ["## Run health", "",
              f"**{health}**" + (" — " + " · ".join(parts) if parts
                                 else " — no degrading events, no stalls"),
              "", "## Step timings", ""]
    lines += _md_table(
        ["Step", "Started (UTC)", "Ended (UTC)", "Duration"],
        [[step, _fmt_when(m.get("started_at")), _fmt_when(m.get("ended_at")),
          _fmt_duration(m.get("started_at"), m.get("ended_at"))]
         for step, m in st.get("metrics", {}).items()])
    lines += ["", "## Tasks", ""]
    lines += _md_table(
        ["Task", "Repo", "Status", "Risk", "Review rounds", "Stalls", "Commit"],
        [[t["id"], Path(t.get("repo") or ".").name, t["status"],
          t.get("risk"), t["review_rounds"], t["stalls"],
          (t.get("commit_sha") or "")[:9] or None] for t in st["tasks"]])
    lines += ["", "## Review verdicts", ""]
    lines += (_md_table(["Task", "Mode", "Verdict", "At (UTC)"],
                        [[r.get("task"), r.get("mode"), r.get("verdict"),
                          _fmt_when(r.get("at"))] for r in reviews])
              if reviews else ["No review verdicts recorded."])
    # Convergence: is the panel actually closing findings, or spinning?
    # field: dual-run comparison — a run's verdict rows carried only
    # mode/verdict/at, so nothing machine-readable answered that. Its own
    # retro then misstated the final round, and the human gate at
    # `exhausted` had no one-glance framing that the trajectory had been
    # 9 → 7 → 2 → 2 (converging) rather than flat (stuck). Rendered only
    # when at least one reviewer supplied the optional count — an empty
    # table would imply the data was collected and was zero.
    conv = [r for r in reviews if r.get("blocking_findings") is not None]
    if conv:
        lines += ["", "## Review convergence", ""]
        by_mode: dict[str, list[dict]] = {}
        for r in conv:
            by_mode.setdefault(str(r.get("mode")), []).append(r)
        rows = []
        for mode_name, rs in by_mode.items():
            for i, r in enumerate(rs, 1):
                rows.append([mode_name, i, r.get("plan_generation"),
                             r.get("verdict"), r.get("blocking_findings"),
                             _fmt_when(r.get("at"))])
        lines += _md_table(["Mode", "Round", "Plan gen", "Verdict",
                            "Blocking", "At (UTC)"], rows)
    lines += ["", "## Tokens", ""]
    if tokens:
        agg: dict[tuple, dict] = {}
        counts = ("calls", "input", "output", "cache_read", "cache_write")
        for r in tokens:
            key = (r.get("task"), r.get("role"), r.get("model"))
            a = agg.setdefault(key, dict.fromkeys(counts, 0))
            a["calls"] += 1
            for k in counts[1:]:
                a[k] += int(r.get(k) or 0)
        rows = [[t, role, model, *(f"{a[k]:,}" for k in counts)]
                for (t, role, model), a in agg.items()]
        rows.append(["**Total**", "", "",
                     *(f"{sum(a[k] for a in agg.values()):,}" for k in counts)])
        lines += _md_table(["Task", "Role", "Model", "Calls", "Input",
                            "Output", "Cache read", "Cache write"], rows)
    else:
        lines.append("No subagent invocations recorded.")
    flagged = outstanding_flagged(events)
    lines += ["", f"## Flagged events ({len(flagged)})", ""]
    def _detail(e: dict) -> str:
        d = e.get("reason") or e.get("verdict") or ""
        return d[:200] + "…" if len(d) > 200 else d
    lines += (_md_table(["At (UTC)", "Kind", "Task", "Detail"],
                        [[_fmt_when(e.get("at")), e.get("kind"),
                          e.get("task"), _detail(e)] for e in flagged])
              if flagged else ["None — a clean walk."])
    reports = run / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / "metrics.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if manifest is not None:
        with state_mod.locked(run):
            st = state_mod.load(run, workspace)
            if st["cursor"]["current_step"] == "metrics":
                # owned step, owned artifact record (same fix as reconcile)
                set_artifact(st, manifest, "metrics-report",
                             "reports/metrics.md")
                state_mod.save(run, workspace, st)
    return path


def _render_feature_branch(config: dict, st: dict,
                           suffix: str | None = None) -> str:
    """The run's feature-branch name from the declared `naming.branch`
    template, plus preflight's optional branch-aside suffix. One renderer,
    so the name the remote probe checks is byte-identical to the one
    actually cut (field: dual-run comparison)."""
    branch = gitops.render(config["naming"]["branch"],
                           type=st["change_type"],
                           id=st["work_item"]["id"],
                           slug=slug(st["work_item"]["title"]))
    if suffix:
        # slug()'d, not pasted raw: this value comes from a CLI flag and
        # ends up in a ref name, where spaces/`~^:?*[` are illegal
        return f"{branch}-{slug(suffix)}"
    return branch


def _probe_prior_remote_branch(run: Path, repo: Path, name: str,
                               branch: str) -> None:
    """Refuse a preflight whose deterministic branch name is already taken on
    the remote — the collision Run B discovered hours later at push, with an
    open MR and five tasks of committed work already on the name.

    Surface, never auto-fix (ensure_default_branch's stance): three remedies,
    no silent continuation and no automatic adoption of a foreign branch. An
    UNANSWERABLE probe (offline, no remote, auth) degrades to a flagged
    warning — a connectivity blip must not brick preflight, but it must not
    pass unrecorded either."""
    hit = gitops.remote_branch_exists(repo, branch)
    if hit is None:
        # An unanswered probe has two very different causes, and conflating
        # them put one permanent unresolvable flag on every preflight in
        # every local-only workspace (adversarial-review). A repo with no
        # remote — or an ambiguous remote set — is a STRUCTURAL state, not a
        # run-health signal: there is no collision to have, so skip silently.
        # A remote that resolved but would not answer (offline, auth,
        # timeout) is transient and genuinely worth a human's eye.
        try:
            gitops._push_remote(repo)
        except (gitops.GitError, OSError):
            return
        # Once per run per (repo, branch), like tests-quarantined
        # (pre-release review: the probe runs BEFORE the clean/default-branch
        # checks, so a probe-failed repo that is also dirty appended one
        # permanent flag per refusal-and-retry — drowning the gauge with
        # copies of one fact).
        prior = ndjson.read_records(run / "events.ndjson")
        if not any(e.get("kind") == "remote-branch-unverified"
                   and e.get("repo") == name and e.get("branch") == branch
                   for e in prior):
            ndjson.append_record(run / "events.ndjson",
                                 {"kind": "remote-branch-unverified",
                                  "repo": name, "branch": branch,
                                  "reason": "probe-failed",
                                  "actor": "preflight"})
        return
    if not hit:
        return
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "remote-branch-exists", "repo": name,
                          "branch": branch, "actor": "preflight"})
    raise state_mod.StateError(
        f"{name}: branch '{branch}' already exists on the remote — a prior "
        "run of this work item already claimed the deterministic branch "
        "name, and continuing would collide at push (possibly on top of an "
        "already-open PR/MR). Refusing to guess. Either: (1) resume that "
        "work — fetch and reconcile the remote branch by hand, then re-run "
        "preflight; (2) branch aside — re-run preflight with "
        "--feature-branch-suffix <s> to cut a distinct name; or (3) delete "
        "the remote branch if it is abandoned. Never auto-adopted.")


#: report mode -> canonical basename under `<run>/reports/`. `{lens}` is
#: filled from --lens; every other mode ignores it.
REPORT_BASENAMES = {
    "plan-attack": "plan-attack-{lens}",
    "plan-review": "plan-review",
    "pre-pr": "pre-pr",
}
#: report mode -> the pipeline step whose stalls reopen its round snapshot
#: (a lens stall is keyed `step:plan-review:<lens>`, so prefix-matching on
#: the step covers both the synthesizer and its panel members).
REPORT_STEPS = {
    "plan-attack": "plan-review",
    "plan-review": "plan-review",
    "pre-pr": "pre-pr",
}


def save_report(run: Path, mode: str, body: str, lens: str | None = None,
                round_n: int | None = None) -> dict:
    """`harness save-report` — the owned way a read-only reviewer's report
    reaches disk.

    field: dual-run comparison — lens reports for two whole rounds do not
    exist on disk in one of the runs. Read-only lens agents returned their
    reports in-reply and the orchestrator hand-copied them roughly three
    times before the practice lapsed; persistence was PROSE ("the
    orchestrator persists it"), which is exactly the trust-the-prose shape
    the harness refuses everywhere else. The other run invented the
    `-r1/-r2/-r3` round-suffix convention ad hoc, mid-run.

    Writes the live path (what the gate reads) AND that round's immutable
    snapshot in one call — so the "snapshot the old one aside first" step the
    step files used to spell out can no longer be skipped or done
    inconsistently. The round is derived from the run's own ledger unless
    overridden — PER MODE, because different review loops advance on
    different events (pre-release adversarial review: plan-review rounds
    open on a plan re-registration, while pre-pr rounds open on an
    approve-pre-pr rejection, and anchoring both to the plan generation made
    every second pre-pr save refuse with a plan-review-flavored message).
    Re-writing an existing snapshot with different content refuses UNLESS a
    recorded stall for the mode's step postdates the last save — a stall
    means the spawn was re-invoked and this round's report is legitimately
    superseded (the same "a stall opens a new attempt" semantics
    _stall_round_anchor holds). Emits `report-saved`, so the event implies
    the file exists rather than merely asserting that a spawn finished."""
    if not state_mod.state_path(run).exists():
        # The one run-scoped verb that used to skip this check: a typo'd or
        # stale-pasted --run manufactured the whole phantom directory via
        # mkdir(parents=True) and reported success, while the real run's
        # gate later presented a missing report — the exact failure this
        # verb exists to close (pre-release adversarial review).
        raise state_mod.StateError(
            f"{run} is not a run (no state.yaml) — check --run; refusing to "
            "manufacture a phantom run directory")
    if mode not in REPORT_BASENAMES:
        raise state_mod.StateError(
            f"unknown report mode '{mode}' — one of: "
            f"{', '.join(sorted(REPORT_BASENAMES))}")
    template = REPORT_BASENAMES[mode]
    if "{lens}" in template:
        if not (lens or "").strip():
            raise state_mod.StateError(
                f"--mode {mode} needs --lens (the panel member's name) — it "
                "names the report file, and two lenses sharing one path "
                "would silently overwrite each other")
        # the value lands in a FILENAME: refuse anything that could escape
        # the reports directory or collide with the round-suffix convention.
        # Normalized ONCE, here, and the normalized form is what the event
        # records and the reopen comparison matches (re-verify: recording
        # the raw spelling let case-inconsistent --lens values miss their
        # own predecessor in the last-save lookup).
        lens = lens.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", lens):
            raise state_mod.StateError(
                f"--lens {lens!r} must be a lowercase slug ([a-z0-9-]) — it "
                "is used as a path component")
        template = template.format(lens=lens)
    if not body.strip():
        raise state_mod.StateError(
            "refusing to save an empty report — a zero-byte file at the "
            "canonical path is indistinguishable from a persisted one")
    if round_n is not None and round_n < 1:
        raise state_mod.StateError("--round must be >= 1")
    events = ndjson.read_records(run / "events.ndjson")
    if round_n is None:
        # DERIVED, not hand-tracked (re-verify finding: an optional,
        # orchestrator-supplied round number is skippable and mis-typeable,
        # so "prior rounds stay recoverable" was still a promise resting on
        # prose) — and derived PER MODE, from the event that actually opens
        # that mode's next round. Actor-checked for plan-registered, the
        # same anti-forgery stance outstanding_flagged takes: a stray
        # `log-event` record must not move the round.
        if mode == "pre-pr":
            round_n = 1 + sum(
                1 for e in events
                if e.get("kind") == "gate-decision"
                and e.get("gate") == "approve-pre-pr"
                and e.get("decision") != "approved")
        else:
            round_n = max(1, sum(
                1 for e in events
                if e.get("kind") == "plan-registered"
                and e.get("actor") == "plan-register"))
    reports = run / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    snapshot = reports / f"{template}-r{round_n}.md"
    if snapshot.exists() and snapshot.read_text(encoding="utf-8") != body:
        # A recorded stall for this mode's step REOPENS the snapshot: the
        # spawn was re-invoked inside the same round, so the re-invoked
        # reply legitimately supersedes the pre-stall one (pre-release
        # adversarial review, reproduced: without this, the documented
        # stall -> reinvoke -> save flow refused, the live path kept the
        # pre-stall synthesis while reviews.ndjson held the re-invoked
        # verdict — a report/verdict mismatch at the human gate — and the
        # refusal's own advertised remedy could not work).
        # EXACT stall key per report (re-verify on this fix: a prefix match
        # let one early lens stall reopen every sibling lens + the synthesis
        # snapshot for the rest of the round). The synthesizer's stall key is
        # the bare step; a lens's is the declared finer key.
        step = REPORT_STEPS[mode]
        stall_task = f"step:{step}:{lens}" if lens else f"step:{step}"
        last_save = max((e.get("at", "") for e in events
                         if e.get("kind") == "report-saved"
                         and e.get("mode") == mode
                         and e.get("lens") == lens
                         and e.get("round") == round_n), default="")
        # A forged far-future `report-saved` via log-event could pin
        # stalled_since false forever — accepted: the failure direction is
        # OVER-refusal (recoverable at the next round), the same direction
        # every other unvalidated-log-event hazard here resolves to.
        stalled_since = any(
            e.get("kind") == "stall" and e.get("task") == stall_task
            and e.get("at", "") > last_save
            for e in events)
        if not stalled_since:
            raise state_mod.StateError(
                f"reports/{snapshot.name} already exists with different "
                f"content — round {round_n}'s snapshot is immutable. If the "
                "spawn was re-invoked after a stall, record the stall first "
                "(`harness stall`) and re-save; if this is genuinely the "
                "next round, the event that opens it was never recorded "
                "(plan-review rounds open on plan re-registration, pre-pr "
                "rounds on an approve-pre-pr rejection).")
    written = []
    for name in (f"{template}.md", snapshot.name):
        (reports / name).write_text(body, encoding="utf-8")
        written.append(f"reports/{name}")
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "report-saved", "mode": mode, "lens": lens,
                          "round": round_n, "paths": written,
                          "actor": "save-report"})
    return {"mode": mode, "lens": lens, "round": round_n, "paths": written,
            "path": written[0]}


def _has_open_env_miss(run: Path) -> bool:
    """Is the NEWEST env-prereq event a miss? — i.e. is there anything left
    to pair off. Checking `any(miss in history)` instead meant every clean
    run-wide check after the first ever miss appended another satisfied
    event forever (re-verify: ledger noise, not a gauge error — the pairing
    itself was already order-correct)."""
    latest = ""
    open_miss = False
    for e in ndjson.read_records(run / "events.ndjson"):
        if e.get("kind") in ("env-prereq-missing", "env-prereq-satisfied") \
                and e.get("at", "") >= latest:
            latest = e.get("at", "")
            open_miss = e.get("kind") == "env-prereq-missing"
    return open_miss


def env_check(workspace: Path, run: Path, config: dict,
              task_id: str | None = None) -> dict:
    """Probe the environment prerequisites the plan declared, BEFORE the
    developer spawn that would hit the wall.

    field: dual-run comparison — Docker was down in both runs. One never ran
    its Testcontainers integration test at all and shipped that path with
    code-review verification only; the other lost ~1h38m mid-develop
    discovering it, asking, starting Docker, and re-running. The requirement
    was knowable at plan time in both cases; nothing asked.

    Scoped to `--task` when given, else every non-terminal task in the run —
    a done task's requirement is no longer this run's problem. Each distinct
    requirement is probed once. Returns the full picture (probed, present,
    missing) rather than short-circuiting, so one round-trip tells the human
    everything to fix. A missing requirement logs a flagged event and the CLI
    refuses; it never auto-starts anything (surface, never auto-fix)."""
    from .transitions import ensure_live
    with state_mod.locked_read(run):   # torn-read guard, same as show/verify
        st = state_mod.load(run, workspace)
    ensure_live(st, "env-check")
    tasks = st.get("tasks", [])
    if task_id is not None:
        scoped = [t for t in tasks if t["id"] == task_id]
        if not scoped:
            raise state_mod.StateError(
                f"unknown task '{task_id}' — check --task")
    else:
        scoped = [t for t in tasks
                  if t.get("status") not in ("done", "archived")]
    declared = (config or {}).get("env_requirements") or {}
    if not isinstance(declared, dict):
        # `env_requirements` is not schema-validated (nor is `language.*`), so
        # a list-shaped typo used to escape the CLI's JSON error contract as a
        # raw AttributeError traceback (re-verify finding).
        raise state_mod.StateError(
            "config `env_requirements` must be a mapping of name -> "
            f"{{probe, hint}} (got {type(declared).__name__})")
    names: list[str] = []
    for t in scoped:
        for r in t.get("env_requires") or []:
            if r not in names:
                names.append(r)
    checked, missing = [], []
    for name in names:
        spec = declared.get(name)
        if not isinstance(spec, dict) or not str(spec.get("probe") or "").strip():
            # A requirement whose probe vanished from config AFTER
            # plan-register validated it (a workspace-config edit mid-run).
            # Treated as MISSING, never as satisfied: "cannot check" must
            # never render as "checked".
            missing.append({"name": name, "probe": None,
                            "hint": "no `probe` declared in config "
                                    "`env_requirements` — add one, or drop "
                                    "the requirement from the plan",
                            "detail": "unprobeable"})
            continue
        probe = str(spec["probe"])
        try:
            proc = subprocess.run(probe, shell=True, capture_output=True,
                                  text=True, timeout=60,
                                  encoding="utf-8", errors="replace")
            ok, detail = proc.returncode == 0, (
                proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
            detail = detail[0][:200]
        except (subprocess.SubprocessError, OSError) as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        entry = {"name": name, "probe": probe, "detail": detail}
        checked.append({**entry, "present": ok})
        if not ok:
            missing.append({**entry,
                            "hint": str(spec.get("hint") or "").strip()
                            or "make this available, then re-run env-check"})
    if missing:
        ndjson.append_record(run / "events.ndjson",
                             {"kind": "env-prereq-missing",
                              "tasks": [t["id"] for t in scoped],
                              "missing": [m["name"] for m in missing],
                              "actor": "env-check"})
    elif task_id is None and _has_open_env_miss(run):
        # Unlike its sibling kinds this one is genuinely RESOLVABLE — the
        # human starts the service and re-runs — so a permanent flag would
        # leave every such run reading DEGRADED forever (re-verify finding).
        # Paired off in outstanding_flagged like deferral-pending/-recorded.
        # RUN-WIDE invocations only (pre-release review, both lenses,
        # reproduced): outstanding_flagged clears every open miss on one
        # satisfied event, on the strength of "the whole set is re-probed" —
        # which a `--task`-scoped probe makes false. Fixing docker and
        # re-checking only T1 must not clear T2's emulator flag; the
        # run-wide check the develop step documents is the one that clears.
        # No `checked` requirement: a plan revision that REMOVED the failing
        # requirement leaves nothing to probe, and that too resolves the
        # outstanding miss rather than flagging it forever.
        ndjson.append_record(run / "events.ndjson",
                             {"kind": "env-prereq-satisfied",
                              "checked": [c["name"] for c in checked],
                              "actor": "env-check"})
    return {"tasks": [t["id"] for t in scoped], "checked": checked,
            "missing": missing}


def preflight(workspace: Path, run: Path, config: dict, manifest: dict,
              repo: Path, base_branch: str | None = None,
              feature_branch_suffix: str | None = None) -> dict:
    """Create the feature branch from the declared naming template and record
    the `branches` artifact — an owned, mechanical step. Ensures the repo is
    clean and on its default branch first (gitops.ensure_default_branch) so
    the feature branch is always cut from a known-clean base, never from
    whatever branch/dirty state the repo happened to be left in. Pass
    `base_branch` to override the auto-resolved default-branch guess.

    `branches` is keyed by the repo's registered name (design.md piece 4's
    name->path registry) — a run-level SINGLE value here would return repo
    1's branch for every subsequent repo in a multi-repo run (adversarial-
    review finding); keying by name mirrors how `pr` (create_pr) is keyed
    too, and lets create_pr recover the SAME resolved base branch later
    instead of guessing 'main'.

    Idempotent on a SEQUENTIAL retry (mirrors worktree_add's resume pattern):
    if this run already recorded a `branches` entry for this repo, a
    crash-and-retry returns it directly rather than re-deriving/switching
    branches — otherwise ensure_default_branch would see the already-correct
    feature-branch checkout as "clean, off-target" and switch it back to
    default before `checkout -b` fails on the branch already existing. This
    does NOT protect against a genuinely CONCURRENT second run racing this
    same call while it's mid-flight (no repo-level lock exists — see the
    known risk in preflight.md); it only closes the single-caller
    crash-then-retry case."""
    from . import initws
    from .transitions import ensure_live, TransitionError
    name = initws.repo_name(config, repo) or str(repo)
    pre = state_mod.load(run, workspace)
    ensure_live(pre, "preflight")
    # Idempotent retry FIRST — before the precondition check or any git side
    # effect. A run that already recorded a `branches` entry returns it
    # directly regardless of the current cursor step: a crash-and-retry after
    # the cursor advanced past preflight must still no-op, per this function's
    # idempotency contract (review: the F4 precondition below must not pre-empt
    # this, or a post-advance resume would hard-error instead of no-op'ing).
    existing = ((pre.get("artifacts") or {}).get("branches") or {}).get(name)
    if existing:
        # …but a suffix the caller asked for and did NOT get back is a silent
        # lie (adversarial-review): applying the branch-aside remedy to a repo
        # whose branch was already recorded used to return `ok: true` with the
        # ORIGINAL name, leaving the run on the colliding branch believing it
        # had moved aside. Idempotency means "same request, same answer" — a
        # different request must not be answered by the cached one.
        wanted = _render_feature_branch(config, pre, feature_branch_suffix)
        if feature_branch_suffix and existing.get("branch") != wanted:
            raise state_mod.StateError(
                f"{name}: this run already cut branch "
                f"'{existing.get('branch')}'; --feature-branch-suffix "
                f"'{feature_branch_suffix}' would name it '{wanted}'. "
                "Preflight is idempotent per repo and will not rename a "
                "branch that may already carry commits — rename or delete it "
                "by hand, or leave the suffix off to keep the recorded name. "
                "(Branch-aside is for a repo preflight has NOT yet cut.)")
        return existing
    # F4 (validation-walk): for a FRESH preflight, validate the step
    # precondition BEFORE any git side effect. Running preflight with the cursor
    # NOT on a `branches`-producing step used to `checkout -b` first and only
    # then fail inside set_artifact — orphaning a stray, unrecorded branch that
    # then blocked retry. Refuse up front (the spawn guard's validate-before-
    # side-effect model); set_artifact still re-checks under the lock.
    _step = pre["cursor"]["current_step"]
    if "branches" not in (manifest["steps"][_step].get("produces") or []):
        raise TransitionError(
            f"step '{_step}' does not declare producing 'branches' — advance "
            "the cursor to the preflight step before running preflight")
    # Prior-work probe BEFORE any git side effect, and before the run lock:
    # `ls-remote` reaches the network (up to its timeout) and holding the
    # lock across that would stall every concurrent reader. Rendering off
    # `pre` is safe — change_type and work_item are bootstrap-fixed and the
    # in-lock render below produces the same name from the reloaded state.
    _probe_prior_remote_branch(run, repo, name,
                               _render_feature_branch(config, pre,
                                                      feature_branch_suffix))
    resolved = gitops.ensure_default_branch(repo, base_branch)
    # pin `.harness-key` out of `git add -A`'s reach in this repo and every
    # task worktree it will spawn (shared via the common git dir)
    gitops.ensure_repo_excludes(repo)
    with state_mod.locked(run):
        st = state_mod.load(run, workspace)
        branches = dict((st.get("artifacts") or {}).get("branches") or {})
        existing = branches.get(name)
        if existing:
            return existing
        branch = _render_feature_branch(config, st, feature_branch_suffix)
        # F4 (validation-walk): a crash after `checkout -b` but before recording
        # leaves the feature branch AT the base tip — ADOPT only that, a branch
        # pointing exactly where a fresh cut would. The branch name is
        # deterministic per work-item and feature branches are never deleted, so
        # a same-name branch that has DIVERGED is an aborted/foreign same-id
        # run's leftover carrying unrecorded commits that would silently ride
        # into this run's PR — refuse it loudly, as `checkout -b` used to
        # (review finding: adopt must not reuse foreign divergent state; and the
        # recorded base stays truthful because an adopted branch == the base).
        if gitops._branch_exists(repo, branch):
            # rev-parse the branch REFS explicitly (`refs/heads/…`), matching
            # _branch_exists's exactness — a bare name resolves a same-named tag
            # FIRST (gitrevisions), which could mask a divergent branch behind a
            # tag pointing at base (re-verify residual: surface, never guess).
            if gitops.run_git(repo, "rev-parse", f"refs/heads/{branch}") != \
                    gitops.run_git(repo, "rev-parse",
                                   f"refs/heads/{resolved['branch']}"):
                raise TransitionError(
                    f"branch '{branch}' already exists and has diverged from "
                    f"'{resolved['branch']}' — it carries unrecorded commits "
                    "(an aborted or foreign same-id run's leftover?). Refusing "
                    "to adopt it; delete or reconcile the branch by hand, then "
                    "retry preflight")
            gitops.run_git(repo, "checkout", branch)
        else:
            gitops.run_git(repo, "checkout", "-b", branch)
        entry = {"branch": branch, "base": resolved["branch"]}
        branches[name] = entry
        set_artifact(st, manifest, "branches", branches)
        state_mod.save(run, workspace, st)
    return entry
