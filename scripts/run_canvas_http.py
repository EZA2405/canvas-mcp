#!/usr/bin/env python3
"""Launch the Canvas MCP server over streamable HTTP for ChatGPT dev connectors."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    claude_config = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    with claude_config.open() as f:
        canvas_env = json.load(f)["mcpServers"]["canvas"]["env"]

    port = os.environ.get("MCP_PORT", "8765")
    env = os.environ.copy()
    env.update(canvas_env)

    os.execve(
        str(repo / "venv/bin/python"),
        [
            str(repo / "venv/bin/python"),
            str(repo / "canvas_mcp.py"),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--path",
            "/mcp",
            "--dev-allow-tunnel-hosts",
        ],
        env,
    )


if __name__ == "__main__":
    main()
