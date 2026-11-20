from pydantic import BaseModel, Field


class CustomTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=50)
    default_port: int | None = Field(default=None, ge=1, le=65535)
    # Comma/newline separated entries such as "t2-arge, grup:t2-yapayzeka";
    # see app/template_access.py. Empty allow = everyone.
    access_allow: str | None = Field(default=None, max_length=20000)
    access_deny: str | None = Field(default=None, max_length=20000)
