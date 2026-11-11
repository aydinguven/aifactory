"""Admin API: local log retention and forwarding to a syslog collector."""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.agents.manager import agent_manager
from app.audit import audit_event, client_ip, mark_audited
from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.ingress_settings import IngressConfigurationError, validate_agent_ca_bundle
from app.log_forwarding import send_test_event
from app.logging_settings import (
    apply_update,
    forwarding_config,
    runtime_config,
    settings_out,
)
from app.logging_setup import apply_config, component_hostname, status as logging_status
from app.models.logging_settings import LoggingSettings
from app.models.node import Node
from app.models.user import User
from app.schemas.logging_settings import (
    LogForwardingFields,
    LogForwardingTestRequest,
    LogForwardingTestResult,
    LoggingSettingsOut,
    LoggingSettingsUpdate,
)

router = APIRouter()


def _validated_ca(fields: LogForwardingFields) -> None:
    if not fields.forward_ca_pem:
        return
    try:
        validate_agent_ca_bundle(fields.forward_ca_pem.encode("utf-8"), label="Toplayıcı CA paketi")
    except IngressConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/logging-settings", response_model=LoggingSettingsOut)
async def get_logging_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return settings_out(await db.get(LoggingSettings, 1))


@router.put("/logging-settings", response_model=LoggingSettingsOut)
async def update_logging_settings(
    update: LoggingSettingsUpdate,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _validated_ca(update)
    record = await db.get(LoggingSettings, 1)
    if record is None:
        record = LoggingSettings(id=1)
        db.add(record)
    changed = apply_update(record, update, admin.username)
    await db.commit()
    await db.refresh(record)
    # The controller applies the policy now; workers pick it up within 30 s.
    # A new destination waits briefly for the old sender, so not on the loop.
    await asyncio.to_thread(
        apply_config, runtime_config(update, managed=True, revision=record.revision)
    )
    mark_audited(request)
    audit_event(
        "logging.settings.update",
        actor=admin.username,
        actor_role=admin.role.value,
        src_ip=client_ip(request),
        target="logging_settings",
        changed=changed,
        revision=record.revision,
    )
    return settings_out(record)


@router.post("/logging-settings/test", response_model=LogForwardingTestResult)
async def test_log_forwarding(
    request: LogForwardingTestRequest,
    response: Response,
    admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Send one test event from the controller with the unsaved form values."""
    response.headers["Cache-Control"] = "no-store"
    if not request.forward_host:
        raise HTTPException(status_code=422, detail="Toplayıcı adresi zorunludur.")
    _validated_ca(request)
    component, hostname = component_hostname()
    result = await asyncio.to_thread(
        send_test_event,
        forwarding_config(request),
        component=component,
        hostname=hostname,
        version=__version__,
        actor=admin.username,
    )
    return LogForwardingTestResult(**result)


@router.get("/logging-status")
async def get_logging_status(
    response: Response,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Controller and per-worker logging state, from the latest heartbeats."""
    response.headers["Cache-Control"] = "no-store"
    record = await db.get(LoggingSettings, 1)
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    workers = []
    for node in nodes:
        try:
            capabilities = json.loads(node.capabilities_json or "{}")
        except ValueError:
            capabilities = {}
        reported = capabilities.get("logging") if isinstance(capabilities, dict) else None
        workers.append(
            {
                "node_id": node.id,
                "name": node.name,
                "hostname": node.hostname or "",
                "connected": agent_manager.is_connected(node.id),
                "agent_version": node.agent_version or "",
                "logging": reported if isinstance(reported, dict) else None,
            }
        )
    return {
        "revision": record.revision if record else 0,
        "controller": logging_status(),
        "workers": workers,
    }
