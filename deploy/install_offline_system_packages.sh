#!/usr/bin/env bash
# Bootstrap subscription-manager from the air-gap bundle's local DNF repository.
set -Eeuo pipefail

log() {
    printf '[aifactory-system-rpms] %s\n' "$*"
}

fail() {
    printf '[aifactory-system-rpms] ERROR: %s\n' "$*" >&2
    exit 1
}

configure_dnf_repository_options() {
    local help_text
    help_text="$(dnf --help 2>&1)" || fail "Could not inspect the installed DNF command."
    if [[ "${help_text}" == *"--disable-repo"* ]]; then
        DNF_DISABLE_REPOSITORIES="--disable-repo=*"
        DNF_ENABLE_REPOSITORY="--enable-repo=aifactory-offline"
    elif [[ "${help_text}" == *"--disablerepo"* ]]; then
        DNF_DISABLE_REPOSITORIES="--disablerepo=*"
        DNF_ENABLE_REPOSITORY="--enablerepo=aifactory-offline"
    else
        fail "The installed DNF command has no supported repository-disable option."
    fi
    if [[ "${help_text}" == *"--no-gpgchecks"* ]]; then
        DNF_NO_GPG_CHECKS="--no-gpgchecks"
    elif [[ "${help_text}" == *"--nogpgcheck"* ]]; then
        DNF_NO_GPG_CHECKS="--nogpgcheck"
    else
        fail "The installed DNF command has no supported GPG-check disable option."
    fi
}

[[ "$(id -u)" -eq 0 ]] || fail "Run this installer as root."

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RPM_ROOT="${PROJECT_DIR}/offline/system-rpms"

if [[ ! -d "${RPM_ROOT}" ]]; then
    log "This legacy bundle contains no system RPM set; using installed prerequisites."
    exit 0
fi

[[ -r /etc/os-release ]] || fail "/etc/os-release is required."
# shellcheck disable=SC1091
source /etc/os-release
DISTRIBUTION_ID="${ID,,}"
MAJOR_VERSION="${VERSION_ID%%.*}"
ARCHITECTURE="$(uname -m)"

case "${DISTRIBUTION_ID}" in
    rocky|rhel) ;;
    *) fail "Only Rocky Linux and RHEL 9/10 RPM profiles are supported; detected ${ID:-unknown}." ;;
esac
[[ "${MAJOR_VERSION}" == "9" || "${MAJOR_VERSION}" == "10" ]] || fail \
    "Offline bundles support major versions 9 and 10; detected ${VERSION_ID:-unknown}."
[[ "${ARCHITECTURE}" == "x86_64" ]] || fail \
    "This bundle supports x86_64; detected ${ARCHITECTURE}."

PROFILE="${DISTRIBUTION_ID}-${MAJOR_VERSION}-x86_64"
RPM_DIR="${RPM_ROOT}/${PROFILE}"
[[ -d "${RPM_DIR}" ]] || fail \
    "No RPM profile for ${PROFILE}. Use a bundle built on the same distribution."
command -v dnf >/dev/null 2>&1 || fail "DNF is required to install the bundled RPM transaction."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required to verify bundled RPMs."
[[ -f "${RPM_ROOT}/SHA256SUMS" ]] || fail "The system RPM checksum index is missing."
REQUESTED_PACKAGES_FILE="${RPM_DIR}/REQUESTED_PACKAGES"
[[ -f "${REQUESTED_PACKAGES_FILE}" ]] || fail \
    "The ${PROFILE} requested-package list is missing."
[[ -f "${RPM_DIR}/repodata/repomd.xml" ]] || fail \
    "The ${PROFILE} DNF repository metadata is missing."

log "Verifying bundled system RPM checksums..."
(cd "${RPM_ROOT}" && sha256sum -c SHA256SUMS)

grep -Fxq "subscription-manager" "${REQUESTED_PACKAGES_FILE}" || fail \
    "The ${PROFILE} bootstrap repository does not include subscription-manager."

if command -v subscription-manager >/dev/null 2>&1; then
    log "subscription-manager is already installed; leaving system packages to configured repositories."
    exit 0
fi

log "Installing only subscription-manager from the verified ${PROFILE} bootstrap repository..."
configure_dnf_repository_options
dnf \
    "${DNF_DISABLE_REPOSITORIES}" \
    "--repofrompath=aifactory-offline,file://${RPM_DIR}" \
    "${DNF_ENABLE_REPOSITORY}" \
    "${DNF_NO_GPG_CHECKS}" \
    install -y subscription-manager

command -v subscription-manager >/dev/null 2>&1 || fail \
    "Bundled RPM installation did not provide subscription-manager."

log "subscription-manager is ready. Remaining packages will come from configured Satellite/Foreman repositories."
