#!/usr/bin/env bash
set -euo pipefail

CHECK_ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CHECK_ROOT_DIR
cd "$CHECK_ROOT_DIR"

for dependency in bash shellcheck python3 caddy flock awk sed tr head tail mktemp nohup; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    printf '[check] Missing required dependency: %s\n' "$dependency" >&2
    exit 1
  fi
done

for script in scripts/*.sh; do
  bash -n "$script"
done
shellcheck scripts/*.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
