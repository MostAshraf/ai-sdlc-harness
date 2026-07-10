"""Structural Mermaid diagram validator (M8 WS-4, optional — design.md's
"Mermaid validation (A3)" entry under Adopted — Should).

Ported from the original ai-sdlc-harness's validate-mermaid skill/script —
structural rules only. No external mermaid-cli integration (that was the
original's opt-in R10; out of scope here, no CI-env-detection to match) and
no PreToolUse hook payload dispatch (this is a plain CLI verb over a file
path, invoked by the orchestrator before the plan gate — m8-plan-fidelity.md
WS-4 — not a content-interception hook).

R5 (node-ID closure) and R6 (edge-label quoting) are deliberately not
ported: Mermaid auto-declares any referenced node ID, so there is no real
"undefined" state to detect without a full grammar parser — the original
documented both rules but never implemented either. Same scope decision,
made explicit here rather than silently repeated. R2 (HTML-entity escaping)
is the reverse case: the original documented it too but ALSO never
implemented it — R2 here is new enforcement, not a restoration of working
original behavior, unlike R1/R3/R4/R7/R8/R9/R11 which the original's script
genuinely ran.
"""
from __future__ import annotations

import re
from pathlib import Path

FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
KNOWN_OPENERS = (
    "flowchart ", "graph ", "stateDiagram", "classDiagram", "sequenceDiagram",
    "erDiagram", "gitGraph", "journey", "pie ",
)
VALID_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#\d+);")
LABEL_RE = re.compile(r"[\[\(\{]([^\[\]\(\)\{\}]*)[\]\)\}]")
# Edge labels in pipe syntax (`-->|label text|`): the other label form
# _strip_labels must blank before node tokenizing (adversarial-review
# finding: only the bracket forms were stripped, so every WORD inside a
# pipe label counted as its own node — a 24-node flowchart with wordy
# edge labels read as 65 and falsely tripped the R9 cap at the plan gate,
# the exact false-positive class _strip_labels' own docstring says it
# fixed, fixed for one syntax only).
PIPE_LABEL_RE = re.compile(r"\|[^|]*\|")
NODE_ID_RE = re.compile(r"(?:^|\s|-->|---|==>|-\.->)([A-Za-z_]\w*)(?=[\[\(\{\s]|$)")
# Reserved flowchart keywords / direction tokens the node-ID regex would
# otherwise tokenize as pseudo-nodes: the opener (`flowchart TD` / `graph LR`),
# `subgraph`/`end` containers, `direction`, and style directives. Subtracted
# from the counted set so the R9 ceiling counts real nodes, not diagram syntax.
# (These are Mermaid reserved words, so none can be a bare node ID anyway.)
FLOW_KEYWORDS = frozenset({
    "flowchart", "graph", "subgraph", "end", "direction",
    "classDef", "class", "style", "linkStyle", "click",
    "TB", "TD", "BT", "RL", "LR",
})


def _strip_labels(line: str) -> str:
    """Blank out node/edge-label bodies (`[...]`, `(...)`, `{...}`) before
    node-ID tokenizing. Without this the flowchart counter treats each
    space-separated word INSIDE a label as its own node — an unhyphenated
    `A[user changes value]` counts `changes` as a spurious second node,
    inflating the R9 count until a prose-heavy but genuinely small diagram
    trips the 60-node cap (a real plan-run false positive). Iterates
    innermost-out so nested shapes (stadium `([...])`, hexagon `{{...}}`,
    subroutine `[[...]]`) fully collapse; each pass strips at least one
    delimiter pair, so it terminates. Pipe edge-labels (`-->|label|`)
    strip first — same rule, other label syntax."""
    line = PIPE_LABEL_RE.sub(" ", line)
    prev = None
    while prev != line:
        prev = line
        line = LABEL_RE.sub(" ", line)
    return line


class MermaidError(Exception):
    pass


def _extract_fences(text: str) -> list[tuple[int, str]]:
    fences = []
    for m in FENCE_RE.finditer(text):
        line_offset = text.count("\n", 0, m.start()) + 2
        fences.append((line_offset, m.group(1)))
    return fences


def _unescaped_special_chars(label: str) -> list[str]:
    scrubbed = VALID_ENTITY_RE.sub("", label)
    return [c for c in ("<", ">", "&", "|") if c in scrubbed]


def _count_nodes(body: str, first: str, is_sequence: bool) -> int:
    """The 60-node ceiling means 60 DIAGRAM ELEMENTS, not 60 regex tokens —
    a flowchart's node-ID regex applied uniformly to every diagram type
    massively overcounts a classDiagram (every attribute/method name reads
    as a "node") and undercounts a sequenceDiagram (arrow syntax like `->>`
    isn't in the flowchart arrow vocabulary, and dense message traffic
    between few participants never trips a node-ID count at all). Residual:
    for sequenceDiagram this counts distinct PARTICIPANTS, not messages — a
    diagram with few participants but hundreds of messages (tall, not wide)
    isn't caught; not solved here, same "not worth new machinery" bar as
    R5/R6."""
    if first.startswith("classDiagram"):
        # Two legitimate authoring styles: explicit `class Name { ... }`
        # blocks, or relationship-only lines naming types that are never
        # otherwise declared (`EntityA <|-- EntityB`) — count either.
        types = set(re.findall(r"^\s*class\s+(\w+)", body, re.MULTILINE))
        for a, b in re.findall(
                r"(\w+)\s*(?:<\|--|--\*|--o|\*--|o--|<\.\.|\.\.>|\.\.\|>|"
                r"<\|\.\.|-->|--|\.\.)\s*(\w+)", body):
            types.add(a)
            types.add(b)
        return len(types)
    if is_sequence:
        declared = set(re.findall(r"^\s*(?:participant|actor)\s+(\w+)",
                                  body, re.MULTILINE))
        arrow_sides = set(re.findall(
            r"(\w+)\s*-{1,2}[>x)]{1,3}\s*\+?(\w+)", body))
        for a, b in arrow_sides:
            declared.add(a)
            declared.add(b)
        return len(declared)
    nodes: set[str] = set()
    for ln in body.splitlines():
        for m in NODE_ID_RE.finditer(_strip_labels(ln)):
            nodes.add(m.group(1))
    return len(nodes - FLOW_KEYWORDS)


def _first_content_line(lines: list[str]) -> str:
    """The diagram-type opener line: the first line that's neither blank
    nor a `%%` comment (a leading `%%` header labeling the diagram — e.g.
    a plan naming which of its 4 mandated diagrams a fence is — is common
    and legitimate, not a malformed opener)."""
    for ln in lines:
        stripped = ln.strip()
        if stripped and not stripped.startswith("%%"):
            return stripped
    return ""


def _validate_fence(body: str, line_offset: int) -> list[str]:
    failures: list[str] = []
    lines = body.splitlines()

    first = _first_content_line(lines)
    if not any(first.startswith(o) for o in KNOWN_OPENERS):
        failures.append(f"{line_offset}:1: R1: unknown diagram opener: {first!r}")
    is_sequence = first.startswith("sequenceDiagram")

    for i, ln in enumerate(lines):
        stripped = ln.strip()
        # `<!--` anywhere is the HTML-comment signal; a bare trailing `-->`
        # is NOT (adversarial-review finding: `endswith("-->")` hard-failed
        # any legitimate line that merely ends with a mermaid arrow — e.g.
        # a `%% see below -->` comment or a wrapped edge line).
        if "<!--" in stripped:
            failures.append(f"{line_offset + i}:1: R8: HTML comment inside "
                            "mermaid fence — use %% instead")
        if stripped.startswith("//"):
            failures.append(f"{line_offset + i}:1: R8: `//` comment inside "
                            "mermaid fence — use %% instead")
        # R2 is a node/edge-LABEL rule (bracket-delimited shapes) — it does
        # not apply to sequenceDiagram, which has no bracket-label syntax
        # at all; parens/braces there are just prose punctuation, and its
        # own real parsing hazards are covered by R11 below instead.
        if not is_sequence:
            for label in LABEL_RE.findall(ln):
                bad = _unescaped_special_chars(label)
                if bad:
                    failures.append(f"{line_offset + i}:1: R2: unescaped "
                                    f"{'/'.join(bad)} in label — use the HTML "
                                    "entity form (&amp; &lt; &gt; &#124;)")

    for i, ln in enumerate(lines):
        m = re.match(r"\s*subgraph\s+\w+\s*\[(.*)\]\s*$", ln)
        if not m:
            continue
        inner = m.group(1)
        if not (inner.startswith('"') and inner.endswith('"')):
            failures.append(f"{line_offset + i}:1: R3: unquoted subgraph "
                            'title — use `subgraph X["title"]`')

    classdef = set()
    for ln in lines:
        m = re.match(r"\s*classDef\s+(\w+)", ln)
        if m:
            classdef.add(m.group(1))
    is_class_diagram = first.startswith("classDiagram")
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        for cls in re.findall(r":::(\w+)", ln):
            if cls not in classdef:
                failures.append(f"{line_offset + i}:1: R4: class `:::{cls}` "
                                f"referenced but no `classDef {cls}` in this diagram")
        # R12: in a classDiagram, `:::style` is ONLY valid on a `class
        # <Name>:::style` declaration statement (verified against
        # mermaid-cli 11.16: `A --> B:::style` and a bare `B:::style` both
        # raise `got 'STYLE_SEPARATOR'` at render time — the flowchart
        # idiom the plan's styling instruction inadvertently invited). The
        # gate must catch this before the human ever sees the broken render.
        if is_class_diagram and ":::" in stripped and not \
                stripped.startswith("class "):
            failures.append(
                f"{line_offset + i}:1: R12: `:::` style in a classDiagram is "
                "only valid on a `class <Name>:::<style>` statement — not on "
                "a relationship (`A --> B:::x`) or a bare `Name:::x` "
                "reference (Mermaid raises STYLE_SEPARATOR). Put the type on "
                "its own `class <Name>:::<style>` line and draw the "
                "relationship separately.")

    for i, ln in enumerate(lines):
        bare = ln.strip()
        m = re.search(r"\b\w+\(\[([^\]]*)\]\)", bare)
        if m:
            inner = m.group(1)
            if ("(" in inner or ")" in inner) and not (
                    inner.startswith('"') and inner.endswith('"')):
                failures.append(f"{line_offset + i}:1: R7: stadium shape "
                                'with inner parens must be quoted (`node(["..."])`)')

    node_count = _count_nodes(body, first, is_sequence)
    if node_count > 60:
        failures.append(f"{line_offset}:1: R9: diagram has {node_count} "
                        "nodes; cap is 60 — split into sub-diagrams")

    if is_sequence:
        entity_re = re.compile(r"&[a-zA-Z][a-zA-Z0-9]*;")
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if not stripped or stripped.startswith("%%"):
                continue
            m = entity_re.search(stripped)
            if m:
                failures.append(f"{line_offset + i}:1: R11: HTML entity "
                                f"`{m.group(0)}` in sequenceDiagram text — use "
                                "`{placeholder}` syntax, plain prose, or the "
                                "literal Unicode character instead")
            if ":" in stripped and ";" in stripped:
                _, _, content = stripped.partition(":")
                if ";" in content:
                    failures.append(f"{line_offset + i}:1: R11: literal `;` "
                                    "in sequenceDiagram text — Mermaid treats "
                                    "`;` as a statement separator; replace "
                                    "with `—` / `,` or split into two notes")

    return failures


def validate_file(path: Path) -> dict:
    """Validate every Mermaid fence in `path`. Returns
    {"fences": n, "verdict": "valid" | "invalid" | "no-fences", "failures": [...]}."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        raise MermaidError(f"validate-mermaid: read failed: {path}: {e}")

    fences = _extract_fences(content)
    if not fences:
        return {"fences": 0, "verdict": "no-fences", "failures": []}

    failures = []
    for line_offset, body in fences:
        failures.extend(_validate_fence(body, line_offset))

    return {"fences": len(fences),
           "verdict": "invalid" if failures else "valid",
           "failures": failures}
