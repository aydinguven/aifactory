#!/usr/bin/env bash
# Build an AI Factory air-gap bundle using the canonical cross-platform packager.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "${PROJECT_DIR}/deploy/package_offline.py" "$@"
