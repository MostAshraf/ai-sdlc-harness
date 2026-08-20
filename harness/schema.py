"""Schema + coherence validation for ai-sdlc-harness declared data.

Validates the files that are the design's single sources of truth:
  pipeline/manifest.yaml   pipeline vocabulary (RC2/RC4) + per-mode flow
  pipeline/task-fsm.yaml   per-task status FSM
  pipeline/surfaces.yaml   invocation-control classification
  config/defaults/*.yaml   shipped config defaults

The flow-completeness check (every step's preconditions have an earlier
producer in that mode's sequence) mechanically prevents the class of bug the
adversarial pass found as "quick mode consumes what nothing in quick produces".

CLI:  python3 -m harness.schema [repo-root]     exit 0 valid / 1 invalid
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ai-sdlc-harness requires PyYAML for its declared-data files.\n"
        "Remediation (PEP 668-safe, what /init-workspace does):\n"
        '  python3 -m venv "$CLAUDE_PLUGIN_ROOT/.venv" && '
        '"$CLAUDE_PLUGIN_ROOT/.venv/bin/pip" install pyyaml\n'
        "then invoke as: .venv/bin/python -m harness …\n"
        "(Windows: `python -m venv`, and the venv lands its interpreter at "
        ".venv\\Scripts\\python.exe — bin/harness probes both layouts)\n"
    )
    raise

# Artifact references computed by the engine itself, not produced by a step.
ENGINE_COMPUTED_PREFIXES = ("classify.",)
GATE_TOKEN_PREFIX = "gate."

# `spawn_pairing.resolvers` names the engine INTERPRETS — `abandoned` is asked
# for by name (a late stop for an abandoned spawn must be refused, not
# captured), and `captured` is the ordinary close. Data names them, code
# provides the behaviour; a missing name would silently degrade a reader to
# "nothing closes this pending", which is the half-enforced vocabulary this
# file exists to refuse.
REQUIRED_SPAWN_RESOLVERS = ("captured", "abandoned")


class Issues:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ----------------------------------------------------------------- manifest

def _spawn_ok(spawn: dict, surfaces: dict, where: str, issues: Issues) -> None:
    shape, mode = spawn.get("shape"), spawn.get("mode")
    shapes = surfaces.get("shapes", {})
    if shape not in shapes:
        issues.err(f"{where}: unknown shape '{shape}'")
    elif mode not in shapes[shape].get("modes", []):
        issues.err(f"{where}: shape '{shape}' has no mode '{mode}'")


def _config_path_ok(dotted: str, config: dict) -> bool:
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _check_when(when: dict, available: set, config: dict, where: str, issues: Issues) -> None:
    value = when.get("value")
    if value and value not in available:
        issues.err(f"{where}: `when.value` '{value}' has no earlier producer")
    ref = (when.get("at_least") or {}).get("config")
    if ref and not _config_path_ok(ref, config):
        issues.err(f"{where}: `when.at_least.config` '{ref}' not found in config defaults")


def _walk_sequence(name: str, seq: list, steps: dict, config: dict,
                   issues: Issues, seed: set | None = None) -> set:
    """Flow-completeness walk. Returns the artifact set available at the end."""
    available: set = set(seed or ())
    for sid in seq:
        step = steps.get(sid)
        if step is None:
            issues.err(f"manifest: sequence '{name}' references undefined step '{sid}'")
            continue
        where = f"manifest: [{name}] step '{sid}'"
        for pre in step.get("preconditions", []) or []:
            if pre.startswith(ENGINE_COMPUTED_PREFIXES):
                continue
            if pre not in available:
                issues.err(f"{where}: precondition '{pre}' has no earlier producer")
        if step.get("when"):
            _check_when(step["when"], available, config, where, issues)
        if step.get("gate"):
            presents = step.get("presents")
            if presents and presents not in available:
                issues.err(f"{where}: gate presents '{presents}' which is not yet produced")
        available |= set(step.get("produces", []) or [])
        if step.get("gate"):
            available.add(GATE_TOKEN_PREFIX + sid)
    return available


def validate_manifest(manifest: dict, surfaces: dict, config: dict, issues: Issues) -> None:
    steps: dict = manifest.get("steps", {}) or {}
    modes: dict = manifest.get("modes", {}) or {}
    groups: dict = manifest.get("groups", {}) or {}
    entry = manifest.get("entry")

    if entry not in steps:
        issues.err(f"manifest: entry '{entry}' is not a defined step")

    # -- step-level structure
    reachable: set = set()
    for sid, step in steps.items():
        where = f"manifest: step '{sid}'"
        if step.get("gate") and step.get("spawns"):
            issues.err(f"{where}: a gate step must not spawn subagents")
        if step.get("owner") and step.get("spawns"):
            issues.err(f"{where}: declares both owner and spawns")
        if not step.get("gate") and not step.get("owner") and not step.get("spawns"):
            issues.err(f"{where}: needs owner, spawns, or gate")
        if step.get("select") and not step.get("gate"):
            issues.err(f"{where}: `select` is only meaningful on a gate step")
        if step.get("select") and (step.get("on_reject") or step.get("forward_on")):
            issues.err(f"{where}: `select` gates don't use forward_on/on_reject "
                       "— a selection is not an approve/reject decision")
        if step.get("requires_tasks_terminal") and step.get("gate"):
            issues.err(f"{where}: `requires_tasks_terminal` is not meaningful "
                       "on a gate step (gates aren't in the task loop)")
        if step.get("requires_tasks_registered") and step.get("gate"):
            issues.err(f"{where}: `requires_tasks_registered` is not meaningful "
                       "on a gate step (registration happens before the gate)")
        if step.get("requires_repo_confirmed") and step.get("gate"):
            issues.err(f"{where}: `requires_repo_confirmed` is not meaningful "
                       "on a gate step (the repo is ratified before the gate, "
                       "and a gate's exits derive from the human decision)")
        vb = step.get("verdict_bound")
        if vb is not None:
            # Half-enforced vocabulary is the failure mode here: a shape the
            # engine can't interpret must be refused at validation, never
            # discovered as a runtime surprise mid-run.
            if step.get("gate"):
                issues.err(f"{where}: `verdict_bound` is not meaningful on a "
                           "gate step (gates derive from human input, not "
                           "reviewer verdicts)")
            if (not isinstance(vb, dict)
                    or not {"mode", "bound"} <= set(vb)
                    or set(vb) - {"mode", "bound", "outcome_artifact",
                                  "round_marker"}):
                issues.err(f"{where}: `verdict_bound` must be "
                           "{mode, bound: {config: key}} plus optional "
                           "outcome_artifact / round_marker")
            else:
                spawn_modes = {s.get("mode")
                               for s in step.get("spawns", []) or []}
                if vb.get("mode") not in spawn_modes:
                    issues.err(f"{where}: verdict_bound.mode "
                               f"'{vb.get('mode')}' is not a mode this step "
                               "spawns — the step could never produce the "
                               "verdict its exits depend on")
                ref = (vb.get("bound") or {}).get("config")
                if not ref or not _config_path_ok(ref, config):
                    issues.err(f"{where}: verdict_bound.bound.config "
                               f"'{ref}' not found in config defaults")
                if not step.get("returns_to"):
                    issues.err(f"{where}: `verdict_bound` requires a "
                               "`returns_to` edge (the CHANGES_REQUESTED-"
                               "under-bound loop target)")
                oa = vb.get("outcome_artifact")
                if oa is not None and oa not in (step.get("produces") or []):
                    # the engine records it via set_artifact, which refuses
                    # names outside the step's produces — catch the
                    # mismatch at validation, not on the first forward edge
                    issues.err(f"{where}: verdict_bound.outcome_artifact "
                               f"'{oa}' must be one of the step's produces")
                rm = vb.get("round_marker")
                if rm is not None and not (isinstance(rm, str) and rm.strip()):
                    # half-enforced-vocabulary bar: a non-string/blank marker
                    # would silently degrade guard_stall_verdict's anchor to
                    # "no round marker" instead of failing at validation
                    issues.err(f"{where}: verdict_bound.round_marker must be "
                               "a non-empty events.ndjson kind string")
                # Two data features each claiming exclusive exit ownership
                # would silently resolve by interpreter ordering — refuse
                # the combination instead (half-enforced-vocabulary bar).
                for esc in manifest.get("escalations", []) or []:
                    if (esc.get("from") or {}).get("step") == sid:
                        issues.err(
                            f"{where}: `verdict_bound` cannot share a step "
                            "with an escalation source — both claim exclusive "
                            "ownership of the step's exits")
        for spawn in step.get("spawns", []) or []:
            _spawn_ok(spawn, surfaces, where, issues)
        for edge_key in ("on_reject", "returns_to"):
            target = step.get(edge_key)
            if target is not None:
                if target not in steps:
                    issues.err(f"{where}: {edge_key} target '{target}' is not a defined step")
                else:
                    reachable.add(target)
        sel = step.get("selects_mode")
        if sel:
            src = sel.get("from", "")
            if not src.startswith(ENGINE_COMPUTED_PREFIXES):
                issues.err(f"{where}: selects_mode.from '{src}' must be engine-computed (classify.*)")
            for key, target_mode in sel.items():
                if key == "from":
                    continue
                if target_mode not in modes:
                    issues.err(f"{where}: selects_mode targets unknown mode '{target_mode}'")

    # -- per-mode flow completeness (+ shared-entry rule)
    mode_end_artifacts: dict[str, set] = {}
    for mode_name, seq in modes.items():
        if not seq or seq[0] != entry:
            issues.err(f"manifest: mode '{mode_name}' must start with entry '{entry}' (shared-prefix rule)")
        reachable.update(seq or [])
        mode_end_artifacts[mode_name] = _walk_sequence(mode_name, seq or [], steps, config, issues)

    # -- groups: reachable after `available_after`, validated in group order
    for gid, group in groups.items():
        gwhere = f"manifest: group '{gid}'"
        anchor = group.get("available_after")
        gsteps = group.get("steps", []) or []
        reachable.update(gsteps)
        anchoring_modes = [m for m, seq in modes.items() if anchor in (seq or [])]
        if anchor not in steps or not anchoring_modes:
            issues.err(f"{gwhere}: available_after '{anchor}' not found in any mode sequence")
            continue
        for mode_name in anchoring_modes:
            seq = modes[mode_name]
            prefix = seq[: seq.index(anchor) + 1]
            seed = _walk_sequence(f"{mode_name}(prefix)", prefix, steps, config, Issues())
            _walk_sequence(f"{mode_name}/group:{gid}", gsteps, steps, config, issues, seed=seed)

    # -- off-sequence side-steps (reached via on_reject) validated in context
    for sid, step in steps.items():
        if step.get("returns_to") and sid not in {s for seq in modes.values() for s in seq}:
            rejecting_gates = [g for g, s in steps.items() if s.get("on_reject") == sid]
            for gate_id in rejecting_gates:
                for mode_name, seq in modes.items():
                    if gate_id in (seq or []):
                        prefix = seq[: seq.index(gate_id) + 1]
                        seed = _walk_sequence(f"{mode_name}(prefix)", prefix, steps, config, Issues())
                        _walk_sequence(f"{mode_name}/side:{sid}", [sid], steps, config, issues, seed=seed)

    # -- escalations
    for esc in manifest.get("escalations", []) or []:
        where = "manifest: escalation"
        for end in ("from", "to"):
            ref = esc.get(end, {}) or {}
            m, s = ref.get("mode"), ref.get("step")
            if m not in modes:
                issues.err(f"{where}: {end}.mode '{m}' unknown")
            elif s not in (modes[m] or []):
                issues.err(f"{where}: {end}.step '{s}' not in mode '{m}'")

    # -- cross-cutting spawns
    for spawn in manifest.get("always_legal_spawns", []) or []:
        _spawn_ok(spawn, surfaces, "manifest: always_legal_spawns", issues)

    # -- reachability
    for sid in steps:
        if sid not in reachable:
            issues.err(f"manifest: step '{sid}' is unreachable (no sequence, group, or edge references it)")


# ---------------------------------------------------------------------- fsm

def validate_fsm(fsm: dict, surfaces: dict, issues: Issues) -> None:
    states = fsm.get("states", []) or []
    if len(states) != len(set(states)):
        issues.err("fsm: duplicate states")
    if fsm.get("initial") not in states:
        issues.err(f"fsm: initial '{fsm.get('initial')}' not a declared state")
    # `terminal` is READ by the engine (transitions.terminal_statuses), not
    # decorative — an absent or misspelled entry would silently shrink the
    # set every "is this task finished?" question shares, so it is required
    # and every member must be a declared state.
    terminal = fsm.get("terminal")
    if not isinstance(terminal, list) or not terminal:
        issues.err("fsm: `terminal` must be a non-empty list of declared "
                   "states — the engine reads it wherever it asks whether a "
                   "task is finished")
    else:
        for t in terminal:
            if t not in states:
                issues.err(f"fsm: terminal '{t}' not a declared state")
    seen = set()
    for t in fsm.get("transitions", []) or []:
        frm, to = t.get("from"), t.get("to")
        for end, val in (("from", frm), ("to", to)):
            if val not in states:
                issues.err(f"fsm: transition {end} '{val}' not a declared state")
        key = (frm, to)
        if key in seen:
            issues.err(f"fsm: duplicate transition {frm} -> {to}")
        seen.add(key)
    _validate_spawn_pairing(fsm.get("spawn_pairing"), surfaces, issues)


def _record_shape_ok(rec, where: str, issues: Issues) -> tuple | None:
    """A `{kind, actor}` pairing record: both non-blank strings, no extras.

    Held to the same bar `verdict_bound` is — a shape the engine cannot
    interpret is refused at validation rather than discovered as a missing
    refusal mid-run, where its only symptom is a guard that silently stops
    guarding."""
    if (not isinstance(rec, dict)
            or set(rec) != {"kind", "actor"}
            or not all(isinstance(rec.get(k), str) and rec[k].strip()
                       for k in ("kind", "actor"))):
        issues.err(f"{where}: must be {{kind, actor}} with non-empty string "
                   "values — both are matched literally against "
                   "events.ndjson records")
        return None
    return (rec["kind"], rec["actor"])


def _validate_spawn_pairing(pairing, surfaces: dict, issues: Issues) -> None:
    """`spawn_pairing` is READ by four independent readers across two layers
    (engine + hooks), so an absent or misshapen block is not a cosmetic
    problem: `open_pendings` would see no pending kind at all and every
    "is a spawn in flight?" question in the system would answer no."""
    if not isinstance(pairing, dict) or not pairing:
        issues.err("fsm: `spawn_pairing` must declare {pending, resolvers} — "
                   "the engine and the hooks both read it to decide whether a "
                   "background spawn is still in flight")
        return
    if set(pairing) - {"pending", "resolvers"}:
        issues.err("fsm: `spawn_pairing` takes only `pending` and `resolvers`")
    pending = _record_shape_ok(pairing.get("pending"),
                               "fsm: spawn_pairing.pending", issues)
    # The pending kind must ALSO be a flagged kind, or the two layers split:
    # `open_pendings` refuses every re-spawn on a kind the gauge cannot see.
    # Executed (round-4 review) by declaring `spawn-launched`: schema reported
    # "OK — 0 error(s)" while at runtime open_pendings was 1 (the spawn and
    # stall guards refusing everything) and outstanding_flagged 0 with health
    # HEALTHY — the exact split task-fsm.yaml's own comment says this
    # declaration prevents. Imported rather than restated, and imported HERE
    # rather than at module scope, for the same reason `transitions._load_fsm`
    # is lazy: `harness.workflow` reads declared data at import.
    if pending is not None:
        from .workflow import FLAGGED_EVENT_KINDS
        if pending[0] not in FLAGGED_EVENT_KINDS:
            issues.err(f"fsm: spawn_pairing.pending.kind '{pending[0]}' is "
                       "not in workflow.FLAGGED_EVENT_KINDS — an open pending "
                       "would refuse every re-spawn and every stall while "
                       "being invisible to the flagged gauge and to run "
                       "health (the run reads HEALTHY and wedged)")
    # An actor that collides with a declared spawn SHAPE re-opens the forgery
    # round 4 closed: `capture_post_spawn` used to write the shape into
    # `actor`, and a shape is a value any spawn-visible prose already carries,
    # so `pending.actor: reviewer` would make the anti-forgery check pass for
    # anything that can name a shape. The actor must be an OWNER-issued value
    # ("capture", "stall") that only the writing code path uses.
    shapes = set((surfaces or {}).get("shapes") or {})
    for where, rec in (("pending", pairing.get("pending")),
                       *((f"resolvers['{n}']", r) for n, r
                         in (pairing.get("resolvers") or {}).items()
                         if isinstance(r, dict))):
        if isinstance(rec, dict) and rec.get("actor") in shapes:
            issues.err(f"fsm: spawn_pairing.{where}.actor "
                       f"'{rec.get('actor')}' is a declared spawn shape — the "
                       "actor is the anti-forgery bound and must be a value "
                       "only the owning writer issues (`capture`, `stall`), "
                       "never one a spawn already publishes about itself")
    resolvers = pairing.get("resolvers")
    if not isinstance(resolvers, dict) or not resolvers:
        issues.err("fsm: spawn_pairing.resolvers must be a non-empty mapping "
                   "of engine-interpreted name -> {kind, actor}")
        return
    for name in REQUIRED_SPAWN_RESOLVERS:
        if name not in resolvers:
            issues.err(f"fsm: spawn_pairing.resolvers is missing '{name}' — "
                       "the engine interprets that name (data names it, code "
                       "provides it, exactly like a transition `guard`)")
    # …and NOTHING else. `closed_agent_ids` honours every declared resolver
    # by value, so an extra one — even under a name no code reads, even under
    # the empty string — silently becomes a further way to close a pending
    # (fuzzed in round-4 review). Required-plus-extras is a vocabulary only
    # half-enforced, which is the shape this file exists to refuse.
    for name in set(resolvers) - set(REQUIRED_SPAWN_RESOLVERS):
        issues.err(f"fsm: spawn_pairing.resolvers['{name}'] is not a name the "
                   "engine interprets — the declared set is exactly "
                   f"{list(REQUIRED_SPAWN_RESOLVERS)}, and an extra entry is "
                   "honoured as another way to close a pending while no code "
                   "path ever writes it")
    kinds = set()
    for name, rec in resolvers.items():
        shape = _record_shape_ok(
            rec, f"fsm: spawn_pairing.resolvers['{name}']", issues)
        if shape is None:
            continue
        if pending is not None and shape[0] == pending[0]:
            # a resolver that shares the pending's kind would close every
            # pending the moment it was written — the pairing inverted
            issues.err(f"fsm: spawn_pairing.resolvers['{name}'].kind "
                       f"'{shape[0]}' is also the pending kind — a resolver "
                       "must be a DIFFERENT record from the one it closes")
        if shape[0] in kinds:
            issues.err(f"fsm: spawn_pairing.resolvers['{name}'].kind "
                       f"'{shape[0]}' is declared by more than one resolver — "
                       "readers would disagree about which actor owns it")
        kinds.add(shape[0])


# ------------------------------------------------------------------ configs

def validate_configs(config: dict, issues: Issues) -> None:
    naming = config.get("naming", {}) or {}
    change_types = config.get("change_types", []) or []
    if not change_types:
        issues.err("config: change_types must be non-empty")
    for wit, ct in (config.get("work_item_type_map", {}) or {}).items():
        if ct not in change_types:
            issues.err(f"config: work_item_type_map['{wit}'] -> '{ct}' not in change_types")
    # Lens-panel override mapping (workflow.resolve_lenses): keys must be
    # real change_types — a typo'd key would silently never match, and the
    # full default panel would run where the override intended none (the
    # same key-vocabulary rule work_item_type_map gets). Values are held
    # to the SAME lens-slug rule as plan_review.lenses (below): mapped
    # names become file paths (reports/plan-attack-<lens>) and spawn-ask
    # text too (adversarial-review on this change: path-shaped mapped
    # values validated clean while the default list was slug-checked).
    by_ct = (config.get("plan_review", {}) or {}).get(
        "lenses_by_change_type", {}) or {}
    if not isinstance(by_ct, dict):
        issues.err("config: plan_review.lenses_by_change_type must be a "
                   "mapping of change_type -> lens list")
    else:
        for ct, ll in by_ct.items():
            if change_types and ct not in change_types:
                issues.err("config: plan_review.lenses_by_change_type key "
                           f"'{ct}' not in change_types")
            if not isinstance(ll, list) or not all(
                    isinstance(x, str)
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,30}", x)
                    for x in ll):
                issues.err(f"config: plan_review.lenses_by_change_type['{ct}'] "
                           "must be a list of lens-name slugs (lowercase "
                           "[a-z0-9-], ≤31 chars; empty list = "
                           "single-reviewer plan review)")
    for field, needed in (("branch", ("{type}", "{id}")), ("pr_title", ("{id}",))):
        template = naming.get(field, "")
        for ph in needed:
            if ph not in template:
                issues.err(f"config: naming.{field} missing placeholder {ph}")
    commits = naming.get("commit", {}) or {}
    for cls, needed in (("integration", ("{type}", "{id}")), ("working", ("{task}",)),
                        ("wip", ("{task}",)), ("mirror", ("{run}",))):
        template = commits.get(cls, "")
        if not template:
            issues.err(f"config: naming.commit.{cls} missing")
            continue
        for ph in needed:
            if ph not in template:
                issues.err(f"config: naming.commit.{cls} missing placeholder {ph}")

    for shape, val in (config.get("subagent_models", {}) or {}).items():
        if isinstance(val, dict) and "default" not in val:
            issues.err(f"config: subagent_models.{shape} object form needs 'default'")

    qm = config.get("quick_mode", {}) or {}
    for knob in ("loc_max", "files_max"):
        if not isinstance(qm.get(knob), int) or qm.get(knob) <= 0:
            issues.err(f"config: quick_mode.{knob} must be a positive integer")

    sec = config.get("security", {}) or {}
    order = sec.get("severity_order", []) or []
    if sec.get("gate_threshold") not in order:
        issues.err("config: security.gate_threshold must be one of security.severity_order")

    for rule in config.get("review_policy", []) or []:
        if not all(rule.get(k) for k in ("id", "applies", "rule")):
            issues.err(f"config: review_policy entry {rule.get('id') or rule} needs id/applies/rule")

    for knob in ("review_rounds", "stall", "repo_map", "plan_review"):
        if knob not in config:
            issues.err(f"config: workflow defaults missing '{knob}'")

    default_mode = config.get("default_mode", "full")
    if default_mode not in ("full", "lean"):
        # quick is deliberately not defaultable: it needs per-item
        # eligibility (hint + no disqualifying keyword), never a standing
        # workspace choice
        issues.err("config: default_mode must be 'full' or 'lean' "
                   f"(got {default_mode!r})")

    lenses = (config.get("plan_review") or {}).get("lenses")
    if not isinstance(lenses, list) or not all(
            isinstance(x, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,30}", x)
            for x in lenses):
        # empty list is legal (the declared single-reviewer fallback);
        # a non-list, non-string, or non-slug entry is a shape error, not a
        # choice — lens names become file paths (reports/plan-attack-<lens>)
        # and spawn-ask text, so they must be plain slugs, never path or
        # prompt material (adversarial-review finding)
        issues.err("config: plan_review.lenses must be a list of lens-name "
                   "slugs (lowercase [a-z0-9-], ≤31 chars; empty list = "
                   "single-reviewer plan review)")


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge — `override`'s nested keys layer onto `base`'s
    instead of replacing the whole top-level value. Only dicts recurse;
    a list-valued key (e.g. `review_policy`) is still replaced wholesale,
    same as any other non-dict value — callers merging list-valued config
    must resupply the complete list, there is no per-item merge here."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


# ------------------------------------------------------------------- driver

def merge_defaults(defaults_dir: Path, issues: Issues) -> dict:
    merged: dict = {}
    for path in sorted(defaults_dir.glob("*.yaml")):
        data = load_yaml(path) or {}
        for key, val in data.items():
            if key in merged:
                issues.err(f"config: top-level key '{key}' declared in more than one defaults file")
            merged[key] = val
    return merged


def validate_all(root: Path) -> Issues:
    issues = Issues()
    manifest = load_yaml(root / "pipeline" / "manifest.yaml")
    fsm = load_yaml(root / "pipeline" / "task-fsm.yaml")
    surfaces = load_yaml(root / "pipeline" / "surfaces.yaml")
    config = merge_defaults(root / "config" / "defaults", issues)
    validate_configs(config, issues)
    validate_fsm(fsm, surfaces, issues)
    validate_manifest(manifest, surfaces, config, issues)
    return issues


def main(argv: list[str]) -> int:
    # Same UTF-8 output contract as harness/__main__.py — this module has
    # its own entry point (`python -m harness.schema`), and its error lines
    # interpolate declared-data content that legally carries non-cp1252
    # chars; stderr's documented default error handler is restated because
    # reconfigure would otherwise reset it to strict.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    issues = validate_all(root)
    for w in issues.warnings:
        print(f"WARN  {w}")
    for e in issues.errors:
        print(f"ERROR {e}")
    print(f"{'OK' if issues.ok else 'INVALID'} — {len(issues.errors)} error(s), "
          f"{len(issues.warnings)} warning(s)")
    return 0 if issues.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
