# Security Policy

## Supported versions

The latest release on the `3.x` line is supported. Fixes ship forward, not as backports.

| Version | Supported |
|---|---|
| 3.x (latest release) | ✅ |
| 3.x (older) | Upgrade first |
| 2.x | ❌ — see `/migrate-workspace` |

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting:

→ [Report a vulnerability](https://github.com/MostAshraf/ai-sdlc-harness/security/advisories/new)

Please include the CLI (Claude Code or Qwen Code), OS, harness version, and a reproduction. You will get an acknowledgement as promptly as circumstances allow, and credit in the release notes unless you would rather not have it.

## What counts as a vulnerability here

This project's security surface is *integrity of the audit trail and the guard layer*, not a network service. Reports in scope include:

- **Forging evidence** — any way to write a gate approval, a reviewer verdict, a red-proof, or a token record that the owned verbs did not produce, including through the hook launchers.
- **Disarming a guard** — any way to make a recognised violation pass: bypassing the spawn guard's manifest check, the write guard's path confinement, the reviewer's read-only grant, the pre-red test-first lock, or the raw-git block inside a bootstrapped workspace.
- **Breaking the seal silently** — any out-of-band edit to `state.yaml`, a ledger, or a red-proof that does *not* surface as exit code `3`.
- **Leaking workspace secrets** — anything that exposes `.claude/context/.harness-key` or the verbatim contents of `human-input.ndjson` outside the workspace, including via the `ai/**` mirror published onto a feature branch.
- **Arbitrary execution through declared data** — a manifest, config, or work item whose contents cause code execution beyond the configured commands.

## Known limits, deliberately accepted

These are documented trade-offs, not vulnerabilities. A report describing one of them is welcome as an *issue*, but it is already known:

- The bootstrap marker (`.claude/context/overrides.yaml`) is an ordinary config file, not chain-sealed. Deleting it turns the raw-git block back off.
- A session rooted directly in a repo registered to a *sibling* workspace is not recognised as belonging to that workspace, so run-scoped guards do not fire there.
- A plugin installed at a path containing a space or a cmd-reserved character (`( ) & ^ % = , ;`) cannot launch its hooks under Qwen Code's `cmd.exe` fallback.
- Qwen Code's `security.folderTrust.enabled` on an untrusted workspace drops workspace settings entirely, taking the permission allowlist with it.
- The harness constrains an agent operating in good faith and makes bad-faith action *evident*. It is not a sandbox and does not claim to contain a hostile model with shell access.
