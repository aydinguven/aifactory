from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkspaceImageTemplateCreate(BaseModel):
    id: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=255)
    category: str = Field(default="Custom", min_length=1, max_length=50)
    default_port: int = Field(default=8080, ge=1, le=65535)
    ide_type: Literal["vscode", "jupyter", "service", "terminal", "browser"] = "vscode"

    model_config = {"str_strip_whitespace": True}


class WorkspaceImageRegistryImport(BaseModel):
    template_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9-]+$")
    display_name: str = Field(default="", max_length=160)
    source_ref: str = Field(min_length=3, max_length=1024)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=4096)
    new_template: WorkspaceImageTemplateCreate | None = None

    @field_validator("source_ref")
    @classmethod
    def registry_reference_only(cls, value: str) -> str:
        normalized = value.strip()
        if "://" in normalized and not normalized.startswith("docker://"):
            raise ValueError("Only docker:// registry references are supported")
        return normalized

    @model_validator(mode="after")
    def matching_new_template_id(self):
        if self.new_template and self.template_id != self.new_template.id:
            raise ValueError("template_id must match new_template.id")
        return self


class WorkspaceImageUpdate(BaseModel):
    enabled: bool


class WorkspaceImageWorkerProgress(BaseModel):
    node_id: str
    node_name: str
    state: str = "pending"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    error: str = ""


class WorkspaceImageOut(BaseModel):
    id: str
    template_id: str
    display_name: str
    image_ref: str
    source_type: str
    source_ref: str
    digest: str
    sha256: str
    filename: str
    size: int
    architecture: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    synced_workers: int = 0
    total_workers: int = 0
    workers: list[WorkspaceImageWorkerProgress] = Field(default_factory=list)

    model_config = {"from_attributes": True}
