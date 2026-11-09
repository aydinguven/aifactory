#!/usr/bin/env bash
set -Eeuo pipefail

readonly WORKSPACE=/workspace
readonly ASSET_DIR=/release-assets
readonly RELEASE_VENV=/tmp/aifactory-release-venv
readonly CONTAINER_STORAGE_CONF=/tmp/aifactory-containers-storage.conf

export LANG=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1
export CONTAINERS_STORAGE_CONF="${CONTAINER_STORAGE_CONF}"

required_variables=(
  GITHUB_SHA
  AIFACTORY_VERSION
  SHORT_SHA
  PLATFORM_FILENAME
  ASSET_BASE_URL
  IMAGE_REGISTRY
  IMAGE_REPOSITORY
  QUAY_REGISTRY
  QUAY_REPOSITORY
  PUBLISH_QUAY
  SIGN_RELEASE
)
for variable in "${required_variables[@]}"; do
  [[ -n "${!variable:-}" ]] || {
    echo "Release build is missing ${variable}." >&2
    exit 1
  }
done

dnf install -y \
  createrepo_c \
  dnf-plugins-core \
  findutils \
  git \
  gnupg2 \
  gzip \
  pigz \
  podman \
  python3 \
  python3-pip \
  tar

write_storage_conf() {
  cat > "${CONTAINER_STORAGE_CONF}" <<EOF
[storage]
driver = "$1"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
EOF
}

# vfs copies the full root filesystem for every build step, which made image
# builds, pushes and teardown dominate the release. Native overlay needs a
# graphroot that is not itself on overlayfs, so the workflow bind-mounts
# /var/lib/containers from the runner disk. Fall back to vfs when that mount
# or kernel overlay support is missing.
install -d -m 0700 /var/lib/containers
storage_driver=vfs
if [[ "$(stat -f -c %T /var/lib/containers)" != "overlayfs" ]] && grep -qw overlay /proc/filesystems; then
  storage_driver=overlay
fi
write_storage_conf "${storage_driver}"
if [[ "${storage_driver}" == "overlay" ]] && ! podman info >/dev/null 2>&1; then
  echo "Podman overlay storage is unavailable; falling back to vfs." >&2
  rm -rf -- /var/lib/containers/storage
  storage_driver=vfs
  write_storage_conf "${storage_driver}"
fi
echo "Podman storage driver: ${storage_driver}"

dnf download --help >/dev/null
podman info >/dev/null

cd "${WORKSPACE}"
git config --global --add safe.directory "${WORKSPACE}"
git diff --exit-code
git diff --cached --exit-code

python3 -m venv "${RELEASE_VENV}"
"${RELEASE_VENV}/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"${RELEASE_VENV}/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
export PATH="${RELEASE_VENV}/bin:${PATH}"

# The test suite is gated by the CI workflow; the bundle verification below
# still imports the application and checks the packaged wheels.

signing_key=""
if [[ "${SIGN_RELEASE}" == "true" ]]; then
  [[ -n "${RELEASE_GPG_PRIVATE_KEY:-}" ]] || {
    echo "sign_release requires RELEASE_GPG_PRIVATE_KEY." >&2
    exit 1
  }

  printf '%s' "${RELEASE_GPG_PRIVATE_KEY}" | gpg --batch --import
  signing_key="${RELEASE_GPG_KEY_ID:-}"
  if [[ -z "${signing_key}" ]]; then
    signing_key="$(
      gpg --batch --with-colons --list-secret-keys |
        awk -F: '$1 == "fpr" { print $10; exit }'
    )"
  fi
  [[ -n "${signing_key}" ]] || {
    echo "No imported GPG signing key was found." >&2
    exit 1
  }

  gpg --batch --yes --output "${ASSET_DIR}/aifactory-release-keyring.gpg" --export "${signing_key}"
fi

image_logged_in=false
quay_logged_in=false
cleanup() {
  if [[ "${image_logged_in}" == "true" ]]; then
    podman logout "${IMAGE_REGISTRY}" >/dev/null 2>&1 || true
  fi
  if [[ "${quay_logged_in}" == "true" ]]; then
    podman logout "${QUAY_REGISTRY}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Independent steps below run side by side. Prefix every output line so the
# interleaved log stays readable, and fail when any background step failed.
export PYTHONUNBUFFERED=1
labeled() {
  local label="$1"
  shift
  "$@" > >(sed -u "s/^/[${label}] /") 2>&1
}
wait_all() {
  local pid status=0
  for pid in "$@"; do
    wait "${pid}" || status=1
  done
  return "${status}"
}

pull_postgresql() {
  if ! podman image exists localhost/aifactory-postgresql:16; then
    podman pull quay.io/sclorg/postgresql-16-c10s:latest
    podman tag quay.io/sclorg/postgresql-16-c10s:latest localhost/aifactory-postgresql:16
  fi
}

# Both runtime images share one base; pull it once before building them in
# parallel.
podman pull "${AIFACTORY_PYTHON_IMAGE:-registry.access.redhat.com/ubi10/python-312-minimal:latest}"
labeled controller bash deploy/container/build-controller-image.sh &
controller_build=$!
labeled worker bash deploy/container/build-worker-image.sh &
worker_build=$!
labeled postgresql pull_postgresql &
postgresql_pull=$!
wait_all "${controller_build}" "${worker_build}" "${postgresql_pull}"

[[ -n "${IMAGE_USERNAME:-}" && -n "${IMAGE_PASSWORD:-}" ]] || {
  echo "GHCR publishing requires IMAGE_USERNAME and IMAGE_PASSWORD." >&2
  exit 1
}

printf '%s' "${IMAGE_PASSWORD}" |
  podman login --username "${IMAGE_USERNAME}" --password-stdin "${IMAGE_REGISTRY}"
image_logged_in=true

if [[ "${PUBLISH_QUAY}" == "true" ]]; then
  [[ -n "${QUAY_USERNAME:-}" && -n "${QUAY_PASSWORD:-}" ]] || {
    echo "Quay mirroring requires QUAY_USERNAME and QUAY_PASSWORD." >&2
    exit 1
  }

  printf '%s' "${QUAY_PASSWORD}" |
    podman login --username "${QUAY_USERNAME}" --password-stdin "${QUAY_REGISTRY}"
  quay_logged_in=true
fi

push_runtime_images() {
  local registries=("${IMAGE_REGISTRY}/${IMAGE_REPOSITORY}")
  if [[ "${PUBLISH_QUAY}" == "true" ]]; then
    registries+=("${QUAY_REGISTRY}/${QUAY_REPOSITORY}")
  fi
  local repository role local_image remote_tag remote_image
  for repository in "${registries[@]}"; do
    for role in controller worker; do
      local_image="localhost/aifactory-${role}:${AIFACTORY_VERSION}"
      for remote_tag in "${role}-${AIFACTORY_VERSION}" "${role}-${AIFACTORY_VERSION}-${SHORT_SHA}"; do
        remote_image="${repository}:${remote_tag}"
        podman tag "${local_image}" "${remote_image}"
        podman push "${remote_image}"
      done
    done
  done
}

signing_arguments=()
if [[ -n "${signing_key}" ]]; then
  signing_arguments=(--signing-key "${signing_key}")
fi

build_arguments=(
  --output-dir "${ASSET_DIR}"
  --controller-source "${IMAGE_REGISTRY}/${IMAGE_REPOSITORY}:controller-${AIFACTORY_VERSION}-${SHORT_SHA}"
  --worker-source "${IMAGE_REGISTRY}/${IMAGE_REPOSITORY}:worker-${AIFACTORY_VERSION}-${SHORT_SHA}"
  --channel-output "${ASSET_DIR}/aifactory-update-channel.json"
  --channel-url "${ASSET_BASE_URL}/${PLATFORM_FILENAME}"
)
if [[ -n "${signing_key}" ]]; then
  build_arguments+=(--release-keyring "${ASSET_DIR}/aifactory-release-keyring.gpg")
fi

offline_keyring_arguments=()
if [[ -n "${signing_key}" ]]; then
  offline_keyring_arguments=(--release-keyring "${ASSET_DIR}/aifactory-release-keyring.gpg")
fi
# The two offline bundles each run dnf download, so they stay sequential with
# each other; they run in parallel with the platform bundle and the pushes.
build_offline_bundles() {
  local role
  for role in server worker; do
    python deploy/package_offline.py \
      --bundle-role "${role}" \
      --output-dir "${ASSET_DIR}" \
      --skip-image-build \
      "${offline_keyring_arguments[@]}"
  done
}

labeled push push_runtime_images &
image_push=$!
labeled platform python deploy/build_platform_update.py "${build_arguments[@]}" "${signing_arguments[@]}" &
platform_bundle=$!
labeled offline build_offline_bundles &
offline_bundles=$!
wait_all "${image_push}" "${platform_bundle}" "${offline_bundles}"

# Exercise the real updater path once on the finished platform bundle. The
# offline bundles were verified while staged, so only their compressed
# archives are integrity-checked here.
python - "${ASSET_DIR}/${PLATFORM_FILENAME}" "${signing_key}" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

from app.installer.platform import CommandRunner
from app.installer.release import prepare_release
from app.platform_release import load_platform_release

bundle = Path(sys.argv[1])
signing_key = sys.argv[2]
keyring = Path("/release-assets/aifactory-release-keyring.gpg")
with prepare_release(
    bundle,
    runner=CommandRunner(),
    keyring=keyring,
    require_signature=bool(signing_key),
) as prepared:
    release = load_platform_release(prepared.root)
    wheels_dir = prepared.root / "offline" / "wheels"
    wheels = list(wheels_dir.glob("*.whl"))
    assert release.version == os.environ["AIFACTORY_VERSION"]
    assert release.source_commit == os.environ["GITHUB_SHA"]
    assert wheels, "Platform update is missing native-worker Python wheels"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--no-index",
            "--find-links",
            str(wheels_dir),
            "-r",
            str(prepared.root / "requirements.txt"),
        ],
        check=True,
    )
PY

platform_size="$(stat --format=%s "${ASSET_DIR}/${PLATFORM_FILENAME}")"
if (( platform_size >= 2147483648 )); then
  echo "Platform update exceeds GitHub's 2 GiB release-asset limit." >&2
  exit 1
fi

for role in server worker; do
  if [[ "${role}" == "server" ]]; then
    pattern='aifactory-offline-v*.tar.gz'
  else
    pattern='aifactory-worker-offline-v*.tar.gz'
  fi
  bundle="$(find "${ASSET_DIR}" -maxdepth 1 -type f -name "${pattern}" -print -quit)"
  [[ -n "${bundle}" ]] || {
    echo "Missing ${role} offline bundle." >&2
    exit 1
  }
  if (( "$(stat --format=%s "${bundle}")" >= 2147483648 )); then
    echo "The ${role} offline bundle exceeds GitHub's 2 GiB release-asset limit." >&2
    exit 1
  fi
  pigz --test "${bundle}"
done

find "${ASSET_DIR}" -maxdepth 1 -type f -print0 |
  sort -z |
  xargs -0 sha256sum
chmod -R a+rX "${ASSET_DIR}"
