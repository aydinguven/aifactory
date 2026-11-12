#!/usr/bin/env bash
# Enable the admin-managed offline download publisher on an existing host.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${AIFACTORY_ENV_FILE:-${PROJECT_DIR}/.env}"
SERVICE_NAME="${AIFACTORY_SERVICE_NAME:-aifactory.service}"

log() {
    printf '[aifactory-downloads] %s\n' "$*"
}

fail() {
    printf '[aifactory-downloads] ERROR: %s\n' "$*" >&2
    exit 1
}

if (( EUID == 0 )); then
    SUDO=()
else
    command -v sudo >/dev/null 2>&1 || fail "Run as root or install sudo."
    SUDO=(sudo)
fi

run_root() {
    "${SUDO[@]}" "$@"
}

if [[ ! -f "${ENV_FILE}" ]]; then
    [[ -f "${PROJECT_DIR}/.env.example" ]] || fail ".env.example was not found."
    run_root cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
    log "Created ${ENV_FILE} from .env.example."
fi

set_env_value() {
    local key="$1"
    local value="$2"
    if run_root grep -q "^${key}=" "${ENV_FILE}"; then
        run_root sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
    else
        printf '%s=%s\n' "${key}" "${value}" | run_root tee -a "${ENV_FILE}" >/dev/null
    fi
}

env_value() {
    local key="$1"
    run_root awk -F= -v key="${key}" '$1 == key { sub(/^[^=]*=/, ""); gsub(/^\"|\"$/, ""); print; exit }' "${ENV_FILE}"
}

set_env_value DOWNLOADS_ENABLED True
set_env_value DOWNLOAD_UPDATES_ENABLED True

DOWNLOADS_ROOT="$(env_value DOWNLOADS_ROOT)"
DOWNLOAD_BUILD_ROOT="$(env_value DOWNLOAD_BUILD_ROOT)"
DOWNLOADS_ROOT="${DOWNLOADS_ROOT:-/srv/aifactory-downloads}"
DOWNLOAD_BUILD_ROOT="${DOWNLOAD_BUILD_ROOT:-/var/lib/aifactory/download-builds}"

for target in "${DOWNLOADS_ROOT}" "${DOWNLOAD_BUILD_ROOT}"; do
    [[ "${target}" == /* && "${target}" != "/" ]] || fail "Unsafe download directory: ${target}"
done

SERVICE_USER="$(run_root systemctl show "${SERVICE_NAME}" --property=User --value 2>/dev/null || true)"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER:-root}}}"
id "${SERVICE_USER}" >/dev/null 2>&1 || fail "Service user does not exist: ${SERVICE_USER}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

run_root install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0755 "${DOWNLOADS_ROOT}"
run_root install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${DOWNLOAD_BUILD_ROOT}"

if command -v restorecon >/dev/null 2>&1; then
    run_root restorecon -RF "${DOWNLOADS_ROOT}" "${DOWNLOAD_BUILD_ROOT}" || true
fi

log "Enabled download publishing in ${ENV_FILE}."
log "Publication directory: ${DOWNLOADS_ROOT}"
log "Build directory: ${DOWNLOAD_BUILD_ROOT}"

bash "${PROJECT_DIR}/deploy/restart.sh"
log "Download updates are ready in the AI Factory management page."
