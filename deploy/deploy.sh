#!/usr/bin/env bash
# AI Factory Linux VM Deployment Script
set -e

echo "=== Starting AI Factory Platform Setup on Linux VM ==="

# 1. Detect Package Manager and Install Podman, crun & Python
if command -v apt-get >/dev/null 2>&1; then
    echo "Updating apt repositories and installing Podman & Python dependencies..."
    sudo apt-get update
    sudo apt-get install -y podman crun python3 python3-pip python3-venv git curl nginx
    if ! sudo apt-get install -y pigz; then
        echo "WARNING: pigz is unavailable; offline bundles will use slower single-threaded gzip."
    fi
elif command -v dnf >/dev/null 2>&1; then
    echo "Installing Podman & Python via DNF..."
    sudo dnf install -y podman crun python3 python3-pip git curl nginx createrepo_c policycoreutils-python-utils selinux-policy-targeted
    if ! sudo dnf install -y pigz; then
        echo "WARNING: pigz is unavailable; offline bundles will use slower single-threaded gzip."
    fi
else
    echo "Unsupported package manager. Please ensure Podman, crun, and Python 3.11+ are installed."
fi

# 2. Setup Workspace Directory and Permissions
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACES_DIR="/var/lib/aifactory/workspaces"

echo "Creating workspace directory at: ${WORKSPACES_DIR}"
sudo mkdir -p "${WORKSPACES_DIR}"
sudo chown "$USER:$USER" "${WORKSPACES_DIR}"
sudo chmod 755 "${WORKSPACES_DIR}"

DOWNLOADS_DIR="/srv/aifactory-downloads"
DOWNLOAD_BUILD_DIR="/var/lib/aifactory/download-builds"
echo "Preparing offline download publication directories..."
sudo install -d -o "$USER" -g "$USER" -m 0755 "${DOWNLOADS_DIR}"
sudo install -d -o "$USER" -g "$USER" -m 0750 "${DOWNLOAD_BUILD_DIR}"

if command -v restorecon >/dev/null 2>&1; then
    sudo restorecon -RF "${DOWNLOADS_DIR}" "${DOWNLOAD_BUILD_DIR}" || true
fi

echo "Configuring SELinux workspace labels when supported..."
AIFACTORY_SERVICE_USER="$USER" bash "${PROJECT_DIR}/deploy/configure_selinux.sh"

# 3. Create Python Virtual Environment & Install Dependencies
echo "Setting up Python virtual environment..."
cd "${PROJECT_DIR}"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Build Container Images
echo "Building local workspace container images..."
bash containers/build_images.sh

# 5. Setup Systemd Service
echo "Installing AI Factory systemd service..."
SERVICE_FILE="/etc/systemd/system/aifactory.service"
sudo sed -e "s|{{USER}}|$USER|g" \
         -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
         "${PROJECT_DIR}/deploy/aifactory.service" | sudo tee "${SERVICE_FILE}" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable aifactory
sudo bash "${PROJECT_DIR}/deploy/install_ingress.sh" "$USER"
bash "${PROJECT_DIR}/deploy/restart.sh"

echo "=== AI Factory Platform Deployed Successfully! ==="
echo "Access AI Factory in your browser at: http://$(hostname -I | awk '{print $1}')"
