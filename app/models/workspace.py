import enum
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from sqlalchemy import String, Integer, DateTime, Enum, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.database import Base

if TYPE_CHECKING:
    from app.models.node import Node
    from app.models.user import User


class WorkspaceStatus(str, enum.Enum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    ERROR = "error"
    DELETED = "deleted"


# Statuses whose flavor reservation still occupies worker CPU and RAM.
#
# A STOPPED workspace keeps its bind-mounted storage but its container is not
# running, so it releases compute. ERROR stays in the set because the container
# may still be alive after a failed transition. Node placement and per-user
# CPU/RAM quota both charge exactly this set.
ACTIVE_ALLOCATION_STATUSES = frozenset({
    WorkspaceStatus.CREATING,
    WorkspaceStatus.STARTING,
    WorkspaceStatus.STOPPING,
    WorkspaceStatus.ERROR,
    WorkspaceStatus.RUNNING,
})

# `Enum.__hash__` hashes the member *name*, so a raw "running" string never
# matches the enum set above. Compare against the values when the status may
# arrive as a plain string from a serialized payload or a test double.
ACTIVE_ALLOCATION_STATUS_VALUES = frozenset(
    status.value for status in ACTIVE_ALLOCATION_STATUSES
)


def consumes_compute(workspace) -> bool:
    """Report whether a workspace currently charges CPU and RAM.

    Objects without a status are pending reservations that have no lifecycle
    row yet (an MLflow deployment awaiting its workspace), and are charged.
    """
    status = getattr(workspace, "status", None)
    if status is None:
        return True
    return getattr(status, "value", status) in ACTIVE_ALLOCATION_STATUS_VALUES


def normalize_workspace_name(name: str) -> str:
    """Return the per-owner uniqueness key for a workspace name.

    Names are compared without regard to case or repeated whitespace, so one
    user cannot own both "My App" and "my  app". Normalizing in Python instead
    of relying on database collation keeps SQLite and PostgreSQL in agreement.
    """
    return " ".join(str(name or "").strip().lower().split())


def workspace_name_slug(name: str, *, limit: int = 32) -> str:
    """Return a container-safe fragment derived from a workspace name."""
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_workspace_name(name))
    return slug.strip("-")[:limit].strip("-")


class Workspace(Base):
    """Workspace instance deployed as a container."""
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("node_id", "host_port", name="uq_workspaces_node_host_port"),
        UniqueConstraint(
            "node_id",
            "accelerator_device_id",
            "accelerator_slot",
            name="uq_workspaces_accelerator_slot",
        ),
        # Names are the owner's namespace, not the platform's: two users may
        # both keep a "MyWorkspace", one user may not.
        UniqueConstraint("user_id", "name_key", name="uq_workspaces_user_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Normalized form of `name`, maintained by `normalize_workspace_name`.
    name_key: Mapped[str] = mapped_column(
        String(100), default="", server_default="", nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    
    # Ownership
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner: Mapped["User"] = relationship("User", back_populates="workspaces")

    # Every workspace belongs to a worker. The controller has no local
    # container runtime, including in an all-in-one deployment.
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    node: Mapped["Node"] = relationship("Node", back_populates="workspaces")

    # Specifications
    template_id: Mapped[str] = mapped_column(String(50), nullable=False)  # vscode-kilo, jupyter-python, etc.
    flavor_id: Mapped[str] = mapped_column(String(50), nullable=False)    # t1.nano through t1.xlarge
    image_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspace_images.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    accelerator_device_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    accelerator_cdi_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    accelerator_model: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    accelerator_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    accelerator_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accelerator_memory_mb: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    accelerator_shared_slots: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Container details
    container_id: Mapped[str] = mapped_column(String(128), nullable=True)
    container_name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    host_port: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    container_port: Mapped[int] = mapped_column(Integer, default=8080, nullable=False)
    workspace_token: Mapped[str] = mapped_column(String(128), default=lambda: uuid.uuid4().hex, nullable=False)
    # SHA-256 of the Kilo Code config the container was created with ("" for
    # other templates). A different value on start recreates the container so
    # a newly requested AI Factory key or changed Admin settings take effect.
    ai_config_digest: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    
    # Persistence
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    
    # Lifecycle
    status: Mapped[WorkspaceStatus] = mapped_column(
        Enum(WorkspaceStatus), default=WorkspaceStatus.CREATING, nullable=False
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    auto_stop_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = disabled
    # Placement tags the chosen worker had to carry (recorded at creation).
    tags_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]", nullable=False
    )
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("name")
    def _track_name_key(self, _field: str, value: str) -> str:
        """Derive the uniqueness key whenever the display name is set.

        Keeping this in the mapper means no caller can persist a workspace
        whose key disagrees with its name, including the direct constructor
        calls made by fixtures and internal services.
        """
        self.name_key = normalize_workspace_name(value)
        return value

    @property
    def tags(self) -> list[str]:
        from app.placement import decode_tags

        return decode_tags(self.tags_json)

    def __repr__(self) -> str:
        return f"<Workspace id={self.id} name='{self.name}' status='{self.status}'>"
