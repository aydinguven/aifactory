from datetime import datetime
from pydantic import BaseModel, Field
from app.models.workspace import WorkspaceStatus


class FlavorInfo(BaseModel):
    id: str
    name: str
    display_name: str = ""
    description: str
    cpus: float
    memory_mb: int
    memory_display: str
    accelerator_count: int = 0
    accelerator_vendor: str = ""
    accelerator_memory_mb: int = 0
    accelerator_display: str = ""
    available: bool | None = None
    availability_message: str = ""
    available_slots: int | None = None
    eligible_accelerator_models: list[str] = Field(default_factory=list)
    allocation_modes: list[str] = Field(default_factory=list)
    gpu_quota_remaining: int | None = None
    enabled: bool = True


class TemplateInfo(BaseModel):
    id: str
    name: str
    description: str
    category: str
    icon: str
    default_port: int
    image_tag: str
    features: list[str]
    requires_accelerator: bool = False


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=60, pattern=r"^[a-zA-Z0-9_\-\ ]+$")
    description: str = Field(default="", max_length=255)
    template_id: str
    flavor_id: str
    auto_stop_minutes: int = Field(default=0, ge=0, le=1440, description="Maximum minutes since each start, even while active; 0 disables the limit.")
    tags: list[str] = Field(default_factory=list, max_length=10, description="Custom placement tags the worker must carry.")


class PlacementOption(BaseModel):
    name: str
    description: str = ""


class PlacementOptions(BaseModel):
    tags: list[PlacementOption] = Field(default_factory=list)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    description: str
    user_id: int
    node_id: str
    template_id: str
    flavor_id: str
    image_id: str | None = None
    container_name: str
    host_port: int
    container_port: int
    status: WorkspaceStatus
    storage_path: str
    auto_stop_minutes: int = 0
    created_at: datetime
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None
    error_message: str | None = None
    accelerator_device_id: str | None = None
    accelerator_cdi_name: str | None = None
    accelerator_model: str | None = None
    accelerator_kind: str | None = None
    accelerator_slot: int | None = None
    accelerator_memory_mb: int = 0
    accelerator_shared_slots: int = 0
    tags: list[str] = Field(default_factory=list)
    web_url: str | None = None

    model_config = {"from_attributes": True}


class WorkspaceStatusOut(BaseModel):
    id: str
    status: WorkspaceStatus
    container_id: str | None = None
    host_port: int
    is_running: bool
    logs: str | None = None
