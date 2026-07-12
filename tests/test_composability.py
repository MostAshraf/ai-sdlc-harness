"""Composability guarantees (verification round 2026-07-08): new modes are
data (an ordered step list in pipeline/manifest.yaml), new steps are data +
one step file, and the validator + cursor engine handle both with zero
Python changes. These tests run the REAL validator and engine against a
modified copy of the shipped declared data — a failure here means a
step-name or mode-name hardcode crept back into shared machinery.
"""
from __future__ import annotations

import contextlib
import copy
import inspect
import io
import re
import unittest
from pathlib import Path

import yaml

from harness import schema, transitions, workflow
from harness.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent

# A plausible third mode composed purely of existing steps (a "solo" flow:
# planning rigor, no impl/security gates). Never added to the shipped
# manifest — these tests prove it COULD be, with data changes only.
SOLO = ["fetch", "intake", "plan", "approve-plan", "preflight", "develop",
        "pre-pr", "approve-pre-pr", "create-pr", "reconcile", "metrics"]


def shipped() -> tuple[dict, dict, dict]:
    manifest = yaml.safe_load((ROOT / "pipeline" / "manifest.yaml").read_text(encoding="utf-8"))
    surfaces = yaml.safe_load((ROOT / "pipeline" / "surfaces.yaml").read_text(encoding="utf-8"))
    config = schema.merge_defaults(ROOT / "config" / "defaults", schema.Issues())
    return manifest, surfaces, config


def validate(manifest: dict, surfaces: dict, config: dict) -> schema.Issues:
    issues = schema.Issues()
    schema.validate_manifest(manifest, surfaces, config, issues)
    return issues


class ModeComposition(unittest.TestCase):
    def setUp(self):
        self.manifest, self.surfaces, self.config = shipped()

    def test_new_mode_from_existing_steps_validates(self):
        self.manifest["modes"]["solo"] = list(SOLO)
        issues = validate(self.manifest, self.surfaces, self.config)
        self.assertTrue(issues.ok, issues.errors)

    def test_illegal_reorder_fails_loud(self):
        """The reordering guarantee's other half: a sequence that consumes
        before producing must be refused at validation time, not at runtime."""
        seq = [s for s in SOLO if s != "develop"]
        seq.insert(seq.index("preflight"), "develop")  # branches not yet made
        self.manifest["modes"]["solo"] = seq
        issues = validate(self.manifest, self.surfaces, self.config)
        self.assertFalse(issues.ok)
        self.assertTrue(any("precondition 'branches'" in e for e in issues.errors),
                        issues.errors)

    def test_new_step_validates_and_the_engine_walks_it(self):
        """A brand-new owner step no Python has ever heard of: declared in
        the manifest, inserted into a new mode, validated, walked by the
        real cursor engine (its artifact recorded via the generic
        machinery), and NOT skippable."""
        self.manifest["steps"]["docs-sync"] = {
            "owner": "orchestrator",
            "preconditions": ["task-commits"],
            "produces": ["docs-synced"],
        }
        seq = list(SOLO)
        seq.insert(seq.index("pre-pr"), "docs-sync")
        self.manifest["modes"]["solo"] = seq
        issues = validate(self.manifest, self.surfaces, self.config)
        self.assertTrue(issues.ok, issues.errors)

        state = {
            "mode": "solo",
            "cursor": {"current_step": "fetch", "completed_steps": []},
            "artifacts": {}, "gates": {},
            "tasks": [{"id": "T1", "status": "done"}],
            "metrics": {"fetch": {"started_at": "t0", "ended_at": None}},
        }
        walked = []
        for _ in range(len(seq) + 1):
            cur = state["cursor"]["current_step"]
            step_def = self.manifest["steps"][cur]
            for name in step_def.get("produces", []) or []:
                transitions.set_artifact(state, self.manifest, name, f"<{name}>")
            if step_def.get("gate"):
                state["gates"][cur] = {"decision": "approved"}
            cands = transitions.cursor_candidates(state, self.manifest, self.config)
            nxt = next((s for s, r in cands.items() if r == "sequence"), None)
            if nxt is None:
                break
            transitions.advance_cursor(state, self.manifest, self.config,
                                       nxt, "2026-07-08T00:00:00Z")
            walked.append(nxt)
        self.assertEqual(state["cursor"]["current_step"], "metrics", walked)
        self.assertEqual(state["artifacts"].get("docs-synced"), "<docs-synced>")

        # and the inserted step is enforced, not decorative: from develop,
        # jumping straight to pre-pr must be refused
        state2 = {
            "mode": "solo",
            "cursor": {"current_step": "develop", "completed_steps": []},
            "artifacts": {"branches": "<b>", "tasks": "<t>",
                          "task-commits": "<c>"},
            "gates": {}, "tasks": [{"id": "T1", "status": "done"}],
            "metrics": {"develop": {"started_at": "t0", "ended_at": None}},
        }
        with self.assertRaises(transitions.TransitionError) as ctx:
            transitions.advance_cursor(state2, self.manifest, self.config,
                                       "pre-pr", "2026-07-08T00:00:00Z")
        self.assertIn("docs-sync", str(ctx.exception))


class ModeEntry(unittest.TestCase):
    """A run's entry mode is minted from declared data end to end: argparse
    choices from the manifest's modes, classify() returning a verdict, and
    select_mode() resolving it through the entry step's selects_mode."""

    def setUp(self):
        self.manifest, _, self.config = shipped()

    def test_bootstrap_mode_choices_come_from_the_manifest(self):
        parser, _ = build_parser()
        base = ["bootstrap", "--work-item-id", "X", "--title", "t",
                "--change-type", "fix", "--workspace", "/tmp",
                "--run", "/tmp/r"]
        for mode in self.manifest["modes"]:
            args = parser.parse_args(base + ["--mode", mode])
            self.assertEqual(args.mode, mode)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(base + ["--mode", "not-a-mode"])

    def test_classify_returns_a_verdict_not_a_mode_name(self):
        config = {"quick_mode": {"disqualify_keywords": ["migration"]}}
        eligible, reason = workflow.classify(
            {"title": "t", "description": "Mode: quick\nfix a typo"}, config)
        self.assertIs(eligible, True)
        self.assertEqual(reason, "explicitly hinted")
        eligible, reason = workflow.classify(
            {"title": "t", "description": "Mode: quick\nadd a migration"}, config)
        self.assertIs(eligible, False)
        self.assertIn("migration", reason)

    def test_select_mode_reads_the_declared_mapping(self):
        self.assertEqual(workflow.select_mode(self.manifest, True), "quick")
        self.assertEqual(workflow.select_mode(self.manifest, False), "full")

    def test_select_mode_refuses_a_mapping_to_an_undeclared_mode(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["fetch"]["selects_mode"]["true"] = "ghost"
        from harness import state as state_mod
        with self.assertRaises(state_mod.StateError) as ctx:
            workflow.select_mode(broken, True)
        self.assertIn("ghost", str(ctx.exception))


class DeclaredArtifactReads(unittest.TestCase):
    """Regression for the create-pr finding: a step implementation reading
    `st["artifacts"][X]` where X is not in the step's declared preconditions
    or produces is invisible to the flow-completeness validator — the
    reordering proof silently doesn't cover it. Every listed implementation's
    artifact reads must be declared; extend the table when a step
    implementation grows a new artifacts read."""

    READ_RE = re.compile(
        r'\((?:st|pre)\.get\("artifacts"\) or \{\}\)\.get\("([a-z_.-]+)"\)')

    def test_step_implementations_declare_their_artifact_reads(self):
        manifest, _, _ = shipped()
        table = {"create-pr": workflow.create_pr,
                 "preflight": workflow.preflight}
        for step_id, func in table.items():
            reads = set(self.READ_RE.findall(inspect.getsource(func)))
            self.assertTrue(reads, f"{step_id}: the read-pattern regex matched "
                                   "nothing — pattern or implementation drifted")
            step = manifest["steps"][step_id]
            declared = set(step.get("preconditions") or []) | set(
                step.get("produces") or [])
            undeclared = reads - declared
            self.assertFalse(
                undeclared,
                f"{step_id} reads artifact(s) {sorted(undeclared)} that its "
                "manifest entry neither requires nor produces — declare them "
                "so the flow-completeness proof covers the dependency")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
