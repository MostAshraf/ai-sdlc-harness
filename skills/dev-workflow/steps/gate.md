# Step: any gate (⟨approve-plan⟩, ⟨approve-impl⟩, ⟨approve-security⟩, …)

A gate derives its decision from CAPTURED human input — you present and
request; deterministic code decides (design.md RC3).

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness gate --id <gate> --present --run <run>`
2. Show the gate's artifact to the user **verbatim** (the manifest's
   `presents:` names it — plan.md, task summary, security report…), plus the
   options: plain gates → `APPROVED` / `rejected`; security gate →
   `[1] fix-now [2] waive [3] defer`.
3. Wait for the user's reply — it must arrive as a PLAIN TYPED CHAT
   MESSAGE. The capture hook anchors decisions to UserPromptSubmit events
   only; an AskUserQuestion answer arrives as a structured tool result,
   is never captured, and the decide call will refuse with "no human
   input after presentation" (dogfood-run finding — do not burn attempts
   rediscovering this). Do not interpret the reply yourself.
4. `${CLAUDE_PLUGIN_ROOT}/bin/harness gate --id <gate> --decide --run <run>`
   — never pass `--options` here: what a numbered reply means is DECLARED
   data (the manifest's `dispositions`, e.g. the security gate's
   `fix-now,waive,defer`), read by the CLI itself; a caller-supplied list
   at decide time is refused (RC3).
5. Outcomes:
   - decision recorded → `${CLAUDE_PLUGIN_ROOT}/bin/harness cursor --to <next>` (forward or the
     declared `on_reject` target — `show` + the manifest tell you which).
   - a REJECTION-side reply may carry notes after its option word
     (`REJECTED — split T2 into two tasks` decides as rejected; the notes
     ride into the on_reject step). FORWARD words stay bare: a qualified
     approval like "APPROVED but…" (or "waive if…") never decides.
   - refused (no qualifying reply / qualified FORWARD reply)
     → the reply routes to **ad-hoc handling**: triage it
     (`request-triage`), resolve with the user, then `--present` again
     (re-presenting re-stamps the window) and repeat.
6. Security gate only: a `defer` decision → the decide result carries a
   `follow_up` field and logs a flagged `deferral-pending` event that
   stays on the dashboard until you pair it — act on it now: create
   the follow-up work item
   `${CLAUDE_PLUGIN_ROOT}/bin/harness provider --op work_item.create --title
   "<summary>" --description "<finding + repo + severity>"` (github/gitlab/
   local-markdown; a provider that declares it unsupported → comment on the
   parent item instead), then clear the pending flag:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness log-event --json
   '{"kind": "deferral-recorded", "item": "<new-id>"}'`.
7. Publish the mirror after the crossing — **once per preflighted repo**
   (the `branches` artifact in `show` names them), never into the
   workspace: `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --run <run>`.
   **⟨approve-plan⟩ is BEFORE preflight** — no branch exists yet, so **skip
   the mirror entirely at this gate** (there's nothing to snapshot into a
   code branch). See SKILL.md's Publish rule. Best-effort/non-blocking.
8. ⟨approve-impl⟩ only, BEFORE presenting: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   write-back --milestone in_review --run <run>` (no-ops cleanly if
   `write_back.on_in_review` is off, or — for an MCP-transport provider —
   returning `mcp_guidance` instead of raising; invoke the named tool
   yourself if you want live status sync).
