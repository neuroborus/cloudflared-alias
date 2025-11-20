#!/usr/bin/env bash

set -euo pipefail

# Name of the Cloudflare tunnel to use (!: CHANGE IT)
TUNNEL_NAME="localhost-tunnel"

# Config file that this script edits and passes to cloudflared
CONFIG="$HOME/.cloudflared/config.yml"
BACKUP="$HOME/.cloudflared/config.yml.bak"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <port>"
  exit 1
fi

PORT="$1"

# Validate that port is a positive integer
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Error: port must be an integer"
  exit 1
fi

# Ensure that the config contains the desired tunnel
if ! grep -qE "^tunnel:[[:space:]]*$TUNNEL_NAME" "$CONFIG"; then
  echo "Error: tunnel '$TUNNEL_NAME' not found in $CONFIG"
  exit 1
fi

echo ">>> Updating ingress service port to $PORT"

# Create a backup only once
if [ ! -f "$BACKUP" ]; then
  cp "$CONFIG" "$BACKUP"
fi

# Update 'service: http://...:<port>' for HTTP ingress line
# This keeps YAML indentation and replaces host+port with localhost:<PORT>
sed -i -E \
  "s@(service:[[:space:]]*http://)[^:]+:[0-9]+@\1localhost:${PORT}@" \
  "$CONFIG"

# Read updated port from the first localhost service line
CURRENT_PORT=$(
  awk '/service:[[:space:]]*http:\/\/localhost:/ {
    if (match($0, /localhost:([0-9]+)/, m)) { print m[1]; exit }
  }' "$CONFIG"
)

# Read first hostname from ingress section
HOSTNAME=$(
  awk '/hostname:/ { print $NF; exit }' "$CONFIG"
)

echo "Port: $CURRENT_PORT"
echo "Hostname: $HOSTNAME"

echo ">>> Killing existing cloudflared processes (if any)..."
pkill -x cloudflared 2>/dev/null || true

echo ">>> Starting Cloudflare tunnel with explicit config: $CONFIG"
# Important: force cloudflared to use THIS config file
cloudflared --config "$CONFIG" tunnel run "$TUNNEL_NAME"

