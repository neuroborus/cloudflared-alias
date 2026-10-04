#!/usr/bin/env bash
set -euo pipefail

SETUP_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SETUP_ROOT_DIR
SETUP_ARGUMENTS=()
SETUP_MCP_REGISTRATION=1
for argument in "$@"; do
  if [[ "$argument" == --skip-mcp-registration ]]; then
    SETUP_MCP_REGISTRATION=0
  else
    SETUP_ARGUMENTS+=("$argument")
    if [[ "$argument" == --help || "$argument" == -h ]]; then
      SETUP_MCP_REGISTRATION=0
      printf '[setup] --skip-mcp-registration prepares only the toolchain.\n'
    fi
  fi
done
# The bootstrap interpreter uses only its standard library, never host packages.
python3 -I "$SETUP_ROOT_DIR/scripts/toolchain.py" prepare "${SETUP_ARGUMENTS[@]}"
if [[ "$SETUP_MCP_REGISTRATION" == 1 ]]; then
  exec python3 -I "$SETUP_ROOT_DIR/scripts/prepare_mcp.py"
fi
