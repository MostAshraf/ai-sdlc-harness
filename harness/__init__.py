"""ai-sdlc-harness core package.

M0 shipped schema validation for the declared data (pipeline manifest, task
FSM, surfaces, config defaults); M1+ added the owned entry points (state
transitions, commit, merge-task, publish-mirror, sync-branch, verify-red,
log-event) per docs/build-plan.md.
"""
import json as _json
import os as _os
from pathlib import Path as _Path


def qwen_cli_detected() -> bool:
    """True when the calling process runs inside Qwen Code.

    Two spellings, two delivery paths (measured live on Qwen Code 0.22.2):
    `QWEN_CODE` ("1") reaches run_shell_command children — where the CLI
    and init-workspace execute — but NOT hook subprocesses; hooks receive
    `QWEN_CODE_CLI` (the resolved cli-entry.js path) instead. The
    measurement came out of a since-removed guard_spawn branch that keyed
    on `QWEN_CODE` alone and so had never once fired in a real hook run.

    Callers today are the two surfaces where the platform genuinely
    changes behaviour: `initws` (mirroring permissions and the
    `CLAUDE_PLUGIN_ROOT` export into `.qwen/settings.json`, symlinking
    `.qwen/context`) and `cli` (the resolve-model notice — Qwen's agent
    tool takes no model parameter). The hook guards deliberately do NOT
    consult it: they are platform-blind, keyed on the evidence in the
    payload (response shapes, stub envelopes) rather than on which CLI
    spawned them, so one rule holds identically on both platforms.

    Truthy-presence, never `== "1"`: any spelling a CLI revision ships
    keeps detection on, and a stray value can only over-detect — a visible
    failure (a `.qwen/` tree or a notice where none was wanted) rather
    than the silent under-detection that hid the dead branch."""
    return bool(_os.environ.get("QWEN_CODE")
                or _os.environ.get("QWEN_CODE_CLI"))


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
