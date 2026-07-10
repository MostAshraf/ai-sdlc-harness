"""v2.x workspace adoption — THE FORK SEAM (/migrate-workspace).

Forks of this harness ship different adoption logic by replacing THIS
MODULE (plus the skills/migrate-workspace walker) and nothing else. The
contract the CLI verbs and the skill depend on:

    detect(workspace)    -> {legacy, already_bootstrapped, evidence, warnings}
                            orientation only; never raises on absence
    inventory(workspace) -> {runs, in_flight, aborted, legacy_context_files}
                            what the legacy workspace still contains
    extract(workspace)   -> {sections, optional_overrides, unmapped, notes}
                            config proposals in EXACTLY the shape
                            `init-section --json` accepts, self-nested

All three are READ-ONLY over the workspace: nothing here writes, so a
parsing bug can mispropose but never corrupt. Writes happen only through
the owned init path (init-section -> init-verify -> init-finalize), which
is shared with /init-workspace and NOT part of this seam.

Run history is deliberately NOT migrated: v3.0 authority is HMAC-sealed
evidence (state.yaml, ledgers, red-proofs) that v2.x never produced —
fabricating it would forge the audit trail, and grandfathering it would
punch permanent exemption holes in the enforcement layer. In-flight v2.x
stories finish on v2.x; old `ai/` dirs stay in place as readable archives
(v3.0 run discovery keys on state.yaml and skips them).

v2.x configs are LLM-written markdown whose exact shape drifts across
workspaces, so every field parses independently and tolerantly: a field
that doesn't parse is simply absent from the proposal — the interview
asks for it — never an error that blocks the fields that did parse.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# v2.x provider spellings -> v3.0 module names. v2.x named transports
# (`glab-cli`) where v3.0 names forges/trackers; unknown spelling -> None,
# and the interview asks rather than this module guessing.
WORK_ITEM_ALIASES = {
    "local-markdown": "local-markdown", "local": "local-markdown",
    "github": "github", "github-cli": "github", "gh-cli": "github",
    "gitlab": "gitlab", "gitlab-cli": "gitlab", "glab-cli": "gitlab",
    "ado": "ado", "ado-cli": "ado", "az-boards": "ado",
    "azure-devops": "ado", "azure-devops-cli": "ado",
    "ado-mcp": "ado-mcp", "azure-devops-mcp": "ado-mcp",
    "jira": "jira", "jira-mcp": "jira",
    "zoho": "zoho", "zoho-mcp": "zoho",
}
GIT_ALIASES = {
    "local": "local", "none": "local",
    "github": "github", "github-cli": "github", "gh-cli": "github",
    "gitlab": "gitlab", "gitlab-cli": "gitlab", "glab-cli": "gitlab",
    "ado": "ado", "ado-cli": "ado", "azure-devops": "ado",
    "azure-devops-cli": "ado", "ado-mcp": "ado-mcp",
}

# provider-config.md settings bullets worth carrying — only keys v3.0
# actually consumes (initws.verify / the provider modules).
PROVIDER_SETTING_KEYS = ("stories_dir", "github_repo", "gitlab_repo",
                        "ado_org", "ado_project")

# v2.1 naming templates -> v3.0 naming slots, with the legal .format fields
# per slot (gitops.render raises on any other key, so a template carrying
# an untranslatable placeholder is dropped to `unmapped`, never proposed).
# commit_format/tag_format have no slot at all: v3.0 commit messages come
# from declared commit classes, and tags are not a v3.0 naming concept.
_NAMING_SLOTS = {"branch_format": "branch", "pr_title_format": "pr_title"}
_NAMING_FIELDS = {
    "branch": {"story_id": "id", "slug": "slug", "type": "type"},
    # pr_title has no {slug}; the raw-title {summary} is the closest v3.0
    # field — the confirm step shows the translated template before writing.
    "pr_title": {"story_id": "id", "type": "type", "slug": "summary"},
}

_PROVIDER_LINE = re.compile(
    r"\*\*\s*Work\s*Item\s*Provider\s*\*\*\s*:\s*`?([\w.-]+)`?", re.IGNORECASE)
_GIT_LINE = re.compile(
    r"\*\*\s*Git\s*Provider\s*\*\*\s*:\s*`?([\w.-]+)`?", re.IGNORECASE)
_ACTIVE_LINE = re.compile(r"^Workflow active:\s*(\S+)", re.MULTILINE)
_NON_TERMINAL = ("Pending", "In Progress", "In Review")


def _context(workspace: Path) -> Path:
    return workspace / ".claude" / "context"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def detect(workspace: Path) -> dict:
    """Evidence-based fingerprinting, never a guess: each marker is named
    so the skill can show the user WHY this looks like a v2.x workspace."""
    ctx = _context(workspace)
    evidence, warnings = [], []
    if (ctx / "provider-config.md").is_file():
        evidence.append(".claude/context/provider-config.md "
                        "(v2.x interview output)")
    if "Bootstrap completed" in _read(ctx / "state.md"):
        evidence.append(".claude/context/state.md records a v2.x bootstrap")
    trackers = (sorted((workspace / "ai").glob("*/tracker*.md"))
                if (workspace / "ai").is_dir() else [])
    if trackers:
        evidence.append(f"{len(trackers)} v2.x tracker file(s) under ai/")
    bootstrapped = False
    overrides = ctx / "overrides.yaml"
    if overrides.is_file():
        try:
            loaded = yaml.safe_load(overrides.read_text(encoding="utf-8"))
            if loaded is not None and not isinstance(loaded, dict):
                # a valid-YAML list/scalar is exactly as unreadable AS
                # CONFIG as a syntax error — same fail-closed arm
                # (adversarial-review finding: this shape slipped through
                # as "not bootstrapped")
                raise yaml.YAMLError(f"top level is "
                                     f"{type(loaded).__name__}, not a mapping")
            bootstrapped = bool((loaded or {}).get("bootstrap_completed"))
        except yaml.YAMLError:
            # Fail CLOSED: an unreadable overrides.yaml can't prove the
            # workspace ISN'T bootstrapped, and migrating on top of a live
            # v3.0 config is the costlier mistake. (Via the CLI this is
            # unreachable — load_declared refuses corrupt context yaml
            # before dispatch — but the module must hold its own contract.)
            bootstrapped = True
            warnings.append(".claude/context/overrides.yaml is unreadable — "
                            "treating the workspace as already bootstrapped; "
                            "fix or remove that file first")
    return {"legacy": "v2.x" if evidence else None,
            "already_bootstrapped": bootstrapped,
            "evidence": evidence, "warnings": warnings}


def _tracker_in_flight(tracker: Path) -> bool:
    """A task-table row still Pending/In Progress/In Review means the v2.x
    run never finished. The Status column is resolved from the header row
    (never a fixed index), and only that cell is scanned — a task TITLE
    containing 'In Progress' can't false-flag the run."""
    status_idx = None
    for line in _read(tracker).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if status_idx is None:
            lowered = [c.lower() for c in cells]
            # case-insensitive, and "ID" counts as the task-column signal —
            # v2.x tracker headers drift (adversarial-review finding: an
            # `| ID | ... | status |` header was never recognized, so the
            # run silently escaped the in-flight warning)
            if "status" in lowered and any("task" in c or c == "id"
                                           for c in lowered):
                status_idx = lowered.index("status")
            continue
        if set(stripped) <= {"|", "-", " ", ":"}:  # header separator row
            continue
        if status_idx < len(cells) and any(
                word in cells[status_idx] for word in _NON_TERMINAL):
            return True
    return False


def _same_story(a: str, b: str) -> bool:
    """`US-2` and `US-2-add-multiply` are one story spelled two ways;
    `US-1` and `US-11-widgets` are not — the shorter id must end at a `-`
    boundary in the longer (re-verification finding: a bare startswith
    over-merged genuinely different stories)."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter) and longer[len(shorter):][:1] == "-"


def inventory(workspace: Path) -> dict:
    """What the legacy workspace still holds. Informational only — nothing
    downstream gates on it mechanically; the skill presents it so the
    human decides what to do with in-flight v2.x work."""
    ai = workspace / "ai"
    runs, in_flight, aborted = 0, [], []
    if ai.is_dir():
        for run in sorted(p for p in ai.iterdir() if p.is_dir()):
            live = run / "tracker.md"
            variants = [live, run / "tracker.archived.md",
                        run / "tracker.aborted.md"]
            if not any(v.is_file() for v in variants):
                continue
            runs += 1
            if not live.is_file():
                if (run / "tracker.aborted.md").is_file():
                    aborted.append(run.name)
                continue
            if _tracker_in_flight(live):
                # run dirs are `<ISO date>-<id>`; regex, not a fixed slice —
                # non-zero-padded legacy dates mangled `name[11:]`
                dm = re.match(r"\d{4}-\d{1,2}-\d{1,2}-(.+)", run.name)
                in_flight.append({"id": dm.group(1) if dm else run.name,
                                  "run_dir": f"ai/{run.name}",
                                  "evidence": "tracker.md has non-terminal "
                                              "task rows"})
    active = _ACTIVE_LINE.search(_read(_context(workspace) / "state.md"))
    token = active.group(1) if active else ""
    # `Workflow active: none` is a real v2.x completed-state spelling, and
    # dedupe must hold whichever of the two ids is the longer spelling
    if token and token.lower() not in _PLACEHOLDER_VALUES and not any(
            _same_story(token, entry["id"]) for entry in in_flight):
        in_flight.append({"id": token, "run_dir": None,
                          "evidence": "state.md 'Workflow active:' line"})
    ctx = _context(workspace)
    legacy_files = (sorted(p.name for p in ctx.glob("*.md"))
                    if ctx.is_dir() else [])
    return {"runs": runs, "in_flight": in_flight, "aborted": aborted,
            "legacy_context_files": legacy_files}


# Values that are placeholders, not configuration — plus anything carrying
# `<`/`>` (`<org>/<repo>` template text). Shared by the settings scan and
# the state.md active-workflow line.
_PLACEHOLDER_VALUES = {"", "(not set)", "none", "n/a", "-", "—", "tbd", "todo"}


def _settings(text: str, key: str) -> list[str]:
    """ALL occurrences, deduped in order — provider-config.md routinely
    keeps reference bullets for providers NOT in use, and a first-match
    scan carried a placeholder over the real value (adversarial-review
    finding). Placeholder-shaped values are dropped here; the caller
    proposes only an unambiguous single survivor."""
    hits = re.findall(
        rf"\*\*\s*{re.escape(key)}\s*\*\*\s*:\s*`?([^`\n]+?)`?\s*$",
        text, re.IGNORECASE | re.MULTILINE)
    return [v for v in dict.fromkeys(h.strip() for h in hits)
            if v.lower() not in _PLACEHOLDER_VALUES
            and not re.search(r"[<>]", v)]


def _table_rows(text: str) -> list[list[str]]:
    # fenced blocks are examples, not data — LLM-written v2.x docs love a
    # fenced sample table, and every `|` line in one used to become a
    # proposed repo row (adversarial-review finding). An UNCLOSED fence
    # eats to EOF: absent proposals fall through to the interview, which
    # beats resurrecting sample rows as data (re-verification finding).
    text = re.sub(r"^\s*```.*?(?:^\s*```\s*$|\Z)", "", text,
                  flags=re.MULTILINE | re.DOTALL)
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= {"|", "-", " ", ":"}:
            continue
        rows.append([c.strip() for c in stripped.strip("|").split("|")])
    return rows


def _extract_provider(text: str, unmapped: list, notes: list) -> dict:
    provider: dict = {}
    for regex, key, aliases in ((_PROVIDER_LINE, "work_item",
                                 WORK_ITEM_ALIASES),
                                (_GIT_LINE, "git", GIT_ALIASES)):
        m = regex.search(text)
        if not m:
            continue
        raw = m.group(1).lower()
        mapped = aliases.get(raw)
        if mapped is None:
            unmapped.append(f"provider-config.md: unknown {key} provider "
                            f"'{raw}' — the interview will ask")
        else:
            provider[key] = mapped
            if mapped != raw:
                notes.append(f"{key} provider '{raw}' maps to v3.0 "
                             f"'{mapped}'")
    for key in PROVIDER_SETTING_KEYS:
        values = _settings(text, key)
        if len(values) == 1:
            provider[key] = values[0]
        elif values:
            notes.append(f"provider-config.md: multiple values for {key} "
                         f"({', '.join(values)}) — set it manually at the "
                         "confirm step")
    return provider


def _extract_repos(text: str, notes: list, workspace: Path) -> dict:
    rows = _table_rows(text)
    if not rows:
        return {}
    header = [c.lower() for c in rows[0]]

    def col(*words):
        for i, cell in enumerate(header):
            if any(w in cell for w in words):
                return i
        return None

    name_i, path_i = col("repo", "name"), col("path")
    repos = {}
    if name_i is None or path_i is None:
        return {}
    for row in rows[1:]:
        if max(name_i, path_i) >= len(row) or not row[name_i] or not row[path_i]:
            continue
        if [c.lower() for c in row] == header:
            continue           # a second table restating the header row
        name, path = row[name_i], Path(row[path_i])
        if not path.is_absolute():
            # anchor at the workspace like stories_dir — a verbatim
            # relative path resolves against whatever cwd EVERY later
            # consumer happens to have (adversarial-review finding: the
            # existence note itself was already cwd-dependent)
            path = workspace / path
        if path.resolve() == workspace.resolve():
            # proposing this would only defer to a guaranteed apply-time
            # ValueError (write_section's workspace-root refusal) with no
            # in-skill resolution — surface the layout boundary instead
            notes.append(f"repo '{name}' is the workspace root itself — "
                         "v3.0 refuses that registration (harness commit "
                         "stages the whole tree, which would leak ai/** "
                         "run-authority files); dropped from the proposal — "
                         "register a separate project checkout")
            continue
        repos[name] = str(path)
        if not (path / ".git").exists():
            notes.append(f"repo '{name}': {path} is not a git "
                         "checkout on this machine — re-point it at the "
                         "confirm step or init-verify will fail")
    return repos


def _extract_language(text: str) -> dict:
    """Per-repo `### <name>` sections; only the two keys v3.0 consumes
    (test_cmd via resolve_test_cmd, coverage_cmd via harden)."""
    out: dict = {}
    for m in re.finditer(r"^###\s+(\S+)\s*$(.*?)(?=^###\s|\Z)", text,
                         re.MULTILINE | re.DOTALL):
        name, body = m.group(1), m.group(2)
        entry = {}
        for v21_key, v3_key in (("test_command", "test_cmd"),
                                ("coverage_command", "coverage_cmd")):
            # tolerate quotes AND backticks around the command — a
            # backticked value carried verbatim reaches `shell=True` as
            # command substitution (adversarial-review finding)
            fm = re.search(rf'^-\s*{v21_key}:\s*["`]?([^"`\n]+?)["`]?\s*$',
                           body, re.MULTILINE)
            if fm and fm.group(1).strip():
                entry[v3_key] = fm.group(1).strip()
        if entry:
            out[name] = entry
    return out


def _extract_naming(text: str, unmapped: list) -> dict:
    out = {}
    for m in re.finditer(r"^(\w+_format):\s*(.+)$", text, re.MULTILINE):
        v21_key, template = m.group(1), m.group(2).strip()
        slot = _NAMING_SLOTS.get(v21_key)
        if slot is None:
            unmapped.append(f"naming-config.md: {v21_key} has no v3.0 slot "
                            "(commit messages come from declared commit "
                            "classes; tags are not a naming concept) — set "
                            "overrides by hand if still wanted")
            continue
        fields = _NAMING_FIELDS[slot]
        translated = re.sub(
            r"\$\{(\w+)\}",
            lambda mm: ("{%s}" % fields[mm.group(1)]
                        if mm.group(1) in fields else mm.group(0)),
            template)
        # Refuse ANY residue, not just `${`: a `$story_id` or `{story_id}`
        # spelling sails through .format silently-literal or explodes at
        # preflight with "needs param" long after migration ended
        # (adversarial-review finding). gitops.render's legal field set is
        # the arbiter.
        legal = set(fields.values())
        braces = re.findall(r"\{(\w+)\}", translated)
        if "$" in translated or any(b not in legal for b in braces):
            unmapped.append(f"naming-config.md: {v21_key} ({template}) uses "
                            f"placeholders with no v3.0 {slot} field — "
                            "legal fields: " + ", ".join(sorted(legal)))
            continue
        out[slot] = translated
    return out


def extract(workspace: Path) -> dict:
    """Config proposals only — `sections` values go VERBATIM to
    `init-section --json` (self-nested, per that verb's contract).
    `optional_overrides` is kept apart on purpose: translated v2.1 naming
    is usually the v2.1 DEFAULT the user never chose, and blanket-carrying
    it would freeze e.g. a `feature/` branch prefix onto bug fixes — the
    skill offers it opt-in instead of writing it blindly."""
    ctx = _context(workspace)
    unmapped: list = []
    notes: list = []
    sections: dict = {}

    provider = _extract_provider(_read(ctx / "provider-config.md"),
                                 unmapped, notes)
    if provider.get("work_item") == "local-markdown":
        raw_dir = provider.get("stories_dir")
        if raw_dir:
            stories = Path(raw_dir)
            if not stories.is_absolute():
                stories = workspace / stories
            if not stories.is_dir():
                notes.append(f"stories_dir '{raw_dir}' does not exist on "
                             "this machine — confirm before writing")
    if provider:
        sections["provider"] = {"provider": provider}

    repos = _extract_repos(_read(ctx / "repos-paths.md"), notes, workspace)
    if repos:
        sections["repos"] = {"repos": repos}

    language = _extract_language(_read(ctx / "language-config.md"))
    if language:
        sections["language"] = {"language": {"repos": language}}

    naming = _extract_naming(_read(ctx / "naming-config.md"), unmapped)

    if (ctx / "cost-config.md").is_file():
        unmapped.append("cost-config.md: v3.0 has no cost config — "
                        "tokens.ndjson records real per-invocation spend")
    for extra in ("conventions.md", "repos-metadata.md"):
        if (ctx / extra).is_file():
            unmapped.append(f"{extra}: v2.x prose the v3.0 pipeline does "
                            "not consume — its successor (the repo map) is "
                            "generated, never hand-carried")

    return {"sections": sections,
            "optional_overrides": {"naming": naming} if naming else {},
            "unmapped": unmapped, "notes": notes}
