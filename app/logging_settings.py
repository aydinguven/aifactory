"""Conversions between the stored logging policy, its API shapes and the
runtime ``LoggingConfig`` that app.logging_setup applies."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.log_forwarding import ForwardingConfig
from app.logging_setup import LoggingConfig
from app.models.logging_settings import LoggingSettings
from app.schemas.logging_settings import (
    LogForwardingFields,
    LoggingSettingsOut,
    LoggingSettingsUpdate,
    WorkerLoggingSettings,
)


def forwarding_config(fields: LogForwardingFields) -> ForwardingConfig:
    return ForwardingConfig(
        enabled=fields.forward_enabled,
        host=fields.forward_host,
        port=fields.forward_port,
        protocol=fields.forward_protocol,
        format=fields.forward_format,
        tls_verify=fields.forward_tls_verify,
        ca_pem=fields.forward_ca_pem,
        audit=fields.forward_audit,
        app=fields.forward_app,
        app_min_level=fields.forward_app_min_level,
        access=fields.forward_access,
    )


def _fields(record: LoggingSettings) -> dict:
    return {
        "retention_days": record.retention_days,
        "max_total_mb": record.max_total_mb,
        "log_level": record.log_level,
        "forward_enabled": record.forward_enabled,
        "forward_host": record.forward_host,
        "forward_port": record.forward_port,
        "forward_protocol": record.forward_protocol,
        "forward_format": record.forward_format,
        "forward_tls_verify": record.forward_tls_verify,
        "forward_ca_pem": record.forward_ca_pem or "",
        "forward_audit": record.forward_audit,
        "forward_app": record.forward_app,
        "forward_app_min_level": record.forward_app_min_level,
        "forward_access": record.forward_access,
    }


def settings_out(record: LoggingSettings | None) -> LoggingSettingsOut:
    if record is None:
        return LoggingSettingsOut(managed=False, revision=0)
    return LoggingSettingsOut(
        managed=True,
        revision=record.revision,
        updated_at=record.updated_at,
        updated_by=record.updated_by or "",
        **_fields(record),
    )


def worker_settings(record: LoggingSettings | None) -> WorkerLoggingSettings:
    if record is None:
        return WorkerLoggingSettings(managed=False)
    return WorkerLoggingSettings(managed=True, revision=record.revision, **_fields(record))


def runtime_config(value: LoggingSettingsUpdate, *, managed: bool, revision: int) -> LoggingConfig:
    if not managed:
        return LoggingConfig()
    return LoggingConfig(
        managed=True,
        revision=revision,
        retention_days=value.retention_days,
        max_total_mb=value.max_total_mb,
        log_level=value.log_level,
        forwarding=forwarding_config(value),
    )


def apply_update(record: LoggingSettings, update: LoggingSettingsUpdate, actor: str) -> list[str]:
    """Copy ``update`` onto ``record`` and return the names of changed fields."""
    changed = []
    for name, value in update.model_dump().items():
        if getattr(record, name) != value:
            setattr(record, name, value)
            changed.append(name)
    record.revision = (record.revision or 0) + 1
    record.updated_by = actor
    return changed


async def stored_runtime_config(db: AsyncSession) -> LoggingConfig:
    record = await db.get(LoggingSettings, 1)
    if record is None:
        return LoggingConfig()
    return runtime_config(
        LoggingSettingsUpdate(**_fields(record)), managed=True, revision=record.revision
    )
