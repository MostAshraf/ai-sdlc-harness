# Instruction: generate the repo map (planner shape, mode `repo-map`)

Produce the tiered map for ONE repo under
`.claude/context/repo-map/<repo-name>/`. This is what intake's repo
targeting, plan's grounding, and plan-review's conventions check all read —
write it for THOSE consumers, not as free-form notes. Derive everything
from reading the actual code; never invent a pattern you didn't see, and
cite a real file for every claim (a fabricated citation poisons every
downstream step that trusts the map).

Content contract — three tiers, each loadable standalone:

1. **`index.md`** (the targeting tier — intake reads ONLY this): the
   repo's purpose in 2–3 sentences; tech stack (language, framework, build
   tool, test framework — with versions where the manifest states them);
   a module/directory inventory with a one-line purpose per entry; entry
   points (main, HTTP routes root, jobs/consumers); what this repo
   consumes from and produces for the workspace's OTHER registered repos
   (APIs, events, shared schemas — name the repo, cite the file), so a
   work item can be mapped to target repos from the indexes alone.
2. **`areas/<area>.md`** (the detail tier — plan loads only the areas a
   story touches): per functional area — key types and their
   relationships, main flows, notable abstractions, where its tests live,
   and gotchas visible in the code (feature flags, migration state,
   deprecated-but-present paths).
3. **`conventions.md`** (the review tier — plan-review checks plans
   against this): the codebase's OBSERVED conventions, each with one
   cited example file — naming (files, types, tests), layering/module
   boundaries (what imports what), error handling, logging, dependency
   injection/config access, test structure (arrange style, fixture
   patterns, naming of test functions), commit-adjacent norms visible in
   the tree (lint/format configs). Convention means "the dominant pattern
   actually in the code" — where the code is genuinely split, say so
   rather than picking a winner.

Boundaries: never write `.meta.json` or run `repo-map-stamp` (the
orchestrator stamps after you return); write ONLY under
`.claude/context/repo-map/<repo-name>/` for this repo; the map speeds
targeting — it never replaces downstream steps reading the real code, so
prefer short-and-cited over exhaustive-and-stale.
