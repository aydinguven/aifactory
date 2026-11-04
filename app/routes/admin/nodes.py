"""Admin API: worker enrollment, lifecycle, telemetry and upgrades."""

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.node import Node, NodeStatus
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.agents.manager import agent_manager
from app.models.placement_tag import TAG_KIND_CUSTOM, PlacementTag
from app.placement import (
    PlacementRequestError,
    encode_tags,
    is_gpu_worker,
    valid_tag_name,
    validate_node_placement,
)
from app.schemas.node import (
    NodeCreate,
    NodeCreated,
    NodeLabelsUpdate,
    NodeOut,
    NodePlacementUpdate,
    NodeUpdate,
    PlacementTagCreate,
    PlacementTagOut,
    PlacementTagUpdate,
)
from app.schemas.worker_bootstrap import WorkerBootstrapTicketCreated
from app.release_catalog import semantic_version
from app.worker_bootstrap import (
    WORKER_BOOTSTRAP_TTL_SECONDS,
    bootstrap_install_command,
    current_platform_release,
    new_ticket_token,
    require_https_controller_url,
    ticket_hash,
    worker_bootstrap_transport,
)

router = APIRouter()


def _node_out(node: Node, enrollment_token: str | None = None):
    values = dict(
        id=node.id,
        name=node.name,
        hostname=node.hostname,
        enabled=node.enabled,
        schedulable=node.schedulable,
        status=node.status,
        cpu_total=node.cpu_total,
        memory_total_mb=node.memory_total_mb,
        disk_total_mb=node.disk_total_mb,
        cpu_percent=node.cpu_percent,
        memory_used_mb=node.memory_used_mb,
        disk_used_mb=node.disk_used_mb,
        active_containers_count=node.active_containers_count,
        gpu_slots_per_device=node.gpu_slots_per_device,
        labels=json.loads(node.labels_json or "{}"),
        tags=node.tags,
        gpu_worker=is_gpu_worker(node),
        capabilities=json.loads(node.capabilities_json or "{}"),
        inventory=json.loads(node.inventory_json or "[]"),
        reconciliation=json.loads(node.reconciliation_json or "{}"),
        agent_version=node.agent_version,
        last_seen_at=node.last_seen_at,
        created_at=node.created_at,
        connected=agent_manager.is_connected(node.id),
    )
    if enrollment_token is not None:
        return NodeCreated(**values, enrollment_token=enrollment_token)
    return NodeOut(**values)


@router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return [_node_out(node) for node in nodes]


@router.post(
    "/worker-bootstrap-tickets",
    response_model=WorkerBootstrapTicketCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_worker_bootstrap_ticket(
    request: Request,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a short-lived command that may enroll exactly one worker."""
    current_platform_release()
    transport = await worker_bootstrap_transport(request, db)
    require_https_controller_url(request, transport.controller_url)
    token = new_ticket_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=WORKER_BOOTSTRAP_TTL_SECONDS
    )
    db.add(
        WorkerBootstrapTicket(
            token_hash=ticket_hash(token),
            created_by_user_id=str(_admin.id),
            expires_at=expires_at,
        )
    )
    await db.commit()
    install_url = (
        f"{transport.controller_url}/api/bootstrap/workers/{token}/install.sh"
    )
    return WorkerBootstrapTicketCreated(
        install_url=install_url,
        command=bootstrap_install_command(install_url, transport),
        expires_at=expires_at,
    )


@router.post("/nodes", response_model=NodeCreated, status_code=status.HTTP_201_CREATED)
async def create_node(
    data: NodeCreate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    existing = (await db.execute(select(Node).where(Node.name == data.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Bu isimde bir worker zaten var.")
    token = secrets.token_urlsafe(32)
    node = Node(
        name=data.name,
        schedulable=data.schedulable,
        labels_json=json.dumps(data.labels, ensure_ascii=False),
        agent_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        status=NodeStatus.PENDING,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return _node_out(node, token)


@router.patch("/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    data: NodeUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    values = data.model_dump(exclude_unset=True)
    labels = values.pop("labels", None)
    for field_name, value in values.items():
        setattr(node, field_name, value)
    if labels is not None:
        node.labels_json = json.dumps(labels, ensure_ascii=False)
    if not node.schedulable and node.status == NodeStatus.ONLINE:
        node.status = NodeStatus.DRAINING
    elif node.schedulable and agent_manager.is_connected(node.id):
        node.status = NodeStatus.ONLINE
    db.add(node)
    await db.commit()
    await db.refresh(node)
    if not node.enabled:
        await agent_manager.disconnect(node.id, "Worker yönetici tarafından devre dışı bırakıldı")
    return _node_out(node)


@router.post("/nodes/{node_id}/rotate-token", response_model=NodeCreated)
async def rotate_node_token(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    token = secrets.token_urlsafe(32)
    node.agent_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    await agent_manager.disconnect(node.id, "Worker enrollment token'ı yenilendi")
    return _node_out(node, token)


@router.delete("/nodes/{node_id}")
async def delete_node(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Delete a worker node from the cluster."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")

    assigned_workspaces = (
        await db.execute(
            select(func.count(Workspace.id)).where(
                Workspace.node_id == node_id,
            )
        )
    ).scalar_one()
    if assigned_workspaces > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bu worker'a atanmış {assigned_workspaces} çalışma alanı var. "
                "Worker'ı silmeden önce çalışma alanlarını başka bir worker'a taşıyın veya silin."
            ),
        )

    await agent_manager.disconnect(node.id, "Worker sistemden silindi")
    await db.delete(node)
    await db.commit()
    return {"message": f"Worker '{node.name}' başarıyla silindi."}


@router.get("/nodes/events-stream")
async def node_events_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Server-Sent Events stream for real-time node telemetry and connection events."""
    from fastapi.responses import StreamingResponse
    queue = agent_manager.subscribe_events()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'init', 'data': {'message': 'connected'}})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            agent_manager.unsubscribe_events(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.put("/nodes/{node_id}/labels", response_model=NodeOut)
async def update_node_labels(
    node_id: str,
    data: NodeLabelsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Update label annotations for a worker node."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    node.labels_json = json.dumps(data.labels, ensure_ascii=False)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    await agent_manager.broadcast_event(
        "node.updated", {"node_id": node.id, "labels": data.labels}
    )
    return _node_out(node)


@router.put("/nodes/{node_id}/placement", response_model=NodeOut)
async def update_node_placement(
    node_id: str,
    data: NodePlacementUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Assign a worker's placement tags.

    Only new placements are affected; workspaces already on the worker stay.
    """
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    try:
        tags = await validate_node_placement(db, data.tags)
    except PlacementRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    node.tags_json = encode_tags(tags)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    await agent_manager.broadcast_event(
        "node.updated", {"node_id": node.id, "tags": tags}
    )
    return _node_out(node)


async def _tag_worker_counts(db: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in (await db.execute(select(Node))).scalars().all():
        for name in set(node.tags):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _tag_out(tag: PlacementTag, counts: dict[str, int]) -> PlacementTagOut:
    return PlacementTagOut(
        name=tag.name,
        description=tag.description,
        enabled=tag.enabled,
        worker_count=counts.get(tag.name, 0),
    )


@router.get("/placement-tags", response_model=list[PlacementTagOut])
async def list_placement_tags(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    tags = (
        await db.execute(select(PlacementTag).order_by(PlacementTag.name))
    ).scalars().all()
    counts = await _tag_worker_counts(db)
    return [_tag_out(tag, counts) for tag in tags]


@router.post(
    "/placement-tags",
    response_model=PlacementTagOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_placement_tag(
    data: PlacementTagCreate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    name = data.name.strip()
    if not valid_tag_name(name):
        raise HTTPException(
            status_code=400,
            detail="Bu ad ayrılmış veya geçersiz (GPU ve CPU kullanılamaz).",
        )
    existing = (
        await db.execute(
            select(PlacementTag).where(func.lower(PlacementTag.name) == name.lower())
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"'{existing.name}' zaten tanımlı.")
    tag = PlacementTag(
        name=name, kind=TAG_KIND_CUSTOM, description=data.description.strip()
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return _tag_out(tag, {})


@router.patch("/placement-tags/{name}", response_model=PlacementTagOut)
async def update_placement_tag(
    name: str,
    data: PlacementTagUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    tag = await db.get(PlacementTag, name)
    if not tag:
        raise HTTPException(status_code=404, detail="Etiket bulunamadı.")
    if data.description is not None:
        tag.description = data.description.strip()
    if data.enabled is not None:
        tag.enabled = data.enabled
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return _tag_out(tag, await _tag_worker_counts(db))


@router.delete("/placement-tags/{name}")
async def delete_placement_tag(
    name: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete an unused tag; workspaces keep the value they were created with."""
    tag = await db.get(PlacementTag, name)
    if not tag:
        raise HTTPException(status_code=404, detail="Etiket bulunamadı.")
    in_use = (await _tag_worker_counts(db)).get(tag.name, 0)
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bu etiket {in_use} worker'a atanmış. Önce worker'lardan kaldırın "
                "veya silmek yerine devre dışı bırakın."
            ),
        )
    await db.delete(tag)
    await db.commit()
    return {"message": f"'{name}' silindi."}


@router.post("/nodes/{node_id}/upgrade")
async def upgrade_node(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Trigger remote OTA upgrade for a connected worker."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    if not agent_manager.is_connected(node.id):
        raise HTTPException(
            status_code=400, detail="Worker çevrimdışı; yükseltme komutu gönderilemez."
        )
    release = current_platform_release()
    current_semantic = semantic_version(node.agent_version)
    target_semantic = semantic_version(release.version)
    if node.agent_version == release.version:
        return {
            "message": f"Worker '{node.name}' zaten v{release.version} sürümünde.",
            "detail": {
                "status": "already_current",
                "current_version": node.agent_version,
                "target_version": release.version,
                "message": "İndirme veya güncelleme kuyruğu oluşturulmadı.",
            },
        }
    if (
        current_semantic is not None
        and target_semantic is not None
        and target_semantic < current_semantic
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Yayımlanan worker release v{release.version}, kurulu "
                f"v{node.agent_version} sürümünden eski. Sürüm düşürme engellendi."
            ),
        )
    connection = agent_manager.get(node.id)
    try:
        resp = await connection.request("system.upgrade", {}, timeout=15)
        return {
            "message": f"Worker '{node.name}' yükseltme işlemi başlatıldı.",
            "detail": {**resp, "target_version": release.version},
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Yükseltme komutu başarısız oldu: {exc}"
        )


@router.get("/nodes/{node_id}/upgrade-check")
async def check_node_upgrade(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Compare the installed worker agent with the published release."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    release = current_platform_release()
    current_semantic = semantic_version(node.agent_version)
    target_semantic = semantic_version(release.version)
    update_available = node.agent_version != release.version
    if (
        current_semantic is not None
        and target_semantic is not None
        and target_semantic <= current_semantic
    ):
        update_available = False
    return {
        "node_id": node.id,
        "node_name": node.name,
        "connected": agent_manager.is_connected(node.id),
        "installed_version": node.agent_version or "bilinmiyor",
        "published_version": release.version,
        "update_available": update_available,
    }
