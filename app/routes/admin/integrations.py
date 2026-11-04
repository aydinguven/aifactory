"""Admin API: model container registry, Jupyter AI and MLflow server settings."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.node import Node
from app.models.mlflow_deployment import MlflowModelBuild
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.models.kilo_code_settings import KiloCodeSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.model_container_registry_settings import (
    ModelContainerRegistrySettings,
)
from app.orchestrator.admission import admission_transaction
from app.agents.manager import agent_manager
from app.schemas.jupyter_ai_settings import (
    JupyterAiConnectivityTargetResult,
    JupyterAiConnectivityTestRequest,
    JupyterAiConnectivityTestResult,
    JupyterAiModel,
    JupyterAiSettingsOut,
    JupyterAiSettingsUpdate,
)
from app.schemas.mlflow import MlflowServerSettingsOut, MlflowServerSettingsUpdate
from app.schemas.kilo_code_settings import KiloCodeSettingsOut, KiloCodeSettingsUpdate
from app.kilo_code import (
    DEFAULT_INDEXING_DIMENSION,
    DEFAULT_INDEXING_MODEL,
    parse_model_ids,
    parse_skills_urls,
)
from app.schemas.model_container_registry import (
    ModelContainerRegistrySettingsOut,
    ModelContainerRegistrySettingsUpdate,
    ModelContainerRegistryTargetResult,
    ModelContainerRegistryTestRequest,
    ModelContainerRegistryTestResult,
)
from app.model_container_registry import (
    ModelContainerRegistryConfigurationError,
    ModelContainerRegistryConfig,
    config_from_record as model_registry_config_from_record,
    config_from_update as model_registry_config_from_update,
    effective_model_container_registry_config,
    test_model_container_registry,
)
from app.integrations.mlflow import MlflowConfig, validate_config as validate_mlflow_config
from app.jupyter_ai import (
    default_model_catalog,
    normalize_ca_bundle,
    parse_model_catalog,
)
from app.ingress_settings import IngressConfigurationError, validate_agent_ca_bundle
from app.config import settings
from app.security.secrets import (
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
)
from app.release_catalog import semantic_version

router = APIRouter()


def _mlflow_server_settings_out(
    record: MlflowServerSettings | None,
) -> MlflowServerSettingsOut:
    if record is None:
        return MlflowServerSettingsOut(
            managed=False,
            enabled=False,
            base_url="",
            validate_tls=True,
            ca_cert_file="",
            timeout_seconds=10,
            updated_at=None,
        )
    return MlflowServerSettingsOut(
        managed=True,
        enabled=record.enabled,
        base_url=record.base_url,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        timeout_seconds=record.timeout_seconds,
        updated_at=record.updated_at,
    )


def _jupyter_ai_settings_out(
    record: JupyterAiSettings | None,
) -> JupyterAiSettingsOut:
    if record is None:
        return JupyterAiSettingsOut(
            managed=False,
            enabled=False,
            cline_enabled=True,
            gateway_url="",
            model_id="",
            gateway_model_discovery=False,
            models=[JupyterAiModel(**item) for item in default_model_catalog()],
            has_shared_token=False,
            updated_at=None,
        )
    return JupyterAiSettingsOut(
        managed=True,
        enabled=record.enabled,
        # Kept in the response for compatibility with 3.6.x workers. Cline is
        # now part of every VS Code image and cannot be disabled separately.
        cline_enabled=True,
        gateway_url=record.gateway_url,
        model_id=record.model_id,
        gateway_model_discovery=record.gateway_model_discovery,
        models=[
            JupyterAiModel(**item)
            for item in parse_model_catalog(
                record.model_catalog_json, record.model_id
            )
        ],
        has_shared_token=bool(record.encrypted_shared_token),
        ca_bundle_pem=record.ca_bundle_pem or "",
        updated_at=record.updated_at,
    )


def _validated_ca_bundle(value: str) -> str:
    """Parse every certificate and return the canonical bundle, or raise 422."""
    if not value.strip():
        return ""
    try:
        validate_agent_ca_bundle(value.strip().encode("utf-8"), label="Gateway CA paketi")
    except IngressConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    bundle = normalize_ca_bundle(value)
    if not bundle:
        raise HTTPException(
            status_code=422,
            detail="Gateway CA paketi yalnızca PEM sertifika blokları içermelidir.",
        )
    return bundle


def _model_container_registry_settings_out(
    config: ModelContainerRegistryConfig,
    record: ModelContainerRegistrySettings | None,
) -> ModelContainerRegistrySettingsOut:
    return ModelContainerRegistrySettingsOut(
        managed=record is not None,
        enabled=config.enabled,
        registry_url=config.registry_url,
        username=config.username,
        has_password=bool(config.password),
        updated_at=record.updated_at if record else None,
    )


async def _model_registry_current_for_update(
    db: AsyncSession,
    record: ModelContainerRegistrySettings | None,
    submitted_password: str | None,
) -> ModelContainerRegistryConfig:
    try:
        return await effective_model_container_registry_config(db)
    except ModelContainerRegistryConfigurationError:
        if submitted_password is None:
            raise
        fallback_url = (
            record.registry_url
            if record
            else (
                settings.MODEL_CONTAINER_REGISTRY_URL
                or settings.DEVCLOUD_REGISTRY_URL
            )
        )
        return ModelContainerRegistryConfig(
            managed=record is not None,
            enabled=False,
            registry_url=fallback_url.strip().rstrip("/"),
            username=record.username if record else "",
            password="",
        )


@router.get(
    "/model-container-registry-settings",
    response_model=ModelContainerRegistrySettingsOut,
)
async def get_model_container_registry_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the effective generated-model image destination without its password."""
    record = await db.get(ModelContainerRegistrySettings, 1)
    try:
        config = (
            model_registry_config_from_record(record)
            if record
            else await effective_model_container_registry_config(db)
        )
    except ModelContainerRegistryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _model_container_registry_settings_out(config, record)


@router.put(
    "/model-container-registry-settings",
    response_model=ModelContainerRegistrySettingsOut,
)
async def update_model_container_registry_settings(
    update: ModelContainerRegistrySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Encrypt and store the destination used for generated model images."""
    try:
        async with admission_transaction(db):
            record = await db.get(ModelContainerRegistrySettings, 1)
            current = await _model_registry_current_for_update(
                db, record, update.password
            )
            candidate = model_registry_config_from_update(update, current)
            if candidate.registry_url != current.registry_url:
                build_count = (
                    await db.execute(select(func.count(MlflowModelBuild.id)))
                ).scalar_one()
                if build_count:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Model image kayıtları varken registry adresi değiştirilemez. "
                            "Önce model deployment ve image kayıtlarını temizleyin."
                        ),
                    )
            if record is None:
                record = ModelContainerRegistrySettings(id=1)
                record.encrypted_password = encrypt_secret(candidate.password)
            elif update.password is not None:
                record.encrypted_password = encrypt_secret(candidate.password)
            record.enabled = candidate.enabled
            record.registry_url = candidate.registry_url
            record.username = candidate.username
            record.updated_at = datetime.now(timezone.utc)
            db.add(record)
    except ModelContainerRegistryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.refresh(record)
    return _model_container_registry_settings_out(
        model_registry_config_from_record(record), record
    )


@router.post(
    "/model-container-registry-settings/test",
    response_model=ModelContainerRegistryTestResult,
)
async def test_model_container_registry_settings(
    request: ModelContainerRegistryTestRequest,
    response: Response,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Test unsaved registry settings from the controller and every enabled worker."""
    response.headers["Cache-Control"] = "no-store"
    try:
        record = await db.get(ModelContainerRegistrySettings, 1)
        current = await _model_registry_current_for_update(
            db, record, request.password
        )
        candidate = model_registry_config_from_update(
            ModelContainerRegistrySettingsUpdate(
                enabled=True,
                registry_url=request.registry_url,
                username=request.username,
                password=request.password,
            ),
            current,
        )
    except ModelContainerRegistryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    controller_result = await test_model_container_registry(candidate)
    targets = [
        ModelContainerRegistryTargetResult(
            target_id="controller",
            target_name="Controller",
            target_kind="controller",
            **controller_result,
        )
    ]
    nodes = (
        await db.execute(
            select(Node)
            .where(
                Node.enabled.is_(True),
                Node.schedulable.is_(True),
            )
            .order_by(Node.name)
        )
    ).scalars().all()

    async def test_worker(node: Node) -> ModelContainerRegistryTargetResult:
        current_version = semantic_version(node.agent_version)
        minimum_version = semantic_version("3.7.2")
        if (
            current_version is None
            or minimum_version is None
            or current_version < minimum_version
        ):
            return ModelContainerRegistryTargetResult(
                target_id=node.id,
                target_name=node.name,
                target_kind="worker",
                ok=False,
                message="Worker 3.7.2+ olmalıdır; kimlik bilgisi gönderilmedi.",
            )
        if not agent_manager.is_connected(node.id):
            return ModelContainerRegistryTargetResult(
                target_id=node.id,
                target_name=node.name,
                target_kind="worker",
                ok=False,
                message="Worker çevrimdışı veya controller tunnel'ına bağlı değil.",
            )
        try:
            connection = agent_manager.get(node.id)
            if candidate.password and not connection.confidential_for_secrets:
                return ModelContainerRegistryTargetResult(
                    target_id=node.id,
                    target_name=node.name,
                    target_kind="worker",
                    ok=False,
                    message=(
                        "Registry parolası yalnızca WSS veya loopback worker tunnel'ı "
                        "üzerinden gönderilebilir."
                    ),
                )
            result = await connection.request(
                "image.registry.test",
                {
                    "registry_url": candidate.registry_url,
                    "registry_username": candidate.username,
                    "registry_password": candidate.password,
                },
                timeout=20,
            )
            return ModelContainerRegistryTargetResult(
                target_id=node.id,
                target_name=node.name,
                target_kind="worker",
                ok=result.get("ok") is True,
                status_code=result.get("status_code"),
                latency_ms=result.get("latency_ms"),
                message=str(result.get("message") or "Worker yanıt vermedi."),
            )
        except Exception as exc:
            return ModelContainerRegistryTargetResult(
                target_id=node.id,
                target_name=node.name,
                target_kind="worker",
                ok=False,
                message=f"Worker testi çalıştırılamadı: {exc}",
            )

    targets.extend(await asyncio.gather(*(test_worker(node) for node in nodes)))
    return ModelContainerRegistryTestResult(
        ok=all(target.ok for target in targets),
        registry_url=candidate.registry_url,
        targets=targets,
    )


@router.get(
    "/jupyter-ai-settings",
    response_model=JupyterAiSettingsOut,
)
async def get_jupyter_ai_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return central Jupyter AI settings without exposing the shared token."""
    return _jupyter_ai_settings_out(await db.get(JupyterAiSettings, 1))


@router.put(
    "/jupyter-ai-settings",
    response_model=JupyterAiSettingsOut,
)
async def update_jupyter_ai_settings(
    update: JupyterAiSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Encrypt and store the configuration distributed to enrolled workers."""
    record = await db.get(JupyterAiSettings, 1)
    has_existing_token = bool(record and record.encrypted_shared_token)
    if update.enabled and (
        (update.shared_token is None and not has_existing_token)
        or update.shared_token == ""
    ):
        raise HTTPException(
            status_code=422,
            detail="Workspace AI etkinleştirildiğinde ortak gateway API anahtarı zorunludur.",
        )
    if record is None:
        record = JupyterAiSettings(id=1)
    current_models = parse_model_catalog(
        record.model_catalog_json, record.model_id
    )
    effective_models = (
        [model.model_dump() for model in update.models]
        if update.models is not None
        else current_models
    )
    if not effective_models:
        effective_models = default_model_catalog()
    if update.model_id and update.model_id not in {
        model["model_id"] for model in effective_models
    }:
        effective_models.insert(
            0,
            {
                "model_id": update.model_id,
                "name": update.model_id,
                "description": "Default",
            },
        )
    record.enabled = update.enabled
    # Preserve the legacy column during rolling upgrades, but never let an old
    # client disable the Cline copy baked into VS Code workspace images.
    record.cline_enabled = True
    record.gateway_url = update.gateway_url
    record.model_id = update.model_id
    record.gateway_model_discovery = update.gateway_model_discovery
    record.model_catalog_json = json.dumps(effective_models, ensure_ascii=False)
    if update.shared_token is not None:
        record.encrypted_shared_token = encrypt_secret(update.shared_token)
    if update.ca_bundle_pem is not None:
        record.ca_bundle_pem = _validated_ca_bundle(update.ca_bundle_pem)
    elif record.ca_bundle_pem is None:
        record.ca_bundle_pem = ""
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _jupyter_ai_settings_out(record)


@router.post(
    "/jupyter-ai-settings/test",
    response_model=JupyterAiConnectivityTestResult,
)
async def test_jupyter_ai_settings(
    request: JupyterAiConnectivityTestRequest,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Run one minimal LiteLLM inference from every enabled workspace worker."""
    record = await db.get(JupyterAiSettings, 1)
    if record is None or not record.enabled:
        raise HTTPException(
            status_code=409,
            detail="Once etkin Jupyter AI ayarlarini kaydedin.",
        )
    catalog = parse_model_catalog(record.model_catalog_json, record.model_id)
    if request.model_id not in {item["model_id"] for item in catalog}:
        raise HTTPException(
            status_code=422,
            detail="Test modeli kayitli Jupyter AI katalogunda bulunmuyor.",
        )
    try:
        shared_token = decrypt_secret(record.encrypted_shared_token)
    except SecretDecryptionError as exc:
        raise HTTPException(
            status_code=500,
            detail="Jupyter AI ortak tokeni cozulemedi.",
        ) from exc
    if not shared_token:
        raise HTTPException(
            status_code=409,
            detail="Jupyter AI ortak gateway tokeni kayitli degil.",
        )

    node_result = await db.execute(
        select(Node).where(Node.enabled.is_(True)).order_by(Node.name)
    )
    nodes = list(node_result.scalars().all())
    if not nodes:
        raise HTTPException(
            status_code=409,
            detail="Test edilecek etkin worker bulunamadi.",
        )

    async def test_node(node: Node) -> JupyterAiConnectivityTargetResult:
        if not agent_manager.is_connected(node.id):
            return JupyterAiConnectivityTargetResult(
                node_id=node.id,
                node_name=node.name,
                ok=False,
                model_id=request.model_id,
                message="Worker cevrimdisi veya controller tunnel'ina bagli degil.",
            )
        try:
            result = await agent_manager.get(node.id).request(
                "system.jupyter_ai_test",
                {
                    "gateway_url": record.gateway_url,
                    "model_id": request.model_id,
                    "shared_token": shared_token,
                    "ca_bundle_pem": record.ca_bundle_pem or "",
                },
                timeout=40,
            )
            return JupyterAiConnectivityTargetResult(
                node_id=node.id,
                node_name=node.name,
                ok=result.get("ok") is True,
                model_id=request.model_id,
                status_code=result.get("status_code"),
                latency_ms=result.get("latency_ms"),
                message=str(result.get("message") or "Worker yanit vermedi."),
            )
        except Exception as exc:
            return JupyterAiConnectivityTargetResult(
                node_id=node.id,
                node_name=node.name,
                ok=False,
                model_id=request.model_id,
                message=f"Worker testi calistirilamadi: {exc}",
            )

    workers = list(await asyncio.gather(*(test_node(node) for node in nodes)))
    return JupyterAiConnectivityTestResult(
        ok=bool(workers) and all(worker.ok for worker in workers),
        model_id=request.model_id,
        workers=workers,
    )


def _kilo_code_status(
    record: KiloCodeSettings | None,
    workspace_ai: JupyterAiSettings | None,
) -> tuple[bool, str]:
    if record is None or not record.enabled:
        return False, ""
    if workspace_ai is None or not workspace_ai.enabled or not workspace_ai.gateway_url:
        return False, (
            "Workspace AI etkin değil; Kilo Code gateway URL'sini ve model "
            "kataloğunu oradan alır, bu yüzden workspace'lere yapılandırma gönderilmiyor."
        )
    warnings = []
    if not record.catalog_url:
        warnings.append("Katalog URL boş; kilo-catalog ve kilo-install MCP sunucuları eklenmez.")
    if record.indexing_enabled and not record.encrypted_indexing_key:
        warnings.append("Kod indeksleme için API anahtarı kayıtlı değil; indeksleme kapalı kalır.")
    return True, " ".join(warnings)


def _kilo_code_settings_out(
    record: KiloCodeSettings | None,
    workspace_ai: JupyterAiSettings | None,
) -> KiloCodeSettingsOut:
    active, warning = _kilo_code_status(record, workspace_ai)
    if record is None:
        return KiloCodeSettingsOut(
            managed=False,
            enabled=False,
            catalog_url="",
            has_catalog_token=False,
            skills_urls=[],
            model_ids=[],
            indexing_enabled=True,
            indexing_model=DEFAULT_INDEXING_MODEL,
            indexing_dimension=DEFAULT_INDEXING_DIMENSION,
            has_indexing_key=False,
            active=active,
            warning=warning,
            updated_at=None,
        )
    return KiloCodeSettingsOut(
        managed=True,
        enabled=record.enabled,
        catalog_url=record.catalog_url,
        has_catalog_token=bool(record.encrypted_catalog_token),
        skills_urls=parse_skills_urls(record.skills_urls_json),
        model_ids=parse_model_ids(record.model_ids_json),
        indexing_enabled=record.indexing_enabled,
        indexing_model=record.indexing_model,
        indexing_dimension=record.indexing_dimension,
        has_indexing_key=bool(record.encrypted_indexing_key),
        active=active,
        warning=warning,
        updated_at=record.updated_at,
    )


@router.get(
    "/kilo-code-settings",
    response_model=KiloCodeSettingsOut,
)
async def get_kilo_code_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return Kilo Code settings without exposing the catalog token or indexing key."""
    return _kilo_code_settings_out(
        await db.get(KiloCodeSettings, 1), await db.get(JupyterAiSettings, 1)
    )


@router.put(
    "/kilo-code-settings",
    response_model=KiloCodeSettingsOut,
)
async def update_kilo_code_settings(
    update: KiloCodeSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Store Kilo Code settings; they apply to newly created or restarted workspaces."""
    record = await db.get(KiloCodeSettings, 1)
    workspace_ai = await db.get(JupyterAiSettings, 1)
    has_existing_key = bool(record and record.encrypted_indexing_key)
    if update.enabled and update.indexing_enabled and (
        (update.indexing_key is None and not has_existing_key)
        or update.indexing_key == ""
    ):
        raise HTTPException(
            status_code=422,
            detail="Kod indeksleme etkinken indeksleme API anahtarı zorunludur.",
        )
    if update.model_ids:
        catalog_ids = {
            item["model_id"]
            for item in parse_model_catalog(
                workspace_ai.model_catalog_json if workspace_ai else "",
                workspace_ai.model_id if workspace_ai else "",
            )
        }
        unknown = [model_id for model_id in update.model_ids if model_id not in catalog_ids]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Workspace AI model kataloğunda olmayan model: "
                    + ", ".join(unknown)
                ),
            )
    if record is None:
        record = KiloCodeSettings(id=1)
    record.enabled = update.enabled
    record.catalog_url = update.catalog_url
    record.skills_urls_json = json.dumps(update.skills_urls, ensure_ascii=False)
    record.model_ids_json = json.dumps(update.model_ids, ensure_ascii=False)
    record.indexing_enabled = update.indexing_enabled
    record.indexing_model = update.indexing_model
    record.indexing_dimension = update.indexing_dimension
    if update.catalog_token is not None:
        record.encrypted_catalog_token = (
            encrypt_secret(update.catalog_token) if update.catalog_token else ""
        )
    if update.indexing_key is not None:
        record.encrypted_indexing_key = (
            encrypt_secret(update.indexing_key) if update.indexing_key else ""
        )
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _kilo_code_settings_out(record, workspace_ai)


@router.get(
    "/mlflow-server-settings",
    response_model=MlflowServerSettingsOut,
)
async def get_mlflow_server_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the centrally managed MLflow server policy."""
    return _mlflow_server_settings_out(await db.get(MlflowServerSettings, 1))


@router.put(
    "/mlflow-server-settings",
    response_model=MlflowServerSettingsOut,
)
async def update_mlflow_server_settings(
    update: MlflowServerSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Store the MLflow URL and TLS policy without user credentials."""
    candidate = MlflowConfig(
        enabled=update.enabled,
        base_url=update.base_url,
        auth_type="none",
        username="",
        secret="",
        validate_tls=update.validate_tls,
        ca_cert_file=update.ca_cert_file,
        timeout_seconds=update.timeout_seconds,
    )
    if update.enabled:
        try:
            validate_mlflow_config(candidate, require_enabled=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    record = await db.get(MlflowServerSettings, 1)
    if record is None:
        record = MlflowServerSettings(id=1)
    for field_name, value in update.model_dump().items():
        setattr(record, field_name, value)
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _mlflow_server_settings_out(record)
