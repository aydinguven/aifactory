#!/usr/bin/env bash
# Install an air-gapped AI Factory CPU worker and its outbound-only agent service.
set -Eeuo pipefail

log() {
    printf '[aifactory-worker-install] %s\n' "$*"
}

fail() {
    printf '[aifactory-worker-install] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail "Run with sudo: sudo bash deploy/deploy_worker_offline.sh"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELS_DIR="${PROJECT_DIR}/offline/wheels"
IMAGES_DIR="${PROJECT_DIR}/offline/images"
WORKER_IMAGES_DIR="${PROJECT_DIR}/offline/worker-images"
WORKSPACES_DIR="/var/lib/aifactory/workspaces"
VERSION="$(cd "${PROJECT_DIR}" && python3 -c 'from app import __version__; print(__version__)')"
WORKER_IMAGE="localhost/aifactory-worker:${VERSION}"
WORKER_ENV_FILE="/etc/aifactory/worker.env"
WORKER_ENV_SOURCE="${AIFACTORY_WORKER_ENV_FILE:-${WORKER_ENV_FILE}}"

[[ -d "${WHEELS_DIR}" ]] || fail "Offline wheels directory is missing: ${WHEELS_DIR}"
[[ -d "${IMAGES_DIR}" ]] || fail "Offline image directory is missing: ${IMAGES_DIR}"
[[ -d "${WORKER_IMAGES_DIR}" ]] || fail "Offline worker image directory is missing: ${WORKER_IMAGES_DIR}"
[[ -f "${WORKER_ENV_SOURCE}" ]] || fail \
    "Create ${WORKER_ENV_FILE} from deploy/worker.env.example before installation, or set AIFACTORY_WORKER_ENV_FILE."

for required_key in AIFACTORY_NODE_ID AIFACTORY_NODE_TOKEN; do
    grep -Eq "^${required_key}=[^[:space:]]+$" "${WORKER_ENV_SOURCE}" || fail \
        "${required_key} is missing from ${WORKER_ENV_SOURCE}."
done
if ! grep -Eq '^AIFACTORY_CONTROLLER_URL=https?://[^[:space:]]+$' "${WORKER_ENV_SOURCE}"; then
    grep -Eq '^AIFACTORY_MASTER_URL=https?://[^[:space:]]+$' "${WORKER_ENV_SOURCE}" || fail \
        "AIFACTORY_CONTROLLER_URL must start with http:// or https://."
fi
grep -Eq '^AIFACTORY_NODE_ID=(replace-with-node-id)?$' "${WORKER_ENV_SOURCE}" && fail \
    "Replace the placeholder AIFACTORY_NODE_ID in ${WORKER_ENV_SOURCE}."
grep -Eq '^AIFACTORY_NODE_TOKEN=(replace-with-node-token)?$' "${WORKER_ENV_SOURCE}" && fail \
    "Replace the placeholder AIFACTORY_NODE_TOKEN in ${WORKER_ENV_SOURCE}."

bash "${PROJECT_DIR}/deploy/install_offline_system_packages.sh" "${PROJECT_DIR}"

log "Installing worker prerequisites from configured Satellite/Foreman repositories..."
dnf install -y \
    gnupg2 python3 python3-pip policycoreutils-python-utils \
    podman crun tar gzip || fail \
    "Configured repositories could not install worker prerequisites. Register this VM with Satellite/Foreman, confirm its repositories are enabled, and rerun."

command -v python3 >/dev/null 2>&1 || fail "Configured repositories did not provide Python 3."
command -v podman >/dev/null 2>&1 || fail "Configured repositories did not provide Podman."

OCI_RUNTIME="$(command -v crun 2>/dev/null || command -v runc 2>/dev/null || true)"
[[ -n "${OCI_RUNTIME}" ]] || fail "Bundled RPM installation did not provide crun or runc."

log "Verifying the worker bundle and target runtime..."
python3 "${PROJECT_DIR}/deploy/package_offline.py" \
    --verify "${PROJECT_DIR}" --check-runtime --expected-role worker

export XDG_RUNTIME_DIR="/run/user/0"
install -d -m 0700 /run/user/0 /run/containers/storage
install -d -m 0755 /var/lib/containers/storage
install -d -m 0755 /etc/containers/containers.conf.d
cat > /etc/containers/containers.conf.d/00-runtime.conf <<EOF
[engine]
runtime = "${OCI_RUNTIME}"
EOF

log "Preparing worker storage and SELinux labels..."
AIFACTORY_SERVICE_USER=root bash "${PROJECT_DIR}/deploy/configure_selinux.sh"

log "Installing Python packages from the offline wheel set..."
cd "${PROJECT_DIR}"
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --no-index --find-links="${WHEELS_DIR}" -r requirements.txt

log "Checking for legacy embedded workspace images..."
IMAGE_COUNT=0
for image_archive in "${IMAGES_DIR}"/*.tar; do
    [[ -f "${image_archive}" ]] || continue
    podman load -i "${image_archive}"
    IMAGE_COUNT=$((IMAGE_COUNT + 1))
done
log "Loaded ${IMAGE_COUNT} legacy image archive(s); managed images synchronize after enrollment."

log "Loading the verified AI Factory worker runtime image..."
for image_archive in "${WORKER_IMAGES_DIR}"/*.tar; do
    [[ -f "${image_archive}" ]] || continue
    podman load -i "${image_archive}"
done
podman image exists "${WORKER_IMAGE}" || fail "Worker image is missing after archive load: ${WORKER_IMAGE}"

log "Installing worker enrollment and rootful Podman Quadlet..."
install -d -m 0755 /etc/aifactory
if [[ "${WORKER_ENV_SOURCE}" != "${WORKER_ENV_FILE}" ]]; then
    install -m 0600 "${WORKER_ENV_SOURCE}" "${WORKER_ENV_FILE}"
else
    chmod 0600 "${WORKER_ENV_FILE}"
fi

install -d -m 0755 /etc/containers/systemd
sed -e "s|{{WORKER_IMAGE}}|${WORKER_IMAGE}|g" \
    -e "s|{{WORKSPACE_ROOT}}|${WORKSPACES_DIR}|g" \
    "${PROJECT_DIR}/deploy/container/quadlet/aifactory-worker.container" \
    > /etc/containers/systemd/aifactory-worker.container
chmod 0644 /etc/containers/systemd/aifactory-worker.container

systemctl daemon-reload
systemctl enable --now podman.socket
systemctl enable --now aifactory-worker.service

podman exec aifactory-worker python -m app.installer.verify_worker
log "Rootful container worker installed and connected to the configured controller."
log "Follow logs with: sudo journalctl -u aifactory-worker -f"
