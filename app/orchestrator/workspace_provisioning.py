"""Workspace admission, reservation and runtime-definition logic.

Shared by the workspace HTTP routes and the MLflow deployment orchestrator, so
it must not depend on either. Errors are domain exceptions; HTTP mapping is the
caller's job.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import logging
from types import SimpleNamespace
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.mlflow import (
    MlflowConfigurationError,
    config_from_record as mlflow_config_from_record,
    validate_config as validate_mlflow_config,
)
from app.jupyter_ai import CA_BUNDLE_MIN_AGENT_VERSION, parse_model_catalog
from app.mlflow_workspace import environment_from_config
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.models.mlflow_deployment import MlflowDeployment
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_settings import MlflowSettings
from app.models.node import Node
from app.models.kilo_code_settings import KiloCodeSettings
from app.models.skill import Skill
from app.models.user import User
from app.models.workspace import (
    Workspace,
    WorkspaceStatus,
    consumes_compute,
    normalize_workspace_name,
    workspace_name_slug,
)
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.admission import admission_transaction
from app.orchestrator.flavors import Flavor, get_flavor
from app.orchestrator.metrics_service import get_workspace_disk_usage_by_user
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.scheduler import (
    WorkspacePlacement,
    _capabilities as node_capabilities,
    _has_workspace_image as has_workspace_image,
    select_workspace_placement,
)
from app.placement import (
    PlacementConstraints,
    encode_tags,
    resolve_workspace_constraints,
)
from app.quotas import effective_quota
from app.release_catalog import semantic_version
from app.resource_usage import quota_violations
from app.schemas.workspace import WorkspaceCreate
from app.security.secrets import SecretDecryptionError, decrypt_secret
from app.kilo_code import (
    KILO_CODE_ASSISTANT,
    MIN_AGENT_VERSION as KILO_CODE_MIN_AGENT_VERSION,
    config_digest,
    managed_vscode_settings as kilo_code_vscode_settings,
    parse_model_ids,
    parse_skills_urls,
    render_config,
    select_models,
)
from app.skills import (
    MIN_AGENT_VERSION as SKILLS_MIN_AGENT_VERSION,
    SKILLS_DIR as KILO_CODE_SKILLS_DIR,
    build_bundle as build_skills_bundle,
)

logger = logging.getLogger("devcloud.workspaces")


class WorkspaceOperationError(Exception):
    """A lifecycle operation was refused or failed; carries its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def template_definition(template) -> dict:
    return {
        "template_id": template.id,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "image_tag": template.image_tag,
        "default_port": template.default_port,
        "ide_type": template.ide_type,
        "icon": template.icon,
        "mount_workspace": template.mount_workspace,
        "health_path": template.health_path,
        "require_ready": template.require_ready,
    }


def flavor_definition(flavor: Flavor) -> dict:
    return {
        "id": flavor.id,
        "name": flavor.name,
        "display_name": flavor.display_name,
        "description": flavor.description,
        "cpus": flavor.cpus,
        "memory_mb": flavor.memory_mb,
        "memory_display": flavor.memory_display,
        "accelerator_count": flavor.accelerator_count,
        "accelerator_vendor": flavor.accelerator_vendor,
        "accelerator_memory_mb": flavor.accelerator_memory_mb,
        "accelerator_display": flavor.accelerator_display,
        "selectable": flavor.selectable,
    }


async def workspace_mlflow_environment(
    db: AsyncSession,
    user_id: int,
) -> dict[str, str]:
    server = await db.get(MlflowServerSettings, 1)
    if not server or not server.enabled:
        return {}
    credentials = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not credentials or not credentials.enabled:
        return {}
    try:
        config = mlflow_config_from_record(credentials, server)
        validate_mlflow_config(config, require_enabled=True)
    except (MlflowConfigurationError, SecretDecryptionError) as exc:
        logger.warning("Skipping invalid MLflow workspace settings for user %s: %s", user_id, exc)
        return {}
    return environment_from_config(config)


async def workspace_service_environment(
    db: AsyncSession,
    workspace: Workspace,
) -> dict[str, str]:
    if workspace.template_id != "mlflow-serving":
        return {}
    deployment = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.workspace_id == workspace.id
            )
        )
    ).scalar_one_or_none()
    workers = deployment.gunicorn_workers if deployment else 1
    return {
        "DISABLE_NGINX": "false",
        "GUNICORN_CMD_ARGS": f"--workers={workers}",
    }


# IDE types whose containers receive the Admin gateway CA bundle.
CA_BUNDLE_IDE_TYPES = frozenset({"vscode", "jupyter"})
_CA_DIGEST_SALT = b"devcloud-workspace-ai-ca-v1\x00"
_SKILLS_DIGEST_SALT = b"devcloud-workspace-ai-skills-v1\x00"
# IDE types whose containers receive the Admin skill library (Kilo Code
# VS Code templates are narrowed further by their ai_assistant).
SKILLS_IDE_TYPES = frozenset({"vscode", "jupyter"})


def ai_config_digest(kilo_code_config: str, ca_bundle: str, skills_bundle: str = "") -> str:
    """Fingerprint of the AI settings baked into a container's environment.

    Without a CA bundle this is the Kilo Code digest, and without skills it is
    the previous digest, so neither feature recreates a container until an
    Admin actually saves a bundle or a skill.
    """
    if skills_bundle:
        return hashlib.sha256(
            _SKILLS_DIGEST_SALT
            + ai_config_digest(kilo_code_config, ca_bundle).encode("ascii")
            + b"\x00"
            + skills_bundle.encode("ascii")
        ).hexdigest()
    if kilo_code_config:
        # The VS Code user settings ride in the same container environment,
        # so a change to them recreates Kilo Code containers too.
        kilo_code_config = (
            kilo_code_config + "\x00" + kilo_code_vscode_settings(kilo_code_config)
        )
    if not ca_bundle:
        return config_digest(kilo_code_config)
    return hashlib.sha256(
        _CA_DIGEST_SALT
        + kilo_code_config.encode("utf-8")
        + b"\x00"
        + ca_bundle.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class WorkspaceAiConfig:
    """AI wiring sent to the worker with a container.create request."""

    ai_gateway_token: str = ""
    kilo_code_config: str = ""
    # Kilo Code is configured but the owner has no AI Factory key yet.
    user_key_missing: bool = False
    # Why a Kilo Code workspace gets no kilo.jsonc at all (Admin-side cause).
    kilo_code_problem: str = ""
    # Admin > Workspace AI gateway CA bundle (canonical PEM).
    ca_bundle: str = ""
    # Admin > Skill Kütüphanesi: enabled skills as a base64 gzip tar.
    skills_bundle: str = ""

    @property
    def digest(self) -> str:
        return ai_config_digest(self.kilo_code_config, self.ca_bundle, self.skills_bundle)

    def create_arguments(self) -> dict[str, str]:
        arguments = {"ai_gateway_token": self.ai_gateway_token}
        # Older workers drop unknown keys, so these stay rolling-safe.
        if self.kilo_code_config:
            arguments["kilo_code_config"] = self.kilo_code_config
        if self.ca_bundle:
            arguments["ai_ca_bundle"] = self.ca_bundle
        if self.skills_bundle:
            arguments["ai_skills_bundle"] = self.skills_bundle
        return arguments


KILO_CODE_MISSING_KEY_MESSAGE = (
    "Kilo Code sohbeti için AI Factory anahtarınız yok. GenAI sekmesinden anahtar "
    "talep edin, ardından workspace'i durdurup yeniden başlatın. Kod indeksleme "
    "bu sırada çalışmaya devam eder."
)
KILO_CODE_RESTART_HINT = (
    " Ayar düzeltildikten sonra workspace'i durdurup yeniden başlatın."
)
KILO_CODE_DISABLED_MESSAGE = (
    "Kilo Code yapılandırılmadı: Admin > Entegrasyonlar > Kilo Code kartında "
    "\"Kilo Code yapılandırmasını workspace'lere dağıt\" kapalı."
    + KILO_CODE_RESTART_HINT
)
KILO_CODE_NO_GATEWAY_MESSAGE = (
    "Kilo Code yapılandırılmadı: Admin > Entegrasyonlar > Workspace AI kapalı "
    "veya gateway adresi boş." + KILO_CODE_RESTART_HINT
)
KILO_CODE_NO_MODELS_MESSAGE = (
    "Kilo Code yapılandırılmadı: Workspace AI model kataloğunda Kilo Code'a "
    "sunulacak model yok." + KILO_CODE_RESTART_HINT
)


def kilo_code_worker_outdated_message(node_name: str, agent_version: str) -> str:
    return (
        f"Kilo Code yapılandırılmadı: worker '{node_name}' {agent_version or 'bilinmeyen sürüm'} "
        f"çalıştırıyor; Kilo Code ayarlarını yazmak için en az {KILO_CODE_MIN_AGENT_VERSION} "
        "gerekir. Worker'ı güncelleyip workspace'i durdurup yeniden başlatın."
    )


def _worker_at_least(node, minimum: str) -> bool:
    current = semantic_version(str(getattr(node, "agent_version", "") or ""))
    required = semantic_version(minimum)
    return current is None or required is None or current >= required


def worker_applies_kilo_code(node) -> bool:
    """False only when the worker reports a release older than MIN_AGENT_VERSION.

    Such a worker drops kilo_code_config. An unknown version is given the
    benefit of the doubt, matching the scheduler.
    """
    return _worker_at_least(node, KILO_CODE_MIN_AGENT_VERSION)


def worker_applies_ca_bundle(node) -> bool:
    """False when the worker is too old to install ``ai_ca_bundle``."""
    return _worker_at_least(node, CA_BUNDLE_MIN_AGENT_VERSION)


def worker_applies_skills(node) -> bool:
    """False when the worker is too old to install ``ai_skills_bundle``."""
    return _worker_at_least(node, SKILLS_MIN_AGENT_VERSION)


async def applied_ai_config_digest(
    db: AsyncSession,
    workspace: Workspace,
    ai_config: WorkspaceAiConfig,
) -> str:
    """Digest of the config the worker really writes into the container.

    An outdated worker drops ``kilo_code_config``, ``ai_ca_bundle`` or
    ``ai_skills_bundle``;
    recording the sent digest would make the next start believe the container
    is current and never recreate it after the worker is upgraded. Comparing
    a stored digest with this value (not ``ai_config.digest``) also keeps an
    outdated worker from recreating the container on every start.
    """
    if (
        not ai_config.kilo_code_config
        and not ai_config.ca_bundle
        and not ai_config.skills_bundle
    ):
        return ""
    node = await db.get(Node, workspace.node_id) if workspace.node_id else None
    if node is None:
        return ""
    return ai_config_digest(
        ai_config.kilo_code_config if worker_applies_kilo_code(node) else "",
        ai_config.ca_bundle if worker_applies_ca_bundle(node) else "",
        ai_config.skills_bundle if worker_applies_skills(node) else "",
    )


async def workspace_ca_bundle(db: AsyncSession, template) -> str:
    """The Admin gateway CA bundle for this template's containers, else ''."""
    if template is None or template.ide_type not in CA_BUNDLE_IDE_TYPES:
        return ""
    record = await db.get(JupyterAiSettings, 1)
    return (record.ca_bundle_pem or "") if record is not None else ""


async def kilo_code_worker_problem(db: AsyncSession, workspace: Workspace) -> str:
    """Explain an outdated worker for a Kilo Code workspace, else ''."""
    node = await db.get(Node, workspace.node_id) if workspace.node_id else None
    if node is None or worker_applies_kilo_code(node):
        return ""
    return kilo_code_worker_outdated_message(node.name, node.agent_version or "")


async def pin_active_workspace_image(
    db: AsyncSession,
    workspace: Workspace,
    template,
) -> None:
    """Pin an unpinned workspace to its template's enabled image before a recreate.

    Migration 33 unpins the TCMB Code era workspaces. ``vscode-cuda`` keeps its
    tag, so the old TCMB Code image and the new Kilo Code image share it; only
    the pinned checksum tells them apart.
    """
    if workspace.image_id:
        return
    image = await active_workspace_image(
        db, template_id=workspace.template_id, image_ref=template.image_tag
    )
    if image is not None:
        workspace.image_id = image.id


async def runtime_image_missing_problem(
    db: AsyncSession,
    workspace: Workspace,
    template,
) -> str:
    """Explain why the image a recreated container would use is not on its worker.

    Checked before a Kilo Code container is removed or recreated, so a
    workspace keeps its working container until the managed image has
    synchronized (for example right after the TCMB Code to Kilo Code upgrade).
    Call ``pin_active_workspace_image`` first: an image without a checksum
    cannot be told apart from an older one under the same tag.
    """
    node = await db.get(Node, workspace.node_id) if workspace.node_id else None
    if node is None:
        return ""
    image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
    if image_sha256:
        if has_workspace_image(node, image_ref, image_sha256):
            return ""
    elif "workspace_images" not in node_capabilities(node):
        # A worker that reports no image inventory cannot be checked.
        return ""
    return (
        f"{image_ref} image'ı worker '{node.name}' üzerinde henüz yok. "
        "Admin > Workspace Image'ları üzerinden image'ı içe aktarın; senkronize "
        "olduktan sonra workspace'i durdurup yeniden başlatın."
    )


async def workspace_ai_config(
    db: AsyncSession,
    template,
    user_id: int,
) -> WorkspaceAiConfig:
    """Resolve the owner's AI wiring for a new or recreated container."""
    skills_bundle = await workspace_skills_bundle(db, template)
    config = await _workspace_ai_wiring(db, template, user_id, bool(skills_bundle))
    if getattr(template, "ai_assistant", "") == KILO_CODE_ASSISTANT and not config.kilo_code_config:
        # Kilo Code finds the skills through kilo.jsonc; without one they are unused.
        skills_bundle = ""
    ca_bundle = await workspace_ca_bundle(db, template)
    return replace(config, ca_bundle=ca_bundle, skills_bundle=skills_bundle)


async def workspace_skills_bundle(db: AsyncSession, template) -> str:
    """The enabled Admin skills for a Kilo Code or Jupyter template, else ''."""
    if template is None or template.ide_type not in SKILLS_IDE_TYPES:
        return ""
    if (
        template.ide_type == "vscode"
        and getattr(template, "ai_assistant", "") != KILO_CODE_ASSISTANT
    ):
        return ""
    skills = (
        await db.execute(
            select(Skill.name, Skill.content).where(Skill.enabled.is_(True))
        )
    ).all()
    return build_skills_bundle([(name, content) for name, content in skills])


async def _workspace_ai_wiring(
    db: AsyncSession,
    template,
    user_id: int,
    with_skills: bool = False,
) -> WorkspaceAiConfig:
    from app.genai import workspace_gateway_token

    if template is None or template.ide_type == "service":
        return WorkspaceAiConfig()
    if getattr(template, "ai_assistant", "") != KILO_CODE_ASSISTANT:
        # Jupyter and the retired Cline images keep the shared-key fallback.
        return WorkspaceAiConfig(
            ai_gateway_token=await workspace_gateway_token(db, user_id)
        )
    record = await db.get(KiloCodeSettings, 1)
    workspace_ai = await db.get(JupyterAiSettings, 1)
    if record is None or not record.enabled:
        return WorkspaceAiConfig(kilo_code_problem=KILO_CODE_DISABLED_MESSAGE)
    if workspace_ai is None or not workspace_ai.enabled or not workspace_ai.gateway_url:
        return WorkspaceAiConfig(kilo_code_problem=KILO_CODE_NO_GATEWAY_MESSAGE)
    catalog = parse_model_catalog(workspace_ai.model_catalog_json, workspace_ai.model_id)
    models, default_model = select_models(
        catalog, parse_model_ids(record.model_ids_json), workspace_ai.model_id
    )
    if not models:
        return WorkspaceAiConfig(kilo_code_problem=KILO_CODE_NO_MODELS_MESSAGE)
    indexing_key = ""
    if record.indexing_enabled and record.encrypted_indexing_key:
        try:
            indexing_key = decrypt_secret(record.encrypted_indexing_key)
        except SecretDecryptionError:
            logger.warning("Kilo Code indexing key cannot be decrypted; indexing is off.")
    catalog_token = ""
    if record.encrypted_catalog_token:
        try:
            catalog_token = decrypt_secret(record.encrypted_catalog_token)
        except SecretDecryptionError:
            logger.warning(
                "Kilo Code catalog token cannot be decrypted; the catalog is called without it."
            )
    # Only the owner's own key: Kilo Code never falls back to the shared key.
    user_key = await workspace_gateway_token(db, user_id)
    config = render_config(
        gateway_url=workspace_ai.gateway_url,
        models=models,
        default_model=default_model,
        user_key=user_key,
        catalog_url=record.catalog_url,
        catalog_token=catalog_token,
        skills_urls=parse_skills_urls(record.skills_urls_json),
        skills_paths=[KILO_CODE_SKILLS_DIR] if with_skills else None,
        indexing_enabled=record.indexing_enabled,
        indexing_model=record.indexing_model,
        indexing_dimension=record.indexing_dimension,
        indexing_key=indexing_key,
    )
    return WorkspaceAiConfig(
        kilo_code_config=config,
        user_key_missing=bool(config) and not user_key,
        kilo_code_problem="" if config else KILO_CODE_NO_GATEWAY_MESSAGE,
    )


async def sync_mlflow_deployment_status(
    db: AsyncSession,
    workspace: Workspace,
    status,
    message: str,
    error: str | None = None,
) -> None:
    if workspace.template_id != "mlflow-serving":
        return
    deployment = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.workspace_id == workspace.id
            )
        )
    ).scalar_one_or_none()
    if deployment:
        deployment.status = status
        deployment.status_message = message
        deployment.error_message = error
        db.add(deployment)


class QuotaExceeded(RuntimeError):
    pass


class WorkspaceNameConflict(RuntimeError):
    """The owner already has a workspace with the requested name."""


WORKSPACE_NAME_CONFLICT_DETAIL = "Bu adla bir çalışma alanınız zaten var."


def _is_workspace_name_conflict(exc: IntegrityError) -> bool:
    """Tell the per-owner name constraint apart from port/GPU slot races.

    PostgreSQL names the violated index, SQLite names the columns, so both
    spellings are matched.
    """
    message = str(getattr(exc, "orig", exc)).lower()
    return "uq_workspaces_user_name" in message or "name_key" in message


async def available_workspace_name(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    limit: int = 60,
) -> str:
    """Return the requested name, or its first free numeric variant.

    Used where the name is derived rather than typed by the user, so a
    collision should not fail the whole operation.
    """
    base = name.strip()
    candidate = base
    suffix = 2
    while await workspace_name_taken(db, user_id=user_id, name=candidate):
        marker = f"-{suffix}"
        candidate = f"{base[: max(1, limit - len(marker))]}{marker}"
        suffix += 1
    return candidate


async def workspace_name_taken(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
) -> bool:
    """Report whether the owner already uses a workspace name."""
    name_key = normalize_workspace_name(name)
    if not name_key:
        return False
    existing = (
        await db.execute(
            select(Workspace.id).where(
                Workspace.user_id == user_id,
                Workspace.name_key == name_key,
            )
        )
    ).first()
    return existing is not None


async def active_workspace_image(
    db: AsyncSession,
    *,
    template_id: str,
    image_ref: str,
) -> WorkspaceImage | None:
    """Resolve the exact enabled managed image used for a new workspace."""
    if not image_ref:
        return None
    return (
        await db.execute(
            select(WorkspaceImage)
            .where(
                WorkspaceImage.template_id == template_id,
                WorkspaceImage.image_ref == image_ref,
                WorkspaceImage.enabled.is_(True),
            )
            .order_by(WorkspaceImage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def workspace_runtime_image(
    db: AsyncSession,
    workspace: Workspace,
    template,
) -> tuple[str, str]:
    """Return the immutable image reference/checksum pinned to a workspace."""
    if workspace.image_id:
        image = await db.get(WorkspaceImage, workspace.image_id)
        if image is None:
            raise RuntimeError("Workspace için sabitlenen image kaydı bulunamadı.")
        return image.image_ref, image.sha256
    return template.image_tag, ""


async def allocate_workspace_port(db: AsyncSession, node_id: str) -> int:
    """Allocate a port unique on one worker.

    The database constraint closes races between controller requests; the
    worker remains the final authority because unmanaged host processes may
    also occupy a port.
    """
    stmt = select(Workspace.host_port).where(
        Workspace.node_id == node_id,
        Workspace.status != WorkspaceStatus.DELETED,
    )
    used_ports = set((await db.execute(stmt)).scalars().all())
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("Workspace port aralığında boş port kalmadı.")


async def reserve_workspace(
    db: AsyncSession,
    *,
    data: WorkspaceCreate,
    current_user: User,
    placement: WorkspacePlacement,
    template,
    flavor: Flavor,
    workspace_image: WorkspaceImage | None = None,
    constraints: PlacementConstraints | None = None,
) -> Workspace:
    """Atomically reserve a worker port and, when requested, one GPU slot."""
    node = placement.node
    accelerator = placement.accelerator
    workspace_id = str(uuid.uuid4())
    host_port = await allocate_workspace_port(db, node.id)
    name = data.name.strip()
    # The UUID keeps the container name unique; the slug only makes `podman ps`
    # readable, so an unslugifiable name simply contributes nothing.
    name_fragment = workspace_name_slug(name)
    container_name = "-".join(
        part
        for part in ("devcloud", str(current_user.id), name_fragment, workspace_id[:8])
        if part
    )
    workspace = Workspace(
            id=workspace_id,
            # `name_key` is derived by the mapper from `name`.
            name=name,
            description=data.description.strip(),
            user_id=current_user.id,
            node_id=node.id,
            template_id=data.template_id,
            flavor_id=data.flavor_id,
            image_id=workspace_image.id if workspace_image else None,
            container_name=container_name,
            host_port=host_port,
            container_port=template.default_port,
            storage_path="",
            status=WorkspaceStatus.CREATING,
            auto_stop_minutes=data.auto_stop_minutes,
            tags_json=encode_tags(constraints.tags if constraints else ()),
            accelerator_device_id=accelerator.device_id if accelerator else None,
            accelerator_cdi_name=accelerator.cdi_name if accelerator else None,
            accelerator_model=accelerator.model if accelerator else None,
            accelerator_kind=accelerator.kind if accelerator else None,
            accelerator_slot=accelerator.slot if accelerator else None,
            accelerator_memory_mb=accelerator.memory_mb if accelerator else 0,
            accelerator_shared_slots=accelerator.shared_slots if accelerator else 0,
            created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    await db.flush()
    return workspace


def template_minimum_agent_version(template) -> str | None:
    """Oldest worker release that can start this template correctly, if any."""
    candidates = [
        version
        for version in (
            KILO_CODE_MIN_AGENT_VERSION
            if getattr(template, "ai_assistant", "") == KILO_CODE_ASSISTANT
            else "",
            getattr(template, "min_agent_version", ""),
        )
        if semantic_version(version)
    ]
    return max(candidates, key=semantic_version) if candidates else None


async def schedule_and_reserve_workspace(
    db: AsyncSession,
    *,
    data: WorkspaceCreate,
    current_user: User,
    template,
    flavor: Flavor,
    workspace_image: WorkspaceImage | None = None,
    linked_deployment: MlflowDeployment | None = None,
) -> tuple[Workspace, WorkspacePlacement]:
    """Check name, user quota and placement inside one serialized reservation.

    Raises ``PlacementRequestError`` for an unknown/disabled tag.
    """
    user_id = current_user.id
    constraints = await resolve_workspace_constraints(db, data.tags)
    if await workspace_name_taken(db, user_id=user_id, name=data.name):
        raise WorkspaceNameConflict(WORKSPACE_NAME_CONFLICT_DETAIL)
    existing = (await db.execute(select(Workspace).where(Workspace.user_id == user_id))).scalars().all()
    disk_usage = await get_workspace_disk_usage_by_user(existing)
    if workspace_image is None:
        workspace_image = await active_workspace_image(
            db, template_id=template.id, image_ref=template.image_tag
        )
    required_image = workspace_image.image_ref if workspace_image else template.image_tag
    required_sha256 = workspace_image.sha256 if workspace_image else None
    minimum_agent_version = template_minimum_agent_version(template)
    for attempt in range(5):
        try:
            async with admission_transaction(db):
                user = await db.get(User, user_id, populate_existing=True)
                error = None
                if not (linked_deployment and linked_deployment.quota_reserved):
                    error = await get_quota_error(
                        db, user, flavor, disk_used_bytes=disk_usage.get(user_id, 0)
                    )
                if error:
                    raise QuotaExceeded(error)
                placement = await select_workspace_placement(
                    db,
                    flavor,
                    required_image=required_image,
                    required_image_sha256=required_sha256,
                    minimum_agent_version=minimum_agent_version,
                    # Only a reported older version is refused: every real
                    # worker reports one with its heartbeat.
                    allow_unknown_agent_version=True,
                    constraints=constraints,
                )
                workspace = await reserve_workspace(
                    db, data=data, current_user=user, placement=placement,
                    template=template, flavor=flavor, workspace_image=workspace_image,
                    constraints=constraints,
                )
                if linked_deployment is not None:
                    linked_deployment.workspace_id = workspace.id
                    linked_deployment.quota_reserved = False
                    db.add(linked_deployment)
            return workspace, placement
        except IntegrityError as exc:
            # Port and GPU-slot races are worth retrying; a duplicate name is
            # a decision the caller has to change, so surface it verbatim
            # instead of exhausting the attempts and reporting a 503.
            if _is_workspace_name_conflict(exc):
                raise WorkspaceNameConflict(WORKSPACE_NAME_CONFLICT_DETAIL) from exc
            if attempt == 4:
                raise RuntimeError("Workspace reservation conflicted repeatedly.")
    raise RuntimeError("Workspace reservation failed.")


async def get_quota_error(
    db: AsyncSession,
    user: User,
    flavor: Flavor,
    *,
    disk_used_bytes: int | None = None,
    include_disk: bool = True,
) -> str | None:
    """Return a readable quota error for a proposed workspace allocation.

    CPU and RAM are charged only for workspaces that currently hold compute;
    `quota_violations` applies that filter. The full workspace list is still
    passed through so the disk metric keeps counting stopped workspaces.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.user_id == user.id)
    )
    workspaces = result.scalars().all()
    reservations = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.user_id == user.id,
                MlflowDeployment.quota_reserved.is_(True),
                MlflowDeployment.workspace_id.is_(None),
            )
        )
    ).scalars().all()
    quota_allocations = [
        *workspaces,
        *(SimpleNamespace(flavor_id=item.flavor_id) for item in reservations),
    ]
    if disk_used_bytes is None and include_disk:
        disk_usage = await get_workspace_disk_usage_by_user(workspaces)
        disk_used_bytes = disk_usage.get(user.id, 0)
    quota = await effective_quota(db, user)
    violations = await asyncio.to_thread(
        quota_violations,
        user,
        quota_allocations,
        flavor,
        disk_used_bytes=disk_used_bytes or 0,
        include_disk=include_disk,
        quota=quota,
    )
    if not violations:
        return None
    return "Kullanıcı kotası aşıldı: " + "; ".join(violations) + "."


async def get_resume_quota_error(
    db: AsyncSession,
    workspace: Workspace,
) -> str | None:
    """Return a quota error blocking a workspace from resuming, if any.

    Because a stopped workspace no longer charges CPU and RAM, resuming it is a
    fresh admission decision and has to be re-checked. A workspace already
    holding compute is counted in the current usage, so it admits for free.
    Disk is excluded: the check runs under the admission lock, which must never
    perform worker I/O, and a full disk should not trap a user out of the very
    workspace they need in order to clean it up.
    """
    if consumes_compute(workspace):
        return None
    flavor = get_flavor(workspace.flavor_id)
    if not flavor:
        return None
    owner = await db.get(User, workspace.user_id, populate_existing=True)
    if owner is None:
        return None
    return await get_quota_error(
        db, owner, flavor, disk_used_bytes=0, include_disk=False
    )


async def delete_workspace_resources(
    db: AsyncSession,
    workspace: Workspace,
    *,
    allow_transient: bool = False,
) -> None:
    """Delete one workspace after its owning domain has authorized lifecycle."""
    async with admission_transaction(db):
        await db.refresh(workspace)
        if (
            workspace.status in {
                WorkspaceStatus.CREATING,
                WorkspaceStatus.STARTING,
                WorkspaceStatus.STOPPING,
            }
            and not allow_transient
        ):
            raise WorkspaceOperationError(
                409,
                "Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.",
            )
        previous_status = workspace.status
        workspace.status = WorkspaceStatus.STOPPING
    runtime = runtime_for_node(workspace.node_id)
    try:
        if not await runtime.delete_container(
            workspace.container_name, workspace.storage_path
        ):
            raise RuntimeError(
                "Worker could not delete the workspace; data and tracking were preserved."
            )
    except (RuntimeError, TimeoutError) as exc:
        workspace.status = previous_status
        workspace.error_message = str(exc)
        await db.commit()
        raise WorkspaceOperationError(502, str(exc)) from exc
    await db.delete(workspace)
    await db.commit()
