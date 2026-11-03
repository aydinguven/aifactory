from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BambooSettings(Base):
    """Singleton Bamboo plan queued after each published Kubernetes image release."""

    __tablename__ = "bamboo_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    plan_key: Mapped[str] = mapped_column(
        String(64), default="AIMLOPS-RETAG", nullable=False
    )
    encrypted_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
