# Step: any gate (⟨approve-plan⟩, ⟨approve-impl⟩, ⟨approve-security⟩, …)

A gate derives its decision from CAPTURED human input — you present and
request; deterministic code decides (design.md RC3).

1. `${CLAUDE_PLUGIN_ROOT}/bin/harness gate --id <gate> --present --run <run>`
2. Show the gate's artifact to the user **verbatim** (the manifest's
   `presents:` names it — plan.md, task summary, security report…), plus the
   options: plain gates → `APPROVED` / `rejected`; security gate →
   `[1] fix-now [2] waive [3] defer`.
   ⟨approve-plan⟩ / ⟨approve-plan-lean⟩ only: ALSO show
   `<run>/reports/plan-review.md` verbatim — the independent review
   travels with the plan (and if it reached this gate with the review
   bound exhausted, say so explicitly; the human is deciding on a plan
   the panel still rejects — for ⟨approve-plan-lean⟩ that is the ONLY way
   it fires at all: an approved panel self-skips it).
3. Wait for the user's reply — it must arrive as a PLAIN TYPED CHAT
   MESSAGE. The capture hook anchors decisions to UserPromptSubmit events
   only; an AskUserQuestion answer arrives as a structured tool result,
   is never captured, and the decide call will refuse with "no human
   input after presentation" (dogfood-run finding — do not burn attempts
   rediscovering this). Do not interpret the reply yourself.
   **Steps 1 and 4 are separate tool calls in separate turns.** Chaining
   `--present` and `--decide` into one Bash invocation ALWAYS refuses: a
   prompt cannot arrive between two commands of the same call, and decide
   qualifies only replies captured strictly after the presentation.
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
   - refused **"typed in a DIFFERENT session"** → a reply captured from
     another session in this workspace is not this gate's evidence and is
     ignored, never parsed. Do NOT re-present: ask the user to reply again
     **in the session driving this run**, then `--decide` alone. Only if
     they CONFIRM that session is gone (terminal closed, resumed under a
     new id) does `--re-present` apply — it re-stamps the gate to the
     session running it, and ages out anything typed in the old one. If
     THIS process reports no session id, `--re-present` refuses rather
     than clearing the stamp (clearing it would leave the gate decidable
     by a prompt typed in any session). Do NOT invent a value for
     `CLAUDE_CODE_SESSION_ID` to get past that refusal: unless the
     platform's capture hook tags nothing, a value the hook doesn't also
     carry stamps the gate with an identity no reply can match, and
     nothing in the CLI can undo it.
   - refused (no qualifying reply / qualified FORWARD reply)
     → the reply routes to **ad-hoc handling**: triage it
     (`request-triage`), resolve with the user, then `--present
     --re-present` and repeat.
     **Re-presenting re-stamps the window, which ages out any reply
     already captured** — so it is right only when the reply genuinely
     cannot decide. When the refusal says a captured reply merely
     PREDATES the presentation, do NOT present again: ask for one more
     reply and `--decide` alone. `--present` refuses without
     `--re-present` while un-decided replies are waiting, precisely so a
     retry cannot silently make the human type their answer twice.
6. Security gate only: a `defer` decision → the decide result carries a
   `follow_up` field and logs a flagged `deferral-pending` event that
   stays on the dashboard until you pair it — act on it now: create
   the follow-up work item
   `${CLAUDE_PLUGIN_ROOT}/bin/harness provider --op work_item.create --title
   "<summary>" --description "<finding + repo + severity>"` (github/
   github-projects/gitlab/local-markdown; a provider that declares it
   unsupported → comment on the parent item instead), then clear the flag:
   `${CLAUDE_PLUGIN_ROOT}/bin/harness log-event --json
   '{"kind": "deferral-recorded", "item": "<new-id>"}'`.
7. Publish the mirror after the crossing — **once per preflighted repo**
   (the `branches` artifact in `show` names them), never into the
   workspace: `${CLAUDE_PLUGIN_ROOT}/bin/harness publish-mirror --repo <preflighted-repo-path> --run <run>`.
   **⟨approve-plan⟩ and ⟨approve-plan-lean⟩ are BEFORE preflight** — no
   branch exists yet, so **skip the mirror entirely at these gates**
   (there's nothing to snapshot into a code branch). See SKILL.md's
   Publish rule. Best-effort/non-blocking.
8. ⟨approve-impl⟩ only, BEFORE presenting: `${CLAUDE_PLUGIN_ROOT}/bin/harness
   write-back --milestone in_review --run <run>` (no-ops cleanly if
   `write_back.on_in_review` is off, or — for an MCP-transport provider —
   returning `mcp_guidance` instead of raising; invoke the named tool
   yourself if you want live status sync).
