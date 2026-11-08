from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
import uuid
from urllib.parse import quote, urlsplit
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable

from app.installer.models import (
    ControllerRuntime,
    DatabaseMode,
    DeploymentRole,
    InstallConfig,
    RegistryMode,
    UpdateSourceType,
)
from app.installer.backup import create_backup, restore_backup
from app.installer.layout import CURRENT, LEGACY, current_env_key
from app.installer.progress import DEFAULT_QUEUE_ROOT, ProgressReporter
from app.installer.platform import (
    CommandRunner,
    HostPlatform,
    InstallerError,
    detect_platform,
    host_python,
    host_python_packages,
    validate_target,
)
from app.installer.state import InstallationState
from app.platform_release import load_platform_release

if TYPE_CHECKING:  # pragma: no cover
    from app.installer.legacy_layout import LegacyMigration


# Retention after a successful update: the current and previous release
# (rollback target), their runtime images, and the newest backups.
KEEP_RELEASES = 2
KEEP_BACKUPS = 3
RELEASE_MARKERS = (".aifactory-release-id", ".devcloud-release-id")
# Head room an update needs beyond a second copy of the release tree.
UPDATE_FREE_MARGIN_BYTES = 2 * 1024 ** 3


def _tree_size(root: Path) -> int:
    total = 0
    for directory, _subdirectories, files in os.walk(root):
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except OSError:
                pass
    return total


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


@dataclass(slots=True)
class PlanStep:
    key: str
    description: str
    apply: Callable[[], None]


class InstallPlan:
    def __init__(
        self,
        title: str,
        steps: list[PlanStep],
        *,
        on_failure: Callable[[], None] | None = None,
        progress: ProgressReporter | None = None,
    ):
        self.title = title
        self.steps = steps
        self.on_failure = on_failure
        self.progress = progress or ProgressReporter(None)

    @property
    def descriptions(self) -> list[str]:
        return [step.description for step in self.steps]

    def execute(self) -> None:
        try:
            for index, step in enumerate(self.steps):
                self.progress.step(index, len(self.steps), step.key, step.description)
                step.apply()
        except Exception as exc:
            if self.on_failure is not None:
                self.progress.rollback(str(exc))
                try:
                    self.on_failure()
                except Exception as rollback_exc:
                    # Report the original failure first; a rollback error alone
                    # hides why the update stopped.
                    raise InstallerError(
                        f"{exc}\nAutomatic rollback also failed: {rollback_exc}"
                    ) from exc
            raise


class InstallerEngine:
    """Build and apply repeatable installation plans.

    The engine deliberately separates plan creation from execution so the
    interactive UI, answer-file automation, and tests all exercise the same
    actions.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        filesystem_root: Path = Path("/"),
        runner: CommandRunner | None = None,
    ):
        self.project_root = (
            project_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.filesystem_root = filesystem_root.resolve()
        self.runner = runner or CommandRunner()
        self.release_version = self._source_version()
        self.release_id = self._source_release_id()
        self.previous_release: Path | None = None
        self.migration_applied = False
        # Set while a devcloud host moves to the new names from inside the
        # still running devcloud updater (see LegacyMigration).
        self.defer_update_watcher = False

    def _source_version(self) -> str:
        version_file = self.project_root / "app" / "__init__.py"
        if not version_file.is_file():
            raise InstallerError(f"Release version file is missing: {version_file}")
        match = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            version_file.read_text(encoding="utf-8"),
        )
        if not match:
            raise InstallerError("Release does not declare app.__version__")
        return match.group(1)

    def _source_release_id(self) -> str:
        manifest = self.project_root / "release.json"
        if manifest.is_file():
            try:
                import json

                source_commit = str(
                    json.loads(manifest.read_text(encoding="utf-8")).get(
                        "source_commit", ""
                    )
                )
            except (OSError, ValueError):
                source_commit = ""
            if re.fullmatch(r"[0-9a-fA-F]{7,64}", source_commit):
                return f"{self.release_version}-{source_commit[:12].lower()}"

        digest = hashlib.sha256()
        ignored = {
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "data",
            "dist",
            "offline",
        }
        files: list[Path] = []
        for directory, directory_names, file_names in os.walk(
            self.project_root, followlinks=False
        ):
            directory_names[:] = sorted(
                name for name in directory_names if name not in ignored
            )
            root = Path(directory)
            for name in sorted(file_names):
                path = root / name
                if path.is_file() and not path.is_symlink():
                    files.append(path)
        files.sort()
        for path in files:
            relative = path.relative_to(self.project_root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"{self.release_version}-{digest.hexdigest()[:12]}"

    def host_path(self, value: str | Path) -> Path:
        raw = str(value)
        path = Path(raw)
        if self.filesystem_root == Path("/"):
            return path
        if not PurePosixPath(raw.replace("\\", "/")).is_absolute():
            raise InstallerError(f"Expected an absolute installation path: {path}")
        return self.filesystem_root / raw.lstrip("/\\")

    @staticmethod
    def _public_hostname(public_url: str) -> str:
        parsed = urlsplit(public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InstallerError(
                "Public controller URL must be an http:// or https:// URL with a host"
            )
        return parsed.hostname

    def state_path(self, config: InstallConfig) -> Path:
        return self.host_path(config.state_root) / "install-state.json"

    def current_state(self, state_root: str = CURRENT.state_root) -> InstallationState | None:
        return InstallationState.load(self.host_path(state_root) / "install-state.json")

    def preflight(self) -> None:
        if self.runner.dry_run:
            return
        if os.name != "posix":
            raise InstallerError("AI Factory installation is supported only on Linux hosts.")
        if os.geteuid() != 0:
            raise InstallerError("Run aifactory-setup as root.")
        host = detect_platform(self.host_path("/etc/os-release"))
        validate_target(host)
        if not self.runner.exists("dnf"):
            raise InstallerError("DNF is required on Rocky Linux and RHEL 9/10.")
        if not self.runner.exists("systemctl"):
            raise InstallerError("systemd is required.")

    def _host_platform(self) -> HostPlatform | None:
        try:
            return detect_platform(self.host_path("/etc/os-release"))
        except (InstallerError, OSError):
            return None

    def build_install_plan(self, config: InstallConfig) -> InstallPlan:
        self._validate_config(config)
        steps = [
            PlanStep("preflight", "Validate Rocky/RHEL 9/10, root access, DNF, and systemd", self.preflight),
            PlanStep(
                "packages",
                "Install required operating-system packages from configured DNF repositories",
                lambda: self._install_packages(config),
            ),
            PlanStep(
                "service-user",
                f"Create or validate the dedicated {config.service_user} service account",
                lambda: self._ensure_service_user(config),
            ),
            PlanStep(
                "directories",
                "Create application, configuration, state, release, and workspace directories",
                lambda: self._ensure_directories(config),
            ),
            PlanStep(
                "database",
                "Initialize the selected database service and database role",
                lambda: self._configure_database(config),
            ),
            PlanStep(
                "release",
                f"Install AI Factory {self.release_version} as an immutable side-by-side release",
                lambda: self._install_release(config),
            ),
            PlanStep(
                "python",
                (
                    "Use Python dependencies embedded in the immutable controller image"
                    if config.containerized_controller and not config.installs_worker
                    else "Create the release virtual environment and install verified Python dependencies"
                ),
                lambda: self._install_python_dependencies(config),
            ),
            PlanStep(
                "configuration",
                "Write role configuration and protected credentials",
                lambda: self._write_configuration(config),
            ),
            PlanStep(
                "services",
                "Install and enable the selected systemd services",
                lambda: self._install_services(config),
            ),
        ]
        runtime_step_index = 6
        if config.containerized_controller:
            steps.insert(
                runtime_step_index,
                PlanStep(
                    "controller-images",
                    "Load verified controller images or build them from this release",
                    lambda: self._prepare_controller_images(config),
                ),
            )
            runtime_step_index += 1
        if config.containerized_worker:
            steps.insert(
                runtime_step_index,
                PlanStep(
                    "worker-image",
                    "Load the verified worker image or build it from this release",
                    lambda: self._prepare_worker_image(config),
                ),
            )
        if config.installs_worker:
            steps.extend(
                [
                    PlanStep(
                        "selinux",
                        "Configure persistent workspace storage and SELinux labels",
                        lambda: self._configure_worker_storage(config),
                    ),
                    PlanStep(
                        "images",
                        "Prepare controller-managed workspace image synchronization",
                        lambda: self._prepare_images(config),
                    ),
                ]
            )
        if config.installs_controller:
            steps.extend(
                [
                    PlanStep(
                        "migrations",
                        (
                            "Apply database migrations in the controller image before Uvicorn starts"
                            if config.containerized_controller
                            else "Apply explicit database migrations before starting the controller"
                        ),
                        lambda: self._run_migrations(config),
                    ),
                    PlanStep(
                        "ingress",
                        "Install the controller ingress service and initial HTTP configuration",
                        lambda: self._install_ingress(config),
                    ),
                ]
            )
        steps.extend(
            [
                PlanStep(
                    "start",
                    "Start services and wait for systemd activation",
                    lambda: self._start_services(config),
                ),
                PlanStep(
                    "state",
                    "Record the completed installation without persisting secrets",
                    lambda: self._save_state(config),
                ),
            ]
        )
        return InstallPlan(f"Install AI Factory {config.role.value}", steps)

    def build_repair_plan(self, config: InstallConfig) -> InstallPlan:
        self._validate_config(config)
        return InstallPlan(
            f"Repair AI Factory {config.role.value}",
            [
                PlanStep("preflight", "Re-run host preflight checks", self.preflight),
                PlanStep(
                    "directories",
                    "Repair directory ownership and permissions",
                    lambda: self._ensure_directories(config),
                ),
                PlanStep(
                    "controller-images",
                    "Verify or reload runtime container images",
                    lambda: (
                        self._prepare_controller_images(config, refresh=True)
                        if config.containerized_controller
                        else None,
                        self._prepare_worker_image(config, refresh=True)
                        if config.containerized_worker
                        else None,
                    ),
                ),
                PlanStep(
                    "configuration",
                    "Re-render configuration while preserving existing secrets",
                    lambda: self._write_configuration(config),
                ),
                PlanStep(
                    "services",
                    "Reinstall systemd service definitions",
                    lambda: self._install_services(config),
                ),
                PlanStep(
                    "selinux",
                    "Reapply workspace SELinux labels when a worker role is installed",
                    lambda: self._configure_worker_storage(config)
                    if config.installs_worker
                    else None,
                ),
                PlanStep(
                    "restart",
                    "Restart installed services and verify systemd activation",
                    lambda: self._restart_services(config),
                ),
                PlanStep(
                    "state",
                    "Refresh installation state",
                    lambda: self._save_state(config),
                ),
            ],
        )

    def build_update_plan(
        self,
        config: InstallConfig,
        *,
        migration: "LegacyMigration | None" = None,
    ) -> InstallPlan:
        """Update plan; ``migration`` first moves a devcloud host to the new names."""
        self._validate_config(config)
        # The admin panel and worker heartbeats read the step progress.
        self.progress = ProgressReporter.from_environment(
            None if self.runner.dry_run else self.host_path(DEFAULT_QUEUE_ROOT)
        )
        backup_step = []
        if config.installs_controller and migration is None:
            state = self.current_state(config.state_root)
            current_version = state.version if state else "unknown"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = self.host_path(
                f"/var/lib/aifactory/backups/pre-update-v{current_version}-{timestamp}.tar.gz"
            )
            backup_step.append(
                PlanStep(
                    "backup",
                    "Create a verified pre-update configuration and database backup",
                    lambda: create_backup(
                        config=config,
                        version=current_version,
                        output=backup_path,
                        host_path=self.host_path,
                        runner=self.runner,
                        include_workspaces=False,
                    ),
                )
            )
        plan = InstallPlan(
            f"Update AI Factory to {self.release_version}",
            [
                PlanStep(
                    "preflight",
                    "Validate the target host, release source and free disk space",
                    lambda: (self.preflight(), self._check_update_disk_space(config)),
                ),
                *backup_step,
                *(migration.steps() if migration else []),
                PlanStep(
                    "release",
                    f"Stage immutable release {self.release_version} without removing the current release",
                    lambda: self._install_release(config),
                ),
                *(
                    [migration.worker_storage_step()]
                    if migration and config.installs_worker
                    else []
                ),
                PlanStep(
                    "controller-images",
                    "Load the new verified runtime images",
                    lambda: (
                        self._prepare_controller_images(config, refresh=True)
                        if config.containerized_controller
                        else None,
                        self._prepare_worker_image(config, refresh=True)
                        if config.containerized_worker
                        else None,
                    ),
                ),
                PlanStep(
                    "python",
                    "Install bundled Python dependencies or reuse an identical existing environment",
                    lambda: self._install_python_dependencies(config),
                ),
                PlanStep(
                    "configuration",
                    "Re-render configuration while preserving installed secrets",
                    lambda: self._write_configuration(config),
                ),
                PlanStep(
                    "services",
                    "Install the release systemd unit definitions",
                    lambda: self._install_services(config),
                ),
                *(
                    [
                        PlanStep(
                            "ingress",
                            "Refresh the controller ingress helper and systemd watcher",
                            lambda: self._install_ingress(
                                config, apply_initial=False
                            ),
                        )
                    ]
                    if config.installs_controller
                    else []
                ),
                PlanStep(
                    "migrations",
                    "Apply explicit database migrations for the new release",
                    lambda: self._run_migrations(config),
                ),
                PlanStep(
                    "restart",
                    "Restart installed services and verify systemd activation",
                    lambda: self._restart_services(config),
                ),
                PlanStep(
                    "state",
                    "Record the successfully applied release",
                    lambda: self._save_state(config),
                ),
                *([migration.commit_step()] if migration else []),
                PlanStep(
                    "cleanup",
                    "Remove releases, runtime images and backups older than the rollback target",
                    lambda: self._safe_cleanup_after_update(config),
                ),
                *([migration.finish_step()] if migration else []),
            ],
            on_failure=(
                migration.undo if migration else lambda: self._rollback_update(config)
            ),
            progress=self.progress,
        )
        if migration is not None:
            self.defer_update_watcher = migration.inside_update_service
        return plan

    def build_uninstall_plan(
        self,
        config: InstallConfig,
        *,
        purge_data: bool = False,
    ) -> InstallPlan:
        steps = [
            PlanStep(
                "stop",
                "Stop and disable AI Factory services",
                lambda: self._stop_services(config, include_database=True),
            ),
            PlanStep(
                "units",
                "Remove AI Factory systemd unit files",
                lambda: self._remove_service_units(config),
            ),
            PlanStep(
                "software",
                "Remove the active application link while preserving configuration and data",
                lambda: self._remove_active_release(config),
            ),
        ]
        if purge_data:
            steps.append(
                PlanStep(
                    "purge",
                    "Permanently remove AI Factory configuration, releases, state, and workspace data",
                    lambda: self._purge_data(config),
                )
            )
        return InstallPlan("Uninstall AI Factory", steps)

    def build_backup_plan(
        self,
        config: InstallConfig,
        *,
        output: Path,
        include_workspaces: bool = False,
    ) -> InstallPlan:
        return InstallPlan(
            "Back up AI Factory",
            [
                PlanStep(
                    "backup",
                    (
                        "Create a verified encrypted-permissions backup of "
                        "configuration, database, and workspace data"
                        if include_workspaces
                        else "Create a verified backup of configuration and database"
                    ),
                    lambda: create_backup(
                        config=config,
                        version=self.release_version,
                        output=output,
                        host_path=self.host_path,
                        runner=self.runner,
                        include_workspaces=include_workspaces,
                    ),
                )
            ],
        )

    def build_restore_plan(
        self,
        config: InstallConfig,
        *,
        archive: Path,
    ) -> InstallPlan:
        return InstallPlan(
            "Restore AI Factory",
            [
                PlanStep(
                    "stop",
                    "Stop AI Factory services before replacing managed state",
                    lambda: self._stop_services(config),
                ),
                PlanStep(
                    "restore",
                    "Verify and restore configuration, database, and included workspace data",
                    lambda: restore_backup(
                        config=config,
                        archive=archive,
                        host_path=self.host_path,
                        runner=self.runner,
                    ),
                ),
                PlanStep(
                    "selinux",
                    "Restore worker storage ownership and SELinux root labels",
                    lambda: self._configure_worker_storage(config)
                    if config.installs_worker
                    else None,
                ),
                PlanStep(
                    "migrations",
                    "Bring the restored database schema to the installed release",
                    lambda: self._run_migrations(config),
                ),
                PlanStep(
                    "start",
                    "Start restored services",
                    lambda: self._restart_services(config),
                ),
            ],
        )

    def status(self, state_root: str = CURRENT.state_root) -> dict:
        state = self.current_state(state_root)
        if state is None:
            return {"installed": False, "services": {}}
        services: dict[str, str] = {}
        role = DeploymentRole(state.role)
        names = []
        if role in {DeploymentRole.CONTROLLER, DeploymentRole.ALL_IN_ONE}:
            names.append("aifactory-controller.service")
            if (
                state.configuration.get("controller_runtime")
                == ControllerRuntime.CONTAINER.value
                and state.configuration.get("database_mode")
                == DatabaseMode.BUNDLED_POSTGRESQL.value
            ):
                names.append("aifactory-postgresql.service")
        if role in {DeploymentRole.WORKER, DeploymentRole.ALL_IN_ONE}:
            names.append("aifactory-worker.service")
        for name in names:
            if self.runner.dry_run:
                services[name] = "unknown"
                continue
            result = self.runner.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                check=False,
            )
            services[name] = result.stdout.strip() or (
                "inactive" if result.returncode else "unknown"
            )
        return {
            "installed": True,
            "role": state.role,
            "version": state.version,
            "installed_at": state.installed_at,
            "updated_at": state.updated_at,
            "configuration": state.configuration,
            "services": services,
        }

    def _validate_config(self, config: InstallConfig) -> None:
        if config.installs_controller:
            self._public_hostname(config.public_url)
        for field_name in (
            "workspace_root",
            "install_root",
            "state_root",
            "releases_root",
            "downloads_root",
        ):
            value = PurePosixPath(
                str(getattr(config, field_name)).replace("\\", "/")
            )
            if not value.is_absolute() or value == PurePosixPath("/"):
                raise InstallerError(f"{field_name} must be a safe absolute path")
            if not any(
                name in part.lower()
                for part in value.parts
                for name in (CURRENT.name, LEGACY.name)
            ):
                raise InstallerError(
                    f"{field_name} must be a dedicated directory whose path contains 'aifactory'"
                )
        path_policies = {
            "install_root": (PurePosixPath("/opt"), PurePosixPath("/var/lib")),
            "state_root": (PurePosixPath("/var/lib"),),
            "releases_root": (PurePosixPath("/var/lib"),),
            "downloads_root": (PurePosixPath("/srv"), PurePosixPath("/var/lib")),
            "workspace_root": (
                PurePosixPath("/var/lib"),
                PurePosixPath("/srv"),
                PurePosixPath("/mnt"),
            ),
        }
        for field_name, allowed_roots in path_policies.items():
            value = PurePosixPath(
                str(getattr(config, field_name)).replace("\\", "/")
            )
            if not any(value == root or root in value.parents for root in allowed_roots):
                raise InstallerError(
                    f"{field_name} is outside the supported managed roots"
                )
        if (
            config.service_user == "root"
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", config.service_user)
        ):
            raise InstallerError(
                "service_user must be a non-root Linux service account name"
            )
        if config.registry_mode == RegistryMode.EXTERNAL:
            if (
                not config.registry_url
                or "://" in config.registry_url
                or any(character.isspace() for character in config.registry_url)
                or not re.fullmatch(
                    r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._-]+)*",
                    config.registry_url,
                )
            ):
                raise InstallerError(
                    "External registry must be an OCI image prefix such as "
                    "registry.example.com/aifactory"
                )
        if config.installs_controller and config.update_source_type == UpdateSourceType.GIT:
            if (
                not config.update_source.strip()
                or any(character in config.update_source for character in "\r\n\0")
            ):
                raise InstallerError("Git update source requires a repository URL or path")
            if (
                not config.update_ref.strip()
                or any(character.isspace() for character in config.update_ref)
            ):
                raise InstallerError("Git update source requires one branch, tag, or commit")
        if config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL:
            if not config.database_url.startswith(
                ("postgresql+asyncpg://", "postgresql://")
            ):
                existing = self._existing_env("controller.env")
                if not existing.get("DATABASE_URL", "").startswith(
                    ("postgresql+asyncpg://", "postgresql://")
                ):
                    raise InstallerError(
                        "External PostgreSQL requires a postgresql+asyncpg:// URL"
                    )
        if config.role == DeploymentRole.WORKER:
            if not config.worker_id:
                existing_worker = self._existing_env("worker.env")
                if not existing_worker.get("AIFACTORY_NODE_ID"):
                    raise InstallerError("Worker installation requires a worker ID")
            if (
                not config.enrollment_token_file
                and not self._existing_env_path("worker.env").is_file()
                and not self.runner.dry_run
            ):
                raise InstallerError(
                    "Worker installation requires an enrollment token file"
                )
        if config.installs_controller and not config.admin_password:
            existing = self._existing_env_path("controller.env")
            if not existing.is_file():
                raise InstallerError(
                    "A new controller installation requires an administrator password"
                )

    def _existing_env_path(self, name: str) -> Path:
        """An installed environment file, before or after the devcloud move."""
        current = self.host_path(f"{CURRENT.etc_dir}/{name}")
        if current.is_file():
            return current
        return self.host_path(f"{LEGACY.etc_dir}/{name}")

    def _existing_env(self, name: str) -> dict[str, str]:
        return {
            current_env_key(key): value
            for key, value in self._read_env(self._existing_env_path(name)).items()
        }

    def _install_packages(self, config: InstallConfig) -> None:
        packages = {
            "gnupg2",
            "python3",
            "python3-pip",
            "policycoreutils-python-utils",
            "subscription-manager",
            *host_python_packages(self._host_platform()),
        }
        if config.installs_controller:
            packages.update({"nginx", "curl", "createrepo_c", "skopeo"})
            if config.update_source_type == UpdateSourceType.GIT:
                packages.add("git")
        if config.containerized_controller:
            packages.update({"podman", "crun", "tar"})
        if config.installs_worker:
            packages.update({"podman", "crun", "tar", "gzip"})
        if (
            config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
            and not config.containerized_controller
        ):
            packages.add("postgresql-server")
        elif (
            config.installs_controller
            and config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL
        ):
            packages.add("postgresql")
        result = self.runner.run(
            ["dnf", "install", "-y", *sorted(packages)],
            check=False,
        )
        if result.returncode == 0 or self.runner.dry_run:
            return
        raise InstallerError(
            "Configured repositories could not install the required packages. "
            "Register this VM with Satellite/Foreman, confirm its repositories "
            "are enabled and reachable, and rerun setup."
        )

    def _configure_database(self, config: InstallConfig) -> None:
        if (
            not config.installs_controller
            or config.database_mode != DatabaseMode.BUNDLED_POSTGRESQL
        ):
            return
        if config.containerized_controller:
            env_path = self.host_path("/etc/aifactory/postgresql.env")
            existing = self._read_env(env_path)
            password = existing.get("POSTGRESQL_PASSWORD", "") or secrets.token_urlsafe(36)
            self._write_env(
                env_path,
                {
                    "POSTGRESQL_USER": "aifactory",
                    "POSTGRESQL_PASSWORD": password,
                    "POSTGRESQL_DATABASE": "aifactory",
                },
                podman=True,
            )
            config.database_url = (
                "postgresql+asyncpg://aifactory:"
                f"{quote(password, safe='')}@aifactory-postgresql/aifactory"
            )
            return
        pg_version = self.host_path("/var/lib/pgsql/data/PG_VERSION")
        if self.runner.dry_run or not pg_version.is_file():
            self.runner.run(["postgresql-setup", "--initdb"])
        self.runner.run(["systemctl", "enable", "--now", "postgresql.service"])
        role = self.runner.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-tAc",
                "SELECT 1 FROM pg_roles WHERE rolname='aifactory'",
            ],
            capture_output=True,
            check=False,
        )
        if self.runner.dry_run or role.returncode != 0 or role.stdout.strip() != "1":
            self.runner.run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "createuser",
                    "--login",
                    "aifactory",
                ]
            )
        database = self.runner.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-tAc",
                "SELECT 1 FROM pg_database WHERE datname='aifactory'",
            ],
            capture_output=True,
            check=False,
        )
        if (
            self.runner.dry_run
            or database.returncode != 0
            or database.stdout.strip() != "1"
        ):
            self.runner.run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "createdb",
                    "--owner",
                    "aifactory",
                    "aifactory",
                ]
            )

    def _ensure_service_user(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--home-dir",
                    "/var/lib/aifactory",
                    "--shell",
                    "/sbin/nologin",
                    config.service_user,
                ]
            )
            if config.installs_worker and not config.containerized_worker:
                self.runner.run(
                    [
                        "usermod",
                        "--add-subuids",
                        "100000-165535",
                        "--add-subgids",
                        "100000-165535",
                        config.service_user,
                    ]
                )
            return
        result = self.runner.run(
            ["id", "-u", config.service_user],
            capture_output=True,
            check=False,
        ) if self.runner.exists("id") else None
        if not result or result.returncode != 0:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--home-dir",
                    "/var/lib/aifactory",
                    "--shell",
                    "/sbin/nologin",
                    config.service_user,
                ]
            )
        if config.installs_worker and not config.containerized_worker:
            self._ensure_worker_subids(config.service_user)

    def _ensure_worker_subids(self, service_user: str) -> None:
        """Give a system account the mappings required by rootless Podman."""
        subuid = self.host_path("/etc/subuid")
        subgid = self.host_path("/etc/subgid")

        def entry(path: Path) -> tuple[int, int] | None:
            if not path.is_file():
                return None
            for raw in path.read_text(encoding="utf-8").splitlines():
                fields = raw.split(":")
                if len(fields) != 3 or fields[0] != service_user:
                    continue
                try:
                    return int(fields[1]), int(fields[2])
                except ValueError:
                    continue
            return None

        def free_range(path: Path) -> tuple[int, int]:
            candidate = 100000
            length = 65536
            ranges: list[tuple[int, int]] = []
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    fields = raw.split(":")
                    if len(fields) != 3:
                        continue
                    try:
                        start, count = int(fields[1]), int(fields[2])
                    except ValueError:
                        continue
                    ranges.append((start, start + count - 1))
            for start, end in sorted(ranges):
                if candidate + length - 1 < start:
                    break
                if candidate <= end:
                    candidate = end + 1
            return candidate, candidate + length - 1

        uid_entry = entry(subuid)
        gid_entry = entry(subgid)
        command = ["usermod"]
        if uid_entry is None:
            start, end = free_range(subuid)
            command.extend(["--add-subuids", f"{start}-{end}"])
        if gid_entry is None:
            start, end = free_range(subgid)
            command.extend(["--add-subgids", f"{start}-{end}"])
        if len(command) > 1:
            command.append(service_user)
            self.runner.run(command)

    def _ensure_directories(self, config: InstallConfig) -> None:
        owned = [
            "/var/lib/aifactory",
            config.install_root,
            config.state_root,
            config.releases_root,
            "/var/lib/aifactory/database",
            "/var/lib/aifactory/download-builds",
            "/var/lib/aifactory/update-queue",
            "/var/lib/aifactory/update-queue/uploads",
        ]
        if config.installs_worker:
            owned.append(config.workspace_root)
        if config.installs_controller:
            owned.append(config.downloads_root)
        if config.containerized_controller:
            owned.append("/var/lib/aifactory/ingress")
            if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
                owned.append("/var/lib/aifactory/postgresql")
        if self.runner.dry_run:
            for value in owned:
                self.runner.run(
                    [
                        "install",
                        "-d",
                        "-o",
                        config.service_user,
                        "-g",
                        config.service_user,
                        "-m",
                        "0750",
                        value,
                    ]
                )
            self.runner.run(
                [
                    "install",
                    "-d",
                    "-o",
                    "root",
                    "-g",
                    config.service_user,
                    "-m",
                    "0750",
                    "/etc/aifactory",
                ]
            )
            if config.installs_worker:
                self.runner.run(
                    [
                        "install",
                        "-d",
                        "-o",
                        "root",
                        "-g",
                        config.service_user,
                        "-m",
                        "0750",
                        "/etc/aifactory/pki",
                    ]
                )
            return
        for value in owned:
            path = self.host_path(value)
            path.mkdir(parents=True, exist_ok=True)
            if not self.runner.dry_run:
                self.runner.run(
                    ["chown", f"{config.service_user}:{config.service_user}", str(path)]
                )
        etc_dir = self.host_path("/etc/aifactory")
        etc_dir.mkdir(parents=True, exist_ok=True)
        if not self.runner.dry_run:
            self.runner.run(
                ["chown", f"root:{config.service_user}", str(etc_dir)]
            )
            os.chmod(etc_dir, 0o750)
        if config.installs_worker:
            pki_dir = etc_dir / "pki"
            pki_dir.mkdir(parents=True, exist_ok=True)
            self.runner.run(
                ["chown", f"root:{config.service_user}", str(pki_dir)]
            )
            os.chmod(pki_dir, 0o750)
        if config.containerized_controller:
            controller_owned = [
                "/var/lib/aifactory/database",
                "/var/lib/aifactory/download-builds",
                "/var/lib/aifactory/ingress",
                "/var/lib/aifactory/update-queue",
                "/var/lib/aifactory/update-queue/uploads",
                config.downloads_root,
            ]
            for value in controller_owned:
                self.runner.run(["chown", "10001:10001", str(self.host_path(value))])
            if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
                self.runner.run(
                    [
                        "chown",
                        "26:26",
                        str(self.host_path("/var/lib/aifactory/postgresql")),
                    ]
                )

    def _install_release_keyring(self, release_root: Path) -> None:
        bundled_keyring = release_root / "release-keyring.gpg"
        if not bundled_keyring.is_file() or bundled_keyring.is_symlink():
            return
        etc_dir = self.host_path("/etc/aifactory")
        etc_dir.mkdir(parents=True, exist_ok=True)
        keyring_target = etc_dir / "release-keyring.gpg"
        temporary_keyring = etc_dir / f".release-keyring-{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(bundled_keyring, temporary_keyring)
            os.chmod(temporary_keyring, 0o644)
            os.replace(temporary_keyring, keyring_target)
        finally:
            temporary_keyring.unlink(missing_ok=True)

    def _install_release(self, config: InstallConfig) -> None:
        target = self.host_path(config.releases_root) / self.release_id
        if self.runner.dry_run:
            self.runner.run(
                [
                    "aifactory-internal-install-release",
                    str(self.project_root),
                    str(target),
                ]
            )
            return
        marker_name = RELEASE_MARKERS[0]
        if target.exists():
            marker = target / marker_name
            if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != self.release_id:
                raise InstallerError(
                    f"Release target exists without a valid identity marker: {target}"
                )
        else:
            temporary_release = target.with_name(
                f".{target.name}.staging-{uuid.uuid4().hex}"
            )
            try:
                shutil.copytree(
                    self.project_root,
                    temporary_release,
                    symlinks=False,
                    ignore=shutil.ignore_patterns(
                        ".git",
                        ".venv",
                        ".pytest_cache",
                        ".pytest_tmp",
                        "__pycache__",
                        "*.pyc",
                        "dist",
                    ),
                )
                (temporary_release / marker_name).write_text(
                    self.release_id + "\n", encoding="utf-8"
                )
                os.replace(temporary_release, target)
            finally:
                if temporary_release.exists():
                    shutil.rmtree(temporary_release)
        self._install_release_keyring(target)
        install_root = self.host_path(config.install_root)
        install_root.mkdir(parents=True, exist_ok=True)
        current = install_root / "current"
        if current.is_symlink():
            try:
                self.previous_release = current.resolve(strict=True)
            except FileNotFoundError:
                self.previous_release = None
        temporary = install_root / f".current-{uuid.uuid4().hex}"
        relative_target = os.path.relpath(target, start=install_root)
        temporary.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary, current)

    def _rollback_release(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(["aifactory-internal-rollback-release"])
            return
        if self.migration_applied or self.previous_release is None:
            return
        install_root = self.host_path(config.install_root)
        current = install_root / "current"
        temporary = install_root / f".rollback-{uuid.uuid4().hex}"
        relative_target = os.path.relpath(self.previous_release, start=install_root)
        temporary.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary, current)

    def _check_update_disk_space(self, config: InstallConfig) -> None:
        """Fail before changing anything when the release cannot be staged."""
        if self.runner.dry_run:
            return
        releases_root = self.host_path(config.releases_root)
        if (releases_root / self.release_id).exists():
            return
        needed = _tree_size(self.project_root) + UPDATE_FREE_MARGIN_BYTES
        free = _free_bytes(releases_root)
        if free < needed:
            raise InstallerError(
                f"Not enough free disk space for the update in {releases_root}: about "
                f"{needed / 1024 ** 3:.1f} GiB needed, {free / 1024 ** 3:.1f} GiB free. "
                "Remove old backups (/var/lib/aifactory/backups) or unused images "
                "(podman image prune) and retry."
            )

    def _safe_cleanup_after_update(self, config: InstallConfig) -> None:
        # Runs after the state is saved: an exception here would roll back an
        # applied update.
        try:
            self._cleanup_after_update(config)
        except Exception as exc:  # noqa: BLE001
            self._report_detail(f"Eski sürüm temizliği atlandı: {exc}")

    def _cleanup_after_update(self, config: InstallConfig) -> None:
        """Keep the current and previous release; remove older copies.

        Best effort: a failed cleanup never fails an applied update.
        """
        if self.runner.dry_run:
            return
        releases_root = self.host_path(config.releases_root)
        keep_paths = {(releases_root / self.release_id).resolve()}
        if self.previous_release is not None:
            keep_paths.add(self.previous_release.resolve())
        keep_versions = {self.release_version}
        if self.previous_release is not None:
            try:
                keep_versions.add(InstallerEngine(project_root=self.previous_release).release_version)
            except InstallerError:
                pass
        removed = []
        if releases_root.is_dir():
            staged = [
                path for path in releases_root.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and any((path / marker).is_file() for marker in RELEASE_MARKERS)
            ]
            staged.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            survivors = [path for path in staged if path.resolve() in keep_paths]
            for path in staged:
                if path.resolve() in keep_paths:
                    continue
                if len(survivors) < KEEP_RELEASES:
                    survivors.append(path)
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name)
            for leftover in releases_root.glob(".*.staging-*"):
                shutil.rmtree(leftover, ignore_errors=True)
        backups = self.host_path("/var/lib/aifactory/backups")
        if backups.is_dir():
            archives = sorted(
                backups.glob("pre-update-v*.tar.gz"), key=lambda path: path.stat().st_mtime, reverse=True
            )
            for path in archives[KEEP_BACKUPS:]:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        if self.runner.exists("podman"):
            for repository in (
                "localhost/aifactory-controller",
                "localhost/aifactory-worker",
                "localhost/devcloud-controller",
                "localhost/devcloud-worker",
            ):
                listing = self.runner.run(
                    ["podman", "images", "--format", "{{.Tag}}", repository],
                    capture_output=True,
                    check=False,
                )
                for tag in (listing.stdout or "").split():
                    if tag in keep_versions or tag in ("<none>", "latest"):
                        continue
                    result = self.runner.run(
                        ["podman", "image", "rm", f"{repository}:{tag}"], check=False
                    )
                    if result.returncode == 0:
                        removed.append(f"{repository}:{tag}")
        if removed:
            self._report_detail(f"{len(removed)} eski öğe temizlendi")

    def _report_detail(self, text: str) -> None:
        progress = getattr(self, "progress", None)
        if progress is not None:
            progress.detail(text)

    def _rollback_update(self, config: InstallConfig) -> None:
        """Restore the previous release link and its rendered service units."""
        previous = self.previous_release
        self._rollback_release(config)
        if self.runner.dry_run or self.migration_applied or previous is None:
            return
        previous_engine = InstallerEngine(
            project_root=previous,
            filesystem_root=self.filesystem_root,
            runner=self.runner,
        )
        previous_engine._install_services(config)
        previous_engine._install_ingress(config, apply_initial=False)
        previous_engine._restart_services(config)

    def _install_python_dependencies(self, config: InstallConfig) -> None:
        if config.fully_containerized:
            return
        release = self.host_path(config.releases_root) / self.release_id
        venv = release / ".venv"
        if not (venv / "bin" / "python").exists():
            previous = self.previous_release
            current_requirements = release / "requirements.txt"
            previous_requirements = previous / "requirements.txt" if previous else None
            wheels = release / "offline" / "wheels"
            can_reuse = (
                previous is not None
                and (previous / ".venv" / "bin" / "python").exists()
                and previous_requirements is not None
                and previous_requirements.is_file()
                and current_requirements.is_file()
                and hashlib.sha256(previous_requirements.read_bytes()).digest()
                == hashlib.sha256(current_requirements.read_bytes()).digest()
                and not (wheels.is_dir() and any(wheels.glob("*.whl")))
            )
            if can_reuse and not self.runner.dry_run:
                venv.symlink_to(previous / ".venv", target_is_directory=True)
                return
            self.runner.run(
                [host_python(self._host_platform()), "-m", "venv", str(venv)]
            )
        pip = str(venv / "bin" / "python")
        wheels = release / "offline" / "wheels"
        command = [pip, "-m", "pip", "install", "--disable-pip-version-check"]
        if wheels.is_dir() and any(wheels.glob("*.whl")):
            command.extend(["--no-index", "--find-links", str(wheels)])
        command.extend(["-r", str(release / "requirements.txt")])
        self.runner.run(command)

    @staticmethod
    def _quote_env(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _podman_env(value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise InstallerError("Podman environment values cannot contain newlines")
        return value

    def _read_env(self, path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.is_file():
            return values
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw or raw.lstrip().startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            raw_value = value.strip()
            if raw_value.startswith('"') and raw_value.endswith('"'):
                try:
                    values[key] = str(json.loads(raw_value))
                    continue
                except json.JSONDecodeError:
                    pass
            values[key] = raw_value.strip('"')
        return values

    def _write_env(
        self,
        path: Path,
        values: dict[str, str],
        *,
        podman: bool = False,
    ) -> None:
        formatter = self._podman_env if podman else self._quote_env
        payload = "".join(
            f"{key}={formatter(str(value))}\n"
            for key, value in sorted(values.items())
            if value != ""
        )
        self._atomic_write(path, payload.encode("utf-8"), 0o600)

    def _reconcile_worker_fallback_host(
        self,
        controller_url: str,
        fallback_ipv4: str,
    ) -> None:
        """Rotate or remove a bootstrap-managed hosts entry without forcing fallback."""
        hosts_path = self.host_path("/etc/hosts")
        if not hosts_path.is_file() or self.runner.dry_run:
            return
        marker = "# aifactory-controller-fallback"
        markers = (marker, "# devcloud-controller-fallback")
        original = hosts_path.read_text(encoding="utf-8")
        lines = original.splitlines()
        had_managed_entry = any(
            item in line for line in lines for item in markers
        )
        if not had_managed_entry:
            return
        retained = [
            line for line in lines if not any(item in line for item in markers)
        ]
        hostname = urlsplit(controller_url).hostname or ""
        if fallback_ipv4 and hostname:
            retained.append(
                f"{fallback_ipv4}\t{hostname}\t{marker} {hostname}"
            )
        payload = "\n".join(retained) + "\n"
        if payload != original:
            self._atomic_write(
                hosts_path,
                payload.encode("utf-8"),
                stat.S_IMODE(hosts_path.stat().st_mode),
            )

    def _write_configuration(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "aifactory-internal-write-config",
                    config.role.value,
                    "/etc/aifactory",
                ]
            )
            return
        etc_dir = self.host_path("/etc/aifactory")
        etc_dir.mkdir(parents=True, exist_ok=True)
        controller_env_path = etc_dir / "controller.env"
        worker_env_path = etc_dir / "worker.env"
        existing_worker = self._read_env(worker_env_path)

        local_worker_id = ""
        local_worker_token = ""
        if config.role == DeploymentRole.ALL_IN_ONE:
            local_worker_id = existing_worker.get(
                "AIFACTORY_NODE_ID", ""
            ) or str(uuid.uuid4())
            local_worker_token = existing_worker.get(
                "AIFACTORY_NODE_TOKEN", ""
            ) or secrets.token_urlsafe(32)

        if config.installs_controller:
            existing = self._read_env(controller_env_path)
            if (
                config.containerized_controller
                and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
                and not config.database_url
            ):
                postgresql = self._read_env(etc_dir / "postgresql.env")
                password = postgresql.get("POSTGRESQL_PASSWORD", "")
                if not password:
                    raise InstallerError(
                        "Managed PostgreSQL credentials are missing from "
                        "/etc/aifactory/postgresql.env"
                    )
                config.database_url = (
                    "postgresql+asyncpg://aifactory:"
                    f"{quote(password, safe='')}@aifactory-postgresql/aifactory"
                )
            secret_key = existing.get("SECRET_KEY", "") or secrets.token_urlsafe(48)
            admin_password = (
                existing.get("ADMIN_PASSWORD", "")
                or config.admin_password
            )
            controller_values = {
                "ENV": "production",
                "DEBUG": "False",
                "SECRET_KEY": secret_key,
                "COOKIE_SECURE": "True" if config.public_url.startswith("https://") else "False",
                "DATABASE_URL": (
                    (
                        "postgresql+asyncpg://"
                        + existing.get("DATABASE_URL", "")[len("postgresql://") :]
                        if existing.get("DATABASE_URL", "").startswith(
                            "postgresql://"
                        )
                        else existing.get("DATABASE_URL", "")
                    )
                    if config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL
                    and not config.database_url
                    else config.effective_database_url()
                ),
                "AUTO_MIGRATE": "False",
                "STORAGE_ROOT": config.workspace_root,
                "DOWNLOADS_ROOT": config.downloads_root,
                "DOWNLOAD_BUILD_ROOT": "/var/lib/aifactory/download-builds",
                "DOWNLOAD_PUBLIC_BASE_URL": config.public_url,
                "HTTPS_DEFAULT_HOSTNAME": self._public_hostname(config.public_url),
                "ADMIN_USERNAME": config.admin_username,
                "ADMIN_EMAIL": config.admin_email,
                "ADMIN_PASSWORD": admin_password,
                "AIFACTORY_DEPLOYMENT_ROLE": config.role.value,
                "AIFACTORY_REGISTRY_MODE": config.registry_mode.value,
                "AIFACTORY_REGISTRY_URL": config.registry_url,
                "UPDATE_SOURCE_TYPE": config.update_source_type.value,
                "UPDATE_SOURCE": config.update_source,
                "UPDATE_REF": config.update_ref,
            }
            if "WORKER_OTA_ALLOW_UNSIGNED" in existing:
                controller_values["WORKER_OTA_ALLOW_UNSIGNED"] = existing[
                    "WORKER_OTA_ALLOW_UNSIGNED"
                ]
            if config.containerized_controller:
                controller_values.update(
                    {
                        "UPDATES_ENABLED": "True",
                        "DOWNLOAD_UPDATES_ENABLED": "False",
                    }
                )
            if local_worker_id:
                controller_values.update(
                    {
                        "AIFACTORY_BOOTSTRAP_WORKER_ID": local_worker_id,
                        "AIFACTORY_BOOTSTRAP_WORKER_NAME": config.worker_name,
                        "AIFACTORY_BOOTSTRAP_WORKER_TOKEN_HASH": hashlib.sha256(
                            local_worker_token.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            self._write_env(
                controller_env_path,
                controller_values,
                podman=config.containerized_controller,
            )

        if config.installs_worker:
            if config.role == DeploymentRole.WORKER:
                token_path = (
                    Path(config.enrollment_token_file)
                    if config.enrollment_token_file
                    else None
                )
                if (
                    token_path is not None
                    and not token_path.is_file()
                    and not self.runner.dry_run
                ):
                    raise InstallerError(
                        f"Enrollment token file does not exist: {token_path}"
                    )
                token = (
                    token_path.read_text(encoding="utf-8").strip()
                    if token_path is not None and token_path.is_file()
                    else existing_worker.get("AIFACTORY_NODE_TOKEN", "")
                    or "dry-run-token"
                )
                worker_id = (
                    config.worker_id
                    or existing_worker.get("AIFACTORY_NODE_ID", "")
                )
                if token_path is not None:
                    controller_url = (
                        config.controller_url
                        or existing_worker.get("AIFACTORY_CONTROLLER_URL", "")
                        or existing_worker.get("AIFACTORY_MASTER_URL", "")
                    )
                else:
                    # Repair/update must preserve the enrolled worker's live
                    # endpoint instead of reverting to an older state default.
                    controller_url = (
                        existing_worker.get("AIFACTORY_CONTROLLER_URL", "")
                        or existing_worker.get("AIFACTORY_MASTER_URL", "")
                        or config.controller_url
                    )
                if token_path is not None:
                    controller_fallback_ipv4 = (
                        config.controller_fallback_ipv4
                        or existing_worker.get("AIFACTORY_CONTROLLER_FALLBACK_IPV4", "")
                    )
                    agent_ca_file = (
                        config.agent_ca_file
                        or existing_worker.get("AIFACTORY_AGENT_CA_FILE", "")
                    )
                else:
                    controller_fallback_ipv4 = (
                        existing_worker.get("AIFACTORY_CONTROLLER_FALLBACK_IPV4", "")
                        or config.controller_fallback_ipv4
                    )
                    agent_ca_file = (
                        existing_worker.get("AIFACTORY_AGENT_CA_FILE", "")
                        or config.agent_ca_file
                    )
            else:
                token = local_worker_token
                worker_id = local_worker_id
                controller_url = "http://127.0.0.1:8000"
                controller_fallback_ipv4 = ""
                agent_ca_file = ""
            if not token or any(character.isspace() for character in token):
                raise InstallerError("Enrollment token is empty or contains whitespace")

            if config.role == DeploymentRole.WORKER:
                parsed_controller = urlsplit(controller_url)
                if (
                    parsed_controller.scheme != "https"
                    or not parsed_controller.hostname
                    or parsed_controller.username
                    or parsed_controller.password
                    or parsed_controller.path not in {"", "/"}
                    or parsed_controller.query
                    or parsed_controller.fragment
                ):
                    raise InstallerError(
                        "Remote workers require a canonical https:// Controller FQDN"
                    )
                try:
                    ipaddress.ip_address(parsed_controller.hostname)
                except ValueError:
                    pass
                else:
                    raise InstallerError(
                        "Remote worker Controller URL must use the certificate FQDN, not an IP"
                    )

            if controller_fallback_ipv4:
                try:
                    fallback_address = ipaddress.ip_address(controller_fallback_ipv4)
                except ValueError as exc:
                    raise InstallerError(
                        "Worker controller fallback must be a valid IPv4 address"
                    ) from exc
                if (
                    fallback_address.version != 4
                    or fallback_address.is_unspecified
                    or fallback_address.is_multicast
                    or fallback_address.is_loopback
                ):
                    raise InstallerError(
                        "Worker controller fallback must be a usable IPv4 address"
                    )
                controller_fallback_ipv4 = str(fallback_address)

            tls_paths = {
                "AIFACTORY_AGENT_CA_FILE": agent_ca_file,
                "AIFACTORY_AGENT_CERT_FILE": existing_worker.get(
                    "AIFACTORY_AGENT_CERT_FILE", ""
                ),
                "AIFACTORY_AGENT_KEY_FILE": existing_worker.get(
                    "AIFACTORY_AGENT_KEY_FILE", ""
                ),
            }
            if bool(tls_paths["AIFACTORY_AGENT_CERT_FILE"]) != bool(
                tls_paths["AIFACTORY_AGENT_KEY_FILE"]
            ):
                raise InstallerError(
                    "Worker client certificate and key must be configured together"
                )
            for key, path_value in tls_paths.items():
                if not path_value:
                    continue
                path = PurePosixPath(path_value)
                if not path.is_absolute():
                    raise InstallerError(f"{key} must contain an absolute path")
                if config.containerized_worker and not path.is_relative_to(
                    PurePosixPath("/etc/aifactory")
                ):
                    raise InstallerError(
                        f"{key} must be under /etc/aifactory for the container worker"
                    )
                host_tls_path = self.host_path(path_value)
                if not self.runner.dry_run and not host_tls_path.is_file():
                    raise InstallerError(f"{key} file does not exist: {path_value}")
                if self.runner.dry_run:
                    continue
                managed_tls_path = path.is_relative_to(PurePosixPath("/etc/aifactory"))
                if managed_tls_path:
                    self.runner.run(
                        [
                            "chown",
                            f"root:{config.service_user}",
                            str(host_tls_path),
                        ]
                    )
                    os.chmod(host_tls_path, 0o640)
                elif not config.containerized_worker:
                    readable = self.runner.run(
                        [
                            "runuser",
                            "-u",
                            config.service_user,
                            "--",
                            "test",
                            "-r",
                            str(host_tls_path),
                        ],
                        check=False,
                    )
                    if readable.returncode != 0:
                        raise InstallerError(
                            f"{key} is not readable by {config.service_user}: {path_value}"
                        )

            worker_values = {
                "AIFACTORY_CONTROLLER_URL": controller_url,
                "AIFACTORY_MASTER_URL": controller_url,
                "AIFACTORY_CONTROLLER_FALLBACK_IPV4": controller_fallback_ipv4,
                "AIFACTORY_AGENT_CA_FILE": agent_ca_file,
                "AIFACTORY_AGENT_CERT_FILE": tls_paths["AIFACTORY_AGENT_CERT_FILE"],
                "AIFACTORY_AGENT_KEY_FILE": tls_paths["AIFACTORY_AGENT_KEY_FILE"],
                "AIFACTORY_NODE_ID": worker_id,
                "AIFACTORY_NODE_TOKEN": token,
                "AIFACTORY_WORKER_NAME": config.worker_name,
                "STORAGE_ROOT": config.workspace_root,
            }
            # Preserve operator-managed Jupyter AI settings across repair and
            # update runs. worker.env remains mode 0600.
            for key in (
                "JUPYTER_AI_GATEWAY_URL",
                "JUPYTER_AI_MODEL",
                "JUPYTER_AI_GATEWAY_TOKEN",
                "JUPYTER_AI_GATEWAY_MODEL_DISCOVERY",
                "JUPYTER_AI_MODEL_CATALOG_JSON",
            ):
                if existing_worker.get(key):
                    worker_values[key] = existing_worker[key]
            self._write_env(
                worker_env_path,
                worker_values,
                podman=config.containerized_worker,
            )
            if config.role == DeploymentRole.WORKER:
                self._reconcile_worker_fallback_host(
                    controller_url,
                    controller_fallback_ipv4,
                )
            config.controller_url = controller_url
            config.controller_fallback_ipv4 = controller_fallback_ipv4
            config.agent_ca_file = agent_ca_file

    def _run_migrations(self, config: InstallConfig) -> None:
        if not config.installs_controller:
            return
        if config.containerized_controller:
            # The immutable image applies migrations immediately before
            # Uvicorn starts, against the same network and credentials.
            return
        release = self.host_path(config.install_root) / "current"
        python = release / ".venv" / "bin" / "python"
        controller_env = self._read_env(
            self.host_path("/etc/aifactory/controller.env")
        )
        self.runner.run(
            [
                "runuser",
                "-u",
                config.service_user,
                "--",
                str(python),
                "-m",
                "app.migrations",
                "upgrade",
            ],
            cwd=release,
            env=controller_env,
        )
        self.migration_applied = True

    def _controller_image(self) -> str:
        return f"localhost/aifactory-controller:{self.release_version}"

    def _controller_source_image(self) -> str:
        return os.environ.get(
            "AIFACTORY_CONTROLLER_SOURCE_IMAGE",
            f"ghcr.io/aydinguven/devcloud:controller-{self.release_version}",
        )

    def _worker_image(self) -> str:
        return f"localhost/aifactory-worker:{self.release_version}"

    def _worker_source_image(self) -> str:
        return os.environ.get(
            "AIFACTORY_WORKER_SOURCE_IMAGE",
            f"ghcr.io/aydinguven/devcloud:worker-{self.release_version}",
        )

    @staticmethod
    def _normalize_image_id(value: str) -> str:
        image_id = value.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", image_id):
            return f"sha256:{image_id}"
        return image_id

    def _verify_platform_image(
        self,
        config: InstallConfig,
        *,
        role: str,
        image: str,
    ) -> None:
        release = self.host_path(config.install_root) / "current"
        if not (release / "platform-release.json").is_file():
            return
        manifest = load_platform_release(release)
        expected = (
            manifest.controller.digest
            if role == "controller"
            else manifest.worker.digest
        )
        inspected = self.runner.run(
            ["podman", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
        )
        actual = self._normalize_image_id(inspected.stdout)
        if not self.runner.dry_run and actual != expected:
            raise InstallerError(
                f"Loaded {role} image identity does not match the signed platform manifest"
            )

    @staticmethod
    def _postgresql_image() -> str:
        return "localhost/aifactory-postgresql:16"

    def _bundled_postgresql_ip(self) -> str:
        """Return a fixed address for the bundled database on the aifactory network.

        The controller reaches PostgreSQL through an /etc/hosts entry pinned to
        this address, so database access never depends on aardvark-dns (which
        silently stops answering when firewalld drops netavark's runtime rules).
        An empty result keeps the previous DNS-only behaviour.
        """
        exists = self.runner.run(
            ["podman", "network", "exists", "aifactory"], check=False
        )
        if exists.returncode != 0:
            # Let netavark pick a free subnet exactly as Quadlet would; the
            # Quadlet network unit later adopts it via `network create --ignore`.
            self.runner.run(["podman", "network", "create", "aifactory"], check=False)
        inspect = self.runner.run(
            [
                "podman",
                "network",
                "inspect",
                "aifactory",
                "--format",
                "{{range .Subnets}}{{.Subnet}} {{end}}",
            ],
            capture_output=True,
            check=False,
        )
        for candidate in (inspect.stdout or "").split():
            try:
                subnet = ipaddress.ip_network(candidate, strict=False)
            except ValueError:
                continue
            if subnet.version != 4 or subnet.num_addresses < 16:
                continue
            # Stay clear of the gateway (.1) and of IPAM's low dynamic range.
            return str(subnet.broadcast_address - 5)
        return ""

    def _adopt_legacy_image(self, image: str, legacy: str) -> bool:
        """Tag an image that a devcloud release stored under its old name."""
        if self.runner.dry_run:
            return False
        exists = self.runner.run(["podman", "image", "exists", legacy], check=False)
        if exists.returncode != 0:
            return False
        self.runner.run(["podman", "tag", legacy, image])
        return True

    @staticmethod
    def _postgresql_source_image() -> str:
        return os.environ.get(
            "AIFACTORY_POSTGRES_SOURCE_IMAGE",
            "quay.io/sclorg/postgresql-16-c10s:latest",
        )

    def _prepare_controller_images(
        self,
        config: InstallConfig,
        *,
        refresh: bool = False,
    ) -> None:
        if not config.containerized_controller:
            return
        image_root = (
            self.host_path(config.install_root)
            / "current"
            / "offline"
            / "controller-images"
        )
        archives = sorted(image_root.glob("*.tar")) if image_root.is_dir() else []
        if archives:
            for archive in archives:
                self.runner.run(["podman", "load", "-i", str(archive)])
        else:
            controller_image = self._controller_image()
            exists = self.runner.run(
                ["podman", "image", "exists", controller_image],
                check=False,
            )
            if exists.returncode != 0 or refresh or self.runner.dry_run:
                source = self._controller_source_image()
                self.runner.run(["podman", "pull", source])
                self.runner.run(["podman", "tag", source, controller_image])
        required = [self._controller_image()]
        if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
            required.append(self._postgresql_image())
        for image in required:
            result = self.runner.run(
                ["podman", "image", "exists", image],
                check=False,
            )
            if result.returncode == 0 and not self.runner.dry_run:
                continue
            if image == self._postgresql_image() and self._adopt_legacy_image(
                image, "localhost/devcloud-postgresql:16"
            ):
                continue
            if image == self._postgresql_image() and not archives:
                source = self._postgresql_source_image()
                self.runner.run(["podman", "pull", source])
                self.runner.run(["podman", "tag", source, image])
                continue
            if not self.runner.dry_run:
                raise InstallerError(
                    f"Required controller container image is missing: {image}"
                )
        self._verify_platform_image(
            config,
            role="controller",
            image=self._controller_image(),
        )

    def _prepare_worker_image(
        self,
        config: InstallConfig,
        *,
        refresh: bool = False,
    ) -> None:
        if not config.containerized_worker:
            return
        image_root = (
            self.host_path(config.install_root)
            / "current"
            / "offline"
            / "worker-images"
        )
        archives = sorted(image_root.glob("*.tar")) if image_root.is_dir() else []
        for archive in archives:
            self.runner.run(["podman", "load", "-i", str(archive)])
        worker_image = self._worker_image()
        exists = self.runner.run(
            ["podman", "image", "exists", worker_image],
            check=False,
        )
        if archives:
            if exists.returncode != 0 and not self.runner.dry_run:
                raise InstallerError(
                    f"Required worker container image is missing after loading archives: {worker_image}"
                )
        elif exists.returncode != 0 or refresh or self.runner.dry_run:
            source = self._worker_source_image()
            self.runner.run(["podman", "pull", source])
            self.runner.run(["podman", "tag", source, worker_image])
        self._verify_platform_image(config, role="worker", image=worker_image)

    def _install_services(self, config: InstallConfig) -> None:
        release = self.host_path(config.install_root) / "current"
        systemd_dir = self.host_path("/etc/systemd/system")
        if not self.runner.dry_run:
            systemd_dir.mkdir(parents=True, exist_ok=True)
        units: list[tuple[str, str]] = [
            ("aifactory-update.service", "aifactory-update.service"),
            ("aifactory-update.path", "aifactory-update.path"),
        ]
        if config.installs_controller and not config.containerized_controller:
            units.append(("aifactory.service", "aifactory-controller.service"))
        if config.installs_worker and not config.containerized_worker:
            units.append(("aifactory-worker.service", "aifactory-worker.service"))
        python = host_python(self._host_platform())
        service_uid = (
            self._service_uid(config)
            if config.installs_worker and not config.containerized_worker
            else ""
        )
        for source_name, target_name in units:
            template = (self.project_root / "deploy" / source_name).read_text(
                encoding="utf-8"
            )
            rendered = (
                template.replace("{{USER}}", config.service_user)
                .replace("{{UID}}", service_uid)
                .replace("{{PROJECT_DIR}}", str(release))
                .replace("{{PYTHON}}", python)
            )
            if self.runner.dry_run:
                self.runner.run(
                    [
                        "install",
                        "-m",
                        "0644",
                        str(self.project_root / "deploy" / source_name),
                        str(systemd_dir / target_name),
                    ]
                )
            else:
                self._atomic_write(
                    systemd_dir / target_name,
                    rendered.encode("utf-8"),
                    0o644,
                )
        if config.containerized_controller or config.containerized_worker:
            quadlet_dir = self.host_path("/etc/containers/systemd")
            if not self.runner.dry_run:
                quadlet_dir.mkdir(parents=True, exist_ok=True)
            quadlets: list[tuple[str, str]] = []
            if config.containerized_controller:
                quadlets.extend(
                    [
                        ("aifactory.network", "aifactory.network"),
                        ("aifactory-controller.container", "aifactory-controller.container"),
                    ]
                )
            if (
                config.containerized_controller
                and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
            ):
                quadlets.append(
                    ("aifactory-postgresql.container", "aifactory-postgresql.container")
                )
            if config.containerized_worker:
                quadlets.append(
                    ("aifactory-worker.container", "aifactory-worker.container")
                )
            database_ip = (
                self._bundled_postgresql_ip()
                if config.containerized_controller
                and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
                else ""
            )
            for source_name, target_name in quadlets:
                source = (
                    self.project_root
                    / "deploy"
                    / "container"
                    / "quadlet"
                    / source_name
                )
                rendered = source.read_text(encoding="utf-8")
                rendered = rendered.replace(
                    "{{CONTROLLER_IMAGE}}", self._controller_image()
                ).replace("{{POSTGRES_IMAGE}}", self._postgresql_image())
                rendered = rendered.replace(
                    "{{WORKER_IMAGE}}", self._worker_image()
                ).replace("{{WORKSPACE_ROOT}}", config.workspace_root)
                dependencies = (
                    "After=aifactory-postgresql.service\n"
                    "Requires=aifactory-postgresql.service"
                    if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
                    else ""
                )
                rendered = rendered.replace(
                    "{{DATABASE_DEPENDENCIES}}", dependencies
                )
                # Inject the pinned database address in code instead of a
                # template placeholder: an older installer applying this
                # release would copy an unknown placeholder verbatim, and
                # Quadlet then silently drops the whole unit.
                if database_ip and target_name == "aifactory-postgresql.container":
                    rendered = self._add_container_key(rendered, f"IP={database_ip}")
                elif database_ip and target_name == "aifactory-controller.container":
                    rendered = self._add_container_key(
                        rendered, f"AddHost=aifactory-postgresql:{database_ip}"
                    )
                leftover = re.search(r"\{\{[A-Z0-9_]+\}\}", rendered)
                if leftover:
                    raise InstallerError(
                        f"{source_name} still contains {leftover.group(0)} after rendering."
                    )
                if self.runner.dry_run:
                    self.runner.run(
                        ["install", "-m", "0644", str(source), str(quadlet_dir / target_name)]
                    )
                else:
                    self._atomic_write(
                        quadlet_dir / target_name,
                        rendered.encode("utf-8"),
                        0o644,
                    )
        self.runner.run(["systemctl", "daemon-reload"])
        if config.containerized_controller or config.containerized_worker:
            self._verify_generated_units(config)

    @staticmethod
    def _add_container_key(rendered: str, line: str) -> str:
        """Append ``line`` to the [Container] section unless it is already set."""
        lines = rendered.splitlines()
        if line in (item.strip() for item in lines):
            return rendered
        try:
            start = lines.index("[Container]")
        except ValueError as exc:
            raise InstallerError("Quadlet template has no [Container] section.") from exc
        end = start + 1
        while end < len(lines) and not lines[end].startswith("["):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line)
        return "\n".join(lines) + ("\n" if rendered.endswith("\n") else "")

    def _generated_unit_names(self, config: InstallConfig) -> list[str]:
        names: list[str] = []
        if config.containerized_controller:
            if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
                names.append("aifactory-postgresql.service")
            names.append("aifactory-controller.service")
        if config.containerized_worker:
            names.append("aifactory-worker.service")
        return names

    def _verify_generated_units(self, config: InstallConfig) -> None:
        """Fail before any restart when Quadlet rejected a rendered unit.

        The Podman generator skips a .container file it cannot convert, and
        systemd later reports only "Unit ... not found". Surface the
        generator's own explanation instead.
        """
        if self.runner.dry_run:
            return
        missing = []
        for name in self._generated_unit_names(config):
            result = self.runner.run(
                ["systemctl", "show", "--property=LoadState", "--value", name],
                capture_output=True,
                check=False,
            )
            if (result.stdout or "").strip() != "loaded":
                missing.append(name)
        if not missing:
            return
        detail = ""
        generator = self.host_path("/usr/libexec/podman/quadlet")
        if generator.exists():
            dry_run = self.runner.run(
                [str(generator), "-dryrun"],
                capture_output=True,
                check=False,
            )
            lines = [
                line
                for line in (dry_run.stderr or "").splitlines()
                if line.strip()
            ]
            detail = "\n".join(lines[-40:])
        raise InstallerError(
            "Podman Quadlet did not generate "
            + ", ".join(missing)
            + " from /etc/containers/systemd (including any *.container.d "
            "drop-ins)."
            + (f"\nQuadlet output:\n{detail}" if detail else "")
        )

    def _configure_worker_storage(self, config: InstallConfig) -> None:
        if not config.installs_worker:
            return
        script = self.host_path(config.install_root) / "current" / "deploy" / "configure_selinux.sh"
        self.runner.run(
            ["bash", str(script)],
            env={
                "AIFACTORY_SERVICE_USER": config.service_user,
                "AIFACTORY_STORAGE_ROOT": str(self.host_path(config.workspace_root)),
            },
        )

    def _service_uid(self, config: InstallConfig) -> str:
        """Numeric uid of the rootless worker's service account."""
        uid_result = self.runner.run(
            ["id", "-u", config.service_user],
            capture_output=True,
        )
        service_uid = (uid_result.stdout or "").strip()
        if not service_uid.isdigit():
            if not self.runner.dry_run:
                raise InstallerError(
                    f"Could not resolve the uid for service user {config.service_user}."
                )
            service_uid = "SERVICE_UID"
        return service_uid

    def _prepare_images(self, config: InstallConfig) -> None:
        if not config.installs_worker:
            return
        release = self.host_path(config.install_root) / "current"
        image_dir = release / "offline" / "images"
        archives = sorted(image_dir.glob("*.tar")) if image_dir.is_dir() else []
        if config.containerized_worker:
            if archives and config.preload_images:
                for archive in archives:
                    self.runner.run(["podman", "load", "-i", str(archive)], cwd=release)
            return
        runtime_dir = f"/run/user/{self._service_uid(config)}"
        self.runner.run(
            [
                "install",
                "-d",
                "-o",
                config.service_user,
                "-g",
                config.service_user,
                "-m",
                "0700",
                runtime_dir,
            ]
        )
        user_command = [
            "runuser",
            "-u",
            config.service_user,
            "--",
            "env",
            "HOME=/var/lib/aifactory",
            f"XDG_RUNTIME_DIR={runtime_dir}",
        ]
        if archives and config.preload_images:
            for archive in archives:
                self.runner.run(
                    [*user_command, "podman", "load", "-i", str(archive)],
                    cwd=release,
                )
            return
        # New base bundles intentionally contain no workspace images. The
        # enrolled worker agent downloads enabled, verified archives from the
        # controller after its service starts. Legacy bundles can still opt in
        # to loading their embedded archives through preload_images.

    def _install_ingress(
        self, config: InstallConfig, *, apply_initial: bool = True
    ) -> None:
        if not config.installs_controller:
            return
        script = self.host_path(config.install_root) / "current" / "deploy" / "install_ingress.sh"
        owner = "10001" if config.containerized_controller else config.service_user
        self.runner.run(
            ["bash", str(script), owner],
            env={
                "AIFACTORY_HTTPS_HOSTNAME": self._public_hostname(config.public_url),
                "AIFACTORY_INGRESS_APPLY_INITIAL": "1" if apply_initial else "0",
            },
        )

    def _enable_container_firewall_recovery(self, config: InstallConfig) -> None:
        """Re-apply netavark rules whenever firewalld reloads (shipped by netavark >= 1.12)."""
        if not (config.containerized_controller or config.containerized_worker):
            return
        unit = "netavark-firewalld-reload.service"
        present = self.runner.run(
            ["systemctl", "list-unit-files", "--no-legend", unit],
            capture_output=True,
            check=False,
        )
        if self.runner.dry_run or unit in (present.stdout or ""):
            self.runner.run(["systemctl", "enable", "--now", unit], check=False)

    def _start_services(self, config: InstallConfig) -> None:
        self._enable_container_firewall_recovery(config)
        self.runner.run(
            ["systemctl", "enable", "--now", "aifactory-update.path"]
        )
        if (
            config.containerized_controller
            and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
        ):
            self.runner.run(["systemctl", "start", "aifactory-postgresql.service"])
        if config.installs_controller:
            if config.containerized_controller:
                self.runner.run(["systemctl", "start", "aifactory-controller.service"])
            else:
                self.runner.run(
                    ["systemctl", "enable", "--now", "aifactory-controller.service"]
                )
        if config.installs_worker:
            if config.containerized_worker:
                self.runner.run(["systemctl", "enable", "--now", "podman.socket"])
                self.runner.run(["systemctl", "start", "aifactory-worker.service"])
            else:
                self.runner.run(
                    ["systemctl", "enable", "--now", "aifactory-worker.service"]
                )
        self._verify_services(config)

    def _enable_update_watcher(self) -> None:
        if self.defer_update_watcher:
            # The devcloud updater running this update still holds
            # running.json; the new watcher would replay it. LegacyMigration
            # starts the watcher after that updater exits.
            self.runner.run(["systemctl", "enable", "aifactory-update.path"])
            return
        self.runner.run(
            ["systemctl", "enable", "--now", "aifactory-update.path"]
        )

    def _restart_services(self, config: InstallConfig) -> None:
        self._enable_container_firewall_recovery(config)
        self._enable_update_watcher()
        if (
            config.containerized_controller
            and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
        ):
            self.runner.run(
                ["systemctl", "restart", "aifactory-postgresql.service"]
            )
        if config.installs_controller:
            if not config.containerized_controller:
                self.runner.run(["systemctl", "enable", "aifactory-controller.service"])
            self.runner.run(["systemctl", "restart", "aifactory-controller.service"])
        if config.installs_worker:
            if config.containerized_worker:
                self.runner.run(["systemctl", "enable", "--now", "podman.socket"])
            else:
                self.runner.run(["systemctl", "enable", "aifactory-worker.service"])
            self.runner.run(["systemctl", "restart", "aifactory-worker.service"])
        self._verify_services(config)

    def _verify_services(self, config: InstallConfig) -> None:
        names = [] if self.defer_update_watcher else ["aifactory-update.path"]
        if (
            config.containerized_controller
            and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
        ):
            names.append("aifactory-postgresql.service")
        if config.installs_controller:
            names.append("aifactory-controller.service")
        if config.installs_worker:
            names.append("aifactory-worker.service")
        for name in names:
            self.runner.run(["systemctl", "is-active", "--quiet", name])
        if config.containerized_controller:
            inspect = [
                "podman",
                "inspect",
                "--format",
                "{{.State.Health.Status}}",
                "aifactory-controller",
            ]
            if self.runner.dry_run:
                self.runner.run(inspect, capture_output=True)
            else:
                for attempt in range(45):
                    self._report_detail(f"Controller sağlık kontrolü ({attempt + 1}/45)")
                    health = self.runner.run(
                        inspect,
                        capture_output=True,
                        check=False,
                    )
                    if health.returncode == 0 and health.stdout.strip() == "healthy":
                        break
                    if attempt == 44:
                        logs = self.runner.run(
                            ["podman", "logs", "--tail", "100", "aifactory-controller"],
                            capture_output=True,
                            check=False,
                        )
                        raise InstallerError(
                            "Controller container did not become ready. "
                            + (logs.stdout.strip() or logs.stderr.strip())
                        )
                    time.sleep(2)
        if config.installs_worker:
            release = self.host_path(config.install_root) / "current"
            worker_env = self._read_env(
                self.host_path("/etc/aifactory/worker.env")
            )
            if config.containerized_worker:
                self.runner.run(
                    [
                        "podman",
                        "exec",
                        "aifactory-worker",
                        "python",
                        "-m",
                        "app.installer.verify_worker",
                    ]
                )
            else:
                self.runner.run(
                    [
                        "runuser",
                        "-u",
                        config.service_user,
                        "--",
                        str(release / ".venv" / "bin" / "python"),
                        "-m",
                        "app.installer.verify_worker",
                    ],
                    cwd=release,
                    env=worker_env,
                )

    def _stop_services(
        self,
        config: InstallConfig,
        *,
        include_database: bool = False,
    ) -> None:
        managed_units = ["aifactory-update.path", "aifactory-update.service"]
        generated_units = []
        if config.installs_worker:
            target = (
                generated_units if config.containerized_worker else managed_units
            )
            target.append("aifactory-worker.service")
        if config.installs_controller:
            target = (
                generated_units if config.containerized_controller else managed_units
            )
            target.append("aifactory-controller.service")
        if (
            include_database
            and config.containerized_controller
            and config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL
        ):
            generated_units.append("aifactory-postgresql.service")
        for name in managed_units:
            self.runner.run(["systemctl", "disable", "--now", name])
        for name in generated_units:
            self.runner.run(["systemctl", "stop", name])

    def _remove_service_units(self, config: InstallConfig) -> None:
        systemd_dir = self.host_path("/etc/systemd/system")
        names = ["aifactory-update.service", "aifactory-update.path"]
        if config.installs_controller:
            names.append("aifactory-controller.service")
        if config.installs_worker:
            names.append("aifactory-worker.service")
        for name in names:
            if not self.runner.dry_run:
                (systemd_dir / name).unlink(missing_ok=True)
        if config.containerized_controller or config.containerized_worker:
            quadlet_dir = self.host_path("/etc/containers/systemd")
            for name in (
                "aifactory.network",
                "aifactory-controller.container",
                "aifactory-postgresql.container",
                "aifactory-worker.container",
            ):
                if not self.runner.dry_run:
                    (quadlet_dir / name).unlink(missing_ok=True)
        self.runner.run(["systemctl", "daemon-reload"])

    def _remove_active_release(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                ["aifactory-internal-remove-active-release", config.install_root]
            )
            return
        current = self.host_path(config.install_root) / "current"
        if current.is_symlink() or current.is_file():
            current.unlink(missing_ok=True)

    def _purge_data(self, config: InstallConfig) -> None:
        targets = {
            self.host_path(config.install_root),
            self.host_path(config.state_root),
            self.host_path(config.releases_root),
            self.host_path("/etc/aifactory"),
        }
        if config.installs_worker:
            targets.add(self.host_path(config.workspace_root))
        if config.installs_controller:
            targets.update(
                {
                    self.host_path(config.downloads_root),
                    self.host_path("/var/lib/aifactory/database"),
                    self.host_path("/var/lib/aifactory/download-builds"),
                }
            )
            if config.containerized_controller:
                targets.add(self.host_path("/var/lib/aifactory/ingress"))
                if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
                    targets.add(self.host_path("/var/lib/aifactory/postgresql"))
        root = self.filesystem_root.resolve()
        for target in targets:
            resolved = target.resolve()
            if resolved == root or root not in resolved.parents:
                raise InstallerError(f"Refusing unsafe purge target: {resolved}")
            if self.runner.dry_run:
                self.runner.run(["aifactory-internal-purge", str(resolved)])
                continue
            if resolved.exists():
                shutil.rmtree(resolved)

    def _save_state(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "aifactory-internal-save-state",
                    config.role.value,
                    self.release_version,
                ]
            )
            return
        path = self.state_path(config)
        existing = InstallationState.load(path)
        if existing:
            state = InstallationState(
                schema=existing.schema,
                role=config.role.value,
                version=self.release_version,
                installed_at=existing.installed_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                configuration=config.public_dict(),
            )
        else:
            state = InstallationState.create(
                config.role.value, self.release_version, config.public_dict()
            )
        state.save(path)

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(mode))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
