import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import canvas_mcp as canvas

ROOT = Path(__file__).resolve().parent.parent
LIST_TOOLS = ("import asyncio, json, canvas_mcp as c; "
              "print(json.dumps([[t.name, t.description] for t in asyncio.run(c.mcp.list_tools())]))")


def exposed_tools(toolset):
    env = {**os.environ, "CANVAS_TOOLSET": toolset}
    out = subprocess.run([sys.executable, "-c", LIST_TOOLS], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    return dict(json.loads(out))


class ToolsetTests(unittest.IsolatedAsyncioTestCase):
    def test_lean_exposes_read_only_code_mode_and_write_tools(self):
        lean = exposed_tools("lean")
        self.assertEqual(sorted(lean), sorted([
            "canvas_run_code", "canvas_download_file", "canvas_create_planner_note",
            "canvas_submit_assignment", "canvas_post_discussion_entry",
            "canvas_reply_to_entry", "canvas_post_submission_comment",
            "canvas_upload_file", "canvas_send_message"]))
        self.assertEqual(len(exposed_tools("full")), len(canvas._TOOLS))

    def test_descriptions_drop_args_and_returns(self):
        for name, desc in exposed_tools("full").items():
            with self.subTest(name=name):
                self.assertNotIn("Args:", desc)
                self.assertNotIn("Returns:", desc)
                self.assertTrue(desc)

    async def test_scripts_reach_read_tools_not_exposed_in_lean(self):
        async def fake_get(path, params=None):
            return {"name": "Test Student", "id": 1}

        with patch.object(canvas, "_get", fake_get):
            out = await canvas._code_tool("canvas_get_profile", {}, allow_writes=False)
        self.assertIn("Test Student", out)


if __name__ == "__main__":
    unittest.main()
