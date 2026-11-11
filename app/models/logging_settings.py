from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LoggingSettings(Base):
    """Singleton logging policy applied by the controller and every worker."""

    __tablename__ = "logging_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Incremented on every save; workers report the revision they applied.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    max_total_mb: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    log_level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    forward_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    forward_host: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    forward_port: Mapped[int] = mapped_column(Integer, default=514, nullable=False)
    forward_protocol: Mapped[str] = mapped_column(String(8), default="tcp", nullable=False)
    forward_format: Mapped[str] = mapped_column(String(16), default="leef", nullable=False)
    forward_tls_verify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    forward_ca_pem: Mapped[str] = mapped_column(Text, default="", nullable=False)
    forward_audit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    forward_app: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    forward_app_min_level: Mapped[str] = mapped_column(
        String(16), default="WARNING", nullable=False
    )
    forward_access: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
