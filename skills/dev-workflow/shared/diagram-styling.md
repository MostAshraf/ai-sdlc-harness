# Shared baseline: Mermaid diagram styling (cited by the planner)

Structural rules keep every plan diagram valid, renderable Mermaid.
**Mechanically enforced** by `${CLAUDE_PLUGIN_ROOT}/bin/harness
validate-mermaid` (M8 WS-4), run by the orchestrator against `plan.md`
before the plan gate — a rule violation sends the planner back, it's not
just a convention to follow on trust.

- **HTML-entity escaping**: a node label containing `&`, `<`, `>`, or `|`
  uses the entity form (`&amp;`, `&lt;`, `&gt;`, `&#124;`). Not `"` — that's
  how a label is quoted in the first place; a mis-nested inner quote isn't
  mechanically caught (would need a real parser, not a structural check).
- **Quoted subgraph titles**: `subgraph X["..."]`, never the unquoted
  `subgraph X [...]` form.
- **classDef ↔ class-reference closure**: every `:::<class>` reference has a
  matching `classDef <class>` line in the same diagram. The reverse isn't
  required — an unreferenced `classDef` (e.g. `modified` on an all-new-types
  diagram) is harmless boilerplate, not an error.
- **60-node ceiling**: split into sub-diagrams past 60 nodes. The count is by
  distinct **node ID**, not by words — a descriptive multi-word label like
  `A[user changes the value]` counts as the one node `A`. Write labels as
  natural prose; you do **not** need to hyphenate multi-word labels to stay
  under the cap.
- Comments use `%%`, never `<!-- -->` inside a fenced diagram.
- **Not mechanically checked** (Mermaid auto-declares any referenced node
  ID, so there's no real "undefined" state to detect without a full grammar
  parser — same scope decision the original ai-sdlc-harness's validator
  made, though it never said so): node-ID closure, edge-label quoting.

## Class-diagram convention (v3.0-specific, not ported)

A plan's diagrams show the **target codebase's** own types/flow/calls — a
different subject from the harness's own phase/workflow diagrams (which
color hook/skill/agent/orch nodes; that palette doesn't fit here). Plan
diagrams instead mark scope directly on the class diagram:

```
classDef new fill:#ecffec,stroke:#3a7,stroke-width:1px,color:#063
classDef modified fill:#fff7d8,stroke:#b80,stroke-width:1px,color:#530
```

Apply `:::new` / `:::modified` to each type so a reviewer sees scope at a
glance — **but in a `classDiagram` the style attaches ONLY on a dedicated
`class <Name>:::<style>` statement (or the class-body declaration), never
the flowchart idiom.** classDiagram grammar rejects `:::` on a relationship
endpoint or a bare reference (Mermaid raises `got 'STYLE_SEPARATOR'` and the
diagram won't render); `validate-mermaid`'s R12 catches it at the plan gate.

```
%% RIGHT — scope styled on its own class statement:
classDiagram
  classDef modified fill:#fff7d8,stroke:#b80
  AdminController --> WorkflowController : dispatches
  class WorkflowController:::modified {
    +run()
  }
  class AdminController:::new

%% WRONG — :::modified on a relationship endpoint or bare ref (won't parse):
%%   AdminController --> WorkflowController:::modified
%%   WorkflowController:::modified
```

Any `:::modified` type that is also a PATCH/partial-update DTO must satisfy
the wrapper-type rule (`shared/engineering.md` — `patch-dto-wrapper`).
