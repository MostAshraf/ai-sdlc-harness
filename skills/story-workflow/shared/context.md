# shared/context.md — domain + convention context

`improve` and `groom` are stronger when grounded in the team's actual repos and
patterns. v3.0 keeps that context in two places (there are no `repos-metadata`
or `conventions` markdown files — those were v2.x, and are gone):

- **`.claude/context/repos.yaml`** — the registered repos and their local
  paths. This is the map of what exists and where it lives on disk.
- **`.claude/context/repo-map/<repo-name>/`** — the auto-generated, tiered
  codebase map (purpose by directory, key abstractions, notable patterns),
  stamped with the SHA it was generated at. This is where "the team uses the
  repository pattern / MediatR / …" comes from; read it rather than re-deriving
  the codebase from scratch.

Read both with the `Read` tool. Treat them as **context, not gospel**: the
repo-map may be stale (it records its own generation SHA) and the user knows
things the code doesn't. If a repo-map is absent for a repo, say so — `groom`
can still scan the repository directly (see `commands/groom.md`), and `improve`
proceeds on the work item alone.

Never write anything under `.claude/context/` from this skill — that tree is
owned by `/init-workspace`, `/add-repo`, and `/repo-map-refresh`.
