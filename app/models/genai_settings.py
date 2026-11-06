from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GenAiSettings(Base):
    """Singleton admin-managed LiteLLM connection for self-service API keys.

    ``encrypted_admin_key`` belongs to a LiteLLM ``proxy_admin`` user and never
    leaves the controller. Empty optional defaults mean LiteLLM's own defaults.
    """

    __tablename__ = "genai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    encrypted_admin_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    validate_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ca_cert_file: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    user_role: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    models_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    max_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_duration: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    key_duration: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # LiteLLM team alias or id every newly created user joins.
    default_team: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # Team aliases/ids, highest tier first; keys bind to the first one the
    # user belongs to.
    team_priority_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # Create the LiteLLM user and workspace key at every AD user's login.
    auto_provision_directory_users: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
