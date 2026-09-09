#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PROFILE_DIR="$RUNTIME_DIR/tunnel-client-profiles"
HEALTH_URL_FILE="$RUNTIME_DIR/openai-tunnel-health.url"
LOG_FILE="$RUNTIME_DIR/openai-tunnel.log"
TUNNEL_CLIENT="$RUNTIME_DIR/bin/tunnel-client"

if [[ ! -x "$TUNNEL_CLIENT" ]]; then
  echo "Missing $TUNNEL_CLIENT. Install it with: gh release download -R openai/tunnel-client v0.0.10 -p tunnel-client-v0.0.10-darwin-arm64.zip -D .runtime/downloads" >&2
  exit 1
fi

if [[ -z "${CONTROL_PLANE_TUNNEL_ID:-}" ]]; then
  echo "Set CONTROL_PLANE_TUNNEL_ID to the tunnel_... value from https://platform.openai.com/settings/organization/tunnels" >&2
  exit 1
fi

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "Set CONTROL_PLANE_API_KEY to a runtime API key with Tunnels Read + Use." >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"

"$TUNNEL_CLIENT" init \
  --force \
  --sample sample_mcp_stdio_local \
  --profile canvas \
  --profile-dir "$PROFILE_DIR" \
  --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
  --mcp-command "$ROOT_DIR/venv/bin/python $ROOT_DIR/canvas_mcp.py"

"$TUNNEL_CLIENT" doctor --profile canvas --profile-dir "$PROFILE_DIR" --explain

"$TUNNEL_CLIENT" run \
  --profile canvas \
  --profile-dir "$PROFILE_DIR" \
  --health.listen-addr 127.0.0.1:0 \
  --health.url-file "$HEALTH_URL_FILE" \
  --log.file "$LOG_FILE"
