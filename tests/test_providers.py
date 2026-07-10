"""The shared provider contract test (design.md piece 4) — every work-item
provider must pass `assert_work_item_contract`. M4 proves it with
local-markdown; M6 providers reuse the same assertions."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness.providers import ProviderUnsupported, dispatch, get_module

STORY = """# WORK-7: Fix null crash in parser
Type: Bug
Status: Open

## Description
Empty input makes the parser explode.

## Acceptance Criteria
- [ ] parser returns None on empty input
- [x] error is logged once
"""

REQUIRED_FETCH_KEYS = {"id", "title", "type", "state", "description",
                       "acceptance_criteria", "provider_ref"}


def assert_work_item_contract(tc: unittest.TestCase, config: dict, item_id: str):
    """The contract every provider must satisfy, transport-independent."""
    item = dispatch(config, "work_item.fetch", id=item_id)
    tc.assertTrue(REQUIRED_FETCH_KEYS.issubset(item),
                  f"missing keys: {REQUIRED_FETCH_KEYS - set(item)}")
    tc.assertIsInstance(item["acceptance_criteria"], list)
    tc.assertTrue(item["title"])

    # The provider may PROJECT the requested state (emulation hides inside
    # the adapter — binary-state forges collapse richer states). The contract
    # is self-consistency: transition returns the actual resulting state, and
    # a subsequent fetch agrees with it (persistence).
    moved = dispatch(config, "work_item.transition", id=item_id, to="In Progress")
    tc.assertTrue(moved["state"])
    tc.assertEqual(dispatch(config, "work_item.fetch", id=item_id)["state"],
                   moved["state"])

    dispatch(config, "work_item.add_comment", id=item_id, text="round 1 done")

    mod = get_module(config)
    tc.assertEqual(sorted(mod.SUPPORTS), sorted(mod.OPS))
    with tc.assertRaises(ProviderUnsupported):
        dispatch(config, "work_item.list_changelog", id=item_id)


class LocalMarkdownContract(unittest.TestCase):
    def setUp(self):
        self.stories = Path(tempfile.mkdtemp())
        (self.stories / "WORK-7.md").write_text(STORY, encoding="utf-8")
        self.config = {"provider": {"work_item": "local-markdown",
                                    "stories_dir": str(self.stories)}}

    def tearDown(self):
        shutil.rmtree(self.stories)

    def test_passes_the_shared_contract(self):
        assert_work_item_contract(self, self.config, "WORK-7")

    def test_normalization_details(self):
        item = dispatch(self.config, "work_item.fetch", id="WORK-7")
        self.assertEqual(item["id"], "WORK-7")
        self.assertEqual(item["title"], "Fix null crash in parser")
        self.assertEqual(item["type"], "Bug")
        self.assertEqual(item["acceptance_criteria"],
                         ["parser returns None on empty input",
                          "error is logged once"])

    def test_comment_lands_in_file(self):
        dispatch(self.config, "work_item.add_comment", id="WORK-7", text="hello")
        body = (self.stories / "WORK-7.md").read_text()
        self.assertIn("## Comments", body)
        self.assertIn("- hello", body)

    def test_missing_item_is_clean_error(self):
        from harness.providers import ProviderError
        with self.assertRaises(ProviderError):
            dispatch(self.config, "work_item.fetch", id="NOPE-1")

    def test_create_writes_a_fetchable_follow_up(self):
        # B9: the security gate's `defer` disposition runs work_item.create
        # — previously a declared dead end on local-markdown (only the
        # github/gitlab providers implemented it)
        out = dispatch(self.config, "work_item.create",
                       title="Rotate the demo token",
                       description="Deferred from approve-security.")
        self.assertEqual(out["id"], "FU-1")
        item = dispatch(self.config, "work_item.fetch", id="FU-1")
        self.assertEqual(item["title"], "Rotate the demo token")
        self.assertEqual(item["state"], "Open")
        self.assertIn("Deferred from approve-security", item["description"])
        # ids increment past existing follow-ups, never collide
        self.assertEqual(dispatch(self.config, "work_item.create",
                                  title="second")["id"], "FU-2")

    def test_traversal_id_refused_before_any_io(self):
        # adversarial-review finding: `--id ../../x` resolved OUTSIDE
        # stories_dir, and transition/add_comment then WROTE there —
        # silent wrong-file I/O, not an error.
        from harness.providers import ProviderError
        outside = self.stories.parent / "outside.md"
        outside.write_text("# X-1: outside\n")
        rel = f"../{outside.stem}"
        for op, kwargs in (("work_item.fetch", {}),
                           ("work_item.transition", {"to": "Done"}),
                           ("work_item.add_comment", {"text": "hi"})):
            with self.assertRaises(ProviderError) as ctx:
                dispatch(self.config, op, id=rel, **kwargs)
            self.assertIn("escapes stories_dir", str(ctx.exception))
        self.assertNotIn("Done", outside.read_text())   # never touched

    def test_unset_stories_dir_is_a_refusal_not_cwd_hunting(self):
        from harness.providers import ProviderError
        config = {"provider": {"work_item": "local-markdown"}}
        with self.assertRaises(ProviderError) as ctx:
            dispatch(config, "work_item.fetch", id="WORK-7")
        self.assertIn("stories_dir is not configured", str(ctx.exception))


V21_STORY = """# US-042 — Add multiply support

> Status: 🔧 In Progress — 2026-06-01

## Description

calc needs multiply.

## Acceptance Criteria

- [ ] multiply(a, b) returns a * b
"""


class LocalMarkdownV21Adoption(unittest.TestCase):
    """Adopted v2.1 stories (see /migrate-workspace): status lives in a
    `> Status:` blockquote and the H1 carries no `ID:` prefix. Read must
    tolerate both — a strict match read every migrated done-story as
    "Open" and re-offered it — while write-back upgrades the file to the
    strict v3.0 `Status:` form."""

    def setUp(self):
        self.stories = Path(tempfile.mkdtemp())
        (self.stories / "US-042-add-multiply.md").write_text(
            V21_STORY, encoding="utf-8")
        self.config = {"provider": {"work_item": "local-markdown",
                                    "stories_dir": str(self.stories)}}

    def tearDown(self):
        shutil.rmtree(self.stories)

    def test_blockquote_status_is_read_not_defaulted(self):
        item = dispatch(self.config, "work_item.fetch",
                        id="US-042-add-multiply")
        self.assertIn("In Progress", item["state"])
        # no `ID:` prefix in the H1 -> filename stem, em-dash title intact
        self.assertEqual(item["id"], "US-042-add-multiply")
        self.assertIn("multiply", item["title"])
        self.assertEqual(item["acceptance_criteria"],
                         ["multiply(a, b) returns a * b"])

    def test_transition_upgrades_to_strict_v3_form(self):
        dispatch(self.config, "work_item.transition",
                 id="US-042-add-multiply", to="In Review")
        body = (self.stories / "US-042-add-multiply.md").read_text()
        self.assertIn("Status: In Review", body)
        self.assertNotIn("> Status", body)   # blockquote form is gone
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="US-042-add-multiply")["state"],
                         "In Review")

    def test_bolded_blockquote_status_reads(self):
        # `> **Status**: ...` is the same v2.1 drift one spelling over —
        # the tolerance must match it or migrated done-stories read "Open"
        (self.stories / "US-043.md").write_text(
            "# US-043 — Bold status\n\n> **Status**: ✅ Done — 2026-05-01\n\n"
            "## Description\nd\n", encoding="utf-8")
        item = dispatch(self.config, "work_item.fetch", id="US-043")
        self.assertIn("Done", item["state"])

    def test_colon_inside_bold_status_reads_clean(self):
        # `**Status:** Done` — the colon inside the bold — used to parse
        # state as "** Done" (re-verification finding)
        (self.stories / "US-045.md").write_text(
            "# US-045 — Colon in bold\n\n> **Status:** ✅ Done\n\n"
            "## Description\nd\n", encoding="utf-8")
        item = dispatch(self.config, "work_item.fetch", id="US-045")
        self.assertEqual(item["state"], "✅ Done")

    def test_quoted_status_in_the_body_is_prose_not_state(self):
        # adversarial-review finding: the whole-file scan read a quoted
        # `> Status:` inside Description as the item state and REWROTE it
        (self.stories / "US-044.md").write_text(
            "# US-044 — Quoted prose\n\n"
            "## Description\n\n> Status: everything was on fire\n\n"
            "## Acceptance Criteria\n- [ ] x\n", encoding="utf-8")
        item = dispatch(self.config, "work_item.fetch", id="US-044")
        self.assertEqual(item["state"], "Open")     # absent -> defaulted
        dispatch(self.config, "work_item.transition", id="US-044",
                 to="In Progress")
        body = (self.stories / "US-044.md").read_text()
        self.assertIn("> Status: everything was on fire", body)  # untouched
        # the new Status landed in the HEADER, where the read can see it
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="US-044")["state"], "In Progress")


class JiraAcField(unittest.TestCase):
    RAW = {"key": "PROJ-9",
           "fields": {"summary": "Fix it",
                      "issuetype": {"name": "Bug"},
                      "status": {"name": "Open"},
                      "description": {"type": "doc", "content": [
                          {"type": "paragraph", "content": [
                              {"type": "text",
                               "text": "- [ ] heuristic AC from description"}]}]},
                      "customfield_10442": {"type": "doc", "content": [
                          {"type": "paragraph", "content": [
                              {"type": "text", "text": "AC from field"}]}]}}}

    def test_configured_ac_field_used_and_adf_flattened(self):
        # adversarial-review finding: the hardcoded `customfield_ac` matches
        # no real Jira instance (real ids are customfield_NNNNN), so AC
        # extraction always fell back to description heuristics; a dict
        # (ADF) field value was also passed through unflattened.
        from harness.providers import normalize
        config = {"provider": {"work_item": "jira",
                               "jira_ac_field": "customfield_10442"}}
        item = normalize(config, "work_item.fetch", self.RAW)
        self.assertEqual([a.strip() for a in item["acceptance_criteria"]],
                         ["AC from field"])

    def test_unconfigured_falls_back_to_description_heuristics(self):
        from harness.providers import normalize
        config = {"provider": {"work_item": "jira"}}
        item = normalize(config, "work_item.fetch", self.RAW)
        self.assertEqual([a.strip() for a in item["acceptance_criteria"]],
                         ["heuristic AC from description"])


if __name__ == "__main__":
    unittest.main()
