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
        _, self.fsm, _, _ = _load()

    def _errors(self, fsm):
        issues = schema.Issues()
        schema.validate_fsm(fsm, issues)
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


if __name__ == "__main__":
    unittest.main()
