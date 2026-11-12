#!/usr/bin/env bash
# Reload AI Factory's Uvicorn workers without stopping Podman workspace containers.
set -Eeuo pipefail

SERVICE_NAME="${AIFACTORY_SERVICE_NAME:-aifactory.service}"
RELOAD_TIMEOUT_SECONDS="${AIFACTORY_RELOAD_TIMEOUT_SECONDS:-25}"
START_TIMEOUT_SECONDS="${AIFACTORY_START_TIMEOUT_SECONDS:-25}"
HEALTH_URL="${AIFACTORY_HEALTH_URL:-http://127.0.0.1:8000/login}"

log() {
    printf '[aifactory-restart] %s\n' "$*"
}

fail() {
    printf '[aifactory-restart] ERROR: %s\n' "$*" >&2
    exit 1
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_positive_integer "${RELOAD_TIMEOUT_SECONDS}" ||
    fail "AIFACTORY_RELOAD_TIMEOUT_SECONDS must be a positive integer."
is_positive_integer "${START_TIMEOUT_SECONDS}" ||
    fail "AIFACTORY_START_TIMEOUT_SECONDS must be a positive integer."

if (( EUID == 0 )); then
    SUDO=()
else
    command -v sudo >/dev/null 2>&1 || fail "Run as root or install sudo."
    SUDO=(sudo)
fi

systemctl_cmd() {
    "${SUDO[@]}" systemctl "$@"
}

service_state() {
    systemctl_cmd show "${SERVICE_NAME}" --property=ActiveState --value 2>/dev/null ||
        printf 'unknown\n'
}

main_pid() {
    local pid
    pid="$(systemctl_cmd show "${SERVICE_NAME}" --property=MainPID --value 2>/dev/null ||
        printf '0')"
    if [[ "${pid}" =~ ^[1-9][0-9]*$ ]]; then
        printf '%s\n' "${pid}"
    else
        printf '0\n'
    fi
}

pid_is_alive() {
    local pid="$1"
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] &&
        "${SUDO[@]}" kill -0 "${pid}" 2>/dev/null
}

worker_pids() {
    local parent_pid="$1"
    local child_pid
    local child_command

    command -v pgrep >/dev/null 2>&1 || return 0
    command -v ps >/dev/null 2>&1 || return 0

    while IFS= read -r child_pid; do
        [[ "${child_pid}" =~ ^[1-9][0-9]*$ ]] || continue
        child_command="$(ps -o args= -p "${child_pid}" 2>/dev/null || true)"
        # Python's multiprocessing resource tracker survives worker reloads.
        [[ "${child_command}" == *multiprocessing.resource_tracker* ]] && continue
        printf '%s\n' "${child_pid}"
    done < <(pgrep -P "${parent_pid}" || true)
}

health_ready() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --max-time 2 --output /dev/null "${HEALTH_URL}"
    else
        # systemd state remains a useful fallback on minimal offline hosts.
        return 0
    fi
}

show_diagnostics() {
    local state
    state="$(service_state)"
    printf '[aifactory-restart] Service did not become healthy (state: %s).\n' "${state}" >&2
    systemctl_cmd status "${SERVICE_NAME}" --no-pager --full || true
    "${SUDO[@]}" journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
}

wait_for_start() {
    local deadline=$((SECONDS + START_TIMEOUT_SECONDS))
    local state
    local pid

    while (( SECONDS < deadline )); do
        state="$(service_state)"
        pid="$(main_pid)"
        [[ "${state}" == "failed" ]] && break

        if [[ "${state}" == "active" && "${pid}" != "0" ]] && health_ready; then
            log "AI Factory is healthy (main PID: ${pid}, URL: ${HEALTH_URL})."
            return 0
        fi
        sleep 0.5
    done
    return 1
}

STATE="$(service_state)"
OLD_MAIN_PID="$(main_pid)"

if [[ "${STATE}" == "deactivating" ]]; then
    fail "${SERVICE_NAME} is already stuck deactivating. Stop the listed aifactory-* containers once, wait for the unit to become inactive, then rerun this helper. Persistent workspace volumes are not deleted."
fi

if [[ "${STATE}" == "active" ]] && pid_is_alive "${OLD_MAIN_PID}"; then
    OLD_WORKER_PIDS=()
    while IFS= read -r worker_pid; do
        [[ "${worker_pid}" =~ ^[1-9][0-9]*$ ]] && OLD_WORKER_PIDS+=("${worker_pid}")
    done < <(worker_pids "${OLD_MAIN_PID}")

    if (( ${#OLD_WORKER_PIDS[@]} > 0 )); then
        log "Gracefully reloading Uvicorn workers in ${SERVICE_NAME} (main PID: ${OLD_MAIN_PID})..."
        # This works even before the installed unit has picked up ExecReload.
        systemctl_cmd kill --kill-who=main --signal=SIGHUP "${SERVICE_NAME}"

        reload_deadline=$((SECONDS + RELOAD_TIMEOUT_SECONDS))
        while (( SECONDS < reload_deadline )); do
            state="$(service_state)"
            current_main_pid="$(main_pid)"
            workers_reloaded=1

            for worker_pid in "${OLD_WORKER_PIDS[@]}"; do
                if pid_is_alive "${worker_pid}"; then
                    workers_reloaded=0
                    break
                fi
            done

            if [[ "${state}" == "active" && "${current_main_pid}" == "${OLD_MAIN_PID}" ]] &&
                (( workers_reloaded == 1 )) && health_ready; then
                log "AI Factory workers reloaded and healthy (main PID: ${current_main_pid}, URL: ${HEALTH_URL})."
                exit 0
            fi
            [[ "${state}" == "failed" ]] && break
            sleep 0.5
        done

        show_diagnostics
        exit 1
    else
        log "Single-process controller detected in ${SERVICE_NAME} (PID: ${OLD_MAIN_PID}); restarting main process..."
        systemctl_cmd kill --kill-who=main --signal=SIGTERM "${SERVICE_NAME}" || true
        if wait_for_start; then
            exit 0
        fi
        show_diagnostics
        exit 1
    fi
fi

log "Starting ${SERVICE_NAME} without blocking (current state: ${STATE})..."
systemctl_cmd reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl_cmd start --no-block "${SERVICE_NAME}"

if wait_for_start; then
    exit 0
fi

show_diagnostics
exit 1
