import asyncio
import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.model_container_registry import (
    ModelContainerRegistryConfigurationError,
    effective_model_container_registry_config,
    validate_model_container_registry_config,
)
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    MlflowPayloadTooLargeError,
    config_from_record,
    config_from_update,
    normalize_experiment,
    normalize_model,
    normalize_run,
    validate_config,
)
from app.models.mlflow_settings import MlflowSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_deployment import (
    MlflowDeployment,
    MlflowDeploymentEvent,
    MlflowDeploymentStatus,
    MlflowModelBuild,
    MlflowModelBuildStatus,
)
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.mlflow_deployment import (
    MlflowDeploymentCreate,
    MlflowDeploymentCreated,
    MlflowDeploymentEventOut,
    MlflowDeploymentEvents,
    MlflowDeploymentList,
    MlflowDeploymentOut,
    MlflowDeploymentTokenOut,
)
from app.schemas.mlflow import MlflowSettingsOut, MlflowSettingsUpdate, MlflowTestResult
from app.security.secrets import encrypt_secret
from app.workspace_catalog import flavor_enabled, resolve_flavor
from app.orchestrator.workspace_provisioning import (
    WorkspaceOperationError,
    delete_workspace_resources,
)
from app.routes.workspace_routes import (
    start_workspace_endpoint,
    stop_workspace_endpoint,
)
from app.orchestrator.mlflow_deployment_service import (
    append_deployment_event,
    enroll_model_build_cleanup,
    garbage_collect_model_build,
)
from app.orchestrator.admission import admission_transaction

mlflow_router = APIRouter(prefix="/api/mlflow", tags=["MLflow"])

MAX_ARTIFACT_PREVIEW_BYTES = 2 * 1024 * 1024
TEXT_ARTIFACT_EXTENSIONS = {
    ".cfg", ".conf", ".csv", ".ini", ".json", ".log", ".md", ".py",
    ".toml", ".tsv", ".txt", ".xml", ".yaml", ".yml",
}
IMAGE_ARTIFACT_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


async def _mlflow_settings_for_user(
    db: AsyncSession,
    user_id: int,
) -> MlflowSettings | None:
    return (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()


def _settings_out(
    record: MlflowSettings | None,
    server: MlflowServerSettings | None,
) -> MlflowSettingsOut:
    base_url = server.base_url if server else ""
    validate_tls = server.validate_tls if server else True
    ca_cert_file = server.ca_cert_file if server else ""
    timeout_seconds = server.timeout_seconds if server else 10
    if record is None:
        return MlflowSettingsOut(
            enabled=False,
            base_url=base_url,
            auth_type="none",
            username="",
            has_secret=False,
            validate_tls=validate_tls,
            ca_cert_file=ca_cert_file,
            timeout_seconds=timeout_seconds,
        )
    return MlflowSettingsOut(
        enabled=record.enabled,
        base_url=base_url,
        auth_type=record.auth_type,
        username=record.username,
        has_secret=bool(record.encrypted_secret),
        validate_tls=validate_tls,
        ca_cert_file=ca_cert_file,
        timeout_seconds=timeout_seconds,
    )


async def get_mlflow_client(db: AsyncSession, user_id: int) -> MlflowClient:
    record = await _mlflow_settings_for_user(db, user_id)
    server = await db.get(MlflowServerSettings, 1)
    if not server or not server.enabled:
        raise HTTPException(
            status_code=503,
            detail="MLflow sunucusu platform yöneticisi tarafından yapılandırılmamış.",
        )
    if not record:
        raise HTTPException(
            status_code=503,
            detail="MLflow bağlantınızı ML Modelleri sayfasından yapılandırın.",
        )
    try:
        config = config_from_record(record, server)
        validate_config(config, require_enabled=True)
        return MlflowClient(config)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _mlflow_url(client: MlflowClient, fragment: str) -> str:
    return f"{client.config.base_url.rstrip('/')}/#/{fragment.lstrip('/')}"


def _experiment_url(client: MlflowClient, experiment_id: str) -> str:
    return _mlflow_url(client, f"experiments/{quote(experiment_id, safe='')}")


def _run_url(client: MlflowClient, experiment_id: str, run_id: str) -> str:
    return _mlflow_url(
        client,
        f"experiments/{quote(experiment_id, safe='')}/runs/{quote(run_id, safe='')}",
    )


def _safe_artifact_path(value: str) -> PurePosixPath:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "//" in value
        or "\x00" in value
    ):
        raise HTTPException(status_code=400, detail="Geçersiz artifact yolu.")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(status_code=400, detail="Geçersiz artifact yolu.")
    return path


def _deployment_out(
    deployment: MlflowDeployment,
    build: MlflowModelBuild | None = None,
) -> MlflowDeploymentOut:
    base = f"/api/model-endpoints/{deployment.id}"
    return MlflowDeploymentOut(
        id=deployment.id,
        user_id=deployment.user_id,
        build_id=deployment.build_id,
        workspace_id=deployment.workspace_id,
        name=deployment.name,
        model_name=deployment.model_name,
        model_version=deployment.model_version,
        run_id=deployment.run_id,
        source_uri=deployment.source_uri,
        flavor_id=deployment.flavor_id,
        auto_stop_minutes=deployment.auto_stop_minutes,
        gunicorn_workers=deployment.gunicorn_workers,
        status=deployment.status,
        status_message=deployment.status_message,
        error_message=deployment.error_message,
        build_status=build.status.value if build else None,
        build_status_message=build.status_message if build else None,
        build_error_message=build.error_message if build else None,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        endpoint_url=f"{base}/invocations",
        health_url=f"{base}/ping",
    )


def _downsample_metrics(metrics: list[dict], limit: int = 500) -> list[dict]:
    if len(metrics) <= limit:
        return metrics
    step = (len(metrics) - 1) / (limit - 1)
    return [metrics[round(index * step)] for index in range(limit)]


@mlflow_router.get("/settings", response_model=MlflowSettingsOut)
async def get_mlflow_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _settings_out(
        await _mlflow_settings_for_user(db, current_user.id),
        await db.get(MlflowServerSettings, 1),
    )


@mlflow_router.put("/settings", response_model=MlflowSettingsOut)
async def update_mlflow_settings(
    update: MlflowSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _mlflow_settings_for_user(db, current_user.id)
    server = await db.get(MlflowServerSettings, 1)
    if not server or not server.enabled:
        raise HTTPException(
            status_code=503,
            detail="MLflow sunucusu platform yöneticisi tarafından yapılandırılmamış.",
        )
    try:
        candidate = config_from_update(update, server, record)
        if update.enabled:
            validate_config(candidate)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if record is None:
        record = MlflowSettings(user_id=current_user.id)
    for field_name, value in update.model_dump(exclude={"secret"}).items():
        setattr(record, field_name, value)
    if update.secret:
        record.encrypted_secret = encrypt_secret(update.secret)
    elif update.auth_type == "none":
        record.encrypted_secret = ""
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _settings_out(record, server)


@mlflow_router.post("/settings/test", response_model=MlflowTestResult)
async def test_mlflow_settings(
    update: MlflowSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _mlflow_settings_for_user(db, current_user.id)
    server = await db.get(MlflowServerSettings, 1)
    if not server or not server.enabled:
        raise HTTPException(
            status_code=503,
            detail="MLflow sunucusu platform yöneticisi tarafından yapılandırılmamış.",
        )
    try:
        candidate = config_from_update(update, server, record)
        validate_config(candidate)
        count, model_count, elapsed_ms, server_version = await MlflowClient(candidate).test()
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MlflowTestResult(
        success=True,
        message="Kişisel MLflow bağlantınız başarılı.",
        response_time_ms=elapsed_ms,
        experiment_count=count,
        model_count=max(model_count, 0),
        server_version=server_version,
        tracking_available=True,
        registry_available=model_count >= 0,
    )


@mlflow_router.get("/overview")
async def get_mlflow_overview(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    started = time.monotonic()
    try:
        experiments_payload, models_payload = await asyncio.gather(
            client.search_experiments(max_results=1000),
            client.search_registered_models(max_results=200),
        )
        experiments = [
            normalize_experiment(item)
            for item in experiments_payload.get("experiments") or []
        ]
        experiment_ids = [
            str(item.get("experiment_id") or "")
            for item in experiments
            if item.get("experiment_id") is not None
        ]
        runs_payload = (
            await client.search_runs(experiment_ids, max_results=100)
            if experiment_ids
            else {"runs": []}
        )
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    runs = [normalize_run(item) for item in runs_payload.get("runs") or []]
    for run in runs:
        run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
    models = [
        {
            **normalize_model(item),
            "mlflow_url": _mlflow_url(
                client, f"models/{quote(str(item.get('name') or ''), safe='')}"
            ),
        }
        for item in models_payload.get("registered_models") or []
    ]
    status_counts: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "UNKNOWN").upper()
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "response_time_ms": round((time.monotonic() - started) * 1000),
        "experiment_count": len(experiments),
        "experiment_count_is_partial": bool(experiments_payload.get("next_page_token")),
        "model_count": len(models),
        "model_count_is_partial": bool(models_payload.get("next_page_token")),
        "sampled_run_count": len(runs),
        "status_counts": status_counts,
        "recent_runs": runs[:8],
        "recent_models": models[:6],
    }


@mlflow_router.get("/models")
async def list_mlflow_models(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: str = Query(default="", max_length=200),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_registered_models(search=search, page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "models": [
            {
                **normalize_model(item),
                "mlflow_url": _mlflow_url(
                    client, f"models/{quote(str(item.get('name') or ''), safe='')}"
                ),
            }
            for item in payload.get("registered_models") or []
        ],
        "next_page_token": payload.get("next_page_token") or "",
    }


@mlflow_router.get("/models/{model_name}")
async def get_mlflow_model(
    model_name: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        model_payload = await client.get_registered_model(model_name)
        versions_payload = await client.search_model_versions(model_name)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    model = normalize_model(model_payload.get("registered_model") or {})
    versions = []
    for version in versions_payload.get("model_versions") or []:
        run_id = str(version.get("run_id") or "")
        run_link = str(version.get("run_link") or "")
        experiment_id = (
            run_link.split("/experiments/", 1)[1].split("/", 1)[0]
            if "/experiments/" in run_link
            else ""
        )
        versions.append(
            {
                **version,
                "aifactory_run_url": f"/runs/{quote(run_id, safe='')}" if run_id else "",
                "mlflow_run_url": (
                    _run_url(client, experiment_id, run_id)
                    if run_id and experiment_id
                    else run_link
                ),
            }
        )
    model["versions"] = versions
    model["mlflow_url"] = _mlflow_url(
        client, f"models/{quote(model_name, safe='')}"
    )
    return model


@mlflow_router.get("/experiments")
async def list_mlflow_experiments(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_experiments(page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    experiments = []
    for item in payload.get("experiments") or []:
        experiment = normalize_experiment(item)
        experiment["mlflow_url"] = _experiment_url(
            client, str(experiment.get("experiment_id") or "")
        )
        experiments.append(experiment)
    return {
        "experiments": experiments,
        "next_page_token": payload.get("next_page_token") or "",
    }


@mlflow_router.get("/experiments/{experiment_id}")
async def get_mlflow_experiment(
    experiment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.get_experiment(experiment_id)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    experiment = normalize_experiment(payload.get("experiment") or {})
    experiment["mlflow_url"] = _experiment_url(client, experiment_id)
    return experiment


@mlflow_router.get("/runs")
async def list_mlflow_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    experiment_id: str = Query(..., min_length=1, max_length=256),
    filter_string: str = Query(default="", max_length=2000),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_runs(
            [experiment_id],
            filter_string=filter_string,
            page_token=page_token,
        )
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    runs = []
    for item in payload.get("runs") or []:
        run = normalize_run(item)
        run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
        runs.append(run)
    return {"runs": runs, "next_page_token": payload.get("next_page_token") or ""}


@mlflow_router.get("/runs/compare")
async def compare_mlflow_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_ids: Annotated[list[str], Query(min_length=2, max_length=10)],
):
    client = await get_mlflow_client(db, current_user.id)
    unique_ids = list(dict.fromkeys(run_ids))
    if len(unique_ids) < 2 or len(unique_ids) > 10:
        raise HTTPException(status_code=422, detail="Karşılaştırmak için 2-10 farklı run seçin.")
    try:
        payloads = await asyncio.gather(*(client.get_run(run_id) for run_id in unique_ids))
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    runs = []
    for payload in payloads:
        run = normalize_run(payload.get("run") or {})
        run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
        runs.append(run)
    return {"runs": runs}


@mlflow_router.get("/runs/{run_id}")
async def get_mlflow_run(
    run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    run_payload, artifacts_payload, versions_payload = await asyncio.gather(
        client.get_run(run_id),
        client.list_artifacts(run_id),
        client.search_model_versions(run_id=run_id, max_results=100),
        return_exceptions=True,
    )
    if isinstance(run_payload, Exception):
        if isinstance(run_payload, MlflowConnectionError):
            raise HTTPException(status_code=502, detail=str(run_payload)) from run_payload
        raise run_payload

    warnings = []
    if isinstance(artifacts_payload, Exception):
        warnings.append(f"Artifact listesi yüklenemedi: {artifacts_payload}")
        artifacts_payload = {}
    if isinstance(versions_payload, Exception):
        warnings.append(f"Model soy ağacı yüklenemedi: {versions_payload}")
        versions_payload = {}
    run = normalize_run(run_payload.get("run") or {})
    run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
    run["artifacts"] = [
        {
            **artifact,
            "mlflow_url": (
                f"{run['mlflow_url']}/artifacts/"
                f"{quote(str(artifact.get('path') or ''), safe='/')}"
            ),
        }
        for artifact in artifacts_payload.get("files") or []
    ]
    run["registered_model_versions"] = [
        {
            **version,
            "mlflow_url": _mlflow_url(
                client, f"models/{quote(str(version.get('name') or ''), safe='')}"
            ),
        }
        for version in versions_payload.get("model_versions") or []
        if str(version.get("run_id") or "") == run_id
    ]
    run["warnings"] = warnings
    return run


@mlflow_router.get("/runs/{run_id}/metrics/{metric_key}/history")
async def get_mlflow_metric_history(
    run_id: str,
    metric_key: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not metric_key.strip():
        raise HTTPException(status_code=400, detail="Metrik adı boş olamaz.")
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.get_metric_history(run_id, metric_key)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    points = sorted(
        payload.get("metrics") or [],
        key=lambda item: (int(item.get("step") or 0), int(item.get("timestamp") or 0)),
    )
    return {
        "run_id": run_id,
        "metric_key": metric_key,
        "points": _downsample_metrics(points),
        "source_point_count": len(points),
    }


@mlflow_router.get("/runs/{run_id}/artifacts")
async def list_mlflow_artifacts(
    run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(default="", max_length=2000),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.list_artifacts(run_id, path=path, page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "files": payload.get("files") or [],
        "next_page_token": payload.get("next_page_token") or "",
    }


@mlflow_router.get("/runs/{run_id}/artifacts/preview")
async def preview_mlflow_artifact(
    run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(..., min_length=1, max_length=2000),
):
    artifact_path = _safe_artifact_path(path)
    extension = artifact_path.suffix.lower()
    if extension not in TEXT_ARTIFACT_EXTENSIONS and extension not in IMAGE_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Bu artifact türü güvenli önizleme için desteklenmiyor.",
        )
    client = await get_mlflow_client(db, current_user.id)
    parent = "" if artifact_path.parent == PurePosixPath(".") else artifact_path.parent.as_posix()
    try:
        listing = await client.list_artifacts(run_id, path=parent)
        artifact = next(
            (
                item
                for item in listing.get("files") or []
                if str(item.get("path") or "") == artifact_path.as_posix()
            ),
            None,
        )
        if not artifact or artifact.get("is_dir"):
            raise HTTPException(status_code=404, detail="Artifact dosyası bulunamadı.")
        file_size = int(artifact.get("file_size") or 0)
        if file_size > MAX_ARTIFACT_PREVIEW_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Artifact 2 MiB güvenli önizleme sınırını aşıyor.",
            )
        content, _ = await client.download_artifact(
            run_id,
            artifact_path.as_posix(),
            max_bytes=MAX_ARTIFACT_PREVIEW_BYTES,
        )
    except MlflowPayloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if extension in IMAGE_ARTIFACT_TYPES:
        return {
            "kind": "image",
            "path": artifact_path.as_posix(),
            "content_type": IMAGE_ARTIFACT_TYPES[extension],
            "size": len(content),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
    return {
        "kind": "text",
        "path": artifact_path.as_posix(),
        "content_type": "text/plain; charset=utf-8",
        "size": len(content),
        "content": content.decode("utf-8-sig", errors="replace"),
    }



@mlflow_router.post(
    "/deployments",
    response_model=MlflowDeploymentCreated,
    status_code=202,
)
async def create_mlflow_deployment(
    payload: MlflowDeploymentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Validate an immutable model version and enqueue a durable deployment."""
    try:
        registry = await effective_model_container_registry_config(db)
        validate_model_container_registry_config(registry, require_enabled=True)
    except ModelContainerRegistryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    flavor = await resolve_flavor(db, payload.flavor_id)
    if flavor is None or not await flavor_enabled(db, payload.flavor_id):
        raise HTTPException(status_code=400, detail="Geçersiz veya devre dışı kaynak profili.")
    existing = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.user_id == current_user.id,
                MlflowDeployment.name == payload.name,
                MlflowDeployment.status != MlflowDeploymentStatus.FAILED,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Bu adla etkin bir model deployment zaten var.")

    client = await get_mlflow_client(db, current_user.id)
    try:
        version_payload = await client.get_model_version(
            payload.model_name, payload.model_version
        )
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    version = version_payload.get("model_version") or {}
    if (
        str(version.get("name") or "") != payload.model_name
        or str(version.get("version") or "") != payload.model_version
    ):
        raise HTTPException(status_code=409, detail="MLflow beklenen model versiyonunu döndürmedi.")

    access_token = secrets.token_urlsafe(32)
    deployment = MlflowDeployment(
        user_id=current_user.id,
        name=payload.name,
        model_name=payload.model_name,
        model_version=payload.model_version,
        run_id=str(version.get("run_id") or ""),
        source_uri=str(version.get("source") or ""),
        flavor_id=payload.flavor_id,
        auto_stop_minutes=payload.auto_stop_minutes,
        gunicorn_workers=payload.gunicorn_workers,
        access_token_hash=hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
        status=MlflowDeploymentStatus.QUEUED,
        status_message="Deployment kuyruğa alındı.",
    )
    db.add(deployment)
    await db.flush()
    db.add(
        MlflowDeploymentEvent(
            deployment_id=deployment.id,
            sequence=1,
            level="info",
            message=(
                f"{deployment.model_name} v{deployment.model_version} için "
                "deployment kuyruğa alındı."
            ),
        )
    )
    try:
        await db.commit()
        await db.refresh(deployment)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Deployment kaydı oluşturulamadı.") from exc
    return MlflowDeploymentCreated(
        **_deployment_out(deployment).model_dump(),
        access_token=access_token,
    )


@mlflow_router.get("/deployments", response_model=MlflowDeploymentList)
async def list_mlflow_deployments(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployments = (
        await db.execute(
            select(MlflowDeployment)
            .where(MlflowDeployment.user_id == current_user.id)
            .order_by(MlflowDeployment.created_at.desc())
        )
    ).scalars().all()
    return MlflowDeploymentList(
        deployments=[_deployment_out(item) for item in deployments]
    )


async def _garbage_collect_model_build(
    db: AsyncSession,
    build_id: str | None,
) -> None:
    await garbage_collect_model_build(db, build_id)


async def _owned_deployment(
    db: AsyncSession,
    deployment_id: str,
    current_user: User,
) -> MlflowDeployment:
    deployment = await db.get(MlflowDeployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Model deployment bulunamadı.")
    if deployment.user_id != current_user.id and current_user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Bu model deployment için erişim reddedildi.")
    return deployment


@mlflow_router.get("/deployments/{deployment_id}", response_model=MlflowDeploymentOut)
async def get_mlflow_deployment(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    # The image build keeps the container build log tail that explains most
    # deployment failures, so include it on the detail read. The list read stays
    # build-free to avoid a query per deployment.
    build = (
        await db.get(MlflowModelBuild, deployment.build_id)
        if deployment.build_id
        else None
    )
    return _deployment_out(deployment, build)


@mlflow_router.get(
    "/deployments/{deployment_id}/events",
    response_model=MlflowDeploymentEvents,
)
async def get_mlflow_deployment_events(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _owned_deployment(db, deployment_id, current_user)
    events = (
        await db.execute(
            select(MlflowDeploymentEvent)
            .where(MlflowDeploymentEvent.deployment_id == deployment_id)
            .order_by(MlflowDeploymentEvent.sequence)
        )
    ).scalars().all()
    return MlflowDeploymentEvents(
        events=[MlflowDeploymentEventOut.model_validate(item) for item in events]
    )


@mlflow_router.post(
    "/deployments/{deployment_id}/token",
    response_model=MlflowDeploymentTokenOut,
)
async def rotate_mlflow_deployment_token(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    access_token = secrets.token_urlsafe(32)
    deployment.access_token_hash = hashlib.sha256(
        access_token.encode("utf-8")
    ).hexdigest()
    await append_deployment_event(
        db, deployment, "Model endpoint erişim tokenı yenilendi."
    )
    db.add(deployment)
    await db.commit()
    return MlflowDeploymentTokenOut(access_token=access_token)


@mlflow_router.post(
    "/deployments/{deployment_id}/stop",
    response_model=MlflowDeploymentOut,
)
async def stop_mlflow_deployment(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    if not deployment.workspace_id:
        raise HTTPException(status_code=409, detail="Deployment henüz workspace oluşturmadı.")
    await stop_workspace_endpoint(deployment.workspace_id, current_user, db)
    deployment.status = MlflowDeploymentStatus.STOPPED
    deployment.status_message = "Model servisi durduruldu."
    deployment.error_message = None
    await append_deployment_event(db, deployment, deployment.status_message)
    await db.commit()
    await db.refresh(deployment)
    return _deployment_out(deployment)


@mlflow_router.post(
    "/deployments/{deployment_id}/start",
    response_model=MlflowDeploymentOut,
)
async def start_mlflow_deployment(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    if not deployment.workspace_id:
        raise HTTPException(status_code=409, detail="Deployment henüz workspace oluşturmadı.")
    workspace = await start_workspace_endpoint(deployment.workspace_id, current_user, db)
    if workspace.status.value != "running":
        raise HTTPException(status_code=502, detail=workspace.error_message or "Model servisi başlatılamadı.")
    deployment.status = MlflowDeploymentStatus.RUNNING
    deployment.status_message = "Model servisi yeniden başlatıldı."
    deployment.error_message = None
    await append_deployment_event(db, deployment, deployment.status_message, "success")
    await db.commit()
    await db.refresh(deployment)
    return _deployment_out(deployment)


@mlflow_router.post(
    "/deployments/{deployment_id}/retry",
    response_model=MlflowDeploymentOut,
    status_code=202,
)
async def retry_mlflow_deployment(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    if deployment.status != MlflowDeploymentStatus.FAILED:
        raise HTTPException(status_code=409, detail="Yalnızca başarısız deployment yeniden denenebilir.")
    if deployment.workspace_id:
        workspace = await db.get(Workspace, deployment.workspace_id)
        if workspace:
            try:
                await delete_workspace_resources(
                    db, workspace, allow_transient=True
                )
            except WorkspaceOperationError as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail=exc.detail
                ) from exc
        deployment.workspace_id = None
    failed_build_id = None
    if deployment.build_id:
        async with admission_transaction(db):
            await db.refresh(deployment)
            build = await db.get(MlflowModelBuild, deployment.build_id)
            if build and build.status == MlflowModelBuildStatus.FAILED:
                failed_build_id = build.id
                await enroll_model_build_cleanup(
                    db,
                    failed_build_id,
                    excluding_deployment_id=deployment.id,
                )
                deployment.build_id = None
                db.add(deployment)
        if failed_build_id:
            await _garbage_collect_model_build(db, failed_build_id)
    deployment.status = MlflowDeploymentStatus.QUEUED
    deployment.status_message = "Deployment yeniden kuyruğa alındı."
    deployment.error_message = None
    await append_deployment_event(db, deployment, deployment.status_message)
    await db.commit()
    await db.refresh(deployment)
    return _deployment_out(deployment)


@mlflow_router.delete("/deployments/{deployment_id}", status_code=204)
async def delete_mlflow_deployment(
    deployment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deployment = await _owned_deployment(db, deployment_id, current_user)
    if deployment.status not in {
        MlflowDeploymentStatus.RUNNING,
        MlflowDeploymentStatus.STOPPED,
        MlflowDeploymentStatus.FAILED,
        MlflowDeploymentStatus.DELETING,
    }:
        raise HTTPException(
            status_code=409,
            detail="Devam eden deployment silinemez; tamamlanmasını bekleyin.",
        )
    deployment.status = MlflowDeploymentStatus.DELETING
    deployment.status_message = "Model deployment siliniyor."
    deployment.lease_owner = f"api-delete:{current_user.id}"
    deployment.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    build_id = deployment.build_id
    await db.commit()
    try:
        if deployment.workspace_id:
            workspace = await db.get(Workspace, deployment.workspace_id)
            if workspace:
                await delete_workspace_resources(
                db, workspace, allow_transient=True
            )
            deployment.workspace_id = None
    except WorkspaceOperationError as exc:
        deployment.status = MlflowDeploymentStatus.FAILED
        deployment.status_message = "Model deployment silinemedi; yeniden deneyin."
        deployment.error_message = str(exc.detail)[:4000]
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    async with admission_transaction(db):
        await db.refresh(deployment)
        await enroll_model_build_cleanup(
            db,
            build_id,
            excluding_deployment_id=deployment.id,
        )
        await db.delete(deployment)
    return None
