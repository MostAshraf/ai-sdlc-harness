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
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from . import chain, gates, gitops, mermaid, ndjson, qwen_cli_detected, state as state_mod, transitions, workflow
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


def _has_non_inherit_model(sm) -> bool:
    """True if a `subagent_models` value contains any non-`inherit`
    override — handles both the string form (``"sonnet"``) and the
    per-mode dict form (``{"default": "sonnet", "review": "inherit"}``).
    Any value != "inherit" counts, at any depth in the dict."""
    if not sm:
        return False
    if isinstance(sm, str):
        return sm != "inherit"
    if isinstance(sm, dict):
        return any(_has_non_inherit_model(v) for v in sm.values())
    return False


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


def _merge_task(args, config: dict, st: dict) -> str | None:
    """The body of `merge-task`, executed entirely INSIDE the exclusive run
    lock (see the call site for why that moved). `st` is the caller's
    already-loaded state, mutated in place for the caller's single save —
    there is no second read, so there is no window between deciding the SHA
    and recording it. Returns the new integration SHA, or None for the
    autosquash form (which re-derives every task's existing SHA rather than
    adding one).

    Autosquash holds the same lock for a blunter reason: it REWRITES every
    integration commit on the branch, so a sibling task's merge landing
    mid-rebase would be silently dropped from the rewritten history — and
    the SHA re-derivation below would then record a subject-matched commit
    for a task whose real work is gone. It also holds the lock LONGEST: an
    interactive rebase over a branch's whole history is the longest git
    operation this verb can run, so every other run-scoped verb queues
    behind it (state.py's LOCK_WAIT_BUDGET is what makes that a wait
    instead of a crash)."""
    from . import initws
    # WHICH branch this run's work lives on is a fact of the RUN, not of the
    # checkout: preflight cut the feature branch and recorded it per repo.
    # Reading it here (rather than trusting whatever HEAD is) is what lets
    # gitops refuse an operation aimed at the wrong branch — the caller
    # cannot state an expectation it never had. An unrecorded repo is a
    # refusal, never a guess: the two ways to get here are a --repo that
    # names a different repo than the run cut a branch in, and a run whose
    # preflight never happened; guessing "whatever is checked out" would
    # re-open exactly the hole the expectation closes.
    #
    # BOTH FORMS read it (adversarial review, round 4). The autosquash form
    # used to return before this lookup ever ran, so it inherited none of the
    # protection — measured: a `--autosquash` issued while the shared
    # checkout sat on `main` rebased MAIN, and the SHA re-derivation below
    # then matched a same-subject commit that was not the task's and wrote it
    # into state.yaml. A rewrite of every commit on the branch has strictly
    # more to lose from a wrong HEAD than a single squash does.
    name = initws.repo_name(config, args.repo) or str(args.repo)
    branches = (st.get("artifacts") or {}).get("branches") or {}
    recorded = (branches.get(name) or {}).get("branch")
    if not recorded:
        raise gitops.GitError(
            f"merge-task: no feature branch recorded for repo '{name}' — this "
            "run's `branches` artifact covers: "
            f"{', '.join(sorted(branches)) or 'none'}. preflight is what cuts "
            "and records it; merge-task will not guess which branch a task's "
            "work integrates onto.")
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
        gitops.autosquash(args.repo, args.base, recorded)
        for task in st["tasks"]:
            if task["id"] in subjects:  # SHA re-derivation (B10)
                task["commit_sha"] = gitops.find_commit_by_subject(
                    args.repo, args.base, subjects[task["id"]])
        return None
    if not (args.task_id and args.task_branch):
        raise gitops.GitError("merge-task needs --task-id and --task-branch")
    message = gitops.render(config["naming"]["commit"]["integration"],
                            type=st["change_type"],
                            id=st["work_item"]["id"], summary=args.summary)
    sha = gitops.squash_merge(args.repo, args.task_branch, message, recorded)
    for task in st["tasks"]:
        if task["id"] == args.task_id:
            task["commit_sha"] = sha
    return sha


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
                    help="override the auto-resolved default branch — this is "
                         "the BASE the feature branch is cut FROM, not the "
                         "feature branch's own name")
    pf.add_argument("--feature-branch-suffix", default=None,
                    help="suffix the FEATURE branch's rendered name (branch "
                         "aside) — the declared remedy when a prior run of "
                         "this work item already claimed the deterministic "
                         "name on the remote (field: dual-run comparison)")

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

    ub = sub.add_parser("update-base", parents=[common],
                        help="owned fast-forward of the BASE branch onto its "
                             "remote (the remedy for a `behind` count)")
    ub.add_argument("--repo", type=Path, required=True)
    ub.add_argument("--branch", default=None,
                    help="override the auto-resolved default branch — the "
                         "BASE to fast-forward, never a feature branch "
                         "(that is `sync-branch`)")

    bc = sub.add_parser("base-check", parents=[common],
                        help="read-only base-branch freshness for one repo "
                             "(plan step 0) — reports, never refuses")
    bc.add_argument("--repo", type=Path, required=True)
    bc.add_argument("--branch", default=None,
                    help="override the auto-resolved default branch")

    rm = sub.add_parser("resolve-model", parents=[common],
                        help="resolve the model override for a shape/mode "
                             "spawn (subagent_models)")
    rm.add_argument("--shape", required=True)
    rm.add_argument("--mode", required=True)

    sub.add_parser("resolve-lenses", parents=[common],
                   help="resolve the plan-review lens panel for a run's "
                        "change_type (plan_review.lenses overlaid by "
                        "lenses_by_change_type)")

    sr = sub.add_parser("save-report", parents=[common],
                        help="persist a read-only reviewer's report (stdin) "
                             "to its canonical <run>/reports/ path, plus "
                             "this round's snapshot")
    sr.add_argument("--mode", required=True,
                    help="plan-attack | plan-review | pre-pr (per-task "
                         "review verdicts are hook-captured, not persisted)")
    sr.add_argument("--lens", default=None,
                    help="panel member name (required for lens modes) — "
                         "becomes part of the filename")
    sr.add_argument("--round", type=int, default=None, dest="round_n",
                    help="this round's immutable snapshot (<name>-r<N>.md); "
                         "omitted, it is derived from the run's own ledger — "
                         "plan modes count plan re-registrations, pre-pr "
                         "counts approve-pre-pr rejections")
    sr.add_argument("--body-file", type=Path, default=None,
                    help="read the report from this file instead of stdin — "
                         "the preferred form: a report on the command line "
                         "breaks on its own apostrophes AND trips the bash "
                         "guard whenever it quotes a run-authority path or a "
                         "markdown blockquote")

    ec = sub.add_parser("env-check", parents=[common],
                        help="probe the environment prerequisites the plan "
                             "declared (`env_requires`) BEFORE the developer "
                             "spawn; refuses on a missing one")
    ec.add_argument("--task", default=None,
                    help="scope to one task; omit for every non-terminal "
                         "task in the run")

    rtc = sub.add_parser("resolve-test-cmd", parents=[common],
                         help="resolve the per-repo test command "
                              "(language.repos.<name>.test_cmd) with any "
                              "declared quarantine exclusions applied — the "
                              "owned way to build a `harness-test-cmd` "
                              "header, so an agent-run suite excludes the "
                              "same specs the harness-run one does")
    rtc.add_argument("--repo", type=Path, required=True)

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

    sr = sub.add_parser("scope-register", parents=[common],
                        help="record the user-confirmed target-repo scope")
    sr.add_argument("--repos-json", required=True,
                    help="JSON array of registered repo PATHS (config repos "
                         "values) the human confirmed for this run")

    cr_ = sub.add_parser("confirm-repo", parents=[common],
                         help="ratify which registered repo a quick run targets")
    cr_.add_argument("--repo", required=True,
                     help="the registered repo PATH (config repos value) the "
                          "human confirmed for this run")
    cr_.add_argument("--basis", default=None,
                     help="one line of evidence for the choice (repo-map "
                          "index, files the item names) — recorded in state "
                          "and the ledger")

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

    # Read-only, and takes no flags on purpose: the dispatch picture is a
    # property of the run's task DAG, not something a caller narrows or
    # filters. Every filter would be a place for the orchestrator's own idea
    # of readiness to creep back in.
    sub.add_parser("ready-tasks", parents=[common],
                   help="the dispatch picture: which tasks are ready now, "
                        "which are in flight, which are blocked on what")

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
    g.add_argument("--re-present", action="store_true",
                   help="with --present: re-stamp the window even though "
                        "un-decided replies are waiting, discarding them. For "
                        "a reply that genuinely cannot decide (a qualified "
                        "\"APPROVED but…\") after resolving it with the user")
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
    s.add_argument("--task", default=None,
                   help="task id for a per-task spawn; omit for a task-less "
                        "spawn (plan-review, pre-pr, …) — the stall is then "
                        "counted per current step, same declared bounds")
    s.add_argument("--confirm-no-verdict", action="store_true",
                   help="record the stall even though this step's verdict "
                        "ledger holds a verdict for the current round — the "
                        "escape hatch for a spawn that genuinely stalled "
                        "AFTER the capture (field: dual-run comparison)")

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
    # Minted here for the same reason `now` is: the session identity is
    # ambient process context, and the boundary is the one honest place to
    # read it — `harness/gates.py` stays a pure library (no `os`, no I/O)
    # that its unit tests drive with literal dicts. Empirically confirmed
    # (Claude Code 2.1.246): the value the platform exports to a Bash/CLI
    # subprocess here is the SAME string the UserPromptSubmit hook payload
    # carries as `session_id`, which is what makes the two comparable at
    # all. It feeds the gate command twice, for two different jobs:
    #   `gates.present(..., session=)`  — stamp the gate with the session
    #       that PRESENTED it, so a reply typed there is recognizable later;
    #   `gates.decide(..., deciding_session=)` — name the session DECIDING
    #       right now, which qualifies a reply typed here even when the
    #       gate was presented under an older session id (a resumed
    #       session), since `--decide` is run BY the session driving the run.
    # `or None` normalizes an empty export to "unknown", which is never an
    # identity that can never match: at `--decide` it means "do not filter
    # on this" (fail-open), and at `--present` it means "write no stamp" —
    # except over an EXISTING stamp, which `gates.present` refuses to clear
    # rather than silently disarming the gate.
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    NO_RUN = ("init", "fetch", "provider", "provider-normalize", "discover",
              "ensure-default-branch", "update-base", "init-verify", "init-section",
              "init-finalize", "add-repo", "migrate-detect", "migrate-extract",
              "status", "repo-map-check", "repo-map-stamp", "validate-mermaid",
              "resolve-model", "resolve-coverage-cmd", "resolve-test-cmd")
    if args.cmd not in NO_RUN and args.run is None:
        p.error(f"--run is required for '{args.cmd}'")

    try:
        if (args.cmd in ("resolve-test-cmd", "resolve-coverage-cmd")
                and args.run is not None
                and not state_mod.state_path(args.run).exists()):
            # `--run` is OPTIONAL on these two (they resolve a command with
            # or without a run in scope), which is why they sit in NO_RUN and
            # skip the required-run check above — but when one IS given it
            # must name a real run. `quarantine_cmd` appends the
            # `tests-quarantined` flagged event through `ndjson.append_record`,
            # whose `mkdir(parents=True)` will happily build an entire phantom
            # run directory out of a typo'd path and return `ok: true`, while
            # the REAL run never receives the event and the exclusions apply
            # invisibly — the one thing this mechanism must not do.
            #
            # save_report closed exactly this hazard and called itself "the
            # one run-scoped verb that used to skip this check"; the
            # whole-branch adversarial pass reproduced it on both of these
            # (which the step files now mandate `--run` for), leaving that
            # comment false by two verbs.
            raise state_mod.StateError(
                f"{args.run} is not a run (no state.yaml) — check --run; "
                "refusing to manufacture a phantom run directory")
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
            if qwen_cli_detected() and model != "inherit":
                _emit({"ok": True, "model": "inherit", "configured": model,
                       "notice": f"subagent_models configured '{model}' for "
                       f"{args.shape}, but Qwen Code's agent tool has no "
                       "model parameter — the override cannot be applied at "
                       "spawn; the subagent runs on the session model. It "
                       "will apply in Claude Code sessions of this workspace."})
            else:
                _emit({"ok": True, "model": model})
            return 0

        if args.cmd == "resolve-lenses":
            with state_mod.locked_read(args.run):   # torn-read guard
                st = state_mod.load(args.run, args.workspace)
            lenses = workflow.resolve_lenses(config, st.get("change_type"))
            _emit({"ok": True, "change_type": st.get("change_type"),
                   "lenses": lenses})
            return 0

        if args.cmd == "save-report":
            # A file or stdin, never an argument: a report is multi-line
            # prose, and putting it on the command line both breaks on its
            # own apostrophes and trips the bash guard the moment it quotes
            # a run-authority path or a markdown blockquote (the guard reads
            # `>` as a redirect). Same file-form precedent as
            # `--tasks-json-file`.
            body = (args.body_file.read_text(encoding="utf-8")
                    if args.body_file else sys.stdin.read())
            result = workflow.save_report(args.run, args.mode, body,
                                          args.lens, args.round_n)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "env-check":
            result = workflow.env_check(args.workspace, args.run, config,
                                        args.task)
            # Refuse (exit 1) rather than reporting ok:false at exit 0 — the
            # develop step branches on the exit code, and a prerequisite the
            # human has to fix is exactly the "refused" contract.
            _emit({"ok": not result["missing"], **result})
            return 1 if result["missing"] else 0

        if args.cmd == "resolve-test-cmd":
            # The owned resolution the `harness-test-cmd` header is built
            # from. Its absence was the hole in the quarantine mechanism
            # (adversarial-review): the harness applied exclusions to the
            # suites IT ran (verify-red/green), while develop/review/pre-pr/
            # harden handed agents a raw config value to run themselves — so
            # the reviewer re-running the suite still hit the pre-existing
            # failure and issued CHANGES_REQUESTED, which is the field loop
            # the quarantine exists to end.
            from . import initws
            cmd = initws.resolve_test_cmd(config, args.repo)
            if cmd:
                cmd = initws.quarantine_cmd(config, args.repo, cmd, args.run)
            _emit({"ok": True, "test_cmd": cmd})
            return 0

        if args.cmd == "resolve-coverage-cmd":
            from . import initws
            cmd = initws.resolve_coverage_cmd(config, args.repo)
            if cmd:
                # Coverage is the OTHER path the quarantined spec kept
                # aborting (field: dual-run comparison — three times
                # in one run), so it gets the same exclusions the test
                # command does. `--run` is optional on this verb, so the
                # flagged event is only appended when a run is in scope.
                cmd = initws.quarantine_cmd(config, args.repo, cmd, args.run,
                                            coverage=True)
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

        if args.cmd == "update-base":
            # `advanced: false` is a legitimate success (already current), so
            # the caller must read that field rather than the exit code to
            # know whether anything moved — every refusal path raises instead.
            _emit({"ok": True, **gitops.update_base(args.repo, args.branch)})
            return 0

        if args.cmd == "base-check":
            result = workflow.base_check(args.workspace, args.run, config,
                                         args.repo, args.branch)
            # Exit 0 even when stale — deliberately NOT env-check's refusal
            # contract. This surfaces a decision for the human at the plan
            # gate; it is not a gate itself, and a plan step that hard-failed
            # on an upstream commit would be a new blocker nobody asked for.
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
            result = {"ok": True, "written": str(path)}
            if (qwen_cli_detected()
                    and args.section == "overrides"):
                sm = data.get("subagent_models")
                if _has_non_inherit_model(sm):
                    result["notice"] = (
                        "subagent_models contains one or more overrides that "
                        "are stored but inert under Qwen Code (its agent tool "
                        "has no model parameter — subagents run on the session "
                        "model). They will apply in Claude Code sessions of "
                        "this workspace.")
            _emit(result)
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
                events = ndjson.read_records(run / "events.ndjson")
                flagged = workflow.outstanding_flagged(events)
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
                    # a consumed decision (single-use, cleared when its edge
                    # fires) still belongs on the dashboard as history
                    "gates": {g: v.get("decision") or v.get("consumed_decision")
                              for g, v in st["gates"].items()
                              if v.get("decision") or v.get("consumed_decision")},
                    "flagged_events": len(flagged),
                    # process health, not content: HEALTHY unless the run
                    # machinery degraded — evidence-loss events or an
                    # engaged stall procedure (shared rule —
                    # workflow.run_health, the same one metrics' "## Run
                    # health" section reads). The cursor goes in because a
                    # spawn still in flight IN THE CURRENT step is healthy
                    # pipelining, not lost evidence (round 4); the round
                    # anchor goes in with it because a step name repeats
                    # across an `on_reject` bounce, and the verdict FLAPPED
                    # back to HEALTHY when it did.
                    "health": workflow.run_health(
                        events, workflow.stall_count(st),
                        st["cursor"]["current_step"],
                        transitions.latest_gate_decision(st))[0]})
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
                                        manifest, args.repo, args.branch,
                                        args.feature_branch_suffix)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "scope-register":
            try:
                repos = json.loads(args.repos_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"--repos-json is not valid JSON: {exc}") from exc
            result = workflow.scope_register(args.workspace, args.run,
                                             manifest, config, repos)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "confirm-repo":
            result = workflow.confirm_repo(args.workspace, args.run, manifest,
                                           config, args.repo, args.basis)
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
                                            tasks, contracts, config)
            _emit({"ok": True, **result})
            return 0

        if args.cmd == "worktree-add":
            # Already correct for pipelined dispatch, and deliberately left
            # alone: the EXCLUSIVE lock spans the git call, not just the
            # record-keeping around it (same shape merge-task now has). Two
            # tasks' `worktree add` run against one shared checkout — they
            # write the same `.git/worktrees/` index and cut branches from
            # the same ref — so the serialization here is the reason a
            # concurrent lane never sees a half-registered worktree.
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
                # the registered map rides along so a twice-failed worktree
                # can say whether the direct-branch fallback is even legal
                # here (gitops.shares_toplevel)
                wt = gitops.worktree_add(args.repo, args.task_id, args.base,
                                         config.get("repos") or {})
                task["worktree"] = wt
                state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True, "resumed": False, **wt})
            return 0

        if args.cmd == "worktree-remove":
            # Same span, same reason as worktree-add above: the removal (a
            # write to the shared checkout's worktree index) happens inside
            # the lock, so a sibling task's add cannot interleave with it.
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
            # Same phantom-run pre-check merge-task makes, and for the same
            # reason: `locked()`'s unconditional mkdir would build a run
            # directory out of a typo'd --run before anything refused.
            if not state_mod.state_path(args.run).exists():
                raise state_mod.StateError(
                    f"{args.run} is not a run (no state.yaml) — check --run; "
                    "refusing to manufacture a phantom run directory")
            # UNDER THE RUN LOCK (adversarial review of round 3, measured).
            # This verb walks the whole live run directory, copies every file
            # into the repo, PRUNES what no longer belongs, and then stages
            # and commits the result — while `merge-task` may be rewriting
            # the same repo's index and HEAD from another lane, and while
            # any run-scoped writer may be mid-`chain.seal` (two separate
            # atomic replaces) on state.yaml. Unlocked, the mirror could
            # commit a state.yaml paired with the previous seal — a snapshot
            # that fails `verify` on inspection for no real reason — or
            # collide with a merge on git's own index.lock and fail the
            # step. It is a read of run authority plus a write to the repo,
            # so it takes the same lock both of those already take.
            #
            # The PUSH deliberately stays outside: it is network I/O with no
            # bearing on run state, and holding the run lock across it would
            # queue every other verb behind a remote round-trip.
            with state_mod.locked(args.run):
                sha = gitops.publish_mirror(args.repo, args.run, config,
                                            args.run.name)
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
            synced = gitops.sync_branch(args.repo, args.onto)
            # `remote_verified: false` means the rebase used the LOCAL ref
            # because the remote could not be reached — the branch may still
            # be behind upstream. Reported, not silently equated with a real
            # sync (that equation was the original defect).
            _emit({"ok": True, "synced_onto": args.onto, **synced})
            return 0

        if args.cmd == "push":
            gitops.push_branch(args.repo, args.branch, args.force_with_lease)
            _emit({"ok": True, "pushed": args.branch,
                   "force_with_lease": args.force_with_lease})
            return 0

        if args.cmd == "merge-task":
            # Checked BEFORE `locked()`, whose unconditional mkdir would
            # otherwise build a phantom run directory out of a typo'd --run —
            # the stray-directory class `locked_read` avoids by design, and
            # this verb used to inherit that protection by opening with a
            # `locked_read`. It no longer does (see the exclusive section
            # below), so the check is explicit rather than incidental.
            if not state_mod.state_path(args.run).exists():
                raise state_mod.StateError(
                    f"{args.run} is not a run (no state.yaml) — check --run; "
                    "refusing to manufacture a phantom run directory")
            # THE MERGE ITSELF RUNS UNDER THE EXCLUSIVE RUN LOCK (round 3).
            # It used to sit outside, with only the SHA write-back re-taking
            # the lock — harmless while develop merged one task at a time,
            # wrong the moment dispatch became DAG-pipelined: two tasks'
            # merges contend on ONE feature-branch checkout (one index, one
            # HEAD), so a sibling's `merge --squash` could land between this
            # one's merge and its commit. gitops.squash_merge's preconditions
            # can only refuse states the lock cannot rule out; the lock is
            # what stops the two merges from interleaving in the first place.
            #
            # WHAT THIS COSTS, corrected after measurement (the round-3 note
            # here claimed "two tasks merging simultaneously is the ONLY
            # contention this adds" — false, and the error it predicted was
            # the wrong one). This lock is the RUN lock: every run-scoped
            # verb takes it, exclusively on Windows even for reads. So a
            # merge holding it for 15.6s (measured, 20k-file checkout)
            # queues `show`, `verify`, `status`, `task`, `cursor`,
            # `artifact`, `set-state`, `ready-tasks`, `publish-mirror`,
            # `stall`, `abort`, `complete` — every one of them, not just a
            # sibling merge. Pre-fix they did not queue, they DIED at ~9.4s
            # with a raw `OSError: Resource deadlock avoided`; state.py now
            # waits out the holder (LOCK_WAIT_BUDGET) and, only past that
            # budget, refuses with a StateError naming the lock and saying
            # to retry the identical command. Queueing behind a merge is the
            # accepted cost. If the WAIT itself ever becomes the problem,
            # the named escalation is a per-REPO lock (merges of different
            # repos never contend, and would stop queueing behind each
            # other) — deliberately not built now: an unused second lock
            # ordering is its own deadlock risk.
            with state_mod.locked(args.run):
                st = state_mod.load(args.run, args.workspace)
                transitions.ensure_live(st, "merge-task")
                sha = _merge_task(args, config, st)
                state_mod.save(args.run, args.workspace, st)
            _emit({"ok": True, **({"autosquashed": True} if args.autosquash
                                  else {"sha": sha})})
            return 0

        if args.cmd == "ready-tasks":
            # The OWNED derivation of the dispatch picture (round 3). develop
            # dispatches every task whose depends_on is satisfied, so someone
            # has to answer "which are those, right now" — and it must not be
            # the orchestrator, reading state.yaml and re-implementing the
            # DAG walk in prose. Hand-derivation is how a task with an unmet
            # dependency gets dispatched (the FSM refuses it, mid-loop, as a
            # confusing 'not yet done'), and how an IN-FLIGHT task gets
            # dispatched twice (the spawn guard refuses the second, and the
            # orchestrator has no idea why). Read-only, shared lock, same
            # torn-read guard as `show`; a corrupt state raises the CLI's
            # normal integrity error rather than reporting a partial picture.
            with state_mod.locked_read(args.run):
                st = state_mod.load(args.run, args.workspace)
            _emit({"ok": True, **workflow.dispatch_picture(st)})
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
            if args.cmd == "verify":
                _emit({"ok": True, "seq_verified": True})
                return 0
            # `show` enriches the persisted snapshot with ledger-fresh,
            # ENGINE-derived context an orchestrator otherwise can't see from
            # state.yaml alone. Field motive: at a `verdict_bound` step the
            # persisted `<step>.outcome` artifact is stamped `pending` on
            # step ENTRY (advance_cursor) and only re-derived from the
            # reviewer-verdict ledger by the NEXT `cursor --to`. So an
            # orchestrator polling `show` between the reviewer's captured
            # verdict landing in reviews.ndjson and the move that consumes it
            # saw a stale `pending` — and no hint that (say) `approve-plan`
            # was already the sole engine-legal exit. Three added top-level
            # fields close that blind spot:
            #   next_steps  the {step_id: reason} the engine would allow
            #               RIGHT NOW — literally transitions.cursor_candidates,
            #               the SAME computation `cursor --to` validates
            #               against, never a re-implementation of legality.
            #   derived     ledger-fresh artifact values that DIFFER from the
            #               persisted cache (concretely the refreshed
            #               verdict_bound outcome, e.g.
            #               {"plan-review.outcome": "approved"}); always
            #               present as {} when nothing differs.
            #   probe_error null when the candidates walk completed; otherwise
            #               the engine's OWN reason there is no legal move yet.
            #               This is what makes an empty `next_steps` HONEST:
            #               several distinct situations all produce {}, and
            #               only the walk can tell them apart, so `show`
            #               reports the reason rather than flattening them into
            #               one indistinguishable "stuck". Known cases:
            #                 · a LIVE step whose next-in-sequence carries a
            #                   `when` predicate on an artifact THIS step still
            #                   has to produce — at `security`, approve-security's
            #                   predicate reads `security.max_severity` before
            #                   the scan records it, so eval_predicate raises
            #                   "predicate needs artifact … never recorded".
            #                   Empty here means "run this step", NOT "wedged" —
            #                   and the message says so.
            #                 · a corrupt reviews.ndjson at a verdict_bound step
            #                   (the ledger fail-closes; the loud enforcement
            #                   refusal stays on `cursor --to`).
            #                 · a seal-valid but MALFORMED state — e.g. a `mode`
            #                   absent from the manifest after a hand-repair +
            #                   `reseal`, or a mode renamed out from under a
            #                   parked run — which raises a bare KeyError deep in
            #                   the walk. Pre-change `show` emitted such a state
            #                   fine (rc 0); degrading here preserves that.
            # Strictly read-only is load-bearing: cursor_candidates MUTATES the
            # state dict it is handed (it re-stamps the outcome via
            # set_artifact), so the walk runs on a DEEP COPY. The emitted
            # `state` therefore stays byte-for-byte the persisted snapshot —
            # auditability demands an inspector diffing show's `state` against
            # disk sees zero drift — while `derived`/`next_steps` report what
            # the fresh walk found WITHOUT rewriting anything into `state`.
            next_steps: dict = {}
            derived: dict = {}
            probe_error: str | None = None
            if not (st.get("aborted") or st.get("completed")):
                # A terminal run (abort/complete — the exact markers
                # ensure_live refuses every mutation on) has no legal cursor
                # move, so skip the walk entirely: `next_steps` stays an
                # honest {} rather than a phantom exit off a run that can
                # never advance, and show keeps working on terminal runs
                # exactly as it did before these fields existed.
                probe = copy.deepcopy(st)
                try:
                    next_steps = transitions.cursor_candidates(
                        probe, manifest, config, run=args.run)
                except Exception as exc:
                    # The probe is PURE enrichment on a read-only diagnostic,
                    # so ANY probe failure degrades to an empty next_steps
                    # while the persisted `state` is still emitted (rc 0) —
                    # `show` is the tool reached for WHEN a run is wedged and
                    # must never be the thing that crashes on it. The catch is
                    # broad ON PURPOSE: a TransitionError (missing predicate
                    # artifact at a live step, corrupt ledger) AND a bare
                    # KeyError from a malformed sealed state both belong here,
                    # and the pre-change `show` emitted the state fine on the
                    # latter — this preserves that. probe_error carries the
                    # walk's own diagnosis so the empty set is never silently
                    # conflated with a genuinely move-less run.
                    next_steps = {}
                    probe_error = f"{type(exc).__name__}: {exc}"
                    # Scrub any absolute run/workspace path the walk's own
                    # diagnosis embedded — a LedgerCorruption message names the
                    # ledger FILE (transitions.py wraps ndjson.py's `{path}`),
                    # and `show` output is copy-pasted into shared channels
                    # where a local filesystem layout has no business. The
                    # loud `cursor --to` refusal keeps the full path (it fires
                    # locally, for the operator). Replace the RUN prefix before
                    # the workspace prefix: the run dir lives under the
                    # workspace, so scrubbing the shorter workspace path first
                    # would strand the run-dir tail. Both the resolved and the
                    # as-passed spellings are scrubbed so neither leaks.
                    for raw, tag in ((args.run, "<run>"),
                                     (args.workspace, "<workspace>")):
                        if raw is None:
                            continue
                        for form in (str(Path(raw).resolve()), str(raw)):
                            probe_error = probe_error.replace(form, tag)
                # `derived` is computed UNCONDITIONALLY — even when the walk
                # raised. The verdict_bound outcome refresh is the ONLY
                # set_artifact the walk performs and it runs BEFORE the
                # sequence walk, so a later raise must not drop a legitimately
                # refreshed outcome; the deep copy is valid up to the raise
                # point, so its artifact diff is trustworthy either way. The
                # walk only ever refreshes the current verdict_bound step's
                # outcome, but diff the whole map so the contract stays honest
                # if a future engine change refreshes more.
                before = st.get("artifacts") or {}
                after = probe.get("artifacts") or {}
                derived = {k: v for k, v in after.items()
                           if before.get(k) != v}
            # A fourth ledger-fresh field, same motive as the three above —
            # something true on disk that state.yaml alone cannot show. The
            # orchestrator is told to re-read `show` whenever it is unsure
            # what to do next, and "wait for a background spawn vs. call
            # `stall`" is decided at exactly that moment — yet `show`
            # reported no flagged events at all, so the one record that
            # answers it (`spawn-pending`) was invisible unless the
            # orchestrator went reading ndjson by hand. `status` has carried
            # a flagged COUNT for runs; per-KIND is what makes the reading
            # actionable here. Same shared filter, so the number can never
            # disagree with `status` or with metrics' "## Flagged events".
            #
            # Enrichment, never a failure mode: an unreadable/absent ledger
            # degrades to {} exactly like the probe above — `show` is the
            # tool reached for when a run is wedged.
            #
            # `outstanding_spawns` is a SIBLING key, not a reshaping of the
            # one above: the {kind: count} summary answers "how much is
            # owed", and reducing to a count is exactly right for the other
            # ten kinds. For `spawn-pending` it destroys the only thing the
            # dispatch loop needs. Executed (whole-system review, round 4):
            # with T1's developer dead and T2's alive, `ready-tasks` and
            # `show` read IDENTICALLY — no owned verb attributed an open
            # pending to a task — so the only way to find the wedged lane was
            # to `stall` one and see which refused, a destructive probe that
            # SUCCEEDS on the healthy lane and returns the wrong instruction.
            # `outstanding_flagged` has carried the full dicts all along
            # (task/mode/agent_id, and now the spawning step); this hands
            # them over rather than making the orchestrator hand-read the
            # events.ndjson tail, which is the thing owned verbs exist to
            # prevent.
            #
            # `at` rides along, and it is the field that actually closes the
            # case above (round-4 review re-executed it: attribution alone
            # does NOT). Pipelined develop puts both lanes at the SAME step —
            # and `requires_tasks_terminal` means the cursor can never leave
            # develop while T1 is non-terminal, so the step comparison that
            # separates a left-behind pending from a live one can never fire
            # for a dead develop lane. Two entries then read identically
            # except for their AGE, which is the whole discriminator: the
            # outlier among siblings launched into one step is the wedged
            # one. Health cannot make that call for the same reason (see
            # workflow.run_health) — it is per-lane, so it is reported here
            # and diagnosed in dev-workflow/SKILL.md's triage.
            #
            # `clearable`/`clearing_key` answer the NEXT question rather than
            # leaving it to be derived: the declared-unclearable modes
            # (repo-map, request-triage — declared outside every step's
            # spawn-set) match no `stall` key at all, so the abandon
            # instruction the triage used to give for any non-current-step
            # entry bumped a stall counter, wrote no override, cleared
            # nothing, and degraded the run for a brand-new reason.
            outstanding: dict[str, int] = {}
            spawns: list[dict] = []
            legacy: list[dict] = []
            try:
                pending_kind = (transitions.spawn_pairing().get("pending")
                                or {}).get("kind")
                events = ndjson.read_records(args.run / "events.ndjson")
                for e in workflow.outstanding_flagged(events):
                    kind = e.get("kind")
                    outstanding[kind] = outstanding.get(kind, 0) + 1
                    if kind == pending_kind:
                        key = transitions.spawn_clearing_key(manifest, e)
                        spawns.append({"task": e.get("task"),
                                       "mode": e.get("mode"),
                                       "agent_id": e.get("agent_id"),
                                       "step": e.get("step"),
                                       "at": e.get("at"),
                                       "clearable": key is not None,
                                       "clearing_key": key})
                # A pending written by a PRE-ROUND-4 harness carries the spawn
                # shape where `actor` now lives, so it fails the anti-forgery
                # actor check and is invisible to every reader of the family
                # — including `outstanding_flagged` above. Executed (round-4
                # review): a spawn in flight across the upgrade left the gauge
                # empty, health HEALTHY, re-spawn allowed, stall allowed over
                # a live agent, and its real SubagentStop exiting 0 with an
                # empty stderr. Round 3's stale-verdict race, reopened for the
                # upgrade window and reported by nothing. The actor bound is
                # NOT widened to fix it — accepting a declared shape as an
                # alternate actor re-opens the forgery round 4 closed — so
                # these surface under their OWN key instead: visible, never
                # mistaken for an open pending, and named for what they are.
                closed = transitions.closed_agent_ids(events)
                legacy = [{"agent_id": e.get("agent_id"),
                           "task": e.get("task"),
                           "mode": e.get("mode"), "at": e.get("at")}
                          for e in events
                          if e.get("kind") == pending_kind
                          and not transitions.is_open_pending_record(e)
                          and e.get("agent_id") not in closed]
            except Exception:                                 # noqa: BLE001
                outstanding, spawns, legacy = {}, [], []
            _emit({"ok": True, "state": st, "next_steps": next_steps,
                   "derived": derived, "probe_error": probe_error,
                   "outstanding_flagged": outstanding,
                   "outstanding_spawns": spawns,
                   "legacy_spawn_pendings": legacy})
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
                    gitops.verify_green(proof, Path(repo), test_cmd,
                                        run_tests=True, config=config,
                                        task_repo=task["repo"], run=args.run)
                verify_ctx = {"repo": Path(repo), "run_tests": False}

        with state_mod.locked(args.run):
            st = state_mod.load(args.run, args.workspace)
            key = chain.load_key(args.workspace)
            transitions.ensure_live(st, args.cmd)
            extra: dict = {}   # verb-specific fields for the shared emit

            if args.cmd == "cursor":
                skipped = transitions.advance_cursor(st, manifest, config,
                                                     args.to, now,
                                                     run=args.run)
                for s in skipped:
                    # a conditional gate skipped by its declared predicate is
                    # an evaluation, not an omission — the ledger must be able
                    # to prove it happened (e2e E2E-1: approve-security's
                    # silent self-skip was indistinguishable from a hole)
                    #
                    # A skipped NON-gate step is a different fact and gets its
                    # own unflagged kind (adversarial-review B/5): `gate-skipped`
                    # is in FLAGGED_EVENT_KINDS because a gate that didn't ask a
                    # human is worth a human's attention, but confirm-repo — the
                    # first conditional non-gate step — skips on EVERY
                    # single-repo quick run by design, which would park a
                    # permanent flagged row on the zero-ceremony path the
                    # predicate exists to keep clear. Still ledgered either way:
                    # the skip and its false predicate stay provable.
                    kind = ("gate-skipped"
                            if (manifest["steps"].get(s["step"]) or {}).get("gate")
                            else "step-skipped")
                    ndjson.append_record(args.run / "events.ndjson",
                                         {"kind": kind, **s})
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
                    # Re-presenting re-stamps `presented_at`, which INVALIDATES
                    # any reply the human already gave: decide qualifies only
                    # records strictly after the stamp. Field, 2026-08-04
                    # (BUG-2's approve-pre-pr): the human's APPROVED was
                    # captured at 13:04:30, a re-present at ~13:06 aged it out,
                    # decide refused "no human input after presentation", and
                    # they were asked to type it a second time. The step file's
                    # own retry advice ("`--present` again … and repeat") walks
                    # straight into this, so it was a remedy that could not
                    # terminate — the class the 3.3.0 whole-branch pass called
                    # out. Refuse instead of silently discarding the evidence;
                    # `--re-present` is the deliberate escape (a qualified
                    # "APPROVED but…" that can't decide genuinely does need a
                    # fresh window).
                    prev = st["gates"].get(args.id) or {}
                    if (prev.get("presented_at") and prev.get("decision") is None
                            and not args.re_present):
                        try:
                            # UNFILTERED by session, deliberately, and NOT
                            # `gates.qualifying_records`: that helper answers
                            # "which records may I PARSE?", and this guard
                            # asks "which records would re-stamping
                            # DESTROY?" — every record in the window, tagged
                            # or not, ours or another session's. Sharing the
                            # decide-side filter here was a real regression
                            # (review finding, traced): when the session
                            # running `--present` is not the one that replied
                            # — a subagent-presented gate, two live sessions
                            # on one run — the human's genuine record was
                            # filtered out of the guard's view, `waiting`
                            # came back empty, the guard passed, and the
                            # re-stamp aged the reply out SILENTLY, with no
                            # refusal and no event; `--decide` then reported
                            # the stale cause and misdiagnosed the operator.
                            # The guard protects the WINDOW; `decide`
                            # protects the PARSE.
                            waiting = [
                                r for r in ndjson.read_records(
                                    args.run / "human-input.ndjson")
                                if r.get("at", "") > prev["presented_at"]]
                        except (OSError, ndjson.LedgerCorruption):
                            waiting = []   # unreadable → let the present through
                        if waiting:
                            raise gates.GateRefusal(
                                f"gate '{args.id}' is already presented and has "
                                f"{len(waiting)} un-decided repl(y/ies) waiting "
                                "— re-presenting would re-stamp the window and "
                                "throw them away, making the human type it "
                                "again. Run `--decide` instead: it reports what "
                                "those replies actually are. If it says they "
                                "were typed in a DIFFERENT session, follow THAT "
                                "refusal (ask for one more reply in the session "
                                "driving this run — re-presenting here would "
                                "discard the other session's reply unread). If "
                                "the reply genuinely cannot decide (a qualified "
                                "\"APPROVED but…\"), resolve it with the user "
                                "and re-present with --re-present")
                    gates.present(st, args.id, now, options, session=session)
                    # Observability, not identity: NEITHER the id nor its
                    # digest leaves this process, only whether one exists.
                    # The whole session-scoping design rests on the platform
                    # exporting CLAUDE_CODE_SESSION_ID to this subprocess as
                    # the same string the hook payload carries. If that ever
                    # stops being true (platform change, a wrapper that
                    # scrubs env, a subagent context), the allowed set
                    # collapses to empty and the filter goes INERT —
                    # production silently unprotected while every test stays
                    # green, because the tests inject the variable
                    # themselves and so cannot distinguish "the platform
                    # supplies this" from "it does not". That exact
                    # silent-non-protection shape already burned this change
                    # once, so the live answer rides the result of every
                    # presentation, where a human can see it.
                    #
                    # It covers the CLI half ONLY — the likelier break, but
                    # not the whole scheme. If the HOOK stops carrying
                    # `session_id`, every record goes untagged, "unknown
                    # means usable" passes them all, the filter is equally
                    # inert, and this still reports "known": the hook's
                    # payload is not visible from here (re-verification F3).
                    #
                    # Through `session_digest`, never raw truthiness: this
                    # signal must report what the FILTER sees, and the two
                    # disagree on exactly the input that matters — a
                    # whitespace-only CLAUDE_CODE_SESSION_ID is truthy but
                    # digests to None, so `if session` printed "known" over
                    # an entirely inert filter (review finding, probed).
                    extra["session"] = ("known" if gates.session_digest(session)
                                        else "unknown")
                else:
                    if args.options is not None:
                        raise gates.GateRefusal(
                            f"gate '{args.id}': --options is never legal at "
                            "--decide — the decision replays the option list "
                            "sealed at --present (RC3: the caller must not "
                            "define what a numbered reply means)")
                    # A decision is derivable only AT the gate (adversarial-
                    # review, plan-accuracy round: `verdict_bound` made
                    # `decided_at` load-bearing state — an any-cursor decide
                    # could bank an approval before the gate's artifacts even
                    # exist, or move the verdict window mid-plan-cycle and
                    # silently reset the review round budget). --present
                    # stays cursor-free: re-presenting only re-stamps the
                    # window and pops any unconsumed decision, both
                    # fail-closed directions.
                    if st["cursor"]["current_step"] != args.id:
                        raise gates.GateRefusal(
                            f"gate '{args.id}' is not the current step "
                            f"(cursor: {st['cursor']['current_step']}) — a "
                            "decision is derived only at the gate itself; "
                            "advance the cursor there first")
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
                                          if o not in forward),
                        deciding_session=session)
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
                if args.name == "scope":
                    # scope has an owning verb with real validation
                    # (registered paths, task containment, the event
                    # record) — the generic write would bypass all of it
                    # and split the artifact copy from state.scope.
                    raise state_mod.StateError(
                        "the 'scope' artifact is written only by `harness "
                        "scope-register` — the generic artifact verb would "
                        "bypass its validation")
                if args.name == "repo-ambiguity":
                    # Written by fetch from the actual repos.yaml, and read by
                    # confirm-repo's `when` predicate. A generic write here
                    # would let `--value single` skip the confirming step in a
                    # multi-repo workspace — i.e. re-open the very hole
                    # confirm-repo exists to close.
                    raise state_mod.StateError(
                        "the 'repo-ambiguity' artifact is written only by "
                        "`harness fetch`, from the workspace's registered "
                        "repos — it is not orchestrator-settable")
                engine_owned = {vb["outcome_artifact"]
                                for s in manifest["steps"].values()
                                for vb in [s.get("verdict_bound") or {}]
                                if vb.get("outcome_artifact")}
                if args.name in engine_owned:
                    # verdict_bound outcomes are ENGINE-derived from the
                    # verdict ledger (the exception gate's predicate trusts
                    # them); an orchestrator-written value would lie on
                    # every audit surface until the next cursor move
                    # re-derives it.
                    raise state_mod.StateError(
                        f"'{args.name}' is engine-recorded from the "
                        "reviewer-verdict ledger (verdict_bound."
                        "outcome_artifact) — never written by hand")
                transitions.set_artifact(st, manifest, args.name, args.value)
            elif args.cmd == "stall":
                stall_key = args.task or f"step:{st['cursor']['current_step']}"
                # Checked BEFORE record_stall so a refusal leaves the
                # counters untouched (field: dual-run comparison —
                # a stall recorded for an already-captured verdict cost a
                # full lens panel + one of five review rounds).
                #
                # The escape hatch runs the guard too, and keeps what it
                # would have said: every OTHER escape hatch in this codebase
                # is visible afterwards (`verify-red --revise` -> a
                # `test-revision` flag, `coverage-skipped`,
                # `pr-recorded-manually`), while this one wrote a stall
                # record byte-identical to a guarded one — so the reviewer of
                # a DEGRADED run could not tell an overridden stall from a
                # genuine one (whole-branch adversarial review). The guard
                # fails open on an unreadable ledger, so the only thing this
                # catch can see is a real, suppressed refusal — passing the
                # flag on a genuine stall records nothing extra.
                overridden = None
                if not args.confirm_no_verdict:
                    transitions.guard_stall_verdict(st, manifest, args.run,
                                                    stall_key)
                else:
                    try:
                        transitions.guard_stall_verdict(st, manifest, args.run,
                                                        stall_key)
                    except Exception as exc:      # noqa: BLE001 — see below
                        # Deliberately every exception, not just
                        # TransitionError. Before this, the flag SKIPPED the
                        # guard entirely, so nothing the guard could do was
                        # able to break the escape hatch; running it for the
                        # marker handed it that power back. A malformed
                        # ledger record (a non-string `at` makes
                        # _verdict_window's comparison raise TypeError) then
                        # took `stall --confirm-no-verdict` out of the JSON
                        # error contract entirely — on the one path a wedged
                        # run depends on, whose fail-open property the guard's
                        # own comment calls load-bearing (re-verification
                        # finding, reproduced). The flag's contract is
                        # "record it anyway"; anything the guard says on the
                        # way is a note, never a veto.
                        overridden = f"{type(exc).__name__}: {exc}"[:200]
                if overridden:
                    # BEFORE record_stall, deliberately (adversarial review,
                    # executed): `record_stall` raises "unknown task" for a
                    # key absent from state["tasks"], and nothing ever
                    # validates a `harness-task:` header against the task
                    # list — so one typo'd header produced a pending under a
                    # key the counter rejects, and the retirement that would
                    # have freed it lived AFTER the raise. Result: the key
                    # blocked by guard_spawn's one-live-spawn rule, the run
                    # DEGRADED, and no verb able to move either. The flag's
                    # contract is "record it anyway" (see the catch above);
                    # a counter failure is allowed to fail the verb, never to
                    # void the retirement the override already decided on.
                    # The TransitionError still propagates — these writes are
                    # ledger appends, not part of the state save below.
                    ndjson.append_record(
                        args.run / "events.ndjson",
                        {"kind": "stall-verdict-override", "task": stall_key,
                         "reason": overridden, "actor": "stall"})
                    # The override's OTHER consequence, and the only writer
                    # of this kind: declaring a spawn dead has to actually
                    # retire it. Without this the run deadlocks — the
                    # abandoned pending stays open, so guard_spawn's
                    # one-live-spawn rule refuses the very re-spawn the
                    # stall's `reinvoke` action just asked for, and no verb
                    # exists to clear it. It also settles the two other
                    # readers: the pending stops counting on the flagged
                    # gauge / run health (the `stall-verdict-override`
                    # above is the visible anomaly record for this round —
                    # counting both would double-report one event), and a
                    # late SubagentStop for this agent is refused capture
                    # instead of injecting a stale verdict into a round
                    # somebody else has already been spawned to redo.
                    #
                    # EVERY open pending for the key, not just the one the
                    # guard happened to name: a batched panel leaves several
                    # in flight under one key, and a half-abandoned key
                    # deadlocks exactly like an un-abandoned one.
                    abandoned = (transitions.spawn_pairing()
                                 .get("resolvers") or {})["abandoned"]
                    for pend in transitions.open_spawn_pendings(
                            args.run, stall_key, manifest):
                        ndjson.append_record(
                            args.run / "events.ndjson",
                            {"kind": abandoned["kind"],
                             "actor": abandoned["actor"],
                             "agent_id": pend.get("agent_id"),
                             "task": pend.get("task"),
                             "mode": pend.get("mode"),
                             "reason": f"a stall for '{stall_key}' was "
                                       "recorded over this open "
                                       "spawn-pending with "
                                       "--confirm-no-verdict: the spawn is "
                                       "declared dead, the key is freed for "
                                       "a fresh spawn, and a reply arriving "
                                       "late is no longer captured"})
                action = transitions.record_stall(st, config, stall_key)
                ndjson.append_record(
                    args.run / "events.ndjson",
                    {"kind": "stall", "task": stall_key, "action": action,
                     **({"override": "confirm-no-verdict"} if overridden
                        else {})})
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
            # TypeError joins them for the same reason ValueError is here:
            # a hand-edited or migrated state.yaml can carry a field of the
            # wrong SHAPE, and the engine's own list/dict operations then
            # raise it (adversarial review, round 4: a legacy `depends_on:
            # "T1"` string). The owned entry points refuse such shapes by
            # name; this is the floor under everything they don't reach, so
            # a shape defect reads as a refusal instead of a traceback.
            TypeError,
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
