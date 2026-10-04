#!/usr/bin/env bash
set -euo pipefail

MCP_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MCP_ROOT_DIR
MCP_PYTHON="${ALIAS_PYTHON:-$MCP_ROOT_DIR/.venv/bin/python3}"
if ! command -v "$MCP_PYTHON" >/dev/null 2>&1; then
  printf '[mcp] Prepared Python missing; run bash scripts/setup.sh or set ALIAS_PYTHON.\n' >&2
  exit 1
fi
export PATH="$MCP_ROOT_DIR/.tools/caddy/usr/bin:$PATH"
exec "$MCP_PYTHON" -E -s -B "$MCP_ROOT_DIR/scripts/mcp_server.py"
