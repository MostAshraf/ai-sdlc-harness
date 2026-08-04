# Step: confirm-repo (orchestrator-owned, quick mode only)

Quick mode has no `intake`, so it never gets intake's `## Target Repos`
proposal — yet `fetch` still seeded `T1` with `repos[0]`, a **positional
default with no content analysis**. In a single-repo workspace that's the only
repo and this step is skipped outright. Here it isn't: `repos.yaml` lists more
than one, and nothing yet has decided which one this work item belongs to.

**The cursor cannot leave this step until the confirmation below is recorded.**
That is enforced, not advisory — `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to
preflight` refuses outright, and that refusal is what stops `preflight` from
cutting a branch in the wrong repo.

## Propose one repo, from evidence

The registered repos are the entries of `.claude/context/repos.yaml` — short
NAME on the left, absolute PATH on the right. Read that file first; it is both
the candidate list and the source of the `--repo` value below.

Then read, in this order:

1. **Every registered repo's repo-map `index.md`**
   (`.claude/context/repo-map/<repo-name>/index.md`, keyed by the NAME from
   `repos.yaml`) — its Purpose and Stack sections are usually decisive on their
   own (a Nuxt SSR frontend and a Spring Boot service do not get confused for
   one another). Read only the `index.md` files here, not area files.
2. **`<run>/work-item.json`** — the paths, filenames, build commands and
   framework names it mentions are the strongest signal available.
3. **A repo-map absence is not a blocker** — fall back to grepping the
   registered repos for the symbols/files the item names.

Then propose **exactly one** repo, with a one-line evidence-based reason, and
name any registered repo you are deliberately NOT choosing when a human might
expect it. Quick mode carries a single task; a work item that genuinely needs
two repos is in the wrong mode — say so rather than picking one and hoping.
Switching modes means **aborting this run first**
(`${CLAUDE_PLUGIN_ROOT}/bin/harness abort --reason "<why>" --run <run>`), then
editing the item's `Mode:` hint and re-fetching: `fetch` refuses outright while
a live run exists for the item, so "just re-fetch as full" is not a remedy that
can terminate on its own.

> A `**Repo:**` line in the story is worth reading if it survived into
> `work-item.json` — but note the local-markdown provider normalizes
> `description` from the `## Description` section only, so a hint written in
> the header above that section never reaches the run. Treat it as one
> evidence source among the above, never as the answer.

## Present it, then record it

Present as a **confirm-a-default** (same two-kinds-of-question discipline
`intake.md` uses): state the repo you'll take and the evidence, and let a
non-answer resolve cleanly. Escalate to a **resolve-a-real-fork** — an explicit
pick, no default — only when the evidence genuinely splits. Never present the
seeded `T1` repo as if it were already a decision; it is fetch's placeholder.

Once the user confirms:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness confirm-repo --repo <registered repo path> \
  --basis "<one line of evidence>" --run <run>
```

`--repo` takes the exact registered repo **PATH** (a `repos.yaml` *value*),
never the short name. The verb re-points the seeded task, clears its
`provisional` flag, records the `scope` artifact, and writes the
`repo_confirmed` marker that releases the cursor. It is orchestrator-only — a
subagent running it is blocked, because it records that *a human was asked*.

Then advance:

```
${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to preflight --run <run>
```
