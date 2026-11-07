#!/usr/bin/env bash
# Configure persistent SELinux labels for AI Factory workspace bind mounts.
set -Eeuo pipefail

WORKSPACES_DIR="${DEVCLOUD_STORAGE_ROOT:-/var/lib/devcloud/workspaces}"
SERVICE_USER="${DEVCLOUD_SERVICE_USER:-${SUDO_USER:-${USER:-root}}}"

log() {
    printf '[devcloud-selinux] %s\n' "$*"
}

fail() {
    printf '[devcloud-selinux] ERROR: %s\n' "$*" >&2
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

run_root mkdir -p "${WORKSPACES_DIR}"
# Do not recursively chown existing workspaces: Podman's :U mount option owns
# their contents according to each image's non-root user.
run_root chown "${SERVICE_USER}:${SERVICE_USER}" "${WORKSPACES_DIR}"
run_root chmod 0755 "${WORKSPACES_DIR}"

if ! command -v getenforce >/dev/null 2>&1; then
    log "SELinux tools are not installed; no labeling is required on this host."
    exit 0
fi

SELINUX_MODE="$(getenforce 2>/dev/null || printf 'Disabled')"
log "Detected SELinux mode: ${SELINUX_MODE}."

FCONTEXT_PATTERN="${WORKSPACES_DIR}(/.*)?"
PERSISTENT_CONTEXT=0
if command -v semanage >/dev/null 2>&1; then
    if ! run_root semanage fcontext -a -t container_file_t "${FCONTEXT_PATTERN}" >/dev/null 2>&1; then
        run_root semanage fcontext -m -t container_file_t "${FCONTEXT_PATTERN}"
    fi
    PERSISTENT_CONTEXT=1
    log "Registered persistent container_file_t context for ${FCONTEXT_PATTERN}."
else
    log "WARNING: semanage is unavailable; install policycoreutils-python-utils for labels that survive a full relabel."
fi

if [[ "${SELINUX_MODE}" == "Disabled" ]]; then
    log "SELinux is disabled. Persistent rules are prepared; apply labels after the first permissive boot."
    exit 0
fi

if (( PERSISTENT_CONTEXT == 1 )); then
    command -v restorecon >/dev/null 2>&1 || fail "restorecon is required but was not found."
    # Never recursively relabel workspace children during repair: rootless
    # Podman's :Z mount owns their private MCS categories.
    run_root restorecon -Fv "${WORKSPACES_DIR}"
elif command -v chcon >/dev/null 2>&1; then
    run_root chcon -t container_file_t "${WORKSPACES_DIR}"
else
    [[ "${SELINUX_MODE}" == "Disabled" ]] || fail "Neither semanage/restorecon nor chcon is available to label workspace storage."
fi

STORAGE_LABEL="$(run_root ls -Zd "${WORKSPACES_DIR}")"
[[ "${STORAGE_LABEL}" == *":container_file_t:"* ]] ||
    fail "Workspace storage does not have container_file_t: ${STORAGE_LABEL}"
log "Verified workspace label: ${STORAGE_LABEL}"

log "Workspace containers use Podman's private :Z label and :U ownership mapping at launch."
