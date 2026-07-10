"""MCP-transport step-one, through the real CLI. `fetch --from-raw` runs the
same normalize -> classify -> bootstrap that `fetch --id` runs after `dispatch`
— closing the gap where MCP providers (ado-mcp / jira / zoho) `dispatch`-refuse
at startup and the run could never bootstrap. The plain `fetch --id` path still
refuses with tool guidance for an MCP provider (a script can't call the tool).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import initws

ROOT = Path(__file__).resolve().parent.parent

# Raw `wit_get_work_item` shape (== `az boards work-item show`): the MCP twin
# and the CLI provider normalize this identically (shared ado_common).
ADO_RAW = {"id": 4321, "fields": {
    "System.Title": "Add retry to uploader",
    "System.WorkItemType": "Story",
    "System.State": "New",
    "System.Description": "<div>Uploads fail on flaky networks.</div>",
    "Microsoft.VSTS.Common.AcceptanceCriteria":
        "<div>retries 3x with backoff</div>"}}


class FetchFromRaw(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        # `init` hardcodes local-markdown, so stand up an ado-mcp workspace by
        # writing the sections + bootstrap marker directly.
        initws.write_section(self.ws, "provider",
                             {"provider": {"work_item": "ado-mcp",
                                           "git": "ado-mcp",
                                           "ado_project": "Contoso"}})
        initws.write_section(self.ws, "repos", {"repos": {"repo": "."}})
        initws.write_section(self.ws, "language",
                             {"language": {"test_cmd": "true"}})
        initws.mark_bootstrapped(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws)

    def cli(self, *args, stdin=None, expect=0):
        cmd = [sys.executable, "-m", "harness", "--workspace", str(self.ws),
               *args]
        proc = subprocess.run(cmd, cwd=ROOT, input=stdin, capture_output=True,
                              text=True, timeout=120)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        self.assertEqual(proc.returncode, expect,
                         f"harness {' '.join(args)} -> {payload} {proc.stderr}")
        return payload

    def test_plain_fetch_refuses_with_tool_guidance(self):
        # The scripted path can't invoke an MCP tool — refuse, name the tool.
        out = self.cli("fetch", "--id", "4321", expect=1)
        self.assertIn("mcp__azure-devops__wit_get_work_item", out["error"])

    def test_from_raw_bootstraps_the_run(self):
        out = self.cli("fetch", "--from-raw", stdin=json.dumps(ADO_RAW))
        run = Path(out["run"])
        self.assertEqual(out["mode"], "full")            # Story, no quick hint
        self.assertEqual(out["change_type"], "feature")  # Story -> feature (map)
        # work-item.json persisted from the normalized contract (HTML stripped).
        item = json.loads((run / "work-item.json").read_text())
        self.assertEqual(item["id"], "4321")
        self.assertEqual(item["title"], "Add retry to uploader")
        self.assertEqual(item["provider_ref"], "ado#4321")
        self.assertEqual(item["acceptance_criteria"], ["retries 3x with backoff"])
        # state.yaml bootstrapped, and the fetched event recorded.
        self.assertTrue((run / "state.yaml").exists())
        events = [json.loads(l) for l in
                  (run / "events.ndjson").read_text().splitlines()]
        self.assertEqual(events[0]["kind"], "fetched")

    def test_from_raw_collision_refuses_second_time(self):
        self.cli("fetch", "--from-raw", "--date", "2026-02-02",
                 stdin=json.dumps(ADO_RAW))
        # Same item, same date -> from-nothing transition refuses (no clobber).
        self.cli("fetch", "--from-raw", "--date", "2026-02-02",
                 stdin=json.dumps(ADO_RAW), expect=1)

    def test_fetch_without_id_or_from_raw_is_clean_error(self):
        out = self.cli("fetch", expect=1)
        self.assertIn("--id", out["error"])


if __name__ == "__main__":
    unittest.main()
