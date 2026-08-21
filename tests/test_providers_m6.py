"""M6 done-criteria: contract suite green per adapter (fixture-driven fake
CLIs — stateful stubs recording exact argv), PR creation per git provider
(argv + link-emulation asserted), MCP normalize round-trips. Live-forge
verification (real PR) is a user-run step: `gh/glab/az auth` + one fetch."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
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


# `gh project` needs its own stub: the shared STUB above dispatches on
# `issue view`/close/comment, and the Projects surface is a different verb
# set entirely (item-list / field-list / view / item-edit / item-create),
# with the board's single-select Status field as the mutable state. Shapes
# match a live `gh` 2.98 capture (tests/fixtures/forge/github-projects-*).
# `totalCount` is the board's REAL size and the page is truncated to
# --limit, because the gap between them is the truncation signal the
# adapter reports; `broken` corrupts one verb's stdout on demand.
PROJECTS_STUB = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
base = Path(__file__).parent
args = sys.argv[1:]
(base / "invocations.log").open("a").write(json.dumps(args) + "\n")
board = json.loads((base / "board.json").read_text(encoding="utf-8"))
state_file = base / "state.json"
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
fields, items = board["fields"], board["items"]
verb = " ".join(args[:2])
if verb in board.get("broken", []):
    print("<!DOCTYPE html> not json at all")
    sys.exit(0)
if verb in board.get("raw", {}):
    print(json.dumps(board["raw"][verb]))
    sys.exit(0)


def option_name(oid):
    for f in fields:
        for o in f.get("options", []):
            if o["id"] == oid:
                return o["name"]
    return ""


def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default


if verb == "project item-list":
    out = []
    for item in items:
        item = dict(item)
        if item["id"] in state:
            # write the value back under whichever key this board uses for
            # its status field, so a renamed field round-trips too
            item[board.get("status_key", "status")] = state[item["id"]]
        out.append(item)
    print(json.dumps({"items": out[:int(flag("--limit", "30"))],
                      "totalCount": len(items)}))
elif verb == "project field-list":
    print(json.dumps({"fields": fields, "totalCount": len(fields)}))
elif verb == "project view":
    print(json.dumps({"id": board["id"], "number": board["number"]}))
elif verb == "project item-edit":
    state[flag("--id")] = option_name(flag("--single-select-option-id"))
    state_file.write_text(json.dumps(state))
    print("{}")
elif verb == "project item-create":
    print(json.dumps({"id": "PVTI_new_draft", "title": flag("--title"),
                      "body": flag("--body")}))
elif verb == "issue comment":
    print("https://github.com/org/wi-repo/issues/7#issuecomment-1")
else:
    sys.stderr.write("unexpected argv: " + " ".join(args) + "\n")
    sys.exit(2)
'''

STATUS_FIELD = {"id": "PVTSSF_status", "name": "Status",
                "type": "ProjectV2SingleSelectField",
                "options": [{"id": "opt_todo", "name": "Todo"},
                            {"id": "opt_prog", "name": "In Progress"},
                            {"id": "opt_review", "name": "Review"},
                            {"id": "opt_done", "name": "Done"}]}

ISSUE_ITEM = {"id": "PVTI_issue7", "title": "Fix parser", "status": "Todo",
              "labels": ["bug"],
              "content": {"body": GH_BODY, "number": 7,
                          "repository": "org/wi-repo", "title": "Fix parser",
                          "type": "Issue",
                          "url": "https://github.com/org/wi-repo/issues/7"}}

DRAFT_ITEM = {"id": "PVTI_draft1", "title": "Rotate the signing key",
              "status": "Todo",
              "content": {"body": "## Description\nrotate it\n",
                          "title": "Rotate the signing key",
                          "type": "DraftIssue"}}

BOARD = {"id": "PVT_board", "number": 4,
         "fields": [{"id": "PVTF_title", "name": "Title",
                     "type": "ProjectV2Field"}, STATUS_FIELD],
         "items": [ISSUE_ITEM, DRAFT_ITEM]}


class ProjectsBoardHarness(FakeCliHarness):
    """Shared board plumbing for the two id regimes below."""

    CONFIG: dict = {}

    def setUp(self):
        super().setUp()
        self.install(BOARD)

    def install(self, board: dict):
        (self.bin / "board.json").write_text(json.dumps(board),
                                             encoding="utf-8")
        support.write_cli_stub(self.bin, "gh", PROJECTS_STUB)

    def board(self, **over) -> dict:
        board = json.loads(json.dumps(BOARD))
        board.update(over)
        return board

    def config(self, **provider):
        cfg = json.loads(json.dumps(self.CONFIG))
        cfg["provider"].update(provider)
        return cfg


class GithubProjectsAdapter(ProjectsBoardHarness):
    """The board-transport sibling of GithubAdapter: milestones land in the
    Status single-select field, not in open/closed. `github_project_repo`
    is set here — the recommended shape, where ids stay bare numbers."""

    CONFIG = {"provider": {"work_item": "github-projects",
                           "github_project": 4,
                           "github_project_owner": "acme",
                           "github_project_repo": "org/wi-repo"}}

    def test_contract(self):
        assert_work_item_contract(self, self.CONFIG, "7")

    def test_normalization_reads_the_board_column_as_state(self):
        item = dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertEqual((item["id"], item["type"], item["state"]),
                         ("7", "Bug", "Todo"))
        self.assertEqual(item["acceptance_criteria"], ["returns None on empty"])
        self.assertEqual(item["provider_ref"], "github-projects:org/wi-repo#7")

    def test_fetch_targets_the_configured_board_explicitly(self):
        dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertIn(["project", "item-list", "4", "--owner", "acme",
                       "--format", "json", "--limit", "200"],
                      self.invocations())

    def test_transition_writes_the_single_select_option(self):
        moved = dispatch(self.CONFIG, "work_item.transition", id="7", to="Done")
        self.assertEqual(moved["state"], "Done")
        edit = [a for a in self.invocations()
                if a[:2] == ["project", "item-edit"]]
        self.assertEqual(len(edit), 1)
        argv = edit[0]
        # node ids throughout — the form that also works for draft items
        self.assertEqual(argv[argv.index("--single-select-option-id") + 1],
                         "opt_done")
        self.assertEqual(argv[argv.index("--id") + 1], "PVTI_issue7")
        self.assertEqual(argv[argv.index("--project-id") + 1], "PVT_board")
        self.assertEqual(argv[argv.index("--field-id") + 1], "PVTSSF_status")

    def test_every_op_round_trips_the_id_that_fetch_handed_back(self):
        # adversarial-review, converged finding: the id contract is only
        # honoured if the id fetch RETURNS keeps resolving. Feeding it back
        # in is the assertion the original tests never made.
        for spelling in ("7", "org/wi-repo#7", "PVTI_issue7",
                         "https://github.com/org/wi-repo/issues/7"):
            got = dispatch(self.CONFIG, "work_item.fetch", id=spelling)["id"]
            self.assertEqual(got, "7", spelling)
            self.assertEqual(dispatch(self.CONFIG, "work_item.transition",
                                      id=got, to="Done")["id"], "7")
            dispatch(self.CONFIG, "work_item.add_comment", id=got, text="ok")

    def test_a_pinned_repo_keeps_a_colliding_number_unambiguous(self):
        # the same cross-repo board that refuses below resolves cleanly
        # once the workspace declares whose numbering its ids use
        self.install(self.board(items=BOARD["items"] + [_other_repo_issue()]))
        item = dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertEqual((item["id"], item["provider_ref"]),
                         ("7", "github-projects:org/wi-repo#7"))
        # …and the other repo's item is still addressable, qualified
        other = dispatch(self.CONFIG, "work_item.fetch", id="org/other-repo#7")
        self.assertEqual(other["id"], "org/other-repo#7")

    def test_in_review_degrades_through_the_in_progress_chain(self):
        # GitHub's stock template ships Todo/In Progress/Done — an
        # `in-review` write-back must not fail on every default board
        moved = dispatch(self.CONFIG, "work_item.transition", id="7",
                         to="In Review")
        self.assertEqual(moved["state"], "Review")   # alias, not "In Review"
        stock = self.board()
        stock["fields"][1]["options"] = [
            o for o in STATUS_FIELD["options"] if o["name"] != "Review"]
        self.install(stock)
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition", id="7",
                                  to="In Review")["state"], "In Progress")
        # a board that renamed in-progress to "Doing" must not strand
        # in-review either — the two chains have to agree about one board
        doing = self.board()
        doing["fields"][1]["options"] = [{"id": "opt_todo", "name": "Todo"},
                                         {"id": "opt_doing", "name": "Doing"},
                                         {"id": "opt_done", "name": "Done"}]
        self.install(doing)
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition", id="7",
                                  to="In Progress")["state"], "Doing")
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition", id="7",
                                  to="In Review")["state"], "Doing")

    def test_a_non_latin_board_matches_by_name_not_by_position(self):
        # adversarial-review, lens B: an ASCII-only comparison key reduced
        # every Cyrillic option to "", so `done` matched the FIRST option
        # and finished items were silently parked in To Do
        cyrillic = self.board()
        cyrillic["fields"][1]["options"] = [
            {"id": "opt_todo", "name": "К выполнению"},
            {"id": "opt_prog", "name": "В работе"},
            {"id": "opt_done", "name": "Готово"}]
        self.install(cyrillic)
        moved = dispatch(self.CONFIG, "work_item.transition", id="7",
                         to="Готово")
        self.assertEqual(moved["state"], "Готово")
        argv = [a for a in self.invocations()
                if a[:2] == ["project", "item-edit"]][0]
        self.assertEqual(argv[argv.index("--single-select-option-id") + 1],
                         "opt_done")
        # a name with no match is still an honest error, never option #1
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.transition", id="7", to="Done")
        self.assertIn("Готово", str(caught.exception))

    def test_accents_fold_the_way_punctuation_does(self):
        accented = self.board()
        accented["fields"][1]["options"] = [{"id": "opt_done",
                                             "name": "Terminé"}]
        self.install(accented)
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition", id="7",
                                  to="Termine")["state"], "Terminé")

    def test_an_unmatchable_status_names_the_boards_real_options(self):
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.transition", id="7", to="Shipped")
        self.assertIn("Todo, In Progress, Review, Done", str(caught.exception))
        self.assertIn("status_mapping", str(caught.exception))

    def test_a_pull_request_on_the_board_is_never_a_work_item(self):
        # boards carry PRs beside issues; matching on number alone let one
        # be planned and built as if it were the work item
        pr = {"id": "PVTI_pr9", "title": "Fix parser (PR)", "status": "Todo",
              "content": {"body": "", "number": 9,
                          "repository": "org/wi-repo", "type": "PullRequest",
                          "url": "https://github.com/org/wi-repo/pull/9"}}
        self.install(self.board(items=BOARD["items"] + [pr]))
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.fetch", id="9")
        self.assertIn("not found", str(caught.exception))
        # addressed explicitly, it is NAMED rather than silently absent
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.fetch", id="PVTI_pr9")
        self.assertIn("pull request", str(caught.exception))
        # …and it cannot make a real issue read as ambiguous either
        cross = _other_repo_issue()
        cross["content"]["number"] = 9
        cross["content"]["url"] = "https://github.com/org/other-repo/issues/9"
        self.install(self.board(items=BOARD["items"] + [pr, cross]))
        self.assertEqual(dispatch(self.config(github_project_repo=""),
                                  "work_item.fetch", id="9")["provider_ref"],
                         "github-projects:org/other-repo#9")

    def test_a_draft_item_answers_to_its_node_id_and_has_no_comments(self):
        item = dispatch(self.CONFIG, "work_item.fetch", id="PVTI_draft1")
        self.assertEqual(item["id"], "PVTI_draft1")
        self.assertEqual(item["description"].strip(), "rotate it")
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.add_comment", id="PVTI_draft1",
                     text="hi")
        self.assertIn("draft", str(caught.exception))
        # the draft is still transitionable — the board field is the item's
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition",
                                  id="PVTI_draft1", to="Done")["state"], "Done")

    def test_not_found_distinguishes_truncation_from_absence(self):
        # there is no server-side get-one-item call, so "not found" is only
        # ever "not found in the window we could see" — and telling a user
        # to raise a limit that was never the problem is a wrong remedy
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.config(github_project_limit=1), "work_item.fetch",
                     id="PVTI_draft1")
        self.assertIn("first 1 of 2 items", str(caught.exception))
        self.assertIn("github_project_limit", str(caught.exception))
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.fetch", id="PVTI_nope")
        self.assertIn("all 2 of the board's items", str(caught.exception))
        self.assertIn("ARCHIVED", str(caught.exception))
        self.assertNotIn("raise provider.github_project_limit",
                         str(caught.exception))

    def test_a_nonsense_limit_is_refused_before_gh_runs(self):
        # both bounds: the not-found remediation says to RAISE the limit,
        # so a value gh would reject has to fail here with our words
        for bad in ("many", 0, -3, 100000):
            with self.assertRaises(ProviderError):
                dispatch(self.config(github_project_limit=bad),
                         "work_item.fetch", id="7")
        self.assertEqual(self.invocations(), [])

    def test_unset_board_config_fails_closed_naming_both_keys(self):
        with self.assertRaises(ProviderError) as caught:
            dispatch({"provider": {"work_item": "github-projects"}},
                     "work_item.fetch", id="7")
        self.assertIn("github_project", str(caught.exception))
        self.assertIn("github_project_owner", str(caught.exception))
        self.assertEqual(self.invocations(), [])      # never reached gh

    def test_a_renamed_status_field_is_matched_by_shape_not_spelling(self):
        # `gh` keys field values by display name and we must not depend on
        # whether it lowercases or camelCases a multi-word one
        renamed = self.board(status_key="workflowState")
        renamed["fields"][1]["name"] = "Workflow State"
        renamed["items"][0].pop("status")
        renamed["items"][0]["workflowState"] = "Todo"
        self.install(renamed)
        cfg = self.config(github_project_status_field="workflow state")
        self.assertEqual(dispatch(cfg, "work_item.fetch", id="7")["state"],
                         "Todo")
        self.assertEqual(dispatch(cfg, "work_item.transition", id="7",
                                  to="Done")["state"], "Done")

    def test_a_missing_status_field_names_the_fields_that_do_exist(self):
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.config(github_project_status_field="Stage"),
                     "work_item.transition", id="7", to="Done")
        self.assertIn("Title, Status", str(caught.exception))

    def test_a_status_field_that_is_not_single_select_is_refused(self):
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.config(github_project_status_field="Title"),
                     "work_item.transition", id="7", to="Done")
        self.assertIn("single-select", str(caught.exception))

    def test_an_item_with_no_status_value_reads_as_empty_not_as_an_error(self):
        # real `gh` emits a field key only when the field has a value, so a
        # freshly created item has none at all
        blank = self.board()
        blank["items"][0].pop("status")
        self.install(blank)
        self.assertEqual(dispatch(self.CONFIG, "work_item.fetch",
                                  id="7")["state"], "")

    def test_malformed_gh_output_stays_inside_the_provider_error_contract(self):
        # every one of these runs on the best-effort write-back path, which
        # catches ProviderError ONLY — a JSONDecodeError escaping it aborts
        # a step whose work already landed (adversarial-review, lens A)
        for verb, op, kwargs in (
                ("project item-list", "work_item.fetch", {"id": "7"}),
                ("project field-list", "work_item.transition",
                 {"id": "7", "to": "Done"}),
                ("project view", "work_item.transition",
                 {"id": "7", "to": "Done"}),
                ("project item-create", "work_item.create",
                 {"title": "t", "description": "d"})):
            self.install(self.board(broken=[verb]))
            with self.assertRaises(ProviderError, msg=verb) as caught:
                dispatch(self.CONFIG, op, **kwargs)
            self.assertIn("not JSON", str(caught.exception))
            self.assertIn(verb, str(caught.exception))

    def test_wrong_shaped_json_is_refused_not_a_bare_type_error(self):
        # `_json` closed the not-JSON hole; well-formed JSON of the WRONG
        # SHAPE still escaped as a TypeError from iterating a non-list, and
        # `_resolve_option` runs on the ProviderError-only write-back path
        # (re-verification, finding B).
        cases = [
            ({"project item-list": {"items": 5, "totalCount": 5}},
             "work_item.fetch", {"id": "7"}),
            ({"project field-list": {"fields": 5}},
             "work_item.transition", {"id": "7", "to": "Done"}),
        ]
        for raw, op, kwargs in cases:
            self.install(self.board(raw=raw))
            with self.assertRaises(ProviderError, msg=str(raw)):
                dispatch(self.CONFIG, op, **kwargs)
        # …and the same for a non-list nested one call deeper
        self.install(self.board(raw={"project field-list": {
            "fields": [{"id": "f", "name": "Status", "options": 5}]}}))
        with self.assertRaises(ProviderError):
            dispatch(self.CONFIG, "work_item.transition", id="7", to="Done")
        item = json.loads(json.dumps(ISSUE_ITEM))
        item["labels"] = 5
        self.install(self.board(raw={"project item-list": {
            "items": [item], "totalCount": 1}}))
        with self.assertRaises(ProviderError):
            dispatch(self.CONFIG, "work_item.fetch", id="7")

    def test_two_options_that_fold_together_are_refused_not_ordered(self):
        # the name match is deliberately tolerant, so distinct columns can
        # still fold together — taking board order would be the silent
        # wrong-column write the Unicode fix removed
        folding = self.board()
        folding["fields"][1]["options"] = [{"id": "opt_a", "name": "Stage ①"},
                                           {"id": "opt_b", "name": "Stage 1"}]
        self.install(folding)
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.transition", id="7",
                     to="Stage 1")
        self.assertIn("more than one", str(caught.exception))
        self.assertIn("Stage ①, Stage 1", str(caught.exception))

    def test_a_malformed_field_list_member_is_refused_not_a_traceback(self):
        for fields in ([STATUS_FIELD, "not-a-field"],
                       [{"name": "Status", "type": "ProjectV2SingleSelectField",
                         "options": [{"id": "o", "name": "Done"}]}]):
            self.install(self.board(fields=fields))
            try:
                dispatch(self.CONFIG, "work_item.transition", id="7", to="Done")
            except ProviderError:
                pass          # the contract: refused, with our words

    def test_comments_route_to_the_backing_issue(self):
        dispatch(self.CONFIG, "work_item.add_comment", id="7", text="round 1")
        self.assertIn(["issue", "comment", "7", "--repo", "org/wi-repo",
                       "--body", "round 1"], self.invocations())

    def test_create_makes_a_draft_item_on_the_board(self):
        made = dispatch(self.CONFIG, "work_item.create", title="Rotate key",
                        description="finding: hardcoded token")
        self.assertEqual(made["id"], "PVTI_new_draft")
        self.assertIn(["project", "item-create", "4", "--owner", "acme",
                       "--title", "Rotate key", "--body",
                       "finding: hardcoded token", "--format", "json"],
                      self.invocations())


def _other_repo_issue() -> dict:
    other = json.loads(json.dumps(ISSUE_ITEM))
    other["id"] = "PVTI_other7"
    other["content"]["repository"] = "org/other-repo"
    other["content"]["url"] = "https://github.com/org/other-repo/issues/7"
    return other


class GithubProjectsUnpinnedBoard(ProjectsBoardHarness):
    """No `github_project_repo`. A bare number is then not guaranteed to
    mean one item, so the adapter emits the QUALIFIED id instead — the
    converged adversarial-review finding: emitting a bare number here made
    the only fetchable spelling hand back an id that refused every
    subsequent op for the life of the run."""

    CONFIG = {"provider": {"work_item": "github-projects",
                           "github_project": 4,
                           "github_project_owner": "acme"}}

    def test_contract(self):
        assert_work_item_contract(self, self.CONFIG, "7")

    def test_the_emitted_id_is_qualified_and_round_trips(self):
        self.install(self.board(items=BOARD["items"] + [_other_repo_issue()]))
        got = dispatch(self.CONFIG, "work_item.fetch",
                       id="org/other-repo#7")["id"]
        self.assertEqual(got, "org/other-repo#7")
        # the spelling handed back must keep working for every other op
        self.assertEqual(dispatch(self.CONFIG, "work_item.transition", id=got,
                                  to="Done"), {"id": got, "state": "Done"})
        dispatch(self.CONFIG, "work_item.add_comment", id=got, text="round 1")
        self.assertIn(["issue", "comment", "7", "--repo", "org/other-repo",
                       "--body", "round 1"], self.invocations())
        self.assertEqual(dispatch(self.CONFIG, "work_item.fetch",
                                  id=got)["state"], "Done")

    def test_two_items_sharing_a_number_get_distinct_ids(self):
        # they are different work items; one id for both collided on the
        # run directory and the bootstrap lock
        self.install(self.board(items=BOARD["items"] + [_other_repo_issue()]))
        ids = {dispatch(self.CONFIG, "work_item.fetch", id=ref)["id"]
               for ref in ("org/wi-repo#7", "org/other-repo#7")}
        self.assertEqual(ids, {"org/wi-repo#7", "org/other-repo#7"})

    def test_a_bare_ambiguous_number_names_the_config_that_fixes_it(self):
        self.install(self.board(items=BOARD["items"] + [_other_repo_issue()]))
        with self.assertRaises(ProviderError) as caught:
            dispatch(self.CONFIG, "work_item.fetch", id="7")
        self.assertIn("ambiguous", str(caught.exception))
        self.assertIn("org/other-repo#7", str(caught.exception))
        self.assertIn("github_project_repo", str(caught.exception))


class ProviderErrorContract(unittest.TestCase):
    """`run_cli` is every CLI provider's single external seam, and the
    best-effort write-back path catches ProviderError alone."""

    def test_a_hung_cli_is_a_provider_error_not_a_subprocess_error(self):
        from harness.providers import _normalize

        def hang(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=120)

        with mock.patch.object(_normalize.subprocess, "run", hang):
            with self.assertRaises(ProviderError) as caught:
                _normalize.run_cli(["gh", "project", "item-list", "4"])
        self.assertIn("no response within 120s", str(caught.exception))

    def test_a_qualified_work_item_id_is_not_double_hashed(self):
        # `Closes #owner/repo#7` matches nothing; the bare `Closes #7` it
        # replaces resolved against the CODE repo, closing an unrelated
        # issue there (adversarial-review, both lenses)
        self.assertIn("Closes org/wi-repo#7",
                      git_providers._pr_body("org/wi-repo#7", "s", "closes"))
        self.assertIn("Closes #7", git_providers._pr_body("7", "s", "closes"))


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


GLAB_404_STUB = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
base = Path(__file__).parent
args = sys.argv[1:]
(base / "invocations.log").open("a").write(json.dumps(args) + "\n")
if args[:2] == ["mr", "create"]:
    sys.stderr.write("GET https://gitlab/api/v4/projects/g%2Fsub%2Fproj: 404 "
                     "{message: 404 Project Not Found}\n")
    sys.exit(1)
if args[:2] == ["api", "--method"]:
    print(json.dumps({"web_url": "https://gitlab/g/sub/proj/-/merge_requests/12"}))
    sys.exit(0)
if args[0] == "api":
    print(json.dumps([{"id": 4, "path_with_namespace": "other/proj"},
                      {"id": 77, "path_with_namespace": "g/sub/proj"}]))
    sys.exit(0)
sys.exit(1)
'''


class GitlabNumericIdFallback(FakeCliHarness):
    """field: dual-run comparison — both runs ended with
    `pr-recorded-manually` twice. The instance 404s PATH-encoded project
    lookups while numeric-id access works, so `glab mr create` (which
    resolves by path) could never create the MR and the manual hatch was the
    only route."""

    def _repo(self, url="git@gitlab:g/sub/proj.git") -> Path:
        from harness import gitops
        repo = self.bin / "checkout"
        repo.mkdir()
        gitops.run_git(self.bin, "init", "-b", "main", "checkout")
        gitops.run_git(repo, "remote", "add", "origin", url)
        return repo

    KW = dict(branch="fix/7-x", base="main", title="Fix",
              work_item_id="7", summary="s")

    def test_falls_back_to_the_numeric_project_id(self):
        support.write_cli_stub(self.bin, "glab", GLAB_404_STUB)
        pr = create_pr({"provider": {"git": "gitlab"}},
                       repo=self._repo(), **self.KW)
        self.assertEqual(pr["url"],
                         "https://gitlab/g/sub/proj/-/merge_requests/12")
        self.assertEqual(pr["resolved_by"], "numeric-project-id")
        post = self.invocations()[-1]
        # resolved by EXACT path match, never "first search hit"
        self.assertIn("projects/77/merge_requests", post)
        self.assertIn("source_branch=fix/7-x", post)
        self.assertIn("target_branch=main", post)

    def test_non_resolution_failures_still_surface(self):
        # a validation error (branch missing, MR already open, no permission)
        # must raise as itself — retrying it through a second transport
        # would only produce a second, more confusing failure
        support.write_cli_stub(self.bin, "glab", r'''#!/usr/bin/env python3
import sys
sys.stderr.write("branch 'fix/7-x' does not exist\n")
sys.exit(1)
''')
        with self.assertRaises(ProviderError) as ctx:
            create_pr({"provider": {"git": "gitlab"}},
                      repo=self._repo(), **self.KW)
        self.assertIn("does not exist", str(ctx.exception))

    def test_remote_url_spellings_all_yield_the_project_path(self):
        for url, expected in (
                ("git@gitlab.com:g/sub/proj.git", "g/sub/proj"),
                ("https://gitlab.com/g/sub/proj.git", "g/sub/proj"),
                ("https://user@gitlab.com/g/proj.git", "g/proj"),
                ("ssh://git@gitlab.com:2222/g/proj.git", "g/proj"),
                ("https://gitlab.com/g/proj", "g/proj")):
            repo = self._repo(url)
            self.assertEqual(git_providers._remote_project_path(repo), expected,
                             f"for {url}")
            support.rmtree(repo)


if __name__ == "__main__":
    unittest.main()
