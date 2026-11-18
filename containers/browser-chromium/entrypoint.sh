#!/usr/bin/env bash
# Start the display, the window manager, Chromium and the noVNC bridge.
set -euo pipefail

HOME_DIR="${HOME:-/home/browser}"
PORT="${AIFACTORY_BROWSER_PORT:-6080}"
WEB_ROOT="/opt/aifactory-browser/web"
RUNTIME_DIR="/tmp/aifactory-browser"
VNC_SOCKET="${RUNTIME_DIR}/vnc.sock"
PROFILE_DIR="${HOME_DIR}/.config/chromium"
NSS_DB="sql:${HOME_DIR}/.pki/nssdb"
CA_NICKNAME_PREFIX="AI Factory CA"
export DISPLAY=":1"

# A stopped container keeps its /tmp. Stale X locks and sockets from the
# previous run would stop Xvnc from claiming the display again.
rm -rf "${RUNTIME_DIR}" /tmp/.X1-lock /tmp/.X11-unix/X1
install -d -m 700 "${RUNTIME_DIR}"
mkdir -p "${HOME_DIR}/Downloads" "${PROFILE_DIR}"
cd "${HOME_DIR}"

# The profile lives on the persistent volume. A container that was killed
# leaves Chromium's singleton lock behind, pointing at a hostname that no
# longer exists, and the next start would refuse to open the profile.
rm -f "${PROFILE_DIR}"/Singleton{Lock,Socket,Cookie}

# Chromium on Linux trusts the roots in the user's NSS database on top of its
# built-in store. The Admin CA bundle (a TLS-inspecting corporate proxy, for
# example) is imported on every start, and certificates removed from the
# bundle are removed here too.
sync_ca_bundle() {
    install -d -m 700 "${HOME_DIR}/.pki/nssdb"
    if ! certutil -d "${NSS_DB}" -L >/dev/null 2>&1; then
        certutil -d "${NSS_DB}" -N --empty-password
    fi
    certutil -d "${NSS_DB}" -L 2>/dev/null \
        | sed -n "s/^\(${CA_NICKNAME_PREFIX} [0-9]*\) .*$/\1/p" \
        | while IFS= read -r nickname; do
            certutil -d "${NSS_DB}" -D -n "${nickname}" || true
        done
    [[ -n "${AIFACTORY_CA_BUNDLE_PEM:-}" ]] || return 0
    local split_dir="${RUNTIME_DIR}/ca"
    rm -rf "${split_dir}"
    install -d -m 700 "${split_dir}"
    printf '%s\n' "${AIFACTORY_CA_BUNDLE_PEM}" | awk -v dir="${split_dir}" '
        /-----BEGIN CERTIFICATE-----/ { count += 1; writing = 1 }
        writing { print > (dir "/" count ".pem") }
        /-----END CERTIFICATE-----/ { writing = 0; close(dir "/" count ".pem") }
    '
    local index=0 certificate
    for certificate in "${split_dir}"/*.pem; do
        [[ -e "${certificate}" ]] || continue
        index=$((index + 1))
        certutil -d "${NSS_DB}" -A -t "C,," \
            -n "${CA_NICKNAME_PREFIX} ${index}" -i "${certificate}" \
            || echo "aifactory-browser: CA certificate ${index} was not imported" >&2
    done
    echo "aifactory-browser: ${index} CA certificate(s) trusted"
}
sync_ca_bundle
unset AIFACTORY_CA_BUNDLE_PEM

# Clipboard direction between the user's machine and the workspace:
# both (default), in (paste into the workspace only), out (copy out only), none.
accept_clipboard=1
send_clipboard=1
case "${AIFACTORY_BROWSER_CLIPBOARD:-both}" in
    in) send_clipboard=0 ;;
    out) accept_clipboard=0 ;;
    none) accept_clipboard=0; send_clipboard=0 ;;
esac

children=()
chromium_pid=""
shutdown() {
    trap - TERM INT
    # Chromium first, with time to flush cookies and the open tabs to the
    # profile; Podman sends SIGKILL ten seconds after SIGTERM.
    if [[ -n "${chromium_pid}" ]]; then
        kill -TERM "${chromium_pid}" 2>/dev/null || true
        ( sleep 7; kill -KILL "${chromium_pid}" 2>/dev/null ) &
        wait "${chromium_pid}" 2>/dev/null || true
    fi
    echo "aifactory-browser: stopped"
    kill "${children[@]}" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# No TCP listener: the socket is the only way in, and websockify is the only
# process that uses it.
Xvnc "${DISPLAY}" \
    -geometry "${AIFACTORY_BROWSER_GEOMETRY:-1600x900}" \
    -depth 24 \
    -SecurityTypes None \
    -rfbunixpath "${VNC_SOCKET}" \
    -rfbunixmode 0600 \
    -rfbport -1 \
    -AlwaysShared \
    -AcceptCutText="${accept_clipboard}" \
    -SendCutText="${send_clipboard}" \
    -SendPrimary=0 \
    -SetPrimary=0 \
    -desktop "AI Factory" \
    -nolisten tcp &
children+=("$!")
xvnc_pid="$!"

for _ in $(seq 1 100); do
    [[ -S "${VNC_SOCKET}" ]] && break
    sleep 0.1
done
[[ -S "${VNC_SOCKET}" ]] || { echo "aifactory-browser: Xvnc did not start" >&2; exit 1; }

openbox &
children+=("$!")

websockify --web "${WEB_ROOT}" --heartbeat 30 \
    --unix-target "${VNC_SOCKET}" "0.0.0.0:${PORT}" &
children+=("$!")
websockify_pid="$!"

# The sites open on the very first start only; afterwards Chromium restores the
# previous session.
first_run_urls=()
if [[ ! -e "${PROFILE_DIR}/Local State" ]]; then
    # `custom_chrome_frame: false` drops Chromium's own minimize/maximize/close
    # buttons: the browser is the whole desktop, so a minimized window could
    # not be brought back. (--no-first-run skips /etc/chromium/master_preferences,
    # hence the seeded profile.)
    if [[ ! -e "${PROFILE_DIR}/Default/Preferences" ]]; then
        mkdir -p "${PROFILE_DIR}/Default"
        printf '%s\n' '{"browser":{"custom_chrome_frame":false}}' \
            > "${PROFILE_DIR}/Default/Preferences"
    fi
    read -r -a first_run_urls <<< "${AIFACTORY_BROWSER_START_URLS:-https://claude.ai/ https://chatgpt.com/}"
fi

# --no-sandbox: rootless Podman's default seccomp profile does not let Chromium
# create its sandbox namespaces. The container (non-root, no sudo, one per
# user) is the isolation boundary.
# --disable-dev-shm-usage: the container's /dev/shm is 64 MB.
# --password-store=basic: there is no keyring daemon to unlock.
# --log-level=3: without a D-Bus session Chromium logs an error every few
# seconds, which would bury everything else in the workspace log.
run_chromium() {
    chromium \
        --no-sandbox \
        --disable-dev-shm-usage \
        --disable-gpu \
        --no-first-run \
        --no-default-browser-check \
        --password-store=basic \
        --hide-crash-restore-bubble \
        --log-level=3 \
        --start-maximized \
        --window-position=0,0 \
        --user-data-dir="${PROFILE_DIR}" \
        "$@" &
    chromium_pid="$!"
}

run_chromium "${first_run_urls[@]}"
# Closing the last window ends Chromium; open it again rather than leaving the
# user with an empty screen. The container stops when the display or the
# bridge dies, and Podman's restart policy brings it back.
while kill -0 "${xvnc_pid}" 2>/dev/null && kill -0 "${websockify_pid}" 2>/dev/null; do
    if ! kill -0 "${chromium_pid}" 2>/dev/null; then
        wait "${chromium_pid}" 2>/dev/null || true
        sleep 1
        rm -f "${PROFILE_DIR}"/Singleton{Lock,Socket,Cookie}
        run_chromium
    fi
    sleep 1
done
echo "aifactory-browser: display or WebSocket bridge exited" >&2
kill "${children[@]}" "${chromium_pid}" 2>/dev/null || true
exit 1
