"""One-time move of a devcloud-named installation to the aifactory names.

Release 3.49.1 is the first release that uses the aifactory names. When its
installer updates a host installed by an older release, it first runs these
steps:

1. back up configuration and database (controller roles);
2. stop the devcloud services, remove the stopped workspace containers;
3. rename the bundled PostgreSQL role and database;
4. rename /etc/devcloud, /var/lib/devcloud, /opt/devcloud and
   /srv/devcloud-downloads, leaving compatibility symlinks at the old paths
   (the older updater that started this update still uses them);
5. rename the devcloud service account and group (same uid and gid);
6. rewrite environment files, install state, links and file names;
7. remove the devcloud systemd units, Quadlets and ingress helper.

The normal update then installs the aifactory units and starts them.

Every change is recorded in a journal before it is made. When any later step
of the update fails, ``undo`` reverses the recorded changes in reverse order
and starts the devcloud services again. A journal left behind by an
interrupted run is undone before the next attempt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.installer.layout import (
    CURRENT,
    LEGACY,
    ROOT_PAIRS,
    current_env_key,
    current_path,
    replace_legacy_roots,
)
from app.installer.models import ControllerRuntime, DatabaseMode, InstallConfig
from app.installer.platform import InstallerError

if TYPE_CHECKING:  # pragma: no cover
    from app.installer.engine import InstallerEngine, PlanStep


JOURNAL_ROOT = "/var/lib/aifactory-migration"
LEGACY_HOSTS_MARKER = "# devcloud-controller-fallback"
CURRENT_HOSTS_MARKER = "# aifactory-controller-fallback"
LEGACY_UPDATE_SERVICE = "devcloud-update.service"
LEGACY_UPDATE_PATH = "devcloud-update.path"
# Started again in this order when the migration is undone.
LEGACY_UNITS = (
    "devcloud-postgresql.service",
    "devcloud-controller.service",
    "devcloud.service",
    "devcloud-worker.service",
    "devcloud-ingress.path",
    "devcloud-update.path",
)
LEGACY_UNIT_FILES = (
    "devcloud-controller.service",
    "devcloud.service",
    "devcloud-worker.service",
    "devcloud-update.path",
    LEGACY_UPDATE_SERVICE,
    "devcloud-ingress.path",
    "devcloud-ingress.service",
)
LEGACY_QUADLETS = (
    "devcloud.network",
    "devcloud-controller.container",
    "devcloud-postgresql.container",
    "devcloud-worker.container",
)
CURRENT_UNIT_FILES = (
    "aifactory-controller.service",
    "aifactory-worker.service",
    "aifactory-update.path",
    "aifactory-update.service",
    "aifactory-ingress.path",
    "aifactory-ingress.service",
)
CURRENT_QUADLETS = (
    "aifactory.network",
    "aifactory-controller.container",
    "aifactory-postgresql.container",
    "aifactory-worker.container",
)
LEGACY_INGRESS_HELPER = "/usr/local/libexec/devcloud-apply-ingress"
CURRENT_INGRESS_HELPER = "/usr/local/libexec/aifactory-apply-ingress"
LEGACY_NGINX_CONFIG = "/etc/nginx/conf.d/devcloud.conf"
CURRENT_NGINX_CONFIG = "/etc/nginx/conf.d/aifactory.conf"
# Workspace containers are named <prefix>-<user id>-<slug>-<workspace id[:8]>.
# A 3.49.1 controller names new ones aifactory-* while workers still run the
# old release; their bind mounts also point into the old tree.
LEGACY_WORKSPACE_CONTAINER = re.compile(
    r"^(?:devcloud|aifactory)-[0-9]+-(?:.+-)?[0-9a-f]{8}$"
)
WORKER_STATE_NAMES = (
    (".devcloud-agent-registry.json", ".aifactory-agent-registry.json"),
    (".devcloud-image-state.json", ".aifactory-image-state.json"),
    (".devcloud-image-cache", ".aifactory-image-cache"),
)
POSTGRESQL_HELPER_CONTAINER = "aifactory-migration-postgresql"
LEGACY_POSTGRESQL_IMAGE = "localhost/devcloud-postgresql:16"
CURRENT_POSTGRESQL_IMAGE = "localhost/aifactory-postgresql:16"


def legacy_state_file(host_path) -> Path:
    return host_path(LEGACY.state_root) / "install-state.json"


def needs_migration(host_path) -> bool:
    """True when the installed state still lives in the devcloud layout."""
    legacy_root = host_path(LEGACY.var_dir)
    return (
        legacy_root.is_dir()
        and not legacy_root.is_symlink()
        and legacy_state_file(host_path).is_file()
        and not (host_path(CURRENT.state_root) / "install-state.json").is_file()
    )


def migrated_configuration(values: dict[str, Any]) -> dict[str, Any]:
    """Persisted installer configuration with the aifactory paths and user."""
    migrated: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, str):
            value = current_path(value)
        migrated[key] = value
    if migrated.get("service_user") == LEGACY.service_user:
        migrated["service_user"] = CURRENT.service_user
    return migrated


def migrated_env_text(text: str) -> str:
    """Rename DEVCLOUD_* keys and move legacy paths inside the values."""
    lines = []
    for raw in text.splitlines():
        if raw and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            raw = f"{current_env_key(key)}={replace_legacy_roots(value)}"
        lines.append(raw)
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")


def migrated_postgresql_env_text(text: str) -> str:
    lines = []
    for raw in migrated_env_text(text).splitlines():
        key, separator, value = raw.partition("=")
        if separator and key in {"POSTGRESQL_USER", "POSTGRESQL_DATABASE"}:
            if value.strip().strip('"') == LEGACY.database:
                raw = f"{key}={CURRENT.database}"
        lines.append(raw)
    return "\n".join(lines) + "\n"


def migrated_nginx_text(text: str) -> str:
    return (
        replace_legacy_roots(text)
        .replace("$devcloud_connection_upgrade", "$aifactory_connection_upgrade")
        .replace("shared:DevCloudTLS:", "shared:AIFactoryTLS:")
    )


def _mount_points() -> list[str]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    points = []
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            points.append(fields[4].encode("utf-8").decode("unicode_escape"))
    return points


def _rename_sql(source: str, target: str) -> str:
    """Idempotent: renames only what still carries the source name."""
    return (
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT FROM pg_database WHERE datname = '{source}') THEN "
        f"EXECUTE 'ALTER DATABASE {source} RENAME TO {target}'; END IF; "
        f"IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{source}') THEN "
        f"EXECUTE 'ALTER ROLE {source} RENAME TO {target}'; END IF; "
        "END $$;"
    )


class Journal:
    """Ordered record of reversible changes, kept outside both trees."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "journal.json"
        self.files = root / "files"
        self.data: dict[str, Any] = {}

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> None:
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def start(self, version: str) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.files.mkdir(mode=0o700, exist_ok=True)
        self.data = {
            "format": 1,
            "state": "running",
            "target_version": version,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "actions": [],
        }
        self.save()

    def save(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}")
        temporary.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def record(self, action: dict[str, Any]) -> None:
        self.data.setdefault("actions", []).append(action)
        self.save()

    def keep_file(self, path: Path, logical: str) -> None:
        """Remember ``path``'s content (or its absence) before changing it."""
        saved = None
        if path.is_file() and not path.is_symlink():
            saved = uuid.uuid4().hex
            shutil.copy2(path, self.files / saved)
        self.record({"type": "file", "path": logical, "saved": saved})

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class LegacyMigration:
    def __init__(
        self,
        engine: "InstallerEngine",
        legacy_config: InstallConfig,
        config: InstallConfig,
        *,
        legacy_version: str = "unknown",
    ):
        self.engine = engine
        self.runner = engine.runner
        self.host_path = engine.host_path
        self.legacy_config = legacy_config
        self.config = config
        self.legacy_version = legacy_version
        self.journal = Journal(self.host_path(JOURNAL_ROOT))
        self.rename_user = legacy_config.service_user == LEGACY.service_user
        self.inside_update_service = self._unit_running(LEGACY_UPDATE_SERVICE)

    # -- plan -------------------------------------------------------------

    def steps(self) -> list["PlanStep"]:
        from app.installer.engine import PlanStep

        steps = [
            PlanStep(
                "migrate-preflight",
                "Check that this devcloud host can move to the aifactory names",
                self.preflight,
            ),
        ]
        if self.config.installs_controller:
            steps.append(
                PlanStep(
                    "backup",
                    "Create a verified configuration and database backup before the move",
                    self.backup,
                )
            )
        steps.extend(
            [
                PlanStep(
                    "migrate-stop",
                    "Stop the devcloud services and remove stopped workspace containers",
                    self.stop,
                ),
                PlanStep(
                    "migrate-database",
                    "Rename the bundled PostgreSQL role and database to aifactory",
                    self.rename_database,
                ),
                PlanStep(
                    "migrate-move",
                    "Move /etc, /var/lib, /opt and /srv directories to the aifactory names",
                    self.move_directories,
                ),
                PlanStep(
                    "migrate-account",
                    "Rename the devcloud service account and group to aifactory",
                    self.rename_account,
                ),
                PlanStep(
                    "migrate-files",
                    "Rewrite configuration, install state and links for the new names",
                    self.rewrite_files,
                ),
                PlanStep(
                    "migrate-units",
                    "Remove the devcloud systemd units, Quadlets and ingress helper",
                    self.remove_legacy_units,
                ),
            ]
        )
        return steps

    def worker_storage_step(self) -> "PlanStep":
        from app.installer.engine import PlanStep

        return PlanStep(
            "selinux",
            "Label the moved workspace storage for containers",
            self.configure_worker_storage,
        )

    def commit_step(self) -> "PlanStep":
        from app.installer.engine import PlanStep

        return PlanStep(
            "migrate-commit",
            "Mark the move to the aifactory names as applied",
            self.commit,
        )

    def commit(self) -> None:
        """After this point the move is kept; undo and recovery only clean up."""
        if self.runner.dry_run or not self.journal.exists():
            return
        if not self.journal.data:
            self.journal.load()
        self.journal.data["state"] = "committed"
        self.journal.save()

    def finish_step(self) -> "PlanStep":
        from app.installer.engine import PlanStep

        return PlanStep(
            "migrate-finish",
            "Remove the devcloud network, SELinux rule and migration journal",
            self.finish,
        )

    # -- helpers ----------------------------------------------------------

    def _unit_running(self, name: str) -> bool:
        if self.runner.dry_run:
            return False
        result = self.runner.run(
            ["systemctl", "show", "--property=ActiveState", "--value", name],
            capture_output=True,
            check=False,
        )
        return (result.stdout or "").strip() in {"active", "activating", "reloading"}

    def _host_absolute(self, path: Path) -> str:
        """``path`` as seen on the host (without a test filesystem root)."""
        root = self.engine.filesystem_root
        if root == Path("/"):
            return str(path)
        return "/" + str(path.relative_to(root)).lstrip("/")

    def _map_host_path(self, value: str) -> str:
        """Map an absolute on-disk path under a legacy root to its new root."""
        for old, new in ROOT_PAIRS:
            old_path = str(self.host_path(old))
            if value == old_path or value.startswith(old_path + "/"):
                return str(self.host_path(new)) + value[len(old_path) :]
        return value

    def _report(self, text: str) -> None:
        self.engine._report_detail(text)

    def _write_text(self, path: Path, text: str) -> None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        owner = path.stat() if path.exists() else None
        self.engine._atomic_write(path, text.encode("utf-8"), mode)
        if owner is not None and hasattr(os, "chown"):
            try:
                os.chown(path, owner.st_uid, owner.st_gid)
            except PermissionError:
                pass

    def _postgresql_image(self) -> str:
        for image in (LEGACY_POSTGRESQL_IMAGE, CURRENT_POSTGRESQL_IMAGE):
            exists = self.runner.run(
                ["podman", "image", "exists", image], check=False
            )
            if exists.returncode == 0:
                return image
        raise InstallerError(
            "The bundled PostgreSQL image is missing; cannot rename the database"
        )

    def _rename_container_database(self, data_dir: Path, source: str, target: str) -> None:
        """Rename role and database with a short-lived copy of the database container.

        The image starts in admin-only mode, so its start script does not
        depend on which of the two role names currently exists; the throwaway
        superuser password is removed again in the same session.
        """
        self._require_stopped_database()
        image = self._postgresql_image()
        env_file = self.journal.root / "postgresql-helper.env"
        if not self.runner.dry_run:
            # A file keeps the password out of the process list.
            env_file.write_text(
                f"POSTGRESQL_ADMIN_PASSWORD={uuid.uuid4().hex}\n", encoding="utf-8"
            )
            os.chmod(env_file, 0o600)
        name = POSTGRESQL_HELPER_CONTAINER
        self.runner.run(["podman", "rm", "-f", name], check=False)
        try:
            self.runner.run(
                [
                    "podman", "run", "-d", "--name", name,
                    "--network", "none",
                    "--env-file", str(env_file),
                    "-v", f"{data_dir}:/var/lib/pgsql/data:Z",
                    image,
                ]
            )
            self._wait_for_container_database(name)
            self.runner.run(
                [
                    "podman", "exec", name, "psql", "-v", "ON_ERROR_STOP=1",
                    "-d", "postgres",
                    "-c", _rename_sql(source, target),
                    "-c", "ALTER ROLE postgres PASSWORD NULL",
                ]
            )
        finally:
            self.runner.run(["podman", "stop", "-t", "30", name], check=False)
            self.runner.run(["podman", "rm", "-f", name], check=False)
            env_file.unlink(missing_ok=True)

    def _wait_for_container_database(self, name: str) -> None:
        if self.runner.dry_run:
            return
        # The image's start script runs a temporary server first; PID 1 is
        # postgres only once the final server accepts connections.
        for _attempt in range(120):
            running = self.runner.run(
                ["podman", "container", "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True,
                check=False,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                break
            process = self.runner.run(
                ["podman", "exec", name, "cat", "/proc/1/comm"],
                capture_output=True,
                check=False,
            )
            if process.returncode == 0 and process.stdout.strip() == "postgres":
                ready = self.runner.run(
                    ["podman", "exec", name, "pg_isready", "-q"], check=False
                )
                if ready.returncode == 0:
                    return
            time.sleep(1)
        logs = self.runner.run(
            ["podman", "logs", "--tail", "50", name], capture_output=True, check=False
        )
        raise InstallerError(
            "The database did not start for the rename. "
            + (logs.stdout or logs.stderr or "").strip()
        )

    def _require_stopped_database(self) -> None:
        """Never start a second server on the data directory in use."""
        if self.runner.dry_run:
            return
        if self._unit_running("devcloud-postgresql.service"):
            raise InstallerError("devcloud-postgresql.service is still running")
        for name in (LEGACY.postgresql_container, CURRENT.postgresql_container):
            exists = self.runner.run(
                ["podman", "container", "exists", name], check=False
            )
            if exists.returncode == 0:
                raise InstallerError(
                    f"The {name} container still exists; stop it and retry"
                )

    def _rename_native_database(self, source: str, target: str) -> None:
        self.runner.run(
            [
                "runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1",
                "-d", "postgres", "-c", _rename_sql(source, target),
            ]
        )

    # -- steps ------------------------------------------------------------

    def preflight(self) -> None:
        if self.runner.dry_run:
            return
        config = self.legacy_config
        if config.installs_worker and not config.containerized_worker:
            raise InstallerError(
                "This native (rootless) worker cannot move to the aifactory names "
                "in place: its Podman storage is tied to the devcloud account. "
                "Uninstall it and register it again from the controller."
            )
        if self.journal.exists():
            raise InstallerError(
                f"An earlier migration journal is still present: {self.journal.path}"
            )
        mounts = _mount_points()
        for old, new in ROOT_PAIRS:
            source = self.host_path(old)
            target = self.host_path(new)
            if not source.exists() and not source.is_symlink():
                continue
            if source.is_symlink() or not source.is_dir():
                raise InstallerError(f"{old} is not a directory; cannot move it")
            if target.exists() or target.is_symlink():
                raise InstallerError(
                    f"{new} already exists. Move it away and retry the update."
                )
            if os.path.ismount(source):
                raise InstallerError(
                    f"{old} is a mount point. Mount it at {new} instead "
                    "(fstab or mount unit), then retry the update."
                )
            nested = [point for point in mounts if point.startswith(str(source) + "/")]
            if nested:
                raise InstallerError(
                    f"{nested[0]} is mounted below {old}. Mount it below {new} "
                    "instead, then retry the update."
                )
            if source.stat().st_dev != target.parent.stat().st_dev:
                raise InstallerError(
                    f"{old} and {target.parent} are on different filesystems; "
                    "the directory cannot be renamed in place."
                )
        if self.rename_user:
            existing = self.runner.run(
                ["id", "-u", CURRENT.service_user], capture_output=True, check=False
            )
            if existing.returncode == 0:
                raise InstallerError(
                    f"A user named {CURRENT.service_user} already exists; "
                    "cannot rename the devcloud service account."
                )

    def backup(self) -> None:
        from app.installer.backup import create_backup

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = self.host_path(
            f"{LEGACY.var_dir}/backups/pre-migrate-v{self.legacy_version}-{timestamp}.tar.gz"
        )
        create_backup(
            config=self.legacy_config,
            version=self.legacy_version,
            output=output,
            host_path=self.host_path,
            runner=self.runner,
            include_workspaces=False,
            layout=LEGACY,
        )

    def stop(self) -> None:
        if self.runner.dry_run:
            for name in LEGACY_UNITS:
                self.runner.run(["systemctl", "stop", name], check=False)
            return
        self.journal.start(self.engine.release_version)
        active: list[str] = []
        enabled: list[str] = []
        for name in LEGACY_UNITS:
            if self._unit_running(name):
                active.append(name)
            state = self.runner.run(
                ["systemctl", "is-enabled", name], capture_output=True, check=False
            )
            if (state.stdout or "").strip() == "enabled":
                enabled.append(name)
        self.journal.record({"type": "units", "active": active, "enabled": enabled})
        for name in reversed(LEGACY_UNITS):
            if name == LEGACY_UPDATE_PATH:
                # Left running so a reboot resumes this update through the old
                # updater, which hands it to this installer again (that undoes
                # and retries). Stopped with its unit file in migrate-units.
                continue
            if name in enabled:
                self.runner.run(["systemctl", "disable", "--now", name], check=False)
            else:
                self.runner.run(["systemctl", "stop", name], check=False)
        if self.legacy_config.installs_worker:
            listing = self.runner.run(
                ["podman", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True,
                check=False,
            )
            names = [
                name
                for name in (listing.stdout or "").split()
                if LEGACY_WORKSPACE_CONTAINER.fullmatch(name)
            ]
            for name in names:
                self.runner.run(["podman", "stop", "-t", "15", name], check=False)
                self.runner.run(["podman", "rm", "-f", name], check=False)
            if names:
                self._report(
                    f"{len(names)} workspace konteyneri kaldırıldı; ilk başlatmada yeniden oluşturulur"
                )

    def rename_database(self) -> None:
        config = self.legacy_config
        if (
            not config.installs_controller
            or config.database_mode != DatabaseMode.BUNDLED_POSTGRESQL
        ):
            return
        if config.controller_runtime == ControllerRuntime.CONTAINER:
            password = self.engine._read_env(
                self.host_path(f"{LEGACY.etc_dir}/postgresql.env")
            ).get("POSTGRESQL_PASSWORD", "")
            if not password and not self.runner.dry_run:
                raise InstallerError(
                    f"{LEGACY.etc_dir}/postgresql.env has no PostgreSQL password"
                )
            if not self.runner.dry_run:
                self.journal.record({"type": "database", "mode": "container"})
            self._rename_container_database(
                self.host_path(f"{LEGACY.var_dir}/postgresql"),
                LEGACY.database,
                CURRENT.database,
            )
        else:
            if not self.runner.dry_run:
                self.journal.record({"type": "database", "mode": "native"})
            self._rename_native_database(LEGACY.database, CURRENT.database)

    def move_directories(self) -> None:
        for old, new in ROOT_PAIRS:
            source = self.host_path(old)
            target = self.host_path(new)
            if self.runner.dry_run:
                self.runner.run(["mv", old, new])
                self.runner.run(["ln", "-s", new, old])
                continue
            if not source.is_dir() or source.is_symlink():
                continue
            self.journal.record({"type": "move", "old": old, "new": new})
            os.rename(source, target)
            self.journal.record({"type": "symlink", "path": old})
            relative = os.path.relpath(target, start=source.parent)
            source.symlink_to(relative, target_is_directory=True)

    def rename_account(self) -> None:
        if not self.rename_user:
            return
        legacy, current = LEGACY.service_user, CURRENT.service_user
        if not self.runner.dry_run:
            processes = self.runner.run(
                ["pgrep", "-u", legacy, "-l"], capture_output=True, check=False
            )
            if processes.returncode == 0 and processes.stdout.strip():
                raise InstallerError(
                    f"Processes of {legacy} are still running; stop them first:\n"
                    + processes.stdout.strip()
                )
            self.journal.record({"type": "account"})
        group = self.runner.run(
            ["getent", "group", legacy], capture_output=True, check=False
        )
        self.runner.run(
            ["usermod", "-l", current, "-d", CURRENT.var_dir, legacy]
        )
        if self.runner.dry_run or group.returncode == 0:
            self.runner.run(["groupmod", "-n", current, legacy])
        if self.runner.dry_run:
            return
        for name in ("/etc/subuid", "/etc/subgid"):
            path = self.host_path(name)
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            if not any(line.startswith(f"{legacy}:") for line in lines):
                continue
            self.journal.keep_file(path, name)
            rewritten = [
                current + line[len(legacy) :] if line.startswith(f"{legacy}:") else line
                for line in lines
            ]
            self._write_text(path, "\n".join(rewritten) + "\n")

    def rewrite_files(self) -> None:
        if self.runner.dry_run:
            self.runner.run(["aifactory-internal-migrate-files", CURRENT.etc_dir])
            return
        etc = self.host_path(CURRENT.etc_dir)
        for name in ("controller.env", "worker.env"):
            path = etc / name
            if path.is_file():
                self.journal.keep_file(path, f"{CURRENT.etc_dir}/{name}")
                self._write_text(
                    path, migrated_env_text(path.read_text(encoding="utf-8"))
                )
        postgresql_env = etc / "postgresql.env"
        if postgresql_env.is_file():
            self.journal.keep_file(postgresql_env, f"{CURRENT.etc_dir}/postgresql.env")
            self._write_text(
                postgresql_env,
                migrated_postgresql_env_text(postgresql_env.read_text(encoding="utf-8")),
            )

        # The release step replaces it with the new release's keyring.
        keyring = etc / "release-keyring.gpg"
        self.journal.keep_file(keyring, f"{CURRENT.etc_dir}/release-keyring.gpg")

        state = self.host_path(CURRENT.state_root) / "install-state.json"
        if state.is_file():
            self.journal.keep_file(state, f"{CURRENT.state_root}/install-state.json")
            data = json.loads(state.read_text(encoding="utf-8"))
            data["configuration"] = migrated_configuration(data.get("configuration") or {})
            self._write_text(state, json.dumps(data, indent=2) + "\n")

        hosts = self.host_path("/etc/hosts")
        if hosts.is_file():
            text = hosts.read_text(encoding="utf-8")
            if LEGACY_HOSTS_MARKER in text:
                self.journal.keep_file(hosts, "/etc/hosts")
                self._write_text(hosts, text.replace(LEGACY_HOSTS_MARKER, CURRENT_HOSTS_MARKER))

        database = self.host_path(f"{CURRENT.var_dir}/database")
        for suffix in ("", "-wal", "-shm"):
            self._rename(
                database / f"{LEGACY.sqlite_filename}{suffix}",
                database / f"{CURRENT.sqlite_filename}{suffix}",
            )
        if self.config.installs_worker:
            workspace_root = self.host_path(self.config.workspace_root)
            for old_name, new_name in WORKER_STATE_NAMES:
                self._rename(workspace_root / old_name, workspace_root / new_name)
            # The agent finds each workspace's files through this registry.
            registry = workspace_root / WORKER_STATE_NAMES[0][1]
            if registry.is_file():
                text = registry.read_text(encoding="utf-8")
                rewritten = replace_legacy_roots(text)
                if rewritten != text:
                    self.journal.keep_file(registry, self._host_absolute(registry))
                    self._write_text(registry, rewritten)

        self._relink(self.host_path(CURRENT.install_root) / "current")
        releases = self.host_path(self.config.releases_root)
        if releases.is_dir():
            for release in releases.iterdir():
                if release.is_dir() and not release.is_symlink():
                    self._relink(release / ".venv")

        legacy_nginx = self.host_path(LEGACY_NGINX_CONFIG)
        if legacy_nginx.is_file():
            current_nginx = self.host_path(CURRENT_NGINX_CONFIG)
            text = migrated_nginx_text(legacy_nginx.read_text(encoding="utf-8"))
            self.journal.keep_file(current_nginx, CURRENT_NGINX_CONFIG)
            self.engine._atomic_write(current_nginx, text.encode("utf-8"), 0o644)
            self.journal.keep_file(legacy_nginx, LEGACY_NGINX_CONFIG)
            legacy_nginx.unlink()
            self.runner.run(["nginx", "-t"])
            self.runner.run(["systemctl", "reload", "nginx"], check=False)

    def _rename(self, source: Path, target: Path) -> None:
        if not source.exists() or target.exists():
            return
        self.journal.record(
            {
                "type": "rename",
                "old": self._host_absolute(source),
                "new": self._host_absolute(target),
            }
        )
        os.rename(source, target)

    def _relink(self, link: Path) -> None:
        if not link.is_symlink():
            return
        target = os.readlink(link)
        resolved = target if os.path.isabs(target) else os.path.normpath(
            os.path.join(link.parent, target)
        )
        mapped = self._map_host_path(resolved)
        if mapped == resolved:
            return
        new_target = (
            mapped if os.path.isabs(target) else os.path.relpath(mapped, start=link.parent)
        )
        self.journal.record(
            {"type": "link", "path": self._host_absolute(link), "target": target}
        )
        self._replace_link(link, new_target)

    @staticmethod
    def _replace_link(link: Path, target: str) -> None:
        temporary = link.with_name(f".{link.name}-{uuid.uuid4().hex}")
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)

    def remove_legacy_units(self) -> None:
        self.runner.run(["systemctl", "disable", "--now", LEGACY_UPDATE_PATH], check=False)
        systemd_dir = self.host_path("/etc/systemd/system")
        quadlet_dir = self.host_path("/etc/containers/systemd")
        paths = [
            (systemd_dir / name, f"/etc/systemd/system/{name}")
            for name in LEGACY_UNIT_FILES
            if not (name == LEGACY_UPDATE_SERVICE and self.inside_update_service)
        ]
        paths += [
            (quadlet_dir / name, f"/etc/containers/systemd/{name}")
            for name in LEGACY_QUADLETS
        ]
        paths.append((self.host_path(LEGACY_INGRESS_HELPER), LEGACY_INGRESS_HELPER))
        for path, logical in paths:
            if self.runner.dry_run:
                self.runner.run(["rm", "-f", logical])
                continue
            if path.is_file():
                self.journal.keep_file(path, logical)
                path.unlink()
        self.runner.run(["systemctl", "daemon-reload"])

    def configure_worker_storage(self) -> None:
        if not self.runner.dry_run:
            self.journal.record({"type": "fcontext", "root": self.config.workspace_root})
        self.engine._configure_worker_storage(self.config)

    def finish(self) -> None:
        """Best-effort cleanup once the update succeeded; never fails the update."""
        try:
            if self.legacy_config.containerized_controller:
                self.runner.run(["podman", "network", "rm", "devcloud"], check=False)
            if (
                self.legacy_config.installs_worker
                and self.legacy_config.workspace_root != self.config.workspace_root
                and (self.runner.dry_run or self.runner.exists("semanage"))
            ):
                self.runner.run(
                    [
                        "semanage", "fcontext", "-d",
                        f"{self.legacy_config.workspace_root}(/.*)?",
                    ],
                    check=False,
                )
            if self.inside_update_service:
                self._hand_over_update_watcher()
            if not self.runner.dry_run:
                self.journal.remove()
        except Exception as exc:  # noqa: BLE001 - the update itself is applied
            self._report(f"Geçiş temizliği tamamlanamadı: {exc}")

    def _hand_over_update_watcher(self) -> None:
        """Start the aifactory watcher once the devcloud updater has exited.

        Starting it now would run the queued request a second time, because
        the still running devcloud updater keeps running.json until it ends.
        """
        script = (
            "while :; do "
            f"s=$(systemctl show --property=ActiveState --value {LEGACY_UPDATE_SERVICE}); "
            'case "$s" in active|activating|deactivating|reloading) sleep 2;; *) break;; esac; '
            "done; "
            f"rm -f /etc/systemd/system/{LEGACY_UPDATE_SERVICE}; "
            "systemctl daemon-reload; "
            "systemctl enable --now aifactory-update.path"
        )
        result = self.runner.run(
            [
                "systemd-run", "--unit", "aifactory-update-handover", "--collect",
                "--no-block", "/bin/sh", "-c", script,
            ],
            check=False,
        )
        if result.returncode != 0:
            self._report(
                "Güncelleme izleyicisi başlatılamadı; eski güncelleme bittikten sonra "
                "'systemctl enable --now aifactory-update.path' çalıştırın."
            )

    # -- undo -------------------------------------------------------------

    def undo(self) -> None:
        if self.runner.dry_run:
            self.runner.run(["aifactory-internal-undo-migration"])
            return
        if not self.journal.exists():
            return
        if not self.journal.data:
            self.journal.load()
        if self.journal.data.get("state") == "committed":
            # The update was applied; only the cleanup is missing.
            self.finish()
            return
        errors: list[str] = []
        self.warnings: list[str] = []

        def attempt(label: str, action) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - keep undoing
                errors.append(f"{label}: {exc}")

        attempt("new services", self._remove_current_artifacts)
        for action in reversed(self.journal.data.get("actions", [])):
            attempt(action.get("type", "?"), lambda action=action: self._undo(action))
        if errors:
            self.journal.data["state"] = "undo-failed"
            self.journal.data["errors"] = errors
            self.journal.save()
            raise InstallerError(
                "Restoring the devcloud layout failed; the journal is kept in "
                f"{JOURNAL_ROOT} and the next installer run retries it:\n"
                + "\n".join(errors)
            )
        self.journal.remove()
        if self.warnings:
            raise InstallerError(
                "The devcloud layout was restored, but some services did not start:\n"
                + "\n".join(self.warnings)
            )

    def _remove_current_artifacts(self) -> None:
        # The devcloud units may have started again at boot after an
        # interrupted run; they are started again once the layout is back.
        for name in (
            "aifactory-controller.service",
            "aifactory-worker.service",
            "aifactory-postgresql.service",
            "devcloud-controller.service",
            "devcloud.service",
            "devcloud-worker.service",
            "devcloud-postgresql.service",
        ):
            self.runner.run(["systemctl", "stop", name], check=False)
        for name in ("aifactory-update.path", "aifactory-ingress.path"):
            self.runner.run(["systemctl", "disable", "--now", name], check=False)
        systemd_dir = self.host_path("/etc/systemd/system")
        for name in CURRENT_UNIT_FILES:
            (systemd_dir / name).unlink(missing_ok=True)
        quadlet_dir = self.host_path("/etc/containers/systemd")
        for name in CURRENT_QUADLETS:
            (quadlet_dir / name).unlink(missing_ok=True)
        self.host_path(CURRENT_INGRESS_HELPER).unlink(missing_ok=True)
        self.runner.run(["systemctl", "daemon-reload"], check=False)
        if self.legacy_config.containerized_controller:
            self.runner.run(["podman", "network", "rm", "aifactory"], check=False)
        release = self.host_path(self.config.releases_root) / self.engine.release_id
        marker = release / ".aifactory-release-id"
        if (
            marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == self.engine.release_id
            and release.resolve() != (self.engine.previous_release or Path("/")).resolve()
        ):
            shutil.rmtree(release, ignore_errors=True)

    def _undo(self, action: dict[str, Any]) -> None:
        kind = action.get("type")
        if kind == "file":
            path = self.host_path(action["path"])
            saved = action.get("saved")
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                source = self.journal.files / saved
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
                shutil.copy2(source, temporary)
                os.replace(temporary, path)
            if action["path"] in {LEGACY_NGINX_CONFIG, CURRENT_NGINX_CONFIG}:
                self.runner.run(["systemctl", "reload", "nginx"], check=False)
        elif kind == "rename":
            source = self.host_path(action["new"])
            target = self.host_path(action["old"])
            if source.exists() and not target.exists():
                os.rename(source, target)
        elif kind == "link":
            link = self.host_path(action["path"])
            if link.is_symlink():
                self._replace_link(link, action["target"])
        elif kind == "symlink":
            link = self.host_path(action["path"])
            if link.is_symlink():
                link.unlink()
        elif kind == "move":
            source = self.host_path(action["new"])
            target = self.host_path(action["old"])
            if source.is_dir() and not target.exists() and not target.is_symlink():
                os.rename(source, target)
        elif kind == "account":
            current = self.runner.run(
                ["id", "-u", CURRENT.service_user], capture_output=True, check=False
            )
            if current.returncode == 0:
                self.runner.run(
                    [
                        "usermod", "-l", LEGACY.service_user, "-d", LEGACY.var_dir,
                        CURRENT.service_user,
                    ]
                )
            group = self.runner.run(
                ["getent", "group", CURRENT.service_user], capture_output=True, check=False
            )
            if group.returncode == 0:
                self.runner.run(
                    ["groupmod", "-n", LEGACY.service_user, CURRENT.service_user]
                )
        elif kind == "database":
            if action.get("mode") == "container":
                self._rename_container_database(
                    self.host_path(f"{LEGACY.var_dir}/postgresql"),
                    CURRENT.database,
                    LEGACY.database,
                )
            else:
                self._rename_native_database(CURRENT.database, LEGACY.database)
        elif kind == "fcontext":
            if self.runner.exists("semanage"):
                self.runner.run(
                    ["semanage", "fcontext", "-d", f"{action['root']}(/.*)?"],
                    check=False,
                )
        elif kind == "units":
            self.runner.run(["systemctl", "daemon-reload"], check=False)
            for name in LEGACY_UNITS:
                if name in action.get("enabled", []):
                    self.runner.run(["systemctl", "enable", name], check=False)
            for name in LEGACY_UNITS:
                if name in action.get("active", []):
                    started = self.runner.run(["systemctl", "start", name], check=False)
                    if started.returncode != 0:
                        getattr(self, "warnings", []).append(f"systemctl start {name} failed")
            if self.legacy_config.installs_controller:
                self.runner.run(["systemctl", "reload", "nginx"], check=False)


def recover_interrupted_migration(
    engine: "InstallerEngine",
    legacy_config_loader,
) -> bool:
    """Undo a journal left behind by a migration that never finished."""
    journal = Journal(engine.host_path(JOURNAL_ROOT))
    if not journal.exists():
        return False
    journal.load()
    legacy_config, config = legacy_config_loader()
    migration = LegacyMigration(engine, legacy_config, config)
    migration.journal = journal
    migration.undo()
    return True
