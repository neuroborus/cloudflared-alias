#!/usr/bin/env bash

set -euo pipefail

readonly ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

readonly RUNTIME_DIR="${ROOT_DIR}/.runtime"
readonly RUNTIME_CADDY_DIR="${RUNTIME_DIR}/caddy"
readonly RUNTIME_CLOUDFLARED_DIR="${RUNTIME_DIR}/cloudflared"
CADDY_TEMPLATE="${ROOT_DIR}/deploy/caddy/Caddyfile.template"
readonly CADDY_SUBDOMAIN_TEMPLATE="${ROOT_DIR}/deploy/caddy/Caddyfile.subdomain.template"
readonly CADDY_PATH_NOKEY_TEMPLATE="${ROOT_DIR}/deploy/caddy/Caddyfile.path-nokey.template"
readonly CF_TEMPLATE="${ROOT_DIR}/deploy/cloudflared/config.template.yml"
readonly RUNTIME_CADDYFILE="${RUNTIME_CADDY_DIR}/Caddyfile"
readonly RUNTIME_CF_CONFIG="${RUNTIME_CLOUDFLARED_DIR}/config.yml"
readonly CURRENT_URL_FILE="${RUNTIME_DIR}/current-share-url.txt"
readonly CURRENT_ID_FILE="${RUNTIME_DIR}/current-path-id.txt"
readonly CADDY_PID_FILE="${RUNTIME_CADDY_DIR}/caddy.pid"
readonly CF_PID_FILE="${RUNTIME_CLOUDFLARED_DIR}/cloudflared.pid"
readonly CADDY_LOG="${RUNTIME_CADDY_DIR}/caddy.log"
readonly CF_LOG="${RUNTIME_CLOUDFLARED_DIR}/cloudflared.log"
readonly CONFIG_FILE="${ROOT_DIR}/cloudflared-alias.conf"
readonly HISTORY_FILE="${RUNTIME_DIR}/tunnel-history"
readonly HISTORY_MAX=10
readonly REGISTRY_FILE="${RUNTIME_DIR}/registry"
readonly INSTANCES_DIR="${RUNTIME_DIR}/instances"

# Registry line: mode \t path_id \t backend_port \t caddy_port \t caddy_pid \t instance_dir

CLEANED_UP=0

load_config() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  local key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(trim "$line")"
    [[ -z "$line" ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="$(trim "$val")"
      val="${val#\"}"
      val="${val%\"}"
      case "$key" in
        DEFAULT_MODE) [[ ! -v DEFAULT_MODE ]] && DEFAULT_MODE="$val" ;;
        CADDY_PORT)   [[ ! -v CADDY_PORT ]]   && CADDY_PORT="$val" ;;
        ID_LENGTH)    [[ ! -v ID_LENGTH ]]    && ID_LENGTH="$val" ;;
        DETACH)       [[ ! -v DETACH ]]       && DETACH="$val" ;;
        SUBDOMAIN_DOMAIN) [[ ! -v SUBDOMAIN_DOMAIN ]] && SUBDOMAIN_DOMAIN="$val" ;;
        CLOUDFLARED_BASE_CONFIG) [[ ! -v CLOUDFLARED_BASE_CONFIG ]] && CLOUDFLARED_BASE_CONFIG="$val" ;;
      esac
    fi
  done < "$CONFIG_FILE"
}

log() {
  printf '[tunnel] %s\n' "$*"
}

fail() {
  printf '[tunnel] Error: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<USAGE
Usage:
  $(basename "$0") [options] <backend_port> [key]
  $(basename "$0") stop
  $(basename "$0") history

  history, -h     Show last ${HISTORY_MAX} tunnels and pick one interactively.

Modes: -p/--path | -s/--subdomain | -n/--no-key  (else from config DEFAULT_MODE)
  key              Optional. Used as path/subdomain key; else random.

Config: $(basename "$ROOT_DIR")/cloudflared-alias.conf

Environment: CADDY_PORT, ID_LENGTH, DETACH, CLOUDFLARED_BASE_CONFIG, SUBDOMAIN_DOMAIN, …
USAGE
}

# Format: mode \t path_id \t backend_port \t share_url \t created_ts \t last_used_ts
add_to_history() {
  local mode="$1"
  local path_id="$2"
  local backend_port="$3"
  local share_url="$4"
  local created_ts="${5:-$(date +%s)}"
  local last_used_ts="${6:-$created_ts}"
  ensure_dirs
  local new_line="${mode}	${path_id}	${backend_port}	${share_url}	${created_ts}	${last_used_ts}"
  if [[ -f "$HISTORY_FILE" ]]; then
    { printf '%s\n' "$new_line"; head -n $(( HISTORY_MAX - 1 )) "$HISTORY_FILE"; } > "${HISTORY_FILE}.tmp"
    mv "${HISTORY_FILE}.tmp" "$HISTORY_FILE"
  else
    printf '%s\n' "$new_line" > "$HISTORY_FILE"
  fi
}

format_ts() {
  local ts="$1"
  if date -d "@${ts}" +'%Y-%m-%d %H:%M' 2>/dev/null; then
    return 0
  fi
  date -r "${ts}" +'%Y-%m-%d %H:%M' 2>/dev/null || printf '%s' "$ts"
}

list_history() {
  ensure_dirs
  if [[ ! -f "$HISTORY_FILE" ]] || ! [[ -s "$HISTORY_FILE" ]]; then
    log "No tunnel history yet."
    return 0
  fi
  local n=1
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local mode path_id port url created_ts last_used_ts
    IFS=$'\t' read -r mode path_id port url created_ts last_used_ts <<< "$line"
    [[ -z "$last_used_ts" ]] && last_used_ts="$created_ts"
    printf '  %d) %s  %s  port %s  last used %s\n' "$n" "$url" "$mode" "$port" "$(format_ts "$last_used_ts")"
    n=$(( n + 1 ))
  done < "$HISTORY_FILE"
}

get_history_entry() {
  local n="${1:-1}"
  [[ -f "$HISTORY_FILE" ]] || return 1
  sed -n "${n}p" "$HISTORY_FILE"
}

remove_history_entry() {
  local n="$1"
  [[ -f "$HISTORY_FILE" ]] || return 0
  sed "${n}d" "$HISTORY_FILE" > "${HISTORY_FILE}.tmp" && mv "${HISTORY_FILE}.tmp" "$HISTORY_FILE"
}

history_pick_interactive() {
  load_config
  ensure_dirs
  if [[ ! -f "$HISTORY_FILE" ]] || ! [[ -s "$HISTORY_FILE" ]]; then
    fail "No tunnel history yet. Start a tunnel first."
  fi
  local count
  count="$(wc -l < "$HISTORY_FILE")"
  log "Last ${HISTORY_MAX} tunnels:"
  list_history
  printf '\n[tunnel] Select number (1-%s) or Enter to cancel: ' "$count"
  read -r choice
  choice="$(trim "$choice")"
  [[ -z "$choice" ]] && { log "Cancelled."; exit 0; }
  [[ "$choice" =~ ^[0-9]+$ ]] || fail "Invalid choice."
  [[ "$choice" -ge 1 && "$choice" -le "$count" ]] || fail "No such entry."
  local line
  line="$(get_history_entry "$choice")" || fail "No history entry."
  local share_url created_ts last_used_ts
  IFS=$'\t' read -r MODE PATH_ID_ARG BACKEND_PORT share_url created_ts last_used_ts <<< "$line"
  [[ -z "$last_used_ts" ]] && last_used_ts="$created_ts"
  printf '[tunnel] Backend port [%s]: ' "$BACKEND_PORT"
  read -r port_override
  port_override="$(trim "$port_override")"
  [[ -n "$port_override" ]] && BACKEND_PORT="$port_override"
  remove_history_entry "$choice"
  add_to_history "$MODE" "$PATH_ID_ARG" "$BACKEND_PORT" "$share_url" "$created_ts" "$(date +%s)"
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "'$cmd' is required but not installed."
}

ensure_dirs() {
  mkdir -p "$RUNTIME_CADDY_DIR" "$RUNTIME_CLOUDFLARED_DIR" "$INSTANCES_DIR"
}

read_registry() {
  [[ -f "$REGISTRY_FILE" ]] && cat "$REGISTRY_FILE" || true
}

get_next_free_caddy_port() {
  local base="${1:-9090}"
  local port="$base"
  while true; do
    local in_use=0
    port_in_use "$port" && in_use=1
    if [[ "$in_use" -eq 0 ]]; then
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local _mode _path _backend reg_caddy
        IFS=$'\t' read -r _mode _path _backend reg_caddy _ _ <<< "$line"
        if [[ "$reg_caddy" == "$port" ]]; then
          in_use=1
          break
        fi
      done < <(read_registry)
    fi
    [[ "$in_use" -eq 0 ]] && { printf '%s' "$port"; return 0; }
    port=$(( port + 1 ))
    [[ "$port" -gt 65535 ]] && fail "No free Caddy port found from ${base}."
  done
}

# Remove registry entries where path_id or backend_port conflicts; stop their Caddy processes and notify.
registry_remove_conflicting() {
  local mode="$1"
  local path_id="$2"
  local backend_port="$3"
  local stopped=0
  local tmp
  tmp="$(mktemp)"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local reg_mode reg_path reg_backend reg_caddy reg_pid reg_dir
    IFS=$'\t' read -r reg_mode reg_path reg_backend reg_caddy reg_pid reg_dir <<< "$line"
    local conflict=0
    if [[ "$backend_port" == "$reg_backend" ]]; then
      conflict=1
    elif [[ "$mode" == "no-key" && ( "$reg_mode" == "no-key" || -z "$reg_path" ) ]]; then
      conflict=1
    elif [[ -n "$path_id" && "$path_id" == "$reg_path" ]]; then
      conflict=1
    fi
    if [[ "$conflict" -eq 1 ]]; then
      stopped=1
      if [[ -n "$reg_pid" ]] && is_pid_running "$reg_pid" 2>/dev/null; then
        log "Stopping previous tunnel (key=${reg_path:-<no-key>}, port=${reg_backend}) — same key or port."
        kill "$reg_pid" 2>/dev/null || true
        sleep 1
        is_pid_running "$reg_pid" 2>/dev/null && kill -9 "$reg_pid" 2>/dev/null || true
      fi
      rm -f "${reg_dir}/caddy.pid" 2>/dev/null || true
      continue
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < <(read_registry)
  if [[ "$stopped" -eq 1 ]]; then
    [[ -s "$tmp" ]] && mv "$tmp" "$REGISTRY_FILE" || rm -f "$REGISTRY_FILE" "$tmp"
    if [[ -f "$REGISTRY_FILE" && -s "$REGISTRY_FILE" ]]; then
      rebuild_cloudflared_from_registry
      restart_cloudflared_if_running
    fi
  else
    rm -f "$tmp"
  fi
}

registry_add() {
  local mode="$1"
  local path_id="$2"
  local backend_port="$3"
  local caddy_port="$4"
  local caddy_pid="$5"
  local instance_dir="$6"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$mode" "$path_id" "$backend_port" "$caddy_port" "$caddy_pid" "$instance_dir" >> "$REGISTRY_FILE"
}

rebuild_cloudflared_from_registry() {
  [[ -f "$SOURCE_CF_CONFIG" ]] || return 1
  local tunnel_name credentials_file hostname_val
  tunnel_name="$(awk -F': *' '/^tunnel:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
  tunnel_name="$(trim "$tunnel_name")"
  credentials_file="$(awk -F': *' '/^credentials-file:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
  credentials_file="$(trim "$credentials_file")"
  hostname_val="$(awk -F': *' '/hostname:/ {print $2; exit}' "$SOURCE_CF_CONFIG" || true)"
  hostname_val="$(trim "$hostname_val")"
  local subdomain_domain="${SUBDOMAIN_DOMAIN:-${hostname_val#\*\.}}"
  {
    printf 'tunnel: %s\n' "$tunnel_name"
    printf 'credentials-file: %s\n\n' "$credentials_file"
    printf 'ingress:\n'
    read_registry | while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      local reg_mode reg_path reg_backend reg_caddy _ _
      IFS=$'\t' read -r reg_mode reg_path reg_backend reg_caddy _ _ <<< "$line"
      if [[ "$reg_mode" == "subdomain" ]]; then
        printf '  - hostname: "%s.%s"\n' "$reg_path" "$subdomain_domain"
      elif [[ "$reg_mode" == "no-key" || -z "$reg_path" ]]; then
        printf '  - hostname: "%s"\n' "$hostname_val"
      else
        printf '  - hostname: "%s"\n    path: "/%s/*"\n' "$hostname_val" "$reg_path"
      fi
      printf '    service: http://localhost:%s\n' "$reg_caddy"
    done
    printf '  - service: http_status:404\n'
  } > "$RUNTIME_CF_CONFIG"
}

restart_cloudflared_if_running() {
  if [[ -f "$CF_PID_FILE" ]]; then
    local pid
    pid="$(cat "$CF_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && is_pid_running "$pid"; then
      log "Restarting cloudflared (config updated)."
      kill "$pid" 2>/dev/null || true
      sleep 2
      is_pid_running "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      rm -f "$CF_PID_FILE"
    fi
  fi
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
  local hostname_override="${3:-}"

  local path_id_escaped
  local backend_port_escaped
  local caddy_port_escaped
  local tunnel_name_escaped
  local credentials_file_escaped
  local hostname_escaped
  local subdomain_host_escaped

  path_id_escaped="$(escape_sed_replacement "$PATH_ID")"
  backend_port_escaped="$(escape_sed_replacement "$BACKEND_PORT")"
  caddy_port_escaped="$(escape_sed_replacement "$CADDY_PORT")"
  tunnel_name_escaped="$(escape_sed_replacement "$TUNNEL_NAME_VALUE")"
  credentials_file_escaped="$(escape_sed_replacement "$CREDENTIALS_FILE_VALUE")"
  hostname_escaped="$(escape_sed_replacement "${hostname_override:-$HOSTNAME_VALUE}")"
  subdomain_host_escaped="$(escape_sed_replacement "${SUBDOMAIN_HOST_VALUE:-__SUBDOMAIN_HOST__}")"

  sed \
    -e "s|__PATH_ID__|${path_id_escaped}|g" \
    -e "s|__BACKEND_PORT__|${backend_port_escaped}|g" \
    -e "s|__CADDY_PORT__|${caddy_port_escaped}|g" \
    -e "s|__TUNNEL_NAME__|${tunnel_name_escaped}|g" \
    -e "s|__CREDENTIALS_FILE__|${credentials_file_escaped}|g" \
    -e "s|__HOSTNAME__|${hostname_escaped}|g" \
    -e "s|__SUBDOMAIN_HOST__|${subdomain_host_escaped}|g" \
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
  local config_file="${1:-$RUNTIME_CADDYFILE}"
  local pid_file="${2:-$CADDY_PID_FILE}"
  local log_file="${3:-$CADDY_LOG}"
  log "Starting Caddy on localhost:${CADDY_PORT}"
  caddy run --config "$config_file" --adapter caddyfile >"$log_file" 2>&1 &
  local pid=$!
  echo "$pid" > "$pid_file"

  sleep 1
  is_pid_running "$pid" || fail "Caddy failed to start. Check '${log_file}'."
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
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local _ _ _ _ caddy_pid _
    IFS=$'\t' read -r _ _ _ _ caddy_pid _ <<< "$line"
    if [[ -n "$caddy_pid" ]] && is_pid_running "$caddy_pid" 2>/dev/null; then
      log "Stopping Caddy (pid ${caddy_pid})"
      kill "$caddy_pid" 2>/dev/null || true
      sleep 1
      is_pid_running "$caddy_pid" 2>/dev/null && kill -9 "$caddy_pid" 2>/dev/null || true
    fi
  done < <(read_registry)
  rm -f "$REGISTRY_FILE"
  stop_pid_file_if_running "cloudflared" "$CF_PID_FILE"
  log "Stopped runtime Caddy/cloudflared processes (if they were running)."
}

remove_instance_from_registry() {
  local instance_dir="$1"
  [[ -z "$instance_dir" ]] && return 0
  [[ ! -f "$REGISTRY_FILE" ]] && return 0
  local tmp
  tmp="$(mktemp)"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local _ _ _ _ _ reg_dir
    IFS=$'\t' read -r _ _ _ _ _ reg_dir <<< "$line"
    [[ "$reg_dir" == "$instance_dir" ]] && continue
    printf '%s\n' "$line" >> "$tmp"
  done < "$REGISTRY_FILE"
  mv "$tmp" "$REGISTRY_FILE"
}

# Remove registry entries whose Caddy process is no longer running (stale entries).
prune_dead_registry_entries() {
  [[ ! -f "$REGISTRY_FILE" ]] || [[ ! -s "$REGISTRY_FILE" ]] && return 0
  local tmp
  tmp="$(mktemp)"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local _ _ _ _ reg_pid _
    IFS=$'\t' read -r _ _ _ _ reg_pid _ <<< "$line"
    [[ -n "$reg_pid" ]] && is_pid_running "$reg_pid" 2>/dev/null && printf '%s\n' "$line" >> "$tmp"
  done < "$REGISTRY_FILE"
  mv "$tmp" "$REGISTRY_FILE"
}

cleanup_on_exit() {
  if [[ "$CLEANED_UP" -eq 1 ]]; then
    return
  fi
  CLEANED_UP=1
  if [[ -n "${CURRENT_INSTANCE_DIR:-}" ]]; then
    local pid_file="${CURRENT_INSTANCE_DIR}/caddy.pid"
    if [[ -f "$pid_file" ]]; then
      local pid
      pid="$(cat "$pid_file" 2>/dev/null || true)"
      if [[ -n "$pid" ]] && is_pid_running "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        sleep 1
        is_pid_running "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      fi
      rm -f "$pid_file"
    fi
    remove_instance_from_registry "$CURRENT_INSTANCE_DIR"
    prune_dead_registry_entries
    if [[ -f "$REGISTRY_FILE" && -s "$REGISTRY_FILE" ]]; then
      rebuild_cloudflared_from_registry
      restart_cloudflared_if_running
      sleep 1
      is_pid_running "$(cat "$CF_PID_FILE" 2>/dev/null)" 2>/dev/null || start_cloudflared
    else
      stop_pid_file_if_running "cloudflared" "$CF_PID_FILE"
    fi
  else
    stop_all
  fi
}

# Wait only for our Caddy process. Cloudflared is shared and may be restarted when other tunnels start/stop — we do not treat that as this tunnel failing.
wait_for_children() {
  local caddy_pid="$1"
  local caddy_log="${2:-$CADDY_LOG}"

  log "Running in foreground. Press Ctrl+C to stop."

  while true; do
    if ! is_pid_running "$caddy_pid"; then
      fail "Caddy exited unexpectedly. Check '${caddy_log}'."
    fi
    sleep 1
  done
}

validate_path_id() {
  local id="$1"
  local len="${#id}"
  if (( len < 1 || len > 32 )); then
    fail "Key length must be 1–32, got ${len}."
  fi
  if ! [[ "$id" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    fail "Key must be lowercase alphanumeric and hyphens only (e.g. my-key-1)."
  fi
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
    exit 1
  fi

  if [[ $# -eq 1 && "$1" == "stop" ]]; then
    ensure_dirs
    stop_all
    exit 0
  fi

  load_config
  SOURCE_CF_CONFIG="${CLOUDFLARED_BASE_CONFIG:-$HOME/.cloudflared/config.yml}"
  CADDY_PORT="${CADDY_PORT:-9090}"
  ID_LENGTH="${ID_LENGTH:-4}"
  DETACH="${DETACH:-0}"
  DEFAULT_MODE="${DEFAULT_MODE:-path}"

  BACKEND_PORT=""
  PATH_ID_ARG=""
  MODE=""

  if [[ $# -ge 1 && ( "$1" == "history" || "$1" == "-h" ) ]]; then
    history_pick_interactive
    shift
  else
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -p|--path)      MODE="path" ;;
        -s|--subdomain) MODE="subdomain" ;;
        -n|--no-key)    MODE="no-key" ;;
        -h)             fail "Use: $(basename "$0") -h or $(basename "$0") history (no port)." ;;
        stop)           fail "Use: $(basename "$0") stop" ;;
        [0-9]*)        [[ -z "$BACKEND_PORT" ]] || fail "Only one backend port allowed."
                        BACKEND_PORT="$1" ;;
        *)             [[ -z "$PATH_ID_ARG" ]] || fail "Only one key allowed."
                        PATH_ID_ARG="$1" ;;
      esac
      shift
    done
    [[ -z "$MODE" ]] && MODE="$DEFAULT_MODE"
  fi

  case "$MODE" in
    path|subdomain|no-key) ;;
    *) fail "Invalid DEFAULT_MODE or mode: '${MODE}'. Use path, subdomain, or no-key." ;;
  esac
  [[ -n "$BACKEND_PORT" ]] || fail "Backend port required."
  if [[ "$MODE" == "no-key" && -n "$PATH_ID_ARG" ]]; then
    fail "Cannot use no-key mode and a key together."
  fi

  [[ -f "$CADDY_TEMPLATE" ]] || fail "Missing Caddy template: ${CADDY_TEMPLATE}"
  [[ -f "$CF_TEMPLATE" ]] || fail "Missing cloudflared template: ${CF_TEMPLATE}"

  require_cmd caddy
  require_cmd cloudflared
  require_cmd awk
  require_cmd sed
  require_cmd tr
  require_cmd head

  validate_port "backend port" "$BACKEND_PORT"
  validate_port "CADDY_PORT" "$CADDY_PORT"

  if (( ID_LENGTH < 1 || ID_LENGTH > 32 )); then
    fail "ID_LENGTH must be between 1 and 32."
  fi
  if ! [[ "$DETACH" =~ ^[01]$ ]]; then
    fail "DETACH must be 0 or 1."
  fi

  run_tunnel
}

run_tunnel() {
  ensure_dirs
  read_source_tunnel_values

  case "$MODE" in
    no-key)
      PATH_ID=""
      CF_INGRESS_HOSTNAME="${HOSTNAME_VALUE}"
      CADDY_TEMPLATE="$CADDY_PATH_NOKEY_TEMPLATE"
      [[ -f "$CADDY_TEMPLATE" ]] || fail "Missing Caddy path-nokey template: ${CADDY_TEMPLATE}"
      ;;
    subdomain)
      if [[ -n "$PATH_ID_ARG" ]]; then
        validate_path_id "$PATH_ID_ARG"
        PATH_ID="$PATH_ID_ARG"
      else
        generate_path_id "$ID_LENGTH"
      fi
      if [[ ! -v SUBDOMAIN_DOMAIN || -z "${SUBDOMAIN_DOMAIN:-}" ]]; then
        SUBDOMAIN_DOMAIN="${HOSTNAME_VALUE#\*\.}"
      fi
      SUBDOMAIN_HOST_VALUE="${PATH_ID}.${SUBDOMAIN_DOMAIN}"
      CF_INGRESS_HOSTNAME="*.${SUBDOMAIN_DOMAIN}"
      CADDY_TEMPLATE="$CADDY_SUBDOMAIN_TEMPLATE"
      [[ -f "$CADDY_TEMPLATE" ]] || fail "Missing Caddy subdomain template: ${CADDY_TEMPLATE}"
      ;;
    path|*)
      if [[ -n "$PATH_ID_ARG" ]]; then
        validate_path_id "$PATH_ID_ARG"
        PATH_ID="$PATH_ID_ARG"
      else
        generate_path_id "$ID_LENGTH"
      fi
      CF_INGRESS_HOSTNAME="${HOSTNAME_VALUE}"
      CADDY_TEMPLATE="${ROOT_DIR}/deploy/caddy/Caddyfile.template"
      ;;
  esac

  registry_remove_conflicting "$MODE" "${PATH_ID:-}" "$BACKEND_PORT"

  local instance_caddy_port
  instance_caddy_port="$(get_next_free_caddy_port "${CADDY_PORT:-9090}")"
  CADDY_PORT="$instance_caddy_port"
  local instance_dir="${INSTANCES_DIR}/${instance_caddy_port}"
  mkdir -p "$instance_dir"

  render_template "$CADDY_TEMPLATE" "${instance_dir}/Caddyfile"
  start_caddy "${instance_dir}/Caddyfile" "${instance_dir}/caddy.pid" "${instance_dir}/caddy.log"
  local caddy_pid
  caddy_pid="$(cat "${instance_dir}/caddy.pid")"
  registry_add "$MODE" "${PATH_ID:-}" "$BACKEND_PORT" "$instance_caddy_port" "$caddy_pid" "$instance_dir"

  rebuild_cloudflared_from_registry
  restart_cloudflared_if_running
  start_cloudflared

  local share_url
  case "$MODE" in
    no-key)   share_url="https://${HOSTNAME_VALUE}/" ;;
    subdomain) share_url="https://${SUBDOMAIN_HOST_VALUE}/" ;;
    *)        share_url="https://${HOSTNAME_VALUE}/${PATH_ID}/" ;;
  esac

  printf '%s\n' "$share_url" > "$CURRENT_URL_FILE"
  if [[ "$MODE" == "no-key" ]]; then
    printf '%s\n' "" > "$CURRENT_ID_FILE"
  else
    printf '%s\n' "$PATH_ID" > "$CURRENT_ID_FILE"
  fi

  case "$MODE" in
    no-key)    log "Mode            : path (no key)"
               log "Tunnel hostname : ${HOSTNAME_VALUE}" ;;
    subdomain) log "Mode            : subdomain (key in hostname)"
               log "Ingress         : ${CF_INGRESS_HOSTNAME}" ;;
    *)         log "Mode            : path (key in path)"
               log "Tunnel hostname : ${HOSTNAME_VALUE}" ;;
  esac
  if [[ "$MODE" != "no-key" ]]; then
    if [[ -n "$PATH_ID_ARG" ]]; then
      log "Path ID         : ${PATH_ID} (provided)"
    else
      log "Path ID         : ${PATH_ID} (generated)"
    fi
  fi
  log "Share URL       : ${share_url}"
  log "Runtime files   : ${RUNTIME_DIR}"
  log "Logs            : ${instance_dir}/caddy.log, ${CF_LOG}"

  add_to_history "$MODE" "${PATH_ID:-}" "$BACKEND_PORT" "$share_url"

  if [[ "$DETACH" -eq 1 ]]; then
    log "Detached mode enabled (DETACH=1); processes continue in background."
    exit 0
  fi

  CURRENT_INSTANCE_DIR="$instance_dir"
  trap cleanup_on_exit EXIT HUP INT TERM
  wait_for_children "$(cat "${instance_dir}/caddy.pid")" "${instance_dir}/caddy.log"
}

main "$@"
