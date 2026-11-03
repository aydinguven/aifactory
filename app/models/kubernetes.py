"""Kubernetes projects, applications and the model images published for them."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ImageReleaseStatus:
    """Release states, stored as plain strings so PostgreSQL needs no enum type."""

    QUEUED = "queued"
    BUILDING = "building"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"

    ACTIVE = frozenset({QUEUED, BUILDING, PUBLISHING})
    TERMINAL = frozenset({PUBLISHED, FAILED})


class BambooTriggerStatus:
    """Bamboo retag trigger of a published release; "" when the integration was off."""

    NONE = ""
    PENDING = "pending"
    TRIGGERING = "triggering"
    QUEUED = "queued"
    FAILED = "failed"


class K8sProject(Base):
    """An RFS project: one Kubernetes namespace with one or more ML services."""

    __tablename__ = "k8s_projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    rfs_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class K8sApplication(Base):
    """One ML service (container) of a project, bound to one MLflow model."""

    __tablename__ = "k8s_applications"
    __table_args__ = (
        UniqueConstraint("project_id", "app_name", name="uq_k8s_application_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("k8s_projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    app_name: Mapped[str] = mapped_column(String(100), nullable=False)
    mlflow_model_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    bitbucket_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class MlflowImageRelease(Base):
    """One MLflow model version published to the deploy registry as X.Y.Z."""

    __tablename__ = "mlflow_image_releases"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("k8s_applications.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    build_id: Mapped[str | None] = mapped_column(
        ForeignKey("mlflow_model_builds.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    image_ref: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    source_ref: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    digest: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    mlflow_tagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Bamboo retag plan queued after publishing; see BambooTriggerStatus.
    bamboo_status: Mapped[str] = mapped_column(
        String(16), default="", nullable=False, index=True
    )
    bamboo_result_key: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    bamboo_result_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    bamboo_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    bamboo_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=ImageReleaseStatus.QUEUED, nullable=False, index=True
    )
    status_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MlflowImageReleaseEvent(Base):
    """Ordered, durable progress entry for an image release."""

    __tablename__ = "mlflow_image_release_events"
    __table_args__ = (
        UniqueConstraint("release_id", "sequence", name="uq_mlflow_image_release_event_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("mlflow_image_releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
