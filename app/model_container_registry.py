from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.model_container_registry_settings import (
    ModelContainerRegistrySettings,
)
from app.schemas.model_container_registry import (
    ModelContainerRegistrySettingsUpdate,
)
from app.security.secrets import SecretDecryptionError, decrypt_secret


class ModelContainerRegistryConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelContainerRegistryConfig:
    managed: bool
    enabled: bool
    registry_url: str
    username: str
    password: str


def validate_model_container_registry_config(
    config: ModelContainerRegistryConfig,
    *,
    require_enabled: bool = False,
) -> None:
    if require_enabled and not config.enabled:
        raise ModelContainerRegistryConfigurationError(
            "Model Container Registry etkin değil."
        )
    if config.enabled and not config.registry_url:
        raise ModelContainerRegistryConfigurationError(
            "Model Container Registry etkinleştirildiğinde adres zorunludur."
        )
    if config.registry_url:
        if (
            "://" in config.registry_url
            or "@" in config.registry_url
            or "?" in config.registry_url
            or "#" in config.registry_url
            or any(ord(character) < 33 for character in config.registry_url)
        ):
            raise ModelContainerRegistryConfigurationError(
                "Registry adresi geçerli bir host:port[/namespace] image prefix olmalıdır."
            )
        host, *path_parts = config.registry_url.split("/")
        if (
            not host
            or host in {".", ".."}
            or host.startswith(":")
            or any(
                not part or part in {".", ".."} or ":" in part
                for part in path_parts
            )
        ):
            raise ModelContainerRegistryConfigurationError(
                "Registry adresi geçerli bir host:port[/namespace] image prefix olmalıdır."
            )
    if bool(config.username) != bool(config.password):
        raise ModelContainerRegistryConfigurationError(
            "Registry kullanıcı adı ve parolası birlikte girilmelidir."
        )
    for value in (config.registry_url, config.username, config.password):
        if any(ord(character) < 32 for character in value):
            raise ModelContainerRegistryConfigurationError(
                "Registry ayarları kontrol karakteri içeremez."
            )


def environment_model_container_registry_config() -> ModelContainerRegistryConfig:
    preferred_url = settings.MODEL_CONTAINER_REGISTRY_URL.strip().rstrip("/")
    if preferred_url:
        registry_url = preferred_url
        username = settings.MODEL_CONTAINER_REGISTRY_USERNAME.strip()
        password = settings.MODEL_CONTAINER_REGISTRY_PASSWORD
    else:
        registry_url = settings.AIFACTORY_REGISTRY_URL.strip().rstrip("/")
        username = settings.AIFACTORY_REGISTRY_USERNAME.strip()
        password = settings.AIFACTORY_REGISTRY_PASSWORD
    config = ModelContainerRegistryConfig(
        managed=False,
        enabled=bool(registry_url),
        registry_url=registry_url,
        username=username,
        password=password,
    )
    validate_model_container_registry_config(config)
    return config


def config_from_record(
    record: ModelContainerRegistrySettings,
) -> ModelContainerRegistryConfig:
    try:
        password = decrypt_secret(record.encrypted_password)
    except SecretDecryptionError as exc:
        raise ModelContainerRegistryConfigurationError(
            "Kayıtlı Model Container Registry parolası çözülemedi; yeniden kaydedin."
        ) from exc
    config = ModelContainerRegistryConfig(
        managed=True,
        enabled=record.enabled,
        registry_url=record.registry_url.strip().rstrip("/"),
        username=record.username.strip(),
        password=password,
    )
    validate_model_container_registry_config(config)
    return config


async def effective_model_container_registry_config(
    db: AsyncSession,
) -> ModelContainerRegistryConfig:
    record = await db.get(ModelContainerRegistrySettings, 1)
    return config_from_record(record) if record else environment_model_container_registry_config()


def config_from_update(
    update: ModelContainerRegistrySettingsUpdate,
    current: ModelContainerRegistryConfig,
) -> ModelContainerRegistryConfig:
    password = current.password if update.password is None else update.password
    config = ModelContainerRegistryConfig(
        managed=True,
        enabled=update.enabled,
        registry_url=update.registry_url,
        username=update.username,
        password=password,
    )
    validate_model_container_registry_config(config)
    return config


def registry_api_url(registry_url: str) -> str:
    host = registry_url.split("/", 1)[0]
    return f"https://{host}/v2/"


async def test_model_container_registry(
    config: ModelContainerRegistryConfig,
) -> dict[str, object]:
    validate_model_container_registry_config(config)
    if not config.registry_url:
        raise ModelContainerRegistryConfigurationError(
            "Registry bağlantı testi için adres zorunludur."
        )
    auth = (config.username, config.password) if config.username else None
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            auth=auth,
        ) as client:
            response = await client.get(registry_api_url(config.registry_url))
        latency_ms = round((time.monotonic() - started) * 1000)
        ok = response.status_code == 200
        return {
            "ok": ok,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "message": (
                "Registry v2 API erişim ve kimlik doğrulama kontrolü başarılı; "
                "push/pull/delete yetkileri gerçek build sırasında doğrulanır."
                if ok
                else f"Registry v2 API HTTP {response.status_code} döndürdü."
            ),
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "message": f"Registry bağlantısı kurulamadı: {exc}",
        }
