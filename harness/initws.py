"""init-workspace mechanics (design.md piece 4, M7): discovery, verification
gates, per-section config, permission allowlist, repo-map staleness. The
interactive interview is the skill's job; every check and write is code.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from . import gitops, ndjson
from .schema import deep_merge

# marker file -> (language, proposed test_cmd, proposed coverage_cmd or None
# where there's no widely-agreed convention to guess — user configures it).
# node and java coverage are NOT in this table: their proposals are
# evidence-based, resolved per marker dir by _coverage_proposal below
# (a static node guess like `npm run coverage` can propose a script the
# repo doesn't have, while a java repo with jacoco right in its pom gets
# nothing if "don't guess" excludes detection too).
MARKERS = [
    ("pyproject.toml", "python", "python3 -m pytest", "python3 -m pytest --cov"),
    ("setup.py", "python", "python3 -m pytest", "python3 -m pytest --cov"),
    ("package.json", "node", "npm test", None),
    ("go.mod", "go", "go test ./...", "go test -cover ./..."),
    ("Cargo.toml", "rust", "cargo test", None),
    ("pom.xml", "java", "mvn -q test", None),
]


def _coverage_proposal(marker: str, marker_dir: Path,
                       static: str | None) -> str | None:
    """Coverage command proposed from repo EVIDENCE, never a bare guess:
    node — a `coverage` script wins; else jest (coverage built-in) or
    vitest with a @vitest/coverage-* provider installed justify
    `npm test -- --coverage`; nothing proves out → no proposal. java —
    jacoco named in the pom is detection, not guessing → propose the
    jacoco report run. Other markers pass the static table value through
    (python/go conventions are toolchain-wide, no per-repo evidence to
    check)."""
    if marker == "package.json":
        try:
            pkg = json.loads((marker_dir / marker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        scripts = pkg.get("scripts") or {}
        if "coverage" in scripts:
            return "npm run coverage"
        deps = {**(pkg.get("dependencies") or {}),
                **(pkg.get("devDependencies") or {})}
        test_script = str(scripts.get("test") or "")
        if "jest" in test_script or (
                "vitest" in test_script
                and any(d.startswith("@vitest/coverage") for d in deps)):
            return "npm test -- --coverage"
        return None
    if marker == "pom.xml":
        try:
            pom = (marker_dir / marker).read_text(encoding="utf-8")
        except OSError:
            return None
        return "mvn -q test jacoco:report" if "jacoco" in pom else None
    return static

# Directories that hold generated/vendored content, never a hand-authored
# subproject worth proposing as its own monorepo logical-repo (a Nuxt/Nitro
# `.output/server/package.json`, a `dist`/`build`/`target` bundle, ...).
EXCLUDED_DIRS = {".venv", "node_modules", ".output", "dist", "build", "target"}

# tool binary -> project-local wrapper script: prefer the wrapper when the
# marker's directory has one, since a bare global command only works if
# that tool happens to be installed system-wide.
WRAPPER_TOOLS = {"mvn": "mvnw"}


def _wrapper_test_cmd(marker_dir: Path, test_cmd: str) -> str:
    """Existence, not the executable bit, is the real signal — wrappers
    committed without +x (common from a non-git checkout, or a repo that
    always invokes them as `sh mvnw`) are just as usable via `sh`, which
    doesn't care whether the script itself is marked executable."""
    tool, _, rest = test_cmd.partition(" ")
    wrapper = WRAPPER_TOOLS.get(tool)
    if wrapper is None or not (marker_dir / wrapper).is_file():
        return test_cmd
    return f"sh {wrapper} {rest}" if rest else f"sh {wrapper}"


def discover(repo: Path, depth: int = 3, branch: str | None = None) -> dict:
    """Language/toolchain proposals from repo markers; multiple hits in
    different subtrees -> a proposed monorepo logical-repo split. Ensures
    the repo is clean and on its default branch first
    (gitops.ensure_default_branch — the reusable precondition also used by
    preflight) so proposals reflect the stable default-branch state, not
    whatever branch/dirty state the repo happened to be left in. Pass
    `branch` to override the auto-resolved guess (no resolvable origin/HEAD,
    or it resolved to the wrong branch)."""
    branch_check = gitops.ensure_default_branch(repo, branch)
    # One pruned walk, not one rglob per marker (adversarial-review
    # finding: rglob TRAVERSES node_modules/.venv/… fully and only then
    # filters the result — six times over — minutes on a big JS monorepo).
    # Pruning skips excluded and hidden trees entirely and stops at
    # `depth`; found markers land in MARKERS order, sorted per marker,
    # exactly as the rglob version emitted them.
    by_marker: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(repo):
        rel = Path(dirpath).relative_to(repo)
        rel_parts = rel.parts
        if len(rel_parts) >= depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for name in filenames:
            by_marker.setdefault(name, []).append(Path(dirpath))
    hits = []
    for marker, lang, test_cmd, coverage_cmd in MARKERS:
        for marker_dir in sorted(by_marker.get(marker, [])):
            hit = {"language": lang,
                   "root": str(marker_dir.relative_to(repo)),
                   "test_cmd": _wrapper_test_cmd(marker_dir, test_cmd)}
            cov = _coverage_proposal(marker, marker_dir, coverage_cmd)
            if cov:
                hit["coverage_cmd"] = _wrapper_test_cmd(marker_dir, cov)
            hits.append(hit)
    roots = {h["root"] for h in hits}
    return {"proposals": hits,
            "monorepo_split": sorted(roots) if len(roots) > 1 else None,
            "default_branch": branch_check["branch"],
            "branch_check": branch_check}


def _probe(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[:200]
    except FileNotFoundError:
        return False, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]}: probe timed out"


# cmd.exe builtins a test command could plausibly start with — they resolve
# to nothing on PATH, so the first-token check below must not call a command
# built from one of these "not found"
_CMD_BUILTINS = frozenset({"cd", "pushd", "popd", "call", "set", "echo",
                           "type", "start"})


def _first_token_resolves(cmd: str, cwd: Path | None = None) -> bool:
    """Whether a shell command's FIRST token names something invocable —
    the Windows side of verify()'s test_cmd invocability gate, where the
    exit code alone can't distinguish `missing-cmd` (cmd.exe exits 1) from
    a runnable-but-red suite (also 1). A quoted first token may contain
    spaces (`\"C:\\Program Files\\x\\python.exe\" -m pytest`).

    `cwd` is the directory the command actually RAN in (the repo) —
    a relative repo-local runner (`.\\run-tests.cmd`, `.\\gradlew.bat`)
    exists there, not wherever the harness process happens to sit
    (adversarial-review finding: a legitimately-red repo-local runner was
    misclassified "command not found", blocking init-finalize against
    this check's own stated contract that a red suite passes)."""
    import re as _re
    import shutil as _shutil
    m = _re.match(r'\s*(?:"([^"]+)"|(\S+))', cmd or "")
    tok = (m.group(1) or m.group(2)) if m else ""
    if not tok:
        return False
    if tok.lower() in _CMD_BUILTINS or Path(tok).exists():
        return True
    if cwd is not None and (Path(cwd) / tok).exists():
        return True
    return _shutil.which(tok) is not None


def repo_name(config: dict, repo_path) -> str | None:
    """Invert the name->path repo registry. Exact-string match first (the
    original convention, same as verify()'s repo:<name> check), then a
    resolved-path comparison — since the per-repo `branches`/`pr` artifact
    keying, this function must return a STABLE name across separate CLI
    invocations (preflight now, create-pr later), and those may spell the
    same repo differently (relative vs. absolute, `..` segments, symlink).
    Without normalization a spelling drift silently forked the artifact key
    and dropped the recorded base branch (adversarial-review finding)."""
    target_raw = str(repo_path)
    entries = list((config.get("repos") or {}).items())
    for name, path in entries:
        if str(path) == target_raw:
            return name
    target = Path(target_raw).resolve()
    for name, path in entries:
        if Path(str(path)).resolve() == target:
            return name
    return None


def _test_cmd_for_name(config: dict, name: str) -> str | None:
    """Shared by resolve_test_cmd (path->name lookup) and verify() (which
    already has the name from iterating repos). Per-repo entries live under
    `language.repos.<name>` — a sub-key, not a sibling of the global
    `test_paths`/`test_closure` keys — so a repo name can never collide with
    those (mirrors how `security.scan_cmd` already isolates its per-repo
    keys from `severity_order`/`gate_threshold`)."""
    repos_cfg = (config.get("language") or {}).get("repos")
    if not isinstance(repos_cfg, dict):
        return None
    entry = repos_cfg.get(name)
    return entry.get("test_cmd") if isinstance(entry, dict) else None


def resolve_test_cmd(config: dict, repo_path) -> str | None:
    """The one place that maps a task's repo path back to its registered
    name and looks up that repo's language-config test command."""
    name = repo_name(config, repo_path)
    if name is None:
        return None
    return _test_cmd_for_name(config, name)


def resolve_scan_cmd(config: dict, repo_path) -> str | None:
    """Same per-repo resolution for the security step's scanner command.
    Returns None rather than raising if `security.scan_cmd` is still the
    pre-per-repo flat-string shape."""
    name = repo_name(config, repo_path)
    if name is None:
        return None
    cmds = (config.get("security") or {}).get("scan_cmd")
    return cmds.get(name) if isinstance(cmds, dict) else None


def resolve_coverage_cmd(config: dict, repo_path) -> str | None:
    """Same per-repo resolution for `harden`'s coverage tool (adversarial-
    review finding: harden.md told agents to "run the coverage tool
    (language-config)" but no `coverage_cmd` key existed anywhere in
    defaults or `discover()`'s proposals — the step was executable only by
    improvisation). Mirrors `resolve_test_cmd`'s `language.repos.<name>`
    convention exactly, one sibling key over."""
    name = repo_name(config, repo_path)
    if name is None:
        return None
    repos_cfg = (config.get("language") or {}).get("repos")
    if not isinstance(repos_cfg, dict):
        return None
    entry = repos_cfg.get(name)
    return entry.get("coverage_cmd") if isinstance(entry, dict) else None


def verify(config: dict, workspace: Path | None = None) -> list[dict]:
    """Verification gates (a real gate, not a rubber-stamp): every check
    returns pass/fail/manual + remediation. Callers block on failures.
    `workspace`, when given, additionally re-checks the workspace-root-as-
    repo hazard for configs that PREDATE `write_section`'s write-time
    refusal or were hand-edited past it (re-review finding: write-time
    enforcement alone left old/edited configs reporting ok:true while still
    carrying the exact `git add -A` authority-file leak the refusal
    exists to stop)."""
    checks: list[dict] = []

    def add(name, ok, detail, remediation=""):
        checks.append({"check": name, "status": ok, "detail": detail,
                       "remediation": remediation})

    ok, detail = (True, "importable")
    try:
        import yaml as _  # noqa: F401
    except ImportError:  # pragma: no cover
        ok, detail = False, "PyYAML missing"
    add("pyyaml", "pass" if ok else "fail", detail, "pip install pyyaml")

    wi = (config.get("provider") or {}).get("work_item")
    if wi == "local-markdown":
        raw_dir = (config.get("provider") or {}).get("stories_dir") or ""
        if not str(raw_dir).strip():
            # Path("") is Path(".") and Path(".").is_dir() is True — an
            # UNSET stories_dir used to false-pass this check and then hunt
            # for stories in whatever cwd the process had (adversarial-
            # review finding).
            add("work-item provider", "fail", "stories_dir: (not set)",
                "set provider.stories_dir (init-section --section provider)")
        else:
            stories = Path(raw_dir)
            add("work-item provider", "pass" if stories.is_dir() else "fail",
                f"stories_dir: {stories}", "create the stories directory")
    elif wi in ("github", "gitlab"):
        cli = {"github": "gh", "gitlab": "glab"}[wi]
        ok, detail = _probe([cli, "auth", "status"])
        add("work-item provider", "pass" if ok else "fail", detail,
            f"{cli} auth login")
        # Auth alone isn't enough: without the explicit repo target the
        # adapter refuses at runtime (cwd-resolution wrong-issue risk) —
        # catch it at verify time, where fixing config is cheap.
        repo_key = f"{wi}_repo"
        target = (config.get("provider") or {}).get(repo_key)
        add(f"{repo_key}", "pass" if target else "fail",
            str(target or "(not set)"),
            f"set provider.{repo_key} to the repo hosting the work items "
            "(init-section --section provider)")
    elif wi == "ado":
        ok, detail = _probe(["az", "account", "show"])
        add("work-item provider", "pass" if ok else "fail", detail, "az login")
    elif wi in ("ado-mcp", "jira", "zoho"):
        add("work-item provider", "manual",
            f"{wi} is MCP-transport — run the model-in-the-loop "
            "MCP integration checklist", "")
    else:
        add("work-item provider", "fail", f"unknown provider '{wi}'", "")

    repos = config.get("repos") or {}
    # An empty `repos` map would otherwise emit zero repo:<name>/test_cmd:<name>
    # checks below — an absence of failures, not a pass — silently reporting
    # `ok: true` for a workspace that /dev-workflow can't do anything with
    # (adversarial-review finding: a full-replace `init-section --section
    # repos` call gone wrong, e.g. an unnested payload, wipes every repo and
    # verify doesn't notice).
    add("repos", "pass" if repos else "fail", f"{len(repos)} registered",
        "register at least one repo (init --repo / add-repo / "
        "init-section --section repos)")

    ws_root = workspace.resolve() if workspace is not None else None
    for name, path in repos.items():
        if ws_root is not None and Path(str(path)).resolve() == ws_root:
            add(f"repo:{name}", "fail", str(path),
                "the workspace root itself must not be a registered repo — "
                "`harness commit`'s `git add -A` would stage ai/** run-"
                "authority files (incl. human-input.ndjson) into project "
                "history; register the actual project checkout instead")
            continue
        is_repo = (Path(path) / ".git").exists()
        add(f"repo:{name}", "pass" if is_repo else "fail", str(path),
            "path must be a git checkout")

    for name, path in repos.items():
        cmd = _test_cmd_for_name(config, name)
        if not cmd:
            add(f"test_cmd:{name}", "fail", "not configured",
                f"set language.repos.{name}.test_cmd")
            continue
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  text=True, timeout=300,
                                  encoding="utf-8", errors="replace",
                                  cwd=path if Path(path).is_dir() else None)
            # 126/127 are the POSIX not-executable/not-found codes. Windows
            # has no reserved code: `cmd /c missing-cmd` exits **1**
            # (measured — the 9009 this check was blind-written against
            # only appears in batch-file contexts), and 1 is also what a
            # legitimately-red suite exits with. So on Windows a not-found-
            # shaped exit only counts when the command's first token ALSO
            # resolves to nothing — a red suite's runner resolves fine.
            not_runnable = proc.returncode in (126, 127) or (
                os.name == "nt" and proc.returncode in (1, 9009)
                and not _first_token_resolves(
                    cmd, Path(path) if Path(path).is_dir() else None))
            if not_runnable:
                add(f"test_cmd:{name}", "fail", f"exit {proc.returncode}",
                    f"command not found — fix language.repos.{name}.test_cmd")
            else:
                # Runnable. init-verify gates on INVOCABILITY only (126/127),
                # never the suite's exit code — a suite may legitimately be red
                # at init (TDD red state, pre-existing failures). A non-zero
                # exit here is a deliberate PASS, so it must NOT carry the
                # "command not found" remediation (validation-walk F1a: a PASS
                # used to emit `exit 2` + a not-found remediation at once).
                detail = ("exit 0" if proc.returncode == 0 else
                          f"exit {proc.returncode} — command runs; suite "
                          "non-zero, not gated at init")
                add(f"test_cmd:{name}", "pass", detail, "")
        except subprocess.TimeoutExpired:
            # The command RUNS — it's just slower than the verify cap. That
            # is not a broken test_cmd (adversarial-review finding: this
            # reported "fail: fix test_cmd" for any suite over 300s), but a
            # human should confirm the suite completes and consider a
            # faster smoke command; `manual` doesn't block init-finalize.
            add(f"test_cmd:{name}", "manual",
                "ran past the 300s verify cap — command exists and runs",
                "confirm the suite completes on its own; consider a faster "
                f"smoke command for language.repos.{name}.test_cmd")
        except OSError as exc:
            add(f"test_cmd:{name}", "fail", str(exc)[:200],
                f"fix language.repos.{name}.test_cmd")
    return checks


SECTION_FILES = {"provider": "provider.yaml", "repos": "repos.yaml",
                 "language": "language.yaml", "overrides": "overrides.yaml"}


def _refuse_workspace_root_repo(workspace: Path, repos: dict[str, str]) -> None:
    """Registering the workspace root itself as a repo (adversarial-review
    finding) leaks the "ai/** never leaves the workspace" privacy
    guarantee: `commit_class`/`commit_fixup` run `git add -A`, which would
    stage `state.yaml`, `.redproof/`, `human-input.ndjson`, `.harness-key` —
    everything `publish_mirror`'s exclusion list otherwise protects — and a
    later push would publish them. Refused outright, same as any other
    registry collision this project catches (`add_repo`'s name/path
    aliasing checks)."""
    ws = workspace.resolve()
    for name, path in repos.items():
        if Path(path).resolve() == ws:
            raise ValueError(
                f"repo '{name}' resolves to the workspace root ({workspace}) — "
                "registering the workspace itself as a repo would let "
                "`harness commit`'s `git add -A` stage ai/** run-authority "
                "files (state.yaml, .redproof/, human-input.ndjson); "
                "register the actual project checkout instead")


def write_section(workspace: Path, section: str, data: dict) -> Path:
    """Per-section config write — every section independently refreshable
    (the original forced --full to change a provider). `provider`/`repos`/
    `language` each expect the FULL current set on every call (replace
    semantics — matches SKILL.md's instruction to write "the whole set" in
    one call). `overrides` is the one exception: it's a flat grab-bag of
    otherwise-unrelated top-level config keys (status_mapping,
    subagent_models, quick_mode, ..., plus the bootstrap marker written by
    `mark_bootstrapped`) that the interview and `init-finalize` both write
    to independently over time, so it merges instead of replacing — a
    second `--section overrides` call adds/updates keys rather than
    silently discarding whatever an earlier call set (to remove an
    override entirely, edit `overrides.yaml` directly)."""
    if section not in SECTION_FILES:
        raise ValueError(f"unknown section '{section}' "
                         f"(one of {sorted(SECTION_FILES)})")
    from . import state as state_mod
    ctx = workspace / ".claude" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    path = ctx / SECTION_FILES[section]
    # Exclusive lock around the whole read-merge-write (adversarial-review
    # finding: the atomic replace below protects READERS from torn files,
    # not concurrent WRITERS from each other — two parallel `--section
    # overrides` calls interleaved read/merge and one update was lost).
    with state_mod.locked_file(ctx / ".config.lock"):
        return _write_section_locked(workspace, section, path, data)


def _write_section_locked(workspace: Path, section: str, path: Path,
                          data: dict) -> Path:
    if section == "overrides" and path.exists():
        # Recursive merge (adversarial-review finding, both lenses
        # independently reproduced it): a shallow {**existing, **data} let a
        # targeted write of one nested key (e.g. security.scan_cmd.backend)
        # silently drop sibling nested keys (scan_cmd.frontend) that weren't
        # restated. A LIST-valued top-level key (review_policy) still
        # replaces wholesale even with deep_merge — only dicts recurse — so
        # that one genuinely needs the whole list resupplied.
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data = deep_merge(existing, data)
    if section == "provider":
        provider = data.get("provider")
        if provider is not None and not isinstance(provider, dict):
            raise ValueError(f"provider.yaml's 'provider' key is not a "
                             f"mapping (got {type(provider).__name__})")
        provider = provider or {}
        stories_dir = provider.get("stories_dir")
        if provider.get("work_item") == "local-markdown" and stories_dir:
            if not isinstance(stories_dir, str):
                raise ValueError("provider.stories_dir must be a string "
                                 f"(got {type(stories_dir).__name__})")
            # A config value naming a directory that must exist for
            # local-markdown to function shouldn't need a separate
            # verify-time failure to discover it was never created — unlike
            # `repos`' paths (a git checkout can't be conjured by mkdir,
            # so those stay deferred to init-verify), an empty folder is
            # all local-markdown actually needs. A RELATIVE value anchors
            # at the workspace, never at process cwd (adversarial-review
            # finding: the bare Path() here and every later read resolved
            # against whatever cwd each process happened to have) —
            # load_declared() applies the same anchoring on every read.
            target = Path(stories_dir)
            if not target.is_absolute():
                target = workspace / target
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(
                    f"could not create stories_dir '{stories_dir}': "
                    f"{exc}") from exc
    if section == "repos":
        _refuse_workspace_root_repo(workspace, data.get("repos") or {})
    # Atomic swap (matches chain.seal's convention) — a plain write_text
    # leaves a window where a concurrent reader (e.g. another repo's
    # bootstrap in a multi-repo run) can see a truncated file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def mark_bootstrapped(workspace: Path) -> None:
    write_section(workspace, "overrides",
                  {"bootstrap_completed": ndjson.now_iso()})


def write_permissions(workspace: Path, repos: dict[str, str],
                      language: dict[str, dict]) -> Path:
    """Permission allowlist so background agents run unprompted (coverage
    review) — merged non-destructively into .claude/settings.json. Every
    registered repo's own test command gets its binary allow-listed
    (per-repo language-config), not just one global command."""
    path = workspace / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    allow = set(settings.setdefault("permissions", {}).get("allow", []))
    # Every skill/step invokes `${CLAUDE_PLUGIN_ROOT}/bin/harness`, never
    # `python3 -m harness` (adversarial-review finding: the allowlist only
    # ever had the latter, so it matched nothing skills actually run and
    # background agents hit a permission prompt on every harness call).
    # The rule must be the LITERAL, UNEXPANDED string — permission matching
    # happens on the raw command text with no env-var expansion (Claude
    # Code docs warn about exactly this), and skills instruct the model to
    # type `${CLAUDE_PLUGIN_ROOT}/...` verbatim (re-review finding: the
    # first fix wrote the RESOLVED absolute path here, which matches
    # nothing a skill-following model actually types — the same
    # matches-nothing bug it claimed to close, one indirection later).
    # The resolved form is ALSO kept for a model that expands the variable
    # itself before invoking; both prefixes are legitimate spellings.
    plugin_root = Path(__file__).resolve().parent.parent
    allow.update([
        "Bash(${CLAUDE_PLUGIN_ROOT}/bin/harness:*)",
        f"Bash({plugin_root}/bin/harness:*)",
        "Bash(python3 -m harness:*)",   # kept: harmless, covers manual/debug invocation
        "Bash(git status:*)", "Bash(git diff:*)",
        "Bash(git log:*)", "Bash(git add:*)", "Bash(git checkout:*)",
    ])
    allow.update(f"Bash({cmd.split()[0]}:*)" for cmd in
                (lang.get("test_cmd") for lang in language.values()) if cmd)
    allow.update(f"Read({p}/**)" for p in repos.values())
    settings["permissions"]["allow"] = sorted(allow)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return path


class AddRepoError(ValueError):
    pass


def _load_mapping(path: Path, label: str) -> dict:
    """Loads a section file for merging, refusing cleanly (not silently
    losing data, and not crashing with a raw AttributeError several .get()
    calls later) on a shape merging can't safely reason about."""
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AddRepoError(
            f"{label} is not a YAML mapping at its top level (got "
            f"{type(loaded).__name__}) — fix it by hand before add-repo "
            "can safely merge into it")
    return loaded


def add_repo(workspace: Path, name: str, path: str,
             test_cmd: str | None = None) -> dict:
    """Registers one new repo without disturbing already-registered ones —
    `repos.yaml`/`language.yaml` are full-replace files (write_section's
    normal contract), so adding a repo by hand means re-supplying the
    entire existing set or silently dropping it; this reads the current
    set first and merges. Refuses (never silently renames/overwrites/
    aliases — this project's standing convention for any registry write,
    see `bootstrap`'s collision refusal) on: a name that's already
    registered case-insensitively (repo-map directories collide on the
    default case-insensitive macOS filesystem even when two names differ
    only by case), or a path that's already registered under a different
    name (name->path resolution elsewhere in this module matches by exact
    path string first-match, so a silent alias would misattribute that
    other name's test_cmd/scan_cmd). Does NOT run init-verify/init-finalize
    itself — SKILL.md documents those as the required next steps, so the
    verify-then-finalize gate stays the one place that logic lives rather
    than being duplicated here too."""
    ctx = workspace / ".claude" / "context"
    repos_path = ctx / SECTION_FILES["repos"]
    repos_top = _load_mapping(repos_path, "repos.yaml")
    repos = repos_top.get("repos")
    if repos is not None and not isinstance(repos, dict):
        raise AddRepoError(
            f"repos.yaml's 'repos' key is not a mapping (got "
            f"{type(repos).__name__}) — fix it by hand before add-repo "
            "can safely merge into it")
    repos = dict(repos or {})

    target = Path(path).resolve()
    for existing_name, existing_path in repos.items():
        if existing_name.lower() == name.lower():
            raise AddRepoError(
                f"repo '{existing_name}' is already registered (path: "
                f"{existing_path}) — add-repo only adds new entries; to "
                "repoint or rename it, use `init-section --section repos` "
                "with the full corrected map (every registered repo, not "
                "just this one — that section is still full-replace)")
        if Path(existing_path).resolve() == target:
            raise AddRepoError(
                f"path {path} is already registered as '{existing_name}' — "
                "add-repo refuses to register the same repo under a "
                "second name (name->path resolution elsewhere matches by "
                "path, so this would silently misattribute config)")

    repos[name] = path
    write_section(workspace, "repos", {"repos": repos})

    if test_cmd is not None:
        lang_path = ctx / SECTION_FILES["language"]
        lang_top = _load_mapping(lang_path, "language.yaml")
        language = lang_top.get("language")
        if language is not None and not isinstance(language, dict):
            raise AddRepoError(
                f"language.yaml's 'language' key is not a mapping (got "
                f"{type(language).__name__}) — fix it by hand before "
                "add-repo can safely merge into it")
        language = dict(language or {})
        lang_repos = language.get("repos")
        if lang_repos is not None and not isinstance(lang_repos, dict):
            raise AddRepoError(
                "language.yaml's 'language.repos' key is not a mapping "
                f"(got {type(lang_repos).__name__}) — fix it by hand "
                "before add-repo can safely merge into it")
        lang_repos = dict(lang_repos or {})
        lang_repos[name] = {"test_cmd": test_cmd}
        language["repos"] = lang_repos
        write_section(workspace, "language", {"language": language})

    return {"name": name, "path": path, "test_cmd": test_cmd}


# ------------------------------------------------------------- repo-map

def repo_map_dir(workspace: Path, repo_name: str) -> Path:
    return workspace / ".claude" / "context" / "repo-map" / repo_name


def repo_map_stamp(workspace: Path, repo_name: str, repo: Path) -> dict:
    meta = {"sha": gitops.head_sha(repo), "at": ndjson.now_iso()}
    d = repo_map_dir(workspace, repo_name)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def repo_map_check(workspace: Path, repo_name: str, repo: Path,
                   stale_after: int) -> dict:
    meta_file = repo_map_dir(workspace, repo_name) / ".meta.json"
    if not meta_file.exists():
        return {"status": "missing", "behind": None}
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        stamped = meta["sha"]
    except (json.JSONDecodeError, KeyError):
        # A corrupt stamp is answerable — the map needs a refresh — so
        # answer, never traceback (adversarial-review finding).
        return {"status": "missing", "behind": None,
                "note": ".meta.json is corrupt — regenerate via /repo-map-refresh"}
    try:
        behind = len(gitops.run_git(repo, "rev-list",
                                    f"{stamped}..HEAD").splitlines())
    except gitops.GitError:
        # The stamped SHA is unknown to this history (force-pushed default
        # branch, re-clone, gc) — that IS staleness, not an error
        # (adversarial-review finding: raw `unknown revision` GitError,
        # recoverable only by knowing to hand-delete .meta.json).
        return {"status": "stale", "behind": None,
                "generated_at": meta.get("at"),
                "note": "stamped SHA not in this history (rewritten/"
                        "re-cloned) — regenerate via /repo-map-refresh"}
    status = "stale" if behind > stale_after else "fresh"
    return {"status": status, "behind": behind, "generated_at": meta["at"]}
