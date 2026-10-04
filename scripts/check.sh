#!/usr/bin/env bash
set -euo pipefail

CHECK_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CHECK_ROOT_DIR
cd "$CHECK_ROOT_DIR"

for dependency in bash shellcheck python3 flock awk sed tr head tail mktemp nohup; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf '[check] Missing required dependency: %s\n' "$dependency" >&2
    exit 1
  fi
done

if [[ -n "${AGENT_RUNNER_DEPENDENCIES:-}" ]]; then
  # Runner provides both this read-only artifact directory and private scratch.
  if [[ -z "${TMPDIR:-}" || ! -d "$TMPDIR" ]]; then
    printf '[check] Runner artifact preparation requires TMPDIR scratch\n' >&2
    exit 1
  fi
  CHECK_SCRATCH="$(mktemp -d "$TMPDIR/cloudflared-alias-check.XXXXXX")"
  trap 'rm -rf -- "$CHECK_SCRATCH"' EXIT
  export TMPDIR="$CHECK_SCRATCH"
  bash scripts/setup.sh --offline --artifacts "$AGENT_RUNNER_DEPENDENCIES" \
    --tools "$CHECK_SCRATCH/tools" --venv "$CHECK_SCRATCH/venv" --skip-mcp-registration
  CHECK_PYTHON="$CHECK_SCRATCH/venv/bin/python3"
  CHECK_CADDY="$CHECK_SCRATCH/tools/caddy/usr/bin/caddy"
else
  CHECK_PYTHON="$CHECK_ROOT_DIR/.venv/bin/python3"
  CHECK_CADDY="$CHECK_ROOT_DIR/.tools/caddy/usr/bin/caddy"
fi
bash scripts/check-env.sh --python "$CHECK_PYTHON" --caddy "$CHECK_CADDY"
export PATH="${CHECK_PYTHON%/*}:${CHECK_CADDY%/*}:$PATH"
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1

for script in scripts/*.sh; do
  bash -n "$script"
done
shellcheck scripts/*.sh
PYTHONDONTWRITEBYTECODE=1 "$CHECK_PYTHON" -m unittest discover -s tests -v
