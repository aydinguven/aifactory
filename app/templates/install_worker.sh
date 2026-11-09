#!/usr/bin/env bash
# Enroll and install one AI Factory worker using a short-lived controller ticket.
set -Eeuo pipefail
umask 077

log() {
    printf '[devcloud-worker-bootstrap] %s\n' "$*"
}

fail() {
    printf '[devcloud-worker-bootstrap] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail \
    "Run through sudo: curl -fsSL <ticket-url> | sudo bash"

for required_command in base64 basename curl dirname grep hostname install mktemp printenv sha256sum stat tar; do
    command -v "${required_command}" >/dev/null 2>&1 || fail \
        "Required base command is missing: ${required_command}"
done

if ! command -v python3 >/dev/null 2>&1; then
    command -v dnf >/dev/null 2>&1 || fail "Python 3 and DNF are unavailable."
    log "Installing Python 3 from configured repositories..."
    dnf install -y python3 || fail "Configured repositories could not install Python 3."
fi

[[ ! -f /var/lib/devcloud/installer/install-state.json ]] || fail \
    "A managed AI Factory installation already exists; purge it or run update."

GPU_MODE="$(printenv DEVCLOUD_WORKER_GPU_MODE || true)"
[[ -n "$GPU_MODE" ]] || GPU_MODE=auto
[[ "$GPU_MODE" == "auto" || "$GPU_MODE" == "required" ]] || fail \
    "DEVCLOUD_WORKER_GPU_MODE must be auto or required."

NVIDIA_HARDWARE=false
for vendor_file in /sys/bus/pci/devices/*/vendor; do
    [[ -r "$vendor_file" ]] || continue
    class_file="$(dirname "$vendor_file")/class"
    [[ -r "$class_file" ]] || continue
    read -r vendor_id < "$vendor_file"
    read -r device_class < "$class_file"
    if [[ "$vendor_id" == "0x10de" && "$device_class" == 0x03* ]]; then
        NVIDIA_HARDWARE=true
        break
    fi
done

if [[ "$GPU_MODE" == "required" && "$NVIDIA_HARDWARE" != "true" ]]; then
    fail "GPU worker requested, but no NVIDIA display/3D PCI device was detected."
fi

if [[ "$NVIDIA_HARDWARE" == "true" ]]; then
    log "NVIDIA GPU detected; validating the existing host GPU stack..."
    command -v nvidia-smi >/dev/null 2>&1 || fail \
        "NVIDIA driver is missing: nvidia-smi was not found. Install/repair the host driver, then run this same command again."
    NVIDIA_GPU_LIST="$(nvidia-smi -L 2>&1)" || fail \
        "NVIDIA driver check failed: $NVIDIA_GPU_LIST. Install/repair the host driver, then retry."
    grep -q '^GPU [0-9]' <<<"$NVIDIA_GPU_LIST" || fail \
        "nvidia-smi did not report a usable physical GPU."
    command -v nvidia-ctk >/dev/null 2>&1 || fail \
        "NVIDIA Container Toolkit is missing: nvidia-ctk was not found. Install it on the host, then retry."
    NVIDIA_CDI_LIST="$(nvidia-ctk cdi list 2>&1)" || fail \
        "NVIDIA CDI validation failed: $NVIDIA_CDI_LIST. Configure/refresh NVIDIA CDI, then retry."
    grep -q 'nvidia\.com/gpu=' <<<"$NVIDIA_CDI_LIST" || fail \
        "NVIDIA Container Toolkit returned no CDI GPU devices. Configure/refresh CDI, then retry."
    log "NVIDIA preflight passed: driver, Container Toolkit, and CDI devices are ready."
    WORKER_RUNTIME=native
    log "Selecting the native worker agent so host GPU/CDI telemetry remains visible."
else
    log "No NVIDIA GPU detected; registering this machine as a CPU worker."
    WORKER_RUNTIME=container
fi

CONTROLLER_URL=__CONTROLLER_URL__
ENROLLMENT_URL=__ENROLLMENT_URL__
CONTROLLER_HOST=__CONTROLLER_HOST__
CONTROLLER_PORT=__CONTROLLER_PORT__
CONTROLLER_FALLBACK_IPV4=__CONTROLLER_FALLBACK_IPV4__
AGENT_CA_BASE64=__AGENT_CA_BASE64__
AGENT_CA_SHA256=__AGENT_CA_SHA256__
WORKER_NAME="${DEVCLOUD_WORKER_NAME:-}"
if [[ -z "${WORKER_NAME}" ]]; then
    [[ -r /dev/tty && -w /dev/tty ]] || fail \
        "Set DEVCLOUD_WORKER_NAME when running without a terminal."
    DEFAULT_WORKER_NAME="$(hostname -s)"
    printf 'Worker name [%s]: ' "${DEFAULT_WORKER_NAME}" >/dev/tty
    read -r entered_worker_name </dev/tty
    WORKER_NAME="${entered_worker_name:-${DEFAULT_WORKER_NAME}}"
fi
[[ "${WORKER_NAME}" =~ ^[a-zA-Z0-9_.-]{2,100}$ ]] || fail \
    "Worker name must be 2-100 letters, digits, dots, underscores, or hyphens."

TEMP_DIR="$(mktemp -d -t devcloud-worker-bootstrap.XXXXXX)"
cleanup() {
    rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

AGENT_CA_FILE=""
CURL_TLS_ARGS=()
if [[ -n "${AGENT_CA_BASE64}" ]]; then
    log "Installing the controller worker trust bundle..."
    AGENT_CA_TEMP="${TEMP_DIR}/controller-ca.pem"
    printf '%s' "${AGENT_CA_BASE64}" | base64 --decode > "${AGENT_CA_TEMP}" || fail \
        "Controller CA bundle could not be decoded."
    printf '%s  %s\n' "${AGENT_CA_SHA256}" "${AGENT_CA_TEMP}" | sha256sum -c - >/dev/null || fail \
        "Controller CA bundle checksum validation failed."
    install -d -o root -g root -m 0755 /etc/devcloud/pki
    install -o root -g root -m 0644 \
        "${AGENT_CA_TEMP}" /etc/devcloud/pki/controller-ca.pem
    AGENT_CA_FILE=/etc/devcloud/pki/controller-ca.pem
    CURL_TLS_ARGS=(--cacert "${AGENT_CA_FILE}")
fi

reconcile_controller_fallback_hosts() {
    CONTROLLER_HOST="${CONTROLLER_HOST}" \
    CONTROLLER_FALLBACK_IPV4="${1:-}" \
    python3 - <<'PY'
import os
from pathlib import Path

path = Path("/etc/hosts")
marker = "# devcloud-controller-fallback"
hostname = os.environ["CONTROLLER_HOST"]
fallback = os.environ.get("CONTROLLER_FALLBACK_IPV4", "")
original = path.read_text(encoding="utf-8")
lines = [line for line in original.splitlines() if marker not in line]
if fallback:
    lines.append(f"{fallback}\t{hostname}\t{marker} {hostname}")
payload = "\n".join(lines) + "\n"
if payload != original:
    path.write_text(payload, encoding="utf-8")
PY
}

# Remove a stale managed route before testing normal DNS, then install the
# current fallback only if the primary network path is unavailable.
reconcile_controller_fallback_hosts ""

log "Checking the controller HTTPS FQDN route..."
if curl --fail --silent --show-error --output /dev/null \
    "${CURL_TLS_ARGS[@]}" "${CONTROLLER_URL}/healthz"; then
    log "Controller FQDN is reachable through the primary resolver."
else
    primary_status=$?
    case "${primary_status}" in
        5|6|7|28)
            [[ -n "${CONTROLLER_FALLBACK_IPV4}" ]] || fail \
                "Controller FQDN route failed and no fallback IPv4 is configured."
            log "Primary FQDN route failed; validating the configured IPv4 fallback without changing TLS identity..."
            curl --fail --silent --show-error --output /dev/null \
                "${CURL_TLS_ARGS[@]}" \
                --resolve "${CONTROLLER_HOST}:${CONTROLLER_PORT}:${CONTROLLER_FALLBACK_IPV4}" \
                "${CONTROLLER_URL}/healthz" || fail \
                "Configured fallback IPv4 cannot reach the controller with valid FQDN TLS."
            reconcile_controller_fallback_hosts "${CONTROLLER_FALLBACK_IPV4}"
            curl --fail --silent --show-error --output /dev/null \
                "${CURL_TLS_ARGS[@]}" "${CONTROLLER_URL}/healthz" || fail \
                "Controller fallback route was installed but did not become reachable."
            log "Controller FQDN identity is using the validated IPv4 route fallback."
            ;;
        *)
            fail "Controller HTTPS validation failed (curl exit ${primary_status}); check the FQDN certificate and worker CA bundle."
            ;;
    esac
fi

log "Enrolling ${WORKER_NAME} with the controller..."
ENROLLMENT_PAYLOAD="$(WORKER_NAME="${WORKER_NAME}" python3 -c \
    'import json, os; print(json.dumps({"name": os.environ["WORKER_NAME"]}))')"
ENROLLMENT_RESPONSE="${TEMP_DIR}/enrollment.json"
curl --fail-with-body --silent --show-error \
    "${CURL_TLS_ARGS[@]}" \
    --request POST \
    --header 'Content-Type: application/json' \
    --data "${ENROLLMENT_PAYLOAD}" \
    --output "${ENROLLMENT_RESPONSE}" \
    "${ENROLLMENT_URL}"

mapfile -t CREDENTIALS < <(python3 -c \
    'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(value["node_id"]); print(value["enrollment_token"]); print(value["controller_url"])' \
    "${ENROLLMENT_RESPONSE}")
[[ "${#CREDENTIALS[@]}" -eq 3 ]] || fail "Controller returned invalid enrollment data."
NODE_ID="${CREDENTIALS[0]}"
NODE_TOKEN="${CREDENTIALS[1]}"
CONTROLLER_URL="${CREDENTIALS[2]%/}"
[[ -n "${NODE_ID}" && -n "${NODE_TOKEN}" ]] || fail \
    "Controller returned empty worker credentials."

CURL_AUTH_CONFIG="${TEMP_DIR}/curl-auth.conf"
printf 'header = "Authorization: Bearer %s"\n' "${NODE_TOKEN}" > "${CURL_AUTH_CONFIG}"
chmod 0600 "${CURL_AUTH_CONFIG}"
RELEASE_METADATA="${TEMP_DIR}/release.json"
log "Resolving the controller's current platform release..."
curl --fail-with-body --silent --show-error \
    "${CURL_TLS_ARGS[@]}" \
    --config "${CURL_AUTH_CONFIG}" \
    --output "${RELEASE_METADATA}" \
    "${CONTROLLER_URL}/api/agent/releases/latest?node_id=${NODE_ID}"

mapfile -t RELEASE < <(python3 -c \
    'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(value["filename"]); print(value["url"]); print(value["sha256"]); print(value["size"])' \
    "${RELEASE_METADATA}")
[[ "${#RELEASE[@]}" -eq 4 ]] || fail "Controller returned invalid release metadata."
BUNDLE_FILENAME="${RELEASE[0]}"
BUNDLE_URL="${RELEASE[1]}"
EXPECTED_SHA256="${RELEASE[2]}"
EXPECTED_SIZE="${RELEASE[3]}"
[[ "${BUNDLE_FILENAME}" == "$(basename -- "${BUNDLE_FILENAME}")" ]] || fail \
    "Controller returned an unsafe release filename."
if [[ "${BUNDLE_URL}" == /* ]]; then
    BUNDLE_URL="${CONTROLLER_URL}${BUNDLE_URL}"
elif [[ "${BUNDLE_URL}" != "${CONTROLLER_URL}/api/agent/releases/"* ]]; then
    fail "Controller returned a release URL outside its canonical FQDN origin."
fi
[[ "${BUNDLE_URL}" =~ ^https://[^[:space:]]+$ ]] || fail \
    "Controller returned an invalid HTTPS release URL."
[[ "${EXPECTED_SHA256}" =~ ^[0-9a-f]{64}$ ]] || fail \
    "Controller returned an invalid release checksum."
[[ "${EXPECTED_SIZE}" =~ ^[0-9]+$ ]] || fail \
    "Controller returned an invalid release size."

BUNDLE_PATH="${TEMP_DIR}/${BUNDLE_FILENAME}"
log "Downloading ${BUNDLE_FILENAME}..."
curl --fail-with-body --silent --show-error \
    "${CURL_TLS_ARGS[@]}" \
    --config "${CURL_AUTH_CONFIG}" \
    --output "${BUNDLE_PATH}" \
    "${BUNDLE_URL}"
[[ "$(stat -c '%s' "${BUNDLE_PATH}")" == "${EXPECTED_SIZE}" ]] || fail \
    "Downloaded release size does not match controller metadata."
printf '%s  %s\n' "${EXPECTED_SHA256}" "${BUNDLE_FILENAME}" \
    > "${TEMP_DIR}/${BUNDLE_FILENAME}.sha256"
(cd "${TEMP_DIR}" && sha256sum -c "${BUNDLE_FILENAME}.sha256")

RELEASE_DIRECTORY="$(python3 -c \
    'import sys, tarfile; archive=tarfile.open(sys.argv[1]); roots={name.split("/", 1)[0] for name in archive.getnames() if name}; print(next(iter(roots)) if len(roots) == 1 else "")' \
    "${BUNDLE_PATH}")"
[[ "${RELEASE_DIRECTORY}" =~ ^devcloud-[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail \
    "Platform release has an unexpected top-level directory."
tar -xf "${BUNDLE_PATH}" -C "${TEMP_DIR}"
RELEASE_ROOT="${TEMP_DIR}/${RELEASE_DIRECTORY}"
[[ -f "${RELEASE_ROOT}/deploy/devcloud-setup.sh" ]] || fail \
    "Platform release does not contain the managed installer."

TOKEN_FILE="${TEMP_DIR}/enrollment-token"
printf '%s\n' "${NODE_TOKEN}" > "${TOKEN_FILE}"
chmod 0600 "${TOKEN_FILE}"
export DEVCLOUD_INSTALL_CONTROLLER_URL="${CONTROLLER_URL}"
export DEVCLOUD_INSTALL_CONTROLLER_FALLBACK_IPV4="${CONTROLLER_FALLBACK_IPV4}"
export DEVCLOUD_INSTALL_AGENT_CA_FILE="${AGENT_CA_FILE}"
export DEVCLOUD_INSTALL_WORKER_ID="${NODE_ID}"
export DEVCLOUD_INSTALL_TOKEN_FILE="${TOKEN_FILE}"
export DEVCLOUD_INSTALL_WORKER_NAME="${WORKER_NAME}"
export DEVCLOUD_INSTALL_WORKSPACE_ROOT="${STORAGE_ROOT:-/var/lib/devcloud/workspaces}"
export DEVCLOUD_INSTALL_PRELOAD_IMAGES=false
export DEVCLOUD_INSTALL_WORKER_RUNTIME="$WORKER_RUNTIME"

log "Installing the verified worker release..."
bash "${RELEASE_ROOT}/deploy/devcloud-setup.sh" --yes install worker
log "Worker ${WORKER_NAME} enrolled and installed successfully."
