#!/usr/bin/env bash
set -euo pipefail

SETUP_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SETUP_ROOT_DIR
# The bootstrap interpreter uses only its standard library, never host packages.
exec python3 -I "$SETUP_ROOT_DIR/scripts/toolchain.py" prepare "$@"
