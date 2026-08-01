"""The shared provider contract test (design.md piece 4) — every work-item
provider must pass `assert_work_item_contract`. M4 proves it with
local-markdown; M6 providers reuse the same assertions."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness.providers import ProviderUnsupported, dispatch, get_module
from tests import support

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
        support.rmtree(self.stories)

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
        body = (self.stories / "WORK-7.md").read_text(encoding="utf-8")
        self.assertIn("## Comments", body)
        self.assertIn("- hello", body)

    def test_missing_item_is_clean_error(self):
        from harness.providers import ProviderError
        with self.assertRaises(ProviderError):
            dispatch(self.config, "work_item.fetch", id="NOPE-1")

    def test_slug_suffixed_filename_resolves_by_its_short_h1_id(self):
        # field: US-CHAT-00 run. The round trip that used to break: a story
        # file named with a descriptive slug suffix is fetched by its FULL
        # filename, hands back the SHORT id from its H1 — and that short id
        # is what lands in state and what write_back replays into
        # transition(). Every op must accept both spellings, or the id that
        # resolved going in stops resolving coming back.
        (self.stories / "US-CHAT-00-frontend-test-infrastructure.md").write_text(
            "# US-CHAT-00: Frontend test infrastructure\nType: Story\n"
            "Status: Open\n\n## Description\nStand up vitest.\n\n"
            "## Acceptance Criteria\n- [ ] suite runs green\n", encoding="utf-8")
        item = dispatch(self.config, "work_item.fetch",
                        id="US-CHAT-00-frontend-test-infrastructure")
        self.assertEqual(item["id"], "US-CHAT-00")          # the short form
        self.assertTrue(item["provider_ref"].endswith(
            "US-CHAT-00-frontend-test-infrastructure.md"))  # the long one
        # the short id now resolves every op, not just the one that was
        # handed the full filename
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="US-CHAT-00")["id"], "US-CHAT-00")
        dispatch(self.config, "work_item.transition", id="US-CHAT-00",
                 to="In Progress")
        dispatch(self.config, "work_item.add_comment", id="US-CHAT-00",
                 text="round 1 done")
        body = (self.stories
                / "US-CHAT-00-frontend-test-infrastructure.md").read_text(
                    encoding="utf-8")
        self.assertIn("Status: In Progress", body)   # wrote the RIGHT file
        self.assertIn("- round 1 done", body)

    def test_exact_filename_wins_over_a_slug_sibling_claiming_no_id(self):
        # the fallback is a fallback: a sibling that declares no id of its own
        # must never shadow the file that literally answers to it
        (self.stories / "WORK-7-notes.md").write_text(
            "## Scratch notes\nnot a story\n", encoding="utf-8")
        dispatch(self.config, "work_item.transition", id="WORK-7", to="Done")
        self.assertIn("Status: Done",
                      (self.stories / "WORK-7.md").read_text(encoding="utf-8"))
        self.assertNotIn("Status",        # sibling untouched
                         (self.stories / "WORK-7-notes.md").read_text(
                             encoding="utf-8"))

    def test_exact_plus_slug_both_claiming_the_id_is_ambiguous(self):
        # adversarial-review, lens A: gating the ambiguity check on "no exact
        # match" left the stated guarantee false in exactly the case where a
        # write is most dangerous — the run's provider_ref pointed at the slug
        # file while every replayed short id silently wrote the other one.
        from harness.providers import ProviderError
        (self.stories / "WORK-7-add-multiply.md").write_text(
            "# WORK-7: Add multiply\nStatus: Open\n", encoding="utf-8")
        with self.assertRaises(ProviderError) as ctx:
            dispatch(self.config, "work_item.transition", id="WORK-7", to="Done")
        self.assertIn("matches multiple files", str(ctx.exception))
        for name in ("WORK-7.md", "WORK-7-add-multiply.md"):    # neither written
            self.assertIn("Status: Open",
                          (self.stories / name).read_text(encoding="utf-8"))

    def test_a_sibling_report_does_not_brick_the_story_it_documents(self):
        # adversarial-review, lens A: /story-workflow writes `<id>-readiness.md`
        # and `<id>-technical-notes.md` into this same directory (analyze.md,
        # groom.md). Matching on filename shape alone made every one of those
        # refuse the story it documents — telling the human to rename an
        # artifact the harness itself had just created. Those reports open
        # `## Story Readiness Report` and declare no id; a story does.
        (self.stories / "US-8-the-actual-story.md").write_text(
            "# US-8: Real story\nStatus: Open\n", encoding="utf-8")
        for sibling in ("US-8-readiness.md", "US-8-technical-notes.md"):
            (self.stories / sibling).write_text(
                "## Story Readiness Report\n**Work Item:** #US-8 — Real story\n",
                encoding="utf-8")
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="US-8")["title"], "Real story")
        dispatch(self.config, "work_item.transition", id="US-8", to="Done")
        self.assertIn("Status: Done",
                      (self.stories / "US-8-the-actual-story.md").read_text(
                          encoding="utf-8"))

    def test_glob_metacharacters_in_an_id_resolve_nothing(self):
        # adversarial-review, both lenses: the id is an IDENTIFIER, but it was
        # interpolated into a glob PATTERN. A wildcard hitting exactly one file
        # triggers no ambiguity refusal, so `*` / `U?-1` / `US-[1]` each
        # silently resolved — and WROTE — a different item's file.
        from harness.providers import ProviderError
        (self.stories / "US-1-only-story.md").write_text(
            "# US-1: Only story\nStatus: Open\n", encoding="utf-8")
        for probe in ("*", "**", "U?RK-7", "WORK-[7]", "US-1[0]", "?S-1"):
            with self.assertRaises(ProviderError, msg=probe) as ctx:
                dispatch(self.config, "work_item.transition", id=probe, to="Done")
            self.assertIn("not found", str(ctx.exception), msg=probe)
        for name in ("WORK-7.md", "US-1-only-story.md"):        # nothing written
            self.assertIn("Status: Open",
                          (self.stories / name).read_text(encoding="utf-8"))

    def test_recursive_glob_id_cannot_escape_stories_dir(self):
        # adversarial-review, lens B — CONFIRMED escape, reproduced through the
        # shipped CLI. The confinement check reads the id as a literal path
        # component, where `**` is a directory name and `**/..` collapses back
        # INSIDE stories_dir; glob reads `**` as the recursive wildcard
        # matching zero segments, leaving `..` a real parent hop. The plain
        # `../secrets/X` spelling was refused throughout, so the guard read as
        # intact while this one wrote outside.
        from harness.providers import ProviderError
        outside = self.stories.parent / "secrets"
        outside.mkdir()
        (outside / "PRIVATE-1-notes.md").write_text(
            "# PRIVATE-1: outside\nStatus: Open\n", encoding="utf-8")
        try:
            for probe in ("**/../secrets/PRIVATE-1", "../secrets/PRIVATE-1"):
                with self.assertRaises(ProviderError, msg=probe):
                    dispatch(self.config, "work_item.transition", id=probe,
                             to="PWNED")
            self.assertIn("Status: Open",
                          (outside / "PRIVATE-1-notes.md").read_text(
                              encoding="utf-8"))
        finally:
            support.rmtree(outside)

    def test_a_slug_symlink_out_of_stories_dir_is_refused(self):
        # adversarial-review, lens B: the confinement check validated
        # `stories/<id>.md` — a path that by definition does not exist on the
        # fallback branch — and the globbed result was never re-checked. The
        # SAME symlink was refused under its exact name and accepted under a
        # slug name, silently losing a guarantee the module already had.
        from harness.providers import ProviderError
        outside = self.stories.parent / "outside-target.md"
        outside.write_text("# SYM-3: outside\nStatus: Open\n", encoding="utf-8")
        try:
            (self.stories / "SYM-3-slug.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        try:
            with self.assertRaises(ProviderError) as ctx:
                dispatch(self.config, "work_item.transition", id="SYM-3",
                         to="PWNED")
            self.assertIn("outside stories_dir", str(ctx.exception))
            self.assertIn("Status: Open", outside.read_text(encoding="utf-8"))
        finally:
            outside.unlink()

    def test_a_non_utf8_sibling_does_not_abort_the_run(self):
        # re-verify finding: _declared_id caught OSError only, but
        # UnicodeDecodeError is a ValueError — so one cp1252 smart quote in a
        # sibling file raised straight out of _path, past
        # _best_effort_transition's ProviderError catch, killing develop at
        # its first verb. Verbatim the field failure this change exists to fix.
        (self.stories / "WORK-7-notes.md").write_bytes(
            b"# WORK-7: notes \x92smart quote\x92\n")
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="WORK-7")["id"], "WORK-7")

    def test_an_h1_quoted_inside_a_section_is_prose_not_a_claim(self):
        # re-verify finding: _declared_id scanned the whole file, so a
        # grooming note that QUOTED the story's heading re-triggered "matches
        # multiple files" — the same header-scoping lesson FIELD_RE records.
        (self.stories / "WORK-7-technical-notes.md").write_text(
            "## Technical Notes\nThe story reads:\n\n# WORK-7: Fix null crash\n",
            encoding="utf-8")
        dispatch(self.config, "work_item.transition", id="WORK-7", to="Done")
        self.assertIn("Status: Done",
                      (self.stories / "WORK-7.md").read_text(encoding="utf-8"))

    def test_an_irregular_file_candidate_is_skipped_not_read(self):
        # re-verify finding: the confinement re-check ran AFTER the read, and
        # reading a FIFO with no writer blocks forever — wedging every op on
        # that id with no timeout anywhere. Regular files only, checked first.
        import os
        target = self.stories / "WORK-7-pipe.md"
        try:
            os.mkfifo(target)
        except (AttributeError, OSError, NotImplementedError):
            self.skipTest("FIFOs unavailable on this platform")
        (self.stories / "WORK-7-dir.md").mkdir()
        signal = __import__("signal")
        signal.signal(signal.SIGALRM,
                      lambda *a: (_ for _ in ()).throw(AssertionError("hung")))
        signal.alarm(10)
        try:
            self.assertEqual(dispatch(self.config, "work_item.fetch",
                                      id="WORK-7")["id"], "WORK-7")
        finally:
            signal.alarm(0)

    def test_a_slug_named_follow_up_is_not_re_minted(self):
        # adversarial-review, lens A: create()'s `taken` scan required a bare
        # `FU-<n>` stem, but _path() now answers `FU-1` with `FU-1-fix-login.md`
        # — so the minter re-issued an id the resolver had already given away,
        # writing a second file it then preferred and shadowing the first.
        (self.stories / "FU-1-fix-login.md").write_text(
            "# FU-1: Fix login\nStatus: Open\n", encoding="utf-8")
        self.assertEqual(dispatch(self.config, "work_item.create",
                                  title="deferred")["id"], "FU-2")
        self.assertEqual(dispatch(self.config, "work_item.fetch",
                                  id="FU-1")["title"], "Fix login")

    def test_ambiguous_slug_match_refuses_rather_than_picking_one(self):
        # two files claiming one id is a workspace mistake; picking a side
        # would land the write in an arbitrary one of them
        from harness.providers import ProviderError
        for slug in ("US-9-first.md", "US-9-second.md"):
            (self.stories / slug).write_text("# US-9: dup\nStatus: Open\n",
                                             encoding="utf-8")
        for op, kwargs in (("work_item.fetch", {}),
                           ("work_item.transition", {"to": "Done"}),
                           ("work_item.add_comment", {"text": "hi"})):
            with self.assertRaises(ProviderError) as ctx:
                dispatch(self.config, op, id="US-9", **kwargs)
            self.assertIn("matches multiple files", str(ctx.exception))
        for slug in ("US-9-first.md", "US-9-second.md"):    # neither written
            self.assertIn("Status: Open",
                          (self.stories / slug).read_text(encoding="utf-8"))

    def test_traversal_id_is_refused_before_the_slug_fallback(self):
        # the confinement check must run BEFORE the glob, or a traversal id
        # with no exact-match file would fall through to a pattern carrying
        # the `..` itself — reaching outside stories_dir by the back door.
        from harness.providers import ProviderError
        outside = self.stories.parent / f"{self.stories.name}-escape"
        outside.mkdir()
        (outside / "X-1-slug.md").write_text("# X-1: outside\nStatus: Open\n",
                                             encoding="utf-8")
        try:
            with self.assertRaises(ProviderError) as ctx:
                dispatch(self.config, "work_item.transition",
                         id=f"../{outside.name}/X-1", to="Done")
            self.assertIn("escapes stories_dir", str(ctx.exception))
            self.assertIn("Status: Open",
                          (outside / "X-1-slug.md").read_text(encoding="utf-8"))
        finally:
            support.rmtree(outside)

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
        self.assertNotIn("Done", outside.read_text(encoding="utf-8"))   # never touched

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
        support.rmtree(self.stories)

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
        body = (self.stories / "US-042-add-multiply.md").read_text(encoding="utf-8")
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
        body = (self.stories / "US-044.md").read_text(encoding="utf-8")
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
