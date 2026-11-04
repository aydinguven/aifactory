import asyncio
import json

import pytest

from app.config import settings
from app.cline import managed_cline_files, openai_compatible_base_url
from app.jupyter_ai import default_model_catalog
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template
from app.orchestrator.podman_service import PodmanService, _redacted_command
from app.mlflow_workspace import validate_mlflow_environment
from app.cline import managed_vscode_settings


def test_podman_command_redacts_workspace_and_mlflow_secrets():
    rendered = _redacted_command(
        [
            "podman",
            "run",
            "PASSWORD=workspace-secret",
            "MLFLOW_TRACKING_TOKEN=user-token",
            "MLFLOW_TRACKING_URI=https://mlflow.internal",
        ]
    )

    assert "workspace-secret" not in rendered
    assert "user-token" not in rendered
    assert "MLFLOW_TRACKING_URI=https://mlflow.internal" in rendered


@pytest.mark.asyncio
async def test_run_cmd_waits_for_cli_process_without_communicate(monkeypatch):
    """Detached container descendants must not keep command capture open."""
    captured = {}

    class FakeProcess:
        returncode = 0

        async def wait(self):
            return self.returncode

        async def communicate(self):
            raise AssertionError("run_cmd must not wait for pipe EOF via communicate()")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.update(kwargs)
        kwargs["stdout"].write(b"container-id\n")
        kwargs["stderr"].write(b"")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    svc = PodmanService(podman_bin="podman")

    code, stdout, stderr = await svc.run_cmd("run", "-d", "example")

    assert captured["stdout"] != asyncio.subprocess.PIPE
    assert captured["stderr"] != asyncio.subprocess.PIPE
    assert (code, stdout, stderr) == (0, "container-id", "")

@pytest.mark.asyncio
async def test_podman_service_mock_lifecycle():
    """Test Podman service creation, execution and state manipulation in mock mode."""
    svc = PodmanService()
    svc._mock_mode = True

    container_name = "devcloud-1-test-ws-1234"
    # 1. Create container
    cid, storage_path = await svc.create_workspace_container(
        workspace_id="test-ws-12345",
        user_id=1,
        container_name=container_name,
        template_id="vscode-java",
        flavor_id="t1.mini",
        host_port=10105,
        workspace_token="testtoken123",
        mlflow_environment={
            "MLFLOW_TRACKING_URI": "https://managed-mlflow.internal",
            "MLFLOW_TRACKING_TOKEN": "user-token",
        },
    )
    assert cid.startswith("mock-cid-")
    assert "test-ws-12345" in storage_path
    assert await svc.container_exists(container_name) is True
    assert svc._mock_containers[container_name]["mlflow_environment"] == {
        "MLFLOW_TRACKING_URI": "https://managed-mlflow.internal",
        "MLFLOW_TRACKING_TOKEN": "user-token",
    }
    
    # 2. Check status
    status = await svc.get_container_status(container_name)
    assert status == "running"

    # 3. Stop
    assert await svc.stop_container(container_name) is True
    assert await svc.get_container_status(container_name) == "stopped"

    # 4. Start
    assert await svc.start_container(container_name) is True
    assert await svc.get_container_status(container_name) == "running"

    # 5. Retrieve logs
    logs = await svc.get_logs(container_name)
    assert "2.0 CPU ve 2 GB RAM ayrıldı" in logs

    # 6. Delete
    assert await svc.delete_container(container_name) is True
    assert await svc.container_exists(container_name) is False


def test_mlflow_workspace_environment_rejects_arbitrary_keys():
    with pytest.raises(ValueError, match="Desteklenmeyen"):
        validate_mlflow_environment({"LD_PRELOAD": "/tmp/injected.so"})


def test_managed_cline_files_cover_legacy_and_sdk_storage():
    files = managed_cline_files(
        "https://llm-gateway.internal/",
        "shared-api-key",
        "local-coder",
    )

    assert openai_compatible_base_url("https://llm-gateway.internal") == (
        "https://llm-gateway.internal/v1"
    )
    assert openai_compatible_base_url("https://llm-gateway.internal/v1") == (
        "https://llm-gateway.internal/v1"
    )
    global_state = json.loads(files["globalState.json"])
    secrets = json.loads(files["secrets.json"])
    providers = json.loads(files["settings/providers.json"])
    assert global_state["planModeApiProvider"] == "openai"
    assert global_state["actModeApiProvider"] == "openai"
    assert global_state["openAiBaseUrl"] == "https://llm-gateway.internal/v1"
    assert global_state["planModeOpenAiModelId"] == "local-coder"
    assert global_state["actModeOpenAiModelId"] == "local-coder"
    assert secrets == {"openAiApiKey": "shared-api-key"}
    assert providers["lastUsedProvider"] == "openai-compatible"
    assert providers["providers"]["openai-compatible"]["settings"] == {
        "provider": "openai-compatible",
        "apiKey": "shared-api-key",
        "model": "local-coder",
        "baseUrl": "https://llm-gateway.internal/v1",
    }


def test_managed_vscode_settings_disable_native_chat_and_lock_cline():
    user_settings = json.loads(managed_vscode_settings())
    assert user_settings["chat.disableAIFeatures"] is True
    assert user_settings["cline.rollout.bundleOverride"] == "legacy"
    assert user_settings["extensions.autoUpdate"] is False
    assert "chat.defaultModel" not in user_settings


@pytest.mark.asyncio
async def test_vscode_launch_uses_admin_managed_cline_profile(monkeypatch):
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        return (0, "container-id", "") if args[0] == "run" else (0, "", "")

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace"
    )
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(
        settings, "JUPYTER_AI_GATEWAY_URL", "https://llm-gateway.internal"
    )
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL", "local-coder")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_TOKEN", "shared-api-key")
    monkeypatch.setattr(settings, "JUPYTER_AI_CLINE_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "JUPYTER_AI_MODEL_CATALOG_JSON",
        json.dumps(
            [
                {
                    "model_id": "local-coder",
                    "name": "Local Coder",
                    "description": "On-Prem",
                }
            ]
        ),
    )

    await svc.create_workspace_container(
        workspace_id="12345678-1234-1234-1234-123456789abc",
        user_id=1,
        container_name="devcloud-1-12345678",
        template_id="vscode-python",
        flavor_id="t1.micro",
        host_port=10100,
        workspace_token="secret-workspace-token",
        mlflow_environment={
            "MLFLOW_TRACKING_URI": "https://managed-mlflow.internal",
            "MLFLOW_TRACKING_TOKEN": "personal-mlflow-token",
        },
    )

    run_command = next(args for args in commands if args[0] == "run")
    assert "/workspace:/home/coder/project:Z,U" in run_command
    assert run_command[run_command.index("--entrypoint") + 1] == "/bin/bash"
    assert "CLINE_DATA_DIR=/home/coder/.cline/data" in run_command
    assert "MLFLOW_TRACKING_URI=https://managed-mlflow.internal" in run_command
    assert "MLFLOW_TRACKING_TOKEN=personal-mlflow-token" in run_command
    assert "DEVCLOUD_WORKSPACE_ID=12345678-1234-1234-1234-123456789abc" in run_command
    assert "DEVCLOUD_USER_ID=1" in run_command
    assert "CLINE_DIR=/home/coder/.cline" in run_command
    assert (
        "VSCODE_USER_DIR=/home/coder/.local/share/code-server/User"
        in run_command
    )
    global_state = json.loads(
        next(
            value.removeprefix("DEVCLOUD_CLINE_GLOBAL_STATE_JSON=")
            for value in run_command
            if value.startswith("DEVCLOUD_CLINE_GLOBAL_STATE_JSON=")
        )
    )
    secrets = json.loads(
        next(
            value.removeprefix("DEVCLOUD_CLINE_SECRETS_JSON=")
            for value in run_command
            if value.startswith("DEVCLOUD_CLINE_SECRETS_JSON=")
        )
    )
    providers = json.loads(
        next(
            value.removeprefix("DEVCLOUD_CLINE_PROVIDERS_JSON=")
            for value in run_command
            if value.startswith("DEVCLOUD_CLINE_PROVIDERS_JSON=")
        )
    )
    assert global_state["openAiBaseUrl"] == "https://llm-gateway.internal/v1"
    assert global_state["actModeOpenAiModelId"] == "local-coder"
    assert secrets["openAiApiKey"] == "shared-api-key"
    assert providers["lastUsedProvider"] == "openai-compatible"
    assert not any(
        value.startswith("DEVCLOUD_VSCODE_CHAT_MODELS_JSON=")
        for value in run_command
    )

    vscode_settings = json.loads(
        next(
            value.removeprefix("DEVCLOUD_VSCODE_SETTINGS_JSON=")
            for value in run_command
            if value.startswith("DEVCLOUD_VSCODE_SETTINGS_JSON=")
        )
    )
    assert vscode_settings["chat.disableAIFeatures"] is True
    assert vscode_settings["cline.rollout.bundleOverride"] == "legacy"
    image_index = run_command.index("localhost/devcloud-vscode-python:latest")
    startup_command = run_command[image_index + 1:]
    assert startup_command[0] == "-lc"
    assert "$CLINE_DATA_DIR/settings/providers.json" in startup_command[1]
    assert "chatLanguageModels.json" not in startup_command[1]
    assert "$VSCODE_USER_DIR/settings.json" in startup_command[1]
    assert "--disable-extension saoudrizwan.claude-dev" not in startup_command[1]
    assert "chmod 600" in startup_command[1]
    assert "exec /usr/bin/entrypoint.sh" in startup_command[1]
    assert "--bind-addr 0.0.0.0:8080" in startup_command[1]


@pytest.mark.asyncio
async def test_vscode_launch_ignores_legacy_disabled_cline_toggle(monkeypatch):
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        return (0, "container-id", "") if args[0] == "run" else (0, "", "")

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda *_args: "/workspace"
    )
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(
        settings, "JUPYTER_AI_GATEWAY_URL", "https://llm-gateway.internal"
    )
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL", "local-coder")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_TOKEN", "shared-api-key")
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL_CATALOG_JSON", "[]")
    monkeypatch.setattr(settings, "JUPYTER_AI_CLINE_ENABLED", False)

    await svc.create_workspace_container(
        workspace_id="12345678-1234-1234-1234-123456789abc",
        user_id=1,
        container_name="devcloud-1-12345678",
        template_id="vscode-python",
        flavor_id="t1.micro",
        host_port=10100,
        workspace_token="secret-workspace-token",
    )

    run_command = next(args for args in commands if args[0] == "run")
    assert any(
        value.startswith("DEVCLOUD_CLINE_") for value in run_command
    )
    assert not any(
        value.startswith("DEVCLOUD_VSCODE_CHAT_MODELS_JSON=")
        for value in run_command
    )
    image_index = run_command.index("localhost/devcloud-vscode-python:latest")
    startup_command = run_command[image_index + 1:]
    assert "chatLanguageModels.json" not in startup_command[1]
    assert "--disable-extension" not in startup_command[1]
    assert "--uninstall-extension saoudrizwan.claude-dev" not in startup_command[1]
    assert "disabled-extensions" not in startup_command[1]
    assert "exec /usr/bin/entrypoint.sh" in startup_command[1]


@pytest.mark.asyncio
async def test_jupyter_launch_installs_gateway_ca_bundle(monkeypatch, tmp_path):
    """The bundle is trusted next to the system roots before Jupyter starts."""
    import shutil
    import ssl
    import subprocess

    from app.orchestrator.podman_service import (
        WORKSPACE_CA_DIR,
        WORKSPACE_CA_ENVIRONMENT,
        WORKSPACE_CA_SETUP,
    )
    from tests.test_jupyter_ai_settings import _self_signed_pem

    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        return (0, "container-id", "") if args[0] == "run" else (0, "", "")

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace"
    )
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    bundle = _self_signed_pem("LiteLLM Gateway LAB CA", ca=True)

    await svc.create_workspace_container(
        workspace_id="12345678-1234-1234-1234-123456789abc",
        user_id=1,
        container_name="devcloud-1-12345678",
        template_id="jupyter-python",
        flavor_id="t1.micro",
        host_port=10100,
        workspace_token="secret-workspace-token",
        ai_ca_bundle=bundle,
    )

    run_command = next(args for args in commands if args[0] == "run")
    assert f"DEVCLOUD_CA_BUNDLE_PEM={bundle}" in run_command
    for key, value in WORKSPACE_CA_ENVIRONMENT.items():
        assert f"{key}={value}" in run_command
    image_index = run_command.index("localhost/devcloud-jupyter-python:latest")
    assert run_command[image_index + 3].startswith(WORKSPACE_CA_SETUP + " && ")

    if not shutil.which("bash"):
        pytest.skip("bash is required to run the container setup command")
    setup = WORKSPACE_CA_SETUP.replace(WORKSPACE_CA_DIR, str(tmp_path / "ca"))
    subprocess.run(
        ["bash", "-c", setup],
        env={"PATH": "/usr/bin:/bin", "DEVCLOUD_CA_BUNDLE_PEM": bundle},
        check=True,
    )
    extra = (tmp_path / "ca" / "extra-ca.pem").read_text()
    combined = (tmp_path / "ca" / "ca-bundle.pem").read_text()
    assert extra.strip() == bundle.strip()
    assert combined.rstrip().endswith(bundle.strip())
    context = ssl.create_default_context(cafile=str(tmp_path / "ca" / "ca-bundle.pem"))
    subjects = {
        value
        for certificate in context.get_ca_certs()
        for field in certificate["subject"]
        for key, value in field
        if key == "commonName"
    }
    assert "LiteLLM Gateway LAB CA" in subjects


@pytest.mark.asyncio
async def test_jupyter_launch_uses_secure_workspace_base_url(monkeypatch):
    """Jupyter must remain token-protected and mounted below its proxy prefix."""
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        if args[0] == "run":
            return 0, "container-id", ""
        return 0, "", ""

    async def fake_ensure_image_exists(*args, **kwargs):
        return True

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_open_connection(*args, **kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace"
    )
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", fake_ensure_image_exists)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(
        settings, "JUPYTER_AI_GATEWAY_URL", "https://llm-gateway.internal"
    )
    monkeypatch.setattr(settings, "JUPYTER_AI_MODEL", "local-coder")
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_TOKEN", "shared-ai-token")
    catalog = default_model_catalog()
    catalog.insert(
        0,
        {"model_id": "local-coder", "name": "Local Coder", "description": "On-Prem"},
    )
    monkeypatch.setattr(
        settings, "JUPYTER_AI_MODEL_CATALOG_JSON", json.dumps(catalog)
    )
    monkeypatch.setattr(settings, "JUPYTER_AI_GATEWAY_MODEL_DISCOVERY", True)

    workspace_id = "12345678-1234-1234-1234-123456789abc"
    await svc.create_workspace_container(
        workspace_id=workspace_id,
        user_id=1,
        container_name="devcloud-1-12345678",
        template_id="jupyter-python",
        flavor_id="t1.micro",
        host_port=10100,
        workspace_token="secret-workspace-token",
    )

    run_command = next(args for args in commands if args[0] == "run")
    image_index = run_command.index("localhost/devcloud-jupyter-python:latest")
    startup_command = run_command[image_index + 1:]

    assert "/workspace:/home/jovyan/work:Z,U" in run_command
    assert startup_command[:2] == ("bash", "-lc")
    assert "exec start-notebook.py \"$@\"" in startup_command[2]
    assert startup_command[3] == "devcloud-jupyter"
    assert f"--ServerApp.base_url=/proxy/{workspace_id}/" in startup_command
    assert "--ServerApp.default_url=/lab" in startup_command
    assert "-e" in run_command
    assert "JUPYTER_TOKEN=secret-workspace-token" in run_command
    assert "ANTHROPIC_BASE_URL=https://llm-gateway.internal" in run_command
    assert "ANTHROPIC_MODEL=local-coder" in run_command
    assert "ANTHROPIC_DEFAULT_MODEL=local-coder" in run_command
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=local-coder" in run_command
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME=Local Coder" in run_command
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL=local-coder" in run_command
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL=local-coder" in run_command
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL=local-coder" in run_command
    assert "CLAUDE_CONFIG_DIR=/tmp/devcloud-claude" in run_command
    assert "CLAUDE_CODE_EXECUTABLE=/opt/conda/bin/claude" in run_command
    raw_claude_settings = next(
        value.removeprefix("DEVCLOUD_CLAUDE_SETTINGS_JSON=")
        for value in run_command
        if value.startswith("DEVCLOUD_CLAUDE_SETTINGS_JSON=")
    )
    claude_settings = json.loads(raw_claude_settings)
    expected_model_ids = [model["model_id"] for model in catalog]
    assert claude_settings["model"] == "local-coder"
    assert claude_settings["availableModels"] == expected_model_ids
    assert claude_settings["enforceAvailableModels"] is True
    assert claude_settings["modelPicker"]["mode"] == "replace"
    assert [
        option["model"] for option in claude_settings["modelPicker"]["options"]
    ] == expected_model_ids
    assert claude_settings["modelPicker"]["options"][0] == {
        "model": "local-coder",
        "label": "Local Coder",
        "description": "On-Prem",
    }
    assert not any(value.startswith("CLAUDE_AVAILABLE_MODELS=") for value in run_command)
    assert not any(value.startswith("ANTHROPIC_CUSTOM_MODEL_OPTION=") for value in run_command)
    assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1" in run_command
    assert "ANTHROPIC_AUTH_TOKEN=shared-ai-token" in run_command
    assert "CLAUDE_CODE_DISABLE_FAST_MODE=1" in run_command
    assert "CLAUDE_CODE_ENABLE_AUTO_MODE=0" in run_command
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in run_command
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS=132000" in run_command
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000" in run_command
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=95" in run_command
    assert (
        "--PersonaManager.default_persona_id="
        "jupyter-ai-personas::jupyter_ai_acp_client::ClaudeAcpPersona"
    ) in startup_command
    assert not any("disable_check_xsrf" in arg for arg in startup_command)
    assert not any("allow_origin" in arg for arg in startup_command)


@pytest.mark.asyncio
async def test_workspace_creation_refuses_unsynchronized_managed_image(monkeypatch):
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False

    monkeypatch.setattr(
        svc, "ensure_workspace_storage", lambda user_id, workspace_id: "/workspace"
    )

    async def missing_image(*_args, **_kwargs):
        return False

    async def fake_run_cmd(*_args, **_kwargs):
        return 0, "", ""

    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", missing_image)

    with pytest.raises(RuntimeError, match="not synchronized"):
        await svc.create_workspace_container(
            workspace_id="12345678-1234-1234-1234-123456789abc",
            user_id=1,
            container_name="devcloud-1-12345678",
            template_id="vscode-python",
            flavor_id="t1.micro",
            host_port=10100,
            workspace_token="secret-workspace-token",
        )


@pytest.mark.asyncio
async def test_gpu_container_uses_one_exact_cdi_device(monkeypatch):
    svc = PodmanService(podman_bin="podman")
    svc._mock_mode = False
    commands = []

    async def fake_run_cmd(*args, timeout=None):
        commands.append(args)
        return (0, "gpu-container-id", "") if args[0] == "run" else (0, "", "")

    async def image_ready(*_args, **_kwargs):
        return True

    class FakeWriter:
        def close(self):
            pass
        async def wait_closed(self):
            pass

    async def ready_port(*_args, **_kwargs):
        return object(), FakeWriter()

    monkeypatch.setattr(svc, "ensure_workspace_storage", lambda *_args: "/workspace")
    monkeypatch.setattr(svc, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(svc, "ensure_image_exists", image_ready)
    monkeypatch.setattr(asyncio, "open_connection", ready_port)

    cdi = "nvidia.com/gpu=GPU-test-4090"
    await svc.create_workspace_container(
        workspace_id="gpu-workspace",
        user_id=1,
        container_name="gpu-container",
        template_id="vscode-python",
        flavor_id="g1.shared",
        host_port=10101,
        workspace_token="token",
        accelerator_cdi_name=cdi,
    )
    run_command = next(args for args in commands if args[0] == "run")
    assert run_command[run_command.index("--device") + 1] == cdi
    assert "label=disable" in run_command
    assert not any(value.endswith("=all") for value in run_command)

    with pytest.raises(ValueError, match="tekil"):
        await svc.create_workspace_container(
            workspace_id="gpu-workspace-2", user_id=1, container_name="gpu-container-2",
            template_id="vscode-python", flavor_id="g1.shared", host_port=10102,
            workspace_token="token", accelerator_cdi_name="nvidia.com/gpu=all",
        )
