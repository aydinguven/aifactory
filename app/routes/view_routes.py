import asyncio
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.database import get_db
import json
import logging
from app.models.user import User, UserRole
from app.models.directory_settings import DirectorySettings
from app.session_settings import session_timeout_minutes
from app.models.mlflow_settings import MlflowSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.onboarding import OnboardingSettings
from app.models.model_container_registry_settings import (
    ModelContainerRegistrySettings,
)
from app.models.download_settings import DownloadSettings
from app.models.deploy_registry_settings import DeployRegistrySettings
from app.models.bamboo_settings import BambooSettings
from app.bamboo import (
    BambooConfigurationError,
    bamboo_settings_out,
    effective_bamboo_config,
)
from app.deploy_registry import (
    DeployRegistryConfigurationError,
    deploy_registry_settings_out,
    effective_deploy_registry_config,
)
from app.models.node import Node
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.models.kilo_code_settings import KiloCodeSettings
from app.kilo_code import parse_model_ids, parse_skills_urls
from app.models.genai_settings import GenAiSettings
from app.genai import is_configured as genai_is_configured
from app.integrations.litellm import parse_models as parse_genai_models
from app.models.custom_template import CustomTemplate
from app.jupyter_ai import default_model_catalog, parse_model_catalog
from app.model_container_registry import (
    ModelContainerRegistryConfigurationError,
    effective_model_container_registry_config,
)
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.scheduler import (
    accelerator_availability_details,
    flavor_availability,
)
from app.orchestrator.templates import list_builtin_templates
from app.placement import is_gpu_worker, load_tag_catalog, placement_options
from app.orchestrator.runtime_backend import runtime_for_node
from app.agents.manager import AgentUnavailable
from app.resource_usage import get_all_user_usage, get_cluster_usage, get_user_usage
from app.directory_sync import directory_teams_by_key, parse_summary
from app.quotas import build_group_views, build_hierarchy, effective_quota, load_quota_groups
from app.orchestrator.metrics_service import get_workspace_disk_usage_by_user
from app.schemas.workspace import WorkspaceOut
from app.workspace_catalog import configured_flavors, configured_templates, list_enabled_flavors, list_enabled_templates
from app.static_assets import STATIC_ASSET_VERSION

logger = logging.getLogger("aifactory.views")

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["app_version"] = settings.APP_VERSION
templates.env.globals["static_version"] = STATIC_ASSET_VERSION

def _safe_from_json(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    try:
        data = json.loads(val)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

templates.env.filters["from_json"] = _safe_from_json
templates.env.tests["gpu_worker"] = is_gpu_worker

view_router = APIRouter(include_in_schema=False)


@view_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve login page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    directory_settings = await db.get(DirectorySettings, 1)
    registration_enabled = not (
        directory_settings and directory_settings.enabled
    )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "app_name": settings.APP_NAME,
            "user": None,
            "registration_cpu_quota": settings.DEFAULT_USER_CPU_QUOTA,
            "registration_memory_gb_quota": settings.DEFAULT_USER_MEMORY_MB_QUOTA / 1024,
            "registration_disk_gb_quota": settings.DEFAULT_USER_DISK_MB_QUOTA / 1024,
            "registration_enabled": registration_enabled,
            "directory_enabled": not registration_enabled,
        },
    )


@view_router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve register page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    directory_settings = await db.get(DirectorySettings, 1)
    if directory_settings and directory_settings.enabled:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "app_name": settings.APP_NAME,
            "user": None,
            "registration_cpu_quota": settings.DEFAULT_USER_CPU_QUOTA,
            "registration_memory_gb_quota": settings.DEFAULT_USER_MEMORY_MB_QUOTA / 1024,
            "registration_disk_gb_quota": settings.DEFAULT_USER_DISK_MB_QUOTA / 1024,
        },
    )


@view_router.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve main workspace dashboard."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    stmt = (
        select(Workspace)
        .where(
            Workspace.user_id == current_user.id,
            Workspace.status != WorkspaceStatus.DELETED,
            Workspace.template_id != "mlflow-serving",
        )
        .order_by(Workspace.created_at.desc())
    )
    result = await db.execute(stmt)
    workspaces = result.scalars().all()

    # Self-healing status reconciliation with Podman engine
    status_changed = False
    for ws in workspaces:
        if ws.status == WorkspaceStatus.CREATING and ws.container_name:
            try:
                actual_status = await runtime_for_node(ws.node_id).get_container_status(ws.container_name)
            except AgentUnavailable:
                continue
            if actual_status == "running":
                ws.status = WorkspaceStatus.RUNNING
                db.add(ws)
                status_changed = True
    if status_changed:
        await db.commit()

    ws_list = [WorkspaceOut.model_validate(ws) for ws in workspaces]
    for ws_out in ws_list:
        ws_out.web_url = f"/proxy/{ws_out.id}/"

    nodes = (await db.execute(select(Node))).scalars().all()
    disk_usage = await get_workspace_disk_usage_by_user(workspaces)
    system_usage = get_cluster_usage(nodes)
    quota = await effective_quota(db, current_user)
    user_usage = await asyncio.to_thread(
        get_user_usage,
        current_user,
        workspaces,
        disk_used_bytes=disk_usage.get(current_user.id, 0),
        quota=quota,
    )

    flavor_catalog = []
    for item in await list_enabled_flavors(db):
        available = True
        message = ""
        flavor = get_flavor(item.id)
        details: dict[str, object] = {}
        if flavor and flavor.accelerator_count:
            available, message = await flavor_availability(db, flavor)
            details = await accelerator_availability_details(db, flavor)
            gpu_quota_remaining = int(user_usage["gpu"]["remaining"])
            details["gpu_quota_remaining"] = gpu_quota_remaining
            if gpu_quota_remaining < flavor.accelerator_count:
                available = False
                message = "GPU kotanız yok. Yöneticinizden GPU slot kotası isteyin."
        flavor_catalog.append(item.model_copy(update={
            "available": available,
            "availability_message": message,
            **details,
        }))

    all_tpls = await list_enabled_templates(db)
    default_flavor_id = (
        "t1.micro"
        if any(item.id == "t1.micro" for item in flavor_catalog)
        else (flavor_catalog[0].id if flavor_catalog else "")
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "workspaces": ws_list,
            "templates": all_tpls,
            "flavors": flavor_catalog,
            "default_flavor_id": default_flavor_id,
            "placement_options": await placement_options(db),
            "system_usage": system_usage,
            "user_usage": user_usage,
        },
    )


@view_router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
async def workspace_detail_page(
    workspace_id: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve workspace detail and IDE frame page."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace or (
        workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN
    ):
        return RedirectResponse(url="/", status_code=302)

    # Reconcile status with Podman if needed
    if workspace.container_name:
        try:
            actual_status = await runtime_for_node(workspace.node_id).get_container_status(workspace.container_name)
        except AgentUnavailable:
            actual_status = "worker-offline"
        if actual_status == "running" and workspace.status != WorkspaceStatus.RUNNING:
            workspace.status = WorkspaceStatus.RUNNING
            db.add(workspace)
            await db.commit()

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"

    return templates.TemplateResponse(
        request=request,
        name="workspace_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "workspace": ws_out,
            "raw_workspace": workspace,
        },
    )


@view_router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Serve user profile page."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
        },
    )


@view_router.get("/genai", response_class=HTMLResponse)
async def genai_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    genai_settings = await db.get(GenAiSettings, 1)
    return templates.TemplateResponse(
        request=request,
        name="genai.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "genai_configured": genai_is_configured(genai_settings),
        },
    )


@view_router.get("/genai/stats", response_class=HTMLResponse)
async def genai_stats_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="genai_stats.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "genai_configured": genai_is_configured(await db.get(GenAiSettings, 1)),
        },
    )


@view_router.get("/mlflow", response_class=HTMLResponse)
async def mlflow_dashboard_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    mlflow_settings = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    mlflow_server_settings = await db.get(MlflowServerSettings, 1)
    return templates.TemplateResponse(
        request=request,
        name="mlflow_dashboard.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "mlflow_enabled": bool(
                mlflow_server_settings
                and mlflow_server_settings.enabled
                and mlflow_settings
                and mlflow_settings.enabled
            ),
        },
    )


@view_router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    mlflow_settings = (
        await db.execute(
            select(MlflowSettings).where(
                MlflowSettings.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    mlflow_server_settings = await db.get(MlflowServerSettings, 1)
    return templates.TemplateResponse(
        request=request,
        name="models.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "mlflow_enabled": bool(
                mlflow_server_settings
                and mlflow_server_settings.enabled
                and mlflow_settings
                and mlflow_settings.enabled
            ),
            "mlflow_settings": mlflow_settings,
            "mlflow_server_settings": mlflow_server_settings,
            "mlflow_base_url": (
                mlflow_server_settings.base_url if mlflow_server_settings else ""
            ),
        },
    )


@view_router.get("/models/{model_name}", response_class=HTMLResponse)
async def model_detail_page(
    model_name: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="model_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "model_name": model_name,
        },
    )


@view_router.get("/deployments", response_class=HTMLResponse)
async def mlflow_deployments_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="mlflow_deployments.html",
        context={"app_name": settings.APP_NAME, "user": current_user},
    )


@view_router.get("/experiments", response_class=HTMLResponse)
async def experiments_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    mlflow_settings = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    mlflow_server_settings = await db.get(MlflowServerSettings, 1)
    return templates.TemplateResponse(
        request=request,
        name="experiments.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "mlflow_enabled": bool(
                mlflow_server_settings
                and mlflow_server_settings.enabled
                and mlflow_settings
                and mlflow_settings.enabled
            ),
        },
    )


@view_router.get("/experiments/{experiment_id}", response_class=HTMLResponse)
async def experiment_detail_page(
    experiment_id: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="experiment_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "experiment_id": experiment_id,
        },
    )


@view_router.get("/runs/compare", response_class=HTMLResponse)
async def run_compare_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="run_compare.html",
        context={"app_name": settings.APP_NAME, "user": current_user},
    )


@view_router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(
    run_id: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "run_id": run_id,
        },
    )


# One name per admin section: the menu label, the page title and the overview
# card all use "title". "group" sorts the menu into short, labelled blocks.
ADMIN_SECTIONS = {
    "overview": {
        "title": "Genel Bakış",
        "description": "Platform sağlığını ve yönetim alanlarını tek bakışta izleyin.",
        "group": "",
    },
    "users": {
        "title": "Kullanıcılar",
        "description": "Kullanıcı kotalarını, hesap durumlarını ve kurumsal dizin erişimini yönetin.",
        "group": "Kullanıcılar ve alanlar",
    },
    "workspaces": {
        "title": "Çalışma Alanları",
        "description": "Tüm çalışma alanlarını, kaynak profillerini ve ortam şablonlarını yönetin.",
        "group": "Kullanıcılar ve alanlar",
    },
    "workers": {
        "title": "Worker'lar",
        "description": "Worker kayıtlarını, bağlantı durumlarını ve yerleşim etiketlerini yönetin.",
        "group": "Altyapı",
    },
    "images": {
        "title": "Workspace Image'ları",
        "description": "Image sürümlerini registry veya tar arşivinden içe aktarın ve worker senkronizasyonunu izleyin.",
        "group": "Altyapı",
    },
    "kubernetes": {
        "title": "Kubernetes",
        "description": "Model imajlarının yayınlandığı registry'yi, Kubernetes projelerini ve uygulamalarını yönetin.",
        "group": "Altyapı",
    },
    "integrations": {
        "title": "Entegrasyonlar",
        "description": "Workspace AI, Kilo Code, registry, MLflow ve GenAI bağlantılarını yönetin.",
        "group": "Platform",
    },
    "skills": {
        "title": "Skill Kütüphanesi",
        "description": "Kilo Code ve Jupyter AI sohbetlerinin kullanacağı SKILL.md talimatlarını yönetin.",
        "group": "Platform",
    },
    "system": {
        "title": "Sistem",
        "description": "Platform güncellemelerini, oturum ayarlarını, HTTPS'i ve offline paketleri yönetin.",
        "group": "Platform",
    },
}


@view_router.get("/admin", response_class=HTMLResponse)
@view_router.get("/admin/{section}", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    section: str = "overview",
):
    """Serve one focused admin category without loading unrelated datasets."""
    if not current_user or current_user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=302)
    if section not in ADMIN_SECTIONS:
        raise HTTPException(status_code=404, detail="Yönetim bölümü bulunamadı.")

    context = {
        "app_name": settings.APP_NAME,
        "user": current_user,
        "admin_section": section,
        "admin_meta": ADMIN_SECTIONS[section],
        "admin_sections": ADMIN_SECTIONS,
    }

    if section == "overview":
        context["admin_stats"] = {
            "users": (await db.execute(select(func.count(User.id)))).scalar_one(),
            "workspaces": (
                await db.execute(select(func.count(Workspace.id)))
            ).scalar_one(),
            "running_workspaces": (
                await db.execute(
                    select(func.count(Workspace.id)).where(
                        Workspace.status == WorkspaceStatus.RUNNING
                    )
                )
            ).scalar_one(),
            "workers": (await db.execute(select(func.count(Node.id)))).scalar_one(),
        }
    elif section == "users":
        users = (
            await db.execute(select(User).order_by(User.id.asc()))
        ).scalars().all()
        workspaces = (
            await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
        ).scalars().all()
        disk_usage = await get_workspace_disk_usage_by_user(workspaces)
        quota_groups = await load_quota_groups(db)
        quotas = {user.id: quota_groups.resolve(user) for user in users}
        usage_by_user = await asyncio.to_thread(
            get_all_user_usage,
            users,
            workspaces,
            disk_usage_by_user=disk_usage,
            quotas=quotas,
        )
        directory_record = await db.get(DirectorySettings, 1)
        team_views = build_group_views(
            users, quota_groups, usage_by_user, directory_teams=await directory_teams_by_key(db)
        )
        context.update(
            {
                "all_users": users,
                "usage_by_user": usage_by_user,
                "quotas_by_user": quotas,
                "user_groups": team_views,
                "user_hierarchy": build_hierarchy(team_views, quota_groups),
                "directory_settings": directory_record,
                "directory_sync": parse_summary(directory_record),
            }
        )
    elif section == "workspaces":
        context["all_workspaces"] = (
            await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
        ).scalars().all()
        context["admin_flavors"] = await configured_flavors(db)
        context["admin_templates"] = await configured_templates(db)
        context["builtin_template_ids"] = set(list_builtin_templates_item.id for list_builtin_templates_item in list_builtin_templates())
    elif section == "images":
        image_templates = list_builtin_templates()
        custom_templates = (
            await db.execute(select(CustomTemplate).order_by(CustomTemplate.name))
        ).scalars().all()
        known_ids = {template.id for template in image_templates}
        for custom in custom_templates:
            if custom.id not in known_ids:
                image_templates.append(custom)
                known_ids.add(custom.id)
        context["workspace_image_templates"] = image_templates
    elif section == "workers":
        download_settings = None
        try:
            download_settings = await db.get(DownloadSettings, 1)
        except Exception:
            pass

        try:
            nodes_res = await db.execute(select(Node).order_by(Node.name))
            nodes = nodes_res.scalars().all()
        except Exception as exc:
            logger.warning("Error fetching nodes for admin workers view: %s. Attempting schema ensure...", exc)
            try:
                from app.database import ensure_node_columns
                # Run dynamic migration via connection
                conn = await db.connection()
                await ensure_node_columns(conn)
                nodes_res = await db.execute(select(Node).order_by(Node.name))
                nodes = nodes_res.scalars().all()
            except Exception as retry_exc:
                logger.error("Failed to recover nodes query: %s", retry_exc)
                nodes = []

        context["nodes"] = nodes
        tag_catalog = await load_tag_catalog(db)
        context["placement_tags"] = list(tag_catalog.values())
        tag_usage: dict[str, int] = {}
        for node in nodes:
            for name in set(node.tags):
                tag_usage[name] = tag_usage.get(name, 0) + 1
        context["placement_tag_usage"] = tag_usage
        context["download_settings"] = download_settings
        context["download_public_base_url"] = (
            download_settings.public_base_url
            if download_settings and download_settings.public_base_url
            else (settings.DOWNLOAD_PUBLIC_BASE_URL or str(request.base_url).rstrip("/"))
        )
    elif section == "integrations":
        jupyter_ai_settings = await db.get(JupyterAiSettings, 1)
        context["jupyter_ai_settings"] = jupyter_ai_settings
        context["jupyter_ai_models"] = (
            parse_model_catalog(
                jupyter_ai_settings.model_catalog_json,
                jupyter_ai_settings.model_id,
            )
            if jupyter_ai_settings
            else default_model_catalog()
        )
        kilo_code_settings = await db.get(KiloCodeSettings, 1)
        context["kilo_code_settings"] = kilo_code_settings
        context["kilo_code_model_ids"] = (
            parse_model_ids(kilo_code_settings.model_ids_json)
            if kilo_code_settings
            else []
        )
        context["kilo_code_skills_urls"] = (
            parse_skills_urls(kilo_code_settings.skills_urls_json)
            if kilo_code_settings
            else []
        )
        context["mlflow_server_settings"] = await db.get(MlflowServerSettings, 1)
        genai_settings = await db.get(GenAiSettings, 1)
        context["genai_settings"] = genai_settings
        context["genai_models"] = (
            "\n".join(parse_genai_models(genai_settings.models_json))
            if genai_settings
            else ""
        )
        context["genai_team_priority"] = (
            "\n".join(parse_genai_models(genai_settings.team_priority_json))
            if genai_settings
            else ""
        )
        # Until GenAI is saved, suggest the LiteLLM root already used by Jupyter AI.
        context["genai_default_url"] = (
            genai_settings.base_url
            if genai_settings
            else (jupyter_ai_settings.gateway_url if jupyter_ai_settings else "")
        )
        registry_record = await db.get(ModelContainerRegistrySettings, 1)
        try:
            registry_config = await effective_model_container_registry_config(db)
            context["model_container_registry_settings"] = {
                "managed": registry_record is not None,
                "enabled": registry_config.enabled,
                "registry_url": registry_config.registry_url,
                "username": registry_config.username,
                "has_password": bool(registry_config.password),
            }
            context["model_container_registry_error"] = ""
        except ModelContainerRegistryConfigurationError as exc:
            context["model_container_registry_settings"] = None
            context["model_container_registry_error"] = str(exc)
    elif section == "kubernetes":
        deploy_record = await db.get(DeployRegistrySettings, 1)
        try:
            deploy_config = await effective_deploy_registry_config(db)
            context["deploy_registry_settings"] = deploy_registry_settings_out(
                deploy_config, deploy_record
            )
            context["deploy_registry_error"] = ""
        except DeployRegistryConfigurationError as exc:
            context["deploy_registry_settings"] = None
            context["deploy_registry_error"] = str(exc)
        bamboo_record = await db.get(BambooSettings, 1)
        try:
            context["bamboo_settings"] = bamboo_settings_out(
                await effective_bamboo_config(db), bamboo_record
            )
            context["bamboo_error"] = ""
        except BambooConfigurationError as exc:
            context["bamboo_settings"] = None
            context["bamboo_error"] = str(exc)
    elif section == "system":
        context["session_timeout_minutes"] = await session_timeout_minutes(db)
        context["onboarding_settings"] = await db.get(OnboardingSettings, 1)
        download_settings = await db.get(DownloadSettings, 1)
        context.update(
            {
                "download_public_base_url": (
                    download_settings.public_base_url
                    if download_settings
                    else settings.DOWNLOAD_PUBLIC_BASE_URL
                ),
                "download_settings": download_settings,
                "download_agent_ca_uploaded": (
                    Path(settings.INGRESS_STAGING_ROOT) / "agent-ca.pem"
                ).is_file(),
                "https_default_hostname": settings.HTTPS_DEFAULT_HOSTNAME,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=context,
    )
