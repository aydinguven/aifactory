"""Admin API: resource flavors, custom templates and worker catalog sync."""

import re
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.node import Node
from app.models.workspace_image import WorkspaceImage
from app.models.custom_template import CustomTemplate
from app.models.flavor_settings import FlavorSettings
from app.models.template_settings import TemplateSettings
from app.agents.manager import agent_manager
from app.schemas.workspace import FlavorInfo
from app.schemas.flavor_settings import FlavorCreate, FlavorSettingsUpdate
from app.schemas.custom_template import CustomTemplateUpdate
from app.config import settings
from app.orchestrator.templates import (
    BUILTIN_TEMPLATE_IDS,
    TEMPLATES,
    register_custom_template,
)
from app.orchestrator.flavors import FLAVORS, get_flavor
from app.workspace_catalog import configured_flavors, resolve_flavor
from app.workspace_image_service import (
    image_archive_path,
    import_registry_image,
)
from app.routes.admin.images import register_workspace_image

router = APIRouter()


@router.get("/flavors", response_model=list[FlavorInfo])
async def list_admin_flavors(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await configured_flavors(db)


@router.patch("/flavors/{flavor_id}", response_model=FlavorInfo)
async def update_flavor_settings(
    flavor_id: str,
    payload: FlavorSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    flavor = await resolve_flavor(db, flavor_id)
    if not flavor or not flavor.selectable:
        raise HTTPException(status_code=404, detail="Kaynak profili bulunamadı")
    record = await db.get(FlavorSettings, flavor_id)
    if record is None:
        record = FlavorSettings(flavor_id=flavor_id, enabled=True)
    values = payload.model_dump(exclude_unset=True)
    for field_name, value in values.items():
        setattr(record, field_name, value)
    db.add(record)
    await db.commit()
    flavors = await configured_flavors(db)
    return next(item for item in flavors if item.id == flavor_id)


@router.post("/flavors", response_model=FlavorInfo, status_code=status.HTTP_201_CREATED)
async def create_flavor(
    payload: FlavorCreate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if get_flavor(payload.id) or await db.get(FlavorSettings, payload.id):
        raise HTTPException(status_code=409, detail="Bu kaynak profili ID zaten kullanılıyor")
    if payload.accelerator_count and not payload.accelerator_vendor.strip():
        raise HTTPException(status_code=422, detail="GPU profili için hızlandırıcı üreticisi gereklidir")
    record = FlavorSettings(
        flavor_id=payload.id,
        is_custom=True,
        **payload.model_dump(exclude={"id"}),
    )
    db.add(record)
    await db.commit()
    flavors = await configured_flavors(db)
    return next(item for item in flavors if item.id == payload.id)


@router.delete("/flavors/{flavor_id}")
async def delete_flavor(
    flavor_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(FlavorSettings, flavor_id)
    if not record or not record.is_custom:
        raise HTTPException(status_code=400, detail="Yalnızca sonradan eklenen profiller silinebilir")
    in_use = (
        await db.execute(
            select(func.count(Workspace.id)).where(
                Workspace.flavor_id == flavor_id,
                Workspace.status != WorkspaceStatus.DELETED,
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(status_code=409, detail="Bu profil aktif workspace kayıtları tarafından kullanılıyor")
    await db.delete(record)
    await db.commit()
    FLAVORS.pop(flavor_id, None)
    return {"deleted": True, "flavor_id": flavor_id}


@router.post("/catalog/sync")
async def sync_workspace_catalog_to_workers(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    flavors = await configured_flavors(db)
    custom_templates = (await db.execute(select(CustomTemplate))).scalars().all()
    payload = {
        "flavors": [item.model_dump() for item in flavors],
        "templates": [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "category": item.category,
                "icon": item.icon,
                "image_tag": item.image_tag,
                "default_port": item.default_port,
                "ide_type": item.ide_type,
            }
            for item in custom_templates
        ],
    }
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    results = []
    for node in nodes:
        if not agent_manager.is_connected(node.id):
            results.append({"node_id": node.id, "node_name": node.name, "success": False, "message": "Worker çevrimdışı"})
            continue
        try:
            result = await agent_manager.get(node.id).request("system.catalog_sync", payload)
            results.append({"node_id": node.id, "node_name": node.name, "success": True, "message": result.get("message", "Katalog güncellendi")})
        except Exception as exc:
            results.append({"node_id": node.id, "node_name": node.name, "success": False, "message": str(exc)[:500]})
    return {"workers": results, "success_count": sum(1 for item in results if item["success"]), "total_count": len(results)}


@router.get("/templates")
async def list_custom_templates(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all custom database templates."""
    from app.models.custom_template import CustomTemplate
    stmt = select(CustomTemplate).order_by(CustomTemplate.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.delete("/templates/{template_id}")
async def delete_custom_template(
    template_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Delete a custom template from database."""
    from app.models.custom_template import CustomTemplate
    stmt = select(CustomTemplate).where(CustomTemplate.id == template_id)
    res = await db.execute(stmt)
    tpl = res.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found.")
    in_use = (
        await db.execute(
            select(func.count(Workspace.id)).where(
                Workspace.template_id == template_id,
                Workspace.status != WorkspaceStatus.DELETED,
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(status_code=409, detail="Bu şablon aktif workspace kayıtları tarafından kullanılıyor.")
    images = (
        await db.execute(select(WorkspaceImage).where(WorkspaceImage.template_id == template_id))
    ).scalars().all()
    archive_paths = [image_archive_path(item.filename) for item in images]
    for image in images:
        await db.delete(image)
    await db.delete(tpl)
    await db.commit()
    for archive_path in archive_paths:
        archive_path.unlink(missing_ok=True)
    TEMPLATES.pop(template_id, None)
    return {"message": f"Template {template_id} deleted successfully."}


@router.patch("/templates/{template_id}")
async def update_custom_template(
    template_id: str,
    payload: CustomTemplateUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    template = await db.get(CustomTemplate, template_id)
    values = payload.model_dump(exclude_unset=True)
    if template:
        for field_name, value in values.items():
            setattr(template, field_name, value)
        db.add(template)
        await db.commit()
        await db.refresh(template)
        register_custom_template(
            template_id=template.id,
            name=template.name,
            description=template.description,
            category=template.category,
            image_tag=template.image_tag,
            default_port=template.default_port,
            ide_type=template.ide_type,
            icon=template.icon,
        )
        return template
    if template_id not in BUILTIN_TEMPLATE_IDS:
        raise HTTPException(status_code=404, detail="Workspace tipi bulunamadı.")
    if "default_port" in values:
        raise HTTPException(status_code=422, detail="Yerleşik workspace tipinin runtime portu değiştirilemez.")
    record = await db.get(TemplateSettings, template_id)
    if record is None:
        record = TemplateSettings(template_id=template_id)
    for field_name in ("name", "description", "category"):
        if field_name in values:
            setattr(record, field_name, values[field_name])
    db.add(record)
    await db.commit()
    base = TEMPLATES[template_id]
    return {
        **base.to_schema().model_dump(),
        "name": record.name or base.name,
        "description": record.description or base.description,
        "category": record.category or base.category,
    }


@router.post("/templates/build-stream")
async def build_custom_template_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()],
    template_id: Annotated[str, Form()],
    containerfile: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "Custom",
    default_port: Annotated[int, Form()] = 8080,
    ide_type: Annotated[str, Form()] = "vscode",
):
    """Admin: Build a new Containerfile and register as a template with SSE logs."""
    import json
    import asyncio
    from fastapi.responses import StreamingResponse
    from app.models.custom_template import CustomTemplate

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def run_builder():
        try:
            from app.orchestrator.flavors import get_flavor
            from app.orchestrator.scheduler import select_worker_node

            if not settings.AIFACTORY_REGISTRY_URL and not settings.USE_MOCK_PODMAN:
                raise RuntimeError(
                    "Custom image builds require AIFACTORY_REGISTRY_URL so every "
                    "worker can pull the result."
                )
            normalized_template_id = template_id.strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", normalized_template_id):
                raise RuntimeError("Şablon ID yalnızca küçük harf, sayı, nokta, tire ve alt çizgi içerebilir.")
            if normalized_template_id in BUILTIN_TEMPLATE_IDS or await db.get(CustomTemplate, normalized_template_id):
                raise RuntimeError("Bu şablon ID zaten kullanılıyor.")
            prefix = settings.AIFACTORY_REGISTRY_URL.rstrip("/")
            image_tag = (
                f"{prefix}/aifactory-{normalized_template_id}:latest"
                if prefix
                else f"localhost/aifactory-{normalized_template_id}:latest"
            )
            await emit(f"🔨 Starting build for [{image_tag}]...", "info")
            flavor = get_flavor("t1.nano")
            if flavor is None:
                raise RuntimeError("Build worker resource profile is missing.")
            node = await select_worker_node(db, flavor)
            await emit(f"Build worker selected: {node.name}", "info")
            result = await agent_manager.get(node.id).request(
                "image.build",
                {
                    "containerfile": containerfile,
                    "image_tag": image_tag,
                    "push": bool(prefix),
                },
                timeout=900,
            )
            if not result.get("success"):
                err_payload = json.dumps({"type": "error", "text": f"Build failed:\n{result.get('logs', '')}"})
                await queue.put(f"data: {err_payload}\n\n")
                return

            # Save in database
            custom_tpl = CustomTemplate(
                id=normalized_template_id,
                name=name.strip(),
                description=description.strip(),
                category=category.strip() or "Custom",
                icon="cube",
                image_tag=image_tag,
                default_port=default_port,
                ide_type=ide_type,
                containerfile=containerfile,
                is_ready=True,
            )
            if prefix:
                await emit("Image controller kataloğu için arşivleniyor...", "info")
                metadata = await asyncio.to_thread(
                    import_registry_image,
                    image_ref=image_tag,
                    source_ref=image_tag,
                    username="",
                    password="",
                )
                await register_workspace_image(
                    db,
                    template_id=normalized_template_id,
                    display_name=name,
                    default_display_name=name,
                    source_type="builder",
                    source_ref=image_tag,
                    metadata=metadata,
                    custom_template=custom_tpl,
                )
                await emit("Image worker senkronizasyon kataloğuna eklendi.", "success")
            else:
                db.add(custom_tpl)
                await db.commit()
            register_custom_template(
                template_id=custom_tpl.id,
                name=custom_tpl.name,
                description=custom_tpl.description,
                category=custom_tpl.category,
                image_tag=custom_tpl.image_tag,
                default_port=custom_tpl.default_port,
                ide_type=custom_tpl.ide_type,
                icon=custom_tpl.icon,
            )

            done_payload = json.dumps({"type": "done", "template_id": normalized_template_id, "image_tag": image_tag})
            await queue.put(f"data: {done_payload}\n\n")
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "text": f"Unexpected error: {str(exc)}"})
            await queue.put(f"data: {err_payload}\n\n")
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(run_builder())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream(), media_type="text/event-stream")
