import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

import canvas_mcp as canvas


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        roots = patch.object(canvas, "FILE_ROOTS", [Path(tempfile.gettempdir()).resolve()])
        roots.start()
        self.addCleanup(roots.stop)

    async def test_upload_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "note.txt"
            local_path.write_bytes(b"attachment contents")
            for destination, folder in (("course", None), ("course", "Handouts"),
                                        ("user", "conversation attachments")):
                for status in (200, 201, 301, 302, 303, 307, 400):
                    for display_name in (None, "renamed.txt"):
                        with self.subTest(destination=destination, folder=folder,
                                          status=status, display_name=display_name):
                            requests = []
                            options = []
                            name = display_name or local_path.name

                            def respond(request):
                                requests.append(request)
                                if len(requests) == 1:
                                    return httpx.Response(200, json={
                                        "upload_url": "https://storage.example/upload",
                                        "upload_params": {"key": 123},
                                        "file_param": "attachment",
                                    })
                                if len(requests) == 2:
                                    return httpx.Response(status, json={"id": 42},
                                                          headers={"location": "https://canvas.example/confirm"})
                                return httpx.Response(200, json={"id": 42})

                            original_client = canvas._canvas_client

                            def client(**kwargs):
                                options.append(kwargs.copy())
                                return original_client(transport=httpx.MockTransport(respond), **kwargs)

                            with patch.object(canvas, "_canvas_client", side_effect=client), \
                                    patch.object(canvas, "CANVAS_API_TOKEN", "test-token"):
                                if destination == "course":
                                    upload = canvas._canvas_upload_file(7, str(local_path), display_name, folder)
                                else:
                                    upload = canvas._upload_user_file(str(local_path), display_name)
                                if status in (307, 400):
                                    with self.assertRaises(httpx.HTTPStatusError):
                                        await upload
                                else:
                                    self.assertEqual(await upload, {"id": 42})

                            slot, upload = requests[:2]
                            self.assertEqual(slot.method, "POST")
                            self.assertEqual(slot.url.path, "/api/v1/courses/7/files" if destination == "course"
                                             else "/api/v1/users/self/files")
                            expected = {"name": [name], "size": ["19"], "content_type": ["text/plain"]}
                            if folder:
                                expected["parent_folder_path"] = [folder]
                            self.assertEqual(parse_qs(slot.content.decode()), expected)
                            self.assertEqual(slot.headers["authorization"], "Bearer test-token")
                            self.assertEqual(upload.method, "POST")
                            self.assertNotIn("authorization", upload.headers)
                            self.assertEqual(upload.headers["accept"], "*/*")
                            self.assertIn(f'name="attachment"; filename="{name}"'.encode(), upload.content)
                            self.assertIn(b'name="key"\r\n\r\n123', upload.content)
                            self.assertIn(b"attachment contents", upload.content)
                            self.assertEqual(options[:2], [
                                {"timeout": canvas.REQUEST_TIMEOUT},
                                {"timeout": 120.0, "follow_redirects": False, "headers": {"Accept": "*/*"}},
                            ])
                            self.assertEqual(len(requests), 3 if status in (301, 302, 303) else 2)
                            if len(requests) == 3:
                                self.assertEqual(requests[2].method, "GET")
                                self.assertEqual(str(requests[2].url), "https://canvas.example/confirm")
                                self.assertEqual(requests[2].headers["authorization"], "Bearer test-token")
                                self.assertEqual(options[2], {"timeout": canvas.REQUEST_TIMEOUT})

    async def test_invalid_paths_make_no_requests(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(canvas, "_canvas_client") as client:
            for local_path, message in ((str(Path(directory) / "missing"), "Local file not found"),
                                        (directory, "Path is not a file")):
                for destination in ("course", "user"):
                    with self.subTest(destination=destination, local_path=local_path):
                        with self.assertRaises(canvas.CanvasError) as error:
                            if destination == "course":
                                await canvas._canvas_upload_file(7, local_path, None, None)
                            else:
                                await canvas._upload_user_file(local_path, None)
                        self.assertEqual(str(error.exception), f"{message}: {local_path}")
            client.assert_not_called()

    async def test_slot_and_confirmation_errors_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "note.txt"
            local_path.write_text("attachment contents")
            for destination in ("course", "user"):
                for failed_step in (1, 3):
                    with self.subTest(destination=destination, failed_step=failed_step):
                        requests = []

                        def respond(request):
                            requests.append(request)
                            if len(requests) == failed_step:
                                return httpx.Response(403)
                            if len(requests) == 1:
                                return httpx.Response(200, json={"upload_url": "https://storage.example/upload"})
                            self.assertIn(b'name="file"', request.content)
                            return httpx.Response(302, headers={"location": "https://canvas.example/confirm"})

                        original_client = canvas._canvas_client
                        with patch.object(canvas, "_canvas_client", side_effect=lambda **kwargs:
                                          original_client(transport=httpx.MockTransport(respond), **kwargs)), \
                                patch.object(canvas, "CANVAS_API_TOKEN", "test-token"):
                            with self.assertRaises(httpx.HTTPStatusError) as error:
                                if destination == "course":
                                    await canvas._canvas_upload_file(7, str(local_path), None, None)
                                else:
                                    await canvas._upload_user_file(str(local_path), None)
                            self.assertEqual(error.exception.response.status_code, 403)
                            self.assertEqual(len(requests), failed_step)

    async def test_previews_make_no_requests(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(canvas, "_canvas_client") as client, \
                patch.object(canvas, "_send") as send:
            local_path = Path(directory) / "note.txt"
            local_path.write_text("attachment contents")
            upload = await canvas.canvas_upload_file(canvas.UploadFileInput(
                course_id=7, local_path=str(local_path)))
            message = await canvas.canvas_send_message(canvas.SendMessageInput(
                recipient_ids=["9"], subject="Test", body="Hello", local_paths=[str(local_path)]))
            self.assertIn("DRY RUN", upload)
            self.assertIn("DRY RUN", message)
            client.assert_not_called()
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
