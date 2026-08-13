<!-- The PR title becomes the squash-merge commit message. Make it read as one. -->

## What changed, and why

<!-- The failure or gap this closes. Link the issue if there is one. -->

## Checks

<!-- All three run in CI on Linux/macOS/Windows × Python 3.10/3.14. Run them locally first. -->

- [ ] `python -m harness.schema` — declared data validates
- [ ] `python tools/budget_check.py` — line budget + duplication sweep clean
- [ ] `python -m unittest discover -s tests` — suite green, count noted below
- [ ] Tests added for the behavior this changes (a failing-first test, if it's a fix)

Test count before → after:

## Blast radius

- [ ] Touches declared data (`pipeline/*.yaml`, `config/defaults/*`) — schema updated to match
- [ ] Touches `hooks/guards.py` — fail-open/fail-closed policy stated below and tested
- [ ] Touches a `harness` verb's contract (args, exit codes, ledger writes)
- [ ] Touches Windows-sensitive paths, shell selection, or file encoding
- [ ] None of the above

<!-- If you ticked the guards box: which policy did you choose, and why? -->

## Docs

- [ ] `## [Unreleased]` entry added to CHANGELOG.md for anything user-visible
- [ ] README updated, if this changes what a first-time visitor needs to know
- [ ] Version files **not** bumped — versions move only at release
