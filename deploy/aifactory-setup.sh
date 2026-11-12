#!/usr/bin/env bash
# Bootstrap the interactive AI Factory lifecycle installer on Rocky/RHEL 9 or 10.
set -Eeuo pipefail

log() {
    printf '[aifactory-setup] %s\n' "$*"
}

fail() {
    printf '[aifactory-setup] ERROR: %s\n' "$*" >&2
    exit 1
}

dnf_disable_repositories_option() {
    local help_text
    help_text="$(dnf --help 2>&1)" || fail "Could not inspect the installed DNF command."
    if [[ "${help_text}" == *"--disable-repo"* ]]; then
        printf '%s\n' "--disable-repo=*"
    elif [[ "${help_text}" == *"--disablerepo"* ]]; then
        printf '%s\n' "--disablerepo=*"
    else
        fail "The installed DNF command has no supported repository-disable option."
    fi
}

[[ "$(id -u)" -eq 0 ]] || fail "Run aifactory-setup as root."
# Hardened hosts often set a 027/077 root umask; files created here must stay
# readable by the service user (secrets are written with explicit modes).
umask 0022
[[ -r /etc/os-release ]] || fail "/etc/os-release is required."
# shellcheck disable=SC1091
source /etc/os-release

DISTRIBUTION_ID="${ID,,}"
MAJOR_VERSION="${VERSION_ID%%.*}"
ARCHITECTURE="$(uname -m)"
case "${DISTRIBUTION_ID}" in
    rocky|rhel) ;;
    *) fail "Only Rocky Linux and RHEL 9/10 are supported; detected ${ID:-unknown}." ;;
esac
case "${MAJOR_VERSION}" in
    10)
        PYTHON_BIN=/usr/bin/python3
        PYTHON_PACKAGES=(python3)
        ;;
    9)
        # EL9's platform python3 is 3.9; AI Factory needs the AppStream 3.12
        # interpreter that matches the bundled cp312 wheels.
        PYTHON_BIN=/usr/bin/python3.12
        PYTHON_PACKAGES=(python3.12 python3.12-pip)
        ;;
    *) fail "Only Rocky/RHEL major versions 9 and 10 are supported; detected ${VERSION_ID:-unknown}." ;;
esac
[[ "${ARCHITECTURE}" == "x86_64" ]] || fail \
    "Only x86_64 is supported; detected ${ARCHITECTURE}."
command -v dnf >/dev/null 2>&1 || fail "DNF is required."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="${DISTRIBUTION_ID}-${MAJOR_VERSION}-x86_64"
BOOTSTRAP_RPMS="${PROJECT_DIR}/offline/bootstrap-rpms/${PROFILE}"
SYSTEM_RPMS="${PROJECT_DIR}/offline/system-rpms/${PROFILE}"
OFFLINE_MANIFEST="${PROJECT_DIR}/offline/MANIFEST.json"
OFFLINE_BUNDLE=0

if [[ -f "${OFFLINE_MANIFEST}" ]]; then
    [[ -d "${SYSTEM_RPMS}" ]] || fail \
        "The offline bundle has no system RPM repository for ${PROFILE}."
    log "Offline bundle detected; verifying and installing from its bundled DNF repository..."
    bash "${PROJECT_DIR}/deploy/install_offline_system_packages.sh" "${PROJECT_DIR}"
    OFFLINE_BUNDLE=1
    export AIFACTORY_OFFLINE_INSTALL=1
fi

install_subscription_manager() {
    command -v subscription-manager >/dev/null 2>&1 && return 0
    if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
        fail "The verified offline RPM transaction did not install subscription-manager."
    fi
    log "Installing subscription-manager..."
    if dnf install -y subscription-manager; then
        return 0
    fi
    if [[ ! -d "${BOOTSTRAP_RPMS}" && -d "${SYSTEM_RPMS}" ]]; then
        BOOTSTRAP_RPMS="${SYSTEM_RPMS}"
    fi
    [[ -d "${BOOTSTRAP_RPMS}" ]] || fail \
        "subscription-manager is missing, configured repositories could not install it, and no ${PROFILE} bootstrap RPM closure is bundled."
    mapfile -d '' rpms < <(find "${BOOTSTRAP_RPMS}" -maxdepth 1 -type f -name '*.rpm' -print0 | sort -z)
    [[ "${#rpms[@]}" -gt 0 ]] || fail "The subscription-manager bootstrap RPM directory is empty."
    DNF_DISABLE_REPOSITORIES="$(dnf_disable_repositories_option)"
    dnf "${DNF_DISABLE_REPOSITORIES}" install -y "${rpms[@]}"
}

install_subscription_manager

if [[ "${OFFLINE_BUNDLE}" -eq 1 ]] && ! subscription-manager identity >/dev/null 2>&1; then
    cat <<'EOF'

[aifactory-setup] subscription-manager was installed successfully.
[aifactory-setup] Register this VM with Satellite/Foreman now, for example with your
[aifactory-setup] organization and activation key, then rerun:

    bash deploy/aifactory-setup.sh

[aifactory-setup] All remaining operating-system packages will be installed from
[aifactory-setup] the repositories enabled by that registration.
EOF
    exit 0
fi
if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
    log "Satellite/Foreman registration detected; configured repositories will provide remaining system packages."
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    log "Installing ${PYTHON_PACKAGES[*]} from configured repositories..."
    dnf install -y "${PYTHON_PACKAGES[@]}" || fail \
        "Configured repositories could not install ${PYTHON_PACKAGES[*]}. Register this VM with Satellite/Foreman, confirm its BaseOS/AppStream repositories are enabled, and rerun setup."
fi

if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
    log "Verifying the complete offline artifact manifest..."
    "${PYTHON_BIN}" "${PROJECT_DIR}/deploy/package_offline.py" \
        --verify "${PROJECT_DIR}" \
        --check-runtime
fi

export PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
# Subprocesses that switch to the service user must not inherit an inaccessible
# working directory such as /root/aifactory-worker.
cd /
exec "${PYTHON_BIN}" -m app.installer "$@"
