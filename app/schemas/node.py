from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.node import NodeStatus
from app.placement import TAG_NAME_PATTERN


class NodeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    schedulable: bool = True
    labels: dict[str, str] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    enabled: bool | None = None
    schedulable: bool | None = None
    labels: dict[str, str] | None = None
    gpu_slots_per_device: int | None = Field(default=None, ge=0, le=3)


class NodeLabelsUpdate(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)


class NodePlacementUpdate(BaseModel):
    """Placement tags of a worker."""

    tags: list[str] = Field(default_factory=list, max_length=50)


class PlacementTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=TAG_NAME_PATTERN)
    description: str = Field(default="", max_length=255)


class PlacementTagUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class PlacementTagOut(BaseModel):
    name: str
    description: str
    enabled: bool
    worker_count: int = 0

    model_config = {"from_attributes": True}


class NodeOut(BaseModel):
    id: str
    name: str
    hostname: str
    enabled: bool
    schedulable: bool
    status: NodeStatus
    cpu_total: float
    memory_total_mb: int
    disk_total_mb: int
    cpu_percent: float = 0.0
    memory_used_mb: int = 0
    disk_used_mb: int = 0
    active_containers_count: int = 0
    gpu_slots_per_device: int = 0
    labels: dict[str, str]
    tags: list[str] = Field(default_factory=list)
    gpu_worker: bool = False
    capabilities: dict
    inventory: list[dict] = Field(default_factory=list)
    reconciliation: dict = Field(default_factory=dict)
    agent_version: str
    last_seen_at: datetime | None
    created_at: datetime
    connected: bool = False


class NodeCreated(NodeOut):
    enrollment_token: str


class WorkerContainerInventory(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    container_name: str = Field(min_length=1, max_length=128)
    host_port: int = Field(ge=1, le=65535)
    storage_path: str = Field(default="", max_length=512)
    status: str = Field(default="unknown", max_length=32)


class NodeHeartbeat(BaseModel):
    hostname: str = Field(default="", max_length=255)
    cpu_total: float = Field(ge=0, le=4096)
    memory_total_mb: int = Field(ge=0)
    disk_total_mb: int = Field(ge=0)
    cpu_percent: float = Field(default=0.0, ge=0, le=100)
    memory_used_mb: int = Field(default=0, ge=0)
    disk_used_mb: int = Field(default=0, ge=0)
    active_containers_count: int = Field(default=0, ge=0)
    capabilities: dict = Field(default_factory=dict)
    inventory: list[WorkerContainerInventory] = Field(default_factory=list)
    agent_version: str = Field(default="", max_length=64)

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_be_object(cls, value: dict) -> dict:
        return value

