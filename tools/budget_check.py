"""Line-budget + citation-discipline checker (vision principle 7).

Runtime markdown (skills/, agents/ — the files loaded into model context at
run time) is capped: soft ~100 lines (warn), hard ~200 (error). Design docs,
README, and generated artifacts are exempt.

Duplication sweep: a block of >=5 identical consecutive non-blank lines
appearing in two or more runtime files violates "define once, cite elsewhere"
(error). This is the mechanical form of the original harness's duplicated-
prose audit, run from day 0 instead of retrofitted.

CLI:  python3 tools/budget_check.py [repo-root]     exit 0 ok / 1 violations
"""
from __future__ import annotations

import sys
from pathlib import Path

SOFT_CAP = 100
HARD_CAP = 200
DUP_WINDOW = 5
RUNTIME_DIRS = ("skills", "agents")


def runtime_md_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in RUNTIME_DIRS:
        base = root / dirname
        if base.is_dir():
            files.extend(sorted(base.rglob("*.md")))
    return files


def check_budgets(root: Path) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for path in runtime_md_files(root):
        # as_posix so a report line reads `agents/a.md` on every OS —
        # these strings are matched by tests and read by humans across
        # platforms, so the rendering must not vary with the host separator
        rel = path.relative_to(root).as_posix()
        count = len(path.read_text(encoding="utf-8").splitlines())
        if count > HARD_CAP:
            errors.append(f"{rel}: {count} lines exceeds hard cap {HARD_CAP}")
        elif count > SOFT_CAP:
            warnings.append(f"{rel}: {count} lines exceeds soft cap {SOFT_CAP}")
    return errors, warnings


def check_duplication(root: Path) -> list[str]:
    windows: dict[tuple, dict[str, int]] = {}
    for path in runtime_md_files(root):
        rel = path.relative_to(root).as_posix()  # separator-stable reports
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln]  # ignore blanks: dedup is about content
        for i in range(len(lines) - DUP_WINDOW + 1):
            key = tuple(lines[i : i + DUP_WINDOW])
            windows.setdefault(key, {}).setdefault(rel, i + 1)
    errors, reported = [], set()
    for key, locations in windows.items():
        if len(locations) < 2:
            continue
        pair = tuple(sorted(locations))
        if pair in reported:  # one report per file-set, not per overlapping window
            continue
        reported.add(pair)
        errors.append(
            f"duplicated block (>= {DUP_WINDOW} lines) across: "
            + ", ".join(f"{f}:{n}" for f, n in sorted(locations.items()))
            + f" — starts '{key[0][:60]}'"
        )
    return errors


def main(argv: list[str]) -> int:
    # UTF-8 output contract (see harness/__main__.py): the duplication
    # report embeds runtime-markdown content, which carries arrows and
    # em-dashes a cp1252 pipe cannot encode — a finding must print, not
    # crash with UnicodeEncodeError in place of the report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    errors, warnings = check_budgets(root)
    errors += check_duplication(root)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    scanned = len(runtime_md_files(root))
    print(f"{'OK' if not errors else 'INVALID'} — {scanned} runtime file(s), "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 0 if not errors else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
