from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The only kind left; migration 37 removed the former "datacenter" tags.
TAG_KIND_CUSTOM = "custom"


class PlacementTag(Base):
    """Admin-defined placement tag assigned to workers and requested by workspaces.

    A tag is a capability a workspace can require; the worker must carry every
    requested tag. GPU/CPU is not a catalog tag: it is detected from the
    worker's reported hardware.
    """

    __tablename__ = "placement_tags"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=TAG_KIND_CUSTOM)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Disabled tags stay valid on workers and existing workspaces but are no
    # longer offered on the workspace create form.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
