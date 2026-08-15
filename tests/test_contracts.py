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
from tests.test_gitops import make_monorepo, make_repo
from tests import support


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
        # plan_register requires the human-confirmed scope; this unit
        # harness has no init'd workspace config, so seed state directly
        # (what `harness scope-register` records in production)
        st = state_mod.load(self.run, self.workspace)
        st["scope"] = {"repos": [str(self.repo_a)],
                       "at": "2026-01-01T00:00:00+00:00"}
        state_mod.save(self.run, self.workspace, st)

    def tearDown(self):
        support.rmtree(self.workspace)

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
        `git grep -F` (route-structurally for http `{param}` templates), so a
        prose fragment (an English description with the tell-tale em/en-dash)
        matches nothing and false-reports drift on correctly-implemented
        code. Reject it at declaration; the dash-free signatures the other
        schema tests register still validate fine."""
        with self.assertRaises(state_mod.StateError):
            self._register([{"id": "C1", "repos": ["repo-a", "repo-b"],
                             "signature": ["filter_notes(notes, tag) — exact "
                                           "case-sensitive membership"]}])

    def test_param_only_http_fragment_rejected(self):
        """Adversarial review of the route-aware matcher (both lenses,
        independently): an all-param http fragment (`{id}`, `{a}/{b}`)
        elides to an anchorless pattern that matches ANY line — the check
        turns vacuous and fails toward invisible false CLEAN, strictly
        worse than the em-dash prose tell's visible false drift. Same
        fail-fast slot: reject at declaration."""
        for frag in ("{id}", "{a}/{b}"):
            with self.assertRaises(state_mod.StateError):
                self._register([{"id": "C1", "type": "http",
                                 "producer": "repo-a",
                                 "consumers": ["repo-b"],
                                 "signature": [frag]}])

    def test_anchored_http_fragment_still_validates(self):
        # the positive control for the all-param rejection: one literal
        # segment is enough of an anchor
        self._register([{"id": "C1", "type": "http", "producer": "repo-a",
                         "consumers": ["repo-b"],
                         "signature": ["users/{id}"]}])
        c = state_mod.load(self.run, self.workspace)["contracts"][0]
        self.assertEqual(c["signature"], ["users/{id}"])

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
        report = (self.run / "reports" / "contracts.md").read_text(encoding="utf-8")
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
        report = (self.run / "reports" / "contracts.md").read_text(encoding="utf-8")
        self.assertIn("http", report)
        self.assertIn("repo-a → repo-b", report)

    def test_duplicate_consumer_deduped_in_report_lines_and_role_text(self):
        self._register([{"id": "C1", "producer": "repo-a",
                         "consumers": ["repo-b", "repo-b"],
                         "signature": "POST /v2/items"}])
        self._write(self.repo_a, "api.py", "POST /v2/items\n")
        self._write(self.repo_b, "client.py", "POST /v2/items\n")
        workflow.reconcile_contracts(self.workspace, self.run, self.config, self._repos())
        report = (self.run / "reports" / "contracts.md").read_text(encoding="utf-8")
        self.assertEqual(report.count("@ repo-b"), 1)  # one line, not one per duplicate
        self.assertIn("repo-a → repo-b)", report)      # role text also deduped


class HttpRouteReconciliation(_ContractHarness):
    """Field run 459226 (downstream fork report): a `type: http` fragment
    carrying a `{param}` token matches route-STRUCTURALLY — each param
    elides to one path segment, so the two sides may name it differently —
    while the literal route text around it still trips genuine drift.
    Everything without a `{param}`, and every non-http contract, keeps the
    exact literal `-F` match."""

    def _http(self, signature):
        self._register([{"id": "C1", "type": "http", "producer": "repo-a",
                         "consumers": ["repo-b"], "signature": signature}])

    def _verdict(self):
        return workflow.reconcile_contracts(self.workspace, self.run,
                                            self.config, self._repos())

    def test_param_named_differently_across_repos_is_clean(self):
        # the 459226 case verbatim: producer declares the route template
        # (`{id}`), the consumer interpolates a differently-named variable —
        # byte-identical wire shape, previously reported as drift
        self._http(["{id}/authorization"])
        self._write(self.repo_a, "controller.cs",
                    '[Route("{id}/authorization")]\n')
        self._write(self.repo_b, "client.cs",
                    '$"{UsersV5Route}/{Uri.EscapeDataString(userId)}'
                    '/authorization"\n')
        self.assertEqual(self._verdict(), "clean")

    def test_genuinely_divergent_path_still_drifts(self):
        self._http(["{id}/authorization"])
        self._write(self.repo_a, "controller.cs",
                    '[Route("{id}/authorization")]\n')
        self._write(self.repo_b, "client.cs",
                    '$"{UsersV5Route}/{userId}/authz"\n')  # authz ≠ authorization
        self.assertEqual(self._verdict(), "drift")

    def test_non_http_contract_with_braces_stays_literal(self):
        # elision is keyed on `type: http` — a dto fragment with braces
        # still requires the verbatim text on both sides
        self._register([{"id": "C1", "type": "dto", "producer": "repo-a",
                         "consumers": ["repo-b"],
                         "signature": ["{id}/authorization"]}])
        self._write(self.repo_a, "shape.cs", '"{id}/authorization"\n')
        self._write(self.repo_b, "client.cs",
                    '$"{UsersV5Route}/{Uri.EscapeDataString(userId)}'
                    '/authorization"\n')  # no literal {id} → MISSING
        self.assertEqual(self._verdict(), "drift")

    def test_route_metacharacter_matches_only_literally(self):
        # the `.` in a route is ERE-escaped when literal segments are
        # spliced into the regex: `v2x1` must NOT satisfy `v2.1`
        self._http(["v2.1/{id}/items"])
        self._write(self.repo_a, "controller.cs",
                    '[Route("v2.1/{id}/items")]\n')
        self._write(self.repo_b, "client.cs",
                    '"v2x1/" + userId + "/items"\n')
        self.assertEqual(self._verdict(), "drift")
        report = (self.run / "reports" / "contracts.md").read_text(encoding="utf-8")
        self.assertIn("@ repo-a: present", report)     # template side matched
        self.assertIn("@ repo-b: **MISSING**", report)  # x ≠ literal dot

    def test_http_fragment_without_param_stays_literal(self):
        # the other boundary of the switch: `type: http` alone does not
        # opt a fragment into -E — no {param} token, no elision
        self._http(["POST /v2/items"])
        self._write(self.repo_a, "api.py", "POST /v2/items\n")
        self._write(self.repo_b, "client.py", "POST /v2/orders\n")  # not verbatim
        self.assertEqual(self._verdict(), "drift")

    def test_route_constraint_param_still_matches(self):
        # ASP.NET-style constraint syntax: the producer's `{id:int}` is one
        # braced segment to the elision — the declared `{id}` matches it
        self._http(["users/{id}/items"])
        self._write(self.repo_a, "controller.cs",
                    '[Route("users/{id:int}/items")]\n')
        self._write(self.repo_b, "client.cs", '$"users/{uid}/items"\n')
        self.assertEqual(self._verdict(), "clean")


class SubtreeContractSurface(_ContractHarness):
    """A registered repo is not necessarily a checkout ROOT any more: one
    logical repo may sit at `<checkout>` while another sits at
    `<checkout>/frontend`, one `.git` between them (gitops' SubtreeLogicalRepos
    covers the same shape from the git side).

    `git grep`'s exclusion pathspecs are CWD-RELATIVE, so the parent
    registration's `:(exclude,glob)ai/**` and `:(exclude,glob)tests/**`
    anchor at the checkout root and never reach into the sibling — while its
    `-- .` search scope does. The sharp case is the sibling's PUBLISHED
    MIRROR: publish_mirror drops the run's own state.yaml, contract
    declarations verbatim, into `<repo>/ai/<run>/`, so the parent was
    matching the sibling's copy of the DECLARATION and calling it `present`.
    A false CLEAN at ⟨approve-pre-pr⟩ — the direction this checker is not
    allowed to fail in, and in lean mode the single guaranteed human stop."""

    FRAG = "def api_v2(payload)"

    def setUp(self):
        super().setUp()
        self.mono = make_monorepo(self.workspace)
        self.frontend = self.mono / "frontend"

    def _mono_repos(self):
        return {"mono": str(self.mono), "frontend": str(self.frontend)}

    def _verdict(self):
        return workflow.reconcile_contracts(self.workspace, self.run,
                                            self.config, self._mono_repos())

    def _declare(self, repo_name):
        self._register([{"id": "C1", "repos": [repo_name],
                         "signature": self.FRAG}])

    # ------------------------------------------ the parent registration

    def test_sibling_mirror_of_the_declaration_does_not_satisfy_the_parent(self):
        """The reproduced case. Byte-for-byte what publish_mirror writes into
        the frontend registration — and it is the contract's own text, so a
        parent that can see it always reports `present`, for every contract,
        forever. Excluded at the parent's own `ai/**` since session D; the
        sibling's copy needs the nested-registration subtraction."""
        self._declare("mono")
        self._write(self.mono, f"frontend/ai/{self.run.name}/state.yaml",
                    f"contracts:\n- id: C1\n  signature: {self.FRAG}\n")
        self.assertEqual(self._verdict(), "drift")

    def test_sibling_test_file_does_not_satisfy_the_parent(self):
        """The other half of the same cwd-relativity: `tests/**` anchors at
        the parent's cwd, so `frontend/tests/` was un-excluded — resurrecting
        verbatim the "a mention in a test counts as an implementation" bug
        test_match_only_in_test_path_is_excluded_as_false_positive pins for
        the single-checkout shape. Deliberately NOT named `test_*.py`: that
        basename is caught by the `**/test_*.py` glob at any depth, which
        would hide the anchoring defect this test exists to catch."""
        self._declare("mono")
        self._write(self.mono, "frontend/tests/api_helper.py",
                    f"# {self.FRAG} mentioned here, not implemented\n")
        self.assertEqual(self._verdict(), "drift")

    def test_sibling_source_does_not_satisfy_the_parent(self):
        """Same false clean, no mirror and no test path involved: a
        separately-registered logical repo's source is not this repo's
        contract surface — `frontend` implementing the signature satisfies
        `frontend`'s row in the report, never `mono`'s."""
        self._declare("mono")
        self._write(self.mono, "frontend/impl.py", f"{self.FRAG}: pass\n")
        self.assertEqual(self._verdict(), "drift")

    def test_parents_own_source_still_counts(self):
        """The positive control for all three: the subtraction removes the
        nested registration, not the parent's own tree."""
        self._declare("mono")
        self._write(self.mono, "impl.py", f"{self.FRAG}: pass\n")
        self.assertEqual(self._verdict(), "clean")

    # --------------------------------------- the subtree registration

    def test_subtree_registration_is_unaffected(self):
        """Nothing changes for the child: its cwd-relative excludes already
        sit exactly under its cwd-scoped `.`, so its own tests/ and mirror
        were always excluded and its own source always visible. Pinned so a
        later "fix" that prefixes the globs cannot silently break it."""
        self._declare("frontend")
        self._write(self.mono, "frontend/tests/api_helper.py",
                    f"# {self.FRAG} mentioned here, not implemented\n")
        self._write(self.mono, f"frontend/ai/{self.run.name}/state.yaml",
                    f"contracts:\n- id: C1\n  signature: {self.FRAG}\n")
        self.assertEqual(self._verdict(), "drift")
        self._write(self.mono, "frontend/impl.py", f"{self.FRAG}: pass\n")
        self.assertEqual(self._verdict(), "clean")

    def test_parent_source_does_not_satisfy_the_subtree(self):
        # the mirror image, already true before this change (`-- .` is
        # cwd-scoped) — asserted so the pair is symmetric on the record
        self._declare("frontend")
        self._write(self.mono, "impl.py", f"{self.FRAG}: pass\n")
        self.assertEqual(self._verdict(), "drift")


class ContractExcludePathspecs(_ContractHarness):
    """The pathspec list itself, asserted literally — the regression fence
    around "a root registration's behaviour is UNCHANGED"."""

    def setUp(self):
        super().setUp()
        self.mono = make_monorepo(self.workspace)
        self.frontend = self.mono / "frontend"
        self.globs = self.config["language"]["test_paths"]
        self.base = ([f":(exclude,glob){g}" for g in self.globs]
                     + [":(exclude,glob)ai/**"])

    def _mono_repos(self):
        return {"mono": str(self.mono), "frontend": str(self.frontend)}

    def test_separate_checkouts_are_byte_identical_to_before(self):
        # the shape every deployment before subtree registrations had, and
        # still the common one: two registered repos, neither inside the
        # other, so the nested-registration subtraction contributes nothing
        self.assertEqual(
            workflow._contract_excludes(self.repo_a, self.globs, self._repos()),
            self.base)

    def test_subtree_registration_is_byte_identical_too(self):
        # a child registration's excludes are cwd-relative under a cwd-scoped
        # `.`; prefixing them (`frontend/tests/**` issued FROM `frontend`)
        # would apply the prefix twice and match nothing — probed on git
        # 2.55.0.windows.3
        self.assertEqual(
            workflow._contract_excludes(self.frontend, self.globs,
                                        self._mono_repos()),
            self.base)

    def test_parent_registration_subtracts_the_nested_one(self):
        self.assertEqual(
            workflow._contract_excludes(self.mono, self.globs,
                                        self._mono_repos()),
            self.base + [":(exclude,glob)frontend/**"])

    def test_nested_registrations_skips_self_and_outsiders(self):
        # `.` is the dangerous one: `:(exclude,glob)./**` empties the search
        # scope outright (probed), so a second spelling of the repo's own
        # registration must never survive the filter. Deep nesting is kept —
        # redundant beside `frontend`, but the subtraction is not required to
        # know that a registration list has no overlaps.
        repos = {"mono": str(self.mono),
                 "mono-again": str(self.mono) + "/.",
                 "frontend": str(self.frontend),
                 "admin": str(self.frontend / "admin"),
                 "outside": str(self.repo_a)}
        self.assertEqual(workflow._nested_registrations(self.mono, repos),
                         ["frontend", "frontend/admin"])
        self.assertEqual(workflow._nested_registrations(self.repo_a, repos), [])


class HttpRouteRegexUnit(unittest.TestCase):
    """The pure-function edges of _http_route_regex the repo-level tests
    don't reach (all adversarial-review findings on this change)."""

    def test_degenerate_all_param_template_falls_back_to_literal(self):
        # anchorless ERE would match anything (false CLEAN) — refuse and
        # let the caller keep -F, which fails toward visible drift
        self.assertIsNone(workflow._http_route_regex("{id}"))
        self.assertIsNone(workflow._http_route_regex("{a}/{b}"))

    def test_quote_bearing_brace_text_is_not_a_param(self):
        # a JSON shape under an http contract stays a literal fragment —
        # the token charset excludes quotes, same as the elided segment's
        self.assertIsNone(workflow._http_route_regex('{"ok":true}'))

    def test_brace_wrapped_literal_with_separator_stays_literal(self):
        # `{a/b}` is never captured as a token (it carries a separator) —
        # parity classification must keep it literal, not shape-match it
        rx = workflow._http_route_regex("{a/b}/{id}")
        self.assertIn("[{]a/b[}]", rx)


if __name__ == "__main__":
    unittest.main()
