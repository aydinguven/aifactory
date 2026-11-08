#!/usr/bin/env bash
set -e

echo "====================================================="
echo "Building AI Factory Container Images with Podman"
echo "====================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The Kilo Code VSIX URL and SHA-256 are pinned in the Containerfile.
echo "1/4 Building devcloud-vscode-kilo..."
podman build -t localhost/devcloud-vscode-kilo:latest "${SCRIPT_DIR}/vscode-kilo"

# Large (CUDA toolkit, cuDNN, NCCL); only GPU workers use it.
echo "2/4 Building devcloud-vscode-cuda..."
podman build -t localhost/devcloud-vscode-cuda:latest "${SCRIPT_DIR}/vscode-cuda"

echo "3/4 Building devcloud-jupyter-python..."
podman build -t localhost/devcloud-jupyter-python:latest "${SCRIPT_DIR}/jupyter-python"

echo "4/4 Building devcloud-terminal-rocky..."
podman build -t localhost/devcloud-terminal-rocky:latest "${SCRIPT_DIR}/terminal-rocky"

echo "====================================================="
echo "All AI Factory workspace images built successfully!"
echo "====================================================="
podman images | grep aifactory
