"""Kubernetes target of the MLflow deploy modal: publish images to the deploy registry."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.deploy_registry import (
    DeployRegistryConfigurationError,
    effective_deploy_registry_config,
    validate_deploy_registry_config,
)
from app.integrations.mlflow import MlflowConfigurationError, MlflowConnectionError
from app.kubernetes_publish import (
    app_name_fits_project,
    app_name_rule_message,
    can_publish_images,
    highest_version,
    image_repository,
    suggest_next_version,
)
from app.model_container_registry import (
    ModelContainerRegistryConfigurationError,
    effective_model_container_registry_config,
    validate_model_container_registry_config,
)
from app.models.kubernetes import (
    BambooTriggerStatus,
    ImageReleaseStatus,
    K8sApplication,
    K8sProject,
    MlflowImageRelease,
    MlflowImageReleaseEvent,
)
from app.models.mlflow_deployment import MlflowModelBuild, MlflowModelBuildStatus
from app.models.user import User, UserRole
from app.orchestrator.admission import admission_transaction
from app.orchestrator.image_release_service import append_release_event
from app.orchestrator.mlflow_deployment_service import (
    enroll_model_build_cleanup,
    garbage_collect_model_build,
)
from app.routes.mlflow_routes import get_mlflow_client
from app.schemas.kubernetes import (
    ImageReleaseCreate,
    ImageReleaseEventOut,
    ImageReleaseEvents,
    ImageReleaseList,
    ImageReleaseOut,
    KubernetesApplicationOption,
    KubernetesOptions,
    KubernetesProjectOption,
)

image_release_router = APIRouter(prefix="/api/mlflow", tags=["MLflow"])


def _release_out(
    release: MlflowImageRelease,
    *,
    application: K8sApplication | None = None,
    project: K8sProject | None = None,
    username: str = "",
    build: MlflowModelBuild | None = None,
) -> ImageReleaseOut:
    return ImageReleaseOut(
        id=release.id,
        user_id=release.user_id,
        username=username,
        application_id=release.application_id,
        app_name=application.app_name if application else "",
        project_name=project.name if project else "",
        rfs_code=project.rfs_code if project else "",
        namespace=project.namespace if project else "",
        model_name=release.model_name,
        model_version=release.model_version,
        run_id=release.run_id,
        version=release.version,
        image_ref=release.image_ref,
        digest=release.digest,
        mlflow_tagged=release.mlflow_tagged,
        status=release.status,
        status_message=release.status_message,
        error_message=release.error_message,
        build_status=build.status.value if build else None,
        build_error_message=build.error_message if build else None,
        bamboo_status=release.bamboo_status or "",
        bamboo_result_key=release.bamboo_result_key or "",
        bamboo_result_url=release.bamboo_result_url or "",
        bamboo_error=release.bamboo_error,
        created_at=release.created_at,
        updated_at=release.updated_at,
        completed_at=release.completed_at,
    )


async def _detailed_release_out(db: AsyncSession, release: MlflowImageRelease) -> ImageReleaseOut:
    application = await db.get(K8sApplication, release.application_id)
    project = await db.get(K8sProject, application.project_id) if application else None
    user = await db.get(User, release.user_id)
    build = await db.get(MlflowModelBuild, release.build_id) if release.build_id else None
    return _release_out(
        release,
        application=application,
        project=project,
        username=user.username if user else "",
        build=build,
    )


async def _application_versions(db: AsyncSession, application_ids: list[str]) -> dict:
    """Non-failed versions per application: published ones and all in use."""
    used: dict[str, list[str]] = {item: [] for item in application_ids}
    published: dict[str, list[str]] = {item: [] for item in application_ids}
    if not application_ids:
        return {"used": used, "published": published}
    rows = (
        await db.execute(
            select(
                MlflowImageRelease.application_id,
                MlflowImageRelease.version,
                MlflowImageRelease.status,
            ).where(
                MlflowImageRelease.application_id.in_(application_ids),
                MlflowImageRelease.status != ImageReleaseStatus.FAILED,
            )
        )
    ).all()
    for application_id, version, status in rows:
        used[application_id].append(version)
        if status == ImageReleaseStatus.PUBLISHED:
            published[application_id].append(version)
    return {"used": used, "published": published}


@image_release_router.get("/kubernetes/options", response_model=KubernetesOptions)
async def kubernetes_publish_options(
    model_name: Annotated[str, Query(min_length=1, max_length=255)],
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """RFS projects to publish under, and the applications already bound to the model."""
    message = ""
    registry_url = ""
    try:
        deploy = await effective_deploy_registry_config(db)
        validate_deploy_registry_config(deploy, require_enabled=True)
        registry_url = deploy.registry_url
    except DeployRegistryConfigurationError as exc:
        message = str(exc)
    if not can_publish_images(current_user):
        return KubernetesOptions(
            enabled=False,
            can_publish=False,
            registry_url="",
            message="Kubernetes imaj yayını için yetkiniz yok.",
        )
    projects = (
        await db.execute(
            select(K8sProject).where(K8sProject.enabled.is_(True)).order_by(K8sProject.rfs_code)
        )
    ).scalars().all()
    rows = (
        await db.execute(
            select(K8sApplication, K8sProject.rfs_code)
            .join(K8sProject, K8sProject.id == K8sApplication.project_id)
            .where(
                K8sApplication.mlflow_model_name == model_name,
                K8sApplication.enabled.is_(True),
                K8sProject.enabled.is_(True),
            )
            .order_by(K8sProject.rfs_code, K8sApplication.app_name)
        )
    ).all()
    versions = await _application_versions(db, [application.id for application, _ in rows])
    if not message and not projects:
        message = (
            "Henüz RFS kodu tanımlı değil. Yönetici, Yönetim → Kubernetes bölümünden "
            "projenin RFS kodunu eklemelidir."
        )
    return KubernetesOptions(
        enabled=bool(registry_url) and bool(projects),
        can_publish=True,
        registry_url=registry_url,
        message=message,
        projects=[
            KubernetesProjectOption(
                id=project.id,
                rfs_code=project.rfs_code,
                name=project.name,
                namespace=project.namespace,
            )
            for project in projects
        ],
        applications=[
            KubernetesApplicationOption(
                id=application.id,
                project_id=application.project_id,
                app_name=application.app_name,
                repository=image_repository(rfs_code, application.app_name),
                last_version=highest_version(versions["published"][application.id]),
                suggested_version=suggest_next_version(versions["used"][application.id]),
            )
            for application, rfs_code in rows
        ],
    )


async def _publish_target(
    db: AsyncSession, payload: ImageReleaseCreate
) -> tuple[K8sProject, K8sApplication | None]:
    """The project and its application; None when the release is to create it."""
    project = await db.get(K8sProject, payload.project_id, populate_existing=True)
    if project is None:
        raise HTTPException(status_code=404, detail="Kubernetes projesi bulunamadı.")
    if not project.enabled:
        raise HTTPException(status_code=409, detail="Kubernetes projesi devre dışı.")
    application = (
        await db.execute(
            select(K8sApplication)
            .where(
                K8sApplication.project_id == project.id,
                K8sApplication.app_name == payload.app_name,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if application is None:
        if not app_name_fits_project(project.rfs_code, payload.app_name):
            raise HTTPException(status_code=400, detail=app_name_rule_message(project.rfs_code))
        return project, None
    if not application.enabled:
        raise HTTPException(status_code=409, detail="Kubernetes uygulaması devre dışı.")
    # The only guard against pushing an unrelated model under another team's image name.
    if application.mlflow_model_name != payload.model_name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{image_repository(project.rfs_code, application.app_name)} "
                f"{application.mlflow_model_name} modeline bağlı; başka bir model bu adla "
                "yayınlanamaz. Farklı bir uygulama adı seçin."
            ),
        )
    return project, application


@image_release_router.post("/image-releases", response_model=ImageReleaseOut, status_code=202)
async def create_image_release(
    payload: ImageReleaseCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Validate the model version and the target, then queue a durable release."""
    if not can_publish_images(current_user):
        raise HTTPException(status_code=403, detail="Kubernetes imaj yayını için yetkiniz yok.")
    try:
        deploy = await effective_deploy_registry_config(db)
        validate_deploy_registry_config(deploy, require_enabled=True)
        registry = await effective_model_container_registry_config(db)
        validate_model_container_registry_config(registry, require_enabled=True)
    except (DeployRegistryConfigurationError, ModelContainerRegistryConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Fail fast before contacting MLflow; checked again under the admission lock.
    await _publish_target(db, payload)

    client = await get_mlflow_client(db, current_user.id)
    try:
        version_payload = await client.get_model_version(payload.model_name, payload.model_version)
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

    async with admission_transaction(db):
        project, application = await _publish_target(db, payload)
        repository = image_repository(project.rfs_code, payload.app_name)
        created = application is None
        if created:
            application = K8sApplication(
                project_id=project.id,
                app_name=payload.app_name,
                mlflow_model_name=payload.model_name,
            )
            db.add(application)
            await db.flush()
        else:
            taken = (
                await db.execute(
                    select(MlflowImageRelease.id).where(
                        MlflowImageRelease.application_id == application.id,
                        MlflowImageRelease.version == payload.version,
                        MlflowImageRelease.status != ImageReleaseStatus.FAILED,
                    )
                )
            ).first()
            if taken:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{repository}:{payload.version} zaten yayınlandı veya yayınlanıyor; "
                        "farklı bir versiyon seçin."
                    ),
                )
        release = MlflowImageRelease(
            user_id=current_user.id,
            application_id=application.id,
            model_name=payload.model_name,
            model_version=payload.model_version,
            run_id=str(version.get("run_id") or ""),
            source_uri=str(version.get("source") or ""),
            version=payload.version,
            status=ImageReleaseStatus.QUEUED,
            status_message="Yayın kuyruğa alındı.",
        )
        db.add(release)
        await db.flush()
        if created:
            await append_release_event(
                db,
                release,
                f"{repository} uygulaması oluşturuldu ve {payload.model_name} modeline bağlandı.",
            )
        await append_release_event(
            db,
            release,
            f"{payload.model_name} v{payload.model_version}, {repository}:{payload.version} "
            "olarak yayın kuyruğuna alındı.",
        )
    await db.refresh(release)
    return _release_out(
        release, application=application, project=project, username=current_user.username
    )


@image_release_router.get("/image-releases", response_model=ImageReleaseList)
async def list_image_releases(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    query = (
        select(MlflowImageRelease, K8sApplication, K8sProject, User.username)
        .join(K8sApplication, K8sApplication.id == MlflowImageRelease.application_id)
        .join(K8sProject, K8sProject.id == K8sApplication.project_id)
        .join(User, User.id == MlflowImageRelease.user_id)
        .order_by(MlflowImageRelease.created_at.desc())
        .limit(200)
    )
    if current_user.role != UserRole.ADMIN:
        query = query.where(MlflowImageRelease.user_id == current_user.id)
    rows = (await db.execute(query)).all()
    return ImageReleaseList(
        releases=[
            _release_out(release, application=application, project=project, username=username)
            for release, application, project, username in rows
        ]
    )


async def _visible_release(
    db: AsyncSession, release_id: str, current_user: User
) -> MlflowImageRelease:
    release = await db.get(MlflowImageRelease, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="İmaj yayını bulunamadı.")
    if release.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Bu imaj yayını için erişim reddedildi.")
    return release


@image_release_router.get("/image-releases/{release_id}", response_model=ImageReleaseOut)
async def get_image_release(
    release_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    release = await _visible_release(db, release_id, current_user)
    return await _detailed_release_out(db, release)


@image_release_router.get("/image-releases/{release_id}/events", response_model=ImageReleaseEvents)
async def get_image_release_events(
    release_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _visible_release(db, release_id, current_user)
    events = (
        await db.execute(
            select(MlflowImageReleaseEvent)
            .where(MlflowImageReleaseEvent.release_id == release_id)
            .order_by(MlflowImageReleaseEvent.sequence)
        )
    ).scalars().all()
    return ImageReleaseEvents(events=[ImageReleaseEventOut.model_validate(item) for item in events])


@image_release_router.post(
    "/image-releases/{release_id}/retry", response_model=ImageReleaseOut, status_code=202
)
async def retry_image_release(
    release_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    release = await _visible_release(db, release_id, current_user)
    if not can_publish_images(current_user):
        raise HTTPException(status_code=403, detail="Kubernetes imaj yayını için yetkiniz yok.")
    if release.status != ImageReleaseStatus.FAILED:
        raise HTTPException(status_code=409, detail="Yalnızca başarısız yayın yeniden denenebilir.")
    failed_build_id = None
    async with admission_transaction(db):
        await db.refresh(release)
        if release.status != ImageReleaseStatus.FAILED:
            raise HTTPException(status_code=409, detail="Yalnızca başarısız yayın yeniden denenebilir.")
        taken = (
            await db.execute(
                select(MlflowImageRelease.id).where(
                    MlflowImageRelease.application_id == release.application_id,
                    MlflowImageRelease.version == release.version,
                    MlflowImageRelease.status != ImageReleaseStatus.FAILED,
                    MlflowImageRelease.id != release.id,
                )
            )
        ).first()
        if taken:
            raise HTTPException(
                status_code=409,
                detail="Bu versiyon başka bir yayında kullanılıyor; yeni bir versiyonla yayınlayın.",
            )
        if release.build_id:
            build = await db.get(MlflowModelBuild, release.build_id)
            if build and build.status == MlflowModelBuildStatus.FAILED:
                failed_build_id = build.id
                await enroll_model_build_cleanup(
                    db, failed_build_id, excluding_release_id=release.id
                )
                release.build_id = None
        release.status = ImageReleaseStatus.QUEUED
        release.status_message = "Yayın yeniden kuyruğa alındı."
        release.error_message = None
        release.completed_at = None
        db.add(release)
        await append_release_event(db, release, release.status_message)
    if failed_build_id:
        await garbage_collect_model_build(db, failed_build_id)
    await db.refresh(release)
    return await _detailed_release_out(db, release)


@image_release_router.post(
    "/image-releases/{release_id}/bamboo-retry", response_model=ImageReleaseOut, status_code=202
)
async def retry_bamboo_trigger(
    release_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Queue the Bamboo retag plan again for a published release whose trigger failed."""
    release = await _visible_release(db, release_id, current_user)
    if not can_publish_images(current_user):
        raise HTTPException(status_code=403, detail="Kubernetes imaj yayını için yetkiniz yok.")
    claimed = await db.execute(
        update(MlflowImageRelease)
        .where(
            MlflowImageRelease.id == release.id,
            MlflowImageRelease.status == ImageReleaseStatus.PUBLISHED,
            MlflowImageRelease.bamboo_status == BambooTriggerStatus.FAILED,
        )
        .values(bamboo_status=BambooTriggerStatus.PENDING, bamboo_error=None)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Yalnızca Bamboo tetiklemesi başarısız olan yayınlar yeniden denenebilir.",
        )
    await append_release_event(db, release, "Bamboo retag planı yeniden tetiklenecek.")
    await db.commit()
    await db.refresh(release)
    return await _detailed_release_out(db, release)
