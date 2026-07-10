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

from .cli import main  # noqa: E402  (must follow the PYTHONPATH scrub above)

sys.exit(main())
