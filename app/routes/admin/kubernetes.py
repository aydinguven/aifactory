"""Admin API: deploy registry (Nexus), Kubernetes projects and applications."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.bamboo import (
    BambooConfig,
    BambooConfigurationError,
    bamboo_settings_out,
    effective_bamboo_config,
)
from app.bamboo import config_from_record as bamboo_config_from_record
from app.bamboo import config_from_update as bamboo_config_from_update
from app.database import get_db
from app.deploy_registry import (
    DeployRegistryConfig,
    DeployRegistryConfigurationError,
    config_from_record,
    config_from_update,
    deploy_registry_settings_out,
    effective_deploy_registry_config,
    test_deploy_registry,
)
from app.kubernetes_publish import (
    app_name_fits_project,
    app_name_rule_message,
    highest_version,
    image_repository,
)
from app.models.bamboo_settings import BambooSettings
from app.models.deploy_registry_settings import DeployRegistrySettings
from app.models.kubernetes import (
    ImageReleaseStatus,
    K8sApplication,
    K8sProject,
    MlflowImageRelease,
)
from app.models.user import User
from app.orchestrator.admission import admission_transaction
from app.schemas.kubernetes import (
    BambooSettingsOut,
    BambooSettingsUpdate,
    DeployRegistrySettingsOut,
    DeployRegistrySettingsUpdate,
    DeployRegistryTestRequest,
    DeployRegistryTestResult,
    K8sApplicationOut,
    K8sApplicationWrite,
    K8sProjectList,
    K8sProjectOut,
    K8sProjectWrite,
)
from app.security.secrets import encrypt_secret

router = APIRouter()


async def _current_for_update(
    db: AsyncSession,
    record: DeployRegistrySettings | None,
    submitted_password: str | None,
) -> DeployRegistryConfig:
    try:
        return await effective_deploy_registry_config(db)
    except DeployRegistryConfigurationError:
        # An undecryptable stored password can be replaced, not kept.
        if submitted_password is None:
            raise
        return DeployRegistryConfig(
            managed=record is not None,
            enabled=False,
            registry_url=record.registry_url if record else "",
            username=record.username if record else "",
            password="",
        )


@router.get("/deploy-registry-settings", response_model=DeployRegistrySettingsOut)
async def get_deploy_registry_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(DeployRegistrySettings, 1)
    try:
        config = await effective_deploy_registry_config(db)
    except DeployRegistryConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return deploy_registry_settings_out(config, record)


@router.put("/deploy-registry-settings", response_model=DeployRegistrySettingsOut)
async def update_deploy_registry_settings(
    update: DeployRegistrySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Encrypt and store the registry Kubernetes releases are published to."""
    try:
        async with admission_transaction(db):
            record = await db.get(DeployRegistrySettings, 1)
            current = await _current_for_update(db, record, update.password)
            candidate = config_from_update(update, current)
            if record is None:
                record = DeployRegistrySettings(id=1)
                record.encrypted_password = encrypt_secret(candidate.password)
            elif update.password is not None:
                record.encrypted_password = encrypt_secret(candidate.password)
            record.enabled = candidate.enabled
            record.registry_url = candidate.registry_url
            record.username = candidate.username
            record.tls_verify = candidate.tls_verify
            record.manifest_format = candidate.manifest_format
            record.updated_at = datetime.now(timezone.utc)
            db.add(record)
    except DeployRegistryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.refresh(record)
    return deploy_registry_settings_out(config_from_record(record), record)


@router.post("/deploy-registry-settings/test", response_model=DeployRegistryTestResult)
async def test_deploy_registry_settings(
    request: DeployRegistryTestRequest,
    response: Response,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Test unsaved settings from the controller; only the controller pushes here."""
    response.headers["Cache-Control"] = "no-store"
    try:
        record = await db.get(DeployRegistrySettings, 1)
        current = await _current_for_update(db, record, request.password)
        candidate = config_from_update(
            DeployRegistrySettingsUpdate(
                enabled=True,
                registry_url=request.registry_url,
                username=request.username,
                password=request.password,
                tls_verify=request.tls_verify,
            ),
            current,
        )
        result = await test_deploy_registry(candidate)
    except DeployRegistryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DeployRegistryTestResult(registry_url=candidate.registry_url, **result)


@router.get("/bamboo-settings", response_model=BambooSettingsOut)
async def get_bamboo_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(BambooSettings, 1)
    try:
        config = await effective_bamboo_config(db)
    except BambooConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return bamboo_settings_out(config, record)


@router.put("/bamboo-settings", response_model=BambooSettingsOut)
async def update_bamboo_settings(
    update: BambooSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Encrypt and store the Bamboo plan queued after each published release."""
    try:
        async with admission_transaction(db):
            record = await db.get(BambooSettings, 1)
            try:
                current = await effective_bamboo_config(db)
            except BambooConfigurationError:
                # An undecryptable stored token can be replaced, not kept.
                if update.token is None:
                    raise
                current = BambooConfig(
                    managed=True, enabled=False, base_url="", plan_key="", token=""
                )
            candidate = bamboo_config_from_update(update, current)
            if record is None:
                record = BambooSettings(id=1)
                record.encrypted_token = encrypt_secret(candidate.token)
            elif update.token is not None:
                record.encrypted_token = encrypt_secret(candidate.token)
            record.enabled = candidate.enabled
            record.base_url = candidate.base_url
            record.plan_key = candidate.plan_key
            record.tls_verify = candidate.tls_verify
            record.updated_at = datetime.now(timezone.utc)
            db.add(record)
    except BambooConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.refresh(record)
    return bamboo_settings_out(bamboo_config_from_record(record), record)


async def _release_stats(db: AsyncSession) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Non-failed releases per application: their count and published versions."""
    rows = (
        await db.execute(
            select(
                MlflowImageRelease.application_id,
                MlflowImageRelease.version,
                MlflowImageRelease.status,
            ).where(MlflowImageRelease.status != ImageReleaseStatus.FAILED)
        )
    ).all()
    counts: dict[str, int] = {}
    published: dict[str, list[str]] = {}
    for application_id, version, status in rows:
        counts[application_id] = counts.get(application_id, 0) + 1
        if status == ImageReleaseStatus.PUBLISHED:
            published.setdefault(application_id, []).append(version)
    return counts, published


def _application_out(
    application: K8sApplication,
    project: K8sProject,
    counts: dict[str, int],
    published: dict[str, list[str]],
) -> K8sApplicationOut:
    release_count = counts.get(application.id, 0)
    return K8sApplicationOut(
        id=application.id,
        project_id=application.project_id,
        app_name=application.app_name,
        repository=image_repository(project.rfs_code, application.app_name),
        mlflow_model_name=application.mlflow_model_name,
        bitbucket_url=application.bitbucket_url,
        description=application.description,
        enabled=application.enabled,
        release_count=release_count,
        last_version=highest_version(published.get(application.id, [])),
        locked=release_count > 0,
    )


async def _project_out(db: AsyncSession, project: K8sProject) -> K8sProjectOut:
    counts, published = await _release_stats(db)
    applications = (
        await db.execute(
            select(K8sApplication)
            .where(K8sApplication.project_id == project.id)
            .order_by(K8sApplication.app_name)
        )
    ).scalars().all()
    items = [_application_out(item, project, counts, published) for item in applications]
    return K8sProjectOut(
        id=project.id,
        rfs_code=project.rfs_code,
        name=project.name,
        namespace=project.namespace,
        description=project.description,
        enabled=project.enabled,
        locked=any(item.locked for item in items),
        applications=items,
    )


@router.get("/k8s-projects", response_model=K8sProjectList)
async def list_k8s_projects(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    projects = (
        await db.execute(select(K8sProject).order_by(K8sProject.rfs_code))
    ).scalars().all()
    applications = (
        await db.execute(select(K8sApplication).order_by(K8sApplication.app_name))
    ).scalars().all()
    counts, published = await _release_stats(db)
    by_project: dict[str, list[K8sApplication]] = {}
    for application in applications:
        by_project.setdefault(application.project_id, []).append(application)
    result = []
    for project in projects:
        items = [
            _application_out(item, project, counts, published)
            for item in by_project.get(project.id, [])
        ]
        result.append(
            K8sProjectOut(
                id=project.id,
                rfs_code=project.rfs_code,
                name=project.name,
                namespace=project.namespace,
                description=project.description,
                enabled=project.enabled,
                locked=any(item.locked for item in items),
                applications=items,
            )
        )
    return K8sProjectList(projects=result)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@router.post("/k8s-projects", response_model=K8sProjectOut, status_code=201)
async def create_k8s_project(
    payload: K8sProjectWrite,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    project = K8sProject(**payload.model_dump())
    db.add(project)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _conflict("Bu RFS kodu veya namespace başka bir projede kullanılıyor.") from exc
    await db.refresh(project)
    return await _project_out(db, project)


async def _project_or_404(db: AsyncSession, project_id: str) -> K8sProject:
    project = await db.get(K8sProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Kubernetes projesi bulunamadı.")
    return project


async def _project_has_releases(db: AsyncSession, project_id: str) -> bool:
    count = (
        await db.execute(
            select(func.count(MlflowImageRelease.id))
            .join(K8sApplication, K8sApplication.id == MlflowImageRelease.application_id)
            .where(
                K8sApplication.project_id == project_id,
                MlflowImageRelease.status != ImageReleaseStatus.FAILED,
            )
        )
    ).scalar_one()
    return bool(count)


async def _application_has_releases(db: AsyncSession, application_id: str) -> bool:
    count = (
        await db.execute(
            select(func.count(MlflowImageRelease.id)).where(
                MlflowImageRelease.application_id == application_id,
                MlflowImageRelease.status != ImageReleaseStatus.FAILED,
            )
        )
    ).scalar_one()
    return bool(count)


@router.put("/k8s-projects/{project_id}", response_model=K8sProjectOut)
async def update_k8s_project(
    project_id: str,
    payload: K8sProjectWrite,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    project = await _project_or_404(db, project_id)
    if (
        payload.rfs_code != project.rfs_code or payload.namespace != project.namespace
    ) and await _project_has_releases(db, project.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Bu projenin yayınlanmış imajları var; RFS kodu ve namespace değiştirilemez. "
                "Gerekirse projeyi devre dışı bırakıp yenisini oluşturun."
            ),
        )
    for field, value in payload.model_dump().items():
        setattr(project, field, value)
    db.add(project)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _conflict("Bu RFS kodu veya namespace başka bir projede kullanılıyor.") from exc
    await db.refresh(project)
    return await _project_out(db, project)


@router.delete("/k8s-projects/{project_id}", status_code=204)
async def delete_k8s_project(
    project_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    project = await _project_or_404(db, project_id)
    applications = (
        await db.execute(
            select(func.count(K8sApplication.id)).where(K8sApplication.project_id == project.id)
        )
    ).scalar_one()
    if applications:
        raise HTTPException(
            status_code=409,
            detail="Projenin uygulamaları var; önce uygulamaları silin veya projeyi devre dışı bırakın.",
        )
    await db.delete(project)
    await db.commit()
    return None


# Applications are created by their first release from the MLflow deploy modal.


async def _application_or_404(db: AsyncSession, application_id: str) -> K8sApplication:
    application = await db.get(K8sApplication, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Kubernetes uygulaması bulunamadı.")
    return application


@router.put("/k8s-applications/{application_id}", response_model=K8sProjectOut)
async def update_k8s_application(
    application_id: str,
    payload: K8sApplicationWrite,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    application = await _application_or_404(db, application_id)
    project = await _project_or_404(db, application.project_id)
    if payload.app_name != application.app_name:
        if await _application_has_releases(db, application.id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Bu uygulamanın yayınlanmış imajları var; uygulama adı (imaj adı) "
                    "değiştirilemez."
                ),
            )
        if not app_name_fits_project(project.rfs_code, payload.app_name):
            raise HTTPException(status_code=400, detail=app_name_rule_message(project.rfs_code))
    for field, value in payload.model_dump().items():
        setattr(application, field, value)
    db.add(application)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _conflict("Bu projede aynı adlı bir uygulama zaten var.") from exc
    return await _project_out(db, project)


@router.delete("/k8s-applications/{application_id}", status_code=204)
async def delete_k8s_application(
    application_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _application_or_404(db, application_id)
    # Same lock as release creation and retry, so none can slip in between.
    async with admission_transaction(db):
        application = await _application_or_404(db, application_id)
        if await _application_has_releases(db, application.id):
            raise HTTPException(
                status_code=409,
                detail="Uygulamanın yayınları var; silmek yerine devre dışı bırakın.",
            )
        # Only failed releases remain, and a failed release never leaves a tag
        # behind (a pushed tag is recorded as published).
        await db.execute(
            delete(MlflowImageRelease).where(
                MlflowImageRelease.application_id == application.id,
                MlflowImageRelease.status == ImageReleaseStatus.FAILED,
            )
        )
        await db.delete(application)
    return None
