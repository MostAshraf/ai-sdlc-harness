"""M0 done-criteria: shipped declared data validates; the validator provably
catches broken data (mutation tests); YAML round-trips losslessly."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from harness import schema

ROOT = Path(__file__).resolve().parent.parent


def _load():
    manifest = schema.load_yaml(ROOT / "pipeline" / "manifest.yaml")
    fsm = schema.load_yaml(ROOT / "pipeline" / "task-fsm.yaml")
    surfaces = schema.load_yaml(ROOT / "pipeline" / "surfaces.yaml")
    config = schema.merge_defaults(ROOT / "config" / "defaults", schema.Issues())
    return manifest, fsm, surfaces, config


class ShippedDataValidates(unittest.TestCase):
    def test_everything_valid(self):
        issues = schema.validate_all(ROOT)
        self.assertEqual(issues.errors, [])

    def test_round_trip_lossless(self):
        for rel in ("pipeline/manifest.yaml", "pipeline/task-fsm.yaml",
                    "pipeline/surfaces.yaml"):
            data = schema.load_yaml(ROOT / rel)
            self.assertEqual(yaml.safe_load(yaml.safe_dump(data)), data, rel)

    def test_package_version_matches_plugin_json(self):
        # adversarial-review finding: harness.__version__ was a second
        # hardcoded copy ("0.1.0-m0") that drifted from plugin.json through
        # 12 releases — now derived FROM it, so this can't regress silently.
        import json
        import harness
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(harness.__version__, plugin["version"])


class ValidatorCatchesBrokenManifest(unittest.TestCase):
    def setUp(self):
        self.manifest, self.fsm, self.surfaces, self.config = _load()

    def _errors(self, manifest):
        issues = schema.Issues()
        schema.validate_manifest(manifest, self.surfaces, self.config, issues)
        return issues.errors

    def test_missing_producer_is_caught(self):
        # The exact adversarial-pass bug class: quick consuming what nothing
        # in quick produces. Drop fetch's `tasks` output -> develop must fail.
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["fetch"]["produces"].remove("tasks")
        errs = self._errors(broken)
        self.assertTrue(any("develop" in e and "'tasks'" in e for e in errs), errs)

    def test_requires_repo_confirmed_rejected_on_a_gate_step(self):
        # same bar its two siblings get: a flag the engine can't meaningfully
        # honour on a gate must be refused at validation, not discovered as a
        # deadlocked cursor mid-run (a gate's exits derive from the human
        # decision, so there is nothing for this to hold shut)
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["approve-pre-pr"]["requires_repo_confirmed"] = True
        errs = self._errors(broken)
        self.assertTrue(any("requires_repo_confirmed" in e and "gate" in e
                            for e in errs), errs)

    def test_confirm_repo_predicate_needs_an_earlier_producer(self):
        # confirm-repo's `when` reads an artifact fetch produces; drop that
        # declaration and the sequence walk must catch it, because
        # eval_predicate RAISES on a missing artifact at runtime rather than
        # treating it as false — a silent manifest would strand every quick run
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["fetch"]["produces"].remove("repo-ambiguity")
        errs = self._errors(broken)
        self.assertTrue(any("repo-ambiguity" in e for e in errs), errs)

    def test_mode_must_share_entry_prefix(self):
        broken = copy.deepcopy(self.manifest)
        broken["modes"]["quick"].remove("fetch")
        errs = self._errors(broken)
        self.assertTrue(any("shared-prefix" in e for e in errs), errs)

    def test_unknown_spawn_shape(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["develop"]["spawns"][0]["shape"] = "tester"
        self.assertTrue(any("unknown shape 'tester'" in e for e in self._errors(broken)))

    def test_unknown_spawn_mode(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["develop"]["spawns"][0]["mode"] = "juggling"
        self.assertTrue(any("no mode 'juggling'" in e for e in self._errors(broken)))

    def test_verdict_bound_on_gate_step_refused(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["approve-plan"]["verdict_bound"] = {
            "mode": "plan-review", "bound": {"config": "review_rounds.max"}}
        self.assertTrue(any("not meaningful on a gate step" in e
                            for e in self._errors(broken)))

    def test_verdict_bound_wrong_shape_refused(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["plan-review"]["verdict_bound"] = {"mode": "plan-review"}
        self.assertTrue(any("`verdict_bound` must be" in e
                            for e in self._errors(broken)))
        broken["steps"]["plan-review"]["verdict_bound"] = {
            "mode": "plan-review", "bound": {"config": "review_rounds.max"},
            "surprise": True}   # unknown keys refused too
        self.assertTrue(any("`verdict_bound` must be" in e
                            for e in self._errors(broken)))

    def test_verdict_bound_outcome_artifact_must_be_produced(self):
        # the engine records it via set_artifact, which refuses names
        # outside the step's produces — a mismatch must die at validation
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["plan-review"]["verdict_bound"]["outcome_artifact"] = \
            "not-a-produced-name"
        self.assertTrue(any("must be one of the step's produces" in e
                            for e in self._errors(broken)))

    def test_default_mode_validated(self):
        issues = schema.Issues()
        broken = copy.deepcopy(self.config)
        broken["default_mode"] = "laen"   # typo must refuse, never silently
        schema.validate_configs(broken, issues)
        self.assertTrue(any("default_mode" in e for e in issues.errors))
        issues = schema.Issues()
        broken["default_mode"] = "lean"
        schema.validate_configs(broken, issues)
        self.assertFalse(any("default_mode" in e for e in issues.errors))

    def test_verdict_bound_round_marker_must_be_a_nonempty_kind(self):
        # A blank/non-string marker would silently degrade the stall guard's
        # round anchor to "no marker" instead of failing at declaration
        # (field: dual-run comparison).
        for bad in ("", "   ", 7, True):
            broken = copy.deepcopy(self.manifest)
            broken["steps"]["plan-review"]["verdict_bound"]["round_marker"] = bad
            self.assertTrue(
                any("round_marker must be" in e for e in self._errors(broken)),
                f"round_marker {bad!r} was accepted")

    def test_declared_round_markers_are_kinds_the_code_actually_emits(self):
        # adversarial-review: schema can only type-check the marker, so a
        # typo (`plan-register` for `plan-registered`) would validate clean
        # and then never match — the anchor would lose mark (c) and refuse
        # every genuine stall from round 2 on. Pin the declared value against
        # the kinds production code really appends.
        from pathlib import Path
        src = "".join(
            p.read_text(encoding="utf-8")
            for p in (Path(__file__).resolve().parent.parent / "harness")
            .rglob("*.py"))
        for sid, step in self.manifest["steps"].items():
            marker = (step.get("verdict_bound") or {}).get("round_marker")
            if marker:
                self.assertIn(f'"kind": "{marker}"', src,
                              f"{sid}: round_marker '{marker}' is never emitted")

    def test_verdict_bound_mode_must_be_spawned_by_the_step(self):
        # A step gated on a verdict it can never produce would deadlock at
        # runtime — the validator must refuse it at declaration.
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["plan-review"]["verdict_bound"]["mode"] = "pre-pr"
        self.assertTrue(any("not a mode this step spawns" in e
                            for e in self._errors(broken)))

    def test_verdict_bound_config_path_must_exist(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["plan-review"]["verdict_bound"]["bound"] = {
            "config": "no.such.knob"}
        self.assertTrue(any("'no.such.knob' not found" in e
                            for e in self._errors(broken)))

    def test_verdict_bound_requires_returns_to(self):
        broken = copy.deepcopy(self.manifest)
        del broken["steps"]["plan-review"]["returns_to"]
        self.assertTrue(any("requires a `returns_to` edge" in e
                            for e in self._errors(broken)))

    def test_plan_review_lenses_shape_validated(self):
        # the lens panel is declared data the orchestrator reads — a shape
        # error must be a validation refusal, not a runtime surprise
        broken = copy.deepcopy(self.config)
        for bad in ("contradictions",        # not a list
                    [1, 2],                  # non-string entries
                    ["gaps/../../plan"],     # not a slug — becomes a PATH
                    ["Gaps"], [""]):         # case / empty
            issues = schema.Issues()
            broken["plan_review"] = {"lenses": bad}
            schema.validate_configs(broken, issues)
            self.assertTrue(any("plan_review.lenses" in e
                                for e in issues.errors), bad)
        issues = schema.Issues()
        broken["plan_review"] = {"lenses": []}   # empty IS legal (fallback)
        schema.validate_configs(broken, issues)
        self.assertFalse(any("plan_review" in e for e in issues.errors))
        issues = schema.Issues()
        del broken["plan_review"]                # missing knob is not
        schema.validate_configs(broken, issues)
        self.assertTrue(any("missing 'plan_review'" in e
                            for e in issues.errors))

    def test_verdict_bound_cannot_share_a_step_with_an_escalation_source(self):
        # Two data features each claiming exclusive exit ownership would
        # resolve by interpreter ordering — refused at validation instead.
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["quick-recheck"]["verdict_bound"] = {
            "mode": "plan-review", "bound": {"config": "review_rounds.max"}}
        broken["steps"]["quick-recheck"]["returns_to"] = "develop"
        self.assertTrue(any("cannot share a step with an escalation source"
                            in e for e in self._errors(broken)))

    def test_bad_on_reject_target(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["approve-plan"]["on_reject"] = "no-such-step"
        self.assertTrue(any("on_reject target 'no-such-step'" in e
                            for e in self._errors(broken)))

    def test_unreachable_step(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["orphan"] = {"owner": "orchestrator", "produces": ["x"]}
        self.assertTrue(any("'orphan' is unreachable" in e for e in self._errors(broken)))

    def test_when_config_ref_must_exist(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["approve-security"]["when"]["at_least"]["config"] = "nope.nope"
        self.assertTrue(any("'nope.nope' not found" in e for e in self._errors(broken)))

    def test_escalation_refs_validated(self):
        broken = copy.deepcopy(self.manifest)
        broken["escalations"][0]["to"]["step"] = "quick-recheck"  # not in full
        self.assertTrue(any("to.step 'quick-recheck' not in mode 'full'" in e
                            for e in self._errors(broken)))

    def test_gate_may_not_spawn(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["approve-plan"]["spawns"] = [{"shape": "reviewer", "mode": "review"}]
        self.assertTrue(any("gate step must not spawn" in e for e in self._errors(broken)))

    def test_select_only_meaningful_on_a_gate(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["develop"]["select"] = True
        self.assertTrue(any("only meaningful on a gate step" in e
                            for e in self._errors(broken)))

    def test_select_rejects_forward_on_or_on_reject(self):
        broken = copy.deepcopy(self.manifest)
        broken["steps"]["select-comments"]["on_reject"] = "analyze-comments"
        self.assertTrue(any("don't use forward_on/on_reject" in e
                            for e in self._errors(broken)))


class ValidatorCatchesBrokenFsm(unittest.TestCase):
    def setUp(self):
        _, self.fsm, self.surfaces, _ = _load()

    def _errors(self, fsm):
        # the REAL surfaces, so the actor-vs-spawn-shape collision check
        # below runs against the shapes the guards actually accept
        issues = schema.Issues()
        schema.validate_fsm(fsm, self.surfaces, issues)
        return issues.errors

    def test_initial_must_be_declared(self):
        broken = copy.deepcopy(self.fsm)
        broken["initial"] = "limbo"
        self.assertTrue(any("initial 'limbo'" in e for e in self._errors(broken)))

    def test_transition_states_must_exist(self):
        broken = copy.deepcopy(self.fsm)
        broken["transitions"].append({"from": "done", "to": "limbo"})
        self.assertTrue(any("to 'limbo'" in e for e in self._errors(broken)))

    def test_duplicate_transition(self):
        broken = copy.deepcopy(self.fsm)
        broken["transitions"].append({"from": "pending", "to": "in-progress"})
        self.assertTrue(any("duplicate transition" in e for e in self._errors(broken)))

    def test_a_missing_terminal_list_is_an_error(self):
        """`terminal` is read by the engine (transitions.terminal_statuses),
        so an absent one silently empties the set that seven "is this task
        finished?" questions share — the develop sync point would never
        release and no task would ever count as done."""
        broken = copy.deepcopy(self.fsm)
        del broken["terminal"]
        self.assertTrue(any("`terminal`" in e for e in self._errors(broken)))

    def test_an_empty_terminal_list_is_an_error_too(self):
        broken = copy.deepcopy(self.fsm)
        broken["terminal"] = []
        self.assertTrue(any("`terminal`" in e for e in self._errors(broken)))

    def test_a_terminal_entry_must_be_a_declared_state(self):
        broken = copy.deepcopy(self.fsm)
        broken["terminal"] = ["done", "finished"]     # typo for `archived`
        self.assertTrue(any("terminal 'finished'" in e
                            for e in self._errors(broken)))

    def test_the_shipped_terminal_set_is_what_the_engine_reads(self):
        """The declaration and the loader must not be two truths — a test
        pinning only the literal would pass against a loader reading the
        wrong key."""
        from harness import transitions
        self.assertEqual(list(transitions.terminal_statuses()),
                         self.fsm["terminal"])
        self.assertEqual(self.fsm["terminal"], ["done", "archived"])

    def test_a_missing_spawn_pairing_block_is_an_error(self):
        """Read by FOUR independent readers across two layers (the flagged
        gauge, the stall guard, the spawn guard, the SubagentStop capture).
        Absent, `open_pendings` matches no kind at all and every "is a spawn
        still in flight?" question in the system quietly answers no."""
        broken = copy.deepcopy(self.fsm)
        del broken["spawn_pairing"]
        self.assertTrue(any("`spawn_pairing`" in e
                            for e in self._errors(broken)))

    def test_the_pending_record_needs_both_kind_and_actor(self):
        # the actor half IS the anti-forgery bound — a pending declared
        # without one is a pending any `log-event` caller can mint
        for pending in ({"kind": "spawn-pending"}, {"actor": "capture"},
                        {"kind": "spawn-pending", "actor": ""},
                        {"kind": "spawn-pending", "actor": "capture",
                         "extra": "x"}, "spawn-pending"):
            broken = copy.deepcopy(self.fsm)
            broken["spawn_pairing"]["pending"] = pending
            self.assertTrue(any("spawn_pairing.pending" in e
                                for e in self._errors(broken)), pending)

    def test_an_engine_interpreted_resolver_name_must_be_declared(self):
        # `abandoned` is asked for BY NAME (a late stop for an abandoned
        # spawn must be refused capture) — data names it, code provides it
        broken = copy.deepcopy(self.fsm)
        del broken["spawn_pairing"]["resolvers"]["abandoned"]
        self.assertTrue(any("missing 'abandoned'" in e
                            for e in self._errors(broken)))

    def test_a_resolver_may_not_share_the_pending_kind_or_another_resolvers(self):
        broken = copy.deepcopy(self.fsm)
        broken["spawn_pairing"]["resolvers"]["abandoned"]["kind"] = \
            "spawn-pending"
        self.assertTrue(any("also the pending kind" in e
                            for e in self._errors(broken)))
        broken = copy.deepcopy(self.fsm)
        broken["spawn_pairing"]["resolvers"]["abandoned"]["kind"] = \
            "spawn-captured"
        self.assertTrue(any("more than one resolver" in e
                            for e in self._errors(broken)))

    def test_empty_or_misshapen_resolvers_are_errors(self):
        for resolvers in ({}, [], {"captured": {"kind": "spawn-captured"}}):
            broken = copy.deepcopy(self.fsm)
            broken["spawn_pairing"]["resolvers"] = resolvers
            self.assertTrue(any("resolvers" in e for e in self._errors(broken)),
                            resolvers)

    def test_the_pending_kind_must_be_a_flagged_kind(self):
        """The declared gate that did not gate (round-4 review, executed): a
        pending kind outside FLAGGED_EVENT_KINDS validated clean, then at
        runtime `open_pendings` refused every re-spawn and every stall while
        the gauge read 0 outstanding and health read HEALTHY — the exact
        split task-fsm.yaml's own comment claims the declaration prevents."""
        broken = copy.deepcopy(self.fsm)
        broken["spawn_pairing"]["pending"]["kind"] = "spawn-launched"
        self.assertTrue(any("FLAGGED_EVENT_KINDS" in e
                            for e in self._errors(broken)))

    def test_an_undeclared_resolver_name_is_an_error(self):
        """`closed_agent_ids` honours every declared resolver BY VALUE, so an
        extra one — even under a name no code reads, even under the empty
        string the fuzzer found — is a further way to close a pending that
        nothing ever writes."""
        for name in ("retired", ""):
            broken = copy.deepcopy(self.fsm)
            broken["spawn_pairing"]["resolvers"][name] = {
                "kind": "spawn-retired", "actor": "somebody"}
            self.assertTrue(any("not a name the engine interprets" in e
                                for e in self._errors(broken)), name)

    def test_an_actor_may_not_be_a_declared_spawn_shape(self):
        """The actor IS the anti-forgery bound, so it must be a value only
        the owning writer issues. A spawn SHAPE is the opposite of that —
        it is published in every spawn's own headers, and it is exactly what
        `actor` held before round 4 — so declaring `actor: reviewer` would
        re-open the forgery that change closed, on either half of the
        pairing."""
        for path in (("pending",), ("resolvers", "abandoned")):
            for shape in ("reviewer", "developer", "planner"):
                broken = copy.deepcopy(self.fsm)
                node = broken["spawn_pairing"]
                for part in path:
                    node = node[part]
                node["actor"] = shape
                self.assertTrue(any("is a declared spawn shape" in e
                                    for e in self._errors(broken)),
                                (path, shape))

    def test_the_shipped_spawn_pairing_is_what_the_engine_reads(self):
        """Same two-truths rule as `terminal` above: pinning the literal
        alone would pass against a loader reading the wrong key."""
        from harness import transitions
        from harness.workflow import FLAGGED_EVENT_KINDS
        self.assertEqual(transitions.spawn_pairing(), self.fsm["spawn_pairing"])
        self.assertEqual(
            self.fsm["spawn_pairing"],
            {"pending": {"kind": "spawn-pending", "actor": "capture"},
             "resolvers": {
                 "captured": {"kind": "spawn-captured", "actor": "capture"},
                 "abandoned": {"kind": "spawn-abandoned", "actor": "stall"}}})
        # …and the gauge's own membership list names the same record, or the
        # pending is either invisible to the human or unpairable
        self.assertIn(self.fsm["spawn_pairing"]["pending"]["kind"],
                      FLAGGED_EVENT_KINDS)


class ValidatorCatchesBrokenConfig(unittest.TestCase):
    def setUp(self):
        _, _, _, self.config = _load()

    def _errors(self, config):
        issues = schema.Issues()
        schema.validate_configs(config, issues)
        return issues.errors

    def test_threshold_must_be_in_severity_order(self):
        broken = copy.deepcopy(self.config)
        broken["security"]["gate_threshold"] = "catastrophic"
        self.assertTrue(any("gate_threshold" in e for e in self._errors(broken)))

    def test_branch_template_needs_type(self):
        broken = copy.deepcopy(self.config)
        broken["naming"]["branch"] = "{id}-{slug}"
        self.assertTrue(any("naming.branch missing placeholder {type}" in e
                            for e in self._errors(broken)))

    def test_type_map_values_must_be_change_types(self):
        broken = copy.deepcopy(self.config)
        broken["work_item_type_map"]["Bug"] = "hotdog"
        self.assertTrue(any("'hotdog' not in change_types" in e for e in self._errors(broken)))

    def test_model_object_form_needs_default(self):
        broken = copy.deepcopy(self.config)
        broken["subagent_models"]["reviewer"] = {"pre-pr": "claude-opus-4-8"}
        self.assertTrue(any("needs 'default'" in e for e in self._errors(broken)))

    def test_lenses_by_change_type_keys_must_be_change_types(self):
        # a typo'd key would silently never match, and the full default
        # panel would run where the override intended none
        broken = copy.deepcopy(self.config)
        broken.setdefault("plan_review", {})["lenses_by_change_type"] = {
            "hotdog": []}
        self.assertTrue(any(
            "lenses_by_change_type key 'hotdog' not in change_types" in e
            for e in self._errors(broken)))

    def test_lenses_by_change_type_values_must_be_lens_lists(self):
        broken = copy.deepcopy(self.config)
        broken.setdefault("plan_review", {})["lenses_by_change_type"] = {
            "chore": "gaps"}
        self.assertTrue(any("must be a list of lens-name slugs" in e
                            for e in self._errors(broken)))

    def test_lenses_by_change_type_values_held_to_the_slug_rule(self):
        # mapped lens names become file paths (reports/plan-attack-<lens>)
        # and spawn-ask text, exactly like the default list — a path-shaped
        # value must not validate clean where the default would not
        broken = copy.deepcopy(self.config)
        broken.setdefault("plan_review", {})["lenses_by_change_type"] = {
            "fix": ["../../../evil path"]}
        self.assertTrue(any("must be a list of lens-name slugs" in e
                            for e in self._errors(broken)))


if __name__ == "__main__":
    unittest.main()
