#!/usr/bin/env bash
# Local /fetch installer for Linux. Bash, curl, and base64 are already there,
# so the Kilo computer does not need Python. Kilo starts this in the open project.
# It downloads the original bytes from CATALOG_URL and writes them under .kilo.
# Keep behavior in step with install.ps1.
#
# AI Factory bakes this file into the vscode-kilo and vscode-cuda images as
# /opt/kilo/LocalMCP.sh; keep both copies identical.
if [[ -z "${KILO_INSTALL_UNBUFFERED:-}" ]] && command -v stdbuf >/dev/null 2>&1; then
  export KILO_INSTALL_UNBUFFERED=1
  exec stdbuf -oL bash "$0" "$@"
fi

CATALOG_URL="${CATALOG_URL:-http://127.0.0.1:8790}"
CATALOG_URL="${CATALOG_URL%/}"
PROJECT_ROOT="${KILO_PROJECT:-$(pwd)}"
NAME_PATTERN='^[a-z0-9]+(-[a-z0-9]+)*$'
# Tool calls run in command substitutions (subshells), so the time of the last
# catalog open is kept in a file of this server process, not in a variable.
CATALOG_OPEN_STAMP="${TMPDIR:-/tmp}/kilo-install-open.$$"

send_json() {
  printf '%s\n' "$1"
}

json_escape() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\r'/\\r}
  value=${value//$'\n'/\\n}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

send_result() {
  local id_text=$1
  local text=$2
  local is_error=$3
  send_json "{\"jsonrpc\":\"2.0\",\"id\":${id_text},\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"$(json_escape "$text")\"}],\"isError\":${is_error}}}"
}

fail() {
  send_result "$ID_TEXT" "$1" true
}

safe_name() {
  [[ $1 =~ $NAME_PATTERN ]]
}

field() {
  local json=$1
  local key=$2
  if [[ $json =~ \"$key\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

# Same Authorization header as install.ps1 Invoke-CatalogRaw / Get-Pack.
catalog_curl() {
  local method=$1
  local url=$2
  local data=${3:-}
  local max_time=${4:-30}
  local -a args=(-fsS --max-time "$max_time" -X "$method")
  if [[ -n ${MCP_TOKEN:-} ]]; then
    args+=(-H "Authorization: Bearer ${MCP_TOKEN}")
  fi
  if [[ -n $data ]]; then
    args+=(-H 'Content-Type: application/json' --data "$data")
  fi
  curl "${args[@]}" "$url"
}

download_pack() {
  local kind=$1
  local name=$2
  catalog_curl GET "${CATALOG_URL}/pack/${kind}/${name}"
}

check_relative_path() {
  local kind=$1
  local name=$2
  local raw_path=$3
  local part
  if [[ -z $raw_path ]]; then
    echo "Refusing to write an empty path." >&2
    return 1
  fi
  IFS='/' read -r -a parts <<< "${raw_path//\\//}"
  for part in "${parts[@]}"; do
    if [[ -z $part || $part == "." || $part == ".." ]]; then
      echo "Refusing to write '${raw_path}'." >&2
      return 1
    fi
  done
  if [[ $kind == "agentsmd" ]]; then
    if [[ $raw_path != "AGENTS.md" ]]; then
      echo "Refusing to write '${raw_path}'." >&2
      return 1
    fi
  elif [[ $kind != "skill" && $raw_path != "${name}.md" ]]; then
    echo "Refusing to write '${raw_path}'." >&2
    return 1
  fi
  printf '%s\n' "${parts[@]}"
}

write_pack() {
  local pack=$1
  local kind name label destination staging parent target
  kind=$(field "$pack" kind) || { echo "Catalog pack has no kind." >&2; return 1; }
  name=$(field "$pack" name) || { echo "Catalog pack has no name." >&2; return 1; }
  if [[ $kind == "skill" ]]; then
    destination="${PROJECT_ROOT}/.kilo/skills/${name}"
    label="skill:${name} -> .kilo/skills/${name}/"
    staging="${PROJECT_ROOT}/.kilo/skills/.${name}.installing"
  elif [[ $kind == "agent" ]]; then
    # Kilo loads project agents from .kilo/agents/<name>.md
    destination="${PROJECT_ROOT}/.kilo/agents/${name}.md"
    label="agent:${name} -> .kilo/agents/${name}.md"
    parent=$(dirname "$destination")
    staging="${parent}/.${name}.md.installing"
  elif [[ $kind == "workflow" ]]; then
    # Kilo workflows are slash commands under .kilo/commands/<name>.md
    destination="${PROJECT_ROOT}/.kilo/commands/${name}.md"
    label="workflow:${name} -> .kilo/commands/${name}.md"
    parent=$(dirname "$destination")
    staging="${parent}/.${name}.md.installing"
  elif [[ $kind == "agentsmd" ]]; then
    # Kilo loads one project-root AGENTS.md. Catalog names pick which template.
    destination="${PROJECT_ROOT}/AGENTS.md"
    label="agentsmd:${name} -> AGENTS.md"
    parent=$(dirname "$destination")
    staging="${parent}/.AGENTS.md.installing"
  elif [[ $kind == "rule" ]]; then
    destination="${PROJECT_ROOT}/.kilo/rules/${name}.md"
    label="rule:${name} -> .kilo/rules/${name}.md"
    parent=$(dirname "$destination")
    staging="${parent}/.${name}.md.installing"
  else
    echo "Bad kind '${kind}'." >&2
    return 1
  fi

  local -a paths=()
  local -a blobs=()
  local rest=$pack
  local raw_path data
  while [[ $rest =~ \"path\"[[:space:]]*:[[:space:]]*\"([^\"]+)\"[[:space:]]*,[[:space:]]*\"data\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9+/=]+)\" ]]; do
    raw_path=${BASH_REMATCH[1]}
    data=${BASH_REMATCH[2]}
    if ! check_relative_path "$kind" "$name" "$raw_path" >/dev/null; then
      return 1
    fi
    paths+=("$raw_path")
    blobs+=("$data")
    rest=${rest#*"${BASH_REMATCH[0]}"}
  done
  if [[ ${#paths[@]} -eq 0 ]]; then
    echo "'${kind}:${name}' has no files." >&2
    return 1
  fi

  if [[ $kind == "skill" ]]; then
    rm -rf "$staging"
    mkdir -p "$staging"
    local i
    for i in "${!paths[@]}"; do
      target="${staging}/${paths[$i]}"
      mkdir -p "$(dirname "$target")"
      printf '%s' "${blobs[$i]}" | base64 -d > "$target"
    done
    mkdir -p "$(dirname "$destination")"
    rm -rf "$destination"
    mv "$staging" "$destination"
  else
    mkdir -p "$(dirname "$destination")"
    printf '%s' "${blobs[0]}" | base64 -d > "$staging"
    rm -f "$destination"
    mv "$staging" "$destination"
  fi
  printf '%s' "$label"
}

ensure_rules_instruction() {
  local path="${PROJECT_ROOT}/kilo.jsonc"
  local glob='.kilo/rules/*.md'
  local text
  if [[ ! -f $path ]]; then
    printf '{\n  "instructions": ["%s"]\n}\n' "$glob" > "$path"
    printf '%s' "Created kilo.jsonc so Kilo loads .kilo/rules/*.md."
    return
  fi
  text=$(<"$path")
  if [[ $text == *"$glob"* ]]; then
    printf '%s' "kilo.jsonc already loads .kilo/rules/*.md."
    return
  fi
  if [[ ! $text =~ \"instructions\"[[:space:]]*:[[:space:]]*\[ ]]; then
    printf '%s' 'Add ".kilo/rules/*.md" to the instructions array in kilo.jsonc.'
    return
  fi
  local updated=${text/"${BASH_REMATCH[0]}"/"${BASH_REMATCH[0]}"$'\n    ".kilo/rules/*.md",'}
  printf '%s' "$updated" > "$path"
  printf '%s' "Added .kilo/rules/*.md to instructions in kilo.jsonc."
}

# Same rule as install.ps1: only the JSON boolean true counts. The string "true" does not.
replace_is_true() {
  local line=$1
  [[ $line =~ \"replace\"[[:space:]]*:[[:space:]]*true([^[:alnum:]_]|$) ]]
}

install_items() {
  local line=$1
  local replace=false
  local raw_ids id kind name destination pack got_kind got_name err
  local -a ids=()
  local -a planned=()
  declare -A seen=()

  if replace_is_true "$line"; then
    replace=true
  fi

  if [[ ! $line =~ \"ids\"[[:space:]]*:[[:space:]]*\[([^]]*)\] ]]; then
    echo 'ids must be a JSON array with at least one id. Call install_items again with ids such as ["skill:api-notes"].' >&2
    return 1
  fi
  raw_ids=${BASH_REMATCH[1]}
  if [[ -z ${raw_ids//[[:space:],]/} ]]; then
    echo 'ids must be a JSON array with at least one id. Call install_items again with ids such as ["skill:api-notes"].' >&2
    return 1
  fi
  while [[ $raw_ids =~ \"([^\"]+)\" ]]; do
    ids+=("${BASH_REMATCH[1]}")
    raw_ids=${raw_ids#*"${BASH_REMATCH[0]}"}
  done
  if [[ ${#ids[@]} -eq 0 ]]; then
    echo 'ids must be a JSON array with at least one id. Call install_items again with ids such as ["skill:api-notes"].' >&2
    return 1
  fi

  for id in "${ids[@]}"; do
    kind=${id%%:*}
    name=${id#*:}
    if [[ $id != *:* || $name == *:* ]]; then
      echo "Bad id '${id}'. Expected skill:name, agent:name, rule:name, workflow:name, or agentsmd:name. Copy an id from list_catalog. Do not invent an id." >&2
      return 1
    fi
    if [[ $kind != "skill" && $kind != "agent" && $kind != "rule" && $kind != "workflow" && $kind != "agentsmd" ]] || ! safe_name "$name"; then
      echo "Bad id '${id}'. Expected skill:name, agent:name, rule:name, workflow:name, or agentsmd:name. Copy an id from list_catalog. Do not invent an id." >&2
      return 1
    fi
    if [[ -n ${seen[${kind}:${name}]+x} ]]; then
      continue
    fi
    seen["${kind}:${name}"]=1
    if [[ $kind == "skill" ]]; then
      destination="${PROJECT_ROOT}/.kilo/skills/${name}"
    elif [[ $kind == "agent" ]]; then
      destination="${PROJECT_ROOT}/.kilo/agents/${name}.md"
    elif [[ $kind == "workflow" ]]; then
      destination="${PROJECT_ROOT}/.kilo/commands/${name}.md"
    elif [[ $kind == "agentsmd" ]]; then
      destination="${PROJECT_ROOT}/AGENTS.md"
    else
      destination="${PROJECT_ROOT}/.kilo/rules/${name}.md"
    fi
    if [[ -e $destination && $replace != true ]]; then
      echo "'${kind}:${name}' is already installed." >&2
      return 1
    fi
    local pack_err
    pack_err=$(mktemp)
    if ! pack=$(download_pack "$kind" "$name" 2>"$pack_err"); then
      err=$(tr -d '\r' <"$pack_err" 2>/dev/null | head -n 1)
      rm -f "$pack_err"
      echo "Could not download '${kind}:${name}' from ${CATALOG_URL}. Check that the catalog computer is running and CATALOG_URL has no /mcp suffix. Call list_catalog and copy an id from that text.${err:+ ${err}}" >&2
      return 1
    fi
    rm -f "$pack_err"
    got_kind=$(field "$pack" kind || true)
    got_name=$(field "$pack" name || true)
    if [[ $got_kind != "$kind" || $got_name != "$name" ]]; then
      echo "Catalog returned a different item for '${id}'." >&2
      return 1
    fi
    planned+=("$pack")
  done

  local agentsmd_count=0
  for pack in "${planned[@]}"; do
    if [[ $(field "$pack" kind) == "agentsmd" ]]; then
      agentsmd_count=$((agentsmd_count + 1))
    fi
  done
  if (( agentsmd_count > 1 )); then
    echo "Install only one agentsmd id at a time. Kilo loads one AGENTS.md at the project root." >&2
    return 1
  fi

  local lines="Installed:"
  local installed_rule=false
  local label note
  for pack in "${planned[@]}"; do
    if ! label=$(write_pack "$pack"); then
      return 1
    fi
    lines+=$'\n- '"${label}"
    if [[ $(field "$pack" kind) == "rule" ]]; then
      installed_rule=true
    fi
  done
  if [[ $installed_rule == true ]]; then
    note=$(ensure_rules_instruction)
    lines+=$'\n'"${note}"
  fi
  lines+=$'\nRun /reload or start a new session before using a new skill, agent, workflow, or AGENTS.md.'
  printf '%s' "$lines"
}

open_url() {
  # One opener only. Trying the next program after the first opens another tab.
  local url=$1 opener
  if [[ -n ${BROWSER:-} ]]; then
    "$BROWSER" "$url" >/dev/null 2>&1
    return $?
  fi
  for opener in xdg-open kde-open5 kde-open gnome-open wslview open; do
    if command -v "$opener" >/dev/null 2>&1; then
      "$opener" "$url" >/dev/null 2>&1
      return $?
    fi
  done
  if command -v gio >/dev/null 2>&1; then
    gio open "$url" >/dev/null 2>&1
    return $?
  fi
  if command -v cmd.exe >/dev/null 2>&1; then
    cmd.exe /c start "" "$url" >/dev/null 2>&1
    return $?
  fi
  return 1
}

post_outcome() {
  local token=$1 text=$2 field=$3
  local payload
  payload="{\"${field}\":\"$(json_escape "$text")\"}"
  catalog_curl POST "$CATALOG_URL/pick/$token" "$payload" >/dev/null 2>&1 || true
}

wait_for_pick() {
  local token=$1 body text deadline
  if [[ ! $token =~ ^[a-f0-9]{32}$ ]]; then
    return 0
  fi
  deadline=$((SECONDS + 900))
  while (( SECONDS < deadline )); do
    # The catalog holds this call until Download, cancel, or 15 minutes.
    body=$(catalog_curl GET "$CATALOG_URL/pick/$token" "" 960 2>/dev/null) || {
      sleep 2
      continue
    }
    if [[ $body == *'"status": "waiting"'* || $body == *'"status":"waiting"'* ]]; then
      sleep 1
      continue
    fi
    if [[ $body == *'"status": "unknown"'* || $body == *'"status":"unknown"'* ]]; then
      return 0
    fi
    if [[ $body == *'"status": "cancelled"'* || $body == *'"status":"cancelled"'* ]]; then
      return 0
    fi
    if [[ $body == *'"status": "ready"'* || $body == *'"status":"ready"'* ]]; then
      # Same as install.ps1 Complete-Browse: never replace from the catalog page.
      if text=$(install_items "$body" 2>&1); then
        post_outcome "$token" "$text" result
      else
        post_outcome "$token" "$text" error
      fi
      return 0
    fi
  done
}

open_catalog() {
  local token url now last
  # One /catalog turn can call this tool more than once. A second call in a few seconds would open another tab.
  now=$(date +%s)
  last=$(cat "$CATALOG_OPEN_STAMP" 2>/dev/null) || last=0
  [[ $last =~ ^[0-9]+$ ]] || last=0
  if (( now - last < 5 )); then
    printf '%s' "Opened a new catalog page."
    return 0
  fi
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    token=$(tr -d '-' < /proc/sys/kernel/random/uuid)
  else
    token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  fi
  if [[ ! $token =~ ^[a-f0-9]{32}$ ]]; then
    echo "Could not create a catalog page token. Call open_catalog again." >&2
    return 1
  fi
  catalog_curl POST "$CATALOG_URL/pick/$token" '{"register":true}' >/dev/null 2>&1 || {
    echo "Could not reach $CATALOG_URL. Check that the catalog computer is running and CATALOG_URL has no /mcp suffix. Call open_catalog again." >&2
    return 1
  }
  printf '%s' "$now" > "$CATALOG_OPEN_STAMP" 2>/dev/null || true
  url="$CATALOG_URL/browse?token=$token"
  # A background job of this tool call is stopped when the call returns. nohup keeps the waiter alive.
  nohup bash "$0" finish "$token" >/dev/null 2>&1 &
  if ! open_url "$url"; then
    printf '%s\n' "Opened a new catalog page." "Open this page yourself: $url"
    return 0
  fi
  printf '%s' "Opened a new catalog page."
}

handle_message() {
  local line=$1
  local id_raw method tool_name text
  if [[ ! $line =~ \"id\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|[-0-9]+) ]]; then
    return 0
  fi
  id_raw=${BASH_REMATCH[1]}
  ID_TEXT=$id_raw
  if [[ $line =~ \"method\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
    method=${BASH_REMATCH[1]}
  else
    method=
  fi
  case $method in
    initialize)
      send_json "{\"jsonrpc\":\"2.0\",\"id\":${ID_TEXT},\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{\"listChanged\":false}},\"serverInfo\":{\"name\":\"kilo-install\",\"version\":\"1.0.0\"},\"instructions\":\"Use open_catalog to open the catalog page and install the checked items. Use install_items when the user already named the ids.\"}}"
      ;;
    ping)
      send_json "{\"jsonrpc\":\"2.0\",\"id\":${ID_TEXT},\"result\":{}}"
      ;;
    tools/list)
      send_json "{\"jsonrpc\":\"2.0\",\"id\":${ID_TEXT},\"result\":{\"tools\":[{\"name\":\"install_items\",\"description\":\"Download catalog items and write the original files into this project .kilo folder.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"ids\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}},\"replace\":{\"type\":\"boolean\"}},\"required\":[\"ids\"],\"additionalProperties\":false}},{\"name\":\"open_catalog\",\"description\":\"Open the catalog page in the system browser. Returns as soon as the page is open. The page writes the checked items into this project .kilo folder after Download. Works from VS Code and IntelliJ IDEA on Linux.\",\"inputSchema\":{\"type\":\"object\",\"properties\":{},\"additionalProperties\":false}}]}}"
      ;;
    tools/call)
      if [[ ! $line =~ \"name\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
        fail "Unknown tool."
        return
      fi
      tool_name=${BASH_REMATCH[1]}
      local err_file err
      err_file=$(mktemp)
      if [[ $tool_name == "open_catalog" ]]; then
        if text=$(open_catalog 2>"$err_file"); then
          send_result "$ID_TEXT" "$text" false
        else
          err=$(<"$err_file")
          fail "${err:-Could not open the catalog page. Call open_catalog again.}"
        fi
      elif [[ $tool_name == "install_items" ]]; then
        if text=$(install_items "$line" 2>"$err_file"); then
          send_result "$ID_TEXT" "$text" false
        else
          err=$(<"$err_file")
          fail "${err:-Install failed.}"
        fi
      else
        fail "Unknown tool '${tool_name}'. Call install_items or open_catalog."
      fi
      rm -f "$err_file"
      ;;
    *)
      send_json "{\"jsonrpc\":\"2.0\",\"id\":${ID_TEXT},\"error\":{\"code\":-32601,\"message\":\"Method not found\"}}"
      ;;
  esac
}

if [[ "${1:-}" == "finish" ]]; then
  wait_for_pick "${2:-}"
  exit 0
fi

if [[ "${1:-}" == "browse" ]]; then
  open_catalog || exit 1
  exit 0
fi

trap 'rm -f "$CATALOG_OPEN_STAMP"' EXIT
while IFS= read -r line || [[ -n $line ]]; do
  [[ -z ${line//[[:space:]]/} ]] && continue
  handle_message "$line" || true
done
