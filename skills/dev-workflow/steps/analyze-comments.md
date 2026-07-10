# Step: analyze-comments (reviewer shape; on-demand, repeatable group)

Triggered when PR review comments arrive (`reenter_when:
new_comments_present` — re-entry after apply-fixes is a declared edge).

1. Fetch the comments via the git provider:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness fetch-pr-comments --repo <repo> --run <run>`
   (`local` provider returns none — paste them yourself for that case).
2. Spawn `reviewer` with `harness-mode: analyze-comments` + the comments.
   It follows `steps/comment-analysis.md`.
3. Persist its analysis to `<run>/reports/comments-round-<n>.md`, numbering
   each comment (`[1]`, `[2]`, …), and record the declared artifact:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness artifact --name comment-analysis
   --value reports/comments-round-<n>.md --run <run>`.
4. Present ⟨select-comments⟩ — a `select` gate, not an approve/reject one.
   The candidate list is supplied ONCE, at present time, and sealed into
   state — decide replays that exact list, so the numbering the human saw
   can never be redefined between present and decide (RC3):
   ```
   ${CLAUDE_PLUGIN_ROOT}/bin/harness gate --id select-comments --present \
     --options <comment-id-1>,<comment-id-2>,... --run <run>
   ```
   Show the numbered list (same order), wait for the reply, then:
   ```
   ${CLAUDE_PLUGIN_ROOT}/bin/harness gate --id select-comments --decide --run <run>
   ```
   The human's reply (e.g. `1,3`) becomes a LIST decision — any parseable
   selection is forward-legal, so this never routes to `on_reject`; it just
   records which comments `apply-fixes` should address. To select nothing
   (none of the comments need fixing), reply literally `NONE` — that's the
   only input that parses to an empty selection; anything else either
   matches real options or refuses as unparseable.
