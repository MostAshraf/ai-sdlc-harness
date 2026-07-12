"""M6 done-criteria: contract suite green per adapter (fixture-driven fake
CLIs — stateful stubs recording exact argv), PR creation per git provider
(argv + link-emulation asserted), MCP normalize round-trips. Live-forge
verification (real PR) is a user-run step: `gh/glab/az auth` + one fetch."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.providers import dispatch, normalize, ProviderError, ProviderUnsupported
from harness.providers import git_providers
from harness.providers.git_providers import create_pr
from tests.test_providers import assert_work_item_contract
from tests import support

STUB = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
base = Path(__file__).parent
(base / "invocations.log").open("a").write(json.dumps(sys.argv[1:]) + "\n")
(base / "cwd.log").open("a").write(str(Path.cwd()) + "\n")
state_file = base / "state.json"
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {
    "state": "{initial_state}", "comments": []}
args = sys.argv[1:]
joined = " ".join(args)
if "{fetch_marker}" in joined:
    out = json.loads((base / "fetch.json").read_text(encoding="utf-8"))
    {state_patch}
    print(json.dumps(out))
elif any(a in args for a in ("close", "reopen")) or "--state" in joined:
    if "--state" in args:
        state["state"] = args[args.index("--state") + 1]
    else:
        state["state"] = "{closed}" if "close" in args else "{initial_state}"
    state_file.write_text(json.dumps(state)); print("{}")
elif "comment" in args or "note" in args or "--discussion" in joined:
    state["comments"].append(joined); state_file.write_text(json.dumps(state))
    print("{}")
elif "pr" in args or "mr" in args:
    print((base / "pr_output.txt").read_text(encoding="utf-8"))
else:
    print("{}")
state_file.write_text(json.dumps(state))
'''


class FakeCliHarness(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self._path = os.environ["PATH"]
        # os.pathsep, not ':' — a literal ':' corrupts PATH wholesale on
        # Windows (first Windows triage: the real host `glab` leaked
        # through and answered where the stub should have)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._path}"

    def tearDown(self):
        os.environ["PATH"] = self._path
        support.rmtree(self.bin)

    def stub(self, name: str, fetch_json: dict, *, fetch_marker: str,
             initial_state: str, closed: str, state_patch: str = "pass",
             pr_output: str = "https://example/pr/1"):
        (self.bin / "fetch.json").write_text(json.dumps(fetch_json))
        (self.bin / "pr_output.txt").write_text(pr_output)
        script = STUB.replace("{fetch_marker}", fetch_marker) \
            .replace("{initial_state}", initial_state) \
            .replace("{closed}", closed).replace("{state_patch}", state_patch)
        support.write_cli_stub(self.bin, name, script)

    def invocations(self) -> list[list[str]]:
        log = self.bin / "invocations.log"
        return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []


GH_BODY = ("## Description\nparser crashes on empty input\n\n"
           "## Acceptance Criteria\n- [ ] returns None on empty\n")


class GithubAdapter(FakeCliHarness):
    CONFIG = {"provider": {"work_item": "github", "github_repo": "org/wi-repo"}}

    def setUp(self):
        super().setUp()
        # state seeded UPPERCASE — real gh returns "OPEN"/"CLOSED"
        # (tests/fixtures/forge/github-work_item.fetch.json); the provider
        # normalizes to lowercase so fetch agrees with transition()
        self.stub("gh",
                  {"number": 7, "title": "Fix parser", "body": GH_BODY,
                   "state": "OPEN", "labels": [{"name": "bug"}]},
                  fetch_marker="issue view", initial_state="OPEN",
                  closed="CLOSED",
                  state_patch='out["state"] = state["state"]')

    def test_contract(self):
        assert_work_item_contract(self, self.CONFIG, "7")

    def test_normalization(self):
        item = dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertEqual((item["id"], item["type"]), ("7", "Bug"))
        self.assertEqual(item["acceptance_criteria"], ["returns None on empty"])

    def test_transition_maps_to_close(self):
        dispatch(self.CONFIG, "work_item.transition", id="7", to="closed")
        self.assertIn(["issue", "close", "7", "--repo", "org/wi-repo"],
                      self.invocations())


class GitlabAdapter(FakeCliHarness):
    CONFIG = {"provider": {"work_item": "gitlab", "gitlab_repo": "org/wi-repo"}}

    def setUp(self):
        super().setUp()
        self.stub("glab",
                  {"iid": 7, "title": "Fix parser", "description": GH_BODY,
                   "state": "opened", "labels": ["bug"]},
                  fetch_marker="issue view", initial_state="opened",
                  closed="closed",
                  state_patch='out["state"] = state["state"]')

    def test_contract(self):
        assert_work_item_contract(self, self.CONFIG, "7")


class AdoAdapter(FakeCliHarness):
    CONFIG = {"provider": {"work_item": "ado"}}

    def setUp(self):
        super().setUp()
        self.stub("az",
                  {"id": 7, "fields": {
                      "System.Title": "Fix parser",
                      "System.WorkItemType": "Bug",
                      "System.State": "New",
                      "System.Description": "<div>parser crashes</div>",
                      "Microsoft.VSTS.Common.AcceptanceCriteria":
                          "<div>returns None on empty</div>"}},
                  fetch_marker="work-item show", initial_state="New",
                  closed="Closed",
                  state_patch='out["fields"]["System.State"] = state["state"]')

    def test_contract(self):
        assert_work_item_contract(self, self.CONFIG, "7")

    def test_native_fields_and_html_stripping(self):
        item = dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertEqual(item["type"], "Bug")
        self.assertEqual(item["description"], "parser crashes")
        self.assertEqual(item["acceptance_criteria"], ["returns None on empty"])


class GitProviderPrCreation(FakeCliHarness):
    KW = dict(repo=Path("."), branch="fix/7-x", base="main",
              title="fix: #7 Fix parser", work_item_id="7", summary="Fix parser")

    def test_github_pr_with_closes_emulation(self):
        self.stub("gh", {}, fetch_marker="issue view", initial_state="open",
                  closed="closed", pr_output="https://github.com/o/r/pull/9")
        pr = create_pr({"provider": {"git": "github"}}, **self.KW)
        self.assertEqual(pr["url"], "https://github.com/o/r/pull/9")
        argv = self.invocations()[-1]
        self.assertEqual(argv[:2], ["pr", "create"])
        body = argv[argv.index("--body") + 1]
        self.assertIn("Closes #7", body)               # emulated link
        self.assertEqual(argv[argv.index("--head") + 1], "fix/7-x")

    def test_gitlab_mr(self):
        self.stub("glab", {}, fetch_marker="issue view", initial_state="opened",
                  closed="closed", pr_output="https://gitlab.com/o/r/-/mr/9")
        pr = create_pr({"provider": {"git": "gitlab"}}, **self.KW)
        argv = self.invocations()[-1]
        self.assertEqual(argv[:2], ["mr", "create"])
        self.assertIn("--yes", argv)
        self.assertIn("Closes #7",
                      argv[argv.index("--description") + 1])

    def test_ado_pr_with_native_work_item_link(self):
        self.stub("az", {}, fetch_marker="work-item show", initial_state="New",
                  closed="Closed",
                  pr_output=json.dumps({"url": "https://dev.azure/pr/9"}))
        pr = create_pr({"provider": {"git": "ado"}}, **self.KW)
        argv = self.invocations()[-1]
        self.assertIn("--work-items", argv)             # native link, no emulation
        self.assertEqual(argv[argv.index("--work-items") + 1], "7")
        self.assertEqual(pr["url"], "https://dev.azure/pr/9")

    def test_github_pr_create_runs_in_the_target_repo_not_harness_cwd(self):
        # adversarial-review finding: create_github/create_gitlab never
        # passed cwd=repo to run_cli, so `gh`/`glab` resolved the remote
        # from the harness process's cwd instead of the target repo.
        self.stub("gh", {}, fetch_marker="issue view", initial_state="open",
                  closed="closed", pr_output="https://github.com/o/r/pull/9")
        real_repo = self.bin / "a-real-repo-checkout"
        real_repo.mkdir()
        create_pr({"provider": {"git": "github"}}, **{**self.KW, "repo": real_repo})
        seen_cwd = Path((self.bin / "cwd.log").read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(seen_cwd.resolve(), real_repo.resolve())

    def test_unknown_git_provider(self):
        with self.assertRaises(ProviderError):
            create_pr({"provider": {"git": "sourcehut"}}, **self.KW)

    def test_ado_mcp_pr_refuses_with_mapping_guidance(self):
        # MCP git transport can't be scripted: refuse with the create + native
        # link tools the orchestrator invokes (no `Closes #N` emulation).
        with self.assertRaises(ProviderError) as ctx:
            create_pr({"provider": {"git": "ado-mcp"}}, **self.KW)
        msg = str(ctx.exception)
        self.assertIn("mcp__azure-devops__repo_create_pull_request", msg)
        self.assertIn("mcp__azure-devops__wit_link_work_item_to_pull_request",
                      msg)
        self.assertIn("refs/heads/", msg)                # ADO branch prefix


class GitProviderCommentFetch(FakeCliHarness):
    """adversarial-review finding: no git-provider operation ever fetched PR
    comments at all — analyze-comments.md forced an improvised raw `gh pr
    view`. fetch_pr_comments closes that gap the same way create_pr does."""

    def _stub_json(self, name: str, output: str) -> None:
        script = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "base = Path(__file__).parent\n"
            "(base / 'invocations.log').open('a').write(repr(sys.argv[1:]) + '\\n')\n"
            "(base / 'cwd.log').open('a').write(str(Path.cwd()) + '\\n')\n"
            f"print({output!r})\n"
        )
        support.write_cli_stub(self.bin, name, script)

    def _last_argv(self) -> list[str]:
        import ast
        return ast.literal_eval(
            (self.bin / "invocations.log").read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_local_provider_returns_no_comments(self):
        # records-only provider, no forge to fetch from — the human pastes
        # comments instead (analyze-comments.md).
        comments = git_providers.fetch_pr_comments(
            {"provider": {"git": "local"}}, repo=Path("."),
            pr={"url": "file:///x#feature"})
        self.assertEqual(comments, [])

    def _stub_gh_branching(self, view_output: str, api_output: str) -> None:
        """gh stub answering `gh api ...` and `gh pr view ...` differently —
        fetch_comments_github makes BOTH calls (re-review finding: `pr view
        --json comments` alone misses inline diff comments, the dominant
        form of real review feedback, and review-summary bodies)."""
        script = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "base = Path(__file__).parent\n"
            "(base / 'invocations.log').open('a').write(repr(sys.argv[1:]) + '\\n')\n"
            "(base / 'cwd.log').open('a').write(str(Path.cwd()) + '\\n')\n"
            f"print({api_output!r} if sys.argv[1] == 'api' else {view_output!r})\n"
        )
        support.write_cli_stub(self.bin, "gh", script)

    def _all_argv(self) -> list[list[str]]:
        import ast
        return [ast.literal_eval(line) for line in
                (self.bin / "invocations.log").read_text(encoding="utf-8").strip().splitlines()]

    def test_github_fetch_comments_covers_all_three_feedback_surfaces(self):
        view = json.dumps({
            "comments": [
                {"author": {"login": "alice"}, "body": "please add a test"},
                {"author": {"login": "bob"}, "body": "lgtm otherwise"}],
            "reviews": [
                {"author": {"login": "carol"}, "body": "overall: split this",
                 "state": "CHANGES_REQUESTED"},
                {"author": {"login": "dan"}, "body": "", "state": "APPROVED"}]})
        api = json.dumps([
            {"user": {"login": "carol"}, "body": "this loop is O(n^2)",
             "path": "src/x.py", "line": 42}])
        self._stub_gh_branching(view, api)
        repo = self.bin / "repo"
        repo.mkdir()
        comments = git_providers.fetch_pr_comments(
            {"provider": {"git": "github"}}, repo=repo,
            pr={"url": "https://github.com/o/r/pull/9"})
        # conversation + non-blank review body + inline; dan's blank
        # APPROVED click contributes nothing
        self.assertEqual([c["id"] for c in comments], ["1", "2", "3", "4"])
        self.assertEqual(comments[0]["body"], "please add a test")
        self.assertEqual(comments[2]["review_state"], "CHANGES_REQUESTED")
        self.assertEqual(comments[3]["path"], "src/x.py")
        self.assertEqual(comments[3]["line"], 42)
        calls = self._all_argv()
        self.assertEqual(calls[0][:3], ["pr", "view", "9"])
        self.assertIn("comments,reviews", calls[0])
        self.assertEqual(calls[1][0], "api")
        self.assertIn("pulls/9/comments", calls[1][1])
        for line in (self.bin / "cwd.log").read_text(encoding="utf-8").strip().splitlines():
            self.assertEqual(Path(line).resolve(), repo.resolve())

    def test_github_fetch_comments_survives_error_shaped_api_response(self):
        # `gh api` can return a dict (error envelope) instead of the
        # expected list — the inline-comments surface must be skipped, not
        # crash the whole fetch.
        view = json.dumps({"comments": [
            {"author": {"login": "alice"}, "body": "top-level only"}],
            "reviews": []})
        api = json.dumps({"message": "Not Found",
                          "documentation_url": "https://docs.github.com"})
        self._stub_gh_branching(view, api)
        repo = self.bin / "repo2"
        repo.mkdir()
        comments = git_providers.fetch_pr_comments(
            {"provider": {"git": "github"}}, repo=repo,
            pr={"url": "https://github.com/o/r/pull/9"})
        self.assertEqual(comments, [
            {"id": "1", "author": "alice", "body": "top-level only"}])

    def test_gitlab_fetch_comments_parses_and_numbers_them(self):
        # `glab api projects/:id/merge_requests/N/notes` — the ONLY listing
        # surface glab has (adversarial-review finding: the first version
        # invented `glab mr note list`, a nonexistent subcommand, and this
        # test's stub happily echoed JSON for it, shipping the bug green).
        # System notes (GitLab stores state-change events as notes) are
        # filtered out.
        # newest-first with system notes FIRST, matching real forge order
        # (tests/fixtures/forge/gitlab-fetch-pr-comments.json — live-forge
        # finding: enumerate-before-filter gave the first human note id "4")
        self._stub_json("glab", json.dumps(
            [{"author": {"username": "bot"}, "body": "assigned to @alice",
              "system": True},
             {"author": {"username": "bot"}, "body": "changed the description",
              "system": True},
             {"author": {"username": "alice"}, "body": "split this function"}]))
        comments = git_providers.fetch_pr_comments(
            {"provider": {"git": "gitlab"}}, repo=Path("."),
            pr={"url": "https://gitlab.com/o/r/-/merge_requests/9"})
        self.assertEqual(comments,
                         [{"id": "1", "author": "alice", "body": "split this function"}])
        argv = self._last_argv()
        self.assertEqual(argv[0], "api")
        self.assertIn("projects/:id/merge_requests/9/notes", argv[1])

    def test_ado_fetch_comments_declares_unsupported(self):
        with self.assertRaises(ProviderUnsupported):
            git_providers.fetch_pr_comments(
                {"provider": {"git": "ado"}}, repo=Path("."),
                pr={"url": "https://dev.azure/pr/9"})

    def test_ado_mcp_fetch_comments_refuses_with_mapping_guidance(self):
        with self.assertRaises(ProviderError) as ctx:
            git_providers.fetch_pr_comments(
                {"provider": {"git": "ado-mcp"}}, repo=Path("."),
                pr={"url": "https://dev.azure/pr/9"})
        self.assertIn("mcp__azure-devops__repo_get_pull_request_threads",
                      str(ctx.exception))

    def test_unknown_git_provider_comment_fetch(self):
        with self.assertRaises(ProviderError):
            git_providers.fetch_pr_comments(
                {"provider": {"git": "sourcehut"}}, repo=Path("."), pr={})


class WorkItemCreateFollowUp(FakeCliHarness):
    """Security-defer follow-up (coverage B9, adversarial-review finding: no
    provider implemented work_item.create at all, and `harness provider`
    couldn't even carry a title/description). github/gitlab implement it
    (plain-text URL output, not JSON — unlike every other `gh`/`glab` op
    this codebase already wraps); everything else stays declared-unsupported."""

    def _stub_url(self, name: str, url: str) -> None:
        script = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            "(Path(__file__).parent / 'invocations.log').open('a')"
            ".write(repr(sys.argv[1:]) + '\\n')\n"
            f"print({url!r})\n"
        )
        support.write_cli_stub(self.bin, name, script)

    def _last_argv(self) -> list[str]:
        import ast
        return ast.literal_eval(
            (self.bin / "invocations.log").read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_github_create_returns_id_and_url(self):
        self._stub_url("gh", "https://github.com/o/r/issues/42")
        result = dispatch({"provider": {"work_item": "github",
                                        "github_repo": "o/r"}}, "work_item.create",
                          title="Follow up: rotate leaked token",
                          description="found by security scan, repo r, severity high")
        self.assertEqual(result, {"id": "42", "url": "https://github.com/o/r/issues/42"})
        argv = self._last_argv()
        self.assertEqual(argv[:2], ["issue", "create"])
        self.assertEqual(argv[argv.index("--title") + 1],
                         "Follow up: rotate leaked token")

    def test_gitlab_create_returns_id_and_url(self):
        self._stub_url("glab", "https://gitlab.com/o/r/-/issues/9")
        result = dispatch({"provider": {"work_item": "gitlab",
                                        "gitlab_repo": "o/r"}}, "work_item.create",
                          title="Follow up", description="details")
        self.assertEqual(result, {"id": "9", "url": "https://gitlab.com/o/r/-/issues/9"})
        argv = self._last_argv()
        self.assertEqual(argv[:2], ["issue", "create"])

    def test_ado_declares_create_unsupported(self):
        with self.assertRaises(ProviderUnsupported):
            dispatch({"provider": {"work_item": "ado"}}, "work_item.create",
                     title="x", description="y")

    def test_local_markdown_create_supported_but_needs_stories_dir(self):
        # 0.16.14: local-markdown now implements create (the security
        # gate's `defer -> work_item.create` was a declared dead end on
        # file-transport workspaces). The happy path is covered in
        # test_providers.LocalMarkdownContract; here: an unset stories_dir
        # is the same clean refusal every other local-markdown op gives.
        with self.assertRaises(ProviderError) as ctx:
            dispatch({"provider": {"work_item": "local-markdown"}}, "work_item.create",
                     title="x", description="y")
        self.assertIn("stories_dir is not configured", str(ctx.exception))


class McpAdapters(unittest.TestCase):
    def test_jira_dispatch_refuses_with_mapping_guidance(self):
        config = {"provider": {"work_item": "jira"}}
        with self.assertRaises(ProviderError) as ctx:
            dispatch(config, "work_item.fetch", id="PROJ-9")
        self.assertIn("mcp__jira__get_issue", str(ctx.exception))

    def test_jira_normalize_including_adf(self):
        config = {"provider": {"work_item": "jira"}}
        raw = {"key": "PROJ-9", "fields": {
            "summary": "Fix parser", "issuetype": {"name": "Bug"},
            "status": {"name": "To Do"},
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "parser crashes on empty"}]}]}}}
        item = normalize(config, "work_item.fetch", raw)
        self.assertEqual((item["id"], item["type"], item["state"]),
                         ("PROJ-9", "Bug", "To Do"))
        self.assertIn("parser crashes on empty", item["description"])

    def test_zoho_normalize(self):
        config = {"provider": {"work_item": "zoho"}}
        item = normalize(config, "work_item.fetch",
                         {"task": {"id": 42, "title": "Fix parser",
                                   "status": "Open", "description": "boom"}})
        self.assertEqual((item["id"], item["title"], item["state"]),
                         ("42", "Fix parser", "Open"))

    # ADO over MCP: the transport twin of the CLI `ado` provider — same
    # normalized contract, model-invoked tools.
    ADO_MCP = {"provider": {"work_item": "ado-mcp"}}
    ADO_RAW = {"id": 7, "fields": {
        "System.Title": "Fix parser", "System.WorkItemType": "Bug",
        "System.State": "New", "System.Description": "<div>parser crashes</div>",
        "Microsoft.VSTS.Common.AcceptanceCriteria":
            "<div>returns None on empty</div>"}}

    def test_ado_mcp_dispatch_refuses_with_mapping_guidance(self):
        with self.assertRaises(ProviderError) as ctx:
            dispatch(self.ADO_MCP, "work_item.fetch", id="7")
        msg = str(ctx.exception)
        self.assertIn("mcp__azure-devops__wit_get_work_item", msg)
        # fetch guidance points at the bootstrap path, not bare normalize.
        self.assertIn("fetch --from-raw", msg)

    def test_ado_mcp_non_fetch_refusal_omits_fetch_hint(self):
        # transition/add_comment are a single tool call — no bootstrap tail, so
        # they must not carry the fetch-only `--from-raw` guidance.
        with self.assertRaises(ProviderError) as ctx:
            dispatch(self.ADO_MCP, "work_item.transition", id="7", to="Active")
        msg = str(ctx.exception)
        self.assertIn("mcp__azure-devops__wit_update_work_item", msg)
        self.assertNotIn("--from-raw", msg)

    def test_ado_mcp_normalize_shares_cli_field_mapping(self):
        item = normalize(self.ADO_MCP, "work_item.fetch", self.ADO_RAW)
        # Identical to what the CLI transport yields for the same fields —
        # HTML stripped, native ADO type, id as str, ado# ref.
        self.assertEqual(item["id"], "7")
        self.assertEqual(item["type"], "Bug")
        self.assertEqual(item["state"], "New")
        self.assertEqual(item["description"], "parser crashes")
        self.assertEqual(item["acceptance_criteria"], ["returns None on empty"])
        self.assertEqual(item["provider_ref"], "ado#7")

    def test_ado_mcp_status_defaults_match_cli(self):
        from harness.providers.ado_cli import STATUS_DEFAULTS as cli_defaults
        from harness.providers import get_module
        self.assertEqual(get_module(self.ADO_MCP).STATUS_DEFAULTS, cli_defaults)


if __name__ == "__main__":
    unittest.main()
