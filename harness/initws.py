"""init-workspace mechanics (design.md piece 4, M7): discovery, verification
gates, per-section config, permission allowlist, repo-map staleness. The
interactive interview is the skill's job; every check and write is code.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat
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
# `bin`/`obj` are .NET's pair, carrying the same risk `build` already
# accepted — a repo whose hand-authored sources live in a directory of that
# name is skipped. Worth it: a built solution puts hundreds of files under
# each one, at every project, and the walk lists every directory it enters.
EXCLUDED_DIRS = {".venv", "node_modules", ".output", "dist", "build", "target",
                 "bin", "obj"}

# .NET is matched by EXTENSION, not by filename — a solution/project file
# carries the project's own name, so there is no fixed string for MARKERS to
# key on. Kept a separate table for a second reason: its hits must NOT be
# one-per-matching-file the way MARKERS' are (see _dotnet_roots).
DOTNET_SOLUTION_SUFFIXES = (".sln", ".slnx")   # .slnx: the .NET 9 XML format
DOTNET_PROJECT_SUFFIX = ".csproj"
DOTNET_TEST_CMD = "dotnet test"
_TRACKED_SUFFIXES = frozenset((*DOTNET_SOLUTION_SUFFIXES,
                               DOTNET_PROJECT_SUFFIX))


def _dotnet_roots(by_suffix: dict[str, list[Path]]) -> list[Path]:
    """Which directories are .NET logical repos.

    A solution file WINS outright: it names the projects it builds, and one
    `dotnet test` there covers all of them. Proposing per `.csproj` instead
    would turn an ordinary five-project solution into five logical repos,
    each with its own `dotnet test` — a monorepo_split the user then has to
    undo by hand. Cost of the rule, accepted for predictability: a `.csproj`
    outside any solution's subtree is swallowed when a solution exists
    elsewhere in the repo.

    Nested solutions collapse to the outermost — a root `All.sln` beside
    `tools/Tools.sln` is one buildable unit, and proposing both would nest
    one logical repo inside another.

    With no solution anywhere, sibling projects still collapse, to their
    common ancestor, for the same no-fan-out reason. That ancestor often
    holds no project file itself, and `dotnet test` cannot answer such a
    directory — so the root is still proposed (it IS the repo boundary) but
    _dotnet_command returns no `test_cmd` for it. An earlier revision left a
    bare `dotnet test` there on the theory that the interview's probe would
    catch it; adversarial review measured that it does not — see
    _dotnet_command for why that failure is silent until verify-red.
    Deliberately still no guess at which project is the test project.

    NOTE (accepted limit, not a bug): discover()'s `depth` cut applies to
    this walk, so a solution deeper than `depth` is not seen at all, and in
    a deep layout the coverage evidence under it may be out of reach while
    the root itself is found. `src/Services/<Area>/<Project>/` exceeds the
    default 3.
    """
    solutions = sorted({f.parent for suffix in DOTNET_SOLUTION_SUFFIXES
                        for f in by_suffix.get(suffix, [])})
    if solutions:
        return [d for d in solutions
                if not any(other in d.parents for other in solutions)]
    projects = sorted({f.parent for f in
                       by_suffix.get(DOTNET_PROJECT_SUFFIX, [])})
    if not projects:
        return []
    return [Path(os.path.commonpath([str(p) for p in projects]))]


def _dotnet_command(root: Path, by_suffix: dict[str, list[Path]]) -> str | None:
    """The `dotnet test` invocation for `root`, or None when no single one
    covers it.

    `dotnet test` resolves a project or solution IN ITS OWN DIRECTORY, never
    recursively, and refuses two ways: MSB1003 when the directory holds
    none, MSB1011 when it holds several. Both exit 1 with `dotnet` itself
    resolvable — which verify()'s invocability gate reports as PASS, since
    it deliberately can't fail a legitimately-red suite on its exit code.

    So a bare `dotnet test` proposed for a directory that cannot answer it
    survives init entirely, and only detonates later: verify-red accepts ANY
    non-zero exit, so it seals a red-proof over a BUILD error with no test
    ever having run, and verify-green — which needs exit 0 — can then never
    be reached. The task wedges. Naming the file removes the ambiguity case
    (`App.sln` beside `App.slnx` is exactly the state a .NET 9 migration
    passes through), and returning None removes the other: the interview
    asks for a command instead of confirming one that cannot work.
    """
    def named(paths: list[Path]) -> list[str]:
        return sorted(f.name for f in paths if f.parent == root)

    here = named([f for suffix in DOTNET_SOLUTION_SUFFIXES
                  for f in by_suffix.get(suffix, [])])
    if not here:
        here = named(by_suffix.get(DOTNET_PROJECT_SUFFIX, []))
    if len(here) != 1:
        return None
    target = here[0]
    if " " in target:
        target = f'"{target}"'
    return f"{DOTNET_TEST_CMD} {target}"


def _dotnet_coverage(root: Path, projects: list[Path]) -> bool:
    """Whether repo EVIDENCE justifies a coverage proposal for `root`, the
    same bar node and java are held to: the `coverlet.collector` package is
    what makes `--collect:"XPlat Code Coverage"` work at all, so without it
    the flag just produces an empty report. `coverlet.msbuild` is a
    DIFFERENT integration driven by `/p:CollectCoverage=true` —
    deliberately not detected here rather than answered with the wrong flag.

    Confined to `root`'s own subtree, so in a multi-solution repo one
    solution's coverlet reference can't justify a proposal for another."""
    for path in projects:
        if not path.is_relative_to(root):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "coverlet.collector" in text.lower():
            return True
    return False

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
    # Extension-keyed hits ride the SAME walk (full file paths — _dotnet_
    # coverage has to read the project files, not just locate them). Only
    # the suffixes actually in the table are bucketed: keying every
    # extension would grow a dict entry per .py/.js/.png in the repo for
    # nothing.
    by_suffix: dict[str, list[Path]] = {}
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
            suffix = os.path.splitext(name)[1].lower()
            if suffix in _TRACKED_SUFFIXES:
                by_suffix.setdefault(suffix, []).append(Path(dirpath) / name)
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
    # Appended after the MARKERS block, not interleaved: .NET resolves its
    # roots across the whole walk at once, so it has no place in a
    # per-marker loop. No _wrapper_test_cmd — `dotnet` has no project-local
    # wrapper the way maven has mvnw.
    for root in _dotnet_roots(by_suffix):
        hit = {"language": "dotnet", "root": str(root.relative_to(repo))}
        # `test_cmd` is OMITTED, not guessed, when no single project or
        # solution answers this root (_dotnet_command). Coverage hangs off
        # the same resolved target: proposing a coverage command for a test
        # command we couldn't name would be the same guess one level up.
        cmd = _dotnet_command(root, by_suffix)
        if cmd:
            hit["test_cmd"] = cmd
            if _dotnet_coverage(root, by_suffix.get(DOTNET_PROJECT_SUFFIX, [])):
                hit["coverage_cmd"] = f'{cmd} --collect:"XPlat Code Coverage"'
        hits.append(hit)
    roots = {h["root"] for h in hits}
    return {"proposals": hits,
            "monorepo_split": sorted(roots) if len(roots) > 1 else None,
            "default_branch": branch_check["branch"],
            "branch_check": branch_check}


def _probe(cmd: list[str]) -> tuple[bool, str]:
    # `which()` resolution, not a bare exec: Windows' CreateProcess appends
    # only `.exe` to a bare name, so `az` (really `az.cmd`) is unfindable
    # without the PATHEXT walk which() does — a bare exec reports a false
    # "not installed" even when `az` is on PATH and authenticated.
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace")
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[:200]
    except FileNotFoundError:
        return False, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]}: probe timed out"


def _probe_json(cmd: list[str]) -> tuple[bool, dict, str]:
    """A probe whose PAYLOAD matters, not only its exit code — `_probe`
    truncates output at 200 characters, which is right for an auth banner
    and useless for a field list."""
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True,
                              timeout=30, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, {}, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return False, {}, f"{cmd[0]}: probe timed out"
    if proc.returncode != 0:
        return False, {}, (proc.stdout + proc.stderr).strip()[:200]
    try:
        parsed = json.loads(proc.stdout)
    except ValueError:
        return False, {}, f"{cmd[0]}: output was not JSON"
    return True, parsed if isinstance(parsed, dict) else {}, ""


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


class QuarantineError(ValueError):
    """A `language.repos.<name>.quarantine` block the harness refuses to act
    on. `ValueError` so the CLI's error contract already carries it."""


#: The only keys a `quarantine` block may carry. Unknown keys REFUSE rather
#: than being ignored (adversarial-review, both lenses): `language.*` is not
#: schema-validated, so a typo'd key — `test:` for `tests:`, `template:` for
#: `exclude_template:` — would otherwise read as "nothing quarantined" and
#: leave the user believing a config file fixed a failure it never touched.
QUARANTINE_KEYS = {"exclude_template", "coverage_exclude_template", "tests"}
#: `sh -c '<payload>'` and friends: the payload is a whole shell command in
#: its own right, so appended flags become the wrapper's arguments and never
#: reach the runner at all. Same nesting hazard hooks/guards.py `_scan_targets`
#: unwraps for the bash guard (re-verify finding: `sh -c "cd fe && vitest"`
#: passed the quote-aware scan AND init-verify, then silently ran the full
#: suite — a false negative strictly worse than the false positive it fixed).
#: Windows spellings included (pre-release review: `sh.exe -c`, `cmd /c` and
#: `powershell -Command` all slipped the POSIX-only pattern, reviving that
#: exact false negative on the one platform whose toolchains wrap commands
#: most — a Git-Bash `test_cmd: sh.exe -c "…"` is routine there).
_SHELL_WRAPPER_RE = re.compile(
    r"(?:^|\s)(?:"
    r"(?:[a-z]*sh|env)(?:\.\w+)?\s+(?:-\w+\s+)*-\w*c\b"     # sh/bash/zsh/env, .exe ok
    r"|cmd(?:\.\w+)?\s+(?:/\w+\s+)*/c\b"                    # cmd /c
    r"|(?:powershell|pwsh)(?:\.\w+)?\s+(?:-\w+\s+)*-c(?:ommand)?\b"
    r")", re.IGNORECASE)


def _shell_composition(cmd: str) -> str | None:
    """The first shell operator OUTSIDE quotes, or None.

    A quarantined command has its flags APPENDED, so it must be a single
    command: appending to `cd fe && npm test | tee log` attaches the
    exclusion to `tee`, silently running the full suite (adversarial-review).
    Quote-aware, NOT a substring scan (re-verify finding: a bare scan refused
    `go test ./... -run "TestA|TestB"` and every other quoted regex
    alternation — normal single commands — with advice that made no sense).

    Detected: `&&`, `||`, `|`, `;`, a lone backgrounding `&`, a newline
    (a YAML block scalar is an ordinary way to write two commands), and a
    `-c` shell wrapper. Backslash escapes are honoured so `\\"` inside a
    quoted string doesn't end it."""
    if _SHELL_WRAPPER_RE.search(cmd):
        return "-c"
    quote = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and quote != "'":
            i += 2                      # escaped char: never a delimiter
            continue
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif cmd[i:i + 2] in ("&&", "||"):
            return cmd[i:i + 2]
        elif ch in "|;&":
            return ch
        elif ch in "\n\r":
            return "newline"
        i += 1
    return None


def normalize_test_path(path: str) -> str:
    """The ONE spelling a test path is compared in — used for the overlap
    guard, the rendered exclusion flag, and the flagged event alike.

    Collapses everything a runner would treat as the same file but a string
    compare would not: surrounding whitespace, `./`, `//`, `/./`, `/../`
    (re-verify finding: each of these declared-vs-locked mismatches let an
    exclusion take effect while `_refuse_quarantine_overlap` saw no overlap
    — the silent false green at verify-green this guard exists to stop).
    Backslashes are NOT rewritten here; `quarantine_cmd` refuses them
    outright, because a `\\` is as likely to be a regex escape as a Windows
    separator and guessing mangles both."""
    s = posixpath.normpath(str(path).strip())
    return "" if s == "." else s


def resolve_quarantine(config: dict, repo_path) -> tuple[str | None, dict]:
    """(repo name, quarantine block) for a repo path — `{}` when none is
    declared. Same `language.repos.<name>` convention as its `test_cmd` /
    `coverage_cmd` siblings. `language.*` is not schema-validated, so the
    shape is checked here and in `quarantine_cmd` below; `initws.verify()`
    runs the same checks at `init-verify` time, where fixing config is cheap
    rather than mid-develop."""
    name = repo_name(config, repo_path)
    if name is None:
        return None, {}
    repos_cfg = (config.get("language") or {}).get("repos")
    entry = repos_cfg.get(name) if isinstance(repos_cfg, dict) else None
    if not isinstance(entry, dict) or "quarantine" not in entry:
        return name, {}
    block = entry["quarantine"]
    where = f"language.repos.{name}.quarantine"
    if not isinstance(block, dict):
        raise QuarantineError(
            f"{where} must be a mapping of "
            "{exclude_template, coverage_exclude_template?, tests: [...]} — "
            f"got {type(block).__name__}. A bare string or list is silently "
            "not a quarantine; refusing rather than running the full suite "
            "as if the config had taken effect.")
    unknown = sorted(set(block) - QUARANTINE_KEYS)
    if unknown:
        raise QuarantineError(
            f"{where} has unknown key(s) {', '.join(unknown)} — allowed: "
            f"{', '.join(sorted(QUARANTINE_KEYS))}. Refusing rather than "
            "ignoring a typo that would leave nothing actually quarantined.")
    return name, block


def quarantine_cmd(config: dict, repo_path, cmd: str,
                   run: Path | None = None, coverage: bool = False) -> str:
    """`cmd` with this repo's quarantined specs excluded, or `cmd` untouched
    when nothing is quarantined.

    field: dual-run comparison — one pre-existing, unrelated failing
    spec was rediscovered and routed around FOUR times across two runs of the
    same story: it blocked a task's completion in one and aborted the
    frontend coverage run three times in the other. The per-call `--test-cmd`
    override could express the workaround, but the knowledge evaporated
    between runs; this makes it declared workspace config instead.

    Deliberately LOUD, never silent — a quarantine that hides itself is worse
    than the status quo:
      * `reason` and `since` are REQUIRED per entry (no casual, undated
        exclusions);
      * a non-empty `tests` with no template REFUSES rather than running the
        full suite as though nothing were quarantined — the flag is
        runner-specific and must not be guessed;
      * a malformed block refuses (see `resolve_quarantine`) instead of
        reading as "nothing quarantined";
      * a shell-composed command refuses, because appended flags would land
        on the wrong command;
      * the run gets a flagged `tests-quarantined` event naming the excluded
        specs, so the exclusion is on its dashboard.

    `coverage=True` prefers `coverage_exclude_template`: a repo's
    `coverage_cmd` is often a DIFFERENT tool from its `test_cmd`
    (`sh mvnw test` vs `sh mvnw test jacoco:report`, `vitest` vs `nyc
    report`), and appending the test runner's flag to it would kill the
    coverage step with an unknown-flag error — the very symptom this exists
    to remove (adversarial-review).
    """
    name, block = resolve_quarantine(config, repo_path)
    tests = block.get("tests") or []
    if not tests:
        return cmd
    where = f"language.repos.{name}.quarantine"
    if not isinstance(tests, list):
        raise QuarantineError(f"{where}.tests must be a list of "
                              "{test, reason, since} entries")
    key = "coverage_exclude_template" if coverage else "exclude_template"
    template = block.get(key) or block.get("exclude_template")
    if not isinstance(template, str) or not template.strip():
        raise QuarantineError(
            f"{where} lists {len(tests)} quarantined test(s) but no "
            f"`{key}` — the exclusion flag is runner-specific and the "
            "harness will not guess it (vitest '--exclude {test}', pytest "
            "'--deselect {test}', maven '-Dtest=!{test}'; an npm wrapper "
            "needs its own passthrough, e.g. '-- --exclude {test}'). Set "
            f"{where}.{key}, or empty `tests`. Refusing to run the full "
            "suite as if nothing were quarantined.")
    if "{test}" not in template:
        # re-verify finding: str.format leaves a placeholder-free template
        # unchanged, so `exclude_template: "--exclude"` with 3 entries
        # rendered `--exclude --exclude --exclude` and excluded nothing
        raise QuarantineError(
            f"{where}.{key} must contain the '{{test}}' placeholder — it is "
            f"rendered once per entry; {template!r} names no test and would "
            "exclude nothing while looking like it had.")
    hit = _shell_composition(cmd)
    if hit:
        raise QuarantineError(
            f"{where} cannot be applied to {cmd!r}: exclusion flags are "
            f"APPENDED, and this command is shell-composed ({hit!r}), so "
            "they would attach to its last stage instead of the test "
            "runner — silently running the full suite. Move the composition "
            "into a script and point the command at that.")
    excluded = []
    for i, entry in enumerate(tests):
        if not isinstance(entry, dict) or not str(entry.get("test") or "").strip():
            raise QuarantineError(f"{where}.tests[{i}] needs a non-empty `test`")
        missing = [k for k in ("reason", "since")
                   if not str(entry.get(k) or "").strip()]
        if missing:
            raise QuarantineError(
                f"{where}.tests[{i}] ('{entry['test']}') is missing "
                f"{' and '.join(missing)} — quarantine is never casual or "
                "undated; record WHY it is excluded and WHEN it was added so "
                "the next run can tell a stale entry from a live one")
        raw = str(entry["test"])
        # ONE canonical spelling, enforced at declaration: the overlap guard
        # in gitops compares these against a red-proof's LOCKED test paths,
        # while a runner treats `./x`, `x//y`, `x/./y` and a trailing space
        # as the same file — so a non-canonical spelling excluded the task's
        # own test while slipping the guard, the one silent false green this
        # mechanism must not produce (re-verify finding: the first version
        # checked the stripped value but RENDERED the raw one, so a trailing
        # space reopened exactly that hole).
        if "\\" in raw:
            raise QuarantineError(
                f"{where}.tests[{i}] test {raw!r} contains a backslash — "
                "entries are repo-relative PATHS with forward slashes, not "
                "Windows paths and not regexes (a runner-specific pattern "
                "belongs in the template, not here)")
        spec = normalize_test_path(raw)
        if raw != spec or posixpath.isabs(spec) or spec.startswith("../"):
            raise QuarantineError(
                f"{where}.tests[{i}] test {raw!r} must be the repo-relative "
                "path git and the red-proof use — no leading './', no "
                "doubled or '.'/'..' segments, no surrounding whitespace, "
                f"not absolute. Write it as {spec.lstrip('./')!r}")
        excluded.append({**entry, "test": spec})
    from .gitops import render
    flags = " ".join(render(template, test=e["test"]) for e in excluded)
    names = [e["test"] for e in excluded]
    if run is not None:
        from . import ndjson
        # ONCE per run per (repo, set) — not once per application
        # (adversarial-review, both lenses): verify-red fires per task,
        # verify-green again per task, resolve-coverage-cmd once per repo, so
        # a 5-task run emitted ~12 identical records into the very gauge a
        # human reads to decide whether a run needs attention. The fact must
        # be on the dashboard; twelve copies of it drown the dashboard.
        prior = ndjson.read_records(run / "events.ndjson")
        if not any(e.get("kind") == "tests-quarantined"
                   and e.get("repo") == name and e.get("tests") == names
                   for e in prior):
            ndjson.append_record(run / "events.ndjson",
                                 {"kind": "tests-quarantined", "repo": name,
                                  "tests": names,
                                  "reasons": {e["test"]: e["reason"]
                                              for e in excluded},
                                  # SINGULAR `reason` too — that is the key
                                  # the metrics report's flagged table
                                  # renders, and `reasons` (the per-entry map
                                  # a machine reads) is not it, so this row
                                  # came out blank in the one surface a human
                                  # actually reads: the event named the
                                  # exclusions and the dashboard did not
                                  # (whole-branch adversarial review).
                                  "reason": f"{name}: excluded " + "; ".join(
                                      f"{e['test']} ({e['reason']})"
                                      for e in excluded)})
    if cmd.rstrip().endswith(flags):
        # Already applied — re-applying is the documented develop path
        # (`resolve-test-cmd` builds the `harness-test-cmd` header, which
        # develop-task.md then passes back as `--test-cmd` to verify-red) and
        # used to render `--exclude x --exclude x`. Checked AFTER the event
        # (re-verify finding: with the early return first, a run whose first
        # application came from a `--run`-less resolve-test-cmd never got its
        # flagged event at all — the exclusions applied invisibly).
        return cmd
    return f"{cmd} {flags}"


def quarantined_paths(config: dict, repo_path) -> set[str]:
    """Just the quarantined spec paths for a repo — for callers that must
    detect an OVERLAP rather than build a command (see gitops)."""
    _name, block = resolve_quarantine(config, repo_path)
    # normalized on the way out too, so the overlap comparison holds even for
    # a block written before the declaration-time spelling check existed
    return {normalize_test_path(str(e.get("test")))
            for e in (block.get("tests") or [])
            if isinstance(e, dict) and e.get("test")}


def verify(config: dict, workspace: Path | None = None) -> list[dict]:
    """Verification gates (a real gate, not a rubber-stamp): every check
    returns pass/fail/manual + remediation. Callers block on failures.
    `workspace`, when given, additionally re-checks the workspace-root-as-
    repo hazard for configs that PREDATE `write_section`'s write-time
    refusal or were hand-edited past it (re-review finding: write-time
    enforcement alone left old/edited configs reporting ok:true while still
    carrying the exact `git add -A` authority-file leak the refusal
    exists to stop) — and, since the repo gate now accepts a SUBTREE of a
    checkout, the wider containment form of the same hazard: a registration
    whose physical checkout HOLDS the workspace."""
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
    elif wi == "github-projects":
        ok, detail = _probe(["gh", "auth", "status"])
        add("work-item provider", "pass" if ok else "fail", detail,
            "gh auth login")
        prov = config.get("provider") or {}
        number = str(prov.get("github_project") or "").strip()
        owner = str(prov.get("github_project_owner") or "").strip()
        add("github_project", "pass" if number and owner else "fail",
            f"{owner or '(owner not set)'}/{number or '(number not set)'}",
            "set provider.github_project (the board's number) and "
            "provider.github_project_owner (its owner login, or '@me') "
            "— the two path segments of the board's URL "
            "(init-section --section provider)")
        if ok and number and owner:
            # Auth + config both present still isn't enough: `gh auth login`
            # grants `read:project` only if it was asked for, and the whole
            # adapter is unusable without it. Probe the BOARD, not the
            # token — one call proves reachability, scope, and that the
            # owner/number pair actually names something.
            reachable, board, why = _probe_json(
                ["gh", "project", "view", number, "--owner", owner,
                 "--format", "json"])
            detail = why or f"{owner}/{number} readable"
            if reachable and board.get("closed"):
                # A closed board reads perfectly and refuses every write, so
                # reachability alone would report a green workspace whose
                # every status write-back fails (adversarial-review, lens B).
                reachable, detail = False, f"{owner}/{number} is CLOSED"
            add("github_project reachable", "pass" if reachable else "fail",
                detail,
                "check the owner/number pair, reopen the board if it is "
                "closed, and grant the project scope: `gh auth refresh -s "
                "read:project` to read the board, `-s project` to also "
                "write status back")
            if reachable:
                # The one board-shaped key most likely to be mistyped was
                # the one nothing checked: a status field that does not
                # exist fails OPEN at fetch (empty state, so the
                # already-done guard can never fire) and CLOSED at every
                # write-back — /init-workspace passed green and the run
                # degraded silently, three flagged events later
                # (adversarial-review, both lenses).
                from .providers.github_projects_cli import keys_match
                wanted = str(prov.get("github_project_status_field")
                             or "Status").strip() or "Status"
                listed_ok, listed, why = _probe_json(
                    ["gh", "project", "field-list", number, "--owner", owner,
                     "--format", "json", "--limit", "100"])
                fields = [f for f in (listed.get("fields") or [])
                          if isinstance(f, dict)]
                single = next((f for f in fields
                               if keys_match(f.get("name"), wanted)
                               and f.get("options")), None)
                if not listed_ok:
                    # Discarding this flag made a FAILED probe read as a
                    # fact about the board ("'Status' is not a single-select
                    # field"), sending the user to change a correctly
                    # configured key (re-verification, finding C).
                    detail = f"could not list the board's fields: {why}"
                    remediation = ("grant the read scope (`gh auth refresh "
                                   "-s read:project`) and re-run; the "
                                   "status field could not be checked")
                elif single:
                    detail, remediation = f"'{wanted}'", ""
                else:
                    detail = (f"'{wanted}' is not a single-select field on "
                              f"the board — has: "
                              f"{', '.join(str(f.get('name')) for f in fields) or '(none)'}")
                    remediation = ("set provider.github_project_status_field "
                                   "to a single-select field on the board — "
                                   "milestone status write-back writes "
                                   "into it")
                add("github_project status field",
                    "pass" if (listed_ok and single) else "fail",
                    detail, remediation)
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
        top = gitops.work_tree_root(path)
        if ws_root is not None and Path(str(path)).resolve() == ws_root:
            add(f"repo:{name}", "fail", str(path),
                "the workspace root itself must not be a registered repo — "
                "`harness commit`'s `git add -A` would stage ai/** run-"
                "authority files (incl. human-input.ndjson) into project "
                "history; register the actual project checkout instead")
            continue
        # CONTAINMENT, not just equality — the gate below now passes any
        # path inside a work tree, so the workspace no longer has to BE the
        # registration to be inside its blast radius; it only has to sit
        # somewhere in the same physical checkout (`<checkout>/ws` alongside
        # `<checkout>/code/myapp`). Reproduced end to end: that registration
        # verified `pass`, then preflight's `ensure_default_branch` probed
        # dirt at the TOPLEVEL — which now contains the live run's own
        # `ws/ai/<run>/**` — and refused permanently, naming the harness's
        # own state files as the thing to go clean. Committing them to make
        # the refusal go away is worse: the `git checkout <default>` that
        # follows swaps the workspace's chain-sealed `state.yaml` and
        # `.claude/context/**` out from under the running run, which past a
        # reseal is unrecoverable. Pre-subtree this registration hard-FAILED
        # the repo gate, so none of it was reachable; the gate widening is
        # what makes it reachable, and this is where it gets closed.
        if (ws_root is not None and top is not None
                and ws_root.is_relative_to(top.resolve())):
            add(f"repo:{name}", "fail", f"{path} (subtree of {top})",
                f"the harness workspace ({ws_root}) lives INSIDE this "
                f"registration's git checkout ({top}) — every branch-safety "
                "probe this repo drives runs at that checkout, so the run's "
                "own ai/** files read as uncommitted project changes, and a "
                "branch switch there would swap the workspace's sealed "
                "state.yaml and .claude/context/** mid-run; move the "
                "workspace outside that checkout, or register a project "
                "checkout that does not contain it")
            continue
        # Inside a work TREE, not "has a .git of its own": a logical repo may
        # be registered by a SUBTREE of a physical checkout (`<checkout>/
        # frontend` — exactly what discover()'s monorepo_split proposes, and
        # what a .NET solution at the root plus a frontend app under it
        # requires). The old `.git`-exists gate could only ever pass a
        # checkout root, so it failed every split this workspace can now
        # represent. When the two differ the detail NAMES the physical
        # checkout: "registered here, but the git tree is over there" is the
        # fact a human reads this report to learn, and stating only the
        # registered path would quietly hide the shared-checkout relationship
        # that `add -A` scoping and the direct-branch refusal both hinge on.
        detail = str(path)
        if top is not None and top.resolve() != Path(str(path)).resolve():
            detail = f"{path} (subtree of {top})"
            # "inside a work tree" is also true of an UNTRACKED or ignored
            # directory (probed, git 2.55: `git -C <checkout>/generated
            # rev-parse --show-toplevel` answers `<checkout>` for a
            # gitignored `generated/`). Such a registration would verify
            # clean and then be un-runnable: `git worktree add` materializes
            # only what the branch tracks, so the task worktree comes up
            # without that directory at all — see gitops.has_tracked_files.
            # Caught HERE, where the fix is one commit or one config edit,
            # rather than mid-run where it is a wedged task. Root
            # registrations skip the probe entirely: an empty index is a
            # legitimate freshly-init'd checkout, and failing it would break
            # a registration shape that has always been supported.
            if not gitops.has_tracked_files(path):
                add(f"repo:{name}", "fail", detail,
                    f"nothing under this path is tracked by {top} — it is "
                    "inside the checkout but not in its index (untracked, "
                    ".gitignored, or spelled with a case the index doesn't "
                    "use), so a per-task `git worktree add` would produce a "
                    "worktree with no such directory in it and every task "
                    "command would run in a missing cwd; commit the subtree "
                    "to the checkout (or register the tracked spelling of "
                    "its path)")
                continue
        add(f"repo:{name}", "pass" if top is not None else "fail", detail,
            "path must be inside a git checkout (its root, or any subtree "
            "of one — a subtree registers as its own logical repo)")

    for name, path in repos.items():
        # Quarantine shape checked HERE, where fixing config is cheap
        # (adversarial-review, both lenses): `language.*` is not
        # schema-validated, so a malformed block used to surface only at the
        # first verify-red — deep inside develop, after preflight, plan,
        # plan-review and the plan gate — and then wedged every TDD task in
        # the repo. Rendering exercises every refusal the run would hit.
        try:
            quarantine_cmd(config, path, _test_cmd_for_name(config, name) or "x")
            cov = resolve_coverage_cmd(config, path)
            if cov:
                # the coverage path has its own template and its own command
                # shape, so it can refuse where the test path passed
                # (re-verify finding: a shell-composed coverage_cmd, or a
                # non-string coverage_exclude_template, sailed through
                # init-verify and died at harden instead)
                quarantine_cmd(config, path, cov, coverage=True)
            add(f"quarantine:{name}", "pass",
                f"{len(quarantined_paths(config, path))} quarantined test(s)",
                "")
        except QuarantineError as exc:
            add(f"quarantine:{name}", "fail", str(exc),
                f"fix language.repos.{name}.quarantine")

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


#: The plugin's own bash launchers (relative to plugin root) that MUST carry
#: the executable bit for hooks/run-guard's bash `exec` clause — and
#: bin/harness itself — to run at all. See hooks/run-guard's header for the
#: full residual: a mode-stripping distribution channel (GitHub "Download
#: ZIP", a zip-extraction library that drops unix modes, a manual
#: Windows->POSIX copy) can deliver these non-executable; bash's `exec`
#: clause then fails 126 with no fallback, the platform reads that non-2
#: exit as a NON-BLOCKING hook error, and every guard — including the
#: fail-closed spawn/skill guards — silently stops enforcing.
_LAUNCHER_FILES = ("hooks/run-guard", "bin/harness")


def _restore_launcher_exec_bits(plugin_root: Path | None = None) -> None:
    """Self-heals the executable bit on this plugin's OWN launchers at every
    bootstrap — `mark_bootstrapped` is the one call both a fresh `init` and
    an `init-finalize` re-run funnel through, so this is the one place a
    stripped bit gets caught for every path in.

    POSIX only: os.chmod's owner/group/other execute bits are meaningless on
    Windows, where this is a clean no-op (no error, no warning). `plugin_root`
    defaults to THIS installed plugin's own root — the same
    `Path(__file__).resolve().parent.parent` resolution `write_permissions`
    already uses, never a workspace path — but takes a parameter so a test
    can point it at a disposable fixture copy instead of chmod'ing the real
    repo.

    A chmod failure (read-only plugin directory, unusual filesystem) must
    not break bootstrap: warn on stderr naming the file and continue, same
    voice as the Qwen-symlink warnings below. Never silent — a launcher left
    non-executable means guards are off, which is the exact finding this
    self-heal exists to close."""
    if os.name == "nt":
        return
    import sys
    root = (plugin_root if plugin_root is not None
            else Path(__file__).resolve().parent.parent)
    for rel in _LAUNCHER_FILES:
        path = root / rel
        try:
            mode = path.stat().st_mode
            wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if wanted != mode:
                os.chmod(path, wanted)
        except OSError as exc:
            print(f"warning: could not restore the executable bit on "
                  f"{path} ({exc}) — a non-executable launcher fails "
                  "non-blocking and guard hooks silently stop enforcing; "
                  f"fix by hand: chmod +x {path}", file=sys.stderr)


def mark_bootstrapped(workspace: Path) -> None:
    _restore_launcher_exec_bits()
    write_section(workspace, "overrides",
                  {"bootstrap_completed": ndjson.now_iso()})
    if os.environ.get("QWEN_CODE") == "1":
        _link_qwen_context(workspace)


def _link_qwen_context(workspace: Path) -> None:
    """Qwen's extension installer rewrites `.claude/` → `.qwen/` in every
    installed `.md`/`.sh` file, so skills and agents under Qwen point the
    model at `.qwen/context/…` while the CLI and guards read and write the
    physical `.claude/context/` tree (Python is not rewritten by the
    installer). Aliasing `.qwen/context` to the real `.claude/context` via
    a symlink closes that split-brain without relocating state — model
    reads/writes through the rewritten path land in the single physical
    location, and Claude Code sessions are untouched. `.claude/context` is
    created by `write_section` before this runs (mark_bootstrapped is the
    last init step); the symlink is idempotent and re-points an existing
    stale/wrong *symlink* rather than failing on it.

    The link target is RELATIVE (`../.claude/context`), not absolute, so a
    workspace move or rename doesn't dangle it — the absolute form would.

    Adversarial-review hardening: only a symlink (never a regular file or
    directory the user may have placed at `.qwen/context`) is replaced —
    `unlink()` on a regular file would silently delete user data. When the
    host refuses symlinks altogether (unprivileged Windows without Developer
    Mode) OR a real file/directory occupies the link path, the failure is
    NOT silent: the dual-prefix acceptance in guard_write keeps the
    planner's context write unblocked, but without the symlink that write
    lands in a separate `.qwen/context` tree the CLI never reads — so a
    visible stderr warning names the requirement in both cases. Full
    `.qwen/context` round-trip needs the symlink; macOS/Linux always have
    it, Windows needs Developer Mode or an admin shell."""
    import sys
    target = Path("..") / ".claude" / "context"   # relative — survives a workspace move
    link = workspace / ".qwen" / "context"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".claude" / "context").exists():
        return
    if link.is_symlink():
        link.unlink()      # re-point a stale/wrong symlink, never a real file
    elif link.exists():
        # A real file/directory the user (or a prior tool) placed here —
        # leave it (refusing rather than clobbering matches this project's
        # standing convention for any registry/state write), but warn:
        # the outcome is the SAME broken round-trip as a refused symlink
        # (no link → .qwen/context writes lost to the CLI).
        print(f"warning: .qwen/context exists as a real "
              f"{'directory' if link.is_dir() else 'file'} (not a symlink) "
              f"and was left in place; Qwen's installer rewrites skill "
              f"paths to .qwen/context/, so without the symlink, context "
              f"writes through that path won't be read by the harness CLI. "
              f"Remove {link} and re-run /init-workspace to create the "
              f"symlink.", file=sys.stderr)
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Symlinks unavailable. The guard_write dual-prefix still ALLOWS a
        # planner context write through `.qwen/context/…`, but with no
        # symlink that write is functionally lost (the CLI reads only
        # `.claude/context`). Warn so the user knows the round-trip is
        # broken on this host rather than discovering it as silent data loss.
        print("warning: could not symlink .qwen/context -> .claude/context "
              "(symlinks unavailable on this host). Qwen's installer rewrites "
              "skill paths to .qwen/context/, so without the symlink, context "
              "writes through that path won't be read by the harness CLI. "
              "On Windows, enable Developer Mode or run as admin to create "
              "symlinks.", file=sys.stderr)


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
    if os.environ.get("QWEN_CODE") == "1":
        _write_qwen_settings(workspace, plugin_root, sorted(allow))
    return path


def _write_qwen_settings(workspace: Path, plugin_root: Path,
                         allow: list[str]) -> Path:
    """Qwen Code reads permissions and env from `<workspace>/.qwen/
    settings.json` (Claude Code reads `.claude/settings.json`), so the
    allowlist written above must be mirrored here or background agents hit a
    permission prompt on every `harness` call under Qwen. The same file
    also carries the `CLAUDE_PLUGIN_ROOT` env export: Qwen never exports
    that var itself (it substitutes the token textually in installed
    markdown and hook commands at install/load time, but not in the
    runtime-generated block messages guards.py emits), so exporting it via
    the workspace settings `env` block is what makes a model-recovered
    `${CLAUDE_PLUGIN_ROOT}/bin/harness ...` block message runnable.
    `loadEnvironment` writes `env` entries into `process.env` with
    set-if-unset semantics, so Claude Code's own value, when present,
    wins (this dual-write only fires under `QWEN_CODE=1`, when the var is
    guaranteed absent). Read-modify-write with set semantics, matching the
    `.claude` path's non-destructive merge."""
    path = workspace / ".qwen" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing_allow = set(settings.setdefault("permissions", {}).get("allow", []))
    existing_allow.update(allow)
    settings["permissions"]["allow"] = sorted(existing_allow)
    env = settings.setdefault("env", {})
    # Self-heal a stale value: if the previously-stored path no longer
    # exists on disk (the plugin was reinstalled/moved to a new location),
    # overwrite it with the current root. A deliberate user pin that still
    # points at a real directory is preserved; only the dangling case — the
    # harness's OWN prior write, now orphaned by a reinstall — self-heals.
    # Silent on the happy path (a routine reinstall is expected behavior,
    # not a signal worth surfacing).
    stored = env.get("CLAUDE_PLUGIN_ROOT")
    if not stored or not Path(stored).is_dir():
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
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
    d = repo_map_dir(workspace, repo_name)
    # The stamp is the only thing repo-map-check trusts, so stamping an
    # empty directory certifies a map that doesn't exist — "fresh" for the
    # next stale_after_commits commits, with nothing left to notice (usage-
    # review finding: the orchestrator stamps unconditionally after the
    # planner spawn returns, so a failed/empty spawn became a persistent
    # false-fresh). Content is anything but the stamp itself, counted
    # recursively — tiered maps nest detail files under areas/.
    has_content = d.is_dir() and any(
        p.is_file() and p.name != ".meta.json" for p in d.rglob("*"))
    if not has_content:
        raise ValueError(
            f"repo-map-stamp: no map content under {d} — generate the map "
            "first (planner spawn, `harness-mode: repo-map`), then stamp; "
            "stamping an empty map would false-report 'fresh' to "
            "repo-map-check")
    meta = {"sha": gitops.head_sha(repo), "at": ndjson.now_iso()}
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
