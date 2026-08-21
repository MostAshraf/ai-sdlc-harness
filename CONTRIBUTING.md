# Contributing

Thanks for looking. This repo is small, opinionated, and mechanically enforced — most of what follows is about the enforcement, not about etiquette.

## Before you write code

**Open an issue first for anything non-trivial.** The pipeline is declared data ([pipeline/manifest.yaml](pipeline/manifest.yaml), [pipeline/surfaces.yaml](pipeline/surfaces.yaml), [pipeline/task-fsm.yaml](pipeline/task-fsm.yaml)) read by both the orchestrator and the enforcement layer. A change that looks local often has to move in three places at once, and it is much cheaper to agree on the shape before the diff exists.

Bug reports and questions need no ceremony — [open an issue](https://github.com/MostAshraf/ai-sdlc-harness/issues/new/choose).

## Setup

Python 3.10+ and PyYAML. That is the whole dependency list, and it should stay that way.

```sh
git clone https://github.com/MostAshraf/ai-sdlc-harness.git
cd ai-sdlc-harness
python3 -m venv .venv && .venv/bin/pip install pyyaml
```

On Windows the interpreter lands under `Scripts\` instead of `bin/`.

To run your working copy inside a CLI without disturbing an installed version:

```sh
claude --plugin-dir ./ai-sdlc-harness    # per-session, nothing global changes
qwen extensions link /path/to/ai-sdlc-harness
```

## The three checks

Every PR runs all three, on Linux/macOS/Windows × Python 3.10/3.14. All lanes are enforcing. Run them locally first:

```sh
.venv/bin/python -m harness.schema          # declared data vs the fixed vocabulary
.venv/bin/python tools/budget_check.py      # line budget + duplication sweep
.venv/bin/python tools/fasttest.py          # the suite, sharded across workers
```

**`harness.schema`** validates the manifest, FSM, surfaces, and config defaults against a fixed vocabulary. If you add a step, mode, gate, provider, or agent shape, it is not real until the schema knows about it.

**`budget_check.py`** caps runtime markdown (`skills/`, `agents/` — the files loaded into model context at run time) at ~100 lines soft, 200 hard, and errors on any block of 5+ identical consecutive lines appearing in two runtime files. Design docs and the README are exempt. The rule is *define once, cite elsewhere*; if a step file is growing past budget, the fix is almost always extraction, not compression.

**The test suite** is stdlib `unittest` only — no pytest, no plugins. Guard behavior is tested via subprocess against real hook payloads, and git machinery against real temp repos, because that is the only way those tests can be honest. `tools/fasttest.py` runs that same suite sharded by TestCase class across worker processes — a count guard fails the run if the shards' summed test count ever drifts from plain `unittest discover`, and `python -m unittest discover -s tests` remains the serial equivalent. The suite's isolation contract is what makes sharding safe — every test builds its state in its own `tempfile.mkdtemp()` workspace — so **a test must never write a fixed shared path or chdir**: it would pass serially and collide in parallel.

## Conventions worth knowing

**Windows is a first-class lane, not a courtesy.** Path handling, shell selection, and file encoding all differ, and the suite will catch you. Assume nothing about the host shell: hooks route through launcher pairs (`hooks/run-guard` + `.cmd`) that must behave identically under bash and `cmd.exe`.

**Tests come before implementation, and the harness proves it.** That is the product's whole thesis; it applies here too.

**Guard fail-open vs fail-closed is a deliberate, tested choice per guard.** If you touch [hooks/guards.py](hooks/guards.py), say in the PR which policy you picked and why. The spawn guard is fail-closed even on ambiguity, and stays that way.

**No new runtime dependencies** without a strong argument. The install path has to stay a clone plus PyYAML.

**Comments explain *why*.** The existing ones record the failure that motivated the code — that is the standard to match, not a house style you can skip.

## Pull requests

- Branch off `main`. One coherent change per PR.
- Add a `## [Unreleased]` entry in [CHANGELOG.md](CHANGELOG.md) for anything user-visible. Do **not** bump the version — versions move only at release.
- Update the README if you changed what a first-time visitor needs to know.
- Green CI is required. Windows lane included.
- PRs are squash-merged, so the PR title becomes the commit message.

## Reporting a security issue

Do not open a public issue. Email the maintainer via the address on the [GitHub profile](https://github.com/MostAshraf), or use GitHub's private vulnerability reporting on this repo.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
