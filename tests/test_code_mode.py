import unittest
from unittest.mock import patch

import canvas_mcp as canvas


@unittest.skipUnless(canvas._deno_path(), "deno not installed")
class CodeModeTests(unittest.IsolatedAsyncioTestCase):
    async def run_code(self, code):
        return await canvas.canvas_run_code(canvas.RunCodeInput(code=code))

    async def test_get_is_proxied_and_output_returned(self):
        async def fake_get(path, params=None):
            return {"path": path, "params": params}

        with patch.object(canvas, "_get", fake_get):
            out = await self.run_code(
                'const r = await canvas.get("/users/self", {a: 1}); console.log("hi"); return r;')
        self.assertIn("hi", out)
        self.assertIn('"path": "/users/self"', out)

    async def test_rejects_paths_outside_api(self):
        for path in ("https://evil.example/x", "//evil.example", "/../login", "courses"):
            with self.subTest(path=path):
                out = await self.run_code(f'return await canvas.get({path!r});')
                self.assertIn("Error in script", out)

    async def test_write_tools_not_callable(self):
        out = await self.run_code('return await canvas.tool("canvas_submit_assignment", {});')
        self.assertIn("call it as its own tool", out)

    async def test_write_mode_calls_write_tools_unconfirmed_as_preview(self):
        out = await canvas.canvas_run_code_write(canvas.RunCodeInput(code=(
            'return await canvas.tool("canvas_create_planner_note", {title: "x", todo_date: "2026-12-01"});')))
        self.assertIn("DRY RUN", out)

    async def test_course_name_resolution(self):
        courses = [{"id": 70437, "course_code": "THEO 13-L1", "name": "A THEOLOGY OF MARRIAGE"},
                   {"id": 52809, "course_code": "PEPC 11.13-PFT-L1", "name": "DANCE FUNDAMENTALS"},
                   {"id": 63386, "course_code": "PEPC 17.13-PFT-C", "name": "SWIMMING-PFT-C"},
                   {"id": 99, "course_code": "THEO 131-A", "name": "OTHER"}]

        async def fake_fetch(state="active"):
            return courses if state == "active" else []

        with patch.object(canvas, "_fetch_courses", fake_fetch), \
                patch.object(canvas, "_course_cache", {}):
            self.assertEqual(await canvas._resolve_course("THEO 13"), 70437)
            self.assertEqual(await canvas._resolve_course("theo13"), 70437)
            self.assertEqual(await canvas._resolve_course("swimming"), 63386)
            self.assertEqual(await canvas._resolve_course("123"), 123)
            for ref in ("PEPC", "nothing here"):
                with self.subTest(ref=ref), self.assertRaises(canvas.CanvasError):
                    await canvas._resolve_course(ref)

    async def test_sandbox_has_no_escape(self):
        out = await self.run_code('''
            const r = [];
            for (const f of [() => fetch("https://example.com"),
                             () => Deno.readTextFile("canvas_mcp.py"),
                             () => import("./canvas_mcp.py", {with: {type: "text"}}),
                             () => Deno.env.get("HOME"),
                             () => new Deno.Command("ls").output()]) {
              try { await f(); r.push("LEAK"); } catch { r.push("blocked"); }
            }
            return r.join(",");''')
        self.assertEqual(out, "blocked,blocked,blocked,blocked,blocked")

    async def test_call_limit(self):
        with patch.object(canvas, "CODE_MAX_CALLS", 3):
            out = await self.run_code(
                'let ok = 0; for (let i = 0; i < 5; i++) { try { await canvas.htmlToText("x"); ok++; } catch {} } return ok;')
        self.assertEqual(out, "3")


if __name__ == "__main__":
    unittest.main()
