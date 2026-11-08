"""Durable orchestration for immutable MLflow model service deployments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, or_, select, update

from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager
from app.config import settings
from app.database import AsyncSessionLocal
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    config_from_record,
    validate_config,
)
from app.mlflow_workspace import environment_from_config
from app.model_container_registry import (
    ModelContainerRegistryConfig,
    effective_model_container_registry_config,
    validate_model_container_registry_config,
)
from app.models.mlflow_deployment import (
    MlflowDeployment,
    MlflowDeploymentEvent,
    MlflowDeploymentStatus,
    MlflowModelBuild,
    MlflowModelBuildStatus,
)
from app.models.kubernetes import ImageReleaseStatus, MlflowImageRelease
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_settings import MlflowSettings
from app.models.node import Node, NodeStatus
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.flavors import get_flavor
from app.orchestrator.admission import admission_transaction
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from app.orchestrator.templates import get_template
from app.task_health import task_health
from app.orchestrator.workspace_provisioning import (
    QuotaExceeded,
    available_workspace_name,
    delete_workspace_resources,
    flavor_definition,
    get_quota_error,
    schedule_and_reserve_workspace,
    template_definition,
    workspace_runtime_image,
)
from app.schemas.workspace import WorkspaceCreate
from app.workspace_catalog import resolve_flavor
from app.workspace_image_service import (
    WorkspaceImageError,
    delete_registry_image,
    image_archive_path,
    import_registry_image,
)

logger = logging.getLogger("aifactory.mlflow.deployments")
_LEASE_OWNER = f"{socket.gethostname()}:{os.getpid()}"
_LEASE_SECONDS = 300
_ACTIVE_DEPLOYMENT_STATUSES = {
    MlflowDeploymentStatus.WAITING_FOR_IMAGE,
    MlflowDeploymentStatus.SCHEDULING,
    MlflowDeploymentStatus.STARTING,
    MlflowDeploymentStatus.HEALTH_CHECKING,
    MlflowDeploymentStatus.DELETING,
}


class DeploymentLeaseLost(RuntimeError):
    pass


async def _assert_deployment_lease(db, deployment: MlflowDeployment) -> None:
    await db.refresh(deployment)
    if deployment.lease_owner != _LEASE_OWNER:
        raise DeploymentLeaseLost("Deployment lease başka bir worker tarafından devralındı.")


async def _lease_heartbeat(
    deployment_id: str,
    build_id: str | None = None,
    interval_seconds: int = 30,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        async with AsyncSessionLocal() as lease_db:
            deployment_result = await lease_db.execute(
                update(MlflowDeployment)
                .where(
                    MlflowDeployment.id == deployment_id,
                    MlflowDeployment.lease_owner == _LEASE_OWNER,
                    MlflowDeployment.status.in_(_ACTIVE_DEPLOYMENT_STATUSES),
                )
                .values(lease_expires_at=deadline)
            )
            if deployment_result.rowcount != 1:
                await lease_db.rollback()
                return
            if build_id:
                await lease_db.execute(
                    update(MlflowModelBuild)
                    .where(
                        MlflowModelBuild.id == build_id,
                        MlflowModelBuild.lease_owner == _LEASE_OWNER,
                        MlflowModelBuild.status.in_(
                            {
                                MlflowModelBuildStatus.VALIDATING,
                                MlflowModelBuildStatus.BUILDING,
                                MlflowModelBuildStatus.IMPORTING,
                            }
                        ),
                    )
                    .values(lease_expires_at=deadline)
                )
            await lease_db.commit()


@asynccontextmanager
async def _maintain_deployment_lease(
    deployment_id: str,
    build_id: str | None = None,
):
    task = asyncio.create_task(_lease_heartbeat(deployment_id, build_id))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def append_deployment_event(
    db,
    deployment: MlflowDeployment,
    message: str,
    level: str = "info",
) -> None:
    sequence = (
        await db.execute(
            select(func.coalesce(func.max(MlflowDeploymentEvent.sequence), 0)).where(
                MlflowDeploymentEvent.deployment_id == deployment.id
            )
        )
    ).scalar_one()
    db.add(
        MlflowDeploymentEvent(
            deployment_id=deployment.id,
            sequence=int(sequence) + 1,
            level=level[:16],
            message=message[:1000],
        )
    )


async def set_deployment_stage(
    db,
    deployment: MlflowDeployment,
    status: MlflowDeploymentStatus,
    message: str,
    *,
    level: str = "info",
    error: str | None = None,
) -> None:
    await _assert_deployment_lease(db, deployment)
    deployment.status = status
    deployment.status_message = message[:500]
    deployment.error_message = error
    if status in {MlflowDeploymentStatus.FAILED, MlflowDeploymentStatus.DELETING}:
        deployment.quota_reserved = False
    if status in {
        MlflowDeploymentStatus.RUNNING,
        MlflowDeploymentStatus.STOPPED,
        MlflowDeploymentStatus.FAILED,
        MlflowDeploymentStatus.DELETING,
    }:
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
    else:
        deployment.lease_owner = _LEASE_OWNER
        deployment.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_LEASE_SECONDS
        )
    await append_deployment_event(db, deployment, message, level)
    db.add(deployment)
    await db.commit()


async def _mlflow_client_for_user(db, user_id: int) -> MlflowClient:
    credentials = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()
    server = await db.get(MlflowServerSettings, 1)
    if credentials is None or server is None:
        raise MlflowConfigurationError("MLflow bağlantısı yapılandırılmamış.")
    config = config_from_record(credentials, server)
    validate_config(config, require_enabled=True)
    return MlflowClient(config)


async def _reserve_deployment_quota(db, deployment: MlflowDeployment) -> None:
    if deployment.quota_reserved:
        return
    async with admission_transaction(db):
        await db.refresh(deployment)
        if deployment.quota_reserved:
            return
        user = await db.get(User, deployment.user_id, populate_existing=True)
        flavor = await resolve_flavor(db, deployment.flavor_id)
        if user is None or flavor is None:
            raise RuntimeError("Deployment kullanıcısı veya kaynak profili bulunamadı.")
        error = await get_quota_error(db, user, flavor)
        if error:
            raise QuotaExceeded(error)
        deployment.quota_reserved = True
        db.add(deployment)


def _mlflow_ca_certificate(client: MlflowClient) -> str:
    path_value = client.config.ca_cert_file.strip()
    if not path_value:
        return ""
    path = Path(path_value).resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("MLflow özel CA dosyası worker build için okunamadı.") from exc
    if len(content.encode("utf-8")) > 1024 * 1024 or "BEGIN CERTIFICATE" not in content:
        raise RuntimeError("MLflow özel CA dosyası geçersiz veya çok büyük.")
    return content


def _image_tag(build: MlflowModelBuild, registry_url: str) -> str:
    prefix = registry_url.rstrip("/")
    if not prefix:
        raise RuntimeError("Model Container Registry yapılandırılmamış.")
    safe_name = re.sub(r"[^a-z0-9._-]+", "-", build.model_name.lower()).strip("-.")
    safe_name = safe_name[:80] or "model"
    return (
        f"{prefix}/devcloud-mlflow-{safe_name}-v{build.model_version}:"
        f"{build.id[:12]}"
    )


# A failed release keeps its build this long, so fixing the deploy registry
# (rights, manifest format) and retrying does not rebuild the image.
_FAILED_RELEASE_BUILD_RETENTION = timedelta(hours=24)


def _release_pins_build():
    """Releases that still need their build: in progress, or failed recently."""
    return or_(
        MlflowImageRelease.status.in_(ImageReleaseStatus.ACTIVE),
        (MlflowImageRelease.status == ImageReleaseStatus.FAILED)
        & (
            MlflowImageRelease.completed_at
            > datetime.now(timezone.utc) - _FAILED_RELEASE_BUILD_RETENTION
        ),
    )


async def _model_build_references(
    db,
    build_id: str,
    *,
    excluding_deployment_id: str | None = None,
    excluding_release_id: str | None = None,
) -> tuple[int, int]:
    """Count deployments and image releases that still need a build."""
    deployment_query = select(func.count(MlflowDeployment.id)).where(
        MlflowDeployment.build_id == build_id
    )
    if excluding_deployment_id:
        deployment_query = deployment_query.where(
            MlflowDeployment.id != excluding_deployment_id
        )
    # A published release does not pin its build: the copy lives in the
    # deploy registry.
    release_query = select(func.count(MlflowImageRelease.id)).where(
        MlflowImageRelease.build_id == build_id,
        _release_pins_build(),
    )
    if excluding_release_id:
        release_query = release_query.where(MlflowImageRelease.id != excluding_release_id)
    return (
        (await db.execute(deployment_query)).scalar_one(),
        (await db.execute(release_query)).scalar_one(),
    )


async def enroll_model_build_cleanup(
    db,
    build_id: str | None,
    *,
    excluding_deployment_id: str | None = None,
    excluding_release_id: str | None = None,
) -> bool:
    """Mark a last-reference build for durable cleanup in the caller transaction."""
    if not build_id:
        return False
    if any(
        await _model_build_references(
            db,
            build_id,
            excluding_deployment_id=excluding_deployment_id,
            excluding_release_id=excluding_release_id,
        )
    ):
        return False
    build = await db.get(MlflowModelBuild, build_id)
    if build is None:
        return False
    build.status = MlflowModelBuildStatus.DELETING
    build.status_message = "Model image artıkları temizleme kuyruğuna alındı."
    build.lease_owner = ""
    build.lease_expires_at = None
    if build.workspace_image_id:
        image = await db.get(WorkspaceImage, build.workspace_image_id)
        if image:
            image.enabled = False
            db.add(image)
    db.add(build)
    return True


async def enroll_orphaned_model_builds(db, limit: int = 20) -> int:
    enrolled = 0
    async with admission_transaction(db):
        build_ids = list(
            (
                await db.execute(
                    select(MlflowModelBuild.id)
                    .where(
                        MlflowModelBuild.status.in_(
                            {
                                MlflowModelBuildStatus.READY,
                                MlflowModelBuildStatus.FAILED,
                            }
                        ),
                        ~select(MlflowDeployment.id)
                        .where(MlflowDeployment.build_id == MlflowModelBuild.id)
                        .exists(),
                        ~select(MlflowImageRelease.id)
                        .where(
                            MlflowImageRelease.build_id == MlflowModelBuild.id,
                            _release_pins_build(),
                        )
                        .exists(),
                    )
                    .limit(limit)
                )
            ).scalars()
        )
        for build_id in build_ids:
            enrolled += int(await enroll_model_build_cleanup(db, build_id))
    return enrolled


async def garbage_collect_model_build(
    db,
    build_id: str | None,
    *,
    pending: list[str] | None = None,
) -> bool:
    """Persist and retry cleanup until registry, workers, and archive agree.

    When it returns False, ``pending`` (if given) receives one readable reason
    per blocker, so an admin can see what the retry loop is waiting for.
    """
    reasons = pending if pending is not None else []
    if not build_id:
        return True
    references, release_references = await _model_build_references(db, build_id)
    if references:
        reasons.append(f"{references} model deployment bu image'ı kullanıyor.")
    if release_references:
        reasons.append(
            f"{release_references} Kubernetes imaj yayını bu image'ı kullanıyor "
            "(başarısız yayınlar 24 saat tutar)."
        )
    if references or release_references:
        return False
    build = await db.get(MlflowModelBuild, build_id)
    if build is None:
        return True
    registry = await effective_model_container_registry_config(db)
    image = (
        await db.get(WorkspaceImage, build.workspace_image_id)
        if build.workspace_image_id
        else None
    )
    if image:
        workspace_references = (
            await db.execute(
                select(func.count(Workspace.id)).where(Workspace.image_id == image.id)
            )
        ).scalar_one()
        if workspace_references:
            reasons.append(
                f"{workspace_references} workspace bu image'a sabitlenmiş."
            )
            return False
    build.status = MlflowModelBuildStatus.DELETING
    build.status_message = "Model image artıkları temizleniyor."
    build.lease_owner = ""
    build.lease_expires_at = None
    if image:
        image.enabled = False
        db.add(image)
    db.add(build)
    await db.commit()

    image_ref = (
        image.image_ref
        if image
        else _image_tag(build, registry.registry_url)
    )
    source_ref = image.source_ref if image else image_ref
    pending_worker = False
    nodes = (await db.execute(select(Node))).scalars().all()
    await db.commit()
    for node in nodes:
        try:
            capabilities = json.loads(node.capabilities_json or "{}")
        except ValueError:
            capabilities = {}
        reported = any(
            isinstance(item, dict) and item.get("image_ref") == image_ref
            for item in (capabilities.get("workspace_images") or [])
        )
        if not reported:
            continue
        if not agent_manager.is_connected(node.id):
            pending_worker = True
            reasons.append(
                f"Worker '{node.name}' çevrimdışı; image bağlandığında kaldırılacak."
            )
            continue
        try:
            result = await agent_manager.get(node.id).request(
                "image.remove", {"image_tag": image_ref}, timeout=130
            )
            if not bool(result.get("success")):
                pending_worker = True
                reasons.append(
                    f"Worker '{node.name}' image'ı kaldıramadı: "
                    f"{str(result.get('message') or 'bilinmeyen hata')[:300]}"
                )
        except (AgentCommandError, AgentUnavailable, TimeoutError) as exc:
            pending_worker = True
            reasons.append(
                f"Worker '{node.name}' image'ı kaldıramadı: {str(exc)[:300] or type(exc).__name__}"
            )

    registry_deleted = True
    try:
        await asyncio.to_thread(
            delete_registry_image,
            source_ref=source_ref,
            username=registry.username,
            password=registry.password,
        )
    except WorkspaceImageError as exc:
        registry_deleted = False
        reasons.append(
            f"Registry'deki {source_ref} silinemedi (registry hesabının silme "
            f"yetkisini kontrol edin): {str(exc)[:300]}"
        )
        logger.warning("Registry model image cleanup remains pending: %s", source_ref)
    if pending_worker or not registry_deleted:
        return False

    build = await db.get(MlflowModelBuild, build_id)
    image = (
        await db.get(WorkspaceImage, build.workspace_image_id)
        if build and build.workspace_image_id
        else None
    )
    archive_path = image_archive_path(image.filename) if image else None
    if build:
        await db.delete(build)
    if image:
        await db.delete(image)
    await db.commit()
    if archive_path:
        archive_path.unlink(missing_ok=True)
    return True


_MODEL_BUILD_RECIPE = "mlflow-v1"


class ModelBuildOwner(Protocol):
    """What ensure_model_build needs from the job that asked for a build.

    AI Factory deployments and Kubernetes image releases both own builds; each
    keeps its own lease and progress log.
    """

    @property
    def user_id(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def source_uri(self) -> str: ...

    @property
    def build_id(self) -> str | None: ...

    def set_build(self, db, build_id: str) -> None: ...

    async def append_event(self, db, message: str, level: str = "info") -> None: ...

    async def assert_lease(self, db) -> None: ...

    def maintain_lease(self, build_id: str | None): ...

    def cleanup_exclusion(self) -> dict[str, str]: ...


class _DeploymentBuildOwner:
    def __init__(self, deployment: MlflowDeployment):
        self.row = deployment

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
        await append_deployment_event(db, self.row, message, level)

    async def assert_lease(self, db) -> None:
        await _assert_deployment_lease(db, self.row)

    def maintain_lease(self, build_id: str | None):
        return _maintain_deployment_lease(self.row.id, build_id)

    def cleanup_exclusion(self) -> dict[str, str]:
        return {"excluding_deployment_id": self.row.id}


async def _ready_build(
    db,
    *,
    user_id: int,
    model_name: str,
    model_version: str,
    require_image: bool,
) -> MlflowModelBuild | None:
    query = select(MlflowModelBuild).where(
        MlflowModelBuild.user_id == user_id,
        MlflowModelBuild.model_name == model_name,
        MlflowModelBuild.model_version == model_version,
        MlflowModelBuild.recipe_version == _MODEL_BUILD_RECIPE,
        MlflowModelBuild.status == MlflowModelBuildStatus.READY,
    )
    if require_image:
        query = query.where(MlflowModelBuild.workspace_image_id.is_not(None))
    return (
        await db.execute(
            query.order_by(MlflowModelBuild.completed_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


def model_build_source_ref(
    build: MlflowModelBuild,
    image: WorkspaceImage | None,
    registry_url: str,
) -> str:
    """The build's tag in the Model Container Registry (the intermediate registry)."""
    return image.source_ref if image else _image_tag(build, registry_url)


async def ensure_model_build(
    db,
    owner: ModelBuildOwner,
    client: MlflowClient,
    *,
    import_image: bool = True,
) -> MlflowModelBuild:
    """Reuse a ready build of the model version or build it on a worker.

    ``import_image`` also copies the result into the controller image catalog
    so workers can run it (AI Factory). Kubernetes releases only need the
    registry tag; skipping the catalog keeps every worker from syncing a
    multi-GB image that nothing will run on them.
    """
    registry: ModelContainerRegistryConfig | None = None
    cached_build = None
    async with admission_transaction(db):
        registry = await effective_model_container_registry_config(db)
        validate_model_container_registry_config(registry, require_enabled=True)
        await owner.assert_lease(db)
        build = await _ready_build(
            db,
            user_id=owner.user_id,
            model_name=owner.model_name,
            model_version=owner.model_version,
            require_image=import_image,
        )
        if build is not None:
            usable = True
            if import_image:
                image = await db.get(WorkspaceImage, build.workspace_image_id)
                usable = bool(image and image.enabled)
            if usable:
                owner.set_build(db, build.id)
                await owner.append_event(
                    db,
                    "Hazır model image önbellekten kullanılıyor.",
                    "success",
                )
                cached_build = build
        if cached_build is None:
            if owner.build_id:
                await enroll_model_build_cleanup(
                    db,
                    owner.build_id,
                    **owner.cleanup_exclusion(),
                )
            build = MlflowModelBuild(
                user_id=owner.user_id,
                model_name=owner.model_name,
                model_version=owner.model_version,
                run_id=owner.run_id,
                source_uri=owner.source_uri,
                model_uri=f"models:/{owner.model_name}/{owner.model_version}",
                recipe_version=_MODEL_BUILD_RECIPE,
                status=MlflowModelBuildStatus.BUILDING,
                status_message="Model serving image oluşturuluyor.",
                lease_owner=_LEASE_OWNER,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(
                    seconds=_LEASE_SECONDS
                ),
            )
            db.add(build)
            await db.flush()
            owner.set_build(db, build.id)
            await owner.append_event(
                db,
                "Model artifact'ları image build worker'ında hazırlanıyor.",
            )
    if cached_build is not None:
        return cached_build
    if registry is None:
        raise RuntimeError("Model Container Registry çözümlenemedi.")

    build_flavor = get_flavor("t1.nano")
    if build_flavor is None:
        raise RuntimeError("Image build kaynak profili bulunamadı.")
    node = await select_worker_node(
        db, build_flavor, minimum_agent_version="3.7.2"
    )
    await db.commit()
    image_tag = _image_tag(build, registry.registry_url)
    connection = agent_manager.get(node.id)
    if registry.password and not connection.confidential_for_secrets:
        raise RuntimeError(
            "Registry parolası build worker'a yalnızca WSS veya loopback tunnel "
            "üzerinden gönderilebilir."
        )
    # Name the worker and the target tag before the long-running RPC so a build
    # that dies inside the worker can be traced to a specific host from the
    # owner's own progress log.
    await owner.append_event(
        db,
        f"Image build worker'ı seçildi: {node.name}; hedef image {image_tag}.",
    )
    await db.commit()
    async with owner.maintain_lease(build.id):
        result = await connection.request(
            "image.mlflow.build",
            {
                "model_name": build.model_name,
                "model_version": build.model_version,
                "image_tag": image_tag,
                "registry_url": registry.registry_url,
                "registry_username": registry.username,
                "registry_password": registry.password,
                "mlflow_environment": environment_from_config(client.config),
                "mlflow_ca_certificate": _mlflow_ca_certificate(client),
            },
            timeout=float(settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS) + 30,
        )
        if not result.get("success"):
            detail = str(result.get("logs") or "Image build başarısız.")[-4000:]
            build.status = MlflowModelBuildStatus.FAILED
            build.status_message = "Model serving image oluşturulamadı."
            build.error_message = detail
            build.lease_owner = ""
            build.lease_expires_at = None
            build.completed_at = datetime.now(timezone.utc)
            db.add(build)
            await db.commit()
            raise RuntimeError(detail)

        await owner.assert_lease(db)
        if not import_image:
            build.status = MlflowModelBuildStatus.READY
            build.status_message = "Model serving image ara registry'de hazır."
            build.error_message = None
            build.lease_owner = ""
            build.lease_expires_at = None
            build.completed_at = datetime.now(timezone.utc)
            db.add(build)
            await owner.append_event(
                db,
                f"Model image oluşturuldu ve ara registry'ye yüklendi: {image_tag}.",
                "success",
            )
            await db.commit()
            return build
        build.status = MlflowModelBuildStatus.IMPORTING
        build.status_message = "Model image doğrulanıp controller kataloğuna alınıyor."
        db.add(build)
        await owner.append_event(db, "Model image oluşturuldu; checksum kataloğu hazırlanıyor.", "success")
        await db.commit()

        try:
            metadata = await asyncio.to_thread(
                import_registry_image,
                image_ref=image_tag,
                source_ref=image_tag,
                username=registry.username,
                password=registry.password,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Worker image'ı {image_tag} olarak push etti ancak controller "
                f"registry'den çekemedi: {exc}"
            ) from exc
    await owner.assert_lease(db)
    image = WorkspaceImage(
        id=str(metadata["id"]),
        template_id="mlflow-serving",
        purpose="mlflow_model",
        owner_user_id=owner.user_id,
        display_name=f"{owner.model_name} v{owner.model_version}",
        image_ref=str(metadata["image_ref"]),
        source_type="mlflow_model",
        source_ref=image_tag,
        digest=str(metadata["digest"]),
        sha256=str(metadata["sha256"]),
        filename=str(metadata["filename"]),
        size=int(metadata["size"]),
        architecture=str(metadata["architecture"]),
        enabled=True,
    )
    db.add(image)
    await db.flush()
    build.workspace_image_id = image.id
    build.status = MlflowModelBuildStatus.READY
    build.status_message = "Model serving image hazır."
    build.error_message = None
    build.lease_owner = ""
    build.lease_expires_at = None
    build.completed_at = datetime.now(timezone.utc)
    db.add(build)
    await db.commit()
    return build


async def _build_image(
    db,
    deployment: MlflowDeployment,
    client: MlflowClient,
) -> MlflowModelBuild:
    return await ensure_model_build(
        db, _DeploymentBuildOwner(deployment), client, import_image=True
    )


# A fresh model image is a multi-GB archive that every worker pulls from the
# controller on its own 30 s poll, verifies and `podman load`s before it can be
# scheduled there, so a deployment must wait on real transfer progress rather
# than a fixed short window.
_IMAGE_SYNC_TIMEOUT_SECONDS = 1800  # matches the worker's podman load timeout
_IMAGE_SYNC_STALL_SECONDS = 300  # no worker moved the transfer forward
_IMAGE_READY_CAPACITY_SECONDS = 120  # image is on a worker; only capacity is missing
_IMAGE_SYNC_POLL_SECONDS = 5
_IMAGE_SYNC_STATE_RANK = {"queued": 0, "downloading": 1, "verifying": 2, "loading": 3}
_IMAGE_SYNC_STATE_LABELS = {
    "queued": "image sırada",
    "downloading": "image indiriliyor",
    "verifying": "image doğrulanıyor",
    "loading": "image Podman'a yükleniyor",
}


def _node_capabilities(node: Node) -> dict:
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except ValueError:
        return {}
    return capabilities if isinstance(capabilities, dict) else {}


def _image_sync_snapshot(
    nodes: list[Node],
    image_id: str,
    image_ref: str,
    image_sha256: str,
) -> tuple[bool, list[tuple[str, dict]]]:
    """Return whether any worker holds the image, plus per-worker sync progress."""
    ready = False
    progress: list[tuple[str, dict]] = []
    for node in nodes:
        capabilities = _node_capabilities(node)
        images = capabilities.get("workspace_images")
        if isinstance(images, list) and any(
            isinstance(item, dict)
            and item.get("image_ref") == image_ref
            and item.get("sha256") == image_sha256
            for item in images
        ):
            ready = True
            continue
        sync = capabilities.get("workspace_image_sync")
        if not isinstance(sync, list):
            continue
        entry = next(
            (
                item
                for item in sync
                if isinstance(item, dict) and item.get("id") == image_id
            ),
            None,
        )
        if entry is not None:
            progress.append((node.name, entry))
    return ready, progress


async def _reserve_when_image_synced(
    db,
    deployment: MlflowDeployment,
    build: MlflowModelBuild,
):
    image = await db.get(WorkspaceImage, build.workspace_image_id)
    user = await db.get(User, deployment.user_id)
    template = get_template("mlflow-serving")
    flavor = await resolve_flavor(db, deployment.flavor_id)
    if image is None or user is None or template is None or flavor is None:
        raise RuntimeError("Deployment çalışma alanı girdileri artık bulunamıyor.")
    # A failed attempt rolls the session back, which expires every loaded row;
    # touching an expired row on AsyncSession raises instead of reloading. Keep
    # plain keys and reload the rows at the start of each attempt.
    image_id = image.id
    image_ref = image.image_ref
    image_sha256 = image.sha256
    image_size = int(image.size or 0)
    user_id = user.id
    # Deployment names are unique among a user's deployments but may still
    # collide with a workspace they created by hand, so take the next variant
    # rather than failing a deployment that is otherwise ready to serve.
    request = WorkspaceCreate(
        name=await available_workspace_name(
            db, user_id=deployment.user_id, name=deployment.name
        ),
        description=f"MLflow {deployment.model_name} v{deployment.model_version}",
        template_id=template.id,
        flavor_id=flavor.id,
        auto_stop_minutes=deployment.auto_stop_minutes,
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    last_progress_at = started
    ready_since: float | None = None
    best: dict[str, tuple[int, int]] = {}
    reported: dict[str, str] = {}
    worker_error = ""
    last_error: Exception | None = None
    while True:
        try:
            await _assert_deployment_lease(db, deployment)
            image = await db.get(WorkspaceImage, image_id, populate_existing=True)
            user = await db.get(User, user_id, populate_existing=True)
            if image is None or not image.enabled or user is None:
                raise RuntimeError(
                    "Deployment çalışma alanı girdileri artık bulunamıyor."
                )
            workspace, placement = await schedule_and_reserve_workspace(
                db,
                data=request,
                current_user=user,
                template=template,
                flavor=flavor,
                workspace_image=image,
                linked_deployment=deployment,
            )
            return workspace, placement, template, flavor
        except NoSchedulableNode as exc:
            last_error = exc
            await db.rollback()
        # Reloads the rolled-back deployment row and stops if another
        # controller process took the job over meanwhile.
        await _assert_deployment_lease(db, deployment)

        now = loop.time()
        nodes = [
            node
            for node in (
                await db.execute(
                    select(Node)
                    .where(Node.enabled.is_(True), Node.status == NodeStatus.ONLINE)
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            if agent_manager.is_connected(node.id)
        ]
        ready, progress = _image_sync_snapshot(
            nodes, image_id, image_ref, image_sha256
        )
        if ready:
            # The image reached a worker, so what is missing now is CPU/RAM/GPU
            # capacity; allow a short window for it to free up.
            ready_since = ready_since if ready_since is not None else now
            if now - ready_since >= _IMAGE_READY_CAPACITY_SECONDS:
                raise RuntimeError(
                    "Model image worker'da hazır ancak kaynak ayrılamadı: "
                    f"{last_error or 'uygun worker yok'}"
                )
        else:
            ready_since = None
            in_final_steps = False
            for node_name, entry in progress:
                state = str(entry.get("state") or "")
                downloaded = int(entry.get("downloaded_bytes") or 0)
                total = int(entry.get("total_bytes") or image_size or 0)
                if state == "failed":
                    worker_error = (
                        f"{node_name}: {entry.get('error') or 'bilinmeyen hata'}"
                    )
                    label = f"failed:{worker_error}"
                    if reported.get(node_name) != label:
                        reported[node_name] = label
                        await append_deployment_event(
                            db,
                            deployment,
                            f"{node_name}: image senkronizasyonu başarısız, "
                            f"worker yeniden deneyecek: {worker_error}",
                            "warning",
                        )
                        await db.commit()
                    continue
                rank = _IMAGE_SYNC_STATE_RANK.get(state)
                if rank is None:
                    continue
                in_final_steps = in_final_steps or state in {"verifying", "loading"}
                # A retry after a failure restarts at zero bytes; only count
                # getting further than this worker has ever got as progress.
                marker = (rank, downloaded)
                if marker > best.get(node_name, (-1, -1)):
                    best[node_name] = marker
                    last_progress_at = now
                percent = (
                    min(100, downloaded * 100 // total)
                    if state == "downloading" and total
                    else None
                )
                bucket = (
                    f"{state}:{percent // 25}" if percent is not None else state
                )
                if reported.get(node_name) != bucket:
                    reported[node_name] = bucket
                    message = f"{node_name}: {_IMAGE_SYNC_STATE_LABELS[state]}"
                    if percent is not None:
                        message += f" (%{percent})"
                    await append_deployment_event(db, deployment, message + ".")
                    await db.commit()
            # Verification and `podman load` report no byte progress, and the
            # worker bounds them itself, so only the overall timeout applies.
            if (
                not in_final_steps
                and now - last_progress_at >= _IMAGE_SYNC_STALL_SECONDS
            ):
                raise RuntimeError(
                    "Model image hiçbir worker'a senkronize edilemedi; "
                    f"{_IMAGE_SYNC_STALL_SECONDS // 60} dakikadır ilerleme yok. "
                    + (
                        f"Son worker hatası: {worker_error}"
                        if worker_error
                        else f"Zamanlayıcı: {last_error or 'uygun worker yok'}"
                    )
                )
        if now - started >= _IMAGE_SYNC_TIMEOUT_SECONDS:
            raise RuntimeError(
                "Model image worker'lara zamanında senkronize edilemedi: "
                f"{worker_error or last_error or 'uygun worker yok'}"
            )
        await asyncio.sleep(_IMAGE_SYNC_POLL_SECONDS)


async def process_mlflow_deployment(deployment_id: str) -> None:
    async with AsyncSessionLocal() as db:
        claimed = await db.execute(
            update(MlflowDeployment)
            .where(
                MlflowDeployment.id == deployment_id,
                MlflowDeployment.status == MlflowDeploymentStatus.QUEUED,
            )
            .values(
                status=MlflowDeploymentStatus.WAITING_FOR_IMAGE,
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
        deployment = await db.get(MlflowDeployment, deployment_id)
        if deployment is None:
            return
        await append_deployment_event(db, deployment, "Deployment kuyruğundan alındı.")
        await db.commit()

        try:
            client = await _mlflow_client_for_user(db, deployment.user_id)
            payload = await client.get_model_version(
                deployment.model_name, deployment.model_version
            )
            version = payload.get("model_version") or {}
            if (
                str(version.get("name") or "") != deployment.model_name
                or str(version.get("version") or "") != deployment.model_version
            ):
                raise RuntimeError("MLflow beklenen model versiyonunu döndürmedi.")
            deployment.run_id = str(version.get("run_id") or deployment.run_id)
            deployment.source_uri = str(version.get("source") or deployment.source_uri)
            await _reserve_deployment_quota(db, deployment)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.WAITING_FOR_IMAGE,
                f"{deployment.model_name} v{deployment.model_version} doğrulandı.",
                level="success",
            )

            build = await _build_image(db, deployment, client)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.SCHEDULING,
                "Model image worker'lara senkronize ediliyor ve kaynak ayrılıyor.",
            )
            # The wait can outlast the 5-minute lease, so keep renewing it.
            async with _maintain_deployment_lease(deployment.id):
                workspace, placement, template, flavor = (
                    await _reserve_when_image_synced(db, deployment, build)
                )
            deployment.workspace_id = workspace.id
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.STARTING,
                f"Worker seçildi: {placement.node.name}; model servisi başlatılıyor.",
            )

            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            runtime = runtime_for_node(workspace.node_id)
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=deployment.user_id,
                container_name=workspace.container_name,
                template_id=template.id,
                flavor_id=flavor.id,
                template_definition=template_definition(template),
                flavor_definition=flavor_definition(flavor),
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
                accelerator_cdi_name=workspace.accelerator_cdi_name or "",
                image_ref=image_ref,
                image_sha256=image_sha256,
                mlflow_environment={},
                service_environment={
                    "DISABLE_NGINX": "false",
                    "GUNICORN_CMD_ARGS": f"--workers={deployment.gunicorn_workers}",
                },
            )
            workspace.container_id = container_id
            workspace.storage_path = storage_path
            workspace.status = WorkspaceStatus.RUNNING
            workspace.last_started_at = datetime.now(timezone.utc)
            workspace.error_message = None
            db.add(workspace)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.RUNNING,
                "Model servisi hazır; /invocations endpoint'i istek kabul ediyor.",
                level="success",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("MLflow deployment %s failed", deployment_id)
            await db.rollback()
            deployment = await db.get(MlflowDeployment, deployment_id)
            if deployment is None or deployment.lease_owner != _LEASE_OWNER:
                return
            if deployment.build_id:
                build = await db.get(MlflowModelBuild, deployment.build_id)
                if build and build.status in {
                    MlflowModelBuildStatus.VALIDATING,
                    MlflowModelBuildStatus.BUILDING,
                    MlflowModelBuildStatus.IMPORTING,
                }:
                    build.status = MlflowModelBuildStatus.FAILED
                    build.status_message = "Model image build başarısız oldu."
                    build.error_message = str(exc)[:4000]
                    build.lease_owner = ""
                    build.lease_expires_at = None
                    build.completed_at = datetime.now(timezone.utc)
                    db.add(build)
            if deployment.workspace_id:
                workspace = await db.get(Workspace, deployment.workspace_id)
                if workspace and workspace.status != WorkspaceStatus.RUNNING:
                    workspace.status = WorkspaceStatus.ERROR
                    workspace.error_message = str(exc)[:4000]
                    db.add(workspace)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.FAILED,
                "Model deployment başarısız oldu.",
                level="error",
                error=str(exc)[:4000],
            )
            # The generic stage message alone leaves the progress log
            # undiagnosable, so replay the underlying reason (for image builds
            # this is the tail of the worker's build output) as its own event.
            await _append_failure_detail(db, deployment, exc)


_FAILURE_DETAIL_LIMIT = 900


async def _append_failure_detail(
    db,
    deployment: MlflowDeployment,
    exc: BaseException,
) -> None:
    detail = str(exc).strip()
    if not detail:
        detail = exc.__class__.__name__
    label = "Hata ayrıntısı"
    if len(detail) > _FAILURE_DETAIL_LIMIT:
        detail = detail[-_FAILURE_DETAIL_LIMIT:]
        label = f"Hata ayrıntısı (son {_FAILURE_DETAIL_LIMIT} karakter)"
    try:
        await append_deployment_event(db, deployment, f"{label}: {detail}", "error")
        await db.commit()
    except Exception:  # pragma: no cover - diagnostics must never mask a failure
        logger.exception(
            "Could not record failure detail for MLflow deployment %s",
            deployment.id,
        )
        await db.rollback()


async def _recover_stale_deployment(db, deployment: MlflowDeployment) -> None:
    claim = await db.execute(
        update(MlflowDeployment)
        .where(
            MlflowDeployment.id == deployment.id,
            or_(
                MlflowDeployment.lease_expires_at.is_(None),
                MlflowDeployment.lease_expires_at < datetime.now(timezone.utc),
            ),
        )
        .values(
            lease_owner=_LEASE_OWNER,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
    )
    await db.commit()
    if claim.rowcount != 1:
        return
    await db.refresh(deployment)
    if deployment.status == MlflowDeploymentStatus.DELETING:
        deployment.status = MlflowDeploymentStatus.FAILED
        deployment.status_message = "Yarım kalan silme işlemi yeniden denenebilir."
        deployment.error_message = "Controller silme işlemi sırasında yeniden başladı."
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    if not deployment.workspace_id:
        deployment.status = MlflowDeploymentStatus.QUEUED
        deployment.status_message = "Yarım kalan deployment devam ettirilecek."
        deployment.error_message = None
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    workspace = await db.get(Workspace, deployment.workspace_id)
    if workspace is None:
        deployment.workspace_id = None
        deployment.quota_reserved = False
        deployment.status = MlflowDeploymentStatus.QUEUED
        deployment.status_message = "Eksik workspace kaydı yeniden oluşturulacak."
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    await db.commit()
    runtime = runtime_for_node(workspace.node_id)
    try:
        exists = await runtime.container_exists(workspace.container_name)
        running = exists and await runtime.get_container_status(
            workspace.container_name
        ) == "running"
        healthy = running and await runtime.health_ready(
            workspace.container_name, workspace.host_port, "/ping"
        )
    except AgentUnavailable:
        deployment.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=30
        )
        db.add(deployment)
        await db.commit()
        return
    if healthy:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.error_message = None
        workspace.last_started_at = workspace.last_started_at or datetime.now(timezone.utc)
        deployment.status = MlflowDeploymentStatus.RUNNING
        deployment.status_message = "Çalışan model servisi controller restart sonrası benimsendi."
        deployment.error_message = None
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(workspace)
        db.add(deployment)
        await db.commit()
        return
    await delete_workspace_resources(db, workspace, allow_transient=True)
    deployment.workspace_id = None
    deployment.quota_reserved = False
    deployment.status = MlflowDeploymentStatus.QUEUED
    deployment.status_message = "Yarım kalan servis temizlendi; deployment yeniden kuyruğa alındı."
    deployment.error_message = None
    deployment.lease_owner = ""
    deployment.lease_expires_at = None
    db.add(deployment)
    await db.commit()


async def recover_interrupted_mlflow_deployments() -> None:
    async with AsyncSessionLocal() as db:
        deployments = (
            await db.execute(
                select(MlflowDeployment).where(
                    MlflowDeployment.status.in_(_ACTIVE_DEPLOYMENT_STATUSES),
                    or_(
                        MlflowDeployment.lease_expires_at.is_(None),
                        MlflowDeployment.lease_expires_at
                        < datetime.now(timezone.utc),
                    ),
                )
            )
        ).scalars().all()
        for deployment in deployments:
            await _recover_stale_deployment(db, deployment)
        await db.execute(
            update(MlflowModelBuild)
            .where(
                MlflowModelBuild.status.in_(
                    {
                        MlflowModelBuildStatus.VALIDATING,
                        MlflowModelBuildStatus.BUILDING,
                        MlflowModelBuildStatus.IMPORTING,
                    }
                ),
                or_(
                    MlflowModelBuild.lease_expires_at.is_(None),
                    MlflowModelBuild.lease_expires_at
                    < datetime.now(timezone.utc),
                ),
            )
            .values(
                status=MlflowModelBuildStatus.FAILED,
                status_message="Controller image build sırasında yeniden başladı.",
                error_message="Build yeniden denenmelidir.",
                lease_owner="",
                lease_expires_at=None,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


async def mlflow_deployment_background_worker(check_interval_seconds: int = 2) -> None:
    """Claim queued deployments durably; one claim can survive request disconnects."""
    while True:
        try:
            await recover_interrupted_mlflow_deployments()
            async with AsyncSessionLocal() as cleanup_db:
                await enroll_orphaned_model_builds(cleanup_db)
                cleanup_ids = list(
                    (
                        await cleanup_db.execute(
                            select(MlflowModelBuild.id)
                            .where(
                                MlflowModelBuild.status
                                == MlflowModelBuildStatus.DELETING
                            )
                            .limit(10)
                        )
                    ).scalars()
                )
                await cleanup_db.commit()
                for build_id in cleanup_ids:
                    await garbage_collect_model_build(cleanup_db, build_id)
            async with AsyncSessionLocal() as db:
                deployment_ids = list(
                    (
                        await db.execute(
                            select(MlflowDeployment.id)
                            .where(MlflowDeployment.status == MlflowDeploymentStatus.QUEUED)
                            .order_by(MlflowDeployment.created_at)
                            .limit(5)
                        )
                    ).scalars()
                )
                await db.commit()
            for deployment_id in deployment_ids:
                await process_mlflow_deployment(deployment_id)
            task_health.success("mlflow_deployments")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task_health.failure("mlflow_deployments", exc)
            logger.exception("MLflow deployment worker iteration failed")
        await asyncio.sleep(check_interval_seconds)
