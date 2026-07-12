"""Mechanical workflow steps as code (design.md principle 3).

`fetch` and `preflight` are orchestrator-owned steps whose logic is entirely
mechanical — so they ARE code, invoked by one-line step files. The minimal
`init` writes just enough workspace config for a run (the interactive
interview is M7).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

import yaml

from . import gitops, ndjson, state as state_mod
from .state import safe_id
from .transitions import set_artifact

# The ONE definition of which event kinds a human should be shown — read by
# both `status` (count) and `metrics_report` (table). These used to be two
# hand-maintained lists that drifted (field e2e E2E-1: status said 18
# flagged, metrics.md said 23 — same run, same ledger, different filters).
FLAGGED_EVENT_KINDS = (
    "test-revision", "reviewer-rejected", "hook-blocked",
    "missing-status-block", "quick-recheck", "contracts-check",
    "verdict-uncaptured", "background-spawn-uncaptured",
    "coverage-skipped", "pr-recorded-manually", "secret-sweep-blocked",
    "gate-skipped", "deferral-pending")


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
    outstanding deferral (review finding: an audit gauge must not under-count)."""
    flagged = [e for e in events if e.get("kind") in FLAGGED_EVENT_KINDS]
    open_pending: list[dict] = []
    resolved: set[int] = set()
    for e in events:
        kind = e.get("kind")
        if kind == "deferral-pending":
            open_pending.append(e)
        elif kind == "deferral-recorded" and open_pending:
            resolved.add(id(open_pending.pop(0)))   # resolve earliest open pending
    return [e for e in flagged if id(e) not in resolved]


def slug(title: str, limit: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:limit].rstrip("-") or "change"


def classify(item: dict, config: dict) -> tuple[bool, str]:
    """Ex-ante quick eligibility (design.md piece 1). Conservative: eligible
    only when explicitly hinted AND no disqualifying keyword appears; the
    post-develop diff re-check (quick-recheck) is the real-diff backstop.
    Returns (quick_eligible, reason) — a verdict, never a mode NAME: the
    verdict-to-mode mapping is the entry step's declared `selects_mode`,
    resolved by select_mode() below (composability round, 2026-07-08: this
    function used to mint the literals "full"/"quick" itself, leaving the
    manifest's selects_mode declaration decorative)."""
    qm = config.get("quick_mode", {})
    text = f"{item.get('title', '')} {item.get('description', '')}".lower()
    hinted = bool(re.search(r"^mode:\s*quick", item.get("description", ""),
                            re.MULTILINE | re.IGNORECASE))
    for keyword in qm.get("disqualify_keywords", []):
        if keyword.lower() in text:
            return False, f"disqualified by keyword '{keyword}'"
    return (True, "explicitly hinted") if hinted else (False, "default")


def select_mode(manifest: dict, quick_eligible: bool) -> str:
    """Resolve classify()'s verdict to a mode name via the entry step's
    declared `selects_mode` mapping — the single place a run's entry mode
    is minted, reading the manifest rather than repeating it."""
    sel = (manifest["steps"][manifest["entry"]] or {}).get("selects_mode") or {}
    mode = sel.get("true" if quick_eligible else "false")
    if mode not in (manifest.get("modes") or {}):
        raise state_mod.StateError(
            f"entry step '{manifest['entry']}' selects_mode maps "
            f"quick_eligible={quick_eligible} to {mode!r}, which is not a "
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


def bootstrap_gate(config: dict) -> None:
    if not config.get("bootstrap_completed"):
        raise state_mod.StateError(
            "bootstrap incomplete — run /init-workspace before /dev-workflow")


def _bootstrap_from_item(workspace: Path, config: dict, manifest: dict,
                         item: dict, date: str | None) -> dict:
    """The shared step-one *tail* (RC2), transport-independent: classify ->
    resolve change_type -> bootstrap state (from-nothing transition, collision-
    refusing) -> seed a single task -> persist work-item.json. Both transports
    converge here — the CLI path after `dispatch`, the MCP path after
    `normalize` — so a run is bootstrapped identically whichever was used."""
    quick_eligible, reason = classify(item, config)
    mode = select_mode(manifest, quick_eligible)
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
                          "quick_eligible": quick_eligible,
                          "classify_reason": reason,
                          "change_type": change_type,
                          "seed_task": {
                              "id": "T1", "repo": seed_repo,
                              "basis": "positional-default (repos[0]); "
                                       "provisional until plan-register"},
                          "actor": "fetch"})
    return {"run": str(run), "mode": mode, "change_type": change_type,
            "classify_reason": reason}


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
    # LITERAL `git grep -F` in every named repo, so a fragment must be a
    # grep-able code substring — a bare symbol or signature that appears
    # verbatim in source (`archived`, `filter_notes(notes, tag)`), never an
    # English description. A prose fragment matches nothing and false-reports
    # drift on correctly-implemented code (E2E-1 false positive). An em/en-dash
    # is a reliable prose tell (a code token never carries one), so reject it at
    # declaration — fail-fast — rather than surfacing a phantom drift at pre-pr.
    # (True semantic matching is the future upgrade noted in reconcile_contracts.)
    for f in fragments:
        if "—" in f or "–" in f:
            raise state_mod.StateError(
                f"contract signature fragment {f!r} reads as prose (dash "
                "separator) — reconcile-contracts matches fragments by literal "
                "source search; declare a grep-able code token or signature "
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


def plan_register(workspace: Path, run: Path, manifest: dict,
                  tasks: list[dict], contracts: list[dict] | None = None) -> dict:
    """Replace the fetch-seeded task list with the approved plan's tasks
    (+ declared cross-repo contracts). Legal only while the cursor is at
    `plan` — the plan is what the gate will approve."""
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
        st["tasks"] = [
            {"id": t["id"], "repo": t.get("repo", "."), "status": "pending",
             "depends_on": t.get("depends_on", []), "risk": t.get("risk", "low"),
             "test_intents": t.get("test_intents", []),
             "commit_sha": None, "review_rounds": 0, "stalls": 0,
             "worktree": None}
            for t in tasks]
        for c in contracts or []:
            _validate_contract(c)
        st["contracts"] = contracts or []
        state_mod.save(run, workspace, st)
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
    cheaply. True semantic/AST comparison remains a documented future upgrade,
    not attempted here: it would need structured fragments (the symbol + its
    kind, not a free string) plus a per-language matcher — stdlib `ast` for
    Python, a parser or heuristic for JS, tree-sitter for universal coverage —
    which trades the language-agnostic, near-zero-dependency stance `git grep`
    was chosen for; hence deferred."""
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
        for repo_name in repo_names:
            repo = Path(repos.get(repo_name, repo_name))
            missing = []
            for frag in fragments:
                try:
                    gitops.run_git(repo, "grep", "-F", "-q", frag, "--", ".", *excludes)
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
    status sync; write-back is best-effort, never a blocking requirement."""
    from .transitions import ensure_live
    with state_mod.locked_read(run):  # torn-read guard, same as show/verify
        st = state_mod.load(run, workspace)
    ensure_live(st, "write-back")  # never push a live tracker status for a dead run
    target = resolve_write_back_status(config, milestone, st["work_item"].get("type"))
    if target is None:
        return {"written": False}
    from .providers import dispatch, get_module
    if getattr(get_module(config), "TRANSPORT", "") == "mcp":
        return {"written": False, "mcp_target": target,
               "mcp_guidance": f"MCP-transport provider — a script can't call "
                               f"an MCP tool; invoke the mapped work_item."
                               f"transition tool yourself if you want live "
                               f"status sync (to={target!r})."}
    dispatch(config, "work_item.transition", id=st["work_item"]["id"], to=target)
    return {"written": True, "to": target}


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
    from .providers import dispatch
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
    if not skip_transition:
        target = resolve_write_back_status(config, "done", st["work_item"].get("type"))
        if target is not None:
            dispatch(config, "work_item.transition",
                     id=st["work_item"]["id"], to=target)
    ndjson.append_record(run / "events.ndjson",
                         {"kind": "reconciled", "actor": "reconcile"})
    return {"reconciled": True}


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
    """Deterministic aggregation rendered as human-readable tables: timings
    from state, tokens from the ledger (aggregated per task × role),
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
             f"{_fmt_when(ndjson.now_iso())} UTC", "", "## Step timings", ""]
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


def preflight(workspace: Path, run: Path, config: dict, manifest: dict,
              repo: Path, base_branch: str | None = None) -> dict:
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
        branch = gitops.render(config["naming"]["branch"],
                               type=st["change_type"],
                               id=st["work_item"]["id"],
                               slug=slug(st["work_item"]["title"]))
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
