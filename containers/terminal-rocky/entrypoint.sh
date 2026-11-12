#!/usr/bin/env bash
# Seed the persistent home directory, then hand the session to ttyd.
set -euo pipefail

HOME_DIR="${HOME:-/home/devuser}"
SKELETON="/opt/aifactory-home-skel.tar"
PORT="${AIFACTORY_TERMINAL_PORT:-7681}"
SENTINEL="${HOME_DIR}/.aifactory-home-seeded"

# AI Factory mounts persistent storage over the home directory, so on the very
# first start the image's dotfiles are hidden behind an empty volume. Restore
# them once and leave the marker behind; later starts keep whatever the user
# has since changed.
# Homes seeded by images before 3.49.1 carry the devcloud marker.
if [[ -e "${HOME_DIR}/.devcloud-home-seeded" && ! -e "${SENTINEL}" ]]; then
    mv "${HOME_DIR}/.devcloud-home-seeded" "${SENTINEL}"
fi
if [[ ! -e "${SENTINEL}" ]]; then
    if [[ -r "${SKELETON}" ]]; then
        tar -xf "${SKELETON}" -C "${HOME_DIR}"
    fi
    mkdir -p "${HOME_DIR}/projects"
    date --iso-8601=seconds > "${SENTINEL}"
fi

cd "${HOME_DIR}"

# A tmux session makes the shell survive a dropped WebSocket: reconnecting
# reattaches instead of discarding running work. `new -A` attaches to the
# existing session when there is one.
exec ttyd \
    --port "${PORT}" \
    --writable \
    --cwd "${HOME_DIR}" \
    --client-option disableLeaveAlert=true \
    --client-option rendererType=canvas \
    --client-option titleFixed="AI Factory Terminal" \
    tmux new-session -A -s aifactory
