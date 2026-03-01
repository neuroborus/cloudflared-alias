#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RUNTIME_DIR="${ROOT_DIR}/.runtime"
RUNTIME_CADDY_DIR="${RUNTIME_DIR}/caddy"
RUNTIME_CLOUDFLARED_DIR="${RUNTIME_DIR}/cloudflared"

CADDY_TEMPLATE="${ROOT_DIR}/deploy/caddy/Caddyfile.template"
CF_TEMPLATE="${ROOT_DIR}/deploy/cloudflared/config.template.yml"

RUNTIME_CADDYFILE="${RUNTIME_CADDY_DIR}/Caddyfile"
RUNTIME_CF_CONFIG="${RUNTIME_CLOUDFLARED_DIR}/config.yml"
CURRENT_URL_FILE="${RUNTIME_DIR}/current-share-url.txt"
CURRENT_ID_FILE="${RUNTIME_DIR}/current-path-id.txt"

CADDY_PID_FILE="${RUNTIME_CADDY_DIR}/caddy.pid"
CF_PID_FILE="${RUNTIME_CLOUDFLARED_DIR}/cloudflared.pid"

CADDY_LOG="${RUNTIME_CADDY_DIR}/caddy.log"
CF_LOG="${RUNTIME_CLOUDFLARED_DIR}/cloudflared.log"

SOURCE_CF_CONFIG="${CLOUDFLARED_BASE_CONFIG:-$HOME/.cloudflared/config.yml}"
CADDY_PORT="${CADDY_PORT:-8080}"
ID_LENGTH="${ID_LENGTH:-12}"
DETACH="${DETACH:-0}"

CLEANED_UP=0

log() {
  printf '[run-tunnel-with-caddy] %s\n' "$*"
}

fail() {
  printf '[run-tunnel-with-caddy] Error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<USAGE
Usage:
  $(basename "$0") <backend_port>
  $(basename "$0") stop

Environment variables:
  CADDY_PORT              Local Caddy listen port (default: 8080)
  ID_LENGTH               Random path ID length (default: 12)
  DETACH                  Run in background and exit immediately (default: 0)
  CLOUDFLARED_BASE_CONFIG Source Cloudflare config (default: ~/.cloudflared/config.yml)
  TUNNEL_NAME             Override tunnel name from source config
  TUNNEL_HOSTNAME         Override hostname from source config
  TUNNEL_CREDENTIALS_FILE Override credentials-file from source config
USAGE
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "'$cmd' is required but not installed."
}

ensure_dirs() {
  mkdir -p "$RUNTIME_CADDY_DIR" "$RUNTIME_CLOUDFLARED_DIR"
}

is_pid_running() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

stop_pid_file_if_running() {
  local label="$1"
  local pid_file="$2"

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"

    if [[ -n "$pid" ]] && is_pid_running "$pid"; then
      log "Stopping existing ${label} process (pid ${pid})"
      kill "$pid" 2>/dev/null || true
      sleep 1
      if is_pid_running "$pid"; then
        log "Force stopping ${label} process (pid ${pid})"
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi

    rm -f "$pid_file"
  fi
}

port_in_use() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :${port} )" | tail -n +2 | grep -q .
    return $?
  fi

  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk -v p=":${port}$" '$4 ~ p { found=1 } END { exit(found ? 0 : 1) }'
    return $?
  fi

  return 1
}

validate_port() {
  local label="$1"
  local value="$2"

  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    fail "${label} must be an integer, got '${value}'."
  fi

  if (( value < 1 || value > 65535 )); then
    fail "${label} must be between 1 and 65535, got '${value}'."
  fi
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

read_source_tunnel_values() {
  [[ -f "$SOURCE_CF_CONFIG" ]] || fail "Cloudflare config not found at '${SOURCE_CF_CONFIG}'."

  local tunnel_name="${TUNNEL_NAME:-}"
  local hostname="${TUNNEL_HOSTNAME:-}"
  local credentials_file="${TUNNEL_CREDENTIALS_FILE:-}"

  if [[ -z "$tunnel_name" ]]; then
    tunnel_name="$(awk -F': *' '/^tunnel:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
    tunnel_name="$(trim "$tunnel_name")"
  fi

  if [[ -z "$credentials_file" ]]; then
    credentials_file="$(awk -F': *' '/^credentials-file:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
    credentials_file="$(trim "$credentials_file")"
  fi

  if [[ -z "$hostname" ]]; then
    hostname="$(awk -F': *' '/hostname:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
    hostname="$(trim "$hostname")"
  fi

  [[ -n "$tunnel_name" ]] || fail "Tunnel name is missing. Set 'tunnel:' in '${SOURCE_CF_CONFIG}' or export TUNNEL_NAME."
  [[ -n "$credentials_file" ]] || fail "credentials-file is missing. Set it in '${SOURCE_CF_CONFIG}' or export TUNNEL_CREDENTIALS_FILE."
  [[ -n "$hostname" ]] || fail "Ingress hostname is missing. Add a hostname entry in '${SOURCE_CF_CONFIG}' or export TUNNEL_HOSTNAME."

  TUNNEL_NAME_VALUE="$tunnel_name"
  HOSTNAME_VALUE="$hostname"
  CREDENTIALS_FILE_VALUE="$credentials_file"
}

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

render_template() {
  local template="$1"
  local output="$2"

  local path_id_escaped
  local backend_port_escaped
  local caddy_port_escaped
  local tunnel_name_escaped
  local credentials_file_escaped
  local hostname_escaped

  path_id_escaped="$(escape_sed_replacement "$PATH_ID")"
  backend_port_escaped="$(escape_sed_replacement "$BACKEND_PORT")"
  caddy_port_escaped="$(escape_sed_replacement "$CADDY_PORT")"
  tunnel_name_escaped="$(escape_sed_replacement "$TUNNEL_NAME_VALUE")"
  credentials_file_escaped="$(escape_sed_replacement "$CREDENTIALS_FILE_VALUE")"
  hostname_escaped="$(escape_sed_replacement "$HOSTNAME_VALUE")"

  sed \
    -e "s|__PATH_ID__|${path_id_escaped}|g" \
    -e "s|__BACKEND_PORT__|${backend_port_escaped}|g" \
    -e "s|__CADDY_PORT__|${caddy_port_escaped}|g" \
    -e "s|__TUNNEL_NAME__|${tunnel_name_escaped}|g" \
    -e "s|__CREDENTIALS_FILE__|${credentials_file_escaped}|g" \
    -e "s|__HOSTNAME__|${hostname_escaped}|g" \
    "$template" > "$output"
}

generate_path_id() {
  local len="$1"
  local id

  id="$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c "$len" || true)"
  [[ "${#id}" -eq "$len" ]] || fail "Failed to generate a random path ID."

  PATH_ID="$id"
}

start_caddy() {
  log "Starting Caddy on localhost:${CADDY_PORT}"
  caddy run --config "$RUNTIME_CADDYFILE" --adapter caddyfile >"$CADDY_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$CADDY_PID_FILE"

  sleep 1
  is_pid_running "$pid" || fail "Caddy failed to start. Check '${CADDY_LOG}'."
}

start_cloudflared() {
  log "Starting cloudflared tunnel '${TUNNEL_NAME_VALUE}'"
  cloudflared --config "$RUNTIME_CF_CONFIG" tunnel run "$TUNNEL_NAME_VALUE" >"$CF_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$CF_PID_FILE"

  sleep 1
  is_pid_running "$pid" || fail "cloudflared failed to start. Check '${CF_LOG}'."
}

stop_all() {
  stop_pid_file_if_running "cloudflared" "$CF_PID_FILE"
  stop_pid_file_if_running "Caddy" "$CADDY_PID_FILE"
  log "Stopped runtime Caddy/cloudflared processes (if they were running)."
}

cleanup_on_exit() {
  if [[ "$CLEANED_UP" -eq 1 ]]; then
    return
  fi
  CLEANED_UP=1
  stop_all
}

wait_for_children() {
  local caddy_pid="$1"
  local cloudflared_pid="$2"

  log "Running in foreground. Press Ctrl+C to stop."

  while true; do
    if ! is_pid_running "$caddy_pid"; then
      fail "Caddy exited unexpectedly. Check '${CADDY_LOG}'."
    fi
    if ! is_pid_running "$cloudflared_pid"; then
      fail "cloudflared exited unexpectedly. Check '${CF_LOG}'."
    fi
    sleep 1
  done
}

main() {
  if [[ $# -ne 1 ]]; then
    usage
    exit 1
  fi

  [[ -f "$CADDY_TEMPLATE" ]] || fail "Missing Caddy template: ${CADDY_TEMPLATE}"
  [[ -f "$CF_TEMPLATE" ]] || fail "Missing cloudflared template: ${CF_TEMPLATE}"

  if [[ "$1" == "stop" ]]; then
    ensure_dirs
    stop_all
    exit 0
  fi

  require_cmd caddy
  require_cmd cloudflared
  require_cmd awk
  require_cmd sed
  require_cmd tr
  require_cmd head

  BACKEND_PORT="$1"

  validate_port "backend port" "$BACKEND_PORT"
  validate_port "CADDY_PORT" "$CADDY_PORT"

  if (( ID_LENGTH < 10 || ID_LENGTH > 32 )); then
    fail "ID_LENGTH must be between 10 and 32 (recommended 10-12)."
  fi
  if ! [[ "$DETACH" =~ ^[01]$ ]]; then
    fail "DETACH must be 0 or 1."
  fi

  ensure_dirs
  read_source_tunnel_values
  generate_path_id "$ID_LENGTH"

  if port_in_use "$CADDY_PORT"; then
    log "Port ${CADDY_PORT} is currently in use; attempting to stop previous runtime Caddy process first."
  fi

  stop_all

  if port_in_use "$CADDY_PORT"; then
    fail "Port ${CADDY_PORT} is already in use by another process. Set a different CADDY_PORT."
  fi

  render_template "$CADDY_TEMPLATE" "$RUNTIME_CADDYFILE"
  render_template "$CF_TEMPLATE" "$RUNTIME_CF_CONFIG"

  start_caddy
  start_cloudflared

  local share_url
  share_url="https://${HOSTNAME_VALUE}/${PATH_ID}/"

  printf '%s\n' "$share_url" > "$CURRENT_URL_FILE"
  printf '%s\n' "$PATH_ID" > "$CURRENT_ID_FILE"

  log "Tunnel hostname : ${HOSTNAME_VALUE}"
  log "Generated path ID: ${PATH_ID}"
  log "Share URL       : ${share_url}"
  log "Runtime files   : ${RUNTIME_DIR}"
  log "Logs            : ${CADDY_LOG}, ${CF_LOG}"

  if [[ "$DETACH" -eq 1 ]]; then
    log "Detached mode enabled (DETACH=1); processes continue in background."
    exit 0
  fi

  trap cleanup_on_exit EXIT HUP INT TERM
  wait_for_children "$(cat "$CADDY_PID_FILE")" "$(cat "$CF_PID_FILE")"
}

main "$@"
