#!/usr/bin/env bash
set -euo pipefail

ENV_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_ROOT_DIR
exec python3 -I "$ENV_ROOT_DIR/scripts/toolchain.py" verify "$@"
