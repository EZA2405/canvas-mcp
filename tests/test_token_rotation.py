import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import canvas_mcp
import canvas_token_rotation as rotation


class TokenRotationTests(unittest.TestCase):
    def test_initialize_and_protect_token_file(self):
        origin = "https://canvas.example.edu"
        now = datetime(2026, 12, 10, 17, tzinfo=timezone.utc)

        def canvas(request):
            self.assertEqual(request.url.host, "canvas.example.edu")
            if request.url.path == rotation.TOKEN_LIST:
                return httpx.Response(200, json=[
                    {"id": 8, "visible_token": "fixture...", "expires_at": "2026-12-30T17:00:00Z"},
                ])
            return httpx.Response(200, json={"id": 10})

        with tempfile.TemporaryDirectory() as directory, httpx.Client(
            base_url=origin, transport=httpx.MockTransport(canvas)
        ) as client:
            path = Path(directory) / "token.json"
            rotation.initialize(client, path, "fixture-token", now)
            self.assertEqual(rotation.read_state(path)["next_rotation"], "2026-12-23T17:00:00+00:00")
            alias = path.with_name("alias.json")
            alias.hardlink_to(path)
            with patch.multiple(canvas_mcp, CANVAS_TOKEN_FILE=str(path), CANVAS_BASE_URL="https://other.example.edu",
                                FILE_ROOTS=[path.parent.resolve()]):
                with self.assertRaises(canvas_mcp.CanvasError):
                    canvas_mcp._headers()
                with self.assertRaisesRegex(canvas_mcp.CanvasError, "token file is protected"):
                    canvas_mcp._safe_path(alias)
                with self.assertRaisesRegex(canvas_mcp.CanvasError, "token file is protected"):
                    canvas_mcp._safe_path(path)
            with patch.multiple(canvas_mcp, CANVAS_TOKEN_FILE=str(path), CANVAS_BASE_URL=origin):
                with self.assertRaises(canvas_mcp.CanvasError):
                    canvas_mcp._headers("https://other.example.edu/redirect")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                rotation.read_state(path)

    def test_token_pagination_rejects_another_origin(self):
        def canvas(request):
            self.assertEqual(request.url.host, "canvas.example.edu")
            return httpx.Response(200, json=[], headers={
                "Link": '<https://other.example.edu/api/v1/users/self/user_generated_tokens?page=2>; rel="next"',
            })

        with httpx.Client(
            base_url="https://canvas.example.edu", transport=httpx.MockTransport(canvas)
        ) as client:
            with self.assertRaises(ValueError):
                rotation.token_records(client, "fixture-token")

    def test_swap_and_cleanup(self):
        now = datetime(2026, 12, 10, 17, tzinfo=timezone.utc)
        old_expiration = now + timedelta(days=25)
        records = [
            dict(id=1, visible_token="old...", expires_at=old_expiration.isoformat(),
                 created_at=(old_expiration - timedelta(days=30)).isoformat(), scopes=[]),
            dict(id=2, visible_token="old...", expires_at=(now + timedelta(days=30)).isoformat(),
                 created_at=now.isoformat(), scopes=[]),
            dict(id=3, visible_token="old...", expires_at=(now + timedelta(days=50)).isoformat(),
                 created_at=(now + timedelta(days=20)).isoformat(), scopes=[]),
        ]
        requests = []
        writes = []
        replacement_account = 11
        active = {1}

        def canvas(request):
            requests.append(request.method)
            if request.method == "GET":
                if request.url.path == httpx.URL(rotation.PROFILE).path:
                    account = replacement_account if request.headers["Authorization"] == "Bearer new-secret" else 10
                    return httpx.Response(200, json={"id": account})
                return httpx.Response(200, json=[record for record in records if record["id"] in active])
            writes.append((request.method, request.url.path))
            if request.method == "POST":
                self.assertIn(b"token%5Bexpires_at%5D", request.content)
                if len(writes) == 1:
                    active.add(2)
                    return httpx.Response(200, json={"id": 2, "visible_token": "new-secret"})
                active.add(3)
                return httpx.Response(200, json={"id": 3, "visible_token": "next-secret"})
            active.remove(int(request.url.path.rsplit("/", 1)[-1]))
            return httpx.Response(204)

        origin = "https://canvas.example.edu"
        with tempfile.TemporaryDirectory() as directory, httpx.Client(
            base_url=origin, transport=httpx.MockTransport(canvas)
        ) as client:
            path = Path(directory) / "token.json"
            rotation.save(path, {"token": "old-secret", "id": 1, "base_url": origin})
            with patch.multiple(canvas_mcp, CANVAS_TOKEN_FILE=str(path), CANVAS_BASE_URL=origin):
                self.assertEqual(canvas_mcp._headers()["Authorization"], "Bearer old-secret")
                rotation.rotate(client, path, now, check_only=True)
                self.assertEqual(writes, [])
                with self.assertRaisesRegex(ValueError, "expected Canvas account"):
                    rotation.rotate(client, path, now, rotate_now=True)
                self.assertEqual(writes[-1], ("DELETE", "/api/v1/users/self/tokens/2"))
                self.assertEqual(canvas_mcp._headers()["Authorization"], "Bearer old-secret")
                replacement_account = 10
                writes.clear()
                rotation.rotate(client, path, now, rotate_now=True)
                self.assertEqual(canvas_mcp._headers()["Authorization"], "Bearer new-secret")
                count = len(requests)
                rotation.rotate(client, path, now + timedelta(days=19))
                self.assertEqual(len(requests), count)
                rotation.rotate(client, path, now + timedelta(days=20))
            self.assertEqual(writes[1], ("DELETE", "/api/v1/users/self/tokens/1"))
            state = json.loads(path.read_text())
            self.assertEqual(state["id"], 3)
            self.assertEqual(state["next_rotation"], (now + timedelta(days=40)).isoformat())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
