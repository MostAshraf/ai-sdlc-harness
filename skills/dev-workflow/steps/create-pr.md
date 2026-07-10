# Step: create-pr (orchestrator-owned, fully mechanical)

The feature branch must exist on the remote before the provider CLI can open
a PR/MR against it — push it first (owned entry point, RC1: never a raw
`git push`):

```
${CLAUDE_PLUGIN_ROOT}/bin/harness push --repo <repo> --branch <feature-branch> --run <run>
```

Then create the PR:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness create-pr --repo <repo> --run <run>
```

Creates the PR via the configured git provider (title from the declared
`pr_title` template; base branch is the one `preflight` resolved and
recorded for this repo — never a hardcoded guess), records the `pr`
artifact (keyed by repo name — one PR per repo in multi-repo runs).

**Provider outage escape hatch:** if the provider CLI cannot create the
PR (infrastructure fault — e.g. a proxy 404ing path-encoded project
lookups) but the branch is pushed, the human creates the PR by hand and
you record it with `--url <its-url>` on the same command (the URL must
end in the PR/MR number — the comment loop derives the id from it).
Recording stays owned: same artifact, same locking, plus a
`pr-recorded-manually` audit event. Never hand-edit state.yaml, never
skip recording.

Then publish the mirror **once per preflighted repo** (SKILL.md's Publish
rule) so the final run snapshot lands in each PR's branch — `--push` is
REQUIRED here: the PR was created from the already-pushed branch, so a
mirror commit made after it stays local forever without its own push
(field finding: every run ended with its audit snapshot stranded one
commit ahead of the remote, invisible to the PR reviewer):
`${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --push --run <run>`.
Advance to `reconcile`. The `pr-comments` group is now available on demand
(round-triggered, repeatable).
