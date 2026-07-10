"""M8 WS-4 (optional, m8-plan-fidelity.md): structural Mermaid validator,
ported from the original ai-sdlc-harness's validate-mermaid script. R5/R6
are deliberately not ported (see harness/mermaid.py's module docstring)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import mermaid

ROOT = Path(__file__).resolve().parent.parent


def _fence(body: str) -> str:
    return f"```mermaid\n{body}\n```\n"


class MermaidValidator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _check(self, content: str) -> dict:
        path = self.tmp / "diagram.md"
        path.write_text(content)
        return mermaid.validate_file(path)

    def test_file_not_found_raises(self):
        with self.assertRaises(mermaid.MermaidError):
            mermaid.validate_file(self.tmp / "missing.md")

    def test_no_fences_is_a_no_op(self):
        result = self._check("just prose, no diagrams here\n")
        self.assertEqual(result["verdict"], "no-fences")
        self.assertEqual(result["failures"], [])

    def test_clean_flowchart_is_valid(self):
        result = self._check(_fence("flowchart TD\n    A[Start] --> B[End]"))
        self.assertEqual(result["verdict"], "valid")
        self.assertEqual(result["failures"], [])

    def test_r1_unknown_opener_rejected(self):
        result = self._check(_fence("foobar TD\n    A --> B"))
        self.assertEqual(result["verdict"], "invalid")
        self.assertTrue(any("R1" in f for f in result["failures"]))

    def test_r2_unescaped_angle_bracket_rejected(self):
        result = self._check(_fence('flowchart TD\n    A[a < b] --> B[ok]'))
        self.assertTrue(any("R2" in f for f in result["failures"]))

    def test_r2_html_entity_form_accepted(self):
        result = self._check(_fence('flowchart TD\n    A[a &lt; b] --> B[ok]'))
        self.assertEqual(result["verdict"], "valid")

    def test_r3_unquoted_subgraph_title_rejected(self):
        result = self._check(_fence(
            "flowchart TD\n    subgraph X [Title]\n    A --> B\n    end"))
        self.assertTrue(any("R3" in f for f in result["failures"]))

    def test_r3_quoted_subgraph_title_accepted(self):
        result = self._check(_fence(
            'flowchart TD\n    subgraph X["Title"]\n    A --> B\n    end'))
        self.assertEqual(result["verdict"], "valid")

    def test_r3_bare_subgraph_with_no_title_accepted(self):
        result = self._check(_fence(
            "flowchart TD\n    subgraph X\n    A --> B\n    end"))
        self.assertEqual(result["verdict"], "valid")

    def test_r4_undeclared_classdef_reference_rejected(self):
        result = self._check(_fence("flowchart TD\n    A:::missing --> B"))
        self.assertTrue(any("R4" in f for f in result["failures"]))

    def test_r4_declared_classdef_reference_accepted(self):
        result = self._check(_fence(
            "flowchart TD\n    classDef ok fill:#fff\n    A:::ok --> B"))
        self.assertEqual(result["verdict"], "valid")

    def test_r12_classdiagram_style_on_relationship_endpoint_rejected(self):
        # field report + verified against mermaid-cli 11.16: in a
        # classDiagram, `:::style` on a relationship endpoint raises
        # `got 'STYLE_SEPARATOR'` and won't render — the gate must catch it.
        result = self._check(_fence(
            "classDiagram\n    classDef modified fill:#fff7d8\n"
            "    AdminCtl --> WorkflowController:::modified"))
        self.assertTrue(any("R12" in f for f in result["failures"]))

    def test_r12_classdiagram_bare_style_reference_rejected(self):
        result = self._check(_fence(
            "classDiagram\n    classDef modified fill:#fff7d8\n"
            "    WorkflowController:::modified"))
        self.assertTrue(any("R12" in f for f in result["failures"]))

    def test_r12_classdiagram_style_on_class_statement_accepted(self):
        for body in (
            "classDiagram\n    classDef modified fill:#fff7d8\n"
            "    AdminCtl --> WorkflowController\n    class WorkflowController:::modified",
            "classDiagram\n    classDef modified fill:#fff7d8\n"
            "    class WorkflowController:::modified {\n        +run()\n    }",
        ):
            result = self._check(_fence(body))
            self.assertEqual(result["verdict"], "valid", body)

    def test_r12_is_classdiagram_only_flowchart_style_still_valid(self):
        # R12 must not touch flowcharts, where `A:::x` is legal
        result = self._check(_fence(
            "flowchart TD\n    classDef modified fill:#fff7d8\n"
            "    A[start]:::modified --> B[end]"))
        self.assertEqual(result["verdict"], "valid")

    def test_r7_unquoted_stadium_with_inner_parens_rejected(self):
        result = self._check(_fence('flowchart TD\n    A([f(x)]) --> B'))
        self.assertTrue(any("R7" in f for f in result["failures"]))

    def test_r7_quoted_stadium_with_inner_parens_accepted(self):
        result = self._check(_fence('flowchart TD\n    A(["f(x)"]) --> B'))
        self.assertEqual(result["verdict"], "valid")

    def test_r8_html_comment_rejected(self):
        result = self._check(_fence(
            "flowchart TD\n    <!-- bad -->\n    A --> B"))
        self.assertTrue(any("R8" in f for f in result["failures"]))

    def test_r8_percent_comment_accepted(self):
        result = self._check(_fence(
            "flowchart TD\n    %% fine\n    A --> B"))
        self.assertEqual(result["verdict"], "valid")

    def test_r8_line_merely_ending_with_arrow_accepted(self):
        # adversarial-review finding: `endswith("-->")` hard-failed any
        # legitimate line ending with a mermaid arrow — e.g. a %% comment
        # mentioning one — as an "HTML comment".
        result = self._check(_fence(
            "flowchart TD\n    %% see the branch below -->\n    A --> B"))
        self.assertEqual(result["verdict"], "valid")

    def test_r9_pipe_edge_labels_do_not_count_as_nodes(self):
        # adversarial-review finding: only bracket labels were stripped
        # before node tokenizing — every WORD in a `-->|pipe label|`
        # counted as a node, falsely tripping the 60-node cap on a small
        # prose-heavy diagram (a real plan-gate blocker class).
        body = "flowchart TD\n" + "\n".join(
            f"    N{i}[step] -->|user submits the completed form data| N{i+1}[next]"
            for i in range(20))
        result = self._check(_fence(body))
        self.assertEqual(result["verdict"], "valid",
                         f"pipe-label words counted as nodes: {result['failures']}")

    def test_r9_over_60_nodes_rejected(self):
        body = "flowchart TD\n" + "\n".join(
            f"    N{i}[n] --> N{i + 1}[n]" for i in range(65))
        result = self._check(_fence(body))
        self.assertTrue(any("R9" in f for f in result["failures"]))

    def test_r9_under_60_nodes_accepted(self):
        body = "flowchart TD\n" + "\n".join(
            f"    N{i}[n] --> N{i + 1}[n]" for i in range(10))
        result = self._check(_fence(body))
        self.assertEqual(result["verdict"], "valid")

    def test_r9_flowchart_multiword_labels_count_by_node_id(self):
        # Regression: the flowchart node counter tokenized each word INSIDE a
        # label, so an unhyphenated multi-word label like `A[user changes
        # value]` counted `changes` as a spurious node and inflated the R9
        # count — a real plan-run tripped the 60-node cap on a small diagram.
        body = ("flowchart TD\n"
                "    A[user changes the value] --> B[system stores the result]\n"
                "    B --> C[all done here]")
        self.assertEqual(mermaid._count_nodes(body, "flowchart TD", False), 3)

    def test_r9_multiword_labels_do_not_trip_the_cap(self):
        # 40 real nodes, each a 4-word label: with the old per-word counting
        # the interior words alone would push well past 60 and falsely reject.
        body = "flowchart TD\n" + "\n".join(
            f"    N{i}[handle the input step] --> N{i + 1}[emit the output step]"
            for i in range(40))
        result = self._check(_fence(body))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_r9_still_rejects_genuinely_over_60_real_nodes(self):
        # Guard: the label-stripping fix must not mask a real over-cap diagram.
        body = "flowchart TD\n" + "\n".join(
            f"    N{i}[the node] --> N{i + 1}[the node]" for i in range(65))
        result = self._check(_fence(body))
        self.assertTrue(any("R9" in f for f in result["failures"]))

    def test_r9_subgraph_and_direction_keywords_not_counted(self):
        # `subgraph`/`end`/`direction`/the opener are diagram syntax, not
        # nodes: only S (the subgraph id), A and B should count.
        body = ('flowchart TD\n'
                '    subgraph S["a group"]\n'
                '    direction LR\n'
                '    A[first thing] --> B[second thing]\n'
                '    end')
        self.assertEqual(mermaid._count_nodes(body, "flowchart TD", False), 3)

    def test_r9_classdiagram_counts_classes_not_every_member_token(self):
        # Regression: a flowchart-style token count would treat every
        # attribute/method name as a "node", wrongly flagging a normal
        # class diagram with real (non-repeated) member names.
        lines = ["classDiagram"]
        for i in range(26):
            lines += [f"    class Type{i} {{", f"        +int fieldA{i}",
                     f"        +bool fieldB{i}", f"        +computeResult{i}() int",
                     "    }"]
        result = self._check(_fence("\n".join(lines)))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_r9_classdiagram_over_60_classes_rejected(self):
        lines = ["classDiagram"] + [f"    class Type{i}" for i in range(65)]
        result = self._check(_fence("\n".join(lines)))
        self.assertTrue(any("R9" in f for f in result["failures"]))

    def test_r9_classdiagram_relationship_only_types_also_counted(self):
        # Regression: a legitimate classDiagram authoring style names types
        # only via relationship lines (`A <|-- B`), never a `class X` block
        # — counting only explicit declarations would silently exempt it.
        lines = ["classDiagram"] + [
            f"    Entity{i} <|-- Entity{i + 1}" for i in range(70)]
        result = self._check(_fence("\n".join(lines)))
        self.assertTrue(any("R9" in f for f in result["failures"]))

    def test_r9_classdiagram_mixed_style_under_ceiling_accepted(self):
        lines = ["classDiagram"]
        for i in range(20):
            lines += [f"    class Type{i} {{", f"        +int fieldA{i}", "    }"]
        for i in range(19):
            lines.append(f"    Type{i} --> Type{i + 1}")
        result = self._check(_fence("\n".join(lines)))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_r9_sequencediagram_counts_participants_not_message_volume(self):
        # 15 participants, 210 messages between them: dense but not
        # actually too many distinct actors to read.
        participants = [f"Actor{i}" for i in range(15)]
        lines = ["sequenceDiagram", "autonumber"]
        for i in range(210):
            a, b = participants[i % 15], participants[(i + 1) % 15]
            lines.append(f"    {a}->>{b}: ping")
        result = self._check(_fence("\n".join(lines)))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_r9_sequencediagram_over_60_participants_rejected(self):
        lines = ["sequenceDiagram"] + [
            f"    participant Actor{i}" for i in range(65)]
        result = self._check(_fence("\n".join(lines)))
        self.assertTrue(any("R9" in f for f in result["failures"]))

    def test_r2_does_not_misfire_on_sequencediagram_prose(self):
        # Regression: sequenceDiagram has no bracket-label syntax; ordinary
        # prose describing a condition isn't a Mermaid label.
        result = self._check(_fence(
            "sequenceDiagram\n    autonumber\n"
            "    Client->>API: POST /v2/orders (payload > 0 bytes)\n"
            "    Note over API,DB: retries if attempts less than 3"))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_r1_leading_percent_comment_before_opener_accepted(self):
        # Regression: a leading %% header (e.g. naming which of the plan's
        # 4 diagrams this fence is) is legitimate, not a malformed opener.
        result = self._check(_fence(
            "%% Task dependency graph\nflowchart LR\n    A --> B"))
        self.assertEqual(result["verdict"], "valid", result["failures"])

    def test_multiple_distinct_rule_violations_in_one_fence_all_collected(self):
        result = self._check(_fence(
            'flowchart TD\n'
            '    subgraph X [Unquoted]\n'
            '    A[a < b] --> B:::missing\n'
            '    end'))
        rules_hit = {f.split(": ", 2)[1] for f in result["failures"]}
        self.assertEqual(rules_hit, {"R2", "R3", "R4"})

    def test_r11_html_entity_in_sequence_diagram_rejected(self):
        result = self._check(_fence("sequenceDiagram\n    A->>B: x &lt; y"))
        self.assertTrue(any("R11" in f for f in result["failures"]))

    def test_r11_semicolon_in_sequence_diagram_text_rejected(self):
        result = self._check(_fence(
            "sequenceDiagram\n    Note over A: hi; there"))
        self.assertTrue(any("R11" in f for f in result["failures"]))

    def test_r11_rules_do_not_apply_outside_sequence_diagrams(self):
        result = self._check(_fence('flowchart TD\n    A["x &lt; y; z"] --> B'))
        self.assertEqual(result["verdict"], "valid")

    def test_multiple_fences_all_checked(self):
        content = (_fence("flowchart TD\n    A --> B")
                  + "\nprose between\n\n"
                  + _fence("foobar TD\n    A --> B"))
        result = self._check(content)
        self.assertEqual(result["fences"], 2)
        self.assertEqual(result["verdict"], "invalid")
        self.assertEqual(len(result["failures"]), 1)


class MermaidCliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _cli(self, *args) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "harness", "--workspace", str(self.tmp), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout)

    def test_valid_file_exits_zero(self):
        path = self.tmp / "plan.md"
        path.write_text(_fence("flowchart TD\n    A[Start] --> B[End]"))
        code, out = self._cli("validate-mermaid", "--file", str(path))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["verdict"], "valid")

    def test_invalid_file_exits_one_with_failures(self):
        path = self.tmp / "plan.md"
        path.write_text(_fence("foobar TD\n    A --> B"))
        code, out = self._cli("validate-mermaid", "--file", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(out["verdict"], "invalid")
        self.assertTrue(out["failures"])

    def test_missing_file_exits_one_with_clear_error(self):
        code, out = self._cli("validate-mermaid", "--file", str(self.tmp / "nope.md"))
        self.assertEqual(code, 1)
        self.assertIn("read failed", out["error"])


if __name__ == "__main__":
    unittest.main()
