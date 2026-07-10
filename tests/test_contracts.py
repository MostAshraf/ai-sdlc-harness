"""WS-3 unit coverage (m8-plan-fidelity.md): contract schema depth
(type/producer/consumers, multi-fragment signature) and reconciliation
false-positive reduction (test-path exclusion), isolated from the full
fetch->plan->register CLI flow test_breadth.py::TwoRepoContracts already
covers end-to-end for the legacy flat shape."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness import gitops, state as state_mod, workflow
from harness.cli import load_declared
from tests.test_gitops import make_repo


class _ContractHarness(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.run = self.workspace / "ai" / "2026-01-01-C-1"
        self.manifest, self.fsm, self.config = load_declared(self.workspace)
        self.repo_a = make_repo(self.workspace, "repo-a")
        self.repo_b = make_repo(self.workspace, "repo-b")
        state_mod.bootstrap(
            self.run, self.workspace,
            work_item={"id": "C-1", "title": "t", "provider_ref": ""},
            mode="full", change_type="feature",
            tasks=[{"id": "T1", "repo": str(self.repo_a)}], entry_step="plan")

    def tearDown(self):
        shutil.rmtree(self.workspace)

    def _repos(self):
        return {"repo-a": str(self.repo_a), "repo-b": str(self.repo_b)}

    def _register(self, contracts):
        workflow.plan_register(
            self.workspace, self.run, self.manifest,
            tasks=[{"id": "T1", "repo": str(self.repo_a)}], contracts=contracts)

    def _write(self, repo, path, content):
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        gitops.run_git(repo, "add", "-A")
        gitops.run_git(repo, "commit", "-m", "wip")


class ContractSchema(_ContractHarness):
    def test_legacy_flat_shape_still_validates(self):
        self._register([{"id": "C1", "signature": "def api()",
                         "repos": ["repo-a", "repo-b"]}])
        st = state_mod.load(self.run, self.workspace)
        self.assertEqual(st["contracts"][0]["repos"], ["repo-a", "repo-b"])

    def test_directional_shape_requires_both_producer_and_consumers(self):
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "signature": "def api()",
                             "producer": "repo-a"}])

    def test_bad_type_rejected(self):
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "signature": "def api()",
                             "repos": ["repo-a"], "type": "carrier-pigeon"}])

    def test_both_repos_and_directional_rejected_as_ambiguous(self):
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "signature": "def api()",
                             "repos": ["repo-a", "repo-b"],
                             "producer": "repo-a", "consumers": ["repo-b"]}])

    def test_empty_fragment_rejected(self):
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "repos": ["repo-a"],
                             "signature": ["real fragment", ""]}])

    def test_prose_fragment_rejected(self):
        """Validation-walk F3: reconcile-contracts matches fragments by literal
        `git grep -F`, so a prose fragment (an English description with the
        tell-tale em/en-dash) matches nothing and false-reports drift on
        correctly-implemented code. Reject it at declaration; the dash-free
        signatures the other schema tests register still validate fine."""
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "repos": ["repo-a", "repo-b"],
                             "signature": ["filter_notes(notes, tag) — exact "
                                           "case-sensitive membership"]}])

    def test_directional_enriched_shape_round_trips(self):
        self._register([{"id": "C1", "type": "http", "producer": "repo-a",
                         "consumers": ["repo-b"],
                         "signature": ["POST /v2", "field: x"]}])
        c = state_mod.load(self.run, self.workspace)["contracts"][0]
        self.assertEqual((c["producer"], c["consumers"], c["type"], c["signature"]),
                         ("repo-a", ["repo-b"], "http", ["POST /v2", "field: x"]))


class ContractReconciliation(_ContractHarness):
    def test_all_fragments_present_is_clean(self):
        self._register([{"id": "C1", "producer": "repo-a", "consumers": ["repo-b"],
                         "signature": ["POST /v2/items", "field: item_id"]}])
        self._write(self.repo_a, "api.py", "POST /v2/items\nfield: item_id\n")
        self._write(self.repo_b, "client.py", "POST /v2/items\nfield: item_id\n")
        verdict = workflow.reconcile_contracts(self.workspace, self.run, self.config,
                                               self._repos())
        self.assertEqual(verdict, "clean")

    def test_one_fragment_absent_is_drift(self):
        self._register([{"id": "C1", "producer": "repo-a", "consumers": ["repo-b"],
                         "signature": ["POST /v2/items", "field: item_id"]}])
        self._write(self.repo_a, "api.py", "POST /v2/items\nfield: item_id\n")
        self._write(self.repo_b, "client.py", "POST /v2/items\n")  # missing 2nd fragment
        verdict = workflow.reconcile_contracts(self.workspace, self.run, self.config,
                                               self._repos())
        self.assertEqual(verdict, "drift")
        report = (self.run / "reports" / "contracts.md").read_text()
        self.assertIn("field: item_id", report)
        self.assertIn("MISSING", report)

    def test_match_only_in_test_path_is_excluded_as_false_positive(self):
        self._register([{"id": "C1", "repos": ["repo-a"],
                         "signature": "def api_v2(payload)"}])
        self._write(self.repo_a, "tests/test_api.py",
                   "# def api_v2(payload) mentioned here, not implemented\n")
        verdict = workflow.reconcile_contracts(self.workspace, self.run, self.config,
                                               self._repos())
        self.assertEqual(verdict, "drift")

    def test_match_outside_test_path_still_counts(self):
        self._register([{"id": "C1", "repos": ["repo-a"],
                         "signature": "def api_v2(payload)"}])
        self._write(self.repo_a, "api.py", "def api_v2(payload): pass\n")
        verdict = workflow.reconcile_contracts(self.workspace, self.run, self.config,
                                               self._repos())
        self.assertEqual(verdict, "clean")

    def test_root_level_test_file_still_excluded(self):
        """Regression: git's non-glob pathspec interpretation of a `**/`-
        prefixed exclude (e.g. `**/*_test.*`) only matches past at least one
        real directory — silently failing to exclude a root-level file. Must
        use `glob` pathspec magic, matching gitops._match's existing
        `**/`-prefix special-case for this same test_paths convention."""
        self._register([{"id": "C1", "repos": ["repo-a"],
                         "signature": "def api_v2(payload)"}])
        self._write(self.repo_a, "api_test.py",  # root-level; matches **/*_test.*
                   "# def api_v2(payload) mentioned here, not implemented\n")
        verdict = workflow.reconcile_contracts(self.workspace, self.run, self.config,
                                               self._repos())
        self.assertEqual(verdict, "drift")

    def test_type_and_producer_consumer_surfaced_in_report(self):
        self._register([{"id": "C1", "type": "http", "producer": "repo-a",
                         "consumers": ["repo-b"], "signature": "POST /v2/items"}])
        self._write(self.repo_a, "api.py", "POST /v2/items\n")
        self._write(self.repo_b, "client.py", "POST /v2/items\n")
        workflow.reconcile_contracts(self.workspace, self.run, self.config, self._repos())
        report = (self.run / "reports" / "contracts.md").read_text()
        self.assertIn("http", report)
        self.assertIn("repo-a → repo-b", report)

    def test_duplicate_consumer_deduped_in_report_lines_and_role_text(self):
        self._register([{"id": "C1", "producer": "repo-a",
                         "consumers": ["repo-b", "repo-b"],
                         "signature": "POST /v2/items"}])
        self._write(self.repo_a, "api.py", "POST /v2/items\n")
        self._write(self.repo_b, "client.py", "POST /v2/items\n")
        workflow.reconcile_contracts(self.workspace, self.run, self.config, self._repos())
        report = (self.run / "reports" / "contracts.md").read_text()
        self.assertEqual(report.count("@ repo-b"), 1)  # one line, not one per duplicate
        self.assertIn("repo-a → repo-b)", report)      # role text also deduped


if __name__ == "__main__":
    unittest.main()
