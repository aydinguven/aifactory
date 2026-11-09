#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(cd "${ROOT_DIR}" && python3 -c 'from app import __version__; print(__version__)')"
OUTPUT_DIR="${1:-${ROOT_DIR}/dist/container-images}"
CONTROLLER_IMAGE="${DEVCLOUD_CONTROLLER_IMAGE:-localhost/devcloud-controller:${VERSION}}"
WORKER_IMAGE="${DEVCLOUD_WORKER_IMAGE:-localhost/devcloud-worker:${VERSION}}"
POSTGRES_IMAGE="${DEVCLOUD_POSTGRES_IMAGE:-localhost/devcloud-postgresql:16}"
POSTGRES_SOURCE_IMAGE="${DEVCLOUD_POSTGRES_SOURCE_IMAGE:-quay.io/sclorg/postgresql-16-c10s:latest}"

mkdir -p "${OUTPUT_DIR}"
podman image exists "${CONTROLLER_IMAGE}" || {
    echo "ERROR: Controller image is missing: ${CONTROLLER_IMAGE}" >&2
    exit 1
}
podman image exists "${WORKER_IMAGE}" || {
    echo "ERROR: Worker image is missing: ${WORKER_IMAGE}" >&2
    exit 1
}
podman image exists "${POSTGRES_IMAGE}" || {
    podman pull "${POSTGRES_SOURCE_IMAGE}"
    podman tag "${POSTGRES_SOURCE_IMAGE}" "${POSTGRES_IMAGE}"
}

podman save --format oci-archive \
    --output "${OUTPUT_DIR}/devcloud-controller.tar" \
    "${CONTROLLER_IMAGE}"
podman save --format oci-archive \
    --output "${OUTPUT_DIR}/devcloud-worker.tar" \
    "${WORKER_IMAGE}"
podman save --format oci-archive \
    --output "${OUTPUT_DIR}/devcloud-postgresql-16.tar" \
    "${POSTGRES_IMAGE}"

(
    cd "${OUTPUT_DIR}"
    sha256sum ./*.tar > SHA256SUMS
)

echo "Offline AI Factory runtime images exported to ${OUTPUT_DIR}"
