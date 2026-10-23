import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import canvas_mcp as canvas


def mock_canvas(respond):
    """Route every _canvas_client through an httpx MockTransport."""
    original = canvas._canvas_client

    def client(**kwargs):
        return original(transport=httpx.MockTransport(respond), **kwargs)

    async def dns_ok():
        return None

    return patch.multiple(canvas, _canvas_client=client, CANVAS_API_TOKEN="test-token",
                          _dns_ready=dns_ok)


class TempRootsMixin:
    """Let tests use the system temp dir, which is outside the real FILE_ROOTS."""
    def setUp(self):
        roots = patch.object(canvas, "FILE_ROOTS", [Path(tempfile.gettempdir()).resolve()])
        roots.start()
        self.addCleanup(roots.stop)


class DownloadTests(TempRootsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_saves_without_overwriting(self):
        def respond(request):
            if request.url.path == "/api/v1/files/5":
                return httpx.Response(200, json={"display_name": "../Syllabus.pdf",
                                                 "url": "https://files.example/signed"})
            if request.url.host == "files.example":
                return httpx.Response(302, headers={"location": "https://cdn.example/x"})
            return httpx.Response(200, content=b"%PDF-bytes")

        with tempfile.TemporaryDirectory() as d, mock_canvas(respond):
            for expected in ("Syllabus.pdf", "Syllabus (2).pdf"):
                out = await canvas.canvas_download_file(
                    canvas.DownloadFileInput(file_id=5, dest_dir=d))
                self.assertIn(expected, out)
                self.assertEqual((Path(d) / expected).read_bytes(), b"%PDF-bytes")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()),
                             ["Syllabus (2).pdf", "Syllabus.pdf"])

    async def test_failed_download_leaves_no_file(self):
        def respond(request):
            if request.url.path == "/api/v1/files/5":
                return httpx.Response(200, json={"display_name": "a.pdf",
                                                 "url": "https://files.example/signed"})
            return httpx.Response(406)

        with tempfile.TemporaryDirectory() as d, mock_canvas(respond):
            out = await canvas.canvas_download_file(canvas.DownloadFileInput(file_id=5, dest_dir=d))
            self.assertIn("406", out)
            self.assertEqual(list(Path(d).iterdir()), [])


class SubmitLocalFileTests(TempRootsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_preview_then_upload_and_submit(self):
        requests = []

        def respond(request):
            requests.append(request)
            path = request.url.path
            if path.endswith("/submissions/self/files"):
                return httpx.Response(200, json={"upload_url": "https://up.example/",
                                                 "upload_params": {}, "file_param": "file"})
            if request.url.host == "up.example":
                return httpx.Response(201, json={"id": 777})
            if path.endswith("/submissions"):
                return httpx.Response(200, json={"workflow_state": "submitted"})
            return httpx.Response(200, json={"name": "Essay", "due_at": None})

        with tempfile.TemporaryDirectory() as d, mock_canvas(respond):
            essay = Path(d) / "essay.pdf"
            essay.write_bytes(b"x" * 2048)
            args = dict(course_id=1, assignment_id=2, submission_type="online_upload",
                        local_paths=[str(essay)])

            preview = await canvas.canvas_submit_assignment(canvas.SubmitAssignmentInput(**args))
            self.assertIn("DRY RUN", preview)
            self.assertIn("essay.pdf (2 KB)", preview)
            self.assertFalse(any(r.method == "POST" for r in requests))

            done = await canvas.canvas_submit_assignment(
                canvas.SubmitAssignmentInput(**args, confirm=True))
            self.assertIn("Submitted", done)
            submit = requests[-1]
            self.assertEqual(submit.url.path, "/api/v1/courses/1/assignments/2/submissions")
            self.assertIn(b"submission%5Bfile_ids%5D%5B%5D=777", submit.content)

    async def test_missing_local_file_errors_before_any_request(self):
        with mock_canvas(lambda r: self.fail("no request expected")):
            out = await canvas.canvas_submit_assignment(canvas.SubmitAssignmentInput(
                course_id=1, assignment_id=2, submission_type="online_upload",
                local_paths=[str(Path(tempfile.gettempdir()) / "nonexistent" / "essay.pdf")]))
        self.assertIn("Local file not found", out)


class SafePathTests(unittest.IsolatedAsyncioTestCase):
    def test_allows_only_visible_files_inside_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "notes.pdf").write_bytes(b"x")
            (root / ".env").write_text("CANVAS_API_TOKEN=secret")
            (root / "link.txt").symlink_to("/etc/hosts")
            with patch.object(canvas, "FILE_ROOTS", [root]):
                self.assertEqual(canvas._safe_path(root / "notes.pdf"), root / "notes.pdf")
                for bad in ("/etc/hosts", root / ".env", root / ".ssh/id_rsa",
                            root / "link.txt", root / "../escape.txt",
                            canvas._SERVER_DIR / "canvas_mcp.py"):
                    with self.subTest(bad=str(bad)), self.assertRaises(canvas.CanvasError):
                        canvas._safe_path(bad)

    async def test_tools_refuse_paths_outside_roots(self):
        with mock_canvas(lambda r: self.fail("no request expected")):
            msg = await canvas.canvas_send_message(canvas.SendMessageInput(
                recipient_ids=["1"], subject="s", body="b", local_paths=["/etc/hosts"]))
            dl = await canvas.canvas_download_file(canvas.DownloadFileInput(
                file_id=5, dest_dir="/Library/LaunchAgents"))
            sub = await canvas.canvas_submit_assignment(canvas.SubmitAssignmentInput(
                course_id=1, assignment_id=2, submission_type="online_upload",
                local_paths=["~/.ssh/id_rsa"]))
        for out in (msg, dl, sub):
            self.assertIn("Error:", out)
            self.assertNotIn("DRY RUN", out)


class DnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_failure_gives_clear_network_error(self):
        async def fail(*args, **kwargs):
            raise OSError("nodename nor servname provided")

        loop = __import__("asyncio").get_running_loop()
        with patch.object(loop, "getaddrinfo", fail), \
                patch.object(canvas, "CANVAS_API_TOKEN", "test-token"):
            out = await canvas.canvas_get_profile(canvas.ProfileInput())
        self.assertIn("internet or DNS is not answering", out)


if __name__ == "__main__":
    unittest.main()
