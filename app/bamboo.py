"""Bamboo plan (AIMLOPS-RETAG) queued after a Kubernetes image release is published.

The plan re-tags the image for the platform team and opens the Jira tasks.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.kubernetes_publish import image_repository
from app.models.bamboo_settings import BambooSettings
from app.schemas.kubernetes import (
    BAMBOO_PLAN_KEY_PATTERN,
    BambooSettingsOut,
    BambooSettingsUpdate,
    normalize_bamboo_base_url,
)
from app.security.secrets import SecretDecryptionError, decrypt_secret

DEFAULT_PLAN_KEY = "AIMLOPS-RETAG"
BAMBOO_TIMEOUT_SECONDS = 30.0


class BambooConfigurationError(ValueError):
    pass


class BambooTriggerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BambooConfig:
    managed: bool
    enabled: bool
    base_url: str
    plan_key: str
    token: str
    tls_verify: bool = True


def validate_bamboo_config(config: BambooConfig, *, require_enabled: bool = False) -> None:
    if require_enabled and not config.enabled:
        raise BambooConfigurationError("Bamboo entegrasyonu yapılandırılmamış veya etkin değil.")
    if config.enabled and not (config.base_url and config.plan_key and config.token):
        raise BambooConfigurationError(
            "Bamboo etkinleştirildiğinde adres, plan anahtarı ve token zorunludur."
        )
    if config.base_url:
        try:
            normalized = normalize_bamboo_base_url(config.base_url)
        except ValueError as exc:
            raise BambooConfigurationError(str(exc)) from exc
        if normalized != config.base_url:
            raise BambooConfigurationError("Bamboo adresi geçersiz.")
    if config.plan_key and not BAMBOO_PLAN_KEY_PATTERN.fullmatch(config.plan_key):
        raise BambooConfigurationError("Bamboo plan anahtarı geçersiz.")


def config_from_record(record: BambooSettings) -> BambooConfig:
    try:
        token = decrypt_secret(record.encrypted_token)
    except SecretDecryptionError as exc:
        raise BambooConfigurationError(
            "Kayıtlı Bamboo tokenı çözülemedi; yeniden kaydedin."
        ) from exc
    config = BambooConfig(
        managed=True,
        enabled=record.enabled,
        base_url=record.base_url.strip(),
        plan_key=record.plan_key.strip(),
        token=token,
        tls_verify=record.tls_verify,
    )
    validate_bamboo_config(config)
    return config


async def effective_bamboo_config(db: AsyncSession) -> BambooConfig:
    record = await db.get(BambooSettings, 1)
    if record is None:
        return BambooConfig(
            managed=False, enabled=False, base_url="", plan_key=DEFAULT_PLAN_KEY, token=""
        )
    return config_from_record(record)


def config_from_update(update: BambooSettingsUpdate, current: BambooConfig) -> BambooConfig:
    config = BambooConfig(
        managed=True,
        enabled=update.enabled,
        base_url=update.base_url,
        plan_key=update.plan_key,
        token=current.token if update.token is None else update.token,
        tls_verify=update.tls_verify,
    )
    validate_bamboo_config(config)
    return config


def bamboo_settings_out(config: BambooConfig, record: BambooSettings | None) -> BambooSettingsOut:
    return BambooSettingsOut(
        managed=record is not None,
        enabled=config.enabled,
        base_url=config.base_url,
        plan_key=config.plan_key,
        has_token=bool(config.token),
        tls_verify=config.tls_verify,
        updated_at=record.updated_at if record else None,
    )


def retag_variables(rfs_code: str, app_name: str, version: str) -> dict[str, str]:
    """Plan variables; Bamboo requires upper-case package and lower-case image names."""
    return {
        "bamboo.variable.pkg.groupid": rfs_code.upper(),
        "bamboo.variable.pkg.name": app_name.upper(),
        "bamboo.variable.pkg.vers": version,
        "bamboo.variable.pkg.img.name": f"{image_repository(rfs_code, app_name)}:{version}".lower(),
    }


async def queue_retag_build(config: BambooConfig, variables: dict[str, str]) -> tuple[str, str]:
    """Queue the plan; returns the build result key and its browse URL."""
    validate_bamboo_config(config, require_enabled=True)
    url = f"{config.base_url}/rest/api/latest/queue/{config.plan_key}"
    headers = {
        "Authorization": f"Bearer {config.token}",
        "Accept": "application/json",
        # Atlassian XSRF protection rejects form posts to REST without this.
        "X-Atlassian-Token": "no-check",
    }
    try:
        async with httpx.AsyncClient(
            timeout=BAMBOO_TIMEOUT_SECONDS, follow_redirects=False, verify=config.tls_verify
        ) as client:
            response = await client.post(url, data=variables, headers=headers)
    except httpx.HTTPError as exc:
        raise BambooTriggerError(f"Bamboo'ya bağlanılamadı: {exc}") from exc
    if response.status_code not in (200, 201):
        try:
            detail = str(response.json().get("message") or "")
        except ValueError:
            detail = response.text
        raise BambooTriggerError(
            f"Bamboo HTTP {response.status_code} döndürdü: {detail.strip()[:300]}"
        )
    try:
        result_key = str(response.json().get("buildResultKey") or "")
    except ValueError:
        result_key = ""
    result_url = f"{config.base_url}/browse/{result_key}" if result_key else ""
    return result_key, result_url
