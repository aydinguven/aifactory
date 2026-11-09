"""Outbound-only AI Factory worker agent.

Run with AIFACTORY_CONTROLLER_URL, AIFACTORY_NODE_ID and AIFACTORY_NODE_TOKEN set.
The legacy AIFACTORY_MASTER_URL name remains an upgrade compatibility fallback.
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import ssl
import tempfile
import hashlib
import time
import uuid
import contextlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets

from app import __version__
from app.config import settings
from app.installer.progress import live_details
from app.worker_transfers import WorkerTransfers
from app.agents.transfers import CHUNK_BYTES
from app.agents.manager import STREAM_WINDOW, MAX_STREAM_FRAME_BYTES, MAX_STREAMS
from app.orchestrator.podman_service import PodmanExecutionError, podman_service
from app.model_container_registry import (
    ModelContainerRegistryConfig,
    environment_model_container_registry_config,
    test_model_container_registry,
    validate_model_container_registry_config,
)
from app.release_catalog import semantic_version
from app.jupyter_ai import normalize_ca_bundle
from app.schemas.jupyter_ai_settings import JupyterAiModel
from app.worker_gpu import discover_nvidia_capabilities

logger = logging.getLogger("aifactory.worker")


def _download_progress(done: int, total: int) -> dict:
    """Same shape as the root updater's progress file (app/installer/progress.py)."""
    share = min(1.0, done / total) if total else 0.0
    return {
        "phase": "download",
        "label": "Bundle indiriliyor",
        "bytes_done": done,
        "bytes_total": total,
        "percent": round(10.0 * share, 1),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _storage_writable(root: str) -> dict:
    """Create and remove a probe file so a read-only remount is caught."""
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".aifactory-health-"):
            pass
        return {"ok": True, "message": ""}
    except OSError as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]}


def _unsigned_ota_approved(metadata: dict) -> bool:
    """Honor only an explicit boolean approval from the enrolled controller."""
    return metadata.get("allow_unsigned") is True


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} ayarlanmalıdır.")
    return value


def _model_registry_config_from_payload(payload: dict) -> ModelContainerRegistryConfig:
    keys = {"registry_url", "registry_username", "registry_password"}
    if not any(key in payload for key in keys):
        return environment_model_container_registry_config()
    registry_url = str(payload.get("registry_url") or "").strip().rstrip("/")
    username = str(payload.get("registry_username") or "").strip()
    password = str(payload.get("registry_password") or "")
    if len(registry_url) > 1024 or len(username) > 255 or len(password) > 4096:
        raise ValueError("Model Container Registry ayarları izin verilen boyutu aşıyor.")
    config = ModelContainerRegistryConfig(
        managed=True,
        enabled=bool(registry_url),
        registry_url=registry_url,
        username=username,
        password=password,
    )
    validate_model_container_registry_config(config, require_enabled=True)
    return config


def _connection_url() -> str:
    base = (
        os.environ.get("AIFACTORY_CONTROLLER_URL", "").strip()
        or os.environ.get("AIFACTORY_MASTER_URL", "").strip()
    ).rstrip("/")
    if not base:
        raise RuntimeError("AIFACTORY_CONTROLLER_URL ayarlanmalıdır.")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.startswith(("ws://", "wss://")):
        raise RuntimeError("AIFACTORY_CONTROLLER_URL http:// veya https:// ile başlamalıdır.")
    return f"{base}/api/agent/connect/{_required_env('AIFACTORY_NODE_ID')}"


def _controller_http_url() -> str:
    base = (
        os.environ.get("AIFACTORY_CONTROLLER_URL", "").strip()
        or os.environ.get("AIFACTORY_MASTER_URL", "").strip()
    ).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError("AIFACTORY_CONTROLLER_URL http:// veya https:// ile başlamalıdır.")
    return base


def _tls_context(connection_url: str) -> ssl.SSLContext | None:
    if not connection_url.startswith("wss://"):
        return None
    ca_file = os.environ.get("AIFACTORY_AGENT_CA_FILE", "").strip() or None
    context = ssl.create_default_context(cafile=ca_file)
    cert_file = os.environ.get("AIFACTORY_AGENT_CERT_FILE", "").strip()
    key_file = os.environ.get("AIFACTORY_AGENT_KEY_FILE", "").strip()
    if bool(cert_file) != bool(key_file):
        raise RuntimeError("Agent client sertifikası için cert ve key birlikte ayarlanmalıdır.")
    if cert_file:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context



# Workspace image tags move from localhost/devcloud-* to localhost/aifactory-*
# (3.50.1). Archives and local images built before keep the old tag.
_RENAMED_IMAGE_PREFIXES = (
    ("localhost/aifactory-", "localhost/devcloud-"),
    ("localhost/devcloud-", "localhost/aifactory-"),
)


async def _adopt_renamed_image(image_ref: str) -> bool:
    """Tag the local copy of ``image_ref`` stored under the other product prefix."""
    for prefix, other in _RENAMED_IMAGE_PREFIXES:
        if not image_ref.startswith(prefix):
            continue
        twin = other + image_ref[len(prefix):]
        code, _, _ = await podman_service.run_cmd("image", "exists", twin, timeout=30)
        if code != 0:
            return False
        code, _, _ = await podman_service.run_cmd("tag", twin, image_ref, timeout=60)
        return code == 0
    return False

class WorkerAgent:
    def __init__(self):
        self.websocket = None
        self.send_lock = asyncio.Lock()
        self.registry_lock = asyncio.Lock()
        self.image_sync_lock = asyncio.Lock()
        self.upgrade_task: asyncio.Task | None = None
        self.stream_targets: dict[str, object] = {}
        self.stream_tasks = {}
        self.stream_windows = {}
        self.transfers = WorkerTransfers(self._safe_workspace_path)
        self.registry_path = Path(settings.STORAGE_ROOT) / ".aifactory-agent-registry.json"
        self.registry = self._load_registry()
        self.image_state_path = Path(settings.STORAGE_ROOT) / ".aifactory-image-state.json"
        self.image_state = self._load_image_state()
        self.image_progress: dict[str, dict] = {}
        self.upgrade_status: dict = {
            "state": "idle",
            "target_version": "",
            "message": "",
        }
        # Set to send the next heartbeat right away (upgrade progress).
        self.heartbeat_wakeup = asyncio.Event()
        self._self_check: dict = {}
        self._self_check_at = 0.0

    def _load_registry(self) -> dict:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.registry, indent=2), encoding="utf-8")
        temporary.replace(self.registry_path)

    def _load_image_state(self) -> dict[str, dict]:
        try:
            data = json.loads(self.image_state_path.read_text(encoding="utf-8"))
            return {
                str(key): value
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_image_state(self) -> None:
        self.image_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.image_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.image_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.image_state_path)

    def _set_upgrade_status(
        self,
        state: str,
        *,
        target_version: str = "",
        message: str = "",
        progress: dict | None = None,
    ) -> None:
        self.upgrade_status = {
            "state": state,
            "target_version": target_version,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if progress is not None:
            self.upgrade_status["progress"] = progress
        self.heartbeat_wakeup.set()

    def _reported_upgrade_status(self) -> dict:
        if self.upgrade_status.get("state") in {"preparing", "downloading", "failed"}:
            return dict(self.upgrade_status)
        queue_root = Path(settings.UPDATE_QUEUE_ROOT).resolve()
        for filename, fallback_state in (
            ("running.json", "running"),
            ("pending.json", "queued"),
            ("status.json", "idle"),
        ):
            try:
                value = json.loads(
                    (queue_root / filename).read_text(encoding="utf-8")
                )
            except (FileNotFoundError, ValueError, OSError):
                continue
            if not isinstance(value, dict):
                continue
            # running.json is the moved request and may still say "queued".
            state = (
                "running"
                if filename == "running.json"
                else str(value.get("state") or fallback_state)
            )
            target_version = str(value.get("target_version") or "")
            message = str(value.get("error") or value.get("message") or "").strip()
            if not message and state == "failed":
                output_lines = [
                    line.strip()
                    for line in str(value.get("output") or "").splitlines()
                    if line.strip()
                ]
                message = "\n".join(output_lines[-12:])[-2000:]
            if state == "failed" and "release manifest signature is missing" in message.lower():
                message = (
                    "Release manifest imzası bulunamadı. Geçici imzasız OTA için controller "
                    "üzerinde WORKER_OTA_ALLOW_UNSIGNED=true ayarlayıp aifactory-controller "
                    "servisini yeniden başlatın. Teknik hata: release manifest signature is missing"
                )
            if state == "failed" and target_version == __version__:
                state = "succeeded"
                message = (
                    f"Worker zaten hedef sürümde (v{__version__}). "
                    "Önceki aynı-sürüm güncelleme hatası kapatıldı."
                )
            progress = live_details(queue_root, queue_root / filename, value).get("progress")
            return {
                "state": state,
                "target_version": target_version,
                "message": message,
                **({"progress": progress} if progress else {}),
                "return_code": str(value.get("return_code") or ""),
                "updated_at": str(
                    value.get("finished_at")
                    or value.get("started_at")
                    or value.get("queued_at")
                    or ""
                ),
            }
        return dict(self.upgrade_status)

    @staticmethod
    def _http_verify_context(base_url: str):
        if not base_url.startswith("https://"):
            return True
        ca_file = os.environ.get("AIFACTORY_AGENT_CA_FILE", "").strip() or None
        context = ssl.create_default_context(cafile=ca_file)
        cert_file = os.environ.get("AIFACTORY_AGENT_CERT_FILE", "").strip()
        key_file = os.environ.get("AIFACTORY_AGENT_KEY_FILE", "").strip()
        if bool(cert_file) != bool(key_file):
            raise RuntimeError(
                "Agent client sertifikası için cert ve key birlikte ayarlanmalıdır."
            )
        if cert_file:
            context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return context

    async def sync_workspace_images(self) -> list[dict]:
        """Reconcile enabled controller images into the worker's Podman store."""
        async with self.image_sync_lock:
            return await self._sync_workspace_images()

    async def _sync_workspace_images(self) -> list[dict]:
        base_url = _controller_http_url()
        node_id = _required_env("AIFACTORY_NODE_ID")
        headers = {"Authorization": f"Bearer {_required_env('AIFACTORY_NODE_TOKEN')}"}
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            verify=self._http_verify_context(base_url),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{base_url}/api/agent/images/catalog",
                params={"node_id": node_id},
            )
            response.raise_for_status()
            catalog = response.json().get("images", [])
            if not isinstance(catalog, list):
                raise RuntimeError("Controller workspace image catalog is invalid")

            desired_ids: set[str] = set()
            cache_root = Path(settings.STORAGE_ROOT) / ".aifactory-image-cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            for item in catalog:
                if not isinstance(item, dict):
                    continue
                image_id = str(item.get("id") or "")
                image_ref = str(item.get("image_ref") or "")
                expected_sha256 = str(item.get("sha256") or "")
                expected_size = int(item.get("size") or 0)
                image_url = str(item.get("url") or "")
                if image_url.startswith("/"):
                    download_url = f"{base_url}{image_url}"
                elif image_url.startswith(f"{base_url}/api/agent/images/"):
                    download_url = image_url
                else:
                    download_url = ""
                try:
                    safe_image_id = str(uuid.UUID(image_id)) == image_id
                except ValueError:
                    safe_image_id = False
                if (
                    not safe_image_id
                    or not image_ref
                    or len(expected_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in expected_sha256)
                    or expected_size <= 0
                    or not download_url
                ):
                    raise RuntimeError("Controller returned unsafe workspace image metadata")
                desired_ids.add(image_id)
                self.image_progress[image_id] = {
                    "id": image_id,
                    "image_ref": image_ref,
                    "sha256": expected_sha256,
                    "state": "queued",
                    "downloaded_bytes": 0,
                    "total_bytes": expected_size,
                    "error": "",
                }
                current = self.image_state.get(image_id, {})
                exists_code, _, _ = await podman_service.run_cmd(
                    "image", "exists", image_ref, timeout=30
                )
                if (
                    exists_code != 0
                    and current.get("sha256") == expected_sha256
                    and await _adopt_renamed_image(image_ref)
                ):
                    exists_code = 0
                if current.get("sha256") == expected_sha256 and exists_code == 0:
                    self.image_progress[image_id].update(
                        state="ready", downloaded_bytes=expected_size
                    )
                    continue

                temporary = cache_root / f"{image_id}.partial"
                digest = hashlib.sha256()
                downloaded = 0
                try:
                    self.image_progress[image_id]["state"] = "downloading"
                    async with client.stream("GET", download_url) as download:
                        download.raise_for_status()
                        with temporary.open("wb") as destination:
                            async for chunk in download.aiter_bytes(1024 * 1024):
                                downloaded += len(chunk)
                                self.image_progress[image_id][
                                    "downloaded_bytes"
                                ] = downloaded
                                if downloaded > expected_size:
                                    raise RuntimeError(
                                        f"Workspace image {image_ref} exceeded its catalog size"
                                    )
                                digest.update(chunk)
                                destination.write(chunk)
                    self.image_progress[image_id]["state"] = "verifying"
                    if downloaded != expected_size or digest.hexdigest() != expected_sha256:
                        raise RuntimeError(
                            f"Workspace image verification failed for {image_ref}"
                        )
                    self.image_progress[image_id]["state"] = "loading"
                    code, stdout, stderr = await podman_service.run_cmd(
                        "load", "-i", str(temporary), timeout=1800
                    )
                    if code != 0:
                        raise RuntimeError(
                            f"Podman could not load {image_ref}: {stderr or stdout}"
                        )
                    exists_code, _, _ = await podman_service.run_cmd(
                        "image", "exists", image_ref, timeout=30
                    )
                    if exists_code != 0 and not await _adopt_renamed_image(image_ref):
                        raise RuntimeError(
                            f"Loaded archive did not provide expected image {image_ref}"
                        )
                    self.image_state[image_id] = {
                        "id": image_id,
                        "template_id": str(item.get("template_id") or ""),
                        "image_ref": image_ref,
                        "digest": str(item.get("digest") or ""),
                        "sha256": expected_sha256,
                        "size": expected_size,
                    }
                    self._save_image_state()
                    self.image_progress[image_id].update(
                        state="ready", downloaded_bytes=expected_size, error=""
                    )
                    logger.info("Workspace image synchronized: %s", image_ref)
                except Exception as exc:
                    self.image_progress[image_id].update(
                        state="failed", error=str(exc)[:500]
                    )
                    raise
                finally:
                    temporary.unlink(missing_ok=True)

            stale = set(self.image_state) - desired_ids
            if stale:
                for image_id in stale:
                    self.image_state.pop(image_id, None)
                self._save_image_state()
            for image_id in set(self.image_progress) - desired_ids:
                self.image_progress.pop(image_id, None)
            return [self.image_state[key] for key in sorted(self.image_state)]

    async def image_sync_loop(self) -> None:
        while True:
            try:
                await self.sync_workspace_images()
            except Exception as exc:
                logger.warning("Workspace image synchronization failed: %s", exc)
            await asyncio.sleep(30)

    async def sync_jupyter_ai_settings(self) -> bool:
        """Apply centrally managed Jupyter AI settings to new containers."""
        base_url = _controller_http_url()
        node_id = _required_env("AIFACTORY_NODE_ID")
        headers = {"Authorization": f"Bearer {_required_env('AIFACTORY_NODE_TOKEN')}"}
        async with httpx.AsyncClient(
            headers=headers,
            timeout=30.0,
            verify=self._http_verify_context(base_url),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{base_url}/api/agent/jupyter-ai-settings",
                params={"node_id": node_id},
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise RuntimeError("Controller returned invalid Jupyter AI settings")
        if payload.get("managed") is not True:
            return False
        if payload.get("enabled") is not True:
            settings.JUPYTER_AI_GATEWAY_URL = ""
            settings.JUPYTER_AI_MODEL = ""
            settings.JUPYTER_AI_GATEWAY_TOKEN = ""
            settings.JUPYTER_AI_GATEWAY_MODEL_DISCOVERY = False
            settings.JUPYTER_AI_MODEL_CATALOG_JSON = "[]"
            # Cline is baked into every VS Code image. Disabling Workspace AI
            # clears its managed provider credentials, not the extension.
            settings.JUPYTER_AI_CLINE_ENABLED = True
            return True

        gateway_url = str(payload.get("gateway_url") or "").strip().rstrip("/")
        model_id = str(payload.get("model_id") or "").strip()
        shared_token = payload.get("shared_token")
        raw_models = payload.get("models")
        parsed = urlsplit(gateway_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("Controller returned an invalid Jupyter AI gateway URL")
        if (
            not model_id
            or len(model_id) > 255
            or any(char.isspace() for char in model_id)
        ):
            raise RuntimeError("Controller returned an invalid Jupyter AI model ID")
        if (
            not isinstance(shared_token, str)
            or not shared_token
            or len(shared_token) > 4096
        ):
            raise RuntimeError("Controller returned an invalid Jupyter AI shared token")
        if not isinstance(raw_models, list) or len(raw_models) > 50:
            raise RuntimeError("Controller returned an invalid Jupyter AI model catalog")
        try:
            models = [
                JupyterAiModel.model_validate(item).model_dump()
                for item in raw_models
            ]
        except Exception as exc:
            raise RuntimeError(
                "Controller returned an invalid Jupyter AI model catalog"
            ) from exc
        model_ids = [model["model_id"] for model in models]
        if len(model_ids) != len(set(model_ids)) or model_id not in model_ids:
            raise RuntimeError("Controller returned an inconsistent Jupyter AI catalog")

        settings.JUPYTER_AI_GATEWAY_URL = gateway_url
        settings.JUPYTER_AI_MODEL = model_id
        settings.JUPYTER_AI_GATEWAY_TOKEN = shared_token
        settings.JUPYTER_AI_GATEWAY_MODEL_DISCOVERY = (
            payload.get("gateway_model_discovery") is True
        )
        settings.JUPYTER_AI_MODEL_CATALOG_JSON = json.dumps(
            models, ensure_ascii=False
        )
        # Ignore the legacy toggle from older controllers. It remains in the
        # wire schema only so controller and worker upgrades can roll safely.
        settings.JUPYTER_AI_CLINE_ENABLED = True
        return True

    async def jupyter_ai_settings_sync_loop(self) -> None:
        while True:
            try:
                await self.sync_jupyter_ai_settings()
            except Exception as exc:
                logger.warning("Jupyter AI settings synchronization failed: %s", exc)
            await asyncio.sleep(30)

    async def send(self, message: dict) -> None:
        if message.get("type") == "stream_data":
            window = self.stream_windows.get(message.get("stream_id"))
            if window:
                async with asyncio.timeout(120):
                    await window.acquire()
        async with self.send_lock:
            await self.websocket.send(json.dumps(message, ensure_ascii=False))

    async def result(self, request_id: str, payload: dict | None = None, error: str | None = None):
        await self.send(
            {
                "type": "result",
                "request_id": request_id,
                "ok": error is None,
                "payload": payload or {},
                "error": error,
            }
        )

    async def self_check(self) -> dict:
        """Check Podman and workspace storage; refreshed at most once a minute."""
        now = time.monotonic()
        if self._self_check and now - self._self_check_at < 60:
            return self._self_check
        started = time.monotonic()
        if podman_service.is_mock:
            podman = {"ok": True, "version": "mock", "message": "Mock Podman."}
        else:
            try:
                code, stdout, stderr = await podman_service.run_cmd(
                    "info", "--format", "{{.Version.Version}}", timeout=10
                )
                podman = {
                    "ok": code == 0,
                    "version": stdout.strip()[:64] if code == 0 else "",
                    "message": "" if code == 0 else (stderr or stdout)[-300:],
                }
            except PodmanExecutionError as exc:
                podman = {"ok": False, "version": "", "message": str(exc)[:300]}
        podman["latency_ms"] = round((time.monotonic() - started) * 1000)
        storage = await asyncio.to_thread(_storage_writable, settings.STORAGE_ROOT)
        self._self_check = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "podman": podman,
            "storage": storage,
        }
        self._self_check_at = now
        return self._self_check

    async def heartbeat(self) -> None:
        while True:
            workspace_images = [
                self.image_state[key] for key in sorted(self.image_state)
            ]
            disk = shutil.disk_usage(settings.STORAGE_ROOT)
            memory_kb = 0
            mem_avail_kb = 0
            cpu_pct = 0.0
            try:
                import psutil
                cpu_pct = float(psutil.cpu_percent(interval=None))
                mem = psutil.virtual_memory()
                memory_kb = mem.total // 1024
                mem_avail_kb = mem.available // 1024
            except Exception:
                try:
                    for line in Path("/proc/meminfo").read_text().splitlines():
                        if line.startswith("MemTotal:"):
                            memory_kb = int(line.split()[1])
                        elif line.startswith("MemAvailable:"):
                            mem_avail_kb = int(line.split()[1])
                except OSError:
                    pass

            mem_total_mb = memory_kb // 1024
            mem_used_mb = max(0, mem_total_mb - (mem_avail_kb // 1024))
            disk_total_mb = disk.total // (1024 * 1024)
            disk_used_mb = max(0, (disk.total - disk.free) // (1024 * 1024))
            inventory = []
            for container_name, entry in list(self.registry.items()):
                if not isinstance(entry, dict) or entry.get("deleted"):
                    continue
                inventory.append(
                    {
                        "workspace_id": str(entry.get("workspace_id") or ""),
                        "container_name": container_name,
                        "host_port": int(entry.get("host_port") or 0),
                        "storage_path": str(entry.get("storage_path") or ""),
                        "status": await podman_service.get_container_status(
                            container_name
                        ),
                    }
                )
            active_cnt = len(inventory)
            accelerator_capabilities = await asyncio.to_thread(
                discover_nvidia_capabilities
            )
            health = await self.self_check()

            await self.send(
                {
                    "type": "heartbeat",
                    "payload": {
                        "hostname": socket.gethostname(),
                        "cpu_total": float(os.cpu_count() or 0),
                        "memory_total_mb": mem_total_mb,
                        "disk_total_mb": disk_total_mb,
                        "cpu_percent": round(cpu_pct, 1),
                        "memory_used_mb": mem_used_mb,
                        "disk_used_mb": disk_used_mb,
                        "active_containers_count": active_cnt,
                        "capabilities": {
                            "runtime": "podman",
                            "health": health,
                            **accelerator_capabilities,
                            "upgrade": self._reported_upgrade_status(),
                            "workspace_images": workspace_images,
                            "workspace_image_sync": [
                                self.image_progress[key]
                                for key in sorted(self.image_progress)
                            ],
                        },
                        "inventory": inventory,
                        "agent_version": __version__,
                    },
                }
            )
            # Report every 3 s while an upgrade runs, otherwise every 20 s or
            # right after a status change.
            active = self._reported_upgrade_status().get("state") in {
                "preparing", "downloading", "queued", "running",
            }
            self.heartbeat_wakeup.clear()
            try:
                await asyncio.wait_for(
                    self.heartbeat_wakeup.wait(), timeout=3 if active else 20
                )
            except asyncio.TimeoutError:
                pass

    def _registered_storage(self, container_name: str) -> str:
        entry = self._registered_entry(container_name)
        return str(entry.get("storage_path") or "")

    def _registered_entry(self, container_name: str, workspace_id: str = "") -> dict:
        entry = self.registry.get(container_name)
        if not isinstance(entry, dict) or entry.get("deleted"):
            raise PermissionError("Container bu worker'a kayıtlı değil.")
        if workspace_id and str(entry.get("workspace_id") or "") != workspace_id:
            raise PermissionError("Workspace ve container eşleşmesi doğrulanamadı.")
        return entry

    def _safe_workspace_path(self, container_name: str, relative_path: str = "") -> Path:
        storage_path = self._registered_storage(container_name)
        if not storage_path:
            raise FileNotFoundError("Workspace storage kaydı bulunamadı.")
        root = Path(storage_path).resolve()
        target = (root / str(relative_path).lstrip("/\\")).resolve()
        if target != root and root not in target.parents:
            raise PermissionError("Workspace dışındaki dosyalara erişilemez.")
        return target

    async def handle_container_command(self, action: str, payload: dict) -> dict:
        if action in {"container.start", "container.stop", "container.delete", "container.remove"}:
            async with self.registry_lock:
                return await self._handle_container_command(action, payload)
        return await self._handle_container_command(action, payload)

    async def _handle_container_command(self, action: str, payload: dict) -> dict:
        name = str(payload.get("container_name") or "")
        if action == "container.create":
            template_definition = payload.get("template_definition")
            flavor_definition = payload.get("flavor_definition")
            if isinstance(template_definition, dict):
                from app.orchestrator.templates import get_template, register_custom_template
                if not get_template(str(template_definition.get("template_id") or "")):
                    register_custom_template(**template_definition)
            if isinstance(flavor_definition, dict):
                from app.orchestrator.flavors import Flavor, register_flavor
                register_flavor(Flavor(**flavor_definition))
            allowed = {
                "workspace_id", "user_id", "container_name", "template_id",
                "flavor_id", "host_port", "workspace_token",
                "accelerator_cdi_name", "image_ref", "image_sha256",
                "mlflow_environment", "service_environment", "ai_gateway_token",
                "kilo_code_config", "ai_ca_bundle", "ai_skills_bundle",
            }
            args = {key: value for key, value in payload.items() if key in allowed}
            token = args.get("ai_gateway_token")
            if token is not None and (
                not isinstance(token, str)
                or len(token) > 4096
                or any(character.isspace() or ord(character) < 32 for character in token)
            ):
                args.pop("ai_gateway_token")
            if "kilo_code_config" in args:
                from app.kilo_code import validated_config

                kilo_code_config = validated_config(args["kilo_code_config"])
                if kilo_code_config:
                    args["kilo_code_config"] = kilo_code_config
                else:
                    args.pop("kilo_code_config")
            if "ai_ca_bundle" in args:
                ca_bundle = normalize_ca_bundle(args["ai_ca_bundle"])
                if ca_bundle:
                    args["ai_ca_bundle"] = ca_bundle
                else:
                    args.pop("ai_ca_bundle")
            if "ai_skills_bundle" in args:
                from app.skills import validated_bundle

                skills_bundle = validated_bundle(args["ai_skills_bundle"])
                if skills_bundle:
                    args["ai_skills_bundle"] = skills_bundle
                else:
                    logger.warning(
                        "Dropping an invalid skill library bundle for %s",
                        args.get("container_name"),
                    )
                    args.pop("ai_skills_bundle")
            async with self.registry_lock:
                existing = self.registry.get(args["container_name"])
                if isinstance(existing, dict):
                    if str(existing.get("workspace_id") or "") != str(args["workspace_id"]):
                        raise PermissionError(
                            "Container adı başka bir workspace için kayıtlı."
                        )
                    if int(existing.get("host_port") or -1) != int(args["host_port"]):
                        raise PermissionError(
                            "Tekrarlanan create isteğinin host portu kayıtla eşleşmiyor."
                        )
                    if str(existing.get("accelerator_cdi_name") or "") != str(
                        args.get("accelerator_cdi_name") or ""
                    ):
                        raise PermissionError(
                            "Tekrarlanan create isteğinin GPU tahsisi kayıtla eşleşmiyor."
                        )
                    if await podman_service.container_exists(args["container_name"]):
                        return {
                            "container_id": str(existing.get("container_id") or ""),
                            "storage_path": str(existing.get("storage_path") or ""),
                            "reused": True,
                        }
                container_id, storage_path = await podman_service.create_workspace_container(**args)
                self.registry[args["container_name"]] = {
                    "workspace_id": args["workspace_id"],
                    "container_id": container_id,
                    "storage_path": storage_path,
                    "host_port": args["host_port"],
                    "accelerator_cdi_name": args.get("accelerator_cdi_name") or "",
                }
                self._save_registry()
                return {"container_id": container_id, "storage_path": storage_path}
        if not name:
            raise ValueError("container_name gereklidir.")
        if action == "container.delete" and self.registry.get(name, {}).get("deleted"):
            return {"success": True, "already_deleted": True}
        registered = self._registered_entry(name)
        if action == "container.exists":
            return {"exists": await podman_service.container_exists(name)}
        if action == "container.start":
            return {"success": await podman_service.start_container(name)}
        if action == "container.stop":
            return {"success": await podman_service.stop_container(name)}
        if action == "container.remove":
            # Recreate support: the registry entry and the storage stay, so the
            # next container.create for this workspace rebuilds it in place.
            return {"success": await podman_service.delete_container(name)}
        if action == "container.status":
            return {"status": await podman_service.get_container_status(name)}
        if action == "container.logs":
            tail = max(1, min(int(payload.get("tail", 100)), 1000))
            return {"logs": await podman_service.get_logs(name, tail=tail)}
        if action == "container.port_ready":
            host_port = int(payload.get("host_port", -1))
            if int(registered.get("host_port", -2)) != host_port:
                raise PermissionError("Workspace port eşleşmesi doğrulanamadı.")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", host_port), timeout=0.6
                )
            except (OSError, TimeoutError):
                return {"ready": False}
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return {"ready": True}
        if action == "container.health_ready":
            host_port = int(payload.get("host_port", -1))
            path = str(payload.get("path") or "")
            if int(registered.get("host_port", -2)) != host_port:
                raise PermissionError("Workspace port eşleşmesi doğrulanamadı.")
            if (
                not path.startswith("/")
                or len(path) > 128
                or ".." in path
                or "?" in path
                or "#" in path
            ):
                raise ValueError("Geçersiz container sağlık yolu.")
            if podman_service.is_mock:
                return {"ready": True}
            try:
                async with httpx.AsyncClient(
                    timeout=1.0, follow_redirects=False, trust_env=False
                ) as client:
                    response = await client.get(
                        f"http://127.0.0.1:{host_port}{path}"
                    )
                    response.raise_for_status()
                return {"ready": True}
            except httpx.HTTPError:
                return {"ready": False}
        if action == "container.stats":
            return await podman_service.get_container_stats(name)
        if action == "container.storage_size":
            storage_path = self._registered_storage(name)
            if not storage_path:
                return {"bytes": 0}
            from app.orchestrator.metrics_service import get_dir_size_bytes
            return {"bytes": get_dir_size_bytes(storage_path)}
        if action == "container.snapshot":
            workspace_id = str(payload.get("workspace_id") or "")
            self._registered_entry(name, workspace_id)
            image_tag = str(payload.get("image_tag") or "").strip()
            if not image_tag:
                raise ValueError("image_tag gereklidir.")
            success = await podman_service.commit_container(name, image_tag)
            if not success:
                raise RuntimeError("Container image snapshot oluşturulamadı.")
            return {"success": True, "image_tag": image_tag}
        if action == "container.delete":
            success = await podman_service.delete_container(name)
            if not success:
                return {"success": False}
            storage_path = self._registered_storage(name)
            if storage_path:
                root = Path(settings.STORAGE_ROOT).resolve()
                target = Path(storage_path).resolve()
                if target == root or root not in target.parents:
                    raise PermissionError("Workspace storage is outside the configured root.")
                if target.exists():
                    await asyncio.to_thread(shutil.rmtree, target)
            previous = self.registry[name]
            self.registry[name] = {"workspace_id": previous["workspace_id"], "deleted": True}
            try:
                self._save_registry()
            except Exception:
                self.registry[name] = previous
                raise
            return {"success": success}
        raise ValueError(f"Desteklenmeyen container komutu: {action}")

    async def handle_backup_open(self, request_id: str, payload: dict) -> None:
        """Create a ZIP on the worker and stream it over the existing tunnel."""
        from app.orchestrator.backup_service import create_workspace_zip_backup

        stream_id = str(payload["stream_id"])
        container_name = str(payload.get("container_name") or "")
        workspace_id = str(payload.get("workspace_id") or "")
        entry = self._registered_entry(container_name, workspace_id)
        storage_path = str(entry.get("storage_path") or "")
        if not storage_path or not Path(storage_path).is_dir():
            await self.result(request_id, error="Workspace storage bulunamadı.")
            return
        descriptor, archive_name = tempfile.mkstemp(
            prefix=f"aifactory-{workspace_id[:8]}-", suffix=".zip"
        )
        os.close(descriptor)
        archive = Path(archive_name)
        try:
            cancelled = threading.Event()
            build = asyncio.create_task(asyncio.to_thread(
                create_workspace_zip_backup, storage_path, archive,
                cancelled=cancelled, max_bytes=settings.FILE_TRANSFER_MAX_BYTES,
            ))
            try:
                await asyncio.shield(build)
            except asyncio.CancelledError:
                cancelled.set()
                with contextlib.suppress(Exception):
                    await build
                raise
            await self.result(
                request_id,
                {
                    "filename": f"{workspace_id}.zip",
                    "size": archive.stat().st_size,
                },
            )
            with archive.open("rb") as handle:
                while chunk := handle.read(256 * 1024):
                    await self.send(
                        {
                            "type": "stream_data",
                            "stream_id": stream_id,
                            "encoding": "base64",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
            await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send(
                {"type": "stream_error", "stream_id": stream_id, "error": str(exc)}
            )
        finally:
            archive.unlink(missing_ok=True)

    async def handle_file_command(self, action: str, payload: dict) -> dict:
        name = str(payload.get("container_name") or "")
        target = self._safe_workspace_path(name, str(payload.get("path") or ""))
        root = self._safe_workspace_path(name)
        if action == "files.list":
            if not target.is_dir():
                raise FileNotFoundError("Dizin bulunamadı.")
            items = []
            for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                stat = entry.stat()
                items.append(
                    {
                        "name": entry.name,
                        "path": entry.relative_to(root).as_posix(),
                        "is_dir": entry.is_dir(),
                        "size_bytes": stat.st_size if entry.is_file() else 0,
                        "modified_timestamp": stat.st_mtime,
                    }
                )
            current = target.relative_to(root).as_posix()
            return {"current_path": "" if current == "." else current, "items": items}
        if action in {"files.upload", "files.download"}:
            raise ValueError("File transfers require the chunked protocol; update controller and worker together.")
        if action == "files.mkdir":
            target.mkdir(parents=True, exist_ok=True)
            return {"name": target.name}
        if action == "files.delete":
            if target == root:
                raise PermissionError("Workspace kök dizini silinemez.")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                raise FileNotFoundError("Dosya veya dizin bulunamadı.")
            return {"name": target.name}
        raise ValueError(f"Desteklenmeyen dosya komutu: {action}")

    async def handle_image_command(self, action: str, payload: dict) -> dict:
        if action == "image.registry.test":
            registry = _model_registry_config_from_payload(payload)
            return await test_model_container_registry(registry)
        if action == "image.remove":
            image_tag = str(payload.get("image_tag") or "").strip()
            managed = any(
                isinstance(value, dict) and value.get("image_ref") == image_tag
                for value in self.image_state.values()
            )
            if not managed:
                fallback = environment_model_container_registry_config()
                if (
                    not fallback.registry_url
                    or not image_tag.startswith(f"{fallback.registry_url}/")
                ):
                    raise ValueError("Silinecek image worker yönetim kataloğunda bulunmuyor.")
            if podman_service.is_mock:
                return {"success": True, "image_tag": image_tag}
            code, stdout, stderr = await podman_service.run_cmd(
                "rmi", image_tag, timeout=120
            )
            detail = (stderr or stdout)[-1000:]
            missing = any(
                marker in detail.lower()
                for marker in ("no such image", "image not known", "not found")
            )
            success = code == 0 or missing
            if success:
                self.image_state = {
                    key: value
                    for key, value in self.image_state.items()
                    if value.get("image_ref") != image_tag
                }
            return {
                "success": success,
                "image_tag": image_tag,
                "message": detail,
            }
        if action == "image.mlflow.build":
            model_name = str(payload.get("model_name") or "").strip()
            model_version = str(payload.get("model_version") or "").strip()
            image_tag = str(payload.get("image_tag") or "").strip()
            registry = _model_registry_config_from_payload(payload)
            if (
                not model_name
                or len(model_name) > 255
                or not model_version.isdigit()
                or int(model_version) < 1
            ):
                raise ValueError("Geçerli model adı ve sayısal versiyonu gereklidir.")
            if not image_tag.startswith(f"{registry.registry_url}/"):
                raise ValueError("MLflow image hedefi yönetilen registry altında olmalıdır.")
            success, logs = await podman_service.build_mlflow_model_image(
                model_uri=f"models:/{model_name}/{int(model_version)}",
                image_tag=image_tag,
                mlflow_environment=payload.get("mlflow_environment") or {},
                ca_certificate=str(payload.get("mlflow_ca_certificate") or ""),
                registry_username=registry.username,
                registry_password=registry.password,
            )
            return {
                "success": success,
                "logs": logs,
                "image_tag": image_tag,
            }
        if action != "image.build":
            raise ValueError(f"Desteklenmeyen image komutu: {action}")
        image_tag = str(payload.get("image_tag") or "").strip()
        containerfile = str(payload.get("containerfile") or "")
        if not image_tag or not containerfile:
            raise ValueError("image_tag ve containerfile gereklidir.")
        success, logs = await podman_service.build_image_from_content(
            containerfile_content=containerfile,
            image_tag=image_tag,
        )
        if success and payload.get("push") and not podman_service.is_mock:
            code, stdout, stderr = await podman_service.run_cmd(
                "push", image_tag, timeout=600
            )
            if code != 0:
                return {
                    "success": False,
                    "logs": logs + "\n" + (stderr or stdout),
                }
        return {"success": success, "logs": logs, "image_tag": image_tag}

    async def handle_http_open(self, request_id: str, payload: dict) -> None:
        stream_id = str(payload["stream_id"])
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        response = None
        body_handle = None
        try:
            target_url = await self._target_url(payload, websocket=False)
            if payload.get("body_transfer_id"):
                body_handle = self.transfers.take_http_body(payload["body_transfer_id"])
                async def body_chunks():
                    while chunk := await asyncio.to_thread(body_handle.read, CHUNK_BYTES):
                        yield chunk
                body = body_chunks()
            else:
                body = base64.b64decode(payload.get("body", ""), validate=True)
            request = client.build_request(
                method=payload["method"],
                url=target_url,
                headers=payload.get("headers") or {},
                content=body,
            )
            response = await client.send(request, stream=True)
            headers = list(response.headers.multi_items())
            await self.result(
                request_id,
                {"status_code": response.status_code, "headers": headers},
            )
            async for raw in response.aiter_raw():
                # Do not ask httpx to accumulate a full chunk: small SSE and IDE
                # responses must be forwarded immediately.
                for offset in range(0, len(raw), CHUNK_BYTES):
                    chunk = raw[offset:offset + CHUNK_BYTES]
                    await self.send({"type": "stream_data", "stream_id": stream_id,
                                     "encoding": "base64", "data": base64.b64encode(chunk).decode("ascii")})
            await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send({"type": "stream_error", "stream_id": stream_id, "error": str(exc)})
        finally:
            if response is not None:
                await response.aclose()
            await client.aclose()
            if body_handle:
                body_handle.close()

    async def _target_url(self, payload: dict, websocket: bool) -> str:
        scheme = "ws" if websocket else "http"
        if payload.get("custom_port") is not None:
            port = int(payload["custom_port"])
            if not 1 <= port <= 65535:
                raise ValueError("Geçersiz custom port.")
            container_name = str(payload["container_name"])
            self._registered_entry(
                container_name,
                str(payload.get("workspace_id") or ""),
            )
            host = await podman_service.get_container_ip(container_name)
            if not host:
                raise RuntimeError("Container IP adresi bulunamadı.")
        else:
            port = int(payload["host_port"])
            registered = self._registered_entry(
                str(payload["container_name"]),
                str(payload.get("workspace_id") or ""),
            )
            if int(registered.get("host_port", -1)) != port:
                raise PermissionError("Workspace port eşleşmesi doğrulanamadı.")
            host = "127.0.0.1"
        path = "/" + str(payload.get("path") or "").lstrip("/")
        query = str(payload.get("query") or "")
        return f"{scheme}://{host}:{port}{path}" + (f"?{query}" if query else "")

    async def handle_ws_open(self, request_id: str, payload: dict) -> None:
        stream_id = str(payload["stream_id"])
        try:
            target_url = await self._target_url(payload, websocket=True)
            # Offer the browser's subprotocols to the container. ttyd only
            # serves sockets that request "tty", so dropping them here leaves a
            # terminal workspace stuck on "Press Enter to Reconnect".
            subprotocols = [
                str(item) for item in (payload.get("subprotocols") or []) if str(item)
            ]
            target = await websockets.connect(
                target_url,
                additional_headers=payload.get("headers") or None,
                subprotocols=subprotocols or None,
                max_size=MAX_STREAM_FRAME_BYTES,
            )
            self.stream_targets[stream_id] = target
            negotiated = getattr(target, "subprotocol", None)
            await self.result(
                request_id,
                {
                    "connected": True,
                    "subprotocol": str(negotiated) if negotiated else "",
                },
            )
            try:
                while True:
                    message = await target.recv()
                    is_text = isinstance(message, str)
                    raw = message.encode("utf-8") if is_text else message
                    await self.send(
                        {
                            "type": "stream_data",
                            "stream_id": stream_id,
                            "encoding": "text" if is_text else "base64",
                            "data": message if is_text else base64.b64encode(raw).decode("ascii"),
                        }
                    )
            finally:
                await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send({"type": "stream_error", "stream_id": stream_id, "error": str(exc)})
        finally:
            target = self.stream_targets.pop(stream_id, None)
            if target:
                await target.close()

    async def handle_stream_message(self, message: dict) -> None:
        stream_id = str(message.get("stream_id") or "")
        if message.get("type") == "stream_ack":
            window = self.stream_windows.get(stream_id)
            if window and window._value < STREAM_WINDOW:
                window.release()
            return
        if message.get("type") in {"stream_cancel", "stream_end"}:
            task = self.stream_tasks.get(stream_id)
            if task:
                task.cancel()
            return
        target = self.stream_targets.get(stream_id)
        if not target:
            raise RuntimeError("Workspace WebSocket stream is closed")
        data = message.get("data", "")
        if len(data) > 4 * ((MAX_STREAM_FRAME_BYTES + 2) // 3):
            raise ValueError("WebSocket frame exceeds stream limit")
        if message.get("encoding") == "text":
            await target.send(data)
        else:
            await target.send(base64.b64decode(data, validate=True))

    async def handle_system_command(self, action: str, payload: dict) -> dict:
        if action == "system.catalog_sync":
            from app.orchestrator.flavors import BUILTIN_FLAVOR_IDS, FLAVORS, Flavor, register_flavor
            from app.orchestrator.templates import BUILTIN_TEMPLATE_IDS, TEMPLATES, register_custom_template

            flavors = payload.get("flavors") or []
            templates = payload.get("templates") or []
            if not isinstance(flavors, list) or not isinstance(templates, list):
                raise ValueError("Geçersiz workspace kataloğu.")
            desired_flavors = set()
            for item in flavors:
                if not isinstance(item, dict):
                    continue
                definition = {key: item[key] for key in Flavor.__dataclass_fields__ if key in item}
                flavor = Flavor(**definition)
                register_flavor(flavor)
                desired_flavors.add(flavor.id)
            for flavor_id in set(FLAVORS) - set(BUILTIN_FLAVOR_IDS) - desired_flavors:
                FLAVORS.pop(flavor_id, None)
            desired_templates = set()
            for item in templates:
                if not isinstance(item, dict):
                    continue
                definition = dict(item)
                definition["template_id"] = definition.pop("id", "")
                template = register_custom_template(**definition)
                desired_templates.add(template.id)
            for template_id in set(TEMPLATES) - set(BUILTIN_TEMPLATE_IDS) - desired_templates:
                TEMPLATES.pop(template_id, None)
            return {
                "message": f"{len(desired_flavors)} flavor ve {len(desired_templates)} özel şablon güncellendi."
            }
        if action == "system.jupyter_ai_test":
            gateway_url = str(payload.get("gateway_url") or "").strip().rstrip("/")
            model_id = str(payload.get("model_id") or "").strip()
            shared_token = payload.get("shared_token")
            parsed = urlsplit(gateway_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Gecersiz Jupyter AI gateway URL.")
            if (
                not model_id
                or len(model_id) > 255
                or any(character.isspace() for character in model_id)
            ):
                raise ValueError("Gecersiz Jupyter AI model kimligi.")
            if (
                not isinstance(shared_token, str)
                or not shared_token
                or len(shared_token) > 4096
            ):
                raise ValueError("Gecersiz Jupyter AI gateway tokeni.")

            # httpx's default trust (certifi, or SSL_CERT_FILE) plus the Admin
            # gateway CA bundle, the same CAs workspace containers receive.
            verify = httpx.create_ssl_context()
            ca_bundle = normalize_ca_bundle(payload.get("ca_bundle_pem"))
            if ca_bundle:
                verify.load_verify_locations(cadata=ca_bundle)

            started_at = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=5.0),
                    follow_redirects=False,
                    verify=verify,
                ) as client:
                    response = await client.post(
                        f"{gateway_url}/v1/messages",
                        headers={
                            "Authorization": f"Bearer {shared_token}",
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                            "user-agent": "aifactory-jupyter-ai-connectivity-test",
                        },
                        json={
                            "model": model_id,
                            "max_tokens": 4,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Reply with exactly: OK",
                                }
                            ],
                        },
                    )
            except httpx.TimeoutException:
                return {
                    "ok": False,
                    "model_id": model_id,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000),
                    "message": "Gateway istegi 30 saniyede zaman asimina ugradi.",
                }
            except httpx.HTTPError as exc:
                return {
                    "ok": False,
                    "model_id": model_id,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000),
                    "message": f"Gateway baglantisi kurulamadi: {exc}",
                }

            latency_ms = round((time.perf_counter() - started_at) * 1000)
            if response.is_success:
                return {
                    "ok": True,
                    "model_id": model_id,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "message": "LiteLLM model istegi basariyla yanitlandi.",
                }

            detail = ""
            try:
                error_payload = response.json()
                error_value = error_payload.get("error")
                if isinstance(error_value, dict):
                    detail = str(
                        error_value.get("message")
                        or error_value.get("detail")
                        or ""
                    )
                elif error_value:
                    detail = str(error_value)
                if not detail:
                    detail = str(error_payload.get("detail") or "")
            except (TypeError, ValueError):
                detail = response.text
            detail = " ".join(detail.split())[:500]
            return {
                "ok": False,
                "model_id": model_id,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message": (
                    f"LiteLLM HTTP {response.status_code}: {detail}"
                    if detail
                    else f"LiteLLM HTTP {response.status_code} dondurdu."
                ),
            }

        if action == "system.upgrade":
            controller_url = (
                os.environ.get("AIFACTORY_CONTROLLER_URL", "").strip()
                or _required_env("AIFACTORY_MASTER_URL")
            )
            if self.upgrade_task is not None and not self.upgrade_task.done():
                return {
                    "status": "upgrade_in_progress",
                    "message": "Worker güncellemesi zaten çalışıyor.",
                }
            self._set_upgrade_status(
                "preparing",
                message="Controller release bilgisi alınıyor.",
            )
            self.upgrade_task = asyncio.create_task(
                self._execute_upgrade(controller_url)
            )
            return {"status": "upgrade_started", "message": "Worker güncellemesi başlatıldı."}
        raise ValueError(f"Desteklenmeyen sistem komutu: {action}")

    async def _execute_upgrade(self, controller_url: str) -> None:
        await asyncio.sleep(1)
        temporary: Path | None = None
        destination: Path | None = None
        target_version = ""
        try:
            node_id = _required_env("AIFACTORY_NODE_ID")
            token = _required_env("AIFACTORY_NODE_TOKEN")
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(
                timeout=60.0,
                verify=self._http_verify_context(controller_url),
                follow_redirects=False,
            ) as client:
                metadata_response = await client.get(
                    f"{controller_url.rstrip('/')}/api/agent/releases/latest",
                    params={"node_id": node_id},
                    headers=headers,
                )
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
                target_version = str(metadata.get("version") or "")
                current_semantic = semantic_version(__version__)
                target_semantic = semantic_version(target_version)
                if target_version == __version__:
                    self._set_upgrade_status(
                        "succeeded",
                        target_version=target_version,
                        message=f"Worker zaten güncel (v{__version__}).",
                    )
                    return
                if (
                    current_semantic is not None
                    and target_semantic is not None
                    and target_semantic < current_semantic
                ):
                    raise RuntimeError(
                        f"Yayımlanan v{target_version}, worker sürümü "
                        f"v{__version__}'dan eski; sürüm düşürme engellendi."
                    )
                self._set_upgrade_status(
                    "downloading",
                    target_version=target_version,
                    message="Platform bundle indiriliyor.",
                )
                queue_root = Path(settings.UPDATE_QUEUE_ROOT).resolve()
                uploads = queue_root / "uploads"
                uploads.mkdir(parents=True, exist_ok=True)
                if (queue_root / "pending.json").exists() or (
                    queue_root / "running.json"
                ).exists():
                    raise RuntimeError("Başka bir worker güncellemesi zaten bekliyor.")
                suffix = (
                    "".join(Path(str(metadata["filename"])).suffixes[-2:])
                    or ".release"
                )
                destination = uploads / f"{uuid.uuid4().hex}{suffix}"
                temporary = destination.with_suffix(".part")
                download_url = str(metadata.get("url") or "")
                canonical_base = controller_url.rstrip("/")
                if download_url.startswith("/"):
                    download_url = f"{canonical_base}{download_url}"
                elif not download_url.startswith(
                    f"{canonical_base}/api/agent/releases/"
                ):
                    raise RuntimeError(
                        "Controller returned a release URL outside its canonical origin"
                    )
                digest = hashlib.sha256()
                size = 0
                total = int(metadata.get("size") or 0)
                reported = 0
                async with client.stream(
                    "GET", download_url, headers=headers
                ) as response:
                    response.raise_for_status()
                    with temporary.open("xb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > settings.UPDATE_MAX_UPLOAD_BYTES:
                                raise RuntimeError("Worker release boyut sınırını aşıyor.")
                            digest.update(chunk)
                            handle.write(chunk)
                            if size - reported >= 8 * 1024 * 1024:
                                reported = size
                                self._set_upgrade_status(
                                    "downloading",
                                    target_version=target_version,
                                    message="Platform bundle indiriliyor.",
                                    progress=_download_progress(size, total),
                                )
                        handle.flush()
                        os.fsync(handle.fileno())
                if digest.hexdigest() != metadata["sha256"]:
                    raise RuntimeError("Worker release checksum doğrulaması başarısız.")
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
                marker = {
                    "state": "queued",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                    "filename": metadata["filename"],
                    "bundle": str(destination),
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "target_version": target_version,
                    "allow_unsigned": _unsigned_ota_approved(metadata),
                }
                marker_tmp = queue_root / "pending.tmp"
                marker_tmp.write_text(
                    json.dumps(marker, indent=2) + "\n", encoding="utf-8"
                )
                os.chmod(marker_tmp, 0o600)
                os.replace(marker_tmp, queue_root / "pending.json")
                self._set_upgrade_status(
                    "queued",
                    target_version=target_version,
                    message="Bundle root updater kuyruğuna alındı.",
                )
        except Exception as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            self._set_upgrade_status(
                "failed",
                target_version=target_version,
                message=str(exc),
            )
            logger.exception("Upgrade execution error: %s", exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def handle_command(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "")
        action = str(message.get("action") or "")
        payload = message.get("payload") or {}
        handlers = {"proxy.http.open": self.handle_http_open,
                    "proxy.websocket.open": self.handle_ws_open,
                    "workspace.backup.open": self.handle_backup_open}
        if action in handlers:
            if payload.get("flow_control") is not True:
                await self.result(request_id, error="Streaming protocol mismatch; update controller and worker together.")
                return
            stream_id = str(payload.get("stream_id") or "")
            if not stream_id or stream_id in self.stream_tasks or len(self.stream_tasks) >= MAX_STREAMS:
                await self.result(request_id, error="Worker stream limit reached or invalid stream ID")
                return
            self.stream_tasks[stream_id] = asyncio.current_task()
            self.stream_windows[stream_id] = asyncio.Semaphore(STREAM_WINDOW)
            try:
                await handlers[action](request_id, payload)
            except Exception as exc:
                await self.result(request_id, error=str(exc))
                await self.send({"type": "stream_error", "stream_id": stream_id, "error": str(exc)})
            finally:
                self.stream_tasks.pop(stream_id, None)
                self.stream_windows.pop(stream_id, None)
            return
        try:
            if action.startswith("transfer."):
                result = await asyncio.to_thread(self.transfers.command, action, payload)
            elif action == "proxy.websocket.send":
                await self.handle_stream_message({"type": "stream_data", **payload})
                result = {"sent": True}
            elif action.startswith("system."):
                result = await self.handle_system_command(action, payload)
            elif action.startswith("image."):
                result = await self.handle_image_command(action, payload)
            elif action.startswith("files."):
                result = await self.handle_file_command(action, payload)
            else:
                result = await self.handle_container_command(action, payload)
            await self.result(request_id, result)
        except Exception as exc:
            logger.exception("Worker command failed: %s", action)
            await self.result(request_id, error=str(exc))

    async def expire_transfers(self):
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(self.transfers.expire)

    async def run_once(self) -> None:
        headers = {"Authorization": f"Bearer {_required_env('AIFACTORY_NODE_TOKEN')}"}
        connection_url = _connection_url()
        try:
            await self.sync_jupyter_ai_settings()
        except Exception as exc:
            logger.warning("Initial Jupyter AI settings synchronization failed: %s", exc)
        async with websockets.connect(
            connection_url,
            additional_headers=headers,
            ssl=_tls_context(connection_url),
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self.websocket = websocket
            transfer_cleanup_task = asyncio.create_task(self.expire_transfers())
            heartbeat_task = asyncio.create_task(self.heartbeat())
            image_sync_task = asyncio.create_task(self.image_sync_loop())
            jupyter_ai_sync_task = asyncio.create_task(
                self.jupyter_ai_settings_sync_loop()
            )
            tasks: set[asyncio.Task] = set()
            try:
                async for raw in websocket:
                    if not isinstance(raw, str):
                        continue
                    message = json.loads(raw)
                    if message.get("type") == "command":
                        task = asyncio.create_task(self.handle_command(message))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif message.get("type") in {"stream_data", "stream_end", "stream_ack", "stream_cancel"}:
                        await self.handle_stream_message(message)
            finally:
                transfer_cleanup_task.cancel()
                heartbeat_task.cancel()
                image_sync_task.cancel()
                jupyter_ai_sync_task.cancel()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, transfer_cleanup_task, heartbeat_task,
                                     image_sync_task, jupyter_ai_sync_task, return_exceptions=True)
                await asyncio.to_thread(self.transfers.expire, True)

    async def run_forever(self) -> None:
        delay = 1
        while True:
            try:
                await self.run_once()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Controller bağlantısı kesildi: %s; %s sn sonra tekrar denenecek", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(WorkerAgent().run_forever())


if __name__ == "__main__":
    main()
