"""`python3 -m harness <verb>` — see harness/cli.py."""
import os
import sys

# bin/harness sets PYTHONPATH so this package resolves regardless of the
# caller's cwd; sys.path is already seeded from it by the time this line
# runs, so popping it from the environment here doesn't affect our own
# imports — it only stops it leaking into subprocess.run() calls this CLI
# makes later (test/build/scan commands run in the TARGET repo, which must
# never see ai-sdlc-harness's own import path spliced into theirs).
os.environ.pop("PYTHONPATH", None)

# The CLI's output encoding is a CONTRACT, not a locale accident: payloads
# are printed with ensure_ascii=False (cli.py), and on Windows the default
# pipe encoding is cp1252 — which mojibakes every em-dash/arrow in a detail
# string for whoever parses the output (the orchestrator, the test suite).
# UTF-8 on both streams, unconditionally, on every OS. The error handlers
# must be RESTATED per stream — reconfigure resets errors= to "strict",
# and stderr's documented default is "backslashreplace" on every platform
# (adversarial-review finding: dropping it let an un-encodable char turn a
# printed error into a raised one). stdout's default IS "strict" — kept.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

if sys.version_info < (3, 10):  # a bare-`python` fallback (bin/harness,
    # hooks.json) can land on an ancient interpreter — refuse with the
    # actual requirement instead of a downstream SyntaxError
    sys.exit(f"ai-sdlc-harness requires Python 3.10+ — this is "
             f"{sys.version.split()[0]}")

from .cli import main  # noqa: E402  (must follow the PYTHONPATH scrub above)

sys.exit(main())
