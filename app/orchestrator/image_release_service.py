"""Durable publishing of MLflow model images to the Kubernetes deploy registry.

A release builds (or reuses) the serving image of one model version in the
Model Container Registry, then the controller copies that tag to
``{deploy registry}/{rfs_code}/{app_name}:{X.Y.Z}``. The platform team re-tags
and deploys from there; nothing here ever deletes or overwrites a deploy tag.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update

from app.bamboo import (
    BambooConfigurationError,
    BambooTriggerError,
    effective_bamboo_config,
    queue_retag_build,
    retag_variables,
)
from app.database import AsyncSessionLocal
from app.deploy_registry import (
    DeployRegistryConfig,
    effective_deploy_registry_config,
    validate_deploy_registry_config,
)
from app.integrations.mlflow import MlflowClient
from app.kubernetes_publish import image_reference
from app.model_container_registry import effective_model_container_registry_config
from app.models.bamboo_settings import BambooSettings
from app.models.kubernetes import (
    BambooTriggerStatus,
    ImageReleaseStatus,
    K8sApplication,
    K8sProject,
    MlflowImageRelease,
    MlflowImageReleaseEvent,
)
from app.models.mlflow_deployment import MlflowModelBuild, MlflowModelBuildStatus
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.mlflow_deployment_service import (
    _LEASE_OWNER,
    _LEASE_SECONDS,
    _mlflow_client_for_user,
    ensure_model_build,
    model_build_source_ref,
)
from app.task_health import task_health
from app.workspace_image_service import copy_registry_image, registry_tag_digest

logger = logging.getLogger("devcloud.mlflow.image_releases")

MLFLOW_IMAGE_TAG = "aifactory.image"
MLFLOW_DIGEST_TAG = "aifactory.image_digest"
_BUILD_ACTIVE_STATUSES = {
    MlflowModelBuildStatus.VALIDATING,
    MlflowModelBuildStatus.BUILDING,
    MlflowModelBuildStatus.IMPORTING,
}
_FAILURE_DETAIL_LIMIT = 900


class ReleaseLeaseLost(RuntimeError):
    pass


async def append_release_event(
    db,
    release: MlflowImageRelease,
    message: str,
    level: str = "info",
) -> None:
    sequence = (
        await db.execute(
            select(func.coalesce(func.max(MlflowImageReleaseEvent.sequence), 0)).where(
                MlflowImageReleaseEvent.release_id == release.id
            )
        )
    ).scalar_one()
    db.add(
        MlflowImageReleaseEvent(
            release_id=release.id,
            sequence=int(sequence) + 1,
            level=level[:16],
            message=message[:1000],
        )
    )
    # The session does not autoflush: without this, two events added before a
    # commit would both read the same max(sequence).
    await db.flush()


async def _assert_release_lease(db, release: MlflowImageRelease) -> None:
    # Read only the owner: the session does not autoflush, so a refresh() would
    # drop release fields set but not yet committed (image_ref, digest, ...).
    owner = (
        await db.execute(
            select(MlflowImageRelease.lease_owner).where(MlflowImageRelease.id == release.id)
        )
    ).scalar_one_or_none()
    if owner != _LEASE_OWNER:
        raise ReleaseLeaseLost("İmaj yayını başka bir controller süreci tarafından devralındı.")


async def set_release_stage(
    db,
    release: MlflowImageRelease,
    status: str,
    message: str,
    *,
    level: str = "info",
    error: str | None = None,
) -> None:
    await _assert_release_lease(db, release)
    release.status = status
    release.status_message = message[:500]
    release.error_message = error
    if status in ImageReleaseStatus.TERMINAL:
        release.lease_owner = ""
        release.lease_expires_at = None
        release.completed_at = datetime.now(timezone.utc)
    else:
        release.lease_owner = _LEASE_OWNER
        release.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_LEASE_SECONDS
        )
    await append_release_event(db, release, message, level)
    db.add(release)
    await db.commit()


async def _release_lease_heartbeat(
    release_id: str,
    build_id: str | None = None,
    interval_seconds: int = 30,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        async with AsyncSessionLocal() as lease_db:
            renewed = await lease_db.execute(
                update(MlflowImageRelease)
                .where(
                    MlflowImageRelease.id == release_id,
                    MlflowImageRelease.lease_owner == _LEASE_OWNER,
                    MlflowImageRelease.status.in_(ImageReleaseStatus.ACTIVE),
                )
                .values(lease_expires_at=deadline)
            )
            if renewed.rowcount != 1:
                await lease_db.rollback()
                return
            if build_id:
                await lease_db.execute(
                    update(MlflowModelBuild)
                    .where(
                        MlflowModelBuild.id == build_id,
                        MlflowModelBuild.lease_owner == _LEASE_OWNER,
                        MlflowModelBuild.status.in_(_BUILD_ACTIVE_STATUSES),
                    )
                    .values(lease_expires_at=deadline)
                )
            await lease_db.commit()


@asynccontextmanager
async def _maintain_release_lease(release_id: str, build_id: str | None = None):
    task = asyncio.create_task(_release_lease_heartbeat(release_id, build_id))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _ReleaseBuildOwner:
    """Lets ensure_model_build log to and lease through a release."""

    def __init__(self, release: MlflowImageRelease):
        self.row = release

    @property
    def user_id(self) -> int:
        return self.row.user_id

    @property
    def model_name(self) -> str:
        return self.row.model_name

    @property
    def model_version(self) -> str:
        return self.row.model_version

    @property
    def run_id(self) -> str:
        return self.row.run_id

    @property
    def source_uri(self) -> str:
        return self.row.source_uri

    @property
    def build_id(self) -> str | None:
        return self.row.build_id

    def set_build(self, db, build_id: str) -> None:
        self.row.build_id = build_id
        db.add(self.row)

    async def append_event(self, db, message: str, level: str = "info") -> None:
        await append_release_event(db, self.row, message, level)

    async def assert_lease(self, db) -> None:
        await _assert_release_lease(db, self.row)

    def maintain_lease(self, build_id: str | None):
        return _maintain_release_lease(self.row.id, build_id)

    def cleanup_exclusion(self) -> dict[str, str]:
        return {"excluding_release_id": self.row.id}


async def _release_target(
    db, release: MlflowImageRelease
) -> tuple[K8sApplication, K8sProject]:
    application = await db.get(K8sApplication, release.application_id)
    project = await db.get(K8sProject, application.project_id) if application else None
    if application is None or project is None:
        raise RuntimeError("Kubernetes uygulaması veya projesi artık bulunamıyor.")
    if not application.enabled or not project.enabled:
        raise RuntimeError("Kubernetes uygulaması veya projesi devre dışı bırakıldı.")
    if application.mlflow_model_name != release.model_name:
        raise RuntimeError(
            f"{application.app_name} uygulaması artık {release.model_name} modeline bağlı değil."
        )
    return application, project


def _deploy_tag_digest(config: DeployRegistryConfig, image_ref: str) -> str | None:
    return registry_tag_digest(
        image_ref=image_ref,
        username=config.username,
        password=config.password,
        tls_verify=config.tls_verify,
    )


async def _tag_mlflow_version(
    db,
    release: MlflowImageRelease,
    client: MlflowClient,
) -> bool:
    """Best effort: a missing EDIT permission in MLflow must not fail the release."""
    try:
        await client.set_model_version_tag(
            release.model_name, release.model_version, MLFLOW_IMAGE_TAG, release.image_ref
        )
        await client.set_model_version_tag(
            release.model_name, release.model_version, MLFLOW_DIGEST_TAG, release.digest
        )
    except Exception as exc:  # noqa: BLE001 - tagging must never fail a release
        await append_release_event(
            db,
            release,
            f"MLflow model versiyonuna yayın etiketi yazılamadı: {str(exc)[:500]}",
            "warning",
        )
        return False
    await append_release_event(
        db,
        release,
        f"MLflow model versiyonuna {MLFLOW_IMAGE_TAG} ve {MLFLOW_DIGEST_TAG} etiketleri yazıldı.",
    )
    return True


async def _mark_published(
    db,
    release: MlflowImageRelease,
    digest: str,
    message: str,
    *,
    level: str = "success",
    client: MlflowClient | None = None,
) -> None:
    release.digest = digest
    if client is None:
        try:
            client = await _mlflow_client_for_user(db, release.user_id)
        except Exception as exc:  # noqa: BLE001 - tagging is best effort
            await append_release_event(
                db,
                release,
                f"MLflow model versiyonuna yayın etiketi yazılamadı: {str(exc)[:500]}",
                "warning",
            )
    release.mlflow_tagged = bool(client) and await _tag_mlflow_version(db, release, client)
    # Committed together with PUBLISHED, so a restart cannot lose the trigger;
    # the worker queues the Bamboo plan afterwards (trigger_pending_bamboo_builds).
    bamboo = await db.get(BambooSettings, 1)
    if bamboo is not None and bamboo.enabled:
        release.bamboo_status = BambooTriggerStatus.PENDING
    await set_release_stage(db, release, ImageReleaseStatus.PUBLISHED, message, level=level)


async def _settle_started_publish(
    db,
    release: MlflowImageRelease,
    failure: BaseException,
) -> bool:
    """Decide a PUBLISHING release whose copy began but whose run then failed.

    The tag was checked to be free right before PUBLISHING, so if it exists now
    it holds this release's image: record it as published instead of failing a
    version the platform team can already see. Returns False when the tag is
    absent (the copy itself failed) so the caller marks the release failed.
    """
    try:
        deploy = await effective_deploy_registry_config(db)
        digest = await asyncio.to_thread(_deploy_tag_digest, deploy, release.image_ref)
    except Exception as exc:  # noqa: BLE001 - unknown outcome, decide later
        logger.warning("Image release %s publish outcome unknown: %s", release.id, exc)
        await _assert_release_lease(db, release)
        release.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        await append_release_event(
            db,
            release,
            "Kopyalamanın sonucu doğrulanamadı; registry'ye yeniden erişildiğinde "
            f"kontrol edilecek: {str(exc)[:400]}",
            "warning",
        )
        db.add(release)
        await db.commit()
        return True
    if not digest:
        return False
    await _mark_published(
        db,
        release,
        digest,
        f"Image yayınlandı: {release.image_ref} ({digest}); kopyalamadan sonraki bir "
        f"adım başarısız oldu ({str(failure)[:200]}) ancak etiket registry'de.",
        level="warning",
    )
    return True


async def _append_failure_detail(db, release: MlflowImageRelease, exc: BaseException) -> None:
    detail = str(exc).strip() or exc.__class__.__name__
    label = "Hata ayrıntısı"
    if len(detail) > _FAILURE_DETAIL_LIMIT:
        detail = detail[-_FAILURE_DETAIL_LIMIT:]
        label = f"Hata ayrıntısı (son {_FAILURE_DETAIL_LIMIT} karakter)"
    try:
        await append_release_event(db, release, f"{label}: {detail}", "error")
        await db.commit()
    except Exception:  # pragma: no cover - diagnostics must never mask a failure
        logger.exception("Could not record failure detail for image release %s", release.id)
        await db.rollback()


async def process_image_release(release_id: str) -> None:
    async with AsyncSessionLocal() as db:
        claimed = await db.execute(
            update(MlflowImageRelease)
            .where(
                MlflowImageRelease.id == release_id,
                MlflowImageRelease.status == ImageReleaseStatus.QUEUED,
            )
            .values(
                status=ImageReleaseStatus.BUILDING,
                status_message="Model versiyonu doğrulanıyor.",
                error_message=None,
                lease_owner=_LEASE_OWNER,
                lease_expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=_LEASE_SECONDS),
            )
        )
        await db.commit()
        if claimed.rowcount != 1:
            return
        release = await db.get(MlflowImageRelease, release_id)
        if release is None:
            return
        await append_release_event(db, release, "Yayın kuyruğundan alındı.")
        await db.commit()

        try:
            client = await _mlflow_client_for_user(db, release.user_id)
            payload = await client.get_model_version(release.model_name, release.model_version)
            version = payload.get("model_version") or {}
            if (
                str(version.get("name") or "") != release.model_name
                or str(version.get("version") or "") != release.model_version
            ):
                raise RuntimeError("MLflow beklenen model versiyonunu döndürmedi.")
            release.run_id = str(version.get("run_id") or release.run_id)
            release.source_uri = str(version.get("source") or release.source_uri)
            application, project = await _release_target(db, release)
            deploy = await effective_deploy_registry_config(db)
            validate_deploy_registry_config(deploy, require_enabled=True)
            release.image_ref = image_reference(
                deploy.registry_url, project.rfs_code, application.app_name, release.version
            )
            await set_release_stage(
                db,
                release,
                ImageReleaseStatus.BUILDING,
                f"{release.model_name} v{release.model_version} doğrulandı; "
                f"hedef {release.image_ref}.",
                level="success",
            )
            image_ref = release.image_ref
            # Fail before a long build when the version is already taken.
            existing = await asyncio.to_thread(_deploy_tag_digest, deploy, image_ref)
            if existing:
                raise RuntimeError(
                    f"{image_ref} dağıtım registry'sinde zaten var ({existing}); "
                    "üzerine yazılmaz, farklı bir versiyon seçin."
                )

            build = await ensure_model_build(
                db, _ReleaseBuildOwner(release), client, import_image=False
            )
            registry = await effective_model_container_registry_config(db)
            image = (
                await db.get(WorkspaceImage, build.workspace_image_id)
                if build.workspace_image_id
                else None
            )
            release.source_ref = model_build_source_ref(build, image, registry.registry_url)
            # The build can take half an hour; re-check before PUBLISHING so that
            # state always means "the tag was free and the copy began".
            existing = await asyncio.to_thread(_deploy_tag_digest, deploy, image_ref)
            if existing:
                raise RuntimeError(
                    f"{image_ref} build sırasında başka biri tarafından yüklendi "
                    f"({existing}); üzerine yazılmadı."
                )
            await set_release_stage(
                db,
                release,
                ImageReleaseStatus.PUBLISHING,
                f"Image dağıtım registry'sine kopyalanıyor: {image_ref}.",
            )
            source_ref = release.source_ref
            async with _maintain_release_lease(release.id):
                await asyncio.to_thread(
                    copy_registry_image,
                    source_ref=source_ref,
                    destination_ref=image_ref,
                    source_username=registry.username,
                    source_password=registry.password,
                    destination_username=deploy.username,
                    destination_password=deploy.password,
                    destination_tls_verify=deploy.tls_verify,
                    manifest_format=deploy.manifest_format,
                )
                digest = await asyncio.to_thread(_deploy_tag_digest, deploy, image_ref)
            if not digest:
                raise RuntimeError(
                    f"Kopyalama tamamlandı ancak {image_ref} registry'de bulunamadı."
                )
            await _assert_release_lease(db, release)
            await _mark_published(
                db,
                release,
                digest,
                f"Image yayınlandı: {image_ref} ({digest}). Platform ekibi bu imajı "
                "RC/build numarasıyla etiketleyip Kubernetes'e dağıtır.",
                client=client,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Kubernetes image release %s failed", release_id)
            await db.rollback()
            release = await db.get(MlflowImageRelease, release_id)
            if release is None or release.lease_owner != _LEASE_OWNER:
                return
            try:
                if release.status == ImageReleaseStatus.PUBLISHING and await _settle_started_publish(
                    db, release, exc
                ):
                    return
            except ReleaseLeaseLost:
                return
            if release.build_id:
                build = await db.get(MlflowModelBuild, release.build_id)
                if build and build.status in _BUILD_ACTIVE_STATUSES:
                    build.status = MlflowModelBuildStatus.FAILED
                    build.status_message = "Model image build başarısız oldu."
                    build.error_message = str(exc)[:4000]
                    build.lease_owner = ""
                    build.lease_expires_at = None
                    build.completed_at = datetime.now(timezone.utc)
                    db.add(build)
            await set_release_stage(
                db,
                release,
                ImageReleaseStatus.FAILED,
                "Kubernetes imaj yayını başarısız oldu.",
                level="error",
                error=str(exc)[:4000],
            )
            await _append_failure_detail(db, release, exc)


async def _recover_stale_release(db, release: MlflowImageRelease) -> None:
    claim = await db.execute(
        update(MlflowImageRelease)
        .where(
            MlflowImageRelease.id == release.id,
            MlflowImageRelease.status.in_(
                {ImageReleaseStatus.BUILDING, ImageReleaseStatus.PUBLISHING}
            ),
            or_(
                MlflowImageRelease.lease_expires_at.is_(None),
                MlflowImageRelease.lease_expires_at < datetime.now(timezone.utc),
            ),
        )
        .values(
            lease_owner=_LEASE_OWNER,
            # Longer than the registry check below, so no other process can
            # claim the release while it runs.
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS),
        )
        # SQLite returns naive datetimes; do not re-evaluate the criteria in Python.
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if claim.rowcount != 1:
        return
    await db.refresh(release)
    try:
        if release.status == ImageReleaseStatus.PUBLISHING and release.image_ref:
            # The copy may have finished just before the restart.
            if await _settle_started_publish(
                db, release, RuntimeError("controller kopyalama sırasında yeniden başladı")
            ):
                return
        await _assert_release_lease(db, release)
    except ReleaseLeaseLost:
        return
    release.status = ImageReleaseStatus.QUEUED
    release.status_message = "Yarım kalan yayın devam ettirilecek."
    release.error_message = None
    release.lease_owner = ""
    release.lease_expires_at = None
    await append_release_event(db, release, release.status_message, "warning")
    db.add(release)
    await db.commit()


async def recover_interrupted_image_releases() -> None:
    async with AsyncSessionLocal() as db:
        releases = (
            await db.execute(
                select(MlflowImageRelease).where(
                    MlflowImageRelease.status.in_(
                        {ImageReleaseStatus.BUILDING, ImageReleaseStatus.PUBLISHING}
                    ),
                    or_(
                        MlflowImageRelease.lease_expires_at.is_(None),
                        MlflowImageRelease.lease_expires_at < datetime.now(timezone.utc),
                    ),
                )
            )
        ).scalars().all()
        for release in releases:
            await _recover_stale_release(db, release)


# Longer than the Bamboo request timeout: a TRIGGERING claim older than this
# belongs to a controller that stopped mid-request.
_BAMBOO_CLAIM_SECONDS = 120


async def _finish_bamboo_trigger(
    db,
    release: MlflowImageRelease,
    status: str,
    message: str,
    level: str,
    *,
    error: str | None = None,
    result_key: str = "",
    result_url: str = "",
) -> None:
    release.bamboo_status = status
    release.bamboo_error = error
    release.bamboo_result_key = result_key
    release.bamboo_result_url = result_url
    release.bamboo_attempted_at = datetime.now(timezone.utc)
    await append_release_event(db, release, message, level)
    db.add(release)
    await db.commit()


async def trigger_bamboo_build(release_id: str) -> None:
    """Queue the retag plan for one PENDING release; never changes the release status."""
    async with AsyncSessionLocal() as db:
        claimed = await db.execute(
            update(MlflowImageRelease)
            .where(
                MlflowImageRelease.id == release_id,
                MlflowImageRelease.status == ImageReleaseStatus.PUBLISHED,
                MlflowImageRelease.bamboo_status == BambooTriggerStatus.PENDING,
            )
            .values(
                bamboo_status=BambooTriggerStatus.TRIGGERING,
                bamboo_attempted_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if claimed.rowcount != 1:
            return
        release = await db.get(MlflowImageRelease, release_id)
        if release is None:
            return
        try:
            config = await effective_bamboo_config(db)
            application = await db.get(K8sApplication, release.application_id)
            project = await db.get(K8sProject, application.project_id) if application else None
            if application is None or project is None:
                raise BambooTriggerError("Kubernetes uygulaması veya projesi artık bulunamıyor.")
            variables = retag_variables(project.rfs_code, application.app_name, release.version)
            result_key, result_url = await queue_retag_build(config, variables)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a Bamboo failure never fails a release
            known = isinstance(exc, (BambooConfigurationError, BambooTriggerError))
            if not known:
                logger.exception("Bamboo trigger for image release %s failed", release_id)
            detail = str(exc)[:500] or exc.__class__.__name__
            await db.rollback()
            release = await db.get(MlflowImageRelease, release_id)
            if release is None:
                return
            await _finish_bamboo_trigger(
                db,
                release,
                BambooTriggerStatus.FAILED,
                f"Bamboo retag planı tetiklenemedi; imaj yayında, Jira görevleri açılmadı: {detail}",
                "warning",
                error=detail,
            )
            return
        await _finish_bamboo_trigger(
            db,
            release,
            BambooTriggerStatus.QUEUED,
            f"Bamboo {config.plan_key} planı kuyruğa alındı"
            + (f" ({result_key})." if result_key else "."),
            "success",
            result_key=result_key,
            result_url=result_url,
        )


async def _fail_stale_bamboo_triggers(db) -> None:
    """A claim left by a stopped controller: the outcome is unknown, so do not resend.

    Queuing the plan twice would open duplicate Jira tasks; the user checks
    Bamboo and retries by hand if needed.
    """
    deadline = datetime.now(timezone.utc) - timedelta(seconds=_BAMBOO_CLAIM_SECONDS)
    stale = (
        await db.execute(
            select(MlflowImageRelease).where(
                MlflowImageRelease.bamboo_status == BambooTriggerStatus.TRIGGERING,
                or_(
                    MlflowImageRelease.bamboo_attempted_at.is_(None),
                    MlflowImageRelease.bamboo_attempted_at < deadline,
                ),
            )
        )
    ).scalars().all()
    for release in stale:
        detail = "Controller istek sırasında durdu; Bamboo'da kontrol edip gerekirse yeniden deneyin."
        claimed = await db.execute(
            update(MlflowImageRelease)
            .where(
                MlflowImageRelease.id == release.id,
                MlflowImageRelease.bamboo_status == BambooTriggerStatus.TRIGGERING,
            )
            .values(bamboo_status=BambooTriggerStatus.FAILED, bamboo_error=detail)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount == 1:
            await append_release_event(
                db, release, f"Bamboo tetiklemesinin sonucu bilinmiyor. {detail}", "warning"
            )
        await db.commit()


async def trigger_pending_bamboo_builds() -> None:
    async with AsyncSessionLocal() as db:
        await _fail_stale_bamboo_triggers(db)
        release_ids = list(
            (
                await db.execute(
                    select(MlflowImageRelease.id)
                    .where(
                        MlflowImageRelease.status == ImageReleaseStatus.PUBLISHED,
                        MlflowImageRelease.bamboo_status == BambooTriggerStatus.PENDING,
                    )
                    .order_by(MlflowImageRelease.completed_at)
                    .limit(5)
                )
            ).scalars()
        )
        await db.commit()
    for release_id in release_ids:
        await trigger_bamboo_build(release_id)


async def image_release_background_worker(check_interval_seconds: int = 2) -> None:
    """Separate from the deployment loop, so a long build never blocks the other."""
    while True:
        try:
            await recover_interrupted_image_releases()
            async with AsyncSessionLocal() as db:
                release_ids = list(
                    (
                        await db.execute(
                            select(MlflowImageRelease.id)
                            .where(MlflowImageRelease.status == ImageReleaseStatus.QUEUED)
                            .order_by(MlflowImageRelease.created_at)
                            .limit(5)
                        )
                    ).scalars()
                )
                await db.commit()
            for release_id in release_ids:
                await process_image_release(release_id)
            await trigger_pending_bamboo_builds()
            task_health.success("image_releases")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task_health.failure("image_releases", exc)
            logger.exception("Image release worker iteration failed")
        await asyncio.sleep(check_interval_seconds)
