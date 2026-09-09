#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
SERVER_LOG="$RUNTIME_DIR/canvas-http.log"
TUNNEL_LOG="$RUNTIME_DIR/cloudflared.log"
URL_FILE="$RUNTIME_DIR/chatgpt-mcp-url.txt"
PORT="${MCP_PORT:-8765}"
SERVER_SESSION="canvas-mcp-http"
TUNNEL_SESSION="canvas-mcp-tunnel"

mkdir -p "$RUNTIME_DIR"

screen -S "$TUNNEL_SESSION" -X quit >/dev/null 2>&1 || true
screen -S "$SERVER_SESSION" -X quit >/dev/null 2>&1 || true
rm -f "$URL_FILE"
: > "$SERVER_LOG"
: > "$TUNNEL_LOG"

screen -dmS "$SERVER_SESSION" bash -lc \
  "cd '$ROOT_DIR' && MCP_PORT='$PORT' ./venv/bin/python scripts/run_canvas_http.py >> '$SERVER_LOG' 2>&1"

for _ in {1..50}; do
  if nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

if ! nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
  echo "Canvas HTTP MCP server failed to start. See $SERVER_LOG" >&2
  exit 1
fi

screen -dmS "$TUNNEL_SESSION" bash -lc \
  "cd '$ROOT_DIR' && cloudflared tunnel --protocol http2 --url 'http://127.0.0.1:$PORT' --no-autoupdate >> '$TUNNEL_LOG' 2>&1"

for _ in {1..120}; do
  url="$(grep -Eo 'https://[a-z0-9]+-[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | tail -1 || true)"
  if [[ -n "${url:-}" ]]; then
    echo "$url/mcp" > "$URL_FILE"
    echo "ChatGPT Canvas MCP URL: $url/mcp"
    echo "Logs: $SERVER_LOG and $TUNNEL_LOG"
    echo "If the URL is not reachable immediately, wait 15-30 seconds and retry; Cloudflare quick-tunnel DNS can lag."
    exit 0
  fi
  sleep 0.5
done

echo "Cloudflare tunnel did not produce a URL. See $TUNNEL_LOG" >&2
exit 1
