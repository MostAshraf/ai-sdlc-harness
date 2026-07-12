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

def validate_fsm(fsm: dict, issues: Issues) -> None:
    states = fsm.get("states", []) or []
    if len(states) != len(set(states)):
        issues.err("fsm: duplicate states")
    if fsm.get("initial") not in states:
        issues.err(f"fsm: initial '{fsm.get('initial')}' not a declared state")
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


# ------------------------------------------------------------------ configs

def validate_configs(config: dict, issues: Issues) -> None:
    naming = config.get("naming", {}) or {}
    change_types = config.get("change_types", []) or []
    if not change_types:
        issues.err("config: change_types must be non-empty")
    for wit, ct in (config.get("work_item_type_map", {}) or {}).items():
        if ct not in change_types:
            issues.err(f"config: work_item_type_map['{wit}'] -> '{ct}' not in change_types")
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

    for knob in ("review_rounds", "stall", "repo_map"):
        if knob not in config:
            issues.err(f"config: workflow defaults missing '{knob}'")


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
    validate_fsm(fsm, issues)
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
