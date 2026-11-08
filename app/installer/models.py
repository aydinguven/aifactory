from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any


class DeploymentRole(StrEnum):
    CONTROLLER = "controller"
    ALL_IN_ONE = "all-in-one"
    WORKER = "worker"


class DatabaseMode(StrEnum):
    SQLITE = "sqlite"
    BUNDLED_POSTGRESQL = "bundled-postgresql"
    EXTERNAL_POSTGRESQL = "external-postgresql"


class RegistryMode(StrEnum):
    EXTERNAL = "external"
    PRELOADED = "preloaded"


class ControllerRuntime(StrEnum):
    CONTAINER = "container"
    NATIVE = "native"


class WorkerRuntime(StrEnum):
    CONTAINER = "container"
    NATIVE = "native"


class UpdateSourceType(StrEnum):
    BUNDLE = "bundle"
    GIT = "git"


@dataclass(slots=True)
class InstallConfig:
    role: DeploymentRole
    public_url: str = "http://127.0.0.1"
    controller_url: str = "http://127.0.0.1:8000"
    controller_fallback_ipv4: str = ""
    agent_ca_file: str = ""
    database_mode: DatabaseMode = DatabaseMode.SQLITE
    database_url: str = ""
    registry_mode: RegistryMode = RegistryMode.PRELOADED
    registry_url: str = ""
    controller_runtime: ControllerRuntime = ControllerRuntime.CONTAINER
    worker_runtime: WorkerRuntime = WorkerRuntime.CONTAINER
    service_user: str = "aifactory"
    worker_name: str = ""
    worker_id: str = ""
    enrollment_token_file: str = ""
    workspace_root: str = "/var/lib/aifactory/workspaces"
    install_root: str = "/opt/aifactory"
    state_root: str = "/var/lib/aifactory/installer"
    releases_root: str = "/var/lib/aifactory/releases"
    downloads_root: str = "/srv/aifactory-downloads"
    labels: dict[str, str] = field(default_factory=dict)
    preload_images: bool = False
    enable_tls: bool = False
    tls_hostname: str = ""
    certificate_file: str = ""
    private_key_file: str = ""
    admin_username: str = "admin"
    admin_email: str = "admin@example.com"
    admin_password: str = ""
    update_source_type: UpdateSourceType = UpdateSourceType.BUNDLE
    update_source: str = ""
    update_ref: str = "stable"
    update_token_file: str = ""

    @property
    def installs_controller(self) -> bool:
        return self.role in {DeploymentRole.CONTROLLER, DeploymentRole.ALL_IN_ONE}

    @property
    def installs_worker(self) -> bool:
        return self.role in {DeploymentRole.WORKER, DeploymentRole.ALL_IN_ONE}

    @property
    def containerized_controller(self) -> bool:
        return (
            self.installs_controller
            and self.controller_runtime == ControllerRuntime.CONTAINER
        )

    @property
    def containerized_worker(self) -> bool:
        return self.installs_worker and self.worker_runtime == WorkerRuntime.CONTAINER

    @property
    def fully_containerized(self) -> bool:
        return (
            (not self.installs_controller or self.containerized_controller)
            and (not self.installs_worker or self.containerized_worker)
        )

    def effective_database_url(self) -> str:
        if self.database_mode == DatabaseMode.SQLITE:
            return "sqlite+aiosqlite:////var/lib/aifactory/database/aifactory.db"
        if self.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
            if self.controller_runtime == ControllerRuntime.CONTAINER:
                if not self.database_url:
                    raise ValueError(
                        "Containerized PostgreSQL requires its generated database URL"
                    )
                return self.database_url
            return self.database_url or (
                "postgresql+asyncpg://aifactory@/aifactory?host=/var/run/postgresql"
            )
        if not self.database_url:
            raise ValueError("External PostgreSQL requires database_url")
        if self.database_url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + self.database_url[len("postgresql://") :]
        return self.database_url

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = self.role.value
        data["database_mode"] = self.database_mode.value
        data["registry_mode"] = self.registry_mode.value
        data["controller_runtime"] = self.controller_runtime.value
        data["worker_runtime"] = self.worker_runtime.value
        data["update_source_type"] = self.update_source_type.value
        data.pop("enrollment_token_file", None)
        data.pop("database_url", None)
        data.pop("private_key_file", None)
        data.pop("admin_password", None)
        data.pop("update_token_file", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstallConfig":
        values = dict(data)
        values["role"] = DeploymentRole(values["role"])
        values["database_mode"] = DatabaseMode(
            values.get("database_mode", DatabaseMode.SQLITE)
        )
        values["registry_mode"] = RegistryMode(
            values.get("registry_mode", RegistryMode.PRELOADED)
        )
        # State written before container support always represents a native
        # controller and must not silently change runtime during repair/update.
        values["controller_runtime"] = ControllerRuntime(
            values.get("controller_runtime", ControllerRuntime.NATIVE)
        )
        # State written before worker containers existed always represents the
        # native systemd worker and must not silently migrate during an update.
        values["worker_runtime"] = WorkerRuntime(
            values.get("worker_runtime", WorkerRuntime.NATIVE)
        )
        values["update_source_type"] = UpdateSourceType(
            values.get("update_source_type", UpdateSourceType.BUNDLE)
        )
        # State written by a newer release may carry fields this one does not
        # know; ignore them so a rollback can still read it.
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in values.items() if key in known})

    @classmethod
    def from_json_file(cls, path: Path) -> "InstallConfig":
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("The answer file must contain a JSON object")
        # New answer files select the supported container worker unless the
        # operator explicitly requests the native compatibility runtime.
        loaded.setdefault("worker_runtime", WorkerRuntime.CONTAINER.value)
        return cls.from_dict(loaded)
