"""ai-sdlc-harness core package.

M0 shipped schema validation for the declared data (pipeline manifest, task
FSM, surfaces, config defaults); M1+ added the owned entry points (state
transitions, commit, merge-task, publish-mirror, sync-branch, verify-red,
log-event) per docs/build-plan.md.
"""
import json as _json
from pathlib import Path as _Path


def _read_version() -> str:
    """Read from .claude-plugin/plugin.json — the ONE place the version is
    bumped (/bump-version) — rather than a second hardcoded copy here that
    can silently drift out of sync with it (adversarial-review finding:
    this stayed "0.1.0-m0" through 12 releases)."""
    plugin_json = _Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return _json.loads(plugin_json.read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, ValueError):
        return "0.0.0-unknown"


__version__ = _read_version()
