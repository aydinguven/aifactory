import asyncio
import base64
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import settings
from app.cline import managed_cline_files, managed_vscode_settings
from app.jupyter_ai import (
    claude_settings,
    model_environment,
    normalize_ca_bundle,
    parse_model_catalog,
)
from app.mlflow_workspace import (
    validate_mlflow_environment,
    validate_mlflow_service_environment,
)
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template
from app.kilo_code import (
    CONFIG_DIR as KILO_CODE_CONFIG_DIR,
    CONFIG_FILENAME as KILO_CODE_CONFIG_FILENAME,
    KILO_CODE_ASSISTANT,
    managed_vscode_settings as kilo_code_vscode_settings,
    validated_config as validated_kilo_code_config,
)
from app.skills import (
    SKILLS_DIR as KILO_CODE_SKILLS_DIR,
    validated_bundle as validated_skills_bundle,
)

logger = logging.getLogger("aifactory.podman")
CDI_DEVICE_PATTERN = re.compile(r"^nvidia\.com/gpu=[A-Za-z0-9_.:/-]+$")
_SENSITIVE_ENV_KEYS = {
    "PASSWORD",
    "JUPYTER_TOKEN",
    "AIFACTORY_CLINE_SECRETS_JSON",
    "AIFACTORY_KILO_CONFIG_JSON",
    "MLFLOW_TRACKING_TOKEN",
    "MLFLOW_TRACKING_PASSWORD",
}


# The Admin gateway CA bundle reaches the container as an environment value;
# the startup command writes it next to a copy of the image's system bundle.
# Python (ssl, httpx), requests and curl replace their trust store with the
# combined file, Node (Claude Code, code-server, Kilo Code) adds the extra CAs
# to its built-in roots.
WORKSPACE_CA_DIR = "/tmp/aifactory-ca"
WORKSPACE_CA_EXTRA_FILE = f"{WORKSPACE_CA_DIR}/extra-ca.pem"
WORKSPACE_CA_BUNDLE_FILE = f"{WORKSPACE_CA_DIR}/ca-bundle.pem"
WORKSPACE_CA_ENVIRONMENT = {
    "SSL_CERT_FILE": WORKSPACE_CA_BUNDLE_FILE,
    "REQUESTS_CA_BUNDLE": WORKSPACE_CA_BUNDLE_FILE,
    "CURL_CA_BUNDLE": WORKSPACE_CA_BUNDLE_FILE,
    "NODE_EXTRA_CA_CERTS": WORKSPACE_CA_EXTRA_FILE,
}
WORKSPACE_CA_SETUP = (
    f"install -d -m 755 {WORKSPACE_CA_DIR}"
    f" && printf '%s\\n' \"$AIFACTORY_CA_BUNDLE_PEM\" > {WORKSPACE_CA_EXTRA_FILE}"
    " && { for system_bundle in /etc/ssl/certs/ca-certificates.crt"
    " /etc/pki/tls/certs/ca-bundle.crt; do"
    " if [ -r \"$system_bundle\" ]; then cat \"$system_bundle\"; echo; break; fi;"
    f" done; cat {WORKSPACE_CA_EXTRA_FILE}; }} > {WORKSPACE_CA_BUNDLE_FILE}"
    f" && chmod 644 {WORKSPACE_CA_EXTRA_FILE} {WORKSPACE_CA_BUNDLE_FILE}"
    " && unset AIFACTORY_CA_BUNDLE_PEM"
)


def _skills_setup(target_dir: str) -> str:
    """Unpack the Admin skill library from AIFACTORY_SKILLS_TGZ into ``target_dir``.

    The worker has already checked that the archive only holds
    ``<name>/SKILL.md`` files. A failure is reported and the IDE still starts.
    """
    # The outer group always succeeds, so it can sit in an ``&&`` chain.
    return (
        "{ { install -d -m 700 " + target_dir
        + " && printf '%s' \"$AIFACTORY_SKILLS_TGZ\" | base64 -d"
        " | tar -xzf - --no-same-owner -C " + target_dir + ";"
        " } || echo 'AI Factory: skill kütüphanesi yazılamadı.' >&2;"
        " unset AIFACTORY_SKILLS_TGZ; }"
    )


def _redacted_command(cmd: list[str]) -> str:
    redacted = []
    for argument in cmd:
        key, separator, _value = argument.partition("=")
        if separator and key in _SENSITIVE_ENV_KEYS:
            argument = f"{key}=<redacted>"
        elif separator and key == "AIFACTORY_CA_BUNDLE_PEM":
            # Public, but several KB of base64 per log line.
            argument = f"{key}=<pem>"
        elif separator and key == "AIFACTORY_SKILLS_TGZ":
            # Not secret, but up to ~120 KB of base64.
            argument = f"{key}=<skills>"
        redacted.append(argument)
    return " ".join(redacted)


class MlflowCliUnavailable(Exception):
    """Raised when the host has no usable MLflow command line interface."""


def _mlflow_module_available(name: str = "mlflow") -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def resolve_mlflow_cli(
    configured_bin: str | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    interpreter: str | None = None,
    module_available: Callable[[str], bool] = _mlflow_module_available,
) -> list[str]:
    """Return the argv prefix that runs the MLflow CLI on this host.

    Workers are started as ``<project>/.venv/bin/python -m app.worker_agent``
    while systemd hands them a fixed PATH that excludes the virtualenv, so the
    default bare ``mlflow`` name is regularly unresolvable even though the
    package is installed right next to the running interpreter. Probe the
    configured binary first, then the interpreter's own ``bin/`` directory, and
    finally fall back to ``<python> -m mlflow`` so an image build never dies on
    a PATH detail.
    """
    configured = (
        settings.MLFLOW_BIN if configured_bin is None else configured_bin
    ).strip()
    python = interpreter or sys.executable
    attempted: list[str] = []

    if configured:
        attempted.append(configured)
        if os.sep in configured:
            candidate = Path(configured)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return [str(candidate)]
        else:
            resolved = which(configured)
            if resolved:
                return [resolved]

    if python:
        sibling = Path(python).parent / (Path(configured).name or "mlflow")
        attempted.append(str(sibling))
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return [str(sibling)]
        attempted.append(f"{python} -m mlflow")
        if module_available("mlflow"):
            return [python, "-m", "mlflow"]

    raise MlflowCliUnavailable(
        "MLflow komut satırı aracı build worker'ında bulunamadı. Denenen yollar: "
        + ", ".join(attempted or ["mlflow"])
        + ". Worker'ın sanal ortamında 'pip install -r requirements.txt' "
        "çalıştırın veya MLFLOW_BIN ayarını mlflow çalıştırılabilirinin tam "
        "yoluna ayarlayın."
    )


class PodmanExecutionError(Exception):
    """Exception raised when a podman command fails."""
    pass


class PodmanService:
    """Service to orchestrate Podman containers for user workspaces."""

    def __init__(self, podman_bin: str | None = None):
        self.podman_bin = podman_bin or settings.PODMAN_BIN
        self._mock_mode: bool = settings.USE_MOCK_PODMAN or not self._check_podman_installed()
        # In-memory store for mock mode
        self._mock_containers: dict[str, dict[str, Any]] = {}
        if self._mock_mode:
            logger.info("PodmanService running in MOCK mode (simulated containers).")
        else:
            logger.info(f"PodmanService using Podman binary at: {self.podman_bin}")

    @property
    def is_mock(self) -> bool:
        return self._mock_mode

    def _check_podman_installed(self) -> bool:
        """Check if podman executable is found in PATH."""
        return shutil.which(self.podman_bin) is not None

    async def run_cmd(
        self,
        *args: str,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Execute Podman without waiting for descendant processes to close pipes.

        Podman's monitor process can inherit stdout/stderr after a detached
        podman run. Process.communicate() then waits for EOF until the
        container exits even though the Podman client has already completed.
        File-backed output lets us wait for the CLI process itself and still
        retain its diagnostics.
        """
        cmd = [self.podman_bin, *args]
        safe_command = _redacted_command(cmd)
        logger.debug("Executing command: %s", safe_command)

        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                try:
                    if timeout is None:
                        return_code = await process.wait()
                    else:
                        return_code = await asyncio.wait_for(process.wait(), timeout=timeout)
                except TimeoutError as exc:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    raise PodmanExecutionError(
                        f"Podman command timed out after {timeout:g}s: {safe_command}"
                    ) from exc

                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().decode("utf-8", errors="replace").strip()
                stderr = stderr_file.read().decode("utf-8", errors="replace").strip()
                return return_code or 0, stdout, stderr
        except PodmanExecutionError:
            raise
        except Exception as exc:
            logger.error("Failed to execute podman command '%s': %s", safe_command, exc)
            raise PodmanExecutionError(f"Podman execution failed: {exc}") from exc

    async def get_active_podman_ports(self) -> set[int]:
        """Query running Podman containers to find host ports currently mapped."""
        if self._mock_mode:
            return set()
        code, stdout, _ = await self.run_cmd("ps", "--format", "{{.Ports}}")
        if code != 0 or not stdout:
            return set()
        ports = set()
        import re
        for line in stdout.splitlines():
            matches = re.findall(r":(\d+)->", line)
            for m in matches:
                try:
                    ports.add(int(m))
                except ValueError:
                    pass
        return ports

    def find_free_port(self, used_ports: set[int]) -> int:
        """Find an available TCP port on the host within the configured range."""
        for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END):
            if port in used_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("Yapılandırılan aralıkta kullanılabilir port bulunamadı.")

    async def find_available_port(self, db_used_ports: set[int]) -> int:
        """Find free port considering both database records and active Podman containers."""
        active_ports = await self.get_active_podman_ports()
        all_used = db_used_ports | active_ports
        return self.find_free_port(all_used)


    def ensure_workspace_storage(self, user_id: int, workspace_id: str) -> str:
        """Create and initialize the persistent directory on host for workspace."""
        user_dir = Path(settings.STORAGE_ROOT) / str(user_id)
        workspace_dir = user_dir / str(workspace_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize a default README in the workspace
        readme_file = workspace_dir / "README.md"
        if not readme_file.exists():
            readme_file.write_text(
                f"# Çalışma Alanı {workspace_id}\n\nBulut çalışma alanınıza hoş geldiniz.\n"
                f"Bu dizindeki dosyalar Container yeniden başlatıldığında korunur.\n",
                encoding="utf-8",
            )
        return str(workspace_dir.resolve())

    async def ensure_image_exists(
        self,
        template_id: str,
        image_tag: str,
        progress_callback: Any | None = None,
    ) -> bool:
        """Check whether the controller-managed image exists in local Podman."""
        if self._mock_mode:
            return True

        code, _, _ = await self.run_cmd("image", "exists", image_tag)
        if code == 0:
            if progress_callback:
                await progress_callback(f"Image [{image_tag}] yerel OCI deposunda doğrulandı", "success")
            return True

        logger.error(
            "Controller-managed workspace image is not synchronized: %s", image_tag
        )
        if progress_callback:
            await progress_callback(
                f"Image [{image_tag}] bu worker'a henüz senkronize edilmedi",
                "error",
            )
        return False

    async def create_workspace_container(
        self,
        workspace_id: str,
        user_id: int,
        container_name: str,
        template_id: str,
        flavor_id: str,
        host_port: int,
        workspace_token: str,
        accelerator_cdi_name: str = "",
        image_ref: str = "",
        image_sha256: str = "",
        mlflow_environment: dict[str, str] | None = None,
        service_environment: dict[str, str] | None = None,
        ai_gateway_token: str = "",
        kilo_code_config: str = "",
        ai_ca_bundle: str = "",
        ai_skills_bundle: str = "",
        progress_callback: Any | None = None,
    ) -> tuple[str, str]:
        """Create and run a new container for a workspace.
        
        Returns:
            tuple of (container_id, storage_path)
        """
        import time

        template = get_template(template_id)
        if not template:
            raise ValueError(f"Bilinmeyen şablon: {template_id}")

        flavor = get_flavor(flavor_id)
        if not flavor:
            raise ValueError(f"Bilinmeyen kaynak profili: {flavor_id}")

        accelerator_cdi_name = str(accelerator_cdi_name or "").strip()
        if flavor.accelerator_count:
            if (
                not CDI_DEVICE_PATTERN.fullmatch(accelerator_cdi_name)
                or accelerator_cdi_name.rsplit("=", 1)[-1].lower() == "all"
            ):
                raise ValueError("GPU workspace için tekil ve geçerli NVIDIA CDI cihazı gereklidir.")
        elif accelerator_cdi_name:
            raise ValueError("CPU profiline GPU CDI cihazı atanamaz.")

        storage_path = self.ensure_workspace_storage(user_id, workspace_id)
        mlflow_environment = validate_mlflow_environment(mlflow_environment)
        service_environment = validate_mlflow_service_environment(service_environment)
        image_ref = str(image_ref or template.image_tag).strip()
        if (
            not image_ref
            or len(image_ref) > 512
            or any(ord(character) < 32 for character in image_ref)
        ):
            raise ValueError("Geçerli bir workspace image referansı gereklidir.")
        is_vscode = template.ide_type == "vscode"
        is_kilo_code = is_vscode and template.ai_assistant == KILO_CODE_ASSISTANT
        kilo_code_config = (
            validated_kilo_code_config(kilo_code_config) if is_kilo_code else ""
        )
        if is_kilo_code and not kilo_code_config:
            logger.warning(
                "Kilo Code workspace %s starts without kilo.jsonc: the controller "
                "sent no Kilo Code config (check Admin > Entegrasyonlar > Kilo Code "
                "and Workspace AI).",
                container_name,
            )
        is_jupyter = template.ide_type == "jupyter"
        is_service = template.ide_type == "service"
        # Only images whose startup command AI Factory controls can build the
        # combined bundle before the IDE starts.
        ai_ca_bundle = (
            normalize_ca_bundle(ai_ca_bundle) if is_vscode or is_jupyter else ""
        )
        # Kilo Code reads the skills folder from kilo.jsonc, so without one
        # the library would be unused.
        ai_skills_bundle = (
            validated_skills_bundle(ai_skills_bundle)
            if is_jupyter or (is_kilo_code and kilo_code_config)
            else ""
        )

        if self._mock_mode:
            container_id = f"mock-cid-{workspace_id[:12]}"
            self._mock_containers[container_name] = {
                "id": container_id,
                "status": "running",
                "template": template_id,
                "flavor": flavor_id,
                "host_port": host_port,
                "storage_path": storage_path,
                "accelerator_cdi_name": accelerator_cdi_name,
                "image_ref": image_ref,
                "image_sha256": image_sha256,
                "mlflow_environment": mlflow_environment,
                "service_environment": service_environment,
                "logs": [
                    f"[{container_name}] {template.name} başlatılıyor...",
                    f"[{container_name}] Kalıcı volume bağlandı: {template.container_workdir}",
                    f"[{container_name}] {flavor.cpus} CPU ve {flavor.memory_display} RAM ayrıldı",
                    f"[{container_name}] Servis {template.default_port} portunu dinliyor",
                    f"[{container_name}] Çalışma alanı bağlantıya hazır.",
                ],
            }
            if progress_callback:
                await progress_callback(f"Mock Container {host_port} portunda hazır", "success")
            return container_id, storage_path

        # 1. Clean up any stale/dead container with the same name
        if progress_callback:
            await progress_callback(f"[{container_name}] için önceki örnekler temizleniyor...", "dim")
        await self.run_cmd("rm", "-f", container_name)

        # 2. Refuse implicit pulls/builds when the managed image is unavailable.
        image_ready = await self.ensure_image_exists(
            template_id, image_ref, progress_callback
        )
        if not image_ready:
            raise RuntimeError(
                f"Controller-managed workspace image is not synchronized: {image_ref}"
            )

        # 3. Podman run flags
        if progress_callback:
            await progress_callback(f"podman run çalıştırılıyor: {flavor.cpus} CPU ve {flavor.memory_display} RAM...", "info")

        cmd_args = [
            "run",
            "-d",
            "--name", container_name,
            "--cpus", str(flavor.cpus),
            "--memory", f"{flavor.memory_mb}m",
            "-p", f"127.0.0.1:{host_port}:{template.default_port}",
            "--restart", "unless-stopped",
        ]
        if template.mount_workspace:
            # Relabel for SELinux and align bind-mount ownership with the
            # image's non-root user (jovyan/coder).
            cmd_args.extend([
                "-v", f"{storage_path}:{template.container_workdir}:Z,U",
            ])
        if accelerator_cdi_name:
            cmd_args.extend([
                "--device", accelerator_cdi_name,
                "--security-opt", "label=disable",
            ])

        # Injected environment variables for auth & config
        # A per-user LiteLLM key from the controller replaces the worker's
        # shared gateway token for this container only.
        gateway_token = str(ai_gateway_token or "") or settings.JUPYTER_AI_GATEWAY_TOKEN
        cline_files: dict[str, str] = {}
        if is_vscode:
            cmd_args.extend([
                "-e", f"PASSWORD={workspace_token}",
                "-e", "DISABLE_TELEMETRY=true",
                "--entrypoint", "/bin/bash",
                "-e",
                "VSCODE_USER_DIR=/home/coder/.local/share/code-server/User",
            ])
            if is_kilo_code:
                cmd_args.extend([
                    "-e", "AIFACTORY_VSCODE_SETTINGS_JSON="
                    + kilo_code_vscode_settings(kilo_code_config),
                ])
                if kilo_code_config:
                    cmd_args.extend([
                        "-e", f"AIFACTORY_KILO_CONFIG_DIR={KILO_CODE_CONFIG_DIR}",
                        "-e", "AIFACTORY_KILO_CONFIG_JSON=" + kilo_code_config,
                    ])
            else:
                cmd_args.extend([
                    "-e", "AIFACTORY_VSCODE_SETTINGS_JSON=" + managed_vscode_settings(),
                ])
                cline_files = managed_cline_files(
                    settings.JUPYTER_AI_GATEWAY_URL,
                    gateway_token,
                    settings.JUPYTER_AI_MODEL,
                )
            if cline_files:
                cmd_args.extend([
                    "-e", "CLINE_DIR=/home/coder/.cline",
                    "-e", "CLINE_DATA_DIR=/home/coder/.cline/data",
                    "-e", "AIFACTORY_CLINE_GLOBAL_STATE_JSON="
                    + cline_files["globalState.json"],
                    "-e", "AIFACTORY_CLINE_SECRETS_JSON="
                    + cline_files["secrets.json"],
                    "-e", "AIFACTORY_CLINE_PROVIDERS_JSON="
                    + cline_files["settings/providers.json"],
                ])
        elif is_jupyter:
            model_catalog = parse_model_catalog(
                settings.JUPYTER_AI_MODEL_CATALOG_JSON,
                settings.JUPYTER_AI_MODEL,
            )
            cmd_args.extend([
                "-e", f"JUPYTER_TOKEN={workspace_token}",
                "-e", "JUPYTER_ENABLE_LAB=yes",
                "-e", "CLAUDE_CONFIG_DIR=/tmp/aifactory-claude",
                "-e", "CLAUDE_CODE_EXECUTABLE=/opt/conda/bin/claude",
                "-e", "AIFACTORY_CLAUDE_SETTINGS_JSON=" + json.dumps(
                    claude_settings(settings.JUPYTER_AI_MODEL, model_catalog),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ])
            if settings.JUPYTER_AI_GATEWAY_URL:
                cmd_args.extend([
                    "-e", f"ANTHROPIC_BASE_URL={settings.JUPYTER_AI_GATEWAY_URL}",
                ])
            if gateway_token:
                cmd_args.extend([
                    "-e",
                    f"ANTHROPIC_AUTH_TOKEN={gateway_token}",
                ])
            for key, value in model_environment(
                settings.JUPYTER_AI_MODEL,
                model_catalog,
                discovery_enabled=settings.JUPYTER_AI_GATEWAY_MODEL_DISCOVERY,
            ).items():
                cmd_args.extend(["-e", f"{key}={value}"])
            cmd_args.extend([
                "-e", "CLAUDE_CODE_DISABLE_FAST_MODE=1",
                "-e", "CLAUDE_CODE_ENABLE_AUTO_MODE=0",
                "-e", "CLAUDE_CODE_DISABLE_1M_CONTEXT=1",
                "-e", "CLAUDE_CODE_MAX_CONTEXT_TOKENS=132000",
                "-e", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000",
                "-e", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=95",
                "-e", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=2048",
                "-e", "MAX_MCP_OUTPUT_TOKENS=8000",
                "-e", "BASH_MAX_OUTPUT_LENGTH=12000",
                "-e", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS=4000",
            ])

        if ai_ca_bundle:
            cmd_args.extend(["-e", f"AIFACTORY_CA_BUNDLE_PEM={ai_ca_bundle}"])
            for key, value in WORKSPACE_CA_ENVIRONMENT.items():
                cmd_args.extend(["-e", f"{key}={value}"])
        if ai_skills_bundle:
            cmd_args.extend(["-e", f"AIFACTORY_SKILLS_TGZ={ai_skills_bundle}"])

        for k, v in template.env_vars.items():
            cmd_args.extend(["-e", f"{k}={v}"])
        for key, value in mlflow_environment.items():
            cmd_args.extend(["-e", f"{key}={value}"])
        for key, value in service_environment.items():
            cmd_args.extend(["-e", f"{key}={value}"])
        cmd_args.extend([
            "-e", f"AIFACTORY_WORKSPACE_ID={workspace_id}",
            "-e", f"AIFACTORY_USER_ID={user_id}",
            # Names before 3.49.1, for user scripts that still read them.
            "-e", f"DEVCLOUD_WORKSPACE_ID={workspace_id}",
            "-e", f"DEVCLOUD_USER_ID={user_id}",
        ])

        # Exact image selected and pinned by the controller.
        cmd_args.append(image_ref)

        ca_setup = f"{WORKSPACE_CA_SETUP} && " if ai_ca_bundle else ""
        if is_jupyter:
            cmd_args.extend([
                "bash",
                "-lc",
                ca_setup
                + "install -d -m 700 \"$CLAUDE_CONFIG_DIR\""
                " && printf '%s' \"$AIFACTORY_CLAUDE_SETTINGS_JSON\""
                " > \"$CLAUDE_CONFIG_DIR/settings.json\""
                " && chmod 600 \"$CLAUDE_CONFIG_DIR/settings.json\""
                # Claude Code's user-level skills folder.
                + (
                    " && " + _skills_setup("\"$CLAUDE_CONFIG_DIR/skills\"")
                    if ai_skills_bundle
                    else ""
                )
                + " && exec start-notebook.py \"$@\"",
                "aifactory-jupyter",
                "--ServerApp.ip=0.0.0.0",
                f"--ServerApp.port={template.default_port}",
                f"--ServerApp.root_dir={template.container_workdir}",
                f"--ServerApp.base_url=/proxy/{workspace_id}/",
                "--ServerApp.default_url=/lab",
                "--ServerApp.trust_xheaders=True",
                "--PersonaManager.default_persona_id=jupyter-ai-personas::jupyter_ai_acp_client::ClaudeAcpPersona",
            ])
        elif is_vscode:
            setup_commands: list[str] = [
                *([WORKSPACE_CA_SETUP] if ai_ca_bundle else []),
                "install -d -m 700 \"$VSCODE_USER_DIR\"",
                "printf '%s' \"$AIFACTORY_VSCODE_SETTINGS_JSON\""
                " > \"$VSCODE_USER_DIR/settings.json\"",
                "chmod 600 \"$VSCODE_USER_DIR/settings.json\"",
            ]
            if cline_files:
                setup_commands.extend([
                    "install -d -m 700 \"$CLINE_DATA_DIR/settings\"",
                    "printf '%s' \"$AIFACTORY_CLINE_GLOBAL_STATE_JSON\""
                    " > \"$CLINE_DATA_DIR/globalState.json\"",
                    "printf '%s' \"$AIFACTORY_CLINE_SECRETS_JSON\""
                    " > \"$CLINE_DATA_DIR/secrets.json\"",
                    "printf '%s' \"$AIFACTORY_CLINE_PROVIDERS_JSON\""
                    " > \"$CLINE_DATA_DIR/settings/providers.json\"",
                    "chmod 600 \"$CLINE_DATA_DIR/globalState.json\""
                    " \"$CLINE_DATA_DIR/secrets.json\""
                    " \"$CLINE_DATA_DIR/settings/providers.json\"",
                ])
            if kilo_code_config:
                config_path = f"$AIFACTORY_KILO_CONFIG_DIR/{KILO_CODE_CONFIG_FILENAME}"
                setup_commands.extend([
                    "install -d -m 700 \"$AIFACTORY_KILO_CONFIG_DIR\"",
                    f"printf '%s' \"$AIFACTORY_KILO_CONFIG_JSON\" > \"{config_path}\"",
                    f"chmod 600 \"{config_path}\"",
                    # Keep the keys out of every editor terminal's environment.
                    "unset AIFACTORY_KILO_CONFIG_JSON",
                ])
            if ai_skills_bundle:
                # kilo.jsonc lists this folder under "skills".
                setup_commands.append(_skills_setup(KILO_CODE_SKILLS_DIR))
            setup_commands.append(
                "exec /usr/bin/entrypoint.sh"
                f" --bind-addr 0.0.0.0:{template.default_port}"
                f" --auth none {template.container_workdir}"
            )
            cmd_args.extend([
                "-lc",
                " && ".join(setup_commands),
            ])
        elif template.startup_command:
            cmd_args.extend(template.startup_command)

        try:
            code, stdout, stderr = await self.run_cmd(
                *cmd_args,
                timeout=settings.PODMAN_RUN_TIMEOUT_SECONDS,
            )
        except PodmanExecutionError as exc:
            # A timed-out Podman client may still have created the container.
            # Recover by name so a healthy detached container is not orphaned.
            inspect_code, inspect_stdout, _ = await self.run_cmd(
                "inspect", "--format", "{{.Id}}", container_name, timeout=5
            )
            if inspect_code != 0 or not inspect_stdout:
                raise exc
            code, stdout, stderr = 0, inspect_stdout, ""
            if progress_callback:
                await progress_callback(
                    "Podman client yanıt süresi doldu; çalışan Container adıyla geri kazanıldı.",
                    "info",
                )
        if code != 0:
            raise PodmanExecutionError(f"Container başlatılamadı: {stderr or stdout}")

        container_id = stdout.strip()
        if not container_id:
            inspect_code, inspect_stdout, inspect_stderr = await self.run_cmd(
                "inspect", "--format", "{{.Id}}", container_name, timeout=5
            )
            if inspect_code != 0 or not inspect_stdout:
                raise PodmanExecutionError(
                    f"Container ID olmadan başladı: {inspect_stderr or 'inspect başarısız'}"
                )
            container_id = inspect_stdout.strip()

        # 4. Fast Readiness Health Check
        if progress_callback:
            await progress_callback(f"Container başladı (ID: {container_id[:12]}). {host_port} portu doğrulanıyor...", "info")

        start_time = time.monotonic()
        is_ready = False
        for _ in range(30 if template.require_ready else 15):
            try:
                if template.health_path:
                    async with httpx.AsyncClient(
                        timeout=0.8, follow_redirects=False, trust_env=False
                    ) as client:
                        response = await client.get(
                            f"http://127.0.0.1:{host_port}{template.health_path}"
                        )
                        response.raise_for_status()
                else:
                    _reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", host_port),
                        timeout=0.6,
                    )
                    writer.close()
                    await writer.wait_closed()
                is_ready = True
                elapsed = time.monotonic() - start_time
                if progress_callback:
                    check = template.health_path or f"port {host_port}"
                    await progress_callback(
                        f"Servis sağlık kontrolü başarılı: {check} ({elapsed:.1f} sn)",
                        "success",
                    )
                break
            except Exception:
                await asyncio.sleep(0.5)

        if not is_ready and template.require_ready:
            await self.run_cmd("rm", "-f", container_name, timeout=10)
            raise PodmanExecutionError(
                f"Servis sağlık kontrolü başarısız: {template.health_path or host_port}"
            )
        if not is_ready and progress_callback:
            await progress_callback(
                f"Container çevrimiçi; servis {host_port} portunda başlatılmayı tamamlıyor.",
                "info",
            )

        return container_id, storage_path

    async def container_exists(self, container_name: str) -> bool:
        """Return whether Podman still has a container with this name."""
        if self._mock_mode:
            return container_name in self._mock_containers

        code, _, _ = await self.run_cmd(
            "container", "exists", container_name, timeout=5
        )
        if code not in (0, 1):
            raise RuntimeError("Podman could not determine whether the container exists.")
        return code == 0

    async def start_container(self, container_name: str) -> bool:
        """Start an existing stopped container."""
        if self._mock_mode:
            if container_name in self._mock_containers:
                self._mock_containers[container_name]["status"] = "running"
                self._mock_containers[container_name]["logs"].append(
                    f"[{container_name}] Container yeniden başlatıldı."
                )
            return True

        code, stdout, stderr = await self.run_cmd("start", container_name)
        if code != 0:
            logger.error(f"Failed to start container {container_name}: {stderr}")
            return False
        return True

    async def stop_container(self, container_name: str) -> bool:
        """Stop a running container."""
        if self._mock_mode:
            if container_name in self._mock_containers:
                self._mock_containers[container_name]["status"] = "stopped"
                self._mock_containers[container_name]["logs"].append(
                    f"[{container_name}] Container durduruldu."
                )
            return True

        if not await self.container_exists(container_name):
            return True
        code, stdout, stderr = await self.run_cmd("stop", "-t", "10", container_name)
        if code != 0:
            logger.error(f"Failed to stop container {container_name}: {stderr}")
            return False
        return True

    async def delete_container(self, container_name: str) -> bool:
        """Remove a container (and any associated pod) permanently."""
        if self._mock_mode:
            self._mock_containers.pop(container_name, None)
            return True

        if not await self.container_exists(container_name):
            return True

        # Force remove container
        code, stdout, stderr = await self.run_cmd("rm", "-f", container_name)
        # Also clean up any pod named container_name if present
        await self.run_cmd("pod", "rm", "-f", container_name)
        if code != 0:
            if not await self.container_exists(container_name):
                return True
            logger.warning(f"Failed to remove container {container_name}: {stderr}")
            return False
        return True

    async def get_container_status(self, container_name: str) -> str:
        """Check if container is running, stopped, or missing."""
        if self._mock_mode:
            return self._mock_containers.get(container_name, {}).get("status", "stopped")

        code, stdout, stderr = await self.run_cmd(
            "inspect", "--format", "{{.State.Status}}", container_name
        )
        if code != 0:
            return "stopped"
        status = stdout.strip().lower()
        return status

    async def get_logs(self, container_name: str, tail: int = 100) -> str:
        """Retrieve recent container stdout/stderr logs."""
        if self._mock_mode:
            logs = self._mock_containers.get(container_name, {}).get("logs", [])
            return "\n".join(logs) or "Henüz log bulunmuyor."

        code, stdout, stderr = await self.run_cmd("logs", "--tail", str(tail), container_name)
        if code != 0:
            if "no container with name or ID" in stderr or "no such container" in stderr:
                return f"Container '{container_name}' çalışmıyor veya henüz oluşturulmadı."
            return f"Container logları ({container_name}): {stderr or stdout}"
        return stdout or stderr or "Container çalışıyor. Henüz log çıktısı yok."

    async def load_offline_images(self, images_dir: Path | str) -> list[str]:
        """Load any .tar or .tar.gz image archives present in images_dir into Podman."""
        images_path = Path(images_dir)
        if not images_path.exists():
            return []

        loaded_files = []
        for file in sorted(images_path.glob("*.tar*")):
            if self._mock_mode:
                logger.info(f"[MOCK] Loaded offline image from archive: {file.name}")
                loaded_files.append(file.name)
            else:
                code, stdout, stderr = await self.run_cmd("load", "-i", str(file))
                if code == 0:
                    logger.info(f"Successfully loaded image from {file.name}: {stdout}")
                    loaded_files.append(file.name)
                else:
                    logger.error(f"Failed to load image from {file.name}: {stderr}")
        return loaded_files

    async def list_images(self) -> list[str]:
        """List local container images."""
        if self._mock_mode:
            return [
                "localhost/devcloud-vscode-kilo:latest",
                "localhost/devcloud-vscode-empty:latest",
                "localhost/devcloud-vscode-python:latest",
                "localhost/devcloud-vscode-react:latest",
                "localhost/devcloud-jupyter-python:latest",
                "localhost/devcloud-vscode-java:latest",
                "localhost/devcloud-terminal-rocky:latest",
            ]
        code, stdout, _ = await self.run_cmd("images", "--format", "{{.Repository}}:{{.Tag}}")
        if code != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    async def get_container_stats(self, container_name: str) -> dict:
        """Fetch real-time CPU, RAM, and IO metrics for a container."""
        if self._mock_mode:
            import random
            return {
                "cpu_percent": round(random.uniform(0.5, 4.2), 1),
                "mem_usage_bytes": 156 * 1024 * 1024,
                "mem_limit_bytes": 1024 * 1024 * 1024,
                "mem_usage_display": "156 MB / 1 GB",
                "mem_percent": 15.2,
                "net_io": "12.4 KB / 4.1 KB",
                "block_io": "0 B / 0 B",
                "pids": 4,
            }

        code, stdout, stderr = await self.run_cmd("stats", "--no-stream", "--format", "json", container_name, timeout=4)
        if code != 0 or not stdout:
            return {
                "cpu_percent": 0.0,
                "mem_usage_bytes": 0,
                "mem_limit_bytes": 0,
                "mem_usage_display": "0 MB",
                "mem_percent": 0.0,
                "net_io": "--",
                "block_io": "--",
                "pids": 0,
            }

        try:
            data = json.loads(stdout)
            if isinstance(data, list) and len(data) > 0:
                data = data[0]

            def parse_pct(val):
                if isinstance(val, (int, float)):
                    return float(val)
                val_str = str(val).replace("%", "").strip()
                try:
                    return float(val_str)
                except ValueError:
                    return 0.0

            return {
                "cpu_percent": parse_pct(data.get("CPUPercent", data.get("CPU", 0))),
                "mem_usage_bytes": data.get("MemUsageRaw", 0),
                "mem_limit_bytes": data.get("MemLimitRaw", 0),
                "mem_usage_display": str(data.get("MemUsage", data.get("MemUsageRaw", "--"))),
                "mem_percent": parse_pct(data.get("MemPercent", 0)),
                "net_io": str(data.get("NetIO", "--")),
                "block_io": str(data.get("BlockIO", "--")),
                "pids": data.get("PIDs", 0),
            }
        except Exception as exc:
            logger.warning(f"Error parsing stats JSON for {container_name}: {exc}")
            return {
                "cpu_percent": 0.0,
                "mem_usage_bytes": 0,
                "mem_limit_bytes": 0,
                "mem_usage_display": "--",
                "mem_percent": 0.0,
                "net_io": "--",
                "block_io": "--",
                "pids": 0,
            }

    async def get_container_ip(self, container_name: str) -> str | None:
        """Get the internal bridge network IP address of a Podman container."""
        if self._mock_mode:
            return "127.0.0.1"

        code, stdout, _ = await self.run_cmd("inspect", "--format", "{{.NetworkSettings.IPAddress}}", container_name, timeout=3)
        if code == 0 and stdout.strip():
            return stdout.strip()
        return None

    async def commit_container(self, container_name: str, target_image_tag: str) -> bool:
        """Snapshot a running/stopped container into a reusable Podman image tag."""
        if self._mock_mode:
            logger.info(f"[MOCK] Committed container {container_name} to image {target_image_tag}")
            return True

        code, stdout, stderr = await self.run_cmd("commit", container_name, target_image_tag, timeout=120)
        if code != 0:
            logger.error(f"Failed to commit container {container_name} to {target_image_tag}: {stderr or stdout}")
            return False
        logger.info(f"Successfully committed container {container_name} -> {target_image_tag}")
        return True

    async def build_mlflow_model_image(
        self,
        *,
        model_uri: str,
        image_tag: str,
        mlflow_environment: dict[str, str],
        ca_certificate: str = "",
        registry_username: str = "",
        registry_password: str = "",
    ) -> tuple[bool, str]:
        """Build and push one MLflow serving image without persisting credentials."""
        mlflow_environment = validate_mlflow_environment(mlflow_environment)
        if not model_uri.startswith("models:/") or not model_uri.rsplit("/", 1)[-1].isdigit():
            raise ValueError("Immutable models:/name/version URI is required.")
        if not image_tag or len(image_tag) > 512 or any(ord(c) < 32 for c in image_tag):
            raise ValueError("Invalid MLflow serving image reference.")
        if "\x00" in model_uri or any(ord(c) < 32 for c in model_uri):
            raise ValueError("Invalid MLflow model URI.")
        if bool(registry_username) != bool(registry_password):
            raise ValueError("Registry username and password must be supplied together.")
        if ca_certificate and (
            len(ca_certificate.encode("utf-8")) > 1024 * 1024
            or "BEGIN CERTIFICATE" not in ca_certificate
        ):
            raise ValueError("Invalid MLflow CA certificate.")
        if self._mock_mode:
            return True, f"Mock MLflow image build succeeded: {image_tag}"

        process_environment = {**os.environ, **mlflow_environment}

        def redact(value: str) -> str:
            redacted = value
            for secret in (
                mlflow_environment.get("MLFLOW_TRACKING_TOKEN", ""),
                mlflow_environment.get("MLFLOW_TRACKING_PASSWORD", ""),
                registry_password,
            ):
                if secret:
                    redacted = redacted.replace(secret, "<redacted>")
            return redacted[-200_000:]

        def phase(title: str) -> str:
            return f"===== {title} ====="

        async def run_process(*arguments: str) -> tuple[int, str]:
            """Run one build step, always returning its output instead of raising.

            A build step that raises escapes to the agent command dispatcher,
            which reports only ``str(exc)`` and leaves the build row without any
            log tail. Returning ``(code, output)`` for launch failures and
            timeouts alike keeps every diagnostic attached to the build.
            """
            with tempfile.TemporaryFile() as output_file:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *arguments,
                        env=process_environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=output_file,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                except OSError as exc:
                    return 127, redact(f"{arguments[0]} başlatılamadı: {exc}")
                timed_out = False
                try:
                    return_code = await asyncio.wait_for(
                        process.wait(),
                        timeout=settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    timed_out = True
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    return_code = await process.wait()
                output_file.seek(0)
                output = output_file.read().decode("utf-8", errors="replace")
                if timed_out:
                    output = (
                        f"{output}\nAdım "
                        f"{settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS}s sonra "
                        "zaman aşımına uğradı ve durduruldu."
                    )
                return (124 if timed_out else return_code or 0), redact(output)

        async def run_podman(*arguments: str) -> tuple[int, str]:
            """Run podman, turning execution errors into reportable output."""
            try:
                code, stdout, stderr = await self.run_cmd(
                    *arguments,
                    timeout=settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS,
                )
            except PodmanExecutionError as exc:
                return 125, redact(str(exc))
            return code, redact("\n".join(filter(None, [stdout, stderr])))

        try:
            mlflow_cli = resolve_mlflow_cli()
        except MlflowCliUnavailable as exc:
            logger.error("MLflow model image build cannot start: %s", exc)
            return False, f"{phase('mlflow cli')}\n{exc}"

        with tempfile.TemporaryDirectory(prefix="aifactory-mlflow-build-") as tmpdir:
            os.chmod(tmpdir, 0o700)
            temporary_root = Path(tmpdir)
            build_directory = temporary_root / "context"
            if ca_certificate:
                ca_path = temporary_root / "mlflow-ca.pem"
                ca_path.write_text(ca_certificate, encoding="utf-8")
                os.chmod(ca_path, 0o600)
                process_environment["MLFLOW_TRACKING_SERVER_CERT_PATH"] = str(ca_path)
                process_environment["REQUESTS_CA_BUNDLE"] = str(ca_path)
            code, generate_output = await run_process(
                *mlflow_cli,
                "models",
                "generate-dockerfile",
                "--model-uri",
                model_uri,
                "-d",
                str(build_directory),
            )
            generate_logs = "\n".join(
                [
                    phase(f"mlflow models generate-dockerfile ({model_uri})"),
                    f"$ {' '.join(mlflow_cli)} models generate-dockerfile",
                    generate_output,
                ]
            )
            if code != 0:
                return False, redact(
                    f"{generate_logs}\nAdım {code} çıkış kodu ile başarısız oldu; "
                    "model artifact'ları indirilemedi."
                )
            recipe = next(
                (
                    candidate
                    for candidate in ("Dockerfile", "Containerfile")
                    if (build_directory / candidate).is_file()
                ),
                None,
            )
            if recipe is None:
                return False, redact(
                    f"{generate_logs}\nMLflow başarı bildirdi ancak "
                    f"{build_directory} altında Dockerfile üretilmedi."
                )

            build_code, build_output = await run_podman(
                "build", "-t", image_tag, str(build_directory)
            )
            logs = redact(
                "\n".join(
                    filter(
                        None,
                        [
                            generate_logs,
                            phase(f"podman build -t {image_tag} ({recipe})"),
                            build_output,
                        ],
                    )
                )
            )
            if build_code != 0:
                return False, redact(
                    f"{logs}\nImage build {build_code} çıkış kodu ile başarısız oldu."
                )

            auth_path: Path | None = None
            try:
                push_args = ["push"]
                if registry_username:
                    registry = image_tag.split("/", 1)[0]
                    encoded = base64.b64encode(
                        f"{registry_username}:{registry_password}".encode("utf-8")
                    ).decode("ascii")
                    auth_path = temporary_root / "registry-auth.json"
                    auth_path.write_text(
                        json.dumps({"auths": {registry: {"auth": encoded}}}),
                        encoding="utf-8",
                    )
                    os.chmod(auth_path, 0o600)
                    push_args.extend(["--authfile", str(auth_path)])
                push_args.append(image_tag)
                push_code, push_output = await run_podman(*push_args)
                logs = redact(
                    "\n".join(
                        filter(
                            None,
                            [logs, phase(f"podman push {image_tag}"), push_output],
                        )
                    )
                )
                if push_code != 0:
                    return False, redact(
                        f"{logs}\nImage push {push_code} çıkış kodu ile başarısız "
                        "oldu; Model Container Registry erişimini doğrulayın."
                    )
                return True, logs
            finally:
                if auth_path:
                    auth_path.unlink(missing_ok=True)

    async def build_image_from_content(self, containerfile_content: str, image_tag: str, progress_callback=None) -> tuple[bool, str]:
        """Build a container image on the fly from raw Containerfile text."""
        import tempfile
        if self._mock_mode:
            if progress_callback:
                await progress_callback(f"[MOCK] Successfully built {image_tag}")
            return True, "Mock build successful"

        with tempfile.TemporaryDirectory() as tmpdir:
            cfile = Path(tmpdir) / "Containerfile"
            cfile.write_text(containerfile_content, encoding="utf-8")

            if progress_callback:
                await progress_callback(f"Starting build for {image_tag}...")

            code, stdout, stderr = await self.run_cmd("build", "-t", image_tag, tmpdir, timeout=300)
            logs = f"{stdout}\n{stderr}".strip()
            if code == 0:
                if progress_callback:
                    await progress_callback(f"Build succeeded for {image_tag}!", "success")
                return True, logs
            else:
                if progress_callback:
                    await progress_callback(f"Build failed: {stderr or stdout}", "error")
                return False, logs



podman_service = PodmanService()
