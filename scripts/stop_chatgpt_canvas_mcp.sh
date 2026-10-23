#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"

screen -S canvas-mcp-tunnel -X quit >/dev/null 2>&1 || true
screen -S canvas-mcp-http -X quit >/dev/null 2>&1 || true
listeners="$(lsof -tiTCP:8765 -sTCP:LISTEN || true)"
if [[ -n "$listeners" ]]; then
  kill $listeners >/dev/null 2>&1 || true
fi
rm -f "$RUNTIME_DIR/chatgpt-mcp-url.txt" "$RUNTIME_DIR/http-secret"
