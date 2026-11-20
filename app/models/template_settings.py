from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TemplateSettings(Base):
    """Admin-controlled metadata for workspace templates.

    Display fields apply to built-in templates (custom templates keep theirs
    in ``custom_templates``); access rules apply to both.
    """

    __tablename__ = "template_settings"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    # Who may use the template (app/template_access.py): JSON lists of
    # entries such as "t2-arge" or "grup:t2-arge". Empty allow = everyone.
    access_allow: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    access_deny: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
