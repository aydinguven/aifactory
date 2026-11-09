"""Per-component health report behind /status and /api/health.

Each component reports ``ok``, ``degraded``, ``down``, ``disabled`` (not
configured) or ``unknown`` (could not be evaluated). The overall status is the
worst component status, except that an optional component can only degrade
the platform, never take it down.

Configuration rows are read once from the request session; network checks then
run concurrently with a per-check timeout, and the whole report is cached for
a few seconds so the public page cannot be used to hammer LDAP or LiteLLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import agent_manager
from app.config import settings
from app.database import release_read_only_connection
from app.task_health import task_health
from app.time_utils import ensure_utc

logger = logging.getLogger("aifactory.health")

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"
DISABLED = "disabled"
UNKNOWN = "unknown"
_RANK = {OK: 0, DISABLED: 0, UNKNOWN: 1, DEGRADED: 1, DOWN: 2}

CHECK_TIMEOUT_SECONDS = 5.0
CACHE_SECONDS = 15.0
DB_SLOW_MS = 500
DISK_DEGRADED_PERCENT = 85
DISK_DOWN_PERCENT = 95
MEMORY_DEGRADED_PERCENT = 95
HEARTBEAT_DEGRADED_SECONDS = 60
HEARTBEAT_DOWN_SECONDS = 120
CERT_WARN_DAYS = 14
UPDATE_FAILURE_WINDOW_DAYS = 7

_ACTIVE_DEPLOYMENT_STATES = {
    "queued", "waiting_for_image", "scheduling", "starting", "health_checking",
}


@dataclass
class Component:
    key: str
    label: str
    required: bool
    status: str = OK
    # ``summary`` is public; ``message`` and ``details`` may name hosts and
    # carry raw errors, so only administrators see them.
    summary: str = ""
    message: str = ""
    latency_ms: int | None = None
    details: dict = field(default_factory=dict)

    def set(self, status: str, summary: str, message: str = "") -> "Component":
        self.status = status
        self.summary = summary
        self.message = message or summary
        return self


def worst(statuses) -> str:
    """Worst of ``statuses``; ``disabled`` counts as fine and ``unknown``
    only wins when nothing is actually degraded."""
    statuses = list(statuses)
    if DOWN in statuses:
        return DOWN
    if DEGRADED in statuses:
        return DEGRADED
    if UNKNOWN in statuses:
        return UNKNOWN
    return OK


def overall_status(components: list[dict]) -> str:
    rank = 0
    for component in components:
        status = component["status"]
        if status == DOWN and not component["required"]:
            status = DEGRADED
        rank = max(rank, _RANK[status])
    return (OK, DEGRADED, DOWN)[rank]


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"{CHECK_TIMEOUT_SECONDS:g} sn içinde yanıt alınamadı."
    return f"{type(exc).__name__}: {exc}"[:500]


def _percent(used: float | None, total: float | None) -> float | None:
    if not total:
        return None
    return round(100.0 * (used or 0) / total, 1)


# --------------------------------------------------------------------------
# Snapshot of configuration, read once from the request session
# --------------------------------------------------------------------------

@dataclass
class _Snapshot:
    nodes: list[dict] = field(default_factory=list)
    directory: object = None  # DirectoryConfig | Exception | None
    directory_last_sync_at: datetime | None = None
    directory_last_sync: dict | None = None
    genai: object = None  # LiteLLMConfig | Exception | None
    mlflow: dict | None = None
    deployments: dict = field(default_factory=dict)
    registry: object = None  # ModelContainerRegistryConfig | Exception
    https: dict | None = None


async def _load_snapshot(db: AsyncSession) -> _Snapshot:
    from app.auth.ldap import config_from_record as directory_config
    from app.directory_sync import parse_summary
    from app.genai import is_configured as genai_configured
    from app.integrations.litellm import config_from_record as litellm_config
    from app.model_container_registry import effective_model_container_registry_config
    from app.models.directory_settings import DirectorySettings
    from app.models.download_settings import DownloadSettings
    from app.models.genai_settings import GenAiSettings
    from app.models.mlflow_deployment import MlflowDeployment
    from app.models.mlflow_server_settings import MlflowServerSettings
    from app.models.node import Node

    snapshot = _Snapshot()
    for node in (await db.execute(select(Node).order_by(Node.name))).scalars():
        snapshot.nodes.append(
            {
                "id": node.id,
                "name": node.name,
                "hostname": node.hostname or "",
                "enabled": bool(node.enabled),
                "schedulable": bool(node.schedulable),
                "connected": agent_manager.is_connected(node.id),
                "last_seen_at": node.last_seen_at,
                "cpu_percent": node.cpu_percent or 0.0,
                "memory_total_mb": node.memory_total_mb or 0,
                "memory_used_mb": node.memory_used_mb or 0,
                "disk_total_mb": node.disk_total_mb or 0,
                "disk_used_mb": node.disk_used_mb or 0,
                "active_containers_count": node.active_containers_count or 0,
                "agent_version": node.agent_version or "",
                "capabilities": _json_object(node.capabilities_json),
                "reconciliation": _json_object(node.reconciliation_json),
            }
        )

    record = await db.get(DirectorySettings, 1)
    if record is not None and record.enabled:
        try:
            snapshot.directory = directory_config(record)
        except Exception as exc:  # noqa: BLE001 - reported as the component state
            snapshot.directory = exc
        snapshot.directory_last_sync_at = record.last_sync_at
        snapshot.directory_last_sync = parse_summary(record)

    record = await db.get(GenAiSettings, 1)
    if genai_configured(record):
        try:
            snapshot.genai = litellm_config(record)
        except Exception as exc:  # noqa: BLE001
            snapshot.genai = exc

    record = await db.get(MlflowServerSettings, 1)
    if record is not None and record.enabled and record.base_url:
        snapshot.mlflow = {
            "base_url": record.base_url,
            "verify": record.ca_cert_file or record.validate_tls,
            "timeout": min(float(record.timeout_seconds or 10), CHECK_TIMEOUT_SECONDS),
        }
        rows = await db.execute(
            select(MlflowDeployment.status, func.count()).group_by(MlflowDeployment.status)
        )
        snapshot.deployments = {
            (status.value if hasattr(status, "value") else str(status)): count
            for status, count in rows
        }

    try:
        snapshot.registry = await effective_model_container_registry_config(db)
    except Exception as exc:  # noqa: BLE001
        snapshot.registry = exc

    record = await db.get(DownloadSettings, 1)
    if record is not None:
        snapshot.https = {
            "enabled": bool(record.https_enabled),
            "hostname": record.https_hostname,
            "not_after": record.certificate_not_after,
            "subject": record.certificate_subject,
        }
    return snapshot


def _json_object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

async def check_database(db: AsyncSession) -> Component:
    from app.migrations import CURRENT_SCHEMA_VERSION

    component = Component("database", "Veritabanı", required=True)
    url = make_url(settings.DATABASE_URL)
    component.details["backend"] = url.get_backend_name()
    if url.host:
        component.details["host"] = url.host
    started = time.monotonic()
    try:
        if url.host:
            # A pooled connection can keep SELECT 1 working after DNS broke.
            await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(url.host, url.port or 5432),
                timeout=3,
            )
        await asyncio.wait_for(db.execute(text("SELECT 1")), CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        await _rollback(db)
        component.latency_ms = _elapsed_ms(started)
        return component.set(DOWN, "Veritabanına ulaşılamıyor.", _error(exc))
    component.latency_ms = _elapsed_ms(started)

    version = None
    for table in ("aifactory_schema_migrations", "devcloud_schema_migrations"):
        try:
            version = (
                await db.execute(text(f"SELECT MAX(version) FROM {table}"))
            ).scalar_one_or_none()
            break
        except Exception:  # noqa: BLE001 - a missing table reads as "unknown version"
            await _rollback(db)
    component.details["schema_version"] = version
    component.details["expected_schema_version"] = CURRENT_SCHEMA_VERSION
    if version != CURRENT_SCHEMA_VERSION:
        return component.set(
            DEGRADED,
            "Veritabanı şeması güncel değil.",
            f"Şema sürümü {version}, beklenen {CURRENT_SCHEMA_VERSION}.",
        )
    if component.latency_ms > DB_SLOW_MS:
        return component.set(DEGRADED, "Veritabanı yavaş yanıt veriyor.")
    return component.set(OK, "Veritabanı yanıt veriyor.")


async def _rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Controller storage
# --------------------------------------------------------------------------

def _storage_paths() -> list[tuple[str, str, bool]]:
    """``(label, path, critical)`` for every directory the controller writes."""
    paths: list[tuple[str, str, bool]] = []
    url = make_url(settings.DATABASE_URL)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        paths.append(("Veritabanı", str(Path(url.database).resolve().parent), True))
    if settings.UPDATES_ENABLED:
        paths.append(("Güncelleme kuyruğu", settings.UPDATE_QUEUE_ROOT, False))
    paths.append(("Ingress", settings.INGRESS_STAGING_ROOT, False))
    if settings.DOWNLOADS_ENABLED:
        paths.append(("İndirmeler", settings.DOWNLOADS_ROOT, False))
        paths.append(("Paket derleme", settings.DOWNLOAD_BUILD_ROOT, False))
    return paths


def classify_path(label: str, path: str, critical: bool) -> dict:
    entry = {"label": label, "path": path, "critical": critical}
    failed = DOWN if critical else DEGRADED
    target = Path(path)
    note = ""
    if not target.is_dir():
        # The controller creates these on first use; it only has to be able to.
        ancestor = next((p for p in target.parents if p.is_dir()), None)
        if ancestor is None or not os.access(ancestor, os.W_OK | os.X_OK):
            return {**entry, "status": failed, "message": "Dizin yok ve oluşturulamıyor."}
        target, note = ancestor, "Henüz oluşturulmamış."
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return {**entry, "status": DEGRADED, "message": _error(exc)}
    used_percent = _percent(usage.total - usage.free, usage.total) or 0.0
    entry.update(used_percent=used_percent, free_mb=usage.free // (1024 * 1024))
    if not note:
        try:
            with tempfile.NamedTemporaryFile(dir=target, prefix=".aifactory-health-"):
                pass
        except OSError as exc:
            return {**entry, "status": failed, "message": f"Yazılamıyor: {_error(exc)}"}
    if used_percent >= DISK_DOWN_PERCENT:
        return {**entry, "status": DOWN, "message": f"Disk %{used_percent:g} dolu."}
    if used_percent >= DISK_DEGRADED_PERCENT:
        return {**entry, "status": DEGRADED, "message": f"Disk %{used_percent:g} dolu."}
    return {**entry, "status": OK, "message": note}


def check_storage() -> Component:
    component = Component("storage", "Controller depolama", required=True)
    entries = [classify_path(*item) for item in _storage_paths()]
    component.details["paths"] = entries
    status = worst([entry["status"] for entry in entries])
    fullest = max((entry.get("used_percent", 0.0) for entry in entries), default=0.0)
    summary = f"En dolu disk %{fullest:g}."
    problems = [f"{e['label']}: {e['message']}" for e in entries if e["status"] != OK]
    return component.set(status, summary, " ".join(problems) or summary)


# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------

def classify_worker(node: dict, now: datetime, controller_version: str) -> dict:
    """Health of one worker from its last heartbeat and live connection."""
    capabilities = node.get("capabilities") or {}
    reconciliation = node.get("reconciliation") or {}
    last_seen = node.get("last_seen_at")
    age = (now - ensure_utc(last_seen)).total_seconds() if last_seen else None
    memory_percent = _percent(node.get("memory_used_mb"), node.get("memory_total_mb"))
    disk_percent = _percent(node.get("disk_used_mb"), node.get("disk_total_mb"))
    nvidia = (capabilities.get("accelerator_runtime") or {}).get("nvidia") or {}
    accelerators = [d for d in capabilities.get("accelerators") or [] if isinstance(d, dict)]
    self_check = capabilities.get("health") if isinstance(capabilities.get("health"), dict) else None

    result = {
        "id": node["id"],
        "name": node["name"],
        "hostname": node.get("hostname", ""),
        "enabled": node["enabled"],
        "schedulable": node["schedulable"],
        "connected": node["connected"],
        "last_seen_at": ensure_utc(last_seen).isoformat() if last_seen else None,
        "heartbeat_age_seconds": round(age) if age is not None else None,
        "agent_version": node.get("agent_version", ""),
        "cpu_percent": node.get("cpu_percent"),
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
        "active_containers": node.get("active_containers_count", 0),
        "gpu": {
            "status": nvidia.get("status", "not_detected"),
            "devices": len(accelerators),
            "unhealthy": sum(1 for d in accelerators if not d.get("healthy")),
        },
        "podman": (self_check or {}).get("podman"),
        "storage": (self_check or {}).get("storage"),
        "problems": [],
    }
    if not node["enabled"]:
        result.update(status=DISABLED, problems=["Yönetici tarafından devre dışı."])
        return result

    problems: list[tuple[str, str]] = []
    if not node["connected"]:
        problems.append((DOWN, "Controller'a bağlı değil."))
    else:
        if age is None:
            problems.append((DEGRADED, "Henüz heartbeat alınmadı."))
        elif age > HEARTBEAT_DOWN_SECONDS:
            problems.append((DOWN, f"Son heartbeat {round(age)} sn önce."))
        elif age > HEARTBEAT_DEGRADED_SECONDS:
            problems.append((DEGRADED, f"Son heartbeat {round(age)} sn önce."))
        if disk_percent is not None and disk_percent >= DISK_DOWN_PERCENT:
            problems.append((DOWN, f"Disk %{disk_percent:g} dolu."))
        elif disk_percent is not None and disk_percent >= DISK_DEGRADED_PERCENT:
            problems.append((DEGRADED, f"Disk %{disk_percent:g} dolu."))
        if memory_percent is not None and memory_percent >= MEMORY_DEGRADED_PERCENT:
            problems.append((DEGRADED, f"Bellek %{memory_percent:g} dolu."))
        if self_check:
            podman = self_check.get("podman") or {}
            storage = self_check.get("storage") or {}
            if podman.get("ok") is False:
                problems.append((DOWN, f"Podman yanıt vermiyor: {podman.get('message', '')}".strip()))
            if storage.get("ok") is False:
                problems.append((DOWN, f"Workspace deposu yazılamıyor: {storage.get('message', '')}".strip()))
        if reconciliation.get("healthy") is False:
            problems.append(
                (
                    DEGRADED,
                    "Envanter farkı: "
                    f"{len(reconciliation.get('missing') or [])} eksik, "
                    f"{len(reconciliation.get('orphaned') or [])} sahipsiz, "
                    f"{len(reconciliation.get('mismatched') or [])} uyuşmayan container.",
                )
            )
        if nvidia.get("status") == "error":
            problems.append((DEGRADED, f"GPU: {nvidia.get('message') or 'çalışma ortamı hatası'}"))
        elif result["gpu"]["unhealthy"]:
            problems.append((DEGRADED, f"{result['gpu']['unhealthy']} GPU aygıtı kullanılamıyor."))
        upgrade = capabilities.get("upgrade") or {}
        if upgrade.get("state") == "failed":
            problems.append((DEGRADED, "Son worker güncellemesi başarısız."))
        failed_images = [
            item for item in capabilities.get("workspace_image_sync") or []
            if isinstance(item, dict) and item.get("state") == "failed"
        ]
        if failed_images:
            problems.append((DEGRADED, f"{len(failed_images)} imaj senkronize edilemedi."))
        version = node.get("agent_version") or ""
        if version and version != controller_version:
            problems.append((DEGRADED, f"Agent v{version}, controller v{controller_version}."))

    result["status"] = worst([status for status, _ in problems])
    result["problems"] = [message for _, message in problems]
    if not node["schedulable"]:
        result["problems"].append("Bakımda: yeni workspace almıyor.")
    return result


def check_workers(nodes: list[dict], now: datetime) -> Component:
    component = Component("workers", "Worker'lar", required=True)
    workers = [classify_worker(node, now, settings.APP_VERSION) for node in nodes]
    component.details["nodes"] = workers
    enabled = [w for w in workers if w["status"] != DISABLED]
    ready = [w for w in enabled if w["status"] != DOWN and w["schedulable"]]
    summary = f"{len(ready)}/{len(enabled)} worker yeni workspace alabilir."
    problems = " ".join(
        f"{w['name']}: {' '.join(w['problems'])}" for w in enabled if w["status"] != OK
    )
    if not enabled:
        return component.set(DOWN, "Etkin worker yok.")
    if not ready:
        return component.set(DOWN, summary, problems)
    if any(w["status"] != OK for w in enabled):
        return component.set(DEGRADED, summary, problems)
    return component.set(OK, summary)


# --------------------------------------------------------------------------
# Background tasks
# --------------------------------------------------------------------------

def check_tasks() -> Component:
    component = Component("tasks", "Arka plan görevleri", required=True)
    tasks = task_health.snapshot()
    component.details["tasks"] = tasks
    if not tasks:
        return component.set(UNKNOWN, "Arka plan görevleri bu süreçte kayıtlı değil.")
    active = [t for t in tasks if t["status"] != DISABLED]
    failing = [t for t in active if t["status"] != OK]
    status = worst([t["status"] for t in active])
    summary = (
        f"{len(active)} görev çalışıyor."
        if not failing
        else f"{len(failing)}/{len(active)} görevde sorun var."
    )
    message = " ".join(f"{t['label']}: {t['message']}" for t in failing)
    return component.set(status, summary, message or summary)


# --------------------------------------------------------------------------
# Integrations
# --------------------------------------------------------------------------

async def check_directory(snapshot: _Snapshot) -> Component:
    from app.auth.ldap import test_directory_configuration

    # When AD login is on, an unreachable directory locks users out.
    component = Component(
        "directory", "Dizin (LDAP / AD)", required=snapshot.directory is not None
    )
    if snapshot.directory is None:
        return component.set(DISABLED, "Yapılandırılmamış.")
    component.details["last_sync_at"] = (
        ensure_utc(snapshot.directory_last_sync_at).isoformat()
        if snapshot.directory_last_sync_at
        else None
    )
    if snapshot.directory_last_sync:
        component.details["last_sync"] = {
            key: snapshot.directory_last_sync.get(key)
            for key in ("ad_people", "teams", "units", "updated_users", "trigger")
            if key in snapshot.directory_last_sync
        }
    if isinstance(snapshot.directory, Exception):
        return component.set(DOWN, "Dizin ayarları geçersiz.", _error(snapshot.directory))
    component.details["server"] = (
        f"{snapshot.directory.server_host}:{snapshot.directory.server_port}"
    )
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(test_directory_configuration, snapshot.directory),
            CHECK_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        component.latency_ms = _elapsed_ms(started)
        return component.set(DOWN, "Dizin sunucusuna bağlanılamıyor.", _error(exc))
    component.latency_ms = _elapsed_ms(started)
    return component.set(OK, "Bind ve arama başarılı.")


async def check_genai(snapshot: _Snapshot) -> Component:
    from app.integrations.litellm import LiteLLMClient

    component = Component("genai", "GenAI (LiteLLM)", required=False)
    if snapshot.genai is None:
        return component.set(DISABLED, "Yapılandırılmamış.")
    if isinstance(snapshot.genai, Exception):
        return component.set(DOWN, "LiteLLM ayarları geçersiz.", _error(snapshot.genai))
    component.details["base_url"] = snapshot.genai.base_url
    started = time.monotonic()
    try:
        identity = await asyncio.wait_for(
            LiteLLMClient(snapshot.genai).whoami(), CHECK_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        component.latency_ms = _elapsed_ms(started)
        return component.set(DOWN, "LiteLLM'e ulaşılamıyor.", _error(exc))
    component.latency_ms = _elapsed_ms(started)
    component.details["admin_role"] = identity.get("user_role")
    if identity.get("user_role") != "proxy_admin":
        return component.set(
            DEGRADED,
            "LiteLLM yanıt veriyor; yönetici yetkisi eksik.",
            "Kayıtlı anahtarın sahibi proxy_admin rolünde değil; anahtar oluşturma başarısız olur.",
        )
    return component.set(OK, "LiteLLM yanıt veriyor.")


async def check_mlflow(snapshot: _Snapshot) -> Component:
    component = Component("mlflow", "MLflow", required=False)
    if snapshot.mlflow is None:
        return component.set(DISABLED, "Yapılandırılmamış.")
    server = snapshot.mlflow
    counts = snapshot.deployments
    component.details.update(
        base_url=server["base_url"],
        deployments={
            "running": counts.get("running", 0),
            "in_progress": sum(counts.get(state, 0) for state in _ACTIVE_DEPLOYMENT_STATES),
            "failed": counts.get("failed", 0),
        },
    )
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=server["timeout"], verify=server["verify"], follow_redirects=False
        ) as client:
            response = await asyncio.wait_for(
                client.get(server["base_url"].rstrip("/") + "/health"),
                CHECK_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001
        component.latency_ms = _elapsed_ms(started)
        return component.set(DOWN, "MLflow sunucusuna ulaşılamıyor.", _error(exc))
    component.latency_ms = _elapsed_ms(started)
    component.details["http_status"] = response.status_code
    if response.status_code == 200:
        return component.set(OK, "MLflow yanıt veriyor.")
    if response.status_code < 500:
        return component.set(
            DEGRADED, "MLflow beklenmeyen yanıt verdi.", f"/health HTTP {response.status_code}."
        )
    return component.set(DOWN, "MLflow hata veriyor.", f"/health HTTP {response.status_code}.")


async def check_registry(snapshot: _Snapshot) -> Component:
    from app.model_container_registry import test_model_container_registry

    component = Component("registry", "Model container registry", required=False)
    config = snapshot.registry
    if isinstance(config, Exception):
        return component.set(DOWN, "Registry ayarları geçersiz.", _error(config))
    if config is None or not config.enabled or not config.registry_url:
        return component.set(DISABLED, "Yapılandırılmamış.")
    component.details["registry_url"] = config.registry_url
    try:
        result = await asyncio.wait_for(
            test_model_container_registry(config), CHECK_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        return component.set(DOWN, "Registry'ye ulaşılamıyor.", _error(exc))
    component.latency_ms = result.get("latency_ms")
    component.details["http_status"] = result.get("status_code")
    if result.get("ok"):
        return component.set(OK, "Registry yanıt veriyor.")
    if result.get("status_code") is not None and result["status_code"] < 500:
        return component.set(DEGRADED, "Registry erişimi reddedildi.", str(result.get("message")))
    return component.set(DOWN, "Registry'ye ulaşılamıyor.", str(result.get("message")))


# --------------------------------------------------------------------------
# HTTPS ingress and platform update
# --------------------------------------------------------------------------

def check_https(snapshot: _Snapshot, now: datetime) -> Component:
    from app.ingress_settings import IngressManager

    component = Component("https", "HTTPS / TLS", required=False)
    https = snapshot.https
    if not https or not https["enabled"]:
        return component.set(DISABLED, "HTTPS kapalı.")
    component.details.update(hostname=https["hostname"], subject=https["subject"])
    try:
        apply_result = json.loads(IngressManager().result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        apply_result = None
    if isinstance(apply_result, dict) and apply_result.get("success") is False:
        component.details["last_apply_error"] = str(apply_result.get("message") or "")[:500]

    try:
        not_after = ensure_utc(datetime.fromisoformat(str(https["not_after"])))
    except (TypeError, ValueError):
        return component.set(DEGRADED, "Sertifika bilgisi okunamadı.")
    days = (not_after - now).days
    component.details.update(not_after=not_after.isoformat(), days_left=days)
    if not_after <= now:
        return component.set(DOWN, "Sertifikanın süresi dolmuş.")
    if "last_apply_error" in component.details:
        return component.set(
            DEGRADED,
            "Son Nginx yapılandırması uygulanamadı.",
            component.details["last_apply_error"],
        )
    if days < CERT_WARN_DAYS:
        return component.set(DEGRADED, f"Sertifika {days} gün içinde sona eriyor.")
    return component.set(OK, f"Sertifika {days} gün daha geçerli.")


def check_update(now: datetime) -> Component:
    from app.update_queue import read_update_status

    component = Component("update", "Platform güncelleme", required=False)
    if not settings.UPDATES_ENABLED:
        return component.set(DISABLED, "Güncellemeler kapalı.")
    status = read_update_status()
    state = str(status.get("state") or "unknown")
    component.details.update(
        state=state,
        version=settings.APP_VERSION,
        target_version=status.get("target_version"),
        finished_at=status.get("finished_at"),
    )
    if state in {"queued", "running"}:
        return component.set(OK, "Güncelleme sürüyor.")
    if state == "unknown":
        return component.set(DEGRADED, "Güncelleme durumu okunamadı.", str(status.get("error") or ""))
    if state == "failed":
        try:
            finished = ensure_utc(datetime.fromisoformat(str(status.get("finished_at"))))
            recent = (now - finished).days < UPDATE_FAILURE_WINDOW_DAYS
        except (TypeError, ValueError):
            recent = True
        message = str(status.get("error") or "Son güncelleme başarısız.")[:500]
        if recent:
            return component.set(DEGRADED, "Son güncelleme başarısız oldu.", message)
        return component.set(OK, f"v{settings.APP_VERSION} çalışıyor.", message)
    return component.set(OK, f"v{settings.APP_VERSION} çalışıyor.")


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

_LABELS = {
    "database": ("Veritabanı", True),
    "storage": ("Controller depolama", True),
    "workers": ("Worker'lar", True),
    "tasks": ("Arka plan görevleri", True),
    "directory": ("Dizin (LDAP / AD)", False),
    "genai": ("GenAI (LiteLLM)", False),
    "mlflow": ("MLflow", False),
    "registry": ("Model container registry", False),
    "https": ("HTTPS / TLS", False),
    "update": ("Platform güncelleme", False),
}


def _unknown(key: str, summary: str, message: str = "") -> Component:
    label, required = _LABELS[key]
    return Component(key, label, required).set(UNKNOWN, summary, message)


async def _guarded(key: str, check: Awaitable[Component]) -> Component:
    """A check that raises unexpectedly reports itself instead of failing the page."""
    try:
        return await check
    except Exception as exc:  # noqa: BLE001
        logger.exception("Health check %s failed", key)
        return _unknown(key, "Kontrol çalıştırılamadı.", _error(exc))


def _guarded_sync(key: str, function, *args) -> Component:
    try:
        return function(*args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Health check %s failed", key)
        return _unknown(key, "Kontrol çalıştırılamadı.", _error(exc))


async def build_report(db: AsyncSession) -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    database = await _guarded("database", check_database(db))

    snapshot: _Snapshot | None = None
    if database.status != DOWN:
        try:
            snapshot = await _load_snapshot(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Health snapshot failed: %s", _error(exc))
            await _rollback(db)
    # Do not hold a pooled connection during network checks.
    await release_read_only_connection(db)

    storage_check = _guarded("storage", asyncio.to_thread(check_storage))
    if snapshot is None:
        storage = await storage_check
        reason = "Veritabanı okunamadığı için değerlendirilemedi."
        workers, directory, genai, mlflow, registry, https = (
            _unknown(key, reason)
            for key in ("workers", "directory", "genai", "mlflow", "registry", "https")
        )
    else:
        storage, directory, genai, mlflow, registry = await asyncio.gather(
            storage_check,
            _guarded("directory", check_directory(snapshot)),
            _guarded("genai", check_genai(snapshot)),
            _guarded("mlflow", check_mlflow(snapshot)),
            _guarded("registry", check_registry(snapshot)),
        )
        workers = _guarded_sync("workers", check_workers, snapshot.nodes, now)
        https = _guarded_sync("https", check_https, snapshot, now)

    components = [
        database,
        storage,
        workers,
        _guarded_sync("tasks", check_tasks),
        directory,
        genai,
        mlflow,
        registry,
        https,
        _guarded_sync("update", check_update, now),
    ]
    rows = [asdict(component) for component in components]
    return {
        "status": overall_status(rows),
        "version": settings.APP_VERSION,
        "checked_at": now.isoformat(),
        "duration_ms": _elapsed_ms(started),
        "components": rows,
    }


def public_view(report: dict) -> dict:
    """Statuses and safe summaries only: no hosts, URLs, paths or raw errors."""
    return {
        "status": report["status"],
        "version": report["version"],
        "checked_at": report["checked_at"],
        "components": [
            {
                key: component[key]
                for key in ("key", "label", "required", "status", "summary", "latency_ms")
            }
            for component in report["components"]
            if component["status"] != DISABLED
        ],
    }


_cache: tuple[float, dict] | None = None
_lock = asyncio.Lock()


async def get_report(db: AsyncSession, *, refresh: bool = False) -> dict:
    global _cache
    async with _lock:
        if not refresh and _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        report = await build_report(db)
        _cache = (time.monotonic(), report)
        return report


def reset_cache() -> None:
    global _cache
    _cache = None
