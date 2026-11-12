"""Tools shared by the MCP endpoint and the in-app assistant.

Every tool runs as one AI Factory user. Handlers call the same route
functions the web UI calls, so ownership checks, quotas and placement are
not reimplemented here. Two things are enforced in this module because
calling a route function directly skips its FastAPI dependencies:

* ``admin`` tools require an administrator;
* ``write`` tools require the caller to be allowed to change state (an MCP
  token with the "write" scope, or a confirmation in the assistant panel).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit_event
from app.models.user import User, UserRole

logger = logging.getLogger("aifactory.assistant")

MAX_ITEMS = 50
MAX_TEXT = 12000


class ToolError(Exception):
    """A tool call that failed in a way the caller should be told about."""

    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


Handler = Callable[[AsyncSession, User, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Handler
    args: type[BaseModel] = NoArgs
    admin: bool = False
    write: bool = False
    # Shown on the confirmation card of the assistant panel.
    title: str = ""

    @property
    def input_schema(self) -> dict:
        schema = self.args.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> None:
    TOOLS[tool.name] = tool


def is_admin(user: User) -> bool:
    return getattr(user.role, "value", user.role) == UserRole.ADMIN.value


def tools_for(user: User, *, allow_write: bool) -> list[Tool]:
    """Tools this user may see; hidden tools are also refused by call_tool."""
    return [
        tool
        for tool in TOOLS.values()
        if (not tool.admin or is_admin(user)) and (not tool.write or allow_write)
    ]


def _capped(items: list, limit: int = MAX_ITEMS) -> dict:
    items = list(items)
    result: dict[str, Any] = {"count": len(items), "items": items[:limit]}
    if len(items) > limit:
        result["truncated"] = True
    return result


def _pick(value: Any, *fields: str) -> dict:
    data = jsonable_encoder(value)
    return {name: data.get(name) for name in fields if data.get(name) not in (None, "", [])}


def _workspace(value: Any) -> dict:
    data = _pick(
        value,
        "id", "name", "description", "status", "template_id", "flavor_id",
        "auto_stop_minutes", "created_at", "last_started_at", "last_stopped_at",
        "error_message", "accelerator_model", "tags",
    )
    if data.get("id"):
        data["detail_page"] = f"/workspaces/{data['id']}"
    return data


def _tail(text: str, limit: int = MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else "…" + text[-limit:]


async def call_tool(
    db: AsyncSession,
    user: User,
    name: str,
    arguments: dict | None,
    *,
    allow_write: bool,
    source: str,
    src_ip: str = "",
) -> Any:
    """Run one tool as ``user`` and return JSON-safe data.

    Raises ToolError for unknown tools, refused calls, invalid arguments and
    failures reported by the underlying route.
    """
    tool = TOOLS.get(name)
    actor = user.username
    role = getattr(user.role, "value", str(user.role))
    if tool is None or (tool.admin and not is_admin(user)):
        # An admin tool is reported like an unknown one to a non-admin.
        if tool is not None:
            audit_event(
                "assistant.tool", outcome="denied", actor=actor, actor_role=role,
                src_ip=src_ip, target=name, source=source,
            )
        raise ToolError(f"Unknown tool: {name}", code="unknown_tool")
    if tool.write and not allow_write:
        audit_event(
            "assistant.tool", outcome="denied", actor=actor, actor_role=role,
            src_ip=src_ip, target=name, source=source,
        )
        raise ToolError(
            "This action is not permitted for this session.", code="write_not_allowed"
        )
    try:
        args = tool.args.model_validate(arguments or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ToolError(f"Invalid arguments: {problems}", code="invalid_arguments") from exc
    try:
        result = await tool.handler(db, user, args)
    except HTTPException as exc:
        # Drop whatever the failed route left pending; the session is reused
        # for the next tool call. Rollback expires loaded objects, so reload
        # the caller for whoever keeps using it.
        await db.rollback()
        await db.refresh(user)
        if tool.write:
            audit_event(
                "assistant.tool", outcome="failure", actor=actor, actor_role=role,
                src_ip=src_ip, target=name, source=source, status=exc.status_code,
            )
        raise ToolError(str(exc.detail), code=f"http_{exc.status_code}") from exc
    except Exception as exc:  # noqa: BLE001 - e.g. the workspace's worker is offline
        await db.rollback()
        await db.refresh(user)
        logger.warning("Tool %s failed: %s: %s", name, type(exc).__name__, exc)
        raise ToolError(f"The tool could not complete: {exc}"[:300], code="failed") from exc
    if tool.write:
        audit_event(
            "assistant.tool", actor=actor, actor_role=role, src_ip=src_ip,
            target=name, source=source,
            arguments=json.dumps(jsonable_encoder(args), ensure_ascii=False)[:500],
        )
    return jsonable_encoder(result)


# --- Read tools: the caller's own data ---------------------------------


async def _whoami(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    return {
        "username": user.username,
        "full_name": user.full_name,
        "role": getattr(user.role, "value", str(user.role)),
        "team": user.team,
        "directorate": user.organization_unit or user.directorate,
    }


async def _list_workspaces(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    from app.routes.workspace_routes import list_user_workspaces

    return _capped([_workspace(item) for item in await list_user_workspaces(user, db)])


class WorkspaceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=64, description="Workspace ID from list_workspaces.")


async def _get_workspace(db: AsyncSession, user: User, args: WorkspaceRef) -> dict:
    from app.routes.workspace_routes import get_workspace_detail

    return _workspace(await get_workspace_detail(args.workspace_id, user, db))


class WorkspaceLogs(WorkspaceRef):
    tail: int = Field(default=80, ge=1, le=300, description="Number of trailing log lines.")


async def _workspace_logs(db: AsyncSession, user: User, args: WorkspaceLogs) -> dict:
    from app.routes.workspace_routes import get_workspace_logs

    result = await get_workspace_logs(args.workspace_id, user, db, tail=args.tail)
    return {"workspace_id": args.workspace_id, "logs": _tail(result.get("logs", ""))}


async def _workspace_stats(db: AsyncSession, user: User, args: WorkspaceRef) -> Any:
    from app.routes.workspace_routes import get_single_workspace_stats

    return await get_single_workspace_stats(args.workspace_id, user, db)


async def _resource_usage(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app.routes.workspace_routes import get_resource_usage

    return await get_resource_usage(user, db)


async def _list_templates(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    from app.routes.workspace_routes import get_templates

    return _capped([
        _pick(item, "id", "name", "description", "category", "requires_accelerator")
        for item in await get_templates(db)
    ])


async def _list_flavors(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    from app.routes.workspace_routes import get_flavors

    return _capped([
        _pick(
            item, "id", "display_name", "cpus", "memory_display", "accelerator_count",
            "accelerator_display", "available", "availability_message",
        )
        for item in await get_flavors(db, tags=[])
    ])


async def _platform_status(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app.health import get_report, public_view

    report = await get_report(db, refresh=False)
    return report if is_admin(user) else public_view(report)


async def _genai_account(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app import genai

    return await genai.account_status(db, user)


async def _mlflow_overview(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app.routes.mlflow_routes import get_mlflow_overview

    data = jsonable_encoder(await get_mlflow_overview(user, db))
    if isinstance(data, dict) and isinstance(data.get("recent_runs"), list):
        data["recent_runs"] = [
            _pick(run.get("info", run), "run_id", "run_name", "experiment_id", "status", "start_time")
            for run in data["recent_runs"][:15]
        ]
    return data


class ModelSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=200, description="Optional name filter.")


async def _mlflow_models(db: AsyncSession, user: User, args: ModelSearch) -> Any:
    from app.routes.mlflow_routes import list_mlflow_models

    return await list_mlflow_models(user, db, search=args.search, page_token="")


async def _mlflow_experiments(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app.routes.mlflow_routes import list_mlflow_experiments

    return await list_mlflow_experiments(user, db, page_token="")


async def _mlflow_deployments(db: AsyncSession, user: User, _args: NoArgs) -> Any:
    from app.routes.mlflow_routes import list_mlflow_deployments

    return await list_mlflow_deployments(user, db)


# --- Read tools: administrators ----------------------------------------


async def _admin_workers(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    from app.routes.admin.nodes import list_nodes

    workers = []
    for node in jsonable_encoder(await list_nodes(user, db)):
        item = {
            name: node.get(name)
            for name in (
                "name", "status", "connected", "schedulable", "gpu_worker", "cpu_total",
                "cpu_percent", "memory_total_mb", "memory_used_mb", "disk_total_mb",
                "disk_used_mb", "active_containers_count", "agent_version", "tags",
                "last_seen_at",
            )
        }
        item["gpus"] = [
            {name: gpu.get(name) for name in ("model", "kind", "memory_mb", "utilization_percent", "healthy")}
            for gpu in (node.get("capabilities") or {}).get("accelerators", [])
            if isinstance(gpu, dict)
        ]
        workers.append(item)
    return _capped(workers)


class UserSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=100, description="Filter by username, name, team or directorate.")


async def _admin_users(db: AsyncSession, user: User, args: UserSearch) -> dict:
    from app.routes.admin.users import list_all_users

    needle = args.search.casefold()
    users = []
    for item in jsonable_encoder(await list_all_users(user, db)):
        row = {
            name: item.get(name)
            for name in (
                "username", "full_name", "team", "directorate", "organization_unit", "role",
                "is_active", "cpu_quota", "memory_mb_quota", "disk_mb_quota", "gpu_quota",
            )
        }
        if needle and not any(needle in str(value).casefold() for value in row.values()):
            continue
        users.append(row)
    return _capped(users)


async def _admin_workspaces(db: AsyncSession, user: User, _args: NoArgs) -> dict:
    from app.routes.admin.workspaces import list_all_workspaces

    rows = []
    for item in await list_all_workspaces(user, db):
        row = _workspace(item)
        row["user_id"] = item.user_id
        rows.append(row)
    return _capped(rows)


# --- Write tools ---------------------------------------------------------


class CreateWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=60, pattern=r"^[a-zA-Z0-9_\-\ ]+$")
    template_id: str = Field(description="Template ID from list_templates.")
    flavor_id: str = Field(description="Resource profile ID from list_flavors.")
    description: str = Field(default="", max_length=255)
    auto_stop_minutes: int = Field(default=0, ge=0, le=1440, description="Maximum runtime in minutes; 0 disables the limit.")


async def _create_workspace(db: AsyncSession, user: User, args: CreateWorkspace) -> dict:
    from app.routes.workspace_routes import create_workspace
    from app.schemas.workspace import WorkspaceCreate

    return _workspace(await create_workspace(WorkspaceCreate(**args.model_dump()), user, db))


async def _start_workspace(db: AsyncSession, user: User, args: WorkspaceRef) -> dict:
    from app.routes.workspace_routes import start_workspace_endpoint

    return _workspace(await start_workspace_endpoint(args.workspace_id, user, db))


async def _stop_workspace(db: AsyncSession, user: User, args: WorkspaceRef) -> dict:
    from app.routes.workspace_routes import stop_workspace_endpoint

    return _workspace(await stop_workspace_endpoint(args.workspace_id, user, db))


for _tool in (
    Tool("whoami", "Return the current user's name, role, team and directorate.", _whoami),
    Tool("list_workspaces", "List the current user's workspaces with status, template and resource profile.", _list_workspaces),
    Tool("get_workspace", "Return one workspace's details.", _get_workspace, WorkspaceRef),
    Tool("get_workspace_logs", "Return the last container log lines of a workspace. Useful to diagnose a workspace that fails to start.", _workspace_logs, WorkspaceLogs),
    Tool("get_workspace_stats", "Return live CPU, RAM, disk and uptime of a running workspace.", _workspace_stats, WorkspaceRef),
    Tool("get_resource_usage", "Return the current user's CPU/RAM/disk/GPU quota, how much is used and left, and total cluster usage.", _resource_usage),
    Tool("list_templates", "List the workspace templates (IDE environments) that can be created.", _list_templates),
    Tool("list_flavors", "List resource profiles (CPU, RAM, GPU) and whether each can be placed right now.", _list_flavors),
    Tool("get_platform_status", "Return the health of the platform components.", _platform_status),
    Tool("get_genai_account", "Return the current user's GenAI (LiteLLM) account status, models and spend. Never returns a key.", _genai_account),
    Tool("mlflow_overview", "Summarize the user's MLflow experiments, registered models and recent runs.", _mlflow_overview),
    Tool("mlflow_list_models", "List registered MLflow models visible to the user.", _mlflow_models, ModelSearch),
    Tool("mlflow_list_experiments", "List MLflow experiments visible to the user.", _mlflow_experiments),
    Tool("mlflow_list_deployments", "List the user's model deployments on AI Factory.", _mlflow_deployments),
    Tool("admin_list_workers", "Administrators only. List worker nodes with status, load and GPUs.", _admin_workers, admin=True),
    Tool("admin_list_users", "Administrators only. List users with team, directorate and effective quotas.", _admin_users, UserSearch, admin=True),
    Tool("admin_list_all_workspaces", "Administrators only. List every user's workspaces.", _admin_workspaces, admin=True),
    Tool("create_workspace", "Create and start a new workspace for the current user. Subject to the user's quota.", _create_workspace, CreateWorkspace, write=True, title="Çalışma alanı oluştur"),
    Tool("start_workspace", "Start a stopped workspace.", _start_workspace, WorkspaceRef, write=True, title="Çalışma alanını başlat"),
    Tool("stop_workspace", "Stop a running workspace. Its files are kept; CPU and RAM quota are released.", _stop_workspace, WorkspaceRef, write=True, title="Çalışma alanını durdur"),
):
    _register(_tool)
