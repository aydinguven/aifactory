from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_container_health_endpoints(client):
    health = await client.get("/healthz")
    ready = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_controller_image_runs_migrations_before_single_uvicorn_process():
    entrypoint = (
        ROOT / "deploy" / "container" / "controller-entrypoint.sh"
    ).read_text(encoding="utf-8")
    containerfile = (
        ROOT / "containers" / "devcloud-controller" / "Containerfile"
    ).read_text(encoding="utf-8")

    assert "python -m app.migrations upgrade" in entrypoint
    assert "--workers 1" in entrypoint
    assert "USER 10001:10001" in containerfile


def test_controller_quadlet_is_offline_and_loopback_only():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-controller.container"
    ).read_text(encoding="utf-8")

    assert "Pull=never" in quadlet
    assert "PublishPort=127.0.0.1:8000:8000" in quadlet
    assert "ReadOnly=true" in quadlet
    assert "EnvironmentFile=/etc/devcloud/controller.env" in quadlet


def test_postgresql_is_not_published_to_the_host():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-postgresql.container"
    ).read_text(encoding="utf-8")

    assert "Network=devcloud.network" in quadlet
    assert "PublishPort=" not in quadlet
    assert "Pull=never" in quadlet


def test_worker_image_and_quadlet_use_rootful_host_podman_socket():
    containerfile = (
        ROOT / "containers" / "devcloud-worker" / "Containerfile"
    ).read_text(encoding="utf-8")
    entrypoint = (
        ROOT / "deploy" / "container" / "worker-entrypoint.sh"
    ).read_text(encoding="utf-8")
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-worker.container"
    ).read_text(encoding="utf-8")

    assert "microdnf install -y podman" in containerfile
    assert "USER 0" in containerfile
    assert "podman info" in entrypoint
    assert "Network=host" in quadlet
    assert "Requires=podman.socket" in quadlet
    assert "/run/podman/podman.sock:/run/podman/podman.sock" in quadlet
    assert "SecurityLabelDisable=true" in quadlet
    assert "Privileged=true" not in quadlet


def test_builtin_vscode_image_installs_pinned_kilo_code():
    code_server_base = (
        "FROM docker.io/codercom/code-server:4.137.0@"
        "sha256:57ac684d44deb6fa94317b3e8f3e128dd7fb897fffd95b4efcd16f56ce607971"
    )
    containerfile = (
        ROOT / "containers" / "vscode-kilo" / "Containerfile"
    ).read_text(encoding="utf-8")
    assert code_server_base in containerfile
    assert "ARG KILO_CODE_VERSION=7.8.17" in containerfile
    assert "kilocode.kilo-code-${KILO_CODE_VERSION}@linux-x64.vsix" in containerfile
    assert (
        'echo "${KILO_CODE_VSIX_SHA256}  /tmp/kilocode.vsix" | sha256sum -c -'
    ) in containerfile
    assert "code-server --install-extension /tmp/kilocode.vsix" in containerfile
    assert 'grep -Fx "kilocode.kilo-code@${KILO_CODE_VERSION}"' in containerfile
    assert "KILO_LANCEDB_PATH=/opt/kilo/lancedb/" in containerfile
    assert '"extensions.autoCheckUpdates":false' in containerfile
    assert '"extensions.autoUpdate":false' in containerfile
    assert '"chat.disableAIFeatures":true' in containerfile
    assert "code-server:latest" not in containerfile
    for retired in ("vscode-empty", "vscode-python", "vscode-react", "vscode-java"):
        assert not (ROOT / "containers" / retired).exists()




def test_terminal_workspace_image_is_pinned_and_proxy_compatible():
    """The terminal image must stay reproducible and reachable via the proxy."""
    containerfile = (
        ROOT / "containers" / "terminal-rocky" / "Containerfile"
    ).read_text(encoding="utf-8")

    # Digest-pinned Rocky 10 base, matching the release container.
    assert (
        "FROM docker.io/rockylinux/rockylinux:10.2@"
        "sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834"
    ) in containerfile
    # Checksum-locked ttyd, mirroring the Cline VSIX pattern.
    assert "ARG TTYD_VERSION=1.7.7" in containerfile
    assert (
        "ARG TTYD_SHA256="
        "8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55"
    ) in containerfile
    assert 'echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c -' in containerfile
    # The proxy publishes template.default_port; they have to agree.
    assert "EXPOSE 7681" in containerfile
    # Workspaces are unprivileged and carry no privilege-escalation tooling.
    assert "USER devuser" in containerfile
    instructions = "\n".join(
        line
        for line in containerfile.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "sudo" not in instructions

    entrypoint = (
        ROOT / "containers" / "terminal-rocky" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    # ttyd must be writable, or the terminal is read-only.
    assert "--writable" in entrypoint
    # tmux keeps a dropped WebSocket from killing the user's work.
    assert "tmux new-session -A -s devcloud" in entrypoint
    # No upstream credential: AI Factory is the only authenticator.
    assert "--credential" not in entrypoint
    # The home volume is seeded on first start, since the mount hides the image
    # skeleton.
    assert "/opt/devcloud-home-skel.tar" in entrypoint


def test_terminal_template_matches_its_image_contract():
    from app.orchestrator.templates import get_template

    template = get_template("terminal-rocky")
    assert template is not None
    assert template.ide_type == "terminal"
    assert template.default_port == 7681
    assert template.container_workdir == "/home/devuser"
    assert template.image_tag == "localhost/devcloud-terminal-rocky:latest"
    # Persistent home requires the workspace volume to be mounted.
    assert template.mount_workspace is True
    # ttyd answers 200 on /, so readiness uses an HTTP probe.
    assert template.health_path == "/"
    # An ide_type outside the vscode/jupyter/service branches means
    # podman_service leaves the image ENTRYPOINT in charge.
    assert template.startup_command == []


def test_terminal_workspace_uses_the_image_entrypoint():
    """No entrypoint override or injected IDE environment for terminals."""
    import inspect as inspect_module

    from app.orchestrator.podman_service import PodmanService

    source = inspect_module.getsource(PodmanService.create_workspace_container)
    # The three branches that rewrite the command are all IDE specific.
    assert 'is_vscode = template.ide_type == "vscode"' in source
    assert 'is_jupyter = template.ide_type == "jupyter"' in source
    assert 'is_service = template.ide_type == "service"' in source
    assert 'ide_type == "terminal"' not in source
