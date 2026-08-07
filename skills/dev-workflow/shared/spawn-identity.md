# Spawn identity — which agent to spawn for each harness shape

The dev-workflow steps say "Spawn `reviewer`" — the **shape word**. Your
platform's agent tool needs the **agent identity**, which is different:

| Shape (in step text) | Agent `name` (frontmatter) | Example `subagent_type` values |
|---|---|---|
| `planner` | `ai-sdlc-planner` | `ai-sdlc-planner` (Qwen, bare) or `ai-sdlc-harness:ai-sdlc-planner` (Claude, prefixed) |
| `developer` | `ai-sdlc-developer` | `ai-sdlc-developer` or `ai-sdlc-harness:ai-sdlc-developer` |
| `reviewer` | `ai-sdlc-reviewer` | `ai-sdlc-reviewer` or `ai-sdlc-harness:ai-sdlc-reviewer` |

## Rules

1. **Pass whichever `subagent_type` your platform offers for that agent.**
   Match on the frontmatter `name` (`ai-sdlc-<shape>`). Your platform's
   subagent picker lists the available types — if the bare name doesn't
   work, try the prefixed form; one of them will.

2. **Never substitute a generic agent** (`general-purpose`, `Explore`,
   `Task`, or any non-harness agent) for a harness shape. A generic agent
   runs the step **ungoverned**: no spawn gating, no write confinement
   (a "reviewer" that can write), no verdict capture. The spawn guard
   now blocks these spawns outright (WI-2).

3. **Never omit `subagent_type`** — under Qwen Code, omission silently
   runs the default builtin (`general-purpose`), which is the same as
   substituting a generic agent.

4. **A wrong name is loud, not silent.** Both platforms emit a visible
   error listing available agents when `subagent_type` doesn't match
   anything — you get a list of correct names to choose from. Precision
   is safe; guessing at a generic fallback is not.
