import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import agent_manager
from app.database import get_db
from app.models.node import Node, NodeStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_image import WorkspaceImage
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.orchestrator.templates import requires_accelerator
from app.schemas.node import NodeHeartbeat
from app.schemas.jupyter_ai_settings import WorkerJupyterAiSettings
from app.jupyter_ai import parse_model_catalog
from app.config import settings
from app.release_catalog import RELEASE_PATTERN, latest_release
from app.security.secrets import SecretDecryptionError, decrypt_secret
from app.workspace_image_service import image_archive_path

agent_router = APIRouter(prefix="/api/agent", tags=["Worker Agent"])


def normalize_worker_capabilities(
    capabilities: dict,
    agent_version: str,
) -> dict:
    """Close stale failed OTA state once the worker reports the target version."""
    normalized = dict(capabilities)
    raw_upgrade = normalized.get("upgrade")
    if not isinstance(raw_upgrade, dict):
        return normalized
    upgrade = dict(raw_upgrade)
    if (
        upgrade.get("state") == "failed"
        and agent_version
        and str(upgrade.get("target_version") or "") == agent_version
    ):
        upgrade.update(
            {
                "state": "succeeded",
                "message": (
                    f"Worker hedef sürümü çalıştırıyor (v{agent_version}); "
                    "önceki aynı-sürüm hata kaydı kapatıldı."
                ),
            }
        )
    normalized["upgrade"] = upgrade
    return normalized


async def reconcile_worker_inventory(
    db: AsyncSession,
    node: Node,
    inventory: list[dict],
) -> dict:
    """Compare worker truth with controller assignments without deleting data."""
    assigned = (
        await db.execute(select(Workspace).where(Workspace.node_id == node.id))
    ).scalars().all()
    expected = {workspace.container_name: workspace for workspace in assigned}
    actual = {
        str(item.get("container_name") or ""): item
        for item in inventory
        if item.get("container_name")
    }
    missing = sorted(set(expected) - set(actual))
    orphaned = sorted(set(actual) - set(expected))
    mismatched = sorted(
        name
        for name in set(expected) & set(actual)
        if str(actual[name].get("workspace_id") or "") != expected[name].id
        or int(actual[name].get("host_port") or -1) != expected[name].host_port
    )
    # A missing container is recoverable by the normal start action. Surface
    # the drift immediately, but do not delete worker data or rewrite placement.
    for name in missing:
        workspace = expected[name]
        if workspace.status == WorkspaceStatus.RUNNING:
            workspace.status = WorkspaceStatus.ERROR
            workspace.error_message = (
                "Worker reconciliation did not find the assigned container; "
                "start the workspace to recreate it from persistent storage."
            )
            db.add(workspace)
    return {
        "expected": len(expected),
        "actual": len(actual),
        "missing": missing,
        "orphaned": orphaned,
        "mismatched": mismatched,
        "healthy": not (missing or orphaned or mismatched),
    }


def _bearer_token(connection: Request | WebSocket) -> str:
    value = connection.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" else ""


async def _authenticated_node(
    request: Request,
    node_id: str,
    db: AsyncSession,
) -> Node:
    node = await db.get(Node, node_id)
    token_hash = hashlib.sha256(_bearer_token(request).encode("utf-8")).hexdigest()
    if (
        not node
        or not node.enabled
        or not secrets.compare_digest(node.agent_token_hash, token_hash)
    ):
        raise HTTPException(status_code=403, detail="Geçersiz worker kimliği")
    return node


def _reports_nvidia_stack(node: Node) -> bool:
    """True once the worker's heartbeat reported NVIDIA hardware.

    A detected but unhealthy stack ("error") still counts, so a transient
    driver or CDI fault does not make the worker drop and re-download its
    GPU images.
    """
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
        status = capabilities["accelerator_runtime"]["nvidia"]["status"]
    except (ValueError, TypeError, KeyError):
        return False
    return status in {"ready", "error"}


@agent_router.get("/check")
async def worker_enrollment_check(
    request: Request,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Confirm both enrollment credentials and the live worker tunnel."""
    node = await _authenticated_node(request, node_id, db)
    return {
        "node_id": node.id,
        "accepted": True,
        "connected": agent_manager.is_connected(node.id),
    }


@agent_router.get("/releases/latest")
async def worker_latest_release(
    request: Request,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return release metadata only to an enrolled worker."""
    await _authenticated_node(request, node_id, db)
    release = latest_release(Path(settings.DOWNLOADS_ROOT))
    if release is None:
        raise HTTPException(status_code=404, detail="Yayımlanmış release bulunamadı")
    base = str(request.base_url).rstrip("/")
    return {
        "version": release.version,
        "filename": release.path.name,
        "url": (
            f"{base}/api/agent/releases/{release.path.name}"
            f"?node_id={node_id}"
        ),
        "sha256": release.sha256,
        "size": release.size,
        "allow_unsigned": settings.WORKER_OTA_ALLOW_UNSIGNED,
    }


@agent_router.api_route("/releases/{filename}", methods=["GET", "HEAD"])
async def worker_download_release(
    filename: str,
    request: Request,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Stream an allow-listed release only to an enrolled worker."""
    await _authenticated_node(request, node_id, db)
    if not RELEASE_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Release bulunamadı")
    root = (Path(settings.DOWNLOADS_ROOT) / "releases").resolve()
    candidate = root / filename
    path = candidate.resolve()
    if candidate.is_symlink() or path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Release bulunamadı")
    return FileResponse(
        path,
        media_type=(
            "application/gzip" if path.name.endswith(".tar.gz") else "application/zip"
        ),
        filename=filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@agent_router.get("/images/catalog")
async def worker_image_catalog(
    request: Request,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the enabled, digest-pinned workspace image set to an enrolled worker."""
    node = await _authenticated_node(request, node_id, db)
    # Disabling an image only withdraws it from new workspaces: a worker keeps
    # every image that one of its existing workspaces is pinned to.
    pinned_ids = set(
        (
            await db.execute(
                select(Workspace.image_id).where(
                    Workspace.node_id == node.id,
                    Workspace.image_id.is_not(None),
                )
            )
        ).scalars()
    )
    records = (
        await db.execute(
            select(WorkspaceImage)
            .where(
                or_(
                    WorkspaceImage.enabled.is_(True),
                    WorkspaceImage.id.in_(pinned_ids),
                )
            )
            .order_by(WorkspaceImage.template_id, WorkspaceImage.created_at.desc())
        )
    ).scalars().all()
    # GPU-only images are several GB; keep them off CPU workers.
    if not _reports_nvidia_stack(node):
        records = [
            record
            for record in records
            if record.id in pinned_ids or not requires_accelerator(record.template_id)
        ]
    return {
        "images": [
            {
                "id": record.id,
                "template_id": record.template_id,
                "image_ref": record.image_ref,
                "digest": record.digest,
                "sha256": record.sha256,
                "size": record.size,
                "url": f"/api/agent/images/{record.id}/archive?node_id={node_id}",
            }
            for record in records
        ]
    }


@agent_router.get(
    "/jupyter-ai-settings",
    response_model=WorkerJupyterAiSettings,
)
async def worker_jupyter_ai_settings(
    request: Request,
    response: Response,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return decrypted shared settings only to an enrolled, enabled worker."""
    await _authenticated_node(request, node_id, db)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    record = await db.get(JupyterAiSettings, 1)
    if record is None:
        # Absence means centrally unmanaged. Workers retain legacy worker.env
        # values during a rolling upgrade.
        return WorkerJupyterAiSettings(
            managed=False, enabled=False, cline_enabled=True
        )
    if not record.enabled:
        return WorkerJupyterAiSettings(
            managed=True,
            enabled=False,
            cline_enabled=True,
            updated_at=record.updated_at,
        )
    try:
        shared_token = decrypt_secret(record.encrypted_shared_token)
    except SecretDecryptionError as exc:
        raise HTTPException(
            status_code=500,
            detail="Jupyter AI ortak tokenı çözülemedi",
        ) from exc
    return WorkerJupyterAiSettings(
        managed=True,
        enabled=True,
        cline_enabled=True,
        gateway_url=record.gateway_url,
        model_id=record.model_id,
        gateway_model_discovery=record.gateway_model_discovery,
        models=parse_model_catalog(record.model_catalog_json, record.model_id),
        shared_token=shared_token,
        updated_at=record.updated_at,
    )


@agent_router.api_route("/images/{image_id}/archive", methods=["GET", "HEAD"])
async def worker_download_image(
    image_id: str,
    request: Request,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Stream one enabled OCI archive only to an enrolled worker."""
    await _authenticated_node(request, node_id, db)
    record = await db.get(WorkspaceImage, image_id)
    if not record or not record.enabled:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    try:
        path = image_archive_path(record.filename)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı") from exc
    if path.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    return FileResponse(
        path,
        media_type="application/vnd.oci.image.archive.v1+tar",
        filename=f"{record.template_id}.tar",
        headers={
            "X-Content-Type-Options": "nosniff",
            "X-AIFactory-SHA256": record.sha256,
        },
    )


@agent_router.websocket("/connect/{node_id}")
async def connect_agent(
    websocket: WebSocket,
    node_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Accept the worker-initiated, persistent control/data tunnel."""
    node = await db.get(Node, node_id)
    token_hash = hashlib.sha256(_bearer_token(websocket).encode("utf-8")).hexdigest()
    if not node or not node.enabled or not secrets.compare_digest(node.agent_token_hash, token_hash):
        await websocket.close(code=4403, reason="Geçersiz worker kimliği")
        return

    await websocket.accept()
    connection = await agent_manager.register(node.id, websocket)
    node.status = NodeStatus.ONLINE
    node.last_seen_at = datetime.now(timezone.utc)
    db.add(node)
    await db.commit()
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "heartbeat":
                heartbeat = NodeHeartbeat.model_validate(message.get("payload") or {})
                # Admin changes happen in another request/session. Refresh the
                # policy fields before applying heartbeat data so a stale,
                # long-lived agent session cannot undo drain/disable decisions.
                await db.refresh(node, attribute_names=["enabled", "schedulable"])
                if not node.enabled:
                    await websocket.close(
                        code=4403,
                        reason="Worker yönetici tarafından devre dışı bırakıldı",
                    )
                    break
                node.hostname = heartbeat.hostname
                node.cpu_total = heartbeat.cpu_total
                node.memory_total_mb = heartbeat.memory_total_mb
                node.disk_total_mb = heartbeat.disk_total_mb
                node.cpu_percent = heartbeat.cpu_percent
                node.memory_used_mb = heartbeat.memory_used_mb
                node.disk_used_mb = heartbeat.disk_used_mb
                node.active_containers_count = heartbeat.active_containers_count
                capabilities = normalize_worker_capabilities(
                    heartbeat.capabilities,
                    heartbeat.agent_version,
                )
                node.capabilities_json = json.dumps(capabilities, ensure_ascii=False)
                inventory = [
                    item.model_dump() for item in heartbeat.inventory
                ]
                node.inventory_json = json.dumps(inventory, ensure_ascii=False)
                reconciliation = await reconcile_worker_inventory(db, node, inventory)
                node.reconciliation_json = json.dumps(
                    reconciliation, ensure_ascii=False
                )
                node.last_reconciled_at = datetime.now(timezone.utc)
                node.agent_version = heartbeat.agent_version
                node.status = NodeStatus.DRAINING if not node.schedulable else NodeStatus.ONLINE
                node.last_seen_at = datetime.now(timezone.utc)
                db.add(node)
                await db.commit()
                await agent_manager.broadcast_event(
                    "node.telemetry",
                    {
                        "node_id": node.id,
                        "status": node.status.value,
                        "cpu_total": node.cpu_total,
                        "memory_total_mb": node.memory_total_mb,
                        "disk_total_mb": node.disk_total_mb,
                        "cpu_percent": node.cpu_percent,
                        "memory_used_mb": node.memory_used_mb,
                        "disk_used_mb": node.disk_used_mb,
                        "active_containers_count": node.active_containers_count,
                        "agent_version": node.agent_version,
                        "accelerators": capabilities.get("accelerators", []),
                        "accelerator_runtime": capabilities.get(
                            "accelerator_runtime", {}
                        ),
                        "upgrade_status": capabilities.get("upgrade", {}),
                        "last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None,
                    },
                )
            else:
                await connection.handle_message(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        disconnected = await agent_manager.unregister(node.id, connection)
        if disconnected:
            fresh_node = await db.get(Node, node.id)
            if fresh_node:
                fresh_node.status = NodeStatus.OFFLINE
                db.add(fresh_node)
                await db.commit()
