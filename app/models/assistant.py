from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AssistantSettings(Base):
    """Singleton policy for the in-app assistant and the MCP endpoint."""

    __tablename__ = "assistant_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Side-panel assistant; it calls LiteLLM with each user's workspace key.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # /mcp endpoint for external clients, authenticated by access tokens.
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When off, tools that change state are neither listed nor callable.
    allow_actions: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ApiToken(Base):
    """A user's personal access token for the MCP endpoint.

    The token is shown once; only its SHA-256 is stored. A token acts as its
    owner, with the owner's current role, limited by ``scope``.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # First characters of the token, so the owner can tell tokens apart.
    token_prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # "read": read-only tools. "write": also tools that change state.
    scope: Mapped[str] = mapped_column(String(16), default="read", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
