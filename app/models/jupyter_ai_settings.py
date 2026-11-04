from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.jupyter_ai import default_model_catalog_json


class JupyterAiSettings(Base):
    """Singleton controller-managed Jupyter AI gateway configuration."""

    __tablename__ = "jupyter_ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cline_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    gateway_url: Mapped[str] = mapped_column(
        String(512), default="", nullable=False
    )
    model_id: Mapped[str] = mapped_column(
        String(255), default="", nullable=False
    )
    gateway_model_discovery: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    model_catalog_json: Mapped[str] = mapped_column(
        Text, default=default_model_catalog_json, nullable=False
    )
    encrypted_shared_token: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )
    # Public root/intermediate CAs of a privately signed gateway. Workers use
    # it for the connectivity test and install it in workspace containers.
    ca_bundle_pem: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
