#!/usr/bin/env bash
# ==============================================================================
# AI Factory Platform 1-Click Self-Update Script
# ==============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${AIFACTORY_PROJECT_DIR:-$(dirname "${SCRIPT_DIR}")}"
DATA_DIR="${PROJECT_DIR}/data"
LOCK_FILE="${DATA_DIR}/platform-update.lock"
RESTART_LOG="${DATA_DIR}/platform-update-restart.log"
SERVICE_FILE="/etc/systemd/system/aifactory.service"
UNIT_TMP=""

cleanup() {
    if [ -n "${UNIT_TMP}" ] && [ -f "${UNIT_TMP}" ]; then
        rm -f -- "${UNIT_TMP}"
    fi
}
trap cleanup EXIT

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

cd "${PROJECT_DIR}" || fail "Project directory is unavailable: ${PROJECT_DIR}"
mkdir -p "${DATA_DIR}"

command -v flock >/dev/null 2>&1 || fail "The 'flock' command is required."
exec 9>"${LOCK_FILE}"
flock -n 9 || fail "Another platform update is already running."

# Fail before pulling if the service account cannot update the systemd unit.
if [ "$(id -u)" -ne 0 ]; then
    sudo -n true >/dev/null 2>&1 || fail \
        "Passwordless sudo is required for the AI Factory service account."
fi

command -v git >/dev/null 2>&1 || fail "Git is not installed."
[ -d .git ] || fail "${PROJECT_DIR} is not a Git checkout."

TRACKED_CHANGES="$(git status --porcelain --untracked-files=no)"
if [ -n "${TRACKED_CHANGES}" ]; then
    echo "${TRACKED_CHANGES}" >&2
    fail "Tracked files contain local changes. Commit or revert them before updating."
fi

TARGET_BRANCH="${AIFACTORY_UPDATE_BRANCH:-main}"
git check-ref-format --branch "${TARGET_BRANCH}" >/dev/null 2>&1 || \
    fail "Invalid update branch: ${TARGET_BRANCH}"
OLD_COMMIT="$(git rev-parse HEAD)"

echo "--> [1/5] Fetching origin/${TARGET_BRANCH}..."
git fetch origin "${TARGET_BRANCH}"
if git show-ref --verify --quiet "refs/heads/${TARGET_BRANCH}"; then
    git switch "${TARGET_BRANCH}"
else
    git switch --track -c "${TARGET_BRANCH}" "origin/${TARGET_BRANCH}"
fi
git merge --ff-only "origin/${TARGET_BRANCH}"
NEW_COMMIT="$(git rev-parse HEAD)"
echo "    ${OLD_COMMIT:0:12} -> ${NEW_COMMIT:0:12}"

echo "--> [2/5] Checking Python dependencies..."
[ -x .venv/bin/python ] || fail "Python virtual environment is missing."
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

echo "--> [3/5] Installing the current AI Factory systemd unit..."
[ -f deploy/aifactory.service ] || fail "deploy/aifactory.service is missing."
SERVICE_USER="${AIFACTORY_SERVICE_USER:-}"
if [ -z "${SERVICE_USER}" ]; then
    SERVICE_USER="$(systemctl show --property User --value aifactory 2>/dev/null || true)"
fi
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
UNIT_TMP="$(mktemp)"
sed -e "s|{{USER}}|${SERVICE_USER}|g" \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
    deploy/aifactory.service >"${UNIT_TMP}"
sudo install -m 0644 "${UNIT_TMP}" "${SERVICE_FILE}"
sudo systemctl daemon-reload

echo "--> [4/5] Installing the HTTPS ingress helper and watcher..."
[ -f deploy/install_ingress.sh ] || fail "deploy/install_ingress.sh is missing."
sudo env AIFACTORY_INGRESS_APPLY_INITIAL=0 bash "${PROJECT_DIR}/deploy/install_ingress.sh" "${SERVICE_USER}"

echo "--> [5/5] Scheduling a health-checked worker reload..."
[ -f deploy/restart.sh ] || fail "deploy/restart.sh is missing."
nohup bash -c 'sleep 2; exec bash "$1"' _ "${PROJECT_DIR}/deploy/restart.sh" \
    >>"${RESTART_LOG}" 2>&1 </dev/null &
disown || true

echo "--> Update completed successfully."
echo "    Restart log: ${RESTART_LOG}"
