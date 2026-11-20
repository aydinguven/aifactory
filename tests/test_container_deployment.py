import json
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
        ROOT / "containers" / "aifactory-controller" / "Containerfile"
    ).read_text(encoding="utf-8")

    assert "python -m app.migrations upgrade" in entrypoint
    assert "--workers 1" in entrypoint
    assert "USER 10001:10001" in containerfile


def test_controller_quadlet_is_offline_and_loopback_only():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "aifactory-controller.container"
    ).read_text(encoding="utf-8")

    assert "Pull=never" in quadlet
    assert "PublishPort=127.0.0.1:8000:8000" in quadlet
    assert "ReadOnly=true" in quadlet
    assert "EnvironmentFile=/etc/aifactory/controller.env" in quadlet


def test_postgresql_is_not_published_to_the_host():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "aifactory-postgresql.container"
    ).read_text(encoding="utf-8")

    assert "Network=aifactory.network" in quadlet
    assert "PublishPort=" not in quadlet
    assert "Pull=never" in quadlet


def test_worker_image_and_quadlet_use_rootful_host_podman_socket():
    containerfile = (
        ROOT / "containers" / "aifactory-worker" / "Containerfile"
    ).read_text(encoding="utf-8")
    entrypoint = (
        ROOT / "deploy" / "container" / "worker-entrypoint.sh"
    ).read_text(encoding="utf-8")
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "aifactory-worker.container"
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
    assert "tmux new-session -A -s aifactory" in entrypoint
    # No upstream credential: AI Factory is the only authenticator.
    assert "--credential" not in entrypoint
    # The home volume is seeded on first start, since the mount hides the image
    # skeleton.
    assert "/opt/aifactory-home-skel.tar" in entrypoint


def test_browser_workspace_image_is_pinned_and_proxy_compatible():
    """The browser image must stay reproducible, unprivileged and proxy friendly."""
    context = ROOT / "containers" / "browser-chromium"
    containerfile = (context / "Containerfile").read_text(encoding="utf-8")

    assert (
        "ARG BASE_IMAGE=docker.io/library/debian:trixie-slim@"
        "sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
    ) in containerfile
    # The proxy publishes template.default_port; they have to agree.
    assert "EXPOSE 6080" in containerfile
    assert "USER browser" in containerfile
    instructions = "\n".join(
        line
        for line in containerfile.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "sudo" not in instructions
    # Policies must be root-owned files baked into the image.
    assert "/etc/chromium/policies/managed/aifactory.json" in containerfile

    entrypoint = (context / "entrypoint.sh").read_text(encoding="utf-8")
    # No VNC TCP listener and no VNC password: AI Factory is the only
    # authenticator and websockify is the only way to the display.
    assert "-rfbunixpath" in entrypoint
    assert "-rfbport -1" in entrypoint
    assert "--unix-target" in entrypoint
    # server.py owns the public port; websockify is reachable on loopback only.
    assert '"127.0.0.1:${WEBSOCKIFY_PORT}"' in entrypoint
    assert "0.0.0.0:${PORT}" not in entrypoint
    assert "python3 -I /opt/aifactory-browser/server.py" in entrypoint
    # Panel paste: xclip sets the remote clipboard, and Chromium never sees
    # the token that lets the controller do so.
    assert "xclip" in containerfile
    assert "unset AIFACTORY_BROWSER_PASTE_TOKEN" in entrypoint
    assert "COPY server.py /opt/aifactory-browser/server.py" in containerfile
    # A killed container must not lock the persistent profile or the display.
    assert 'rm -f "${PROFILE_DIR}"/Singleton{Lock,Socket,Cookie}' in entrypoint
    assert "/tmp/.X1-lock" in entrypoint
    # The Admin CA bundle reaches Chromium through the user's NSS database.
    assert "AIFACTORY_CA_BUNDLE_PEM" in entrypoint
    assert "certutil" in entrypoint

    # The client is served under /proxy/<workspace-id>/, so it may not use
    # root-relative URLs.
    client = (context / "index.html").read_text(encoding="utf-8")
    assert 'from "./core/rfb.js"' in client
    assert 'src="/' not in client and 'href="/' not in client
    assert "location.pathname" in client
    # noVNC reuses its send buffer; each message must be copied before send()
    # or a WebSocket wrapper that sends later corrupts the VNC stream.
    assert "data.slice()" in client
    assert "new RFB(screen, openSocket()" in client

    managed = json.loads(
        (context / "policies" / "managed.json").read_text(encoding="utf-8")
    )
    assert managed["ExtensionInstallBlocklist"] == ["*"]
    assert managed["PasswordManagerEnabled"] is False
    assert managed["SyncDisabled"] is True
    # The panel lists the folder Chromium downloads into.
    assert managed["DownloadDirectory"] == "/home/browser/Downloads"
    bookmarks = {item.get("url") for item in managed["ManagedBookmarks"]}
    assert {"https://claude.ai/", "https://chatgpt.com/"} <= bookmarks
    assert "https://merkezasistan.tcmb.gov.tr/" in bookmarks

    # Chromium opens the bundled start page, which links the three assistants.
    start_url = "file:///opt/aifactory-browser/start/index.html"
    recommended = json.loads(
        (context / "policies" / "recommended.json").read_text(encoding="utf-8")
    )
    assert recommended["RestoreOnStartup"] == 4
    assert recommended["RestoreOnStartupURLs"] == [start_url]
    assert recommended["HomepageLocation"] == start_url
    assert "COPY start/index.html /opt/aifactory-browser/start/index.html" in containerfile
    start_page = (context / "start" / "index.html").read_text(encoding="utf-8")
    for link in (
        "https://merkezasistan.tcmb.gov.tr/",
        "https://claude.ai/",
        "https://chatgpt.com/",
    ):
        assert f'href="{link}"' in start_page
    # Self-contained: the page must render without any network request.
    assert "<script" not in start_page and "<link" not in start_page
    assert "url(" not in start_page and "<img" not in start_page


def test_browser_template_matches_its_image_contract():
    from app.orchestrator.flavors import FLAVORS
    from app.orchestrator.templates import flavor_mismatch, get_template

    template = get_template("browser-chromium")
    assert template is not None
    assert template.ide_type == "browser"
    assert template.default_port == 6080
    assert template.container_workdir == "/home/browser"
    assert template.image_tag == "localhost/aifactory-browser-chromium:latest"
    assert template.mount_workspace is True
    assert template.health_path == "/"
    assert template.startup_command == []
    # Older workers would not pass the CA bundle to the container.
    assert template.min_agent_version == "3.55.0"
    # Text copied in the workspace can leave it, but nothing is pasted in.
    assert template.env_vars["AIFACTORY_BROWSER_CLIPBOARD"] == "out"
    assert template.env_vars["AIFACTORY_BROWSER_FILES"] == "both"
    entrypoint = (ROOT / "containers" / "browser-chromium" / "entrypoint.sh").read_text(encoding="utf-8")
    assert '"${AIFACTORY_BROWSER_CLIPBOARD:-out}"' in entrypoint
    # Chromium is refused on flavors it cannot run in.
    assert "2 GB" in flavor_mismatch(template, FLAVORS["t1.micro"])
    assert flavor_mismatch(template, FLAVORS["t1.small"]) == ""
    assert flavor_mismatch(get_template("terminal-rocky"), FLAVORS["t1.nano"]) == ""


def test_browser_workspace_receives_the_ca_bundle_without_ide_wiring():
    import inspect as inspect_module

    from app.orchestrator.podman_service import PodmanService
    from app.orchestrator.workspace_provisioning import CA_BUNDLE_IDE_TYPES

    assert "browser" in CA_BUNDLE_IDE_TYPES
    source = inspect_module.getsource(PodmanService.create_workspace_container)
    assert "if is_vscode or is_jupyter or is_browser" in source
    # The image ENTRYPOINT stays in charge: no command branch for browsers.
    assert "elif is_browser" not in source


def test_terminal_template_matches_its_image_contract():
    from app.orchestrator.templates import get_template

    template = get_template("terminal-rocky")
    assert template is not None
    assert template.ide_type == "terminal"
    assert template.default_port == 7681
    assert template.container_workdir == "/home/devuser"
    assert template.image_tag == "localhost/aifactory-terminal-rocky:latest"
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


def test_runtime_images_share_one_pinned_ubi_python_base():
    """Controller and worker builds use one pinned UBI base, never :latest.

    UBI images signed after Red Hat's Rekor change (~2026-11-14) fail Podman's
    signature policy on EL10, so the base stays pinned to an earlier build.
    """
    import re

    files = [
        "containers/aifactory-controller/Containerfile",
        "containers/aifactory-worker/Containerfile",
        "deploy/ci/build-release-assets.sh",
        "deploy/container/build-controller-image.sh",
        "deploy/container/build-worker-image.sh",
    ]
    references = set()
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        found = re.findall(r"registry\.access\.redhat\.com/ubi10/python-312-minimal:[\w.-]+", text)
        assert len(found) == 1, name
        references.update(found)
    assert len(references) == 1, references
    assert not references.pop().endswith(":latest")
