# Step: security (orchestrator-owned, fully mechanical)

```
${CLAUDE_PLUGIN_ROOT}/bin/harness security-scan --run <run>
```

One call scans every registered repo (`security.scan_cmd` is per repo,
language-config convention — different repos may need different scanners;
"no scanner" for a repo records informational). Runs concurrently, parses
each repo's max severity via the declared regex, aggregates to ONE true max
across all repos (never a per-call overwrite — a clean repo must not mask
another's critical finding), writes `<run>/reports/security.md`, records
the `security.max_severity` artifact.

Then advance. The manifest's `when` predicate decides the rest mechanically:
severity ≥ threshold → ⟨approve-security⟩ is REQUIRED next (fix-now / waive /
defer, see `gate.md`); below threshold → the gate is skipped and `pre-pr` is
next. `${CLAUDE_PLUGIN_ROOT}/bin/harness show` + a refused advance tell you which.
