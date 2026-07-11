# /story-workflow groom

Technical enrichment pass. Analyzes the relevant repos to identify affected
components, migration concerns, testing strategy, and risks, and produces
per-repo technical notes. This is where Claude Code's native codebase awareness
is the main tool.

## Steps

1. **Fetch** the work item per `shared/provider-io.md`. If it has no clear ACs
   yet, offer to run `improve` first — grooming is more effective on a refined
   story — but proceed if the user wants to.
2. **Identify relevant repos.** Read the repo landscape per `shared/context.md`
   (`repos.yaml` + each `repo-map/<name>/`). From the story, propose which repos
   are affected, each with a one-line reason, and **wait for the user to confirm**
   before scanning anything. Don't scan repos they didn't approve.
3. **Refresh remote knowledge, then check position (never mutate the tree).**
   For each confirmed repo, using its path from `repos.yaml`:
   - `git -C "<path>" fetch origin` — allowed, and updates remote refs without
     touching the working tree. If it fails (no network/remote), report and skip
     that repo — don't scan a repo you couldn't refresh.
   - `git -C "<path>" rev-list HEAD..origin/<default-branch> --count` — a read;
     how far behind the checkout is.
   - **The harness blocks raw `git pull` / `merge` inside any workspace that has
     completed `/init-workspace`** (owned-entry-point rule; this skill only ever
     runs inside one), so this command does **not** update the working
     tree. If a repo is behind, say so and offer two choices: **(a)** analyze the
     current checkout as-is — and stamp the staleness into the notes so the
     reader knows the analysis reflects commit `<short-sha>`, N commits behind
     `origin/<default>`; or **(b)** stop for that repo so the user can update it
     themselves (outside the harness) and re-run `groom`. Never switch branches
     or discard changes to "help".
4. **Analyze each confirmed, fetched repo** against the story. Use `Grep` for
   content, `Glob` for file patterns, `Read` for inspection — not `Bash` for
   searches. Look for: affected files/classes/methods; database & migration
   impact; API surface (controllers, routes, DTOs); configuration & DI; existing
   test coverage for the touched areas; and cross-service dependencies. Align
   with the team's real patterns from the repo-map (repository pattern, MediatR,
   etc.). **Be concrete** — name the file and method, e.g. "`AuthService.cs`
   `ValidateToken` (~line 45) is what this story changes; tests live in
   `AuthServiceTests.cs`", not "the auth module might be affected."
5. **Produce technical notes** in `templates/technical-notes.md` — a section per
   repo, plus Cross-Repo Considerations when more than one repo is involved.
6. **Present** the notes and ask whether to post them as a comment. Iterate on
   the user's input (they know planned refactors and constraints the code
   doesn't show), then post per `shared/provider-io.md`. For `local-markdown`,
   notes are a **comment** by default; do not overwrite the story — if the user
   wants them persisted separately, offer a sibling `<id>-technical-notes.md`.

## Notes

- Comments only — never edit the item's Description or AC fields.
- Frame findings as "here's what I see in the code," not "here's how to build
  it." The goal is a head start on scope, not a prescribed implementation.
- If you can't determine the impact with confidence, say so — "I couldn't tell
  how X is handled; discuss at grooming" is a valid, useful finding.
- Note unrelated concerns (a bug, tech debt) in one line, but don't derail the
  grooming with them.
