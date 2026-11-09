#!/usr/bin/env bash
# ==============================================================================
# AI Factory Air-Gapped / Offline Linux VM Deployment Script
# Specifically optimized for Fedora-based distros (Rocky Linux, RHEL, Fedora, CentOS)
# ==============================================================================
set -euo pipefail

echo "=============================================================================="
echo "Starting AI Factory Offline Deployment (Fedora / Rocky / RHEL Ecosystem)"
echo "=============================================================================="

# 0. Ensure root privileges for systemd and container storage configuration
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: deploy_offline.sh must be run as root or with sudo:"
    echo "  sudo bash deploy/deploy_offline.sh"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE_DIR="${PROJECT_DIR}/offline"
WHEELS_DIR="${OFFLINE_DIR}/wheels"
IMAGES_DIR="${OFFLINE_DIR}/images"
WORKSPACES_DIR="/var/lib/devcloud/workspaces"
# Fix Podman RunRoot & Storage permissions (prevent "RunRoot is pointing to a path which is not writeable")
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/0}"
mkdir -p /run/user/0 /run/containers/storage /var/lib/containers/storage
chmod 0700 /run/user/0 /run/containers/storage 2>/dev/null || true

# 1. Install and verify bundled operating-system prerequisites
bash "${PROJECT_DIR}/deploy/install_offline_system_packages.sh" "${PROJECT_DIR}"

echo "--> Installing operating-system prerequisites from configured Satellite/Foreman repositories..."
if ! dnf install -y \
    gnupg2 python3 python3-pip policycoreutils-python-utils \
    podman crun tar gzip nginx curl createrepo_c postgresql-server postgresql; then
    echo "ERROR: Configured repositories could not install AI Factory prerequisites."
    echo "Register this VM with Satellite/Foreman, confirm its repositories are enabled, and rerun."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not found after the configured repository installation."
    exit 1
fi

if ! command -v podman >/dev/null 2>&1; then
    echo "ERROR: podman is not found after the configured repository installation."
    exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
    echo "ERROR: nginx is not found after the configured repository installation."
    exit 1
fi

# Auto-detect and configure OCI runtime (crun or runc)
echo "--> Detecting OCI container runtime (crun / runc)..."
OCI_RUNTIME=""
if command -v crun >/dev/null 2>&1; then
    OCI_RUNTIME="crun"
elif command -v runc >/dev/null 2>&1; then
    OCI_RUNTIME="runc"
elif [ -x "/usr/bin/crun" ]; then
    OCI_RUNTIME="/usr/bin/crun"
elif [ -x "/usr/bin/runc" ]; then
    OCI_RUNTIME="/usr/bin/runc"
elif [ -x "/usr/local/bin/crun" ]; then
    OCI_RUNTIME="/usr/local/bin/crun"
elif [ -x "/usr/local/bin/runc" ]; then
    OCI_RUNTIME="/usr/local/bin/runc"
fi

if [ -z "$OCI_RUNTIME" ]; then
    echo "=============================================================================="
    echo "ERROR: No OCI container runtime found (neither crun nor runc)."
    echo "Podman requires an OCI runtime to create containers."
    echo "The bundled RPM transaction must provide crun or runc."
    echo "=============================================================================="
    exit 1
fi

echo "--> Configured OCI runtime: ${OCI_RUNTIME}"
mkdir -p /etc/containers/containers.conf.d
cat <<EOF > /etc/containers/containers.conf.d/00-runtime.conf
[engine]
runtime = "${OCI_RUNTIME}"
EOF

if [ ! -d "${WHEELS_DIR}" ]; then
    echo "ERROR: Offline wheels directory (${WHEELS_DIR}) not found!"
    exit 1
fi

echo "--> Verifying air-gap artifact manifest and checksums..."
python3 "${PROJECT_DIR}/deploy/package_offline.py" --verify "${PROJECT_DIR}" --check-runtime --expected-role server

# Print Detected Environment Info
PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
echo "--> Detected Python runtime: ${PYTHON_VER}"
echo "--> Detected Host OS: $(cat /etc/redhat-release 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 || echo 'Linux')"

# 2. Setup Persistent Storage Directory and SELinux
echo "=== [1/5] Configuring workspace persistent storage directory ==="
echo "--> Configuring persistent SELinux workspace labels when supported..."
DEVCLOUD_SERVICE_USER="root" bash "${PROJECT_DIR}/deploy/configure_selinux.sh"

# 3. Create Virtualenv and Install Cached Linux Wheels
echo "=== [2/5] Installing Python packages from offline wheels ==="
cd "${PROJECT_DIR}"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install from offline wheels directory without connecting to PyPI
pip install --no-index --find-links="${WHEELS_DIR}" -r requirements.txt

# 4. Load Container Images into Podman
echo "=== [3/5] Loading container images into Podman ==="
IMAGE_COUNT=0
if [ -d "${IMAGES_DIR}" ]; then
    for tar_file in "${IMAGES_DIR}"/*.tar "${IMAGES_DIR}"/*.tar.gz; do
        if [ -f "$tar_file" ]; then
            echo "--> Loading image: $(basename "$tar_file")..."
            podman load -i "$tar_file"
            IMAGE_COUNT=$((IMAGE_COUNT + 1))
        fi
    done
fi

echo "Loaded ${IMAGE_COUNT} legacy image archive(s). Managed workspace images are added in Admin after installation."

echo "Available Podman images:"
podman images | grep -E "devcloud|REPOSITORY" || true

# 5. Configure the Nginx HTTP ingress. HTTPS can be enabled later in Admin.
echo "=== [4/5] Configuring Nginx ingress on port 80 ==="
bash "${PROJECT_DIR}/deploy/install_ingress.sh" root

# 6. Install and Start Systemd Service
echo "=== [5/5] Configuring and starting systemd service ==="
SERVICE_FILE="/etc/systemd/system/devcloud.service"
sed -e "s|{{USER}}|root|g" \
    -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
    "${PROJECT_DIR}/deploy/devcloud.service" | tee "${SERVICE_FILE}" > /dev/null

systemctl daemon-reload
systemctl enable devcloud
bash "${PROJECT_DIR}/deploy/restart.sh"

IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1')

echo "=============================================================================="
echo "AI Factory Deployment Completed Successfully on Fedora-based VM!"
echo "Status: Active and running on systemd service 'devcloud'"
echo "Dashboard URL: http://${IP_ADDR}"
echo "Check logs: sudo journalctl -u devcloud -f"
echo "=============================================================================="
