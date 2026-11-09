#!/usr/bin/env bash
# Install the immutable root helper and systemd watcher for in-app HTTPS changes.
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: install_ingress.sh must run as root." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${1:-root}"
if [[ "${SERVICE_USER}" =~ ^[0-9]+$ ]] && ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    SERVICE_GROUP="${SERVICE_USER}"
else
    SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
fi
HTTPS_HOSTNAME="${AIFACTORY_HTTPS_HOSTNAME:-127.0.0.1}"
APPLY_INITIAL="${AIFACTORY_INGRESS_APPLY_INITIAL:-1}"
HELPER="/usr/local/libexec/aifactory-apply-ingress"
STAGING_ROOT="/var/lib/aifactory/ingress"
REQUEST_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

case "${APPLY_INITIAL}" in
    0|1) ;;
    *)
        echo "ERROR: AIFACTORY_INGRESS_APPLY_INITIAL must be 0 or 1." >&2
        exit 1
        ;;
esac

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "${PROJECT_DIR}/deploy/apply_ingress.py" "${HELPER}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 "${STAGING_ROOT}"

if command -v getenforce >/dev/null 2>&1 \
    && [[ "$(getenforce)" != "Disabled" ]] \
    && command -v setsebool >/dev/null 2>&1; then
    setsebool -P httpd_can_network_connect on
fi

if [[ ! -f "${STAGING_ROOT}/desired.json" ]]; then
    printf '{"hostname":"%s","http_fallback_enabled":true,"https_enabled":false,"request_id":"%s"}\n' "${HTTPS_HOSTNAME}" "${REQUEST_ID}" > "${STAGING_ROOT}/desired.json"
fi
chown "${SERVICE_USER}:${SERVICE_GROUP}" "${STAGING_ROOT}/desired.json"
chmod 0600 "${STAGING_ROOT}/desired.json"

install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/aifactory-ingress.service" /etc/systemd/system/aifactory-ingress.service
install -o root -g root -m 0644 "${PROJECT_DIR}/deploy/aifactory-ingress.path" /etc/systemd/system/aifactory-ingress.path

if [[ "${APPLY_INITIAL}" == "1" ]]; then
    "${HELPER}"
    systemctl enable nginx
else
    echo "Preserving the currently active Nginx configuration during upgrade."
fi
systemctl daemon-reload
systemctl enable --now aifactory-ingress.path
echo "AI Factory Nginx ingress is ready on port 80."
