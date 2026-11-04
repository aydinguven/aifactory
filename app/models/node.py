import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class NodeStatus(str, enum.Enum):
    PENDING = "pending"
    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"


class Node(Base):
    """A worker host that runs workspace containers."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[NodeStatus] = mapped_column(
        Enum(NodeStatus), default=NodeStatus.PENDING, nullable=False
    )

    cpu_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=True)
    memory_total_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    disk_total_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=True)
    memory_used_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    disk_used_mb: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    active_containers_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    gpu_slots_per_device: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    labels_json: Mapped[str] = mapped_column(Text, default="{}", nullable=True)
    # Placement tags of this worker (JSON list of names).
    tags_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]", nullable=False
    )
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}", nullable=True)
    inventory_json: Mapped[str] = mapped_column(Text, default="[]", nullable=True)
    reconciliation_json: Mapped[str] = mapped_column(Text, default="{}", nullable=True)
    agent_version: Mapped[str] = mapped_column(String(64), default="", nullable=True)
    agent_token_hash: Mapped[str] = mapped_column(String(64), default="", nullable=True)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    workspaces: Mapped[list["Workspace"]] = relationship("Workspace", back_populates="node")

    @property
    def tags(self) -> list[str]:
        from app.placement import decode_tags

        return decode_tags(self.tags_json)
