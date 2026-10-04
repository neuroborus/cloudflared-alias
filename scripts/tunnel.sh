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
readonly SHARE_CONTRACT="${ROOT_DIR}/scripts/share_contract.py"
readonly PUBLICATION="${ROOT_DIR}/scripts/publication.py"
readonly PUBLICATIONS_DIR="${RUNTIME_DIR}/publications"
RUNTIME_LOCKED=0
STRUCTURED=0

log() {
  if [[ "$STRUCTURED" == 1 ]]; then printf '[tunnel] %s\n' "$*" >&2
  else printf '[tunnel] %s\n' "$*"; fi
}
fail() { FAILURE_MESSAGE="$*"; printf '[tunnel] Error: %s\n' "$*" >&2; exit 1; }
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
  $(basename "$0") expose-port PORT [--url-mode MODE] [--key KEY]
  $(basename "$0") expose-files PATH [--url-mode MODE] [--update-mode MODE] [--key KEY]
  $(basename "$0") list-shares
  $(basename "$0") stop-share ID

  --help, -h       Show this help.
  --list, -l       Show last ${HISTORY_MAX} tunnels and pick one interactively.
Modes: -p/--path | -s/--subdomain | -n/--no-key (else config DEFAULT_MODE)
  key             Optional lowercase alphanumeric/hyphen key; else random.
Share commands return JSON, use keyed path mode by default, and always detach.
  --url-mode      path | subdomain | no-key (explicit bare-domain access)
  --key           1–32 lowercase alphanumeric/hyphen characters; else 32 random hex.
  --update-mode   snapshot | manual | live (default for files)
Config: ${CONFIG_FILE}
Environment: DEFAULT_MODE, CADDY_PORT, ID_LENGTH, DETACH, CLOUDFLARED_BASE_CONFIG,
             SUBDOMAIN_DOMAIN, TUNNEL_NAME, TUNNEL_HOSTNAME, TUNNEL_CREDENTIALS_FILE,
             ALIAS_PYTHON (share commands; defaults to .venv/bin/python3 or python3)
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
  for key in DEFAULT_MODE CADDY_PORT ID_LENGTH DETACH SUBDOMAIN_DOMAIN CLOUDFLARED_BASE_CONFIG ALIAS_PYTHON; do
    [[ ! -v "$key" ]] || from_env["$key"]=1
  done
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(trim "$line")"
    [[ -n "$line" && "$line" != \#* ]] || continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || fail "Invalid config line: ${line}"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    case "$key" in
      DEFAULT_MODE|CADDY_PORT|ID_LENGTH|DETACH|SUBDOMAIN_DOMAIN|CLOUDFLARED_BASE_CONFIG|ALIAS_PYTHON)
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
publication_dir() {
  local instance="$1" id="${1##*/}"
  [[ "$instance" == "${INSTANCES_DIR}/${id}" && "$id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ ]] || return 1
  [[ ! -L "$RUNTIME_DIR" && ! -L "$PUBLICATIONS_DIR" ]] || return 1
  printf '%s' "${PUBLICATIONS_DIR}/${id}"
}
cleanup_publication() {
  local directory pid=""
  directory="$(publication_dir "$1")" || return 0
  [[ -d "$directory" && ! -L "$directory" ]] || return 0
  [[ ! -f "${directory}/helper.pid" ]] || pid="$(cat "${directory}/helper.pid")"
  stop_process "$pid" publication.py "${directory}/helper.json"
  rm -rf -- "$directory"
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
    if valid_registry_instance; then printf '%s\n' "$line" >> "$tmp"
    else cleanup_publication "$REG_DIR"; fi
  done < <(read_registry)
  mv "$tmp" "$REGISTRY_FILE"
}
registry_conflicts() {
  [[ ( "$BACKEND_PORT" != 0 && "$BACKEND_PORT" == "$REG_BACKEND" ) || ( -n "$PATH_ID" && "$PATH_ID" == "$REG_KEY" ) || ( "$MODE" == no-key && "$REG_MODE" == no-key ) ]]
}
registry_pending_removal() {
  if [[ -n "${STOPPING_INSTANCE_DIR:-}" ]]; then
    [[ "$REG_DIR" == "$STOPPING_INSTANCE_DIR" ]]
  else
    registry_conflicts
  fi
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
    if registry_pending_removal; then
      if [[ -n "${STOPPING_INSTANCE_DIR:-}" ]]; then log "Stopping share ${REG_DIR##*/}"
      else log "Stopping previous tunnel (key=${REG_KEY:-<no-key>}, port=${REG_BACKEND}) — same key or port."; fi
      stop_process "$REG_PID" caddy "${REG_DIR}/Caddyfile"
      rm -f "${REG_DIR}/caddy.pid"
      cleanup_publication "$REG_DIR"
    elif ! valid_registry_instance; then
      cleanup_publication "$REG_DIR"
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
  local port="$CADDY_PORT" line busy publication helper_port
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
      helper_port=""
      publication="$(publication_dir "$REG_DIR")" || continue
      [[ ! -f "${publication}/helper.port" ]] || helper_port="$(cat "${publication}/helper.port")"
      [[ "$BACKEND_PORT" != "$helper_port" ]] || fail "Backend port ${BACKEND_PORT} is used by a publication helper."
      [[ "$port" != "$helper_port" ]] || busy=1
    done < <(read_registry)
    if (( busy == 0 )); then printf '%s' "$port"; return 0; fi
    port=$(( port + 1 ))
  done
  fail "No free Caddy port found from ${CADDY_PORT}."
}

yaml_quote() { local value="${1//\'/\'\'}"; printf "'%s'" "$value"; }
escape_sed() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
render_template() {
  local template="$1" output="$2" tunnel credentials
  # Nested substitutions can corrupt Bash's parser when a signal trap interrupts them.
  tunnel="$(yaml_quote "$TUNNEL_NAME_VALUE")"
  tunnel="$(escape_sed "$tunnel")"
  credentials="$(yaml_quote "$CREDENTIALS_FILE_VALUE")"
  credentials="$(escape_sed "$credentials")"
  sed -e "s|__CADDY_PORT__|${CADDY_PORT}|g" \
    -e "s|__BACKEND_PORT__|${BACKEND_PORT}|g" \
    -e "s|__PATH_ID__|${PATH_ID}|g" \
    -e "s|__SUBDOMAIN_HOST__|${ROUTE_HOST}|g" \
    -e "s|__TUNNEL_NAME__|${tunnel}|g" \
    -e "s|__CREDENTIALS_FILE__|${credentials}|g" \
    "$template" > "$output"
}
render_file_template() {
  local template="$1" output="$2" content token public_root file_handler fallback_file_handler cache="" prepare="" events=""
  public_root="${CURRENT_PUBLICATION_DIR}/public"
  [[ "$public_root" != *[$'\n\r\t{}']* ]] || fail "Unsupported launcher path for Caddy file serving."
  public_root="${public_root//\\/\\\\}"; public_root="${public_root//\"/\\\"}"
  file_handler="$("$ALIAS_PYTHON" "$PUBLICATION" caddy-file-handler \
    --config "${CURRENT_PUBLICATION_DIR}/helper.json")" || fail "Could not render file routing."
  fallback_file_handler="$("$ALIAS_PYTHON" "$PUBLICATION" caddy-file-handler --fallback \
    --config "${CURRENT_PUBLICATION_DIR}/helper.json")" || fail "Could not render fallback file routing."
  if [[ "$UPDATE_MODE" != snapshot ]]; then cache='header >Cache-Control "no-store"'; fi
  if [[ "$UPDATE_MODE" == manual ]]; then
    prepare="forward_auth 127.0.0.1:${HELPER_PORT} {
      uri /__alias/prepare
      @unavailable status 5xx
      handle_response @unavailable {
        error \"Publication preparation unavailable\" 502
      }
    }"
  elif [[ "$UPDATE_MODE" == live ]]; then
    events="@events path /__alias/events
    reverse_proxy @events 127.0.0.1:${HELPER_PORT} {
      flush_interval -1
    }"
  fi
  render_template "$template" "$output"
  content="$(cat "$output")"
  local -A replacements=(
    [__PUBLIC_ROOT__]="$public_root"
    [__CACHE_POLICY__]="$cache"
    [__PREPARATION_HANDLER__]="$prepare"
    [__EVENT_HANDLER__]="$events"
    [__FILE_HANDLER__]="$file_handler"
    [__FALLBACK_FILE_HANDLER__]="$fallback_file_handler"
  )
  # Scan only remaining template text; filenames and paths stay literal.
  {
    while [[ "$content" =~ __(PUBLIC_ROOT|CACHE_POLICY|PREPARATION_HANDLER|EVENT_HANDLER|FILE_HANDLER|FALLBACK_FILE_HANDLER)__ ]]; do
      token="${BASH_REMATCH[0]}"
      printf '%s%s' "${content%%"$token"*}" "${replacements[$token]}"
      content="${content#*"$token"}"
    done
    printf '%s\n' "$content"
  } > "$output"
}

start_publication() {
  local event_url="/__alias/events" attempt
  CURRENT_PUBLICATION_DIR="$(publication_dir "$CURRENT_INSTANCE_DIR")"
  HELPER_PORT=0
  if [[ "$UPDATE_MODE" != snapshot ]]; then
    # Reserve the candidate Caddy port too; registry installation follows readiness.
    HELPER_PORT="$(CADDY_PORT=$(( CADDY_PORT + 1 )) next_caddy_port)"
  fi
  [[ "$MODE" != path ]] || event_url="/${PATH_ID}${event_url}"
  "$ALIAS_PYTHON" "$PUBLICATION" init "$FILE_SOURCE" "${CURRENT_INSTANCE_DIR##*/}" \
    "$UPDATE_MODE" "$event_url" "$HELPER_PORT" || fail "Could not initialize the file publication."
  if [[ "$UPDATE_MODE" == snapshot ]]; then
    "$ALIAS_PYTHON" "$PUBLICATION" prepare --config "${CURRENT_PUBLICATION_DIR}/helper.json" \
      > "${CURRENT_PUBLICATION_DIR}/helper.log" 2>&1 || fail "File preparation failed. Check '${CURRENT_PUBLICATION_DIR}/helper.log'."
    return 0
  fi
  printf '%s\n' "$HELPER_PORT" > "${CURRENT_PUBLICATION_DIR}/helper.port"
  start_daemon CURRENT_HELPER_PID "${CURRENT_PUBLICATION_DIR}/helper.log" \
    "$ALIAS_PYTHON" "$PUBLICATION" "serve-${UPDATE_MODE}" --config "${CURRENT_PUBLICATION_DIR}/helper.json"
  printf '%s\n' "$CURRENT_HELPER_PID" > "${CURRENT_PUBLICATION_DIR}/helper.pid"
  for (( attempt=0; attempt<50; attempt++ )); do
    is_owned_process "$CURRENT_HELPER_PID" publication.py "${CURRENT_PUBLICATION_DIR}/helper.json" || break
    [[ ! -f "${CURRENT_PUBLICATION_DIR}/ready.json" ]] || return 0
    sleep 0.1
  done
  fail "Publication helper failed to start. Check '${CURRENT_PUBLICATION_DIR}/helper.log'."
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
    # With no survivors, stopping the target is the irreversible commit boundary.
    [[ "${STOP_TRANSACTION:-0}" != 1 ]] || STOP_EMPTY_COMMITTED=1
    finish_empty_registry
  fi
}
finish_empty_registry() {
  commit_registry_update
  stop_cloudflared
  rm -f "$CF_CONFIG" "${RUNTIME_DIR}/current-share-url.txt" "${RUNTIME_DIR}/current-path-id.txt"
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
    cleanup_publication "$REG_DIR"
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
  local status="$1" line tmp registry_before
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
    if [[ -n "${CURRENT_PUBLICATION_DIR:-}" ]]; then
      stop_started_process "${CURRENT_HELPER_PID:-}" publication.py "${CURRENT_PUBLICATION_DIR}/helper.json"
    fi
    cleanup_publication "$CURRENT_INSTANCE_DIR"
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

exit_handler() {
  local status=$?
  trap - EXIT
  # Error serialization must not leave a signal window before transaction cleanup.
  trap '' HUP INT TERM
  if [[ "$STRUCTURED" == 1 && "$status" != 0 ]]; then
    if ! "${ALIAS_PYTHON:-python3}" "$SHARE_CONTRACT" error "${FAILURE_CODE:-launcher_error}" \
      "${FAILURE_MESSAGE:-Launcher failed; see stderr for details.}"; then
      printf '{"error":{"code":"launcher_error","message":"Launcher failed; see stderr for details."}}\n'
    fi
  fi
  if [[ "${STOP_TRANSACTION:-0}" == 1 ]]; then
    trap '' HUP INT TERM
    finish_cloudflared_refresh
    if [[ "${STOP_EMPTY_COMMITTED:-0}" == 1 ]]; then finish_empty_registry
    elif [[ -n "${PENDING_REGISTRY_FILE:-}" ]]; then mv "$PENDING_REGISTRY_FILE" "$REGISTRY_FILE"; fi
    [[ -z "${STOP_REGISTRY_FILE:-}" ]] || rm -f "$STOP_REGISTRY_FILE"
    [[ -z "${STOP_BACKUP_FILE:-}" ]] || rm -f "$STOP_BACKUP_FILE"
    [[ "$RUNTIME_LOCKED" != 1 ]] || unlock_runtime
  elif [[ "${CLEANUP_ON_EXIT:-0}" == 1 ]]; then
    cleanup_on_exit "$status"
  fi
  return "$status"
}

share_result() {
  local state="${1:-active}" host="$REG_HOST" url shared_pid="" directory helper_pid=""
  [[ -n "$host" ]] || host="$(persisted_hostname "$REG_CADDY")" || fail "Cannot recover hostname for share ${REG_DIR##*/}."
  validate_hostname "$host"
  url="https://${host}/"
  [[ "$REG_MODE" != path ]] || url="${url}${REG_KEY}/"
  if [[ "$state" == active ]]; then
    [[ ! -f "$CF_PID_FILE" ]] || shared_pid="$(cat "$CF_PID_FILE")"
    is_owned_process "$shared_pid" cloudflared "$CF_CONFIG" || state=degraded
  fi
  if [[ "$REG_BACKEND" == 0 ]]; then
    directory="$(publication_dir "$REG_DIR")" || fail "Invalid file share directory."
    if [[ "$state" == active && -f "${directory}/helper.port" ]]; then
      [[ ! -f "${directory}/helper.pid" ]] || helper_pid="$(cat "${directory}/helper.pid")"
      is_owned_process "$helper_pid" publication.py "${directory}/helper.json" || state=degraded
    fi
    "$ALIAS_PYTHON" "$SHARE_CONTRACT" files "${REG_DIR##*/}" "$url" "$REG_MODE" "$state" "${directory}/helper.json"
  else
    "$ALIAS_PYTHON" "$SHARE_CONTRACT" port "${REG_DIR##*/}" "$url" "$REG_BACKEND" "$REG_MODE" "$state"
  fi
}

list_shares() {
  local line results="" result
  if [[ ! -f "$REGISTRY_FILE" ]]; then printf '[]\n'; return 0; fi
  lock_runtime
  if [[ ! -f "$REGISTRY_FILE" ]]; then
    unlock_runtime
    printf '[]\n'
    return 0
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    valid_registry_instance || continue
    result="$(share_result)" || fail "Could not inspect share ${REG_DIR##*/}."
    results+="${result}"$'\n'
  done < "$REGISTRY_FILE"
  printf '%s' "$results" | "$ALIAS_PYTHON" "$SHARE_CONTRACT" collect
  unlock_runtime
}

find_active_share() {
  local id="$1" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    if [[ "${REG_DIR##*/}" == "$id" ]] && valid_registry_instance; then printf '%s' "$line"; return 0; fi
  done < "$REGISTRY_FILE"
  return 1
}

stop_share() {
  local id="$1" line target="" result
  [[ "$id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ ]] || fail "Invalid share ID."
  FAILURE_CODE=unknown_share
  [[ -f "$REGISTRY_FILE" ]] || fail "Unknown active share: ${id}."
  # Reject unknown IDs without even creating a lock in an older runtime layout.
  target="$(find_active_share "$id")" || fail "Unknown active share: ${id}."
  lock_runtime
  # Recheck ownership after acquiring the lock; a concurrent replacement may win.
  target="$(find_active_share "$id")" || fail "Unknown active share: ${id}."
  parse_registry "$target"
  FAILURE_CODE=launcher_error
  result="$(share_result stopped)" || fail "Could not inspect share ${id}."
  STOPPING_INSTANCE_DIR="$REG_DIR"
  STOP_TRANSACTION=1
  trap 'handle_signal 129' HUP
  trap 'handle_signal 130' INT
  trap 'handle_signal 143' TERM
  STOP_REGISTRY_FILE="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    parse_registry "$line"
    [[ "$REG_DIR" != "$STOPPING_INSTANCE_DIR" ]] || continue
    valid_registry_instance || continue
    [[ -n "$REG_HOST" ]] || REG_HOST="$(persisted_hostname "$REG_CADDY")" || fail "Cannot recover hostname for share ${REG_DIR##*/}."
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$REG_MODE" "$REG_KEY" "$REG_BACKEND" "$REG_CADDY" "$REG_PID" "$REG_DIR" "$REG_HOST" >> "$STOP_REGISTRY_FILE"
  done < "$REGISTRY_FILE"
  if [[ -s "$STOP_REGISTRY_FILE" ]]; then
    recover_shared_tunnel_identity || fail "Could not recover the running tunnel identity."
    TUNNEL_NAME_VALUE="$SHARED_TUNNEL_NAME"; CREDENTIALS_FILE_VALUE="$SHARED_CREDENTIALS_FILE"
  fi
  STOP_BACKUP_FILE="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  cp "$REGISTRY_FILE" "$STOP_BACKUP_FILE"
  PENDING_REGISTRY_FILE="$STOP_BACKUP_FILE"
  STOP_BACKUP_FILE=""
  mv "$STOP_REGISTRY_FILE" "$REGISTRY_FILE"
  STOP_REGISTRY_FILE=""
  refresh_cloudflared || fail "cloudflared failed to refresh. Check '${CF_LOG}'."
  STOP_TRANSACTION=0
  unlock_runtime
  printf '%s\n' "$result"
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
  if [[ -n "${FILE_SOURCE:-}" ]]; then
    local file_mode="$MODE"
    [[ "$MODE" != no-key ]] || file_mode=nokey
    template="${ROOT_DIR}/deploy/caddy/Caddyfile.files.${file_mode}.template"
  fi
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
  CLEANUP_ON_EXIT=1
  trap exit_handler EXIT
  trap 'handle_signal 129' HUP
  trap 'handle_signal 130' INT
  trap 'handle_signal 143' TERM
  REGISTRY_BEFORE_START="$(read_registry)"
  prune_registry
  check_shared_tunnel
  CADDY_PORT="$(next_caddy_port)"
  CURRENT_INSTANCE_DIR="$(mktemp -d "${INSTANCES_DIR}/${CADDY_PORT}.XXXXXX")"
  if [[ -n "${FILE_SOURCE:-}" ]]; then
    start_publication
    render_file_template "$template" "${CURRENT_INSTANCE_DIR}/Caddyfile"
  else
    render_template "$template" "${CURRENT_INSTANCE_DIR}/Caddyfile"
  fi
  log "Starting Caddy on localhost:${CADDY_PORT}"
  XDG_CONFIG_HOME="${CURRENT_INSTANCE_DIR}/config" XDG_DATA_HOME="${CURRENT_INSTANCE_DIR}/data" \
    start_daemon CURRENT_CADDY_PID "${CURRENT_INSTANCE_DIR}/caddy.log" caddy run --config "${CURRENT_INSTANCE_DIR}/Caddyfile" --adapter caddyfile
  printf '%s\n' "$CURRENT_CADDY_PID" > "${CURRENT_INSTANCE_DIR}/caddy.pid"
  is_owned_process "$CURRENT_CADDY_PID" caddy "${CURRENT_INSTANCE_DIR}/Caddyfile" || fail "Caddy failed to start. Check '${CURRENT_INSTANCE_DIR}/caddy.log'."
  if [[ "$STRUCTURED" == 1 ]]; then
    if [[ -n "${FILE_SOURCE:-}" ]]; then
      SHARE_RESULT="$("$ALIAS_PYTHON" "$SHARE_CONTRACT" files "${CURRENT_INSTANCE_DIR##*/}" "$share_url" "$MODE" active "${CURRENT_PUBLICATION_DIR}/helper.json")"
    else
      SHARE_RESULT="$("$ALIAS_PYTHON" "$SHARE_CONTRACT" port "${CURRENT_INSTANCE_DIR##*/}" "$share_url" "$BACKEND_PORT" "$MODE" active)"
    fi
    printf '%s\n' "$SHARE_RESULT" > "${CURRENT_INSTANCE_DIR}/share.json"
  fi
  registry_backup="$(mktemp "${RUNTIME_DIR}/registry.XXXXXX")"
  if ! cp "$REGISTRY_FILE" "$registry_backup"; then rm -f "$registry_backup"; fail "Could not save the running instance registry."; fi
  PENDING_REGISTRY_FILE="$registry_backup"
  remove_conflicts
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$MODE" "$PATH_ID" "$BACKEND_PORT" "$CADDY_PORT" "$CURRENT_CADDY_PID" "$CURRENT_INSTANCE_DIR" "$ROUTE_HOST" >> "$REGISTRY_FILE"
  refresh_cloudflared || fail "cloudflared failed to start. Check '${CF_LOG}'."
  unset REGISTRY_BEFORE_START
  printf '%s\n' "$share_url" > "${RUNTIME_DIR}/current-share-url.txt"
  printf '%s\n' "$PATH_ID" > "${RUNTIME_DIR}/current-path-id.txt"
  [[ -n "${FILE_SOURCE:-}" ]] || add_to_history "$share_url"
  log "Mode            : ${MODE}"
  log "Tunnel hostname : ${ROUTE_HOST}"
  [[ "$MODE" == no-key ]] || log "Path ID         : ${PATH_ID}"
  if [[ -n "${FILE_SOURCE:-}" ]]; then log "Route base URL  : ${share_url}"
  else log "Share URL       : ${share_url}"; fi
  log "Runtime files   : ${RUNTIME_DIR}"
  log "Logs            : ${CURRENT_INSTANCE_DIR}/caddy.log, ${CF_LOG}"
  unlock_runtime
  if [[ "$DETACH" == 1 ]]; then
    KEEP_RUNNING=1
    log "Detached mode enabled (DETACH=1); processes continue in background."
    [[ "$STRUCTURED" != 1 ]] || printf '%s\n' "$SHARE_RESULT"
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
  case "$1" in
    expose-port|expose-files|list-shares|stop-share)
      STRUCTURED=1
      trap exit_handler EXIT ;;
  esac
  load_config
  if [[ "$STRUCTURED" == 1 ]]; then
    if [[ ! -v ALIAS_PYTHON ]]; then
      if [[ -x "${ROOT_DIR}/.venv/bin/python3" ]]; then ALIAS_PYTHON="${ROOT_DIR}/.venv/bin/python3"
      else ALIAS_PYTHON=python3; fi
    fi
    require_cmd "$ALIAS_PYTHON"
    case "$1" in
      list-shares)
        [[ $# -eq 1 ]] || fail "Use: $(basename "$0") list-shares"
        list_shares; return 0 ;;
      stop-share)
        [[ $# -eq 2 ]] || fail "Use: $(basename "$0") stop-share ID"
        # The rendered tunnel config supplies identity; no source config is read.
        BACKEND_PORT=""; PATH_ID=""; ROUTE_HOST=""; CADDY_PORT=""
        stop_share "$2"; return 0 ;;
    esac
  fi
  SOURCE_CF_CONFIG="${CLOUDFLARED_BASE_CONFIG:-$HOME/.cloudflared/config.yml}"
  CADDY_PORT="${CADDY_PORT-9090}"
  ID_LENGTH="${ID_LENGTH-4}"
  DETACH="${DETACH-0}"
  DEFAULT_MODE="${DEFAULT_MODE-path}"
  BACKEND_PORT=""; PATH_ID_ARG=""; MODE=""
  FILE_SOURCE=""; UPDATE_MODE=live
  if [[ "$1" == expose-port || "$1" == expose-files ]]; then
    local command="$1"
    shift
    [[ $# -gt 0 ]] || fail "${command} requires a source. See --help."
    if [[ "$command" == expose-files ]]; then FILE_SOURCE="$1"; BACKEND_PORT=0
    else BACKEND_PORT="$1"; fi
    shift
    MODE=path; DETACH=1
    local key_given=0 mode_given=0 update_given=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --url-mode)
          [[ $# -ge 2 && "$mode_given" == 0 ]] || fail "Provide --url-mode once with a mode."
          MODE="$2"; mode_given=1; shift ;;
        --key)
          [[ $# -ge 2 && "$key_given" == 0 ]] || fail "Provide --key once with a key."
          PATH_ID_ARG="$2"; key_given=1; validate_key "$PATH_ID_ARG"; shift ;;
        --update-mode)
          [[ "$command" == expose-files && $# -ge 2 && "$update_given" == 0 ]] || fail "Provide --update-mode once for files."
          UPDATE_MODE="$2"; update_given=1; shift ;;
        *) fail "Unknown ${command} option: '$1'." ;;
      esac
      shift
    done
    if [[ "$MODE" != no-key && "$key_given" == 0 ]]; then
      PATH_ID_ARG="$("$ALIAS_PYTHON" "$SHARE_CONTRACT" key)"
    fi
    if [[ "$command" == expose-files ]]; then
      case "$UPDATE_MODE" in snapshot|manual|live) ;; *) fail "Invalid file update mode: '${UPDATE_MODE}'." ;; esac
      [[ -n "$FILE_SOURCE" && "$FILE_SOURCE" != *[$'\n\r\t']* ]] || fail "Provide one single-line file or directory path."
      FILE_SOURCE="$("$ALIAS_PYTHON" "$PUBLICATION" select -- "$FILE_SOURCE")" || fail "Invalid file selection."
    fi
  elif [[ "$1" == --list || "$1" == -l ]]; then
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
  [[ -n "$FILE_SOURCE" ]] || validate_number 'backend port' "$BACKEND_PORT" 65535
  validate_number CADDY_PORT "$CADDY_PORT" 65535
  if [[ "$STRUCTURED" != 1 ]]; then
    validate_number ID_LENGTH "$ID_LENGTH" 32
    ID_LENGTH=$(( 10#$ID_LENGTH ))
  fi
  BACKEND_PORT=$(( 10#$BACKEND_PORT )); CADDY_PORT=$(( 10#$CADDY_PORT ))
  [[ "$DETACH" =~ ^[01]$ ]] || fail "DETACH must be 0 or 1."
  local dependency
  for dependency in caddy cloudflared awk sed tr head tail flock mktemp nohup; do require_cmd "$dependency"; done
  if ! command -v ss >/dev/null 2>&1 && ! command -v netstat >/dev/null 2>&1; then fail "'ss' or 'netstat' is required to select a free Caddy port."; fi
  run_tunnel
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
