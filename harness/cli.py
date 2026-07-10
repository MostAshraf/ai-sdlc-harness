"""`harness` CLI — the owned entry points (M1: state engine verbs).

Every mutation of run authority goes through here, inside the run lock,
validated against the declared data, and chain-sealed. Exit codes:
0 ok · 1 refused (illegal transition / gate refusal / collision) ·
2 usage error (argparse's own exit code — kept distinct on purpose) ·
3 integrity violation detected (adversarial-review finding: this used to
ALSO be 2, so a skill following the documented contract read a typo'd flag
as tampering).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from . import chain, gates, gitops, mermaid, ndjson, state as state_mod, transitions, workflow
from .providers import ProviderError
from .schema import load_yaml, merge_defaults, deep_merge, Issues

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_declared(workspace: Path) -> tuple[dict, dict, dict]:
    manifest = load_yaml(PLUGIN_ROOT / "pipeline" / "manifest.yaml")
    fsm = load_yaml(PLUGIN_ROOT / "pipeline" / "task-fsm.yaml")
    config = merge_defaults(PLUGIN_ROOT / "config" / "defaults", Issues())
    ctx = workspace / ".claude" / "context"
    if ctx.is_dir():  # user config overrides shipped defaults (piece 4)
        for path in sorted(ctx.glob("*.yaml")):
            # A hand-edited file with a YAML syntax error or a non-mapping
            # top level used to brick EVERY verb with a raw traceback —
            # including the verbs you'd use to inspect/repair the config
            # (adversarial-review finding). Refuse cleanly, naming the file.
            try:
                loaded = load_yaml(path)
            except yaml.YAMLError as exc:
                raise ValueError(
                    f"{path}: invalid YAML — fix it by hand ({exc})") from exc
            if loaded is None:
                continue
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"{path}: top level must be a mapping (got "
                    f"{type(loaded).__name__}) — fix it by hand")
            config = deep_merge(config, loaded)
    # A relative stories_dir anchors at the WORKSPACE, not process cwd
    # (adversarial-review finding: both the verify-time check and every
    # local-markdown lookup resolved it against whatever cwd the process
    # had). Anchored once here so every consumer sees the same path.
    provider = config.get("provider")
    if isinstance(provider, dict):
        sd = provider.get("stories_dir")
        if isinstance(sd, str) and sd.strip() and not Path(sd).is_absolute():
            provider["stories_dir"] = str(workspace / sd)
    return manifest, fsm, config


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _json_source(flag: str, inline: str | None, file_path: Path | None, default):
    """Resolve a JSON CLI arg from EITHER an inline `--<flag>` string or a
    `--<flag>-file` path — mutually exclusive. File input sidesteps the
    shell-quoting hazards of inline `$(cat …)` substitution for large
    task/contract payloads, and of workspace paths that contain spaces."""
    if inline is not None and file_path is not None:
        raise ValueError(f"{flag}: pass only one of {flag} / {flag}-file")
    if file_path is not None:
        return json.loads(file_path.read_text(encoding="utf-8"))
    if inline is not None:
        return json.loads(inline)
    return default


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    """The full argparse surface, introspectable — tests validate every
    `harness <verb> --flag` a skill/agent markdown references against the
    real parser (the drift class where docs invoke flags that don't exist,
    which the wrapper-only invocation test can't see)."""
    p = argparse.ArgumentParser(prog="harness")
    p.add_argument("--workspace", type=Path, default=None)  # resolved in main():
    # --run's own parent (runs live at <workspace>/ai/<name>), else cwd —
    # a drifted shell cwd is a known footgun: it can mint a stray key in
    # a repo and phantom-fail integrity
    p.add_argument("--run", type=Path, default=None)  # required per-verb below
    sub = p.add_subparsers(dest="cmd", required=True)

    # Every subparser also accepts --workspace/--run (via `parents=`) so docs
    # across skills/dev-workflow can put them before OR after the verb — the
    # two orderings were used inconsistently. SUPPRESS means an omitted flag
    # here leaves the top-level parser's already-set value untouched instead
    # of clobbering it with a second, subparser-local default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--run", type=Path, default=argparse.SUPPRESS)

    ini = sub.add_parser("init", parents=[common],
                         help="minimal workspace config (interview: M7)")
    ini.add_argument("--stories-dir", type=Path, required=True)
    ini.add_argument("--repo", action="append", required=True, metavar="NAME=PATH")
    ini.add_argument("--test-cmd", action="append", required=True,
                     metavar="NAME=CMD")

    fe = sub.add_parser("fetch", parents=[common], help="shared step-one: fetch + classify + bootstrap")
    fe.add_argument("--id", default=None)
    fe.add_argument("--from-raw", action="store_true",
                    help="MCP transport: read the raw MCP tool result (JSON) on "
                         "stdin, normalize + bootstrap (no --id needed)")
    fe.add_argument("--date", default=None)

    pf = sub.add_parser("preflight", parents=[common], help="create the feature branch (owned)")
    pf.add_argument("--repo", type=Path, required=True)
    pf.add_argument("--branch", default=None,
                    help="override the auto-resolved default branch")

    pv = sub.add_parser("provider", parents=[common], help="dispatch a provider operation")
    pv.add_argument("--op", required=True)
    pv.add_argument("--id", default=None)
    pv.add_argument("--to", default=None)
    pv.add_argument("--text", default=None)
    pv.add_argument("--title", default=None)
    pv.add_argument("--description", default=None)

    pn = sub.add_parser("provider-normalize", parents=[common],
                        help="normalize a raw MCP tool result (stdin JSON)")
    pn.add_argument("--op", required=True)

    dv = sub.add_parser("discover", parents=[common], help="language/toolchain proposals for a repo "
                                          "(switches it to its default branch first)")
    dv.add_argument("--repo", type=Path, required=True)
    dv.add_argument("--branch", default=None,
                    help="override the auto-resolved default branch")

    edb = sub.add_parser("ensure-default-branch", parents=[common],
                         help="clean + on-default-branch precondition (reusable)")
    edb.add_argument("--repo", type=Path, required=True)
    edb.add_argument("--branch", default=None)

    rm = sub.add_parser("resolve-model", parents=[common],
                        help="resolve the model override for a shape/mode "
                             "spawn (subagent_models)")
    rm.add_argument("--shape", required=True)
    rm.add_argument("--mode", required=True)

    rcc = sub.add_parser("resolve-coverage-cmd", parents=[common],
                         help="resolve the per-repo coverage command "
                              "(language.repos.<name>.coverage_cmd)")
    rcc.add_argument("--repo", type=Path, required=True)

    sub.add_parser("init-verify", parents=[common], help="run the init verification gates")

    ws_ = sub.add_parser("init-section", parents=[common], help="write one config section")
    ws_.add_argument("--section", required=True)
    ws_.add_argument("--json", required=True,
                     help="provider/repos/language must be self-nested under "
                          "their own key, e.g. {\"repos\": {...}} — overrides "
                          "must NOT be, it's flat top-level config keys")

    sub.add_parser("init-finalize", parents=[common], help="write permissions + bootstrap "
                                         "marker; run after init-verify passes")

    ar = sub.add_parser("add-repo", parents=[common], help="register one new repo without "
                                         "disturbing already-registered ones")
    ar.add_argument("--name", required=True)
    ar.add_argument("--path", required=True)
    ar.add_argument("--test-cmd", default=None)

    sub.add_parser("migrate-detect", parents=[common],
                   help="fingerprint a pre-v3 workspace + inventory its "
                        "leftovers (read-only)")
    sub.add_parser("migrate-extract", parents=[common],
                   help="propose v3.0 config sections from a v2.x workspace "
                        "(read-only — init-section applies them)")

    sub.add_parser("status", parents=[common], help="workspace dashboard across runs")

    vm = sub.add_parser("validate-mermaid", parents=[common],
                        help="structural check on a markdown file's mermaid "
                             "fences (M8 WS-4, optional)")
    vm.add_argument("--file", type=Path, required=True)

    rmc = sub.add_parser("repo-map-check", parents=[common], help="repo-map staleness check")
    rmc.add_argument("--repo-name", required=True)
    rmc.add_argument("--repo", type=Path, required=True)

    rms = sub.add_parser("repo-map-stamp", parents=[common], help="stamp repo-map generation SHA")
    rms.add_argument("--repo-name", required=True)
    rms.add_argument("--repo", type=Path, required=True)

    pr_ = sub.add_parser("plan-register", parents=[common], help="replace seeded tasks with the plan's")
    pr_.add_argument("--tasks-json", default=None,
                     help="inline JSON array of tasks (or use --tasks-json-file)")
    pr_.add_argument("--tasks-json-file", type=Path, default=None,
                     help="path to a JSON file of tasks — avoids shell-quoting "
                          "large payloads / space-containing paths")
    pr_.add_argument("--contracts-json", default=None,
                     help="inline JSON array of contracts (default: none)")
    pr_.add_argument("--contracts-json-file", type=Path, default=None,
                     help="path to a JSON file of contracts")

    wa = sub.add_parser("worktree-add", parents=[common], help="per-task worktree (M5 charter)")
    wa.add_argument("--repo", type=Path, required=True)
    wa.add_argument("--task-id", required=True)
    wa.add_argument("--base", required=True)

    wr = sub.add_parser("worktree-remove", parents=[common], help="remove a task's worktree")
    wr.add_argument("--repo", type=Path, required=True)
    wr.add_argument("--task-id", required=True)

    qr = sub.add_parser("quick-recheck", parents=[common], help="post-develop diff-pattern re-check")
    qr.add_argument("--repo", type=Path, required=True)
    qr.add_argument("--base", required=True)

    sub.add_parser("security-scan", parents=[common], help="owned security step (all repos)")

    rc = sub.add_parser("reconcile-contracts", parents=[common], help="cross-repo contract check")

    cp = sub.add_parser("create-pr", parents=[common], help="create the PR via the git provider")
    cp.add_argument("--repo", type=Path, required=True)
    cp.add_argument("--url", default=None,
                    help="record an externally-created PR/MR under this URL "
                         "instead of calling the provider (provider-outage "
                         "escape hatch; URL must end in the PR/MR number)")

    fc = sub.add_parser("fetch-pr-comments", parents=[common],
                        help="fetch PR/MR comments via the git provider")
    fc.add_argument("--repo", type=Path, required=True)

    rec = sub.add_parser("reconcile", parents=[common], help="post-merge reconciliation")
    rec.add_argument("--skip-transition", action="store_true",
                     help="orchestrator already handled the work-item transition "
                          "itself (MCP-transport provider) — skip reconcile's own "
                          "dispatch of work_item.transition")

    wb = sub.add_parser("write-back", parents=[common],
                        help="milestone provider status write-back "
                             "(develop_start | in_review | done)")
    wb.add_argument("--milestone", required=True,
                    choices=["develop_start", "in_review", "done"])

    sub.add_parser("metrics", parents=[common], help="deterministic metrics report")

    b = sub.add_parser("bootstrap", parents=[common], help="from-nothing transition (refuses collision)")
    b.add_argument("--work-item-id", required=True)
    b.add_argument("--title", required=True)
    b.add_argument("--provider-ref", default="")
    # choices come from the manifest's declared modes, never a literal list —
    # a new mode added under `modes:` is bootstrappable with no CLI edit
    # (composability round: `--mode solo` used to die on argparse choices)
    b.add_argument("--mode", required=True,
                   choices=sorted(load_yaml(
                       PLUGIN_ROOT / "pipeline" / "manifest.yaml")["modes"]))
    b.add_argument("--change-type", required=True)
    b.add_argument("--task", action="append", default=[], metavar="ID[:REPO]")

    c = sub.add_parser("cursor", parents=[common], help="advance the pipeline cursor")
    c.add_argument("--to", required=True)

    t = sub.add_parser("task", parents=[common], help="task status transition")
    t.add_argument("--id", required=True)
    t.add_argument("--to", required=True)
    t.add_argument("--context", default=None)
    t.add_argument("--repo", type=Path, default=None)
    t.add_argument("--test-cmd", default=None)

    vr = sub.add_parser("verify-red", parents=[common], help="prove the test fails; seal the red-proof")
    vr.add_argument("--repo", type=Path, required=True)
    vr.add_argument("--task", required=True)
    vr.add_argument("--test-cmd", default=None)
    vr.add_argument("--tests", nargs="*", default=None)
    vr.add_argument("--intents", nargs="*", default=None)
    vr.add_argument("--revise", action="store_true")
    vr.add_argument("--reason", default=None)

    sr = sub.add_parser("show-redproof", parents=[common],
                        help="read a task's sealed red-proof, chain-verified "
                             "(owned entry point — never Read the file raw)")
    sr.add_argument("--task", required=True)

    cm = sub.add_parser("commit", parents=[common], help="working/wip commit via declared class")
    cm.add_argument("--repo", type=Path, required=True)
    cm.add_argument("--commit-class", default="working", choices=["working", "wip"])
    cm.add_argument("--task-id", default="")
    cm.add_argument("--summary", default="")
    cm.add_argument("--fixup-of", default=None, metavar="TASK_OR_SHA")

    mt = sub.add_parser("merge-task", parents=[common], help="squash a task branch / fold fixups")
    mt.add_argument("--repo", type=Path, required=True)
    mt.add_argument("--task-id", default=None)
    mt.add_argument("--task-branch", default=None)
    mt.add_argument("--summary", default="")
    mt.add_argument("--autosquash", action="store_true")
    mt.add_argument("--base", default=None)

    pm = sub.add_parser("publish-mirror", parents=[common], help="path-exclusive ai/** snapshot commit")
    pm.add_argument("--repo", type=Path, required=True)
    pm.add_argument("--push", action="store_true",
                    help="push the current branch after the mirror commit "
                         "(owned push machinery) — for the post-create-pr "
                         "and metrics publishes, whose snapshot must reach "
                         "the PR's remote branch")

    sb = sub.add_parser("sync-branch", parents=[common], help="owned rebase onto an updated base")
    sb.add_argument("--repo", type=Path, required=True)
    sb.add_argument("--onto", required=True)

    ph = sub.add_parser("push", parents=[common], help="owned push to the remote")
    ph.add_argument("--repo", type=Path, required=True)
    ph.add_argument("--branch", required=True)
    ph.add_argument("--force-with-lease", action="store_true")

    g = sub.add_parser("gate", parents=[common], help="present a gate / derive its decision")
    g.add_argument("--id", required=True)
    mode = g.add_mutually_exclusive_group(required=True)
    mode.add_argument("--present", action="store_true")
    mode.add_argument("--decide", action="store_true")
    g.add_argument("--options", default=None,
                   help="ONLY for a `select` gate, at --present time: the "
                        "runtime candidate list (e.g. comment ids). Binary "
                        "gates take their options from the manifest "
                        "(dispositions), never from the caller — what a "
                        "numbered human reply means is declared data (RC3)")

    a = sub.add_parser("artifact", parents=[common], help="record a declared step output")
    a.add_argument("--name", required=True)
    a.add_argument("--value", required=True)

    s = sub.add_parser("stall", parents=[common], help="record an agent stall; returns next action")
    s.add_argument("--task", required=True)

    e = sub.add_parser("log-event", parents=[common], help="append to the audit ledger")
    e.add_argument("--json", required=True)

    sub.add_parser("verify", parents=[common], help="verify the integrity chain")
    sub.add_parser("show", parents=[common], help="print current state")

    ab = sub.add_parser("abort", parents=[common],
                        help="end a run before its terminal step (terminal: "
                             "releases the work-item slot, sweeps worktrees, "
                             "keeps the audit trail — never a deletion)")
    ab.add_argument("--reason", required=True)

    sub.add_parser("complete", parents=[common],
                   help="mark a run that finished its walk as terminal (the "
                        "successful sibling of abort — legal only from the "
                        "mode's final step with every task terminal)")

    rs = sub.add_parser("reseal", parents=[common],
                        help="human-invoked recovery: reseal state.yaml after "
                             "a crash between the content and seal writes "
                             "(never automatic — always logged)")
    rs.add_argument("--reason", required=True)

    return p, sub.choices


def main(argv: list[str] | None = None) -> int:
    p, _ = build_parser()
    args = p.parse_args(argv)
    if args.workspace is None:
        # runs live at <workspace>/ai/<run-name> BY CONSTRUCTION (bootstrap
        # creates them there), so an explicit --run names its own workspace
        # — derive it rather than trusting the process cwd, which drifts
        # (a cd into a repo can mint a stray key there and report genuine
        # state as an integrity mismatch)
        args.workspace = (args.run.resolve().parent.parent if args.run
                          else Path.cwd())
    try:
        manifest, fsm, config = load_declared(args.workspace)
    except (ValueError, yaml.YAMLError, OSError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1
    now = ndjson.now_iso()
    NO_RUN = ("init", "fetch", "provider", "provider-normalize", "discover",
              "ensure-default-branch", "init-verify", "init-section",
              "init-finalize", "add-repo", "migrate-detect", "migrate-extract",
              "status", "repo-map-check", "repo-map-stamp", "validate-mermaid",
              "resolve-model", "resolve-coverage-cmd")
    if args.cmd not in NO_RUN and args.run is None:
        p.error(f"--run is required for '{args.cmd}'")

    try:
        if args.cmd == "init":
            def kv(flag, specs):
                # `--repo myrepo` (no '=') used to die inside dict() with
                # "dictionary update sequence element #0 has length 1" —
                # the most likely first-run typo got the least helpful
                # message (adversarial-review finding).
                for spec in specs:
                    if "=" not in spec:
                        raise ValueError(
                            f"{flag} expects NAME=VALUE (got {spec!r})")
                return dict(spec.split("=", 1) for spec in specs)
            repos = kv("--repo", args.repo)
            test_cmds = kv("--test-cmd", args.test_cmd)
            path = workflow.init_minimal(args.workspace, args.stories_dir,
                                         repos, test_cmds)
            _emit({"ok": True, "config": str(path)})
            return 0

        if args.cmd == "fetch":
            if args.from_raw:
                result = workflow.fetch_from_raw(args.workspace, config,
                                                 manifest, json.load(sys.stdin),
                                                 args.date)
            elif args.id:
                result = workflow.fetch_flow(args.workspace, config, manifest,
                                             args.id, args.date)
            else:
                _emit({"ok": False, "error": "fetch needs --id (cli transport) "
                       "or --from-raw (mcp transport, raw JSON on stdin)"})
                return 1
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "provider":
            from .providers import dispatch
            kwargs = {k: v for k, v in
                      (("id", args.id), ("to", args.to), ("text", args.text),
                       ("title", args.title), ("description", args.description))
                      if v is not None}
            # Validated here, not left to Python's TypeError (adversarial-
            # review finding: `provider --op work_item.transition --id 7`
            # without --to crashed with a raw traceback, outside the JSON
            # error contract).
            required = {"work_item.fetch": ("id",),
                        "work_item.transition": ("id", "to"),
                        "work_item.add_comment": ("id", "text"),
                        "work_item.create": ("title",)}
            missing = [k for k in required.get(args.op, ()) if k not in kwargs]
            if missing:
                raise ValueError(
                    f"provider op '{args.op}' needs "
                    + ", ".join(f"--{k}" for k in missing))
            _emit({"ok": True, "result": dispatch(config, args.op, **kwargs)})
            return 0

        if args.cmd == "provider-normalize":
            from .providers import normalize
            raw = json.load(sys.stdin)
            _emit({"ok": True, "result": normalize(config, args.op, raw)})
            return 0

        if args.cmd == "resolve-model":
            model = workflow.resolve_subagent_model(config, args.shape, args.mode)
            _emit({"ok": True, "model": model})
            return 0

        if args.cmd == "resolve-coverage-cmd":
            from . import initws
            cmd = initws.resolve_coverage_cmd(config, args.repo)
            _emit({"ok": True, "coverage_cmd": cmd})
            return 0

        if args.cmd == "discover":
            from . import initws
            _emit({"ok": True, **initws.discover(args.repo, branch=args.branch)})
            return 0

        if args.cmd == "ensure-default-branch":
            result = gitops.ensure_default_branch(args.repo, args.branch)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "init-verify":
            from . import initws
            checks = initws.verify(config, workspace=args.workspace)
            failed = [c for c in checks if c["status"] == "fail"]
            _emit({"ok": not failed, "checks": checks})
            return 1 if failed else 0

        if args.cmd == "init-section":
            from . import initws
            data = json.loads(args.json)
            if not isinstance(data, dict):
                _emit({"ok": False, "error": "--json must be a JSON object "
                       f"(got {type(data).__name__}) — every section file's "
                       "top-level keys are merged straight into config"})
                return 1
            path = initws.write_section(args.workspace, args.section, data)
            _emit({"ok": True, "written": str(path)})
            return 0

        if args.cmd == "init-finalize":
            from . import initws
            checks = initws.verify(config, workspace=args.workspace)
            failed = [c for c in checks if c["status"] == "fail"]
            if failed:
                _emit({"ok": False, "error": "init-verify has failing checks "
                       "— fix and re-run init-verify before init-finalize",
                       "checks": checks})
                return 1
            repos = config.get("repos") or {}
            language = (config.get("language") or {}).get("repos") or {}
            initws.write_permissions(args.workspace, repos, language)
            initws.mark_bootstrapped(args.workspace)
            _emit({"ok": True})
            return 0

        if args.cmd == "add-repo":
            from . import initws
            try:
                added = initws.add_repo(args.workspace, args.name, args.path,
                                        args.test_cmd)
            except initws.AddRepoError as exc:
                _emit({"ok": False, "error": str(exc)})
                return 1
            _emit({"ok": True, "added": added})
            return 0

        if args.cmd == "migrate-detect":
            from . import migrate
            found = migrate.detect(args.workspace)
            payload = {"ok": True, **found}
            if found["legacy"]:
                payload["inventory"] = migrate.inventory(args.workspace)
            _emit(payload)
            return 0

        if args.cmd == "migrate-extract":
            from . import migrate
            found = migrate.detect(args.workspace)
            # Fail-closed at both ends: extraction is the verb whose output
            # feeds writes, so it refuses where detect merely reports.
            if found["already_bootstrapped"]:
                _emit({"ok": False, "error": "workspace is already "
                       "bootstrapped for v3.0 — adjust individual sections "
                       "via /workspace-config instead of migrating on top",
                       **found})
                return 1
            if not found["legacy"]:
                _emit({"ok": False, "error": "no pre-v3 workspace detected "
                       "here — run /init-workspace for a fresh setup",
                       **found})
                return 1
            _emit({"ok": True, **migrate.extract(args.workspace)})
            return 0

        if args.cmd == "status":
            runs = []
            for sf in sorted((args.workspace / "ai").glob("*/state.yaml")):
                run = sf.parent
                # Per-run isolation (adversarial-review finding): one
                # corrupt/tampered run used to kill the WHOLE dashboard —
                # the one verb meant for orientation after something went
                # wrong was the first to die. Show the failure in place.
                try:
                    with state_mod.locked_read(run):
                        st = state_mod.load(run, args.workspace)
                except (chain.IntegrityError, state_mod.StateError,
                        ValueError) as exc:
                    runs.append({"run": run.name,
                                 "error": f"{type(exc).__name__}: {exc}",
                                 "remediation": "harness reseal --run "
                                                f"{run} --reason <why>"})
                    continue
                # F5 (validation-walk): the shared outstanding-flagged filter
                # pairs resolved deferrals off, so status.flagged_events matches
                # metrics' "## Flagged events (N)" and both are a live gauge.
                flagged = workflow.outstanding_flagged(
                    ndjson.read_records(run / "events.ndjson"))
                runs.append({
                    "run": run.name, "mode": st["mode"],
                    "cursor": st["cursor"]["current_step"],
                    **({"aborted": st["aborted"]} if st.get("aborted") else {}),
                    **({"completed": st["completed"]}
                       if st.get("completed") else {}),
                    "work_item": st["work_item"]["id"],
                    "tasks": {t["id"]: t["status"] for t in st["tasks"]},
                    "provisional_tasks": [t["id"] for t in st["tasks"]
                                          if t.get("provisional")],
                    "gates": {g: v.get("decision") for g, v in st["gates"].items()
                              if v.get("decision")},
                    "flagged_events": len(flagged)})
            _emit({"ok": True, "runs": runs})
            return 0

        if args.cmd == "validate-mermaid":
            result = mermaid.validate_file(args.file)
            _emit({"ok": result["verdict"] != "invalid", **result})
            return 0 if result["verdict"] != "invalid" else 1

        if args.cmd == "repo-map-check":
            from . import initws
            stale_after = (config.get("repo_map") or {}).get("stale_after_commits", 50)
            _emit({"ok": True, **initws.repo_map_check(
                args.workspace, args.repo_name, args.repo, stale_after)})
            return 0

        if args.cmd == "repo-map-stamp":
            from . import initws
            meta = initws.repo_map_stamp(args.workspace, args.repo_name, args.repo)
            _emit({"ok": True, **meta})
            return 0

        if args.cmd == "preflight":
            result = workflow.preflight(args.workspace, args.run, config,
                                        manifest, args.repo, args.branch)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "plan-register":
            tasks = _json_source("--tasks-json", args.tasks_json,
                                 args.tasks_json_file, None)
            if tasks is None:
                raise ValueError(
                    "plan-register needs --tasks-json or --tasks-json-file")
            contracts = _json_source("--contracts-json", args.contracts_json,
                                     args.contracts_json_file, [])
            result = workflow.plan_register(args.workspace, args.run, manifest,
                                            tasks, contracts)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "worktree-add":
            with state_mod.locked(args.run):
                st = state_mod.load(args.run, args.workspace)
                # never re-create a worktree abort just swept (leaks it —
                # reconcile refuses to clean it up; adversarial-review finding)
                transitions.ensure_live(st, "worktree-add")
                task = next((t for t in st["tasks"] if t["id"] == args.task_id), None)
                if task is None:
                    raise state_mod.StateError(f"unknown task '{args.task_id}'")
                recorded = task.get("worktree")
                # Idempotent resume (charter) — but only if the recorded
                # path still actually exists. Adversarial-review finding:
                # a worktree deleted on disk (manual cleanup, disk-space
                # script, crash) while still recorded in state used to
                # "resume" straight to a dead path with no existence check
                # at all — every subsequent command against it then failed
                # with a confusing raw git error instead of a clear one.
                if recorded and Path(recorded["path"]).is_dir():
                    _emit({"ok": True, "resumed": True, **recorded})
                    return 0
                wt = gitops.worktree_add(args.repo, args.task_id, args.base)
                task["worktree"] = wt
                state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True, "resumed": False, **wt})
            return 0

        if args.cmd == "worktree-remove":
            with state_mod.locked(args.run):
                st = state_mod.load(args.run, args.workspace)
                transitions.ensure_live(st, "worktree-remove")
                task = next((t for t in st["tasks"] if t["id"] == args.task_id), None)
                if task and task.get("worktree"):
                    gitops.worktree_remove(args.repo, task["worktree"])
                    task["worktree"] = None
                    state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True})
            return 0

        if args.cmd == "quick-recheck":
            verdict = workflow.quick_recheck(args.workspace, args.run, config,
                                             manifest, args.repo, args.base)
            _emit({"ok": True, "verdict": verdict})
            return 0

        if args.cmd == "security-scan":
            sev = workflow.security_scan(args.workspace, args.run, config, manifest)
            _emit({"ok": True, "max_severity": sev})
            return 0

        if args.cmd == "reconcile-contracts":
            repos = {k: v for k, v in (config.get("repos") or {}).items()}
            verdict = workflow.reconcile_contracts(args.workspace, args.run,
                                                   config, repos)
            _emit({"ok": True, "verdict": verdict})
            return 0

        if args.cmd == "create-pr":
            pr = workflow.create_pr(args.workspace, args.run, config, manifest,
                                    args.repo, manual_url=args.url)
            _emit({"ok": True, **pr})
            return 0

        if args.cmd == "fetch-pr-comments":
            from .providers import git_providers
            from . import initws
            # Read-only, but not lock-free (adversarial-review round 3
            # finding: this new command read state with no lock at all —
            # the exact torn-read race `locked_read` exists to close).
            with state_mod.locked_read(args.run):
                st = state_mod.load(args.run, args.workspace)
            name = initws.repo_name(config, args.repo) or str(args.repo)
            pr = ((st.get("artifacts") or {}).get("pr") or {}).get(name)
            if pr is None:
                raise ValueError(f"no 'pr' artifact recorded for repo '{name}' "
                                 "— run create-pr first")
            comments = git_providers.fetch_pr_comments(config, repo=args.repo, pr=pr)
            _emit({"ok": True, "comments": comments})
            return 0

        if args.cmd == "reconcile":
            result = workflow.reconcile_flow(args.workspace, args.run, config, fsm,
                                             manifest,
                                             skip_transition=args.skip_transition)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "abort":
            result = workflow.abort_run(args.workspace, args.run, args.reason)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "complete":
            result = workflow.complete_run(args.workspace, args.run, manifest)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "write-back":
            result = workflow.write_back(args.workspace, args.run, config,
                                         args.milestone)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "metrics":
            path = workflow.metrics_report(args.workspace, args.run, manifest)
            _emit({"ok": True, "report": str(path)})
            return 0

        if args.cmd == "reseal":
            # Deliberately does NOT call state_mod.load() — that raises
            # IntegrityError on exactly the condition this recovers from.
            if not state_mod.state_path(args.run).exists():
                # Refused BEFORE state_mod.locked()'s unconditional mkdir —
                # a typo'd --run must not leave a stray directory behind
                # (the same bug class locked_read's no-mkdir design closed
                # for show/verify/status; reintroduced by this verb in the
                # first pass, caught by re-review).
                raise state_mod.StateError(
                    f"no run at {args.run} — nothing to reseal")
            key = chain.load_key(args.workspace)
            with state_mod.locked(args.run):
                result = chain.reseal(state_mod.state_path(args.run), key)
            ndjson.append_record(args.run / "events.ndjson",
                                 {"kind": "reseal", "reason": args.reason,
                                  "seq": result["seq"]})
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "bootstrap":
            # split(":", 1), not split(":"): a repo path may itself contain
            # colons (Windows drive letters — `T1:C:\repos\x` silently
            # recorded repo "C" before, adversarial-review finding)
            tasks = [{"id": spec.split(":", 1)[0],
                      "repo": spec.split(":", 1)[1] if ":" in spec else "."}
                     for spec in (args.task or ["T1"])]
            st = state_mod.bootstrap(
                args.run, args.workspace,
                work_item={"id": args.work_item_id, "title": args.title,
                           "provider_ref": args.provider_ref},
                mode=args.mode, change_type=args.change_type, tasks=tasks,
                entry_step=manifest["entry"], manifest=manifest)
            _emit({"ok": True, "cursor": st["cursor"]["current_step"]})
            return 0

        if args.cmd == "log-event":
            with state_mod.locked_read(args.run):
                transitions.ensure_live(
                    state_mod.load(args.run, args.workspace), "log-event")
            record = ndjson.append_record(args.run / "events.ndjson",
                                          json.loads(args.json))
            _emit({"ok": True, "at": record["at"]})
            return 0

        if args.cmd == "verify-red":
            from . import initws
            with state_mod.locked_read(args.run):  # torn-read guard
                pre = state_mod.load(args.run, args.workspace)
            transitions.ensure_live(pre, "verify-red")  # don't seal onto a dead run
            vr_task = next((t for t in pre["tasks"] if t["id"] == args.task), None)
            # Resolve via the task's REGISTERED repo (matches repos.yaml), not
            # args.repo — which is the per-task worktree path during develop
            # (M5 charter) and can never match a registered repo name.
            test_cmd = args.test_cmd or (
                initws.resolve_test_cmd(config, vr_task["repo"]) if vr_task else None)
            if not test_cmd:
                raise gitops.RedProofError(
                    "no test command — pass --test-cmd or configure "
                    "language.repos.<repo-name>.test_cmd")
            proof = gitops.verify_red(args.run, args.workspace, args.repo, config,
                                      args.task, test_cmd, args.tests, args.intents,
                                      args.revise, args.reason)
            _emit({"ok": True, "red": True, "tests": sorted(proof["tests"]),
                   "locked_closure": sorted(proof["closure"]),
                   "declared_intents": sorted(proof["declared_intents"]),
                   "missing_intents": sorted(proof["missing_intents"])})
            return 0

        if args.cmd == "show-redproof":
            path = transitions.redproof_path(args.run, args.task)
            if not path.exists():
                raise gitops.RedProofError(
                    f"no red-proof for task {args.task} — run verify-red first")
            key = chain.load_key(args.workspace)
            with state_mod.locked_read(args.run):  # torn-read guard
                proof = json.loads(chain.verify(
                    path, key, label=transitions.redproof_label(args.task)))
            _emit({"ok": True, "task": proof["task"],
                   "tests": sorted(proof["tests"]),
                   "declared_intents": sorted(proof.get("declared_intents", [])),
                   "missing_intents": sorted(proof.get("missing_intents", []))})
            return 0

        if args.cmd == "commit":
            try:
                if args.fixup_of:
                    with state_mod.locked_read(args.run):  # torn-read guard
                        st = state_mod.load(args.run, args.workspace)
                    target = next((t["commit_sha"] for t in st["tasks"]
                                   if t["id"] == args.fixup_of and t.get("commit_sha")),
                                  args.fixup_of)
                    sha = gitops.commit_fixup(args.repo, target)
                else:
                    sha = gitops.commit_class(args.repo, config, args.commit_class,
                                              task=args.task_id, summary=args.summary)
            except gitops.SecretSweepError as exc:
                # dashboard-visible, not just a refused command: a stray key
                # inside a repo means a wrong---workspace invocation happened
                # somewhere and left litter — the run's human should see it
                if args.run is not None:
                    try:
                        ndjson.append_record(
                            args.run / "events.ndjson",
                            {"kind": "secret-sweep-blocked",
                             "repo": str(args.repo), "reason": str(exc)[:300]})
                    except OSError:
                        pass
                raise
            _emit({"ok": True, "sha": sha})
            return 0

        if args.cmd == "publish-mirror":
            sha = gitops.publish_mirror(args.repo, args.run, config, args.run.name)
            out = {"ok": True, "sha": sha}
            if args.push:
                # The final mirror's purpose is the PR's audit trail — and a
                # local-only snapshot never gets there (field finding: the
                # push -> create-pr -> publish-mirror sequence left EVERY
                # run's last mirror commit stranded one ahead of the
                # remote). Pushed even when the mirror itself was a no-op
                # sha=None: an earlier unpushed mirror still needs to land.
                branch = gitops.run_git(args.repo, "rev-parse",
                                        "--abbrev-ref", "HEAD")
                gitops.push_branch(args.repo, branch)
                out["pushed"] = branch
            _emit(out)
            return 0

        if args.cmd == "sync-branch":
            gitops.sync_branch(args.repo, args.onto)
            _emit({"ok": True, "synced_onto": args.onto})
            return 0

        if args.cmd == "push":
            gitops.push_branch(args.repo, args.branch, args.force_with_lease)
            _emit({"ok": True, "pushed": args.branch,
                   "force_with_lease": args.force_with_lease})
            return 0

        if args.cmd == "merge-task":
            with state_mod.locked_read(args.run):  # torn-read guard; the
                # mutating re-read below re-takes the EXCLUSIVE lock — this
                # only protects the pre-read (task SHAs / naming context)
                st = state_mod.load(args.run, args.workspace)
            transitions.ensure_live(st, "merge-task")
            if args.autosquash:
                if not args.base:
                    raise gitops.GitError("--autosquash requires --base")
                # Scope the SHA map to THIS repo's tasks (field report: the
                # unfiltered map swept every task in state.yaml, so on any
                # multi-repo run the `git log` below ran a sibling repo's
                # SHA in args.repo and crashed). Resolved-path comparison —
                # the same spelling-variance stance as initws.repo_name; a
                # task with no/'.' repo (pre-registration seed, unit
                # fixtures) keeps the old include-it behavior, a shape only
                # single-repo runs produce.
                repo_r = args.repo.resolve()
                old = {t["id"]: t["commit_sha"] for t in st["tasks"]
                       if t.get("commit_sha")
                       and (t.get("repo") in (None, ".")
                            or Path(t["repo"]).resolve() == repo_r)}
                subjects = {tid: gitops.run_git(args.repo, "log", "-1",
                                                "--format=%s", sha)
                            for tid, sha in old.items()}
                gitops.autosquash(args.repo, args.base)
                with state_mod.locked(args.run):
                    st = state_mod.load(args.run, args.workspace)
                    for task in st["tasks"]:
                        if task["id"] in subjects:  # SHA re-derivation (B10)
                            task["commit_sha"] = gitops.find_commit_by_subject(
                                args.repo, args.base, subjects[task["id"]])
                    state_mod.save(args.run, args.workspace, st)
                _emit({"ok": True, "autosquashed": True})
                return 0
            if not (args.task_id and args.task_branch):
                raise gitops.GitError("merge-task needs --task-id and --task-branch")
            message = gitops.render(config["naming"]["commit"]["integration"],
                                    type=st["change_type"],
                                    id=st["work_item"]["id"], summary=args.summary)
            sha = gitops.squash_merge(args.repo, args.task_branch, message)
            with state_mod.locked(args.run):
                st = state_mod.load(args.run, args.workspace)
                for task in st["tasks"]:
                    if task["id"] == args.task_id:
                        task["commit_sha"] = sha
                state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True, "sha": sha})
            return 0

        if args.cmd in ("verify", "show"):
            # Read-only, but NOT lock-free (adversarial-review round 2
            # finding: an earlier version of this fix dropped locking
            # entirely, reasoning that atomic-replace alone made a bare read
            # safe — it doesn't; chain.seal()'s content-then-seal write is
            # two separate atomic replaces, and an unlocked reader landing
            # between them raises a spurious IntegrityError). `locked_read`
            # takes a SHARED lock (blocks only against a concurrent
            # exclusive writer) and — unlike `locked()` — never mkdirs, so
            # a typo'd `--run` path still gets a clean refusal from
            # `load()` instead of a stray directory.
            with state_mod.locked_read(args.run):
                st = state_mod.load(args.run, args.workspace)
            _emit({"ok": True, "state": st} if args.cmd == "show"
                  else {"ok": True, "seq_verified": True})
            return 0

        # Expensive verify-green test run happens OUTSIDE the lock (RC4);
        # only the cheap SHA re-check repeats inside it.
        verify_ctx = None
        if args.cmd == "task" and args.to == "in-review":
            with state_mod.locked_read(args.run):  # torn-read guard; the
                # verify-green TEST RUN below stays outside every lock
                # (RC4), and the transition itself re-takes the exclusive
                # lock with a cheap in-lock SHA re-check
                pre = state_mod.load(args.run, args.workspace)
            # Activation mirrors _guard_red_proof exactly: the task's own
            # declared test_intents, never a mode/step-name pair — so a new
            # manifest mode gets the full TDD checkpoint for free wherever
            # its tasks declare intents (quick's intent-less seed task and
            # the plan-approved `test_intents: []` opt-out stay exempt).
            task = next((t for t in pre["tasks"] if t["id"] == args.id), None)
            if task and task.get("test_intents"):
                from . import initws
                repo = args.repo or (Path(task["repo"]) if task else None)
                test_cmd = args.test_cmd or (
                    initws.resolve_test_cmd(config, task["repo"]) if task else None)
                if not repo or not Path(repo).is_dir() or not test_cmd:
                    raise transitions.TransitionError(
                        "completing a task with declared test-intents requires "
                        "--repo and --test-cmd (or configured "
                        "language.repos.<repo-name>.test_cmd) — fail closed")
                proof_path = transitions.redproof_path(args.run, args.id)
                if proof_path.exists():
                    key = chain.load_key(args.workspace)
                    with state_mod.locked_read(args.run):  # torn-read guard
                        proof = json.loads(chain.verify(
                            proof_path, key,
                            label=transitions.redproof_label(args.id)))
                    gitops.verify_green(proof, Path(repo), test_cmd, run_tests=True)
                verify_ctx = {"repo": Path(repo), "run_tests": False}

        with state_mod.locked(args.run):
            st = state_mod.load(args.run, args.workspace)
            key = chain.load_key(args.workspace)
            transitions.ensure_live(st, args.cmd)
            extra: dict = {}   # verb-specific fields for the shared emit

            if args.cmd == "cursor":
                skipped = transitions.advance_cursor(st, manifest, config,
                                                     args.to, now)
                for s in skipped:
                    # a conditional gate skipped by its declared predicate is
                    # an evaluation, not an omission — the ledger must be able
                    # to prove it happened (e2e E2E-1: approve-security's
                    # silent self-skip was indistinguishable from a hole)
                    ndjson.append_record(args.run / "events.ndjson",
                                         {"kind": "gate-skipped", **s})
            elif args.cmd == "task":
                transitions.transition_task(st, fsm, config, args.run, key,
                                            args.id, args.to, args.context,
                                            verify_ctx)
            elif args.cmd == "gate":
                # The option list — what a numbered human reply MEANS — is
                # never caller-defined (adversarial-review finding: a
                # caller-supplied `--options` let a drifting orchestrator
                # reorder the list at decide time, so the human's "1" for
                # `fix-now` recorded as `waive`). Binary gates read the
                # manifest's declared `dispositions`; a `select` gate's
                # candidate set is runtime data, so it is supplied ONCE at
                # --present and sealed into state — decide always replays
                # the sealed list.
                gate_def = manifest["steps"].get(args.id) or {}
                if not gate_def.get("gate"):
                    raise gates.GateRefusal(
                        f"'{args.id}' is not a declared gate step in the manifest")
                is_select = bool(gate_def.get("select"))
                if args.present:
                    if is_select:
                        if not args.options:
                            raise gates.GateRefusal(
                                f"select gate '{args.id}' needs --options at "
                                "--present: the runtime candidate list "
                                "(e.g. comment ids)")
                        options = [o.strip() for o in args.options.split(",")]
                    else:
                        if args.options is not None:
                            raise gates.GateRefusal(
                                f"gate '{args.id}': options are declared in the "
                                "manifest (dispositions) — --options is legal "
                                "only for select gates")
                        options = list(gate_def.get("dispositions")
                                       or ["approved", "rejected"])
                    gates.present(st, args.id, now, options)
                else:
                    if args.options is not None:
                        raise gates.GateRefusal(
                            f"gate '{args.id}': --options is never legal at "
                            "--decide — the decision replays the option list "
                            "sealed at --present (RC3: the caller must not "
                            "define what a numbered reply means)")
                    entry_now = st["gates"].get(args.id) or {}
                    options = entry_now.get("options") or list(
                        gate_def.get("dispositions") or ["approved", "rejected"])
                    # strict: a torn newest reply fails closed, never lets an
                    # older qualifying reply win (adversarial-review finding)
                    try:
                        records = ndjson.read_records(
                            args.run / "human-input.ndjson", strict=True)
                    except ndjson.LedgerCorruption as exc:
                        raise gates.GateRefusal(
                            f"gate '{args.id}': human-input ledger has a "
                            f"corrupt record — re-reply to heal it: {exc}"
                        ) from exc
                    # rejection-side replies may carry notes after the
                    # option word; forward decisions stay bare (gates.py
                    # parse_decision — the manifest's forward_on names
                    # which options move the pipeline)
                    forward = set(gate_def.get("forward_on")
                                  or transitions.FORWARD_DEFAULT)
                    entry = gates.decide(
                        st, args.id, records, options, now, multi=is_select,
                        lenient=frozenset(o for o in options
                                          if o not in forward))
                    ndjson.append_record(args.run / "events.ndjson",
                                         {"kind": "gate-decision", "gate": args.id,
                                          "decision": entry["decision"],
                                          "options": options,
                                          "evidence": entry["evidence"]})
                    extra["decision"] = entry["decision"]
                    if entry["decision"] == "defer":
                        # The follow-through rides the RESULT the
                        # orchestrator actually reads, and the pending event
                        # marks the deferral in the ledger THE INSTANT it is
                        # decided (field, session D: the follow-through was
                        # done correctly 43s after the decide — but an audit
                        # snapshot inside that window was indistinguishable
                        # from a silent drop; pending/recorded make
                        # "in flight" vs "done" vs "dropped" three
                        # distinguishable ledger states).
                        extra["follow_up"] = (
                            "defer requires the follow-up work item NOW: run "
                            "`provider --op work_item.create --title "
                            "'<summary>' --description '<finding + repo + "
                            "severity>'`, then log-event "
                            '`{"kind": "deferral-recorded", "item": "<id>"}` '
                            "(steps/gate.md step 6)")
                        ndjson.append_record(
                            args.run / "events.ndjson",
                            {"kind": "deferral-pending", "gate": args.id,
                             "reason": "defer decided — follow-up work item "
                                       "not yet recorded"})
            elif args.cmd == "artifact":
                transitions.set_artifact(st, manifest, args.name, args.value)
            elif args.cmd == "stall":
                action = transitions.record_stall(st, config, args.task)
                ndjson.append_record(args.run / "events.ndjson",
                                     {"kind": "stall", "task": args.task,
                                      "action": action})
                state_mod.save(args.run, args.workspace, st)
                _emit({"ok": True, "action": action})
                return 0

            state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True, "cursor": st["cursor"]["current_step"],
                   "mode": st["mode"], **extra})
            return 0

    except chain.IntegrityError as exc:
        _emit({"ok": False, "integrity": False, "error": str(exc)})
        return 3
    except (transitions.TransitionError, gates.GateRefusal,
            state_mod.StateError, state_mod.CollisionError,
            gitops.GitError, gitops.RedProofError, mermaid.MermaidError,
            ProviderError, ValueError,
            # Boundary failures must land in the JSON error contract too
            # (adversarial-review finding: a missing gh/glab binary
            # [FileNotFoundError], a CLI timeout [SubprocessError], or a
            # typo'd --tasks-json-file path [OSError] each dumped a raw
            # traceback the orchestrating skill can't parse).
            OSError, subprocess.SubprocessError) as exc:
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
