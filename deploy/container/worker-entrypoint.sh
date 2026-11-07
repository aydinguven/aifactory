#!/usr/bin/env bash
set -Eeuo pipefail

SOCKET="${DEVCLOUD_PODMAN_SOCKET:-/run/podman/podman.sock}"
[[ -S "${SOCKET}" ]] || {
    echo "AI Factory worker cannot access the host Podman socket: ${SOCKET}" >&2
    exit 1
}

export CONTAINER_HOST="unix://${SOCKET}"
podman info >/dev/null

exec python -m app.worker_agent
