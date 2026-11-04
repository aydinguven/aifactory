import asyncio
from datetime import datetime, timezone
import logging
import re
from typing import Annotated
from urllib.parse import quote
from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from app.models.node import Node
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.mlflow_deployment import MlflowDeploymentStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import (
    flavor_mismatch,
    get_template,
    is_retired,
    resolve_template,
)
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.admission import admission_transaction
from app.orchestrator.scheduler import (
    accelerator_availability_details,
    validate_restart_capacity,
    flavor_availability,
    NoSchedulableNode,
)
from app.resource_usage import get_cluster_usage, get_user_usage
from app.quotas import effective_quota
from app.kilo_code import KILO_CODE_ASSISTANT
from app.orchestrator.metrics_service import get_workspace_disk_usage_by_user
from app.orchestrator.workspace_provisioning import (
    CA_BUNDLE_IDE_TYPES,
    KILO_CODE_MISSING_KEY_MESSAGE,
    QuotaExceeded,
    WorkspaceAiConfig,
    WorkspaceNameConflict,
    WorkspaceOperationError,
    applied_ai_config_digest,
    delete_workspace_resources,
    flavor_definition,
    get_resume_quota_error,
    schedule_and_reserve_workspace,
    sync_mlflow_deployment_status,
    kilo_code_worker_problem,
    pin_active_workspace_image,
    runtime_image_missing_problem,
    template_definition,
    workspace_ai_config,
    workspace_ca_bundle,
    workspace_mlflow_environment,
    workspace_runtime_image,
    workspace_service_environment,
    workspace_skills_bundle,
)
from app.placement import (
    PlacementRequestError,
    placement_options,
    resolve_workspace_constraints,
)
from app.schemas.workspace import (
    FlavorInfo,
    PlacementOptions,
    TemplateInfo,
    WorkspaceCreate,
    WorkspaceOut,
)
from app.workspace_catalog import (
    flavor_enabled,
    list_enabled_flavors,
    list_enabled_templates,
    resolve_flavor,
    template_enabled,
)


logger = logging.getLogger("devcloud.routes.workspaces")
workspace_router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])
RETIRED_TEMPLATE_MESSAGE = (
    "Bu VS Code şablonu kullanımdan kaldırıldı; yeni workspace için Kilo Code "
    "şablonunu seçin. Mevcut workspace'ler çalışmaya devam eder."
)


def _snapshot_ide_type(template_id: str) -> str:
    """Carry the source workspace's interface over to its snapshot template."""
    source = get_template(template_id)
    if source:
        return source.ide_type
    if "jupyter" in template_id:
        return "jupyter"
    if "terminal" in template_id:
        return "terminal"
    return "vscode"


@workspace_router.get("/templates", response_model=list[TemplateInfo])
async def get_templates(db: Annotated[AsyncSession, Depends(get_db)]):
    """List available project environment templates."""
    return await list_enabled_templates(db)


@workspace_router.get("/placement-options", response_model=PlacementOptions)
async def get_placement_options(db: Annotated[AsyncSession, Depends(get_db)]):
    """Datacenters and custom placement tags offered on the create form."""
    return await placement_options(db)


@workspace_router.get("/flavors", response_model=list[FlavorInfo])
async def get_flavors(
    db: Annotated[AsyncSession, Depends(get_db)],
    datacenter: Annotated[str, Query(max_length=40)] = "",
    tags: Annotated[list[str], Query()] = [],
):
    """List available resource flavors, optionally for a requested placement."""
    try:
        constraints = await resolve_workspace_constraints(db, datacenter, tags)
    except PlacementRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    catalog = []
    for item in await list_enabled_flavors(db):
        flavor = get_flavor(item.id)
        available, message = await flavor_availability(db, flavor, constraints)
        details = (
            await accelerator_availability_details(db, flavor, constraints)
            if flavor and flavor.accelerator_count
            else {}
        )
        catalog.append(item.model_copy(update={
            "available": available,
            "availability_message": message,
            **details,
        }))
    return catalog


@workspace_router.get("/usage")
async def get_resource_usage(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return host usage plus the current user's allocation and quota."""
    result = await db.execute(
        select(Workspace).where(Workspace.user_id == current_user.id)
    )
    workspaces = result.scalars().all()
    nodes = (await db.execute(select(Node))).scalars().all()
    disk_usage = await get_workspace_disk_usage_by_user(workspaces)
    quota = await effective_quota(db, current_user)
    user_usage = await asyncio.to_thread(
        get_user_usage,
        current_user,
        workspaces,
        disk_used_bytes=disk_usage.get(current_user.id, 0),
        quota=quota,
    )
    return {
        "system": get_cluster_usage(nodes),
        "user": user_usage,
    }


@workspace_router.get("", response_model=list[WorkspaceOut])
async def list_user_workspaces(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all workspaces owned by the logged-in user."""
    stmt = (
        select(Workspace)
        .where(
            Workspace.user_id == current_user.id,
            Workspace.template_id != "mlflow-serving",
        )
        .order_by(Workspace.created_at.desc())
    )
    result = await db.execute(stmt)
    workspaces = result.scalars().all()
    
    # Enrich with web_url for quick launching
    out = []
    for ws in workspaces:
        ws_out = WorkspaceOut.model_validate(ws)
        ws_out.web_url = f"/proxy/{ws.id}/"
        out.append(ws_out)
    return out


@workspace_router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new persistent workspace and deploy container."""
    template = await resolve_template(db, data.template_id)
    if not template:
        raise HTTPException(status_code=400, detail=f"Geçersiz şablon ID: {data.template_id}")
    if template.id == "mlflow-serving":
        raise HTTPException(
            status_code=400,
            detail="MLflow servis workspace yalnızca model deployment API'si ile oluşturulabilir.",
        )
    if not await template_enabled(db, data.template_id):
        detail = (
            RETIRED_TEMPLATE_MESSAGE
            if is_retired(data.template_id)
            else f"Şablon devre dışı: {data.template_id}"
        )
        raise HTTPException(status_code=400, detail=detail)

    flavor = await resolve_flavor(db, data.flavor_id)
    if not flavor or not await flavor_enabled(db, data.flavor_id):
        raise HTTPException(status_code=400, detail=f"Geçersiz kaynak profili ID: {data.flavor_id}")
    mismatch = flavor_mismatch(template, flavor)
    if mismatch:
        raise HTTPException(status_code=400, detail=mismatch)

    try:
        workspace, placement = await schedule_and_reserve_workspace(
            db,
            data=data,
            current_user=current_user,
            template=template,
            flavor=flavor,
        )
    except PlacementRequestError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (QuotaExceeded, WorkspaceNameConflict) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (NoSchedulableNode, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))


    # Launch container via Podman
    try:
        runtime = runtime_for_node(workspace.node_id)
        image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
        ai_config = await workspace_ai_config(db, template, current_user.id)
        if ai_config.kilo_code_problem:
            logger.warning("Workspace %s: %s", workspace.id, ai_config.kilo_code_problem)
        container_id, storage_path = await runtime.create_workspace_container(
            workspace_id=workspace.id,
            user_id=current_user.id,
            container_name=workspace.container_name,
            template_id=template.id,
            flavor_id=flavor.id,
            template_definition=template_definition(template),
            flavor_definition=flavor_definition(flavor),
            host_port=workspace.host_port,
            workspace_token=workspace.workspace_token,
            accelerator_cdi_name=workspace.accelerator_cdi_name or "",
            image_ref=image_ref,
            image_sha256=image_sha256,
            mlflow_environment=await workspace_mlflow_environment(db, current_user.id),
            **ai_config.create_arguments(),
        )
        workspace.container_id = container_id
        workspace.storage_path = storage_path
        workspace.ai_config_digest = await applied_ai_config_digest(db, workspace, ai_config)
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
    except Exception as exc:
        logger.error(f"Error launching workspace {workspace.id}: {exc}")
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = str(exc)

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.post("/deploy-stream")
async def deploy_workspace_stream(
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create and deploy a workspace container with real-time SSE log streaming."""
    import json
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit_log(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def emit_error(text: str):
        payload = json.dumps({"type": "error", "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def emit_done(workspace_id: str, web_url: str):
        payload = json.dumps({"type": "done", "workspace_id": workspace_id, "web_url": web_url})
        await queue.put(f"data: {payload}\n\n")

    async def run_deployment():
        try:
            await emit_log(f"'{data.name}' için kurulum süreci başlatılıyor...", "info")
            await asyncio.sleep(0.05)

            template = await resolve_template(db, data.template_id)
            if not template:
                await emit_error(f"Geçersiz şablon: {data.template_id}")
                return
            if template.id == "mlflow-serving":
                await emit_error(
                    "MLflow servis workspace yalnızca model deployment ekranından oluşturulabilir."
                )
                return
            if not await template_enabled(db, data.template_id):
                await emit_error(
                    RETIRED_TEMPLATE_MESSAGE
                    if is_retired(data.template_id)
                    else f"Şablon devre dışı: {data.template_id}"
                )
                return

            flavor = await resolve_flavor(db, data.flavor_id)
            if not flavor or not await flavor_enabled(db, data.flavor_id):
                await emit_error(f"Geçersiz kaynak profili: {data.flavor_id}")
                return
            mismatch = flavor_mismatch(template, flavor)
            if mismatch:
                await emit_error(mismatch)
                return

            await emit_log(f"Şablon: {template.name} ({template.image_tag})", "info")
            await emit_log(f"Çalışma alanı kaynağı: {flavor.cpus} CPU, {flavor.memory_display} RAM ({flavor.name})", "info")
            placement_request = (data.datacenter or "Any") + (
                f" · etiketler: {', '.join(data.tags)}" if data.tags else ""
            )
            await emit_log(f"Yerleşim isteği: {placement_request}", "info")

            try:
                workspace, placement = await schedule_and_reserve_workspace(
                    db,
                    data=data,
                    current_user=current_user,
                    template=template,
                    flavor=flavor,
                )
                await emit_log(
                    f"Worker seçildi: {placement.node.name}"
                    f"{' (' + placement.node.datacenter + ')' if placement.node.datacenter else ''}"
                    f"; host portu ayrıldı: {workspace.host_port}",
                    "info",
                )
                if placement.accelerator:
                    await emit_log(
                        f"GPU ayrıldı: {placement.accelerator.model}; "
                        f"slot {placement.accelerator.slot + 1}/"
                        f"{placement.accelerator.shared_slots}",
                        "success",
                    )
            except (WorkspaceNameConflict, PlacementRequestError) as e:
                await emit_error(str(e))
                return
            except (NoSchedulableNode, RuntimeError) as e:
                await emit_error(f"Kaynak ayrılamadı: {str(e)}")
                return

            await emit_log(f"Kullanıcı #{current_user.id} için kalıcı volume hazırlandı", "info")

            # Launch container with progress callback
            runtime = runtime_for_node(workspace.node_id)
            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            ai_config = await workspace_ai_config(db, template, current_user.id)
            if ai_config.kilo_code_problem:
                await emit_log(ai_config.kilo_code_problem, "warning")
            if ai_config.user_key_missing:
                await emit_log(KILO_CODE_MISSING_KEY_MESSAGE, "warning")
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=current_user.id,
                container_name=workspace.container_name,
                template_id=template.id,
                flavor_id=flavor.id,
                template_definition=template_definition(template),
                flavor_definition=flavor_definition(flavor),
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
                accelerator_cdi_name=workspace.accelerator_cdi_name or "",
                image_ref=image_ref,
                image_sha256=image_sha256,
                mlflow_environment=await workspace_mlflow_environment(db, current_user.id),
                **ai_config.create_arguments(),
                progress_callback=emit_log,
            )

            workspace.container_id = container_id
            workspace.storage_path = storage_path
            workspace.ai_config_digest = await applied_ai_config_digest(
                db, workspace, ai_config
            )
            workspace.status = WorkspaceStatus.RUNNING
            workspace.last_started_at = datetime.now(timezone.utc)
            workspace.error_message = None

            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)

            await emit_log(f"Kalıcı dizin: {storage_path}", "success")
            await emit_done(workspace.id, f"/proxy/{workspace.id}/")

        except Exception as exc:
            logger.error(f"Error launching workspace: {exc}")
            if "workspace" in locals():
                workspace.status = WorkspaceStatus.ERROR
                workspace.error_message = str(exc)
                db.add(workspace)
                await db.commit()
            await emit_error(f"Kurulum hatası: {str(exc)}")
        finally:
            await queue.put(None)

    async def stream_generator():
        task = asyncio.create_task(run_deployment())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


@workspace_router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace_detail(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get single workspace details."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


async def _kilo_code_image_problem(db: AsyncSession, workspace: Workspace, template) -> str:
    """Pin the template's enabled image and say why it cannot be used yet, else ''.

    The pin is undone when the image is not on the worker, so a later import
    is still picked up.
    """
    previous_image_id = workspace.image_id
    await pin_active_workspace_image(db, workspace, template)
    problem = await runtime_image_missing_problem(db, workspace, template)
    if problem:
        workspace.image_id = previous_image_id
    return problem


@workspace_router.post("/{workspace_id}/start", response_model=WorkspaceOut)
async def start_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Resume / Start a stopped workspace container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    async with admission_transaction(db):
        await db.refresh(workspace)
        if workspace.status in {WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING}:
            raise HTTPException(status_code=409, detail="Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.")
        try:
            await validate_restart_capacity(db, workspace)
        except NoSchedulableNode as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The owner's quota, not the caller's: an admin may resume another
        # user's workspace and must not spend their own allowance.
        resume_quota_error = await get_resume_quota_error(db, workspace)
        if resume_quota_error:
            raise HTTPException(status_code=409, detail=resume_quota_error)
        was_running = workspace.status == WorkspaceStatus.RUNNING
        workspace.status = WorkspaceStatus.STARTING

    try:
        runtime = runtime_for_node(workspace.node_id)
        container_exists = await runtime.container_exists(workspace.container_name)
        if was_running and container_exists and await runtime.get_container_status(workspace.container_name) == "running":
            workspace.status = WorkspaceStatus.RUNNING
            await db.commit()
            ws_out = WorkspaceOut.model_validate(workspace)
            ws_out.web_url = f"/proxy/{workspace.id}/"
            return ws_out

        template = None
        ai_config = None
        if container_exists:
            template = await resolve_template(db, workspace.template_id)
            if template and template.ai_assistant == KILO_CODE_ASSISTANT:
                # The keys live in the container's environment, so a newly
                # requested AI Factory key or changed Admin settings need a
                # new container. Project files are on the bind mount.
                ai_config = await workspace_ai_config(db, template, workspace.user_id)
                if ai_config.kilo_code_problem:
                    logger.warning(
                        "Workspace %s: %s", workspace.id, ai_config.kilo_code_problem
                    )
                worker_problem = await kilo_code_worker_problem(db, workspace)
                config_changed = await applied_ai_config_digest(
                    db, workspace, ai_config
                ) != (workspace.ai_config_digest or "")
                image_problem = ""
                if config_changed and not worker_problem:
                    image_problem = await _kilo_code_image_problem(db, workspace, template)
                if worker_problem:
                    # Recreating on this worker would drop the config again.
                    logger.warning("Workspace %s: %s", workspace.id, worker_problem)
                elif image_problem:
                    # Removing now would leave the workspace with no container.
                    logger.warning(
                        "Workspace %s keeps its container: %s", workspace.id, image_problem
                    )
                elif config_changed:
                    try:
                        if await runtime.remove_container(workspace.container_name):
                            container_exists = False
                            logger.info(
                                "Recreating %s because its Kilo Code settings changed",
                                workspace.container_name,
                            )
                    except Exception as exc:  # noqa: BLE001 - outdated workers
                        logger.warning(
                            "Keeping %s with its previous Kilo Code settings: %s",
                            workspace.container_name,
                            exc,
                        )
            elif template and template.ide_type in CA_BUNDLE_IDE_TYPES:
                # The gateway CA bundle and the skill library are in the
                # container's environment as well, so adding, changing or
                # removing them needs a new one. The digest is what this
                # worker applies, so an outdated worker is not asked to
                # recreate on every start.
                # Only the digested parts: resolving the gateway key here could
                # rotate it under a container that is then merely restarted.
                applied_digest = await applied_ai_config_digest(
                    db,
                    workspace,
                    WorkspaceAiConfig(
                        ca_bundle=await workspace_ca_bundle(db, template),
                        skills_bundle=await workspace_skills_bundle(db, template),
                    ),
                )
                if applied_digest != (workspace.ai_config_digest or ""):
                    try:
                        image_problem = await runtime_image_missing_problem(
                            db, workspace, template
                        )
                    except RuntimeError as exc:
                        image_problem = str(exc)
                    if image_problem:
                        logger.warning(
                            "Workspace %s keeps its container: %s",
                            workspace.id,
                            image_problem,
                        )
                    else:
                        try:
                            if await runtime.remove_container(workspace.container_name):
                                container_exists = False
                                logger.info(
                                    "Recreating %s because the gateway CA bundle "
                                    "or the skill library changed",
                                    workspace.container_name,
                                )
                        except Exception as exc:  # noqa: BLE001 - outdated workers
                            logger.warning(
                                "Keeping %s with its previous CA bundle and skills: %s",
                                workspace.container_name,
                                exc,
                            )

        if container_exists:
            success = await runtime.start_container(workspace.container_name)
            if success and workspace.template_id == "mlflow-serving":
                success = False
                for _ in range(30):
                    if await runtime.health_ready(
                        workspace.container_name, workspace.host_port, "/ping"
                    ):
                        success = True
                        break
                    await asyncio.sleep(0.5)
                if not success:
                    workspace.error_message = "Model servisi /ping sağlık kontrolünü geçemedi."
        else:
            logger.info(
                "Recreating missing container %s with persistent storage %s",
                workspace.container_name,
                workspace.storage_path,
            )
            template = template or await resolve_template(db, workspace.template_id)
            flavor = await resolve_flavor(db, workspace.flavor_id)
            if not template or not flavor:
                raise ValueError("Workspace şablonu veya kaynak profili artık bulunamıyor.")
            if template.ai_assistant == KILO_CODE_ASSISTANT:
                image_problem = await _kilo_code_image_problem(db, workspace, template)
                if image_problem:
                    raise RuntimeError(image_problem)
            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            if ai_config is None:
                ai_config = await workspace_ai_config(db, template, workspace.user_id)
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                container_name=workspace.container_name,
                template_id=workspace.template_id,
                flavor_id=workspace.flavor_id,
                template_definition=template_definition(template),
                flavor_definition=flavor_definition(flavor),
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
                accelerator_cdi_name=workspace.accelerator_cdi_name or "",
                image_ref=image_ref,
                image_sha256=image_sha256,
                mlflow_environment=(
                    {}
                    if workspace.template_id == "mlflow-serving"
                    else await workspace_mlflow_environment(db, workspace.user_id)
                ),
                service_environment=await workspace_service_environment(db, workspace),
                **ai_config.create_arguments(),
            )
            workspace.container_id = container_id
            workspace.storage_path = storage_path
            workspace.ai_config_digest = await applied_ai_config_digest(
                db, workspace, ai_config
            )
            success = True
    except (ValueError, RuntimeError, TimeoutError) as exc:
        logger.exception("Failed to start workspace %s", workspace.id)
        workspace.error_message = str(exc)
        success = False

    if success:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
        await sync_mlflow_deployment_status(
            db,
            workspace,
            MlflowDeploymentStatus.RUNNING,
            "Model servisi başlatıldı.",
        )
    else:
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = workspace.error_message or "Container yeniden başlatılamadı."
        await sync_mlflow_deployment_status(
            db,
            workspace,
            MlflowDeploymentStatus.FAILED,
            "Model servisi başlatılamadı.",
            workspace.error_message,
        )

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.post("/{workspace_id}/stop", response_model=WorkspaceOut)
async def stop_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Pause / Stop a running workspace container (preserves all data to resume later)."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    async with admission_transaction(db):
        await db.refresh(workspace)
        if workspace.status in {WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING}:
            raise HTTPException(status_code=409, detail="Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.")
        previous_status = workspace.status
        workspace.status = WorkspaceStatus.STOPPING
    runtime = runtime_for_node(workspace.node_id)
    try:
        if not await runtime.stop_container(workspace.container_name):
            raise RuntimeError("Worker could not stop the container; state was preserved.")
    except (RuntimeError, TimeoutError) as exc:
        workspace.status = previous_status
        workspace.error_message = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    workspace.status = WorkspaceStatus.STOPPED
    workspace.error_message = None
    workspace.last_stopped_at = datetime.now(timezone.utc)
    await sync_mlflow_deployment_status(
        db,
        workspace,
        MlflowDeploymentStatus.STOPPED,
        "Model servisi durduruldu.",
    )

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.delete("/{workspace_id}")
async def delete_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a user workspace; managed model services use deployment deletion."""
    workspace = await db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")
    if workspace.template_id == "mlflow-serving":
        raise HTTPException(
            status_code=409,
            detail="Model servis workspace'i deployment ekranından silinmelidir.",
        )
    try:
        await delete_workspace_resources(db, workspace)
    except WorkspaceOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"message": f"Çalışma alanı {workspace_id} ve kalıcı depolaması silindi."}


@workspace_router.get("/{workspace_id}/logs")
async def get_workspace_logs(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tail: int = 100,
):
    """Retrieve logs from container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    runtime = runtime_for_node(workspace.node_id)
    logs = await runtime.get_logs(workspace.container_name, tail=tail)
    return {"workspace_id": workspace_id, "logs": logs}


@workspace_router.get("/stats/summary")
async def get_workspaces_stats_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve live CPU, RAM, Disk, and uptime stats for all workspaces owned by the user."""
    from app.orchestrator.metrics_service import get_workspace_live_metrics

    stmt = (
        select(Workspace)
        .where(Workspace.user_id == current_user.id, Workspace.status != WorkspaceStatus.DELETED)
    )
    res = await db.execute(stmt)
    workspaces = res.scalars().all()

    metrics_list = []
    for ws in workspaces:
        m = await get_workspace_live_metrics(ws)
        metrics_list.append(m)

    return {"stats": metrics_list}


@workspace_router.get("/{workspace_id}/stats")
async def get_single_workspace_stats(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve live CPU, RAM, Disk, and uptime stats for a single workspace."""
    from app.orchestrator.metrics_service import get_workspace_live_metrics

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    return await get_workspace_live_metrics(workspace)


@workspace_router.get("/{workspace_id}/backup/download")
async def download_workspace_backup(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Stream a ZIP backup from the worker that owns the workspace."""
    from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    try:
        connection = agent_manager.get(workspace.node_id)
        metadata, stream = await connection.open_stream(
            "workspace.backup.open",
            {
                "workspace_id": workspace.id,
                "container_name": workspace.container_name,
            },
            timeout=120,
        )
    except (AgentUnavailable, AgentCommandError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def body():
        try:
            while True:
                item = await connection.receive_stream(stream)
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item.data
        finally:
            await connection.close_stream(stream.id)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace.name).strip(".-")
    filename = f"{(safe_name[:80] or workspace.id)}-backup.zip"
    headers = {
        "Content-Disposition": (
            'attachment; filename="workspace-backup.zip"; '
            f"filename*=UTF-8''{quote(filename)}"
        ),
    }
    size = metadata.get("size")
    if isinstance(size, int) and size >= 0:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        body(),
        media_type="application/zip",
        headers=headers,
    )


@workspace_router.post("/{workspace_id}/snapshot")
async def snapshot_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    template_name: Annotated[str, Form()],
    template_description: Annotated[str, Form()] = "",
):
    """Snapshot a running/stopped container into a reusable custom template."""
    import re
    from app.agents.manager import agent_manager
    from app.models.custom_template import CustomTemplate

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if (
        settings.DEVCLOUD_DEPLOYMENT_ROLE != "all-in-one"
        and not settings.USE_MOCK_PODMAN
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Workspace snapshots are currently all-in-one only. "
                "Distributed export requires a separately authorized registry "
                "publication workflow."
            ),
        )
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "", template_name.lower().replace(" ", "-"))[:30]
    template_id = f"custom-{slug}"
    image_tag = f"localhost/devcloud-{template_id}:latest"
    try:
        result = await agent_manager.get(workspace.node_id).request(
            "container.snapshot",
            {
                "workspace_id": workspace.id,
                "container_name": workspace.container_name,
                "image_tag": image_tag,
            },
            timeout=180,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    image_tag_or_err = str(result["image_tag"])

    custom_tpl = CustomTemplate(
        id=template_id,
        name=template_name.strip(),
        description=template_description.strip() or f"Snapshotted from {workspace.name}",
        category="Özel",
        icon="cube",
        image_tag=image_tag_or_err,
        default_port=workspace.container_port,
        ide_type=_snapshot_ide_type(workspace.template_id),
        is_ready=True,
    )
    db.add(custom_tpl)
    await db.commit()

    return {
        "message": f"Successfully created template '{template_name}' from workspace.",
        "template_id": template_id,
        "template_name": template_name,
        "image_tag": image_tag_or_err,
    }
