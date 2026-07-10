"""Scaffold the v0.16 end-to-end workspace (docs/validation-plan.md, C + D).

Two repos — the multi-repo path is where every 0.16.x field bug lived — plus
four stories built to walk the machinery the HEX runs exercised the hard way:

  svc/  python (pyproject marker, stdlib-unittest suite) — discover's static
        coverage proposal will fail its confirm-by-run wherever pytest-cov
        isn't installed, deliberately exercising the interview's
        ask-or-skip path (0.16.2) and harden's `coverage-skipped` event.
        Also carries session D's edge bait: `api/serializer.py` (a quick fix
        here dirties quick-recheck via the **/api/** disqualify pattern),
        `fixtures/config_sample.py` (a demo token `tools/scan.sh` reports as
        HIGH, so the approve-security gate actually FIRES), and the scanner
        itself (wire it as `security.scan_cmd.svc: sh tools/scan.sh`).
  web/  node (package.json with a real `coverage` script over `node --test`)
        — deliberately exercising the evidence-based proposal path (0.16.2).

Stories:
  E2E-1  session C's full-mode happy path (tags + contract + docs-only task)
  E2E-2  session D: quick-hinted one-liner whose diff touches api/** —
         quick-recheck dirties, the declared quick->full escalation fires,
         the scanner's HIGH presents approve-security (waive path)
  E2E-3  session D: full mode with edge freight — a ratified contract that
         deliberately declares a forward-looking fragment (unarchive_note)
         nothing implements yet, so reconcile-contracts must flag drift; a
         docs-only criterion; and the scanner's HIGH again (defer path ->
         work_item.create writes stories/FU-1.md)
  E2E-4  session D: decoy for the abort/collision drills — fetched and
         aborted, never finished

Usage: python3 tools/make_e2e_workspace.py <target-dir>
           [--github <owner>] [--github-prefix <prefix>]

--github <owner>: also create two private GitHub repos via `gh` and push
main, so /dev-workflow's create-pr opens REAL PRs (delete the repos after).
Without it, remotes are skipped and the run ends records-only at create-pr.
--github-prefix (default `harness-e2e`): repo-name prefix — pass a distinct
prefix per session (e.g. `harness-e2e-d`) so scratch repos never collide.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STORY = """# E2E-1: Tag support for notes, service and web client
Type: Story
Status: Open

## Description
The notes service (svc) stores plain notes; the web client (web) renders
them. Users need tags: the service filters notes by tag, and the client
mirrors that filter against the same note shape.

The note JSON shape is ratified up front as the cross-repo contract —
`{"id": int, "text": str, "tags": [str]}` — so the web client is built
against the contract, in parallel, not behind the service implementation.

## Acceptance Criteria
- [ ] svc: notes accept an optional `tags` list (default empty); existing
      untagged behavior unchanged
- [ ] svc: `filter_notes(notes, tag)` returns only notes carrying `tag`;
      a non-string tag raises TypeError
- [ ] web: `filterByTag(notes, tag)` mirrors the service filter against
      the ratified note shape `{id, text, tags}`
- [ ] docs: both repos' README usage sections describe tagging
      (documentation only — no tests for this criterion)
"""

STORY_E2E2 = """# E2E-2: Serializer renders notes in the documented format
Type: Task
Status: Open

## Description
Mode: quick

`api/serializer.py` renders a note as `note 1: hello`, but the README and
every consumer document `#1: hello`. One-line formatting fix in the
serializer, nothing else.

## Acceptance Criteria
- [ ] svc: `serialize(note)` renders `#<id>: <text>` (e.g. `#1: hello`)
"""

STORY_E2E3 = """# E2E-3: Note archiving across service and client
Type: Story
Status: Open

## Description
Users want to archive notes without deleting them. The service owns the
archived flag; the web client mirrors the filter against the same shape.

The note JSON shape is ratified up front as the cross-repo contract:
`{"id": int, "text": str, "archived": bool}` — a note with no archived
key is treated as ACTIVE by both sides. Declare ALL contract fragments at
plan-register, including the forward-declared `unarchive_note(notes,
note_id)`: it ships in a follow-up story, and its fragment stays declared
on purpose so reconcile-contracts tracks the drift until then. Do NOT
implement unarchive in this story.

## Acceptance Criteria
- [ ] svc: `archive_note(notes, note_id)` returns a new list with that
      note's `archived` set true; unknown id raises KeyError; a non-int
      id raises TypeError
- [ ] svc: `list_notes(notes, include_archived=False)` filters archived
      notes out by default; `include_archived=True` returns everything
- [ ] web: `filterActive(notes)` returns only non-archived notes,
      treating a missing `archived` property as active
- [ ] docs: both repos' README usage sections describe archiving
      (documentation only — no tests for this criterion)
"""

STORY_E2E4 = """# E2E-4: Web package description mentions notes
Type: Task
Status: Open

## Description
The web package.json description field is empty; set it to "Tiny notes
web client". (Validation-plan session D uses this story for the abort and
collision drills — it is fetched and aborted, never finished.)

## Acceptance Criteria
- [ ] web: package.json description says "Tiny notes web client"
"""

SVC_PYPROJECT = """[project]
name = "svc"
version = "0.1.0"
"""

SVC_SERIALIZER = '''"""Serializers for the notes api surface."""


def serialize(note):
    return f"note {note['id']}: {note['text']}"
'''

SVC_FIXTURE_CONFIG = '''"""Sample config for local development (e2e fixture).

The demo token below is DELIBERATE scanner bait (tools/scan.sh reports it
as HIGH) — it exists to drive the approve-security gate in validation
sessions. It is not a real credential; do not remove it as part of any
story unless a story's acceptance criteria say so.
"""

DEMO_API_TOKEN = "e2e-demo-token-not-a-real-secret"
DEFAULT_PAGE_SIZE = 20
'''

SVC_SCAN_SH = """#!/bin/sh
# e2e fixture scanner (wire as security.scan_cmd.svc: `sh tools/scan.sh`):
# reports HIGH while the demo-token bait is present anywhere in the repo.
# Always exits 0 — severity is parsed from the OUTPUT (severity_regex),
# not the exit code.
if grep -rq "DEMO_API_TOKEN" --exclude-dir=.git --exclude=scan.sh .; then
  echo "HIGH: hardcoded token DEMO_API_TOKEN (fixtures/config_sample.py)"
else
  echo "clean: no findings"
fi
exit 0
"""

SVC_NOTES = '''"""Tiny notes service — the e2e fixture."""


def add_note(notes, text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    note = {"id": len(notes) + 1, "text": text}
    return [*notes, note]


def list_notes(notes):
    return list(notes)
'''

SVC_TEST = '''import unittest

import notes as svc


class TestNotes(unittest.TestCase):
    def test_add_note_appends_with_id(self):
        got = svc.add_note([], "hello")
        self.assertEqual(got, [{"id": 1, "text": "hello"}])

    def test_add_note_rejects_non_string(self):
        with self.assertRaises(TypeError):
            svc.add_note([], 42)


if __name__ == "__main__":
    unittest.main()
'''

SVC_README = """# svc

Tiny notes service (e2e fixture).

## Usage

    import notes
    ns = notes.add_note([], "hello")
"""

WEB_PACKAGE = """{
  "name": "web",
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "test": "node --test",
    "coverage": "node --test --experimental-test-coverage"
  }
}
"""

WEB_NOTES = '''// Tiny notes web client — the e2e fixture.

export function formatNote(note) {
  return `#${note.id}: ${note.text}`;
}

export function loadNotes(raw) {
  return JSON.parse(raw);
}
'''

WEB_TEST = '''import test from "node:test";
import assert from "node:assert/strict";
import { formatNote, loadNotes } from "../src/notes.mjs";

test("formatNote renders id and text", () => {
  assert.equal(formatNote({ id: 1, text: "hello" }), "#1: hello");
});

test("loadNotes parses the service shape", () => {
  assert.deepEqual(loadNotes('[{"id":1,"text":"hi"}]'),
                   [{ id: 1, text: "hi" }]);
});
'''

WEB_README = """# web

Tiny notes web client (e2e fixture).

## Usage

    import { loadNotes } from "./src/notes.mjs";
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _init_repo(repo: Path, message: str) -> None:
    _git(repo, "init", "-b", "main", ".")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _add_github_remote(repo: Path, owner: str, prefix: str) -> str:
    name = f"{prefix}-{repo.name}"
    created = subprocess.run(
        ["gh", "repo", "create", f"{owner}/{name}", "--private"],
        capture_output=True, text=True)
    if created.returncode != 0 and "already exists" not in created.stderr.lower():
        raise SystemExit(f"gh repo create {owner}/{name} failed: "
                         f"{created.stderr.strip()}")
    _git(repo, "remote", "add", "origin", f"git@github.com:{owner}/{name}.git")
    _git(repo, "push", "-u", "origin", "main")
    return f"{owner}/{name}"


def main() -> int:
    args = sys.argv[1:]
    owner, prefix = None, "harness-e2e"
    for flag in ("--github", "--github-prefix"):
        if flag in args:
            i = args.index(flag)
            try:
                value = args[i + 1]
            except IndexError:
                print(__doc__)
                return 2
            if flag == "--github":
                owner = value
            else:
                prefix = value
            args = args[:i] + args[i + 2:]
    if len(args) != 1 or args[0].startswith("-"):
        print(__doc__)
        return 2
    ws = Path(args[0]).expanduser().resolve()
    if ws.exists() and any(ws.iterdir()):
        print(f"refusing: {ws} exists and is not empty")
        return 1

    stories = ws / "stories"
    stories.mkdir(parents=True)
    (stories / "E2E-1.md").write_text(STORY, encoding="utf-8")
    (stories / "E2E-2.md").write_text(STORY_E2E2, encoding="utf-8")
    (stories / "E2E-3.md").write_text(STORY_E2E3, encoding="utf-8")
    (stories / "E2E-4.md").write_text(STORY_E2E4, encoding="utf-8")

    svc = ws / "svc"
    (svc / "tests").mkdir(parents=True)
    (svc / "api").mkdir()
    (svc / "fixtures").mkdir()
    (svc / "tools").mkdir()
    (svc / "pyproject.toml").write_text(SVC_PYPROJECT, encoding="utf-8")
    (svc / "notes.py").write_text(SVC_NOTES, encoding="utf-8")
    (svc / "api" / "__init__.py").write_text("", encoding="utf-8")
    (svc / "api" / "serializer.py").write_text(SVC_SERIALIZER, encoding="utf-8")
    (svc / "fixtures" / "config_sample.py").write_text(SVC_FIXTURE_CONFIG,
                                                       encoding="utf-8")
    (svc / "tools" / "scan.sh").write_text(SVC_SCAN_SH, encoding="utf-8")
    (svc / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (svc / "tests" / "test_notes.py").write_text(SVC_TEST, encoding="utf-8")
    (svc / "README.md").write_text(SVC_README, encoding="utf-8")
    _init_repo(svc, "svc: initial notes + tests")

    web = ws / "web"
    (web / "src").mkdir(parents=True)
    (web / "tests").mkdir(parents=True)
    (web / "package.json").write_text(WEB_PACKAGE, encoding="utf-8")
    (web / "src" / "notes.mjs").write_text(WEB_NOTES, encoding="utf-8")
    (web / "tests" / "notes.test.mjs").write_text(WEB_TEST, encoding="utf-8")
    (web / "README.md").write_text(WEB_README, encoding="utf-8")
    _init_repo(web, "web: initial client + tests")

    remotes = []
    if owner:
        remotes = [_add_github_remote(svc, owner, prefix),
                   _add_github_remote(web, owner, prefix)]

    print(f"e2e workspace ready: {ws}")
    print("  stories: E2E-1 (session C) · E2E-2/3/4 (session D edge sweep)")
    print(f"  repos:  {svc} (python3 -m unittest discover -s tests)")
    print(f"          {web} (npm test — node --test, no npm install needed)")
    if remotes:
        print(f"  github: {remotes[0]}, {remotes[1]} (private — delete after)")
    else:
        print("  github: none (--github <owner> for real create-pr PRs)")
    print("next: sync the plugin copy, start Claude Code IN this directory,"
          " and follow docs/validation-plan.md session C (happy path) or"
          " session D (edge sweep; wire security.scan_cmd.svc at init)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
