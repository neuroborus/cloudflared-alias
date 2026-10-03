#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly RUNTIME_DIR="${ROOT_DIR}/.runtime"
readonly INSTANCES_DIR="${RUNTIME_DIR}/instances"
readonly CF_DIR="${RUNTIME_DIR}/cloudflared"
readonly CF_CONFIG="${CF_DIR}/config.yml"
readonly CF_PID_FILE="${CF_DIR}/cloudflared.pid"
readonly CF_LOG="${CF_DIR}/cloudflared.log"
readonly REGISTRY_FILE="${RUNTIME_DIR}/registry"
readonly HISTORY_FILE="${RUNTIME_DIR}/tunnel-history"
readonly HISTORY_MAX=10
readonly CONFIG_FILE="${ROOT_DIR}/cloudflared-alias.conf"
readonly CF_TEMPLATE="${ROOT_DIR}/deploy/cloudflared/config.template.yml"
RUNTIME_LOCKED=0

log() { printf '[tunnel] %s\n' "$*"; }
fail() { printf '[tunnel] Error: %s\n' "$*" >&2; exit 1; }
trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}
require_cmd() { command -v "$1" >/dev/null 2>&1 || fail "'$1' is required but not installed."; }

usage() {
  cat <<USAGE
Usage:
  $(basename "$0") [options] <backend_port> [key]
  $(basename "$0") stop
  $(basename "$0") --list

  --help, -h       Show this help.
  --list, -l       Show last ${HISTORY_MAX} tunnels and pick one interactively.
Modes: -p/--path | -s/--subdomain | -n/--no-key (else config DEFAULT_MODE)
  key             Optional lowercase alphanumeric/hyphen key; else random.
Config: ${CONFIG_FILE}
Environment: DEFAULT_MODE, CADDY_PORT, ID_LENGTH, DETACH, CLOUDFLARED_BASE_CONFIG,
             SUBDOMAIN_DOMAIN, TUNNEL_NAME, TUNNEL_HOSTNAME, TUNNEL_CREDENTIALS_FILE
USAGE
}

# Parse the supported scalar subset without evaluating shell or YAML code.
scalar_value() {
  local value single_pattern="^'(([^']|'')*)'([[:space:]]+#.*)?$"
  local double_pattern='^"(([^"\\]|\\.)*)"([[:space:]]+#.*)?$'
  value="$(trim "$1")"
  if [[ "$value" == \"* ]]; then
    [[ "$value" =~ $double_pattern ]] || fail "Malformed double-quoted configuration value."
    value="${BASH_REMATCH[1]}"
    value="${value//\\\"/\"}"
    value="${value//\\\\/\\}"
  elif [[ "$value" == \'* ]]; then
    [[ "$value" =~ $single_pattern ]] || fail "Malformed single-quoted configuration value."
    value="${BASH_REMATCH[1]}"
    value="${value//\'\'/\'}"
  else
    value="${value%%[[:space:]]#*}"
    [[ "$value" == \#* ]] && value=""
    value="$(trim "$value")"
  fi
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *$'\t'* ]] || fail "Configuration values must be single-line scalars."
  printf '%s' "$value"
}

load_config() {
  [[ -f "$CONFIG_FILE" ]] || return 0
  local line key value
  local -A from_env=()
  for key in DEFAULT_MODE CADDY_PORT ID_LENGTH DETACH SUBDOMAIN_DOMAIN CLOUDFLARED_BASE_CONFIG; do
    [[ ! -v "$key" ]] || from_env["$key"]=1
  done
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(trim "$line")"
    [[ -n "$line" && "$line" != \#* ]] || continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || fail "Invalid config line: ${line}"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$key" in
      DEFAULT_MODE|CADDY_PORT|ID_LENGTH|DETACH|SUBDOMAIN_DOMAIN|CLOUDFLARED_BASE_CONFIG)
        if [[ ! -v "from_env[$key]" ]]; then
          value="$(scalar_value "$value")"
          printf -v "$key" '%s' "$value"
        fi ;;
      *) fail "Unknown config option: ${key}" ;;
    esac
  done < "$CONFIG_FILE"
  return 0
}

validate_number() {
  local label="$1" value="$2" maximum="$3"
  [[ "$value" =~ ^[0-9]{1,5}$ ]] || fail "${label} must be an integer, got '${value}'."
  (( 10#$value >= 1 && 10#$value <= maximum )) || fail "${label} must be between 1 and ${maximum}, got '${value}'."
}
validate_key() {
  [[ ${#1} -ge 1 && ${#1} -le 32 ]] || fail "Key length must be 1–32."
  [[ "$1" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail "Key must be lowercase alphanumeric and hyphens only (e.g. my-key-1)."
}
validate_hostname() {
  local name="$1" label
  [[ ${#name} -le 253 && "$name" != *[[:cntrl:]]* && "$name" != *..* && "$name" != .* && "$name" != *. ]] || fail "Invalid hostname: '${name}'."
  local -a labels
  IFS=. read -r -a labels <<< "$name"
  [[ ${#labels[@]} -gt 0 ]] || fail "Hostname must not be empty."
  for label in "${labels[@]}"; do
    [[ ${#label} -le 63 && "$label" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$ ]] || fail "Invalid hostname: '${name}'."
  done
}
source_scalar() {
  local key="$1" value=""
  if [[ -f "$SOURCE_CF_CONFIG" ]]; then
    value="$(awk -v key="$key" '$0 ~ "^[[:space:]]*(-[[:space:]]*)?" key ":[[:space:]]*" {sub("^[[:space:]]*(-[[:space:]]*)?" key ":[[:space:]]*", ""); print; exit}' "$SOURCE_CF_CONFIG")"
  fi
  scalar_value "$value"
}
read_source_tunnel_values() {
  TUNNEL_NAME_VALUE="${TUNNEL_NAME:-$(source_scalar tunnel)}"
  CREDENTIALS_FILE_VALUE="${TUNNEL_CREDENTIALS_FILE:-$(source_scalar credentials-file)}"
  HOSTNAME_VALUE="${TUNNEL_HOSTNAME:-$(source_scalar hostname)}"
  [[ -n "$TUNNEL_NAME_VALUE" ]] || fail "Tunnel name is missing. Set 'tunnel:' in '${SOURCE_CF_CONFIG}' or export TUNNEL_NAME."
  [[ -n "$CREDENTIALS_FILE_VALUE" ]] || fail "credentials-file is missing. Set it in '${SOURCE_CF_CONFIG}' or export TUNNEL_CREDENTIALS_FILE."
  [[ -n "$HOSTNAME_VALUE" ]] || fail "Ingress hostname is missing. Add a hostname entry in '${SOURCE_CF_CONFIG}' or export TUNNEL_HOSTNAME."
  [[ "$TUNNEL_NAME_VALUE" != -* && "$TUNNEL_NAME_VALUE" != *[$'\n\r\t']* ]] || fail "Invalid tunnel name."
  [[ "$CREDENTIALS_FILE_VALUE" != *[$'\n\r\t']* ]] || fail "Invalid credentials-file path."
  # Only the path is used; the launcher never reads the credential contents.
  if [[ "$CREDENTIALS_FILE_VALUE" == \~/* ]]; then
    CREDENTIALS_FILE_VALUE="${HOME}/${CREDENTIALS_FILE_VALUE:2}"
  elif [[ "$CREDENTIALS_FILE_VALUE" != /* ]]; then
    CREDENTIALS_FILE_VALUE="$(cd "$(dirname "$SOURCE_CF_CONFIG")" && pwd)/${CREDENTIALS_FILE_VALUE}"
  fi
}

ensure_dirs() { mkdir -p "$CF_DIR" "$INSTANCES_DIR"; }
lock_runtime() {
  [[ "$RUNTIME_LOCKED" != 1 ]] || return 0
  require_cmd flock
  ensure_dirs
  exec 9>"${RUNTIME_DIR}/launcher.lock"
  flock 9
  RUNTIME_LOCKED=1
}
unlock_runtime() { RUNTIME_LOCKED=0; flock -u 9; exec 9>&-; }

# Non-whitespace separators preserve the empty key field in existing no-key rows.
parse_registry() {
  IFS=$'\034' read -r REG_MODE REG_KEY REG_BACKEND REG_CADDY REG_PID REG_DIR REG_HOST <<< "${1//$'\t'/$'\034'}"
}
read_registry() { if [[ -f "$REGISTRY_FILE" ]]; then cat "$REGISTRY_FILE"; fi; }
is_pid_running() {
  local pid="$1" state
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/${pid}/stat" ]] || return 1
  state="$(awk '{sub(/^.*\) /, ""); print $1}' "/proc/${pid}/stat" 2>/dev/null)" || return 1
  [[ "$state" != Z && "$state" != X ]] && kill -0 "$pid" 2>/dev/null
}
is_owned_process() {
  local pid="$1" program="$2" config="$3" argument previous="" position=0
  local has_program=0 has_config=0 interpreter=0
  is_pid_running "$pid" || return 1
  while IFS= read -r -d '' argument; do
    if (( position == 0 )); then
      [[ "${argument##*/}" != "$program" ]] || has_program=1
      case "${argument##*/}" in bash|sh|dash|python|python[0-9]*) interpreter=1 ;; esac
    elif (( position == 1 && interpreter == 1 )); then
      [[ "${argument##*/}" != "$program" ]] || has_program=1
    fi
    [[ "$previous" != --config || "$argument" != "$config" ]] || has_config=1
    previous="$argument"
    position=$(( position + 1 ))
  done < "/proc/${pid}/cmdline"
  [[ "$has_program" -eq 1 && "$has_config" -eq 1 ]]
}
stop_process() {
  local pid="$1" program="$2" config="$3" attempt
  is_owned_process "$pid" "$program" "$config" || return 0
  log "Stopping ${program} (pid ${pid})"
  kill "$pid" 2>/dev/null || true
  for (( attempt=0; attempt<20; attempt++ )); do
    is_owned_process "$pid" "$program" "$config" || return 0
    sleep 0.1
  done
  if is_owned_process "$pid" "$program" "$config"; then kill -9 "$pid" 2>/dev/null || true; fi
}
stop_started_process() {
  local pid="$1" parent
  stop_process "$@"
  # A captured child may still be awaiting exec and lack the daemon command line.
  # Only this launcher's direct children qualify for the fallback, never saved PIDs.
  if is_pid_running "$pid"; then
    parent="$(awk '{sub(/^.*\) /, ""); print $2}' "/proc/${pid}/stat" 2>/dev/null)" || return 0
    [[ "$parent" == "$$" ]] || return 0
    kill -9 "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}
stop_cloudflared() {
  local pid=""
  [[ ! -f "$CF_PID_FILE" ]] || pid="$(cat "$CF_PID_FILE")"
  [[ -z "$pid" ]] || stop_process "$pid" cloudflared "$CF_CONFIG"
  rm -f "$CF_PID_FILE"
}
valid_registry_instance() {
  [[ "$REG_DIR" == "${INSTANCES_DIR}/"* && "$REG_DIR" != *'/../'* ]] || return 1
  [[ "$REG_MODE" == path || "$REG_MODE" == subdomain || "$REG_MODE" == no-key ]] || return 1
  is_owned_process "$REG_PID" caddy "${REG_DIR}/Caddyfile"
}
persisted_hostname() {
  local port="$1" value host
  [[ -f "$CF_CONFIG" && "$port" =~ ^[1-9][0-9]{0,4}$ ]] || return 1
  # Match the generated ingress subset by its unique local service.
  value="$(awk -v port="$port" '
    function finish_rule() {
      if (service == "http://localhost:" port) {
        matches++
        hostname = host
        valid = (host_fields == 1 && service_fields == 1)
      }
    }
    /^[^[:space:]#]/ {
      if (ingress) finish_rule()
      ingress = ($0 ~ /^ingress:[[:space:]]*(#.*)?$/)
      host = service = ""; host_fields = service_fields = 0
    }
    !ingress {next}
    /^[[:space:]]*-[[:space:]]/ {
      finish_rule()
      host = service = ""; host_fields = service_fields = 0
    }
    /^[[:space:]]*(-[[:space:]]*)?hostname:[[:space:]]*/ {
      sub(/^[[:space:]]*(-[[:space:]]*)?hostname:[[:space:]]*/, "")
      host = $0
      host_fields++
      next
    }
    /^[[:space:]]*(-[[:space:]]*)?service:[[:space:]]*/ {
      sub(/^[[:space:]]*(-[[:space:]]*)?service:[[:space:]]*/, "")
      sub(/[[:space:]]+#.*$/, "")
      sub(/[[:space:]]+$/, "")
      service = $0
      service_fields++
    }
    END {finish_rule(); if (matches != 1 || !valid || hostname == "") exit 1; print hostname}
  ' "$CF_CONFIG")" || return 1
  host="$(scalar_value "$value")" || return 1
  (validate_hostname "$host") || return 1
  printf '%s' "$host"
}
recover_registry_hosts() {
  local line host tmp changed=0
  [[ -f "$REGISTRY_FILE" ]] || return 0
  [[ -r "$REGISTRY_FILE" ]] || { log "Cannot read the running instance registry; existing runtime state was preserved." >&2; return 1; }
  tmp="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")" || return 1
  if ! while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if [[ -z "$REG_HOST" ]] && valid_registry_instance; then
      if ! host="$(persisted_hostname "$REG_CADDY")"; then
        rm -f "$tmp"
        log "Cannot recover hostname for legacy Caddy instance on port ${REG_CADDY}. Existing runtime state was preserved; restore its persisted ingress or stop the instances explicitly." >&2
        return 1
      fi
      if ! printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$REG_MODE" "$REG_KEY" "$REG_BACKEND" "$REG_CADDY" "$REG_PID" "$REG_DIR" "$host" >> "$tmp"; then rm -f "$tmp"; return 1; fi
      changed=1
    else
      if ! printf '%s\n' "$line" >> "$tmp"; then rm -f "$tmp"; return 1; fi
    fi
  done < "$REGISTRY_FILE"; then rm -f "$tmp"; return 1; fi
  if (( changed )); then mv "$tmp" "$REGISTRY_FILE"; else rm -f "$tmp"; fi
}
prune_registry() {
  local line tmp
  tmp="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if valid_registry_instance; then printf '%s\n' "$line" >> "$tmp"; fi
  done < <(read_registry)
  mv "$tmp" "$REGISTRY_FILE"
}
registry_conflicts() {
  [[ "$BACKEND_PORT" == "$REG_BACKEND" || ( -n "$PATH_ID" && "$PATH_ID" == "$REG_KEY" ) || ( "$MODE" == no-key && "$REG_MODE" == no-key ) ]]
}
remove_conflicts() {
  local line tmp
  tmp="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if ! registry_conflicts; then printf '%s\n' "$line" >> "$tmp"; fi
  done < <(read_registry)
  mv "$tmp" "$REGISTRY_FILE"
}
commit_registry_update() {
  [[ -n "${PENDING_REGISTRY_FILE:-}" ]] || return 0
  local line pending="$PENDING_REGISTRY_FILE"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if registry_conflicts; then
      log "Stopping previous tunnel (key=${REG_KEY:-<no-key>}, port=${REG_BACKEND}) — same key or port."
      stop_process "$REG_PID" caddy "${REG_DIR}/Caddyfile"
      rm -f "${REG_DIR}/caddy.pid"
    fi
  done < "$pending"
  PENDING_REGISTRY_FILE=""
  rm -f "$pending"
}
port_in_use() {
  local port="$1" listeners
  if command -v ss >/dev/null 2>&1; then
    listeners="$(ss -H -ltn "( sport = :${port} )")" || fail "Could not inspect listening ports with ss."
    [[ -n "$listeners" ]]
  elif command -v netstat >/dev/null 2>&1; then
    listeners="$(netstat -ltn)" || fail "Could not inspect listening ports with netstat."
    awk -v port=":${port}$" '$4 ~ port {found=1} END {exit !found}' <<< "$listeners"
  else
    fail "'ss' or 'netstat' is required to select a free Caddy port."
  fi
}
next_caddy_port() {
  local port="$CADDY_PORT" line busy
  while (( port <= 65535 )); do
    busy=0
    # Never proxy a backend to the Caddy instance itself, including other backends.
    [[ "$port" != "$BACKEND_PORT" ]] || busy=1
    if port_in_use "$port"; then busy=1; fi
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -n "$line" ]] || continue
      parse_registry "$line"
      [[ "$BACKEND_PORT" != "$REG_CADDY" ]] || fail "Backend port ${BACKEND_PORT} is used by a running Caddy instance."
      [[ "$port" != "$REG_CADDY" && "$port" != "$REG_BACKEND" ]] || busy=1
    done < <(read_registry)
    if (( busy == 0 )); then printf '%s' "$port"; return 0; fi
    port=$(( port + 1 ))
  done
  fail "No free Caddy port found from ${CADDY_PORT}."
}

yaml_quote() { local value="${1//\'/\'\'}"; printf "'%s'" "$value"; }
escape_sed() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
render_template() {
  local template="$1" output="$2"
  sed -e "s|__CADDY_PORT__|${CADDY_PORT}|g" \
    -e "s|__BACKEND_PORT__|${BACKEND_PORT}|g" \
    -e "s|__PATH_ID__|${PATH_ID}|g" \
    -e "s|__SUBDOMAIN_HOST__|${ROUTE_HOST}|g" \
    -e "s|__TUNNEL_NAME__|$(escape_sed "$(yaml_quote "$TUNNEL_NAME_VALUE")")|g" \
    -e "s|__CREDENTIALS_FILE__|$(escape_sed "$(yaml_quote "$CREDENTIALS_FILE_VALUE")")|g" \
    "$template" > "$output"
}
recover_shared_tunnel_identity() {
  local line SOURCE_CF_CONFIG="$CF_CONFIG"
  SHARED_TUNNEL_NAME=""; SHARED_CREDENTIALS_FILE=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    valid_registry_instance || continue
    if [[ ! -r "$CF_CONFIG" ]] ||
      ! awk '/^tunnel:/ {names++} /^credentials-file:/ {credentials++} END {exit !(names == 1 && credentials == 1)}' "$CF_CONFIG" ||
      ! SHARED_TUNNEL_NAME="$(source_scalar tunnel)" ||
      ! SHARED_CREDENTIALS_FILE="$(source_scalar credentials-file)" ||
      [[ -z "$SHARED_TUNNEL_NAME" || -z "$SHARED_CREDENTIALS_FILE" ]]; then
      log "Cannot recover shared tunnel identity. Existing runtime state was preserved; restore '${CF_CONFIG}' or stop the instances explicitly." >&2
      return 1
    fi
    # Legacy configs kept home-relative paths; match startup's path expansion.
    if [[ "$SHARED_CREDENTIALS_FILE" == \~/* ]]; then
      SHARED_CREDENTIALS_FILE="${HOME}/${SHARED_CREDENTIALS_FILE:2}"
    fi
    return 0
  done < <(read_registry)
}
check_shared_tunnel() {
  [[ -s "$REGISTRY_FILE" ]] || return 0
  [[ "$SHARED_TUNNEL_NAME" == "$TUNNEL_NAME_VALUE" && "$SHARED_CREDENTIALS_FILE" == "$CREDENTIALS_FILE_VALUE" ]] || fail "Running instances use a different tunnel or credentials-file. Stop them before changing tunnel identity."
}
render_ingress_rules() {
  local line mode host
  # Path rules precede hostname-wide keyed rules and no-key fallbacks.
  for mode in path subdomain no-key; do
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -n "$line" ]] || continue
      parse_registry "$line"
      [[ "$REG_MODE" == "$mode" ]] || continue
      host="$REG_HOST"
      [[ -n "$host" ]] || { log "Missing persisted hostname for Caddy instance on port ${REG_CADDY}." >&2; return 1; }
      printf '  - hostname: %s\n' "$(yaml_quote "$host")" || return 1
      if [[ "$REG_MODE" == path ]]; then printf '    path: %s\n' "$(yaml_quote "^/${REG_KEY}(/.*)?$")" || return 1; fi
      printf '    service: http://localhost:%s\n' "$REG_CADDY" || return 1
    done < <(read_registry)
  done
}
rebuild_cloudflared() {
  local ingress rendered tmp
  ingress="$(mktemp "${CF_DIR}/ingress.XXXXXX")" || return 1
  rendered="$(mktemp "${CF_DIR}/rendered.XXXXXX")" || { rm -f "$ingress"; return 1; }
  tmp="$(mktemp "${CF_DIR}/config.XXXXXX")" || { rm -f "$ingress" "$rendered"; return 1; }
  if ! render_ingress_rules > "$ingress" ||
    ! render_template "$CF_TEMPLATE" "$rendered" ||
    ! sed -e "/__INGRESS_RULES__/r ${ingress}" -e '/__INGRESS_RULES__/d' "$rendered" > "$tmp" ||
    ! mv "$tmp" "$CF_CONFIG"; then
    rm -f "$ingress" "$rendered" "$tmp"
    return 1
  fi
  rm -f "$ingress" "$rendered"
}
handle_signal() {
  if [[ "${DAEMON_STARTING:-0}" == 1 ]]; then DEFERRED_SIGNAL="$1"; else exit "$1"; fi
}
start_daemon() {
  local pid_variable="$1" log_file="$2"
  shift 2
  # Defer exit until the new child PID is captured for rollback.
  DEFERRED_SIGNAL=0; DAEMON_STARTING=1
  nohup "$@" < /dev/null > "$log_file" 2>&1 9>&- &
  printf -v "$pid_variable" '%s' "$!"
  sleep 1
  DAEMON_STARTING=0
  [[ "${DEFERRED_SIGNAL:-0}" == 0 ]] || exit "$DEFERRED_SIGNAL"
}
start_cloudflared() {
  log "Starting cloudflared tunnel '${TUNNEL_NAME_VALUE}'"
  start_daemon PENDING_CF_PID "$CF_LOG" cloudflared --config "$CF_CONFIG" tunnel run "$TUNNEL_NAME_VALUE"
  is_owned_process "$PENDING_CF_PID" cloudflared "$CF_CONFIG"
}
finish_cloudflared_refresh() {
  [[ -n "${CF_BACKUP_DIR:-}" ]] || return 0
  local backup="$CF_BACKUP_DIR" file
  if [[ "${CF_REFRESH_COMMITTED:-0}" == 1 ]]; then
    commit_registry_update
    stop_process "$PREVIOUS_CF_PID" cloudflared "$CF_CONFIG"
  else
    stop_started_process "${PENDING_CF_PID:-}" cloudflared "$CF_CONFIG"
    for file in "$CF_CONFIG" "$CF_PID_FILE"; do
      if [[ -f "${backup}/${file##*/}" ]]; then
        cp "${backup}/${file##*/}" "$file"
      else
        rm -f "$file"
      fi
    done
  fi
  CF_BACKUP_DIR=""; PENDING_CF_PID=""
  rm -rf "$backup"
}
refresh_cloudflared() {
  local line current_host current_url backup file
  if [[ -s "$REGISTRY_FILE" ]]; then
    # Keep the working connector until the candidate starts; restore files on failure.
    backup="$(mktemp -d "${CF_DIR}/refresh.XXXXXX")" || return 1
    for file in "$CF_CONFIG" "$CF_PID_FILE"; do
      if [[ -f "$file" ]] && ! cp "$file" "$backup/"; then rm -rf "$backup"; return 1; fi
    done
    PREVIOUS_CF_PID=""
    [[ ! -f "$CF_PID_FILE" ]] || PREVIOUS_CF_PID="$(cat "$CF_PID_FILE")"
    CF_REFRESH_COMMITTED=0; CF_BACKUP_DIR="$backup"
    if ! rebuild_cloudflared || ! start_cloudflared || ! printf '%s\n' "$PENDING_CF_PID" > "$CF_PID_FILE"; then
      finish_cloudflared_refresh
      return 1
    fi
    CF_REFRESH_COMMITTED=1
    finish_cloudflared_refresh
    line="$(tail -n 1 "$REGISTRY_FILE")"
    parse_registry "$line"
    current_host="$REG_HOST"
    current_url="https://${current_host}/"
    [[ "$REG_MODE" != path ]] || current_url="${current_url}${REG_KEY}/"
    printf '%s\n' "$current_url" > "${RUNTIME_DIR}/current-share-url.txt"
    printf '%s\n' "$REG_KEY" > "${RUNTIME_DIR}/current-path-id.txt"
  else
    stop_cloudflared
    rm -f "$CF_CONFIG" "${RUNTIME_DIR}/current-share-url.txt" "${RUNTIME_DIR}/current-path-id.txt"
  fi
}
stop_all() {
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if valid_registry_instance; then
      stop_process "$REG_PID" caddy "${REG_DIR}/Caddyfile"
      rm -f "${REG_DIR}/caddy.pid"
    fi
  done < <(read_registry)
  # Clean up the pre-registry layout as well, but never trust a PID alone.
  if [[ -f "${RUNTIME_DIR}/caddy/caddy.pid" ]]; then
    stop_process "$(cat "${RUNTIME_DIR}/caddy/caddy.pid")" caddy "${RUNTIME_DIR}/caddy/Caddyfile"
    rm -f "${RUNTIME_DIR}/caddy/caddy.pid"
  fi
  rm -f "$REGISTRY_FILE"
  stop_cloudflared
  rm -f "$CF_CONFIG" "${RUNTIME_DIR}/current-share-url.txt" "${RUNTIME_DIR}/current-path-id.txt"
  log "Stopped runtime Caddy/cloudflared processes (if they were running)."
}
cleanup_on_exit() {
  local status=$? line tmp registry_before
  trap - EXIT
  # Complete rollback and shared connector updates even if another signal arrives.
  trap '' HUP INT TERM
  [[ "${KEEP_RUNNING:-0}" != 1 ]] || return "$status"
  lock_runtime
  finish_cloudflared_refresh
  if [[ -n "${PENDING_REGISTRY_FILE:-}" ]]; then
    mv "$PENDING_REGISTRY_FILE" "$REGISTRY_FILE"
    PENDING_REGISTRY_FILE=""
  fi
  registry_before="${REGISTRY_BEFORE_START-$(read_registry)}"
  if [[ -n "${CURRENT_INSTANCE_DIR:-}" ]]; then
    stop_started_process "${CURRENT_CADDY_PID:-}" caddy "${CURRENT_INSTANCE_DIR}/Caddyfile"
    rm -f "${CURRENT_INSTANCE_DIR}/caddy.pid"
  fi
  tmp="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if [[ "$REG_DIR" != "${CURRENT_INSTANCE_DIR:-}" ]]; then printf '%s\n' "$line" >> "$tmp"; fi
  done < <(read_registry)
  mv "$tmp" "$REGISTRY_FILE"
  prune_registry
  if [[ ! -s "$REGISTRY_FILE" || "$(read_registry)" != "$registry_before" ]]; then
    # A rejected start may have supplied a different identity than the active tunnel.
    if [[ -s "$REGISTRY_FILE" ]]; then
      if ! recover_shared_tunnel_identity; then unlock_runtime; return "$status"; fi
      TUNNEL_NAME_VALUE="$SHARED_TUNNEL_NAME"
      CREDENTIALS_FILE_VALUE="$SHARED_CREDENTIALS_FILE"
    fi
    if ! refresh_cloudflared; then log "Could not restart shared cloudflared; check '${CF_LOG}'." >&2; fi
  fi
  unlock_runtime
  return "$status"
}

parse_history() {
  local line="$1"
  # Releases before the tab-delimited format wrote literal backslash-t sequences.
  if [[ "$line" != *$'\t'* ]]; then line="${line//\\t/$'\t'}"; fi
  IFS=$'\034' read -r HIST_MODE HIST_KEY HIST_PORT HIST_URL HIST_CREATED HIST_USED <<< "${line//$'\t'/$'\034'}"
  if [[ "$HIST_MODE" == no-key && "$HIST_PORT" == *://* ]]; then
    HIST_USED="$HIST_CREATED"; HIST_CREATED="$HIST_URL"; HIST_URL="$HIST_PORT"; HIST_PORT="$HIST_KEY"; HIST_KEY=""
  fi
  HIST_USED="${HIST_USED:-$HIST_CREATED}"
}
add_to_history() {
  local url="$1" created="${HISTORY_CREATED:-$(date +%s)}" line tmp count=1
  if [[ -z "${HISTORY_CREATED:-}" && -f "$HISTORY_FILE" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      parse_history "$line"
      if [[ "$HIST_URL" == "$url" && "$HIST_PORT" == "$BACKEND_PORT" ]]; then
        created="$HIST_CREATED"
        break
      fi
    done < "$HISTORY_FILE"
  fi
  tmp="$(mktemp "${RUNTIME_DIR}/history.XXXXXX")"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$MODE" "$PATH_ID" "$BACKEND_PORT" "$url" "$created" "$(date +%s)" > "$tmp"
  if [[ -f "$HISTORY_FILE" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      parse_history "$line"
      [[ "$HIST_URL" != "$url" || "$HIST_PORT" != "$BACKEND_PORT" ]] || continue
      (( count < HISTORY_MAX )) || break
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$HIST_MODE" "$HIST_KEY" "$HIST_PORT" "$HIST_URL" "$HIST_CREATED" "$HIST_USED" >> "$tmp"
      count=$(( count + 1 ))
    done < "$HISTORY_FILE"
  fi
  mv "$tmp" "$HISTORY_FILE"
}
history_pick() {
  [[ -s "$HISTORY_FILE" ]] || fail "No tunnel history yet. Start a tunnel first."
  local line number=0 choice port_override
  local -a entries=()
  printf '%3s %-52s %-10s %6s %s\n' '#' URL MODE PORT 'LAST USED'
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    entries+=("$line")
    number=$(( number + 1 ))
    parse_history "$line"
    printf '%3d %-52s %-10s %6s %s\n' "$number" "$HIST_URL" "$HIST_MODE" "$HIST_PORT" "$(date -d "@${HIST_USED}" +'%Y-%m-%d %H:%M' 2>/dev/null || printf '%s' "$HIST_USED")"
  done < "$HISTORY_FILE"
  printf '\n[tunnel] Select number (1-%s) or Enter to cancel: ' "$number"
  read -r choice || choice=""
  choice="$(trim "$choice")"
  if [[ -z "$choice" ]]; then log "Cancelled."; exit 0; fi
  [[ "$choice" =~ ^[0-9]{1,5}$ ]] || fail "Invalid choice."
  choice=$(( 10#$choice ))
  (( choice >= 1 && choice <= number )) || fail "No such entry."
  parse_history "${entries[choice-1]}"
  MODE="$HIST_MODE"; PATH_ID_ARG="$HIST_KEY"; BACKEND_PORT="$HIST_PORT"; HISTORY_CREATED="$HIST_CREATED"
  HISTORY_URL="$HIST_URL"
  printf '[tunnel] Backend port [%s]: ' "$BACKEND_PORT"
  read -r port_override || port_override=""
  port_override="$(trim "$port_override")"
  [[ -z "$port_override" ]] || BACKEND_PORT="$port_override"
}

run_tunnel() {
  local template registry_backup
  read_source_tunnel_values
  PATH_ID=""; ROUTE_HOST="$HOSTNAME_VALUE"
  case "$MODE" in
    subdomain)
      SUBDOMAIN_DOMAIN="${SUBDOMAIN_DOMAIN:-${HOSTNAME_VALUE#\*\.}}"
      validate_hostname "$SUBDOMAIN_DOMAIN"
      template="${ROOT_DIR}/deploy/caddy/Caddyfile.subdomain.template" ;;
    no-key) validate_hostname "$HOSTNAME_VALUE"; template="${ROOT_DIR}/deploy/caddy/Caddyfile.path-nokey.template" ;;
    path) validate_hostname "$HOSTNAME_VALUE"; template="${ROOT_DIR}/deploy/caddy/Caddyfile.template" ;;
  esac
  if [[ "$MODE" != no-key ]]; then
    if [[ -n "$PATH_ID_ARG" ]]; then
      validate_key "$PATH_ID_ARG"; PATH_ID="$PATH_ID_ARG"
    else
      PATH_ID="$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c "$ID_LENGTH" || true)"
      [[ ${#PATH_ID} -eq ID_LENGTH ]] || fail "Failed to generate a random key."
    fi
  fi
  [[ "$MODE" != subdomain ]] || ROUTE_HOST="${PATH_ID}.${SUBDOMAIN_DOMAIN}"
  validate_hostname "$ROUTE_HOST"
  local share_url="https://${ROUTE_HOST}/"
  [[ "$MODE" != path ]] || share_url="https://${ROUTE_HOST}/${PATH_ID}/"
  if [[ -n "${HISTORY_URL:-}" && "$HISTORY_URL" != "$share_url" ]]; then
    fail "History URL differs from the current hostname configuration. Start a new tunnel instead."
  fi
  [[ -f "$template" && -f "$CF_TEMPLATE" ]] || fail "Missing Caddy or cloudflared template."
  lock_runtime
  # Recover active identity and legacy routes before installing candidate cleanup.
  recover_shared_tunnel_identity || fail "Could not recover the running tunnel identity."
  recover_registry_hosts || fail "Could not recover the running instance hostnames."
  trap cleanup_on_exit EXIT
  trap 'handle_signal 129' HUP
  trap 'handle_signal 130' INT
  trap 'handle_signal 143' TERM
  REGISTRY_BEFORE_START="$(read_registry)"
  prune_registry
  check_shared_tunnel
  CADDY_PORT="$(next_caddy_port)"
  CURRENT_INSTANCE_DIR="$(mktemp -d "${INSTANCES_DIR}/${CADDY_PORT}.XXXXXX")"
  render_template "$template" "${CURRENT_INSTANCE_DIR}/Caddyfile"
  log "Starting Caddy on localhost:${CADDY_PORT}"
  XDG_CONFIG_HOME="${CURRENT_INSTANCE_DIR}/config" XDG_DATA_HOME="${CURRENT_INSTANCE_DIR}/data" \
    start_daemon CURRENT_CADDY_PID "${CURRENT_INSTANCE_DIR}/caddy.log" caddy run --config "${CURRENT_INSTANCE_DIR}/Caddyfile" --adapter caddyfile
  printf '%s\n' "$CURRENT_CADDY_PID" > "${CURRENT_INSTANCE_DIR}/caddy.pid"
  is_owned_process "$CURRENT_CADDY_PID" caddy "${CURRENT_INSTANCE_DIR}/Caddyfile" || fail "Caddy failed to start. Check '${CURRENT_INSTANCE_DIR}/caddy.log'."
  registry_backup="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  if ! cp "$REGISTRY_FILE" "$registry_backup"; then rm -f "$registry_backup"; fail "Could not save the running instance registry."; fi
  PENDING_REGISTRY_FILE="$registry_backup"
  remove_conflicts
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$MODE" "$PATH_ID" "$BACKEND_PORT" "$CADDY_PORT" "$CURRENT_CADDY_PID" "$CURRENT_INSTANCE_DIR" "$ROUTE_HOST" >> "$REGISTRY_FILE"
  refresh_cloudflared || fail "cloudflared failed to start. Check '${CF_LOG}'."
  unset REGISTRY_BEFORE_START
  printf '%s\n' "$share_url" > "${RUNTIME_DIR}/current-share-url.txt"
  printf '%s\n' "$PATH_ID" > "${RUNTIME_DIR}/current-path-id.txt"
  add_to_history "$share_url"
  log "Mode            : ${MODE}"
  log "Tunnel hostname : ${ROUTE_HOST}"
  [[ "$MODE" == no-key ]] || log "Path ID         : ${PATH_ID}"
  log "Share URL       : ${share_url}"
  log "Runtime files   : ${RUNTIME_DIR}"
  log "Logs            : ${CURRENT_INSTANCE_DIR}/caddy.log, ${CF_LOG}"
  unlock_runtime
  if [[ "$DETACH" == 1 ]]; then
    KEEP_RUNNING=1
    log "Detached mode enabled (DETACH=1); processes continue in background."
    return 0
  fi
  log "Running in foreground. Press Ctrl+C to stop."
  local shared_pid
  while is_owned_process "$CURRENT_CADDY_PID" caddy "${CURRENT_INSTANCE_DIR}/Caddyfile"; do
    lock_runtime
    if ! is_owned_process "$CURRENT_CADDY_PID" caddy "${CURRENT_INSTANCE_DIR}/Caddyfile"; then
      unlock_runtime
      break
    fi
    shared_pid=""
    [[ ! -f "$CF_PID_FILE" ]] || shared_pid="$(cat "$CF_PID_FILE")"
    if ! is_owned_process "$shared_pid" cloudflared "$CF_CONFIG"; then
      unlock_runtime
      fail "cloudflared exited. Check '${CF_LOG}'."
    fi
    unlock_runtime
    sleep 1
  done
  fail "Caddy exited. Check '${CURRENT_INSTANCE_DIR}/caddy.log'."
}

main() {
  [[ $# -gt 0 ]] || { usage; exit 1; }
  if [[ "$1" == --help || "$1" == -h ]]; then usage; return 0; fi
  if [[ "$1" == stop ]]; then
    [[ $# -eq 1 ]] || fail "Use: $(basename "$0") stop"
    lock_runtime; stop_all; unlock_runtime; return 0
  fi
  load_config
  SOURCE_CF_CONFIG="${CLOUDFLARED_BASE_CONFIG:-$HOME/.cloudflared/config.yml}"
  CADDY_PORT="${CADDY_PORT-9090}"
  ID_LENGTH="${ID_LENGTH-4}"
  DETACH="${DETACH-0}"
  DEFAULT_MODE="${DEFAULT_MODE-path}"
  BACKEND_PORT=""; PATH_ID_ARG=""; MODE=""
  if [[ "$1" == --list || "$1" == -l ]]; then
    [[ $# -eq 1 ]] || fail "Use: $(basename "$0") --list (no port)."
    history_pick
  else
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -p|--path) MODE=path ;;
        -s|--subdomain) MODE=subdomain ;;
        -n|--no-key) MODE=no-key ;;
        -h|--help) usage; return 0 ;;
        -*) fail "Unknown option: '$1'." ;;
        stop) fail "Use: $(basename "$0") stop" ;;
        *) if [[ -z "$BACKEND_PORT" ]]; then BACKEND_PORT="$1"
           elif [[ -z "$PATH_ID_ARG" ]]; then PATH_ID_ARG="$1"
           else fail "Only one backend port and one key allowed."; fi ;;
      esac
      shift
    done
    MODE="${MODE:-$DEFAULT_MODE}"
  fi
  case "$MODE" in path|subdomain|no-key) ;; *) fail "Invalid DEFAULT_MODE or mode: '${MODE}'. Use path, subdomain, or no-key." ;; esac
  [[ -n "$BACKEND_PORT" ]] || fail "Backend port required."
  [[ "$MODE" != no-key || -z "$PATH_ID_ARG" ]] || fail "Cannot use no-key mode and a key together."
  validate_number 'backend port' "$BACKEND_PORT" 65535
  validate_number CADDY_PORT "$CADDY_PORT" 65535
  validate_number ID_LENGTH "$ID_LENGTH" 32
  BACKEND_PORT=$(( 10#$BACKEND_PORT )); CADDY_PORT=$(( 10#$CADDY_PORT )); ID_LENGTH=$(( 10#$ID_LENGTH ))
  [[ "$DETACH" =~ ^[01]$ ]] || fail "DETACH must be 0 or 1."
  local dependency
  for dependency in caddy cloudflared awk sed tr head tail flock mktemp nohup; do require_cmd "$dependency"; done
  if ! command -v ss >/dev/null 2>&1 && ! command -v netstat >/dev/null 2>&1; then fail "'ss' or 'netstat' is required to select a free Caddy port."; fi
  run_tunnel
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
