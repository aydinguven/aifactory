import enum
from datetime import datetime, timezone
from typing import List, TYPE_CHECKING
from sqlalchemy import String, Boolean, DateTime, Enum, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    """User account model."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    team: Mapped[str] = mapped_column(
        String(255), default="", server_default="", nullable=False
    )
    directorate: Mapped[str] = mapped_column(
        String(255), default="", server_default="", nullable=False
    )
    organization_unit: Mapped[str] = mapped_column(
        String(255), default="", server_default="", nullable=False
    )
    # The müdürlük this user heads (their title is a unit-head title), synced
    # from the directory. Managers see their müdürlük's teams in GenAI stats.
    managed_unit: Mapped[str] = mapped_column(
        String(255), default="", server_default="", nullable=False
    )
    # AD security groups (CN, nested included when enabled), JSON list,
    # refreshed at every directory login. Used by workspace access rules.
    directory_groups: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER, nullable=False)
    auth_source: Mapped[str] = mapped_column(String(32), default="internal", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cpu_quota: Mapped[float] = mapped_column(
        Float,
        default=lambda: settings.DEFAULT_USER_CPU_QUOTA,
        server_default=str(settings.DEFAULT_USER_CPU_QUOTA),
        nullable=False,
    )
    memory_mb_quota: Mapped[int] = mapped_column(
        Integer,
        default=lambda: settings.DEFAULT_USER_MEMORY_MB_QUOTA,
        server_default=str(settings.DEFAULT_USER_MEMORY_MB_QUOTA),
        nullable=False,
    )
    disk_mb_quota: Mapped[int] = mapped_column(
        Integer,
        default=lambda: settings.DEFAULT_USER_DISK_MB_QUOTA,
        server_default=str(settings.DEFAULT_USER_DISK_MB_QUOTA),
        nullable=False,
    )
    gpu_quota: Mapped[int] = mapped_column(
        Integer,
        default=lambda: settings.DEFAULT_USER_GPU_QUOTA,
        server_default=str(settings.DEFAULT_USER_GPU_QUOTA),
        nullable=False,
    )
    # Legacy per-user columns above are no longer read for enforcement; the
    # effective quota is override -> team group -> default group (app/quotas.py).
    # NULL means "inherit".
    cpu_quota_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_mb_quota_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_mb_quota_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_quota_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    workspaces: Mapped[List["Workspace"]] = relationship(
        "Workspace", back_populates="owner", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username='{self.username}' role='{self.role}'>"
