from __future__ import annotations

import getpass
import sys
from collections.abc import Callable

from app.installer.models import (
    ControllerRuntime,
    DatabaseMode,
    DeploymentRole,
    InstallConfig,
    RegistryMode,
    UpdateSourceType,
)


class InstallerUI:
    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        output=None,
    ):
        self.input = input_fn
        self.secret = secret_fn
        self.output = output or sys.stdout

    def write(self, message: str = "") -> None:
        print(message, file=self.output)

    def choose(self, title: str, options: list[tuple[str, str]], default: int = 1) -> str:
        self.write(f"\n{title}")
        for index, (_, label) in enumerate(options, start=1):
            suffix = " (recommended)" if index == default else ""
            self.write(f"  {index}. {label}{suffix}")
        while True:
            raw = self.input(f"Select [{default}]: ").strip()
            if not raw:
                return options[default - 1][0]
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            self.write("Enter one of the displayed numbers.")

    def ask(self, label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.input(f"{label}{suffix}: ").strip()
        return answer or default

    def confirm(self, label: str, default: bool = True) -> bool:
        suffix = "Y/n" if default else "y/N"
        while True:
            answer = self.input(f"{label} [{suffix}]: ").strip().lower()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.write("Enter yes or no.")

    def installation_role(self) -> DeploymentRole:
        value = self.choose(
            "Installation type",
            [
                (DeploymentRole.CONTROLLER.value, "Controller"),
                (DeploymentRole.ALL_IN_ONE.value, "All-in-one"),
                (DeploymentRole.WORKER.value, "Worker"),
            ],
            default=1,
        )
        return DeploymentRole(value)

    def collect_install_config(self, role: DeploymentRole | None = None) -> InstallConfig:
        role = role or self.installation_role()
        config = InstallConfig(role=role)

        if config.installs_controller:
            config.controller_runtime = ControllerRuntime(
                self.choose(
                    "Controller runtime",
                    [
                        (
                            ControllerRuntime.CONTAINER.value,
                            "Podman container with Quadlet",
                        ),
                        (
                            ControllerRuntime.NATIVE.value,
                            "Native Python systemd service",
                        ),
                    ],
                    default=1,
                )
            )
            config.public_url = self.ask(
                "Public controller URL", "http://127.0.0.1"
            ).rstrip("/")
            config.admin_username = self.ask("Initial administrator username", "admin")
            config.admin_email = self.ask(
                "Initial administrator email", "admin@example.com"
            )
            while not config.admin_password:
                first = self.secret("Initial administrator password: ")
                second = self.secret("Confirm administrator password: ")
                if first and first == second:
                    config.admin_password = first
                else:
                    self.write("Passwords must be non-empty and match.")
            config.controller_url = "http://127.0.0.1:8000"
            db_default = 1 if role == DeploymentRole.CONTROLLER else 2
            config.database_mode = DatabaseMode(
                self.choose(
                    "Database",
                    [
                        (
                            DatabaseMode.BUNDLED_POSTGRESQL.value,
                            "Bundled PostgreSQL",
                        ),
                        (DatabaseMode.SQLITE.value, "SQLite"),
                        (
                            DatabaseMode.EXTERNAL_POSTGRESQL.value,
                            "External PostgreSQL",
                        ),
                    ],
                    default=db_default,
                )
            )
            if config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL:
                config.database_url = self.secret("PostgreSQL SQLAlchemy URL: ")
            config.registry_url = self.ask(
                "Shared OCI registry prefix for administrator-built images (optional)",
                "",
            ).rstrip("/")
            config.registry_mode = (
                RegistryMode.EXTERNAL
                if config.registry_url
                else RegistryMode.PRELOADED
            )
            config.update_source_type = UpdateSourceType(
                self.choose(
                    "Platform update source",
                    [
                        (UpdateSourceType.GIT.value, "Git repository release channel"),
                        (UpdateSourceType.BUNDLE.value, "Local/uploaded platform bundle"),
                    ],
                    default=1,
                )
            )
            if config.update_source_type == UpdateSourceType.GIT:
                config.update_source = self.ask(
                    "Git repository URL or local path",
                    "https://github.com/aydinguven/aifactory.git",
                )
                config.update_ref = self.ask(
                    "Release channel branch/tag", "stable"
                )

        if config.installs_worker:
            if role == DeploymentRole.WORKER:
                config.controller_url = self.ask(
                    "Controller URL", "https://aifactory.example.com"
                ).rstrip("/")
                config.worker_id = self.ask("Worker ID")
                config.enrollment_token_file = self.ask(
                    "Enrollment token file", "/root/aifactory-enrollment-token"
                )
            config.worker_name = self.ask("Worker name", "aifactory-worker-01")
            config.workspace_root = self.ask(
                "Workspace storage", "/var/lib/aifactory/workspaces"
            )
            config.preload_images = False

        return config

    def show_plan(self, title: str, lines: list[str]) -> None:
        self.write(f"\n{title}")
        for line in lines:
            self.write(f"  - {line}")
