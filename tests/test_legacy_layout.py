"""Moving a devcloud-named host to the aifactory names, and undoing it."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.installer.engine import InstallerEngine
from app.installer.legacy_layout import LegacyMigration, needs_migration
from app.installer.models import InstallConfig
from app.installer.legacy_layout import migrated_configuration
from app.installer.platform import CommandRunner, InstallerError


ACTIVE_UNITS = {
    "devcloud-postgresql.service",
    "devcloud-controller.service",
    "devcloud-worker.service",
    "devcloud-ingress.path",
    "devcloud-update.path",
}
ENABLED_UNITS = {"devcloud-ingress.path", "devcloud-update.path"}


class FakeHost(CommandRunner):
    """Answers the commands the migration asks the host."""

    def __init__(self):
        super().__init__()
        self.user = "devcloud"
        self.group = "devcloud"
        self.active = set(ACTIVE_UNITS)

    def run(self, command, **_kwargs):
        self.commands.append(command)
        output, code = "", 0
        if command[:2] == ["systemctl", "show"]:
            output = "active" if command[-1] in self.active else "inactive"
        elif command[:2] == ["systemctl", "stop"] or command[:3] == ["systemctl", "disable", "--now"]:
            self.active.discard(command[-1])
        elif command[:2] == ["systemctl", "start"]:
            self.active.add(command[-1])
        elif command[:3] == ["podman", "container", "exists"]:
            code = 1
        elif command[:3] == ["podman", "container", "inspect"]:
            output = "true"
        elif command[:2] == ["systemctl", "is-enabled"]:
            output = "enabled" if command[-1] in ENABLED_UNITS else "generated"
        elif command[:2] == ["id", "-u"]:
            code = 0 if command[-1] == self.user else 1
        elif command[0] == "pgrep":
            code = 1
        elif command[:2] == ["getent", "group"]:
            code = 0 if command[-1] == self.group else 2
        elif command[:2] == ["usermod", "-l"]:
            self.user = command[2]
        elif command[:2] == ["groupmod", "-n"]:
            self.group = command[2]
        elif command[:3] == ["podman", "ps", "-a"]:
            output = "devcloud-3-my-notebook-1a2b3c4d\ndevcloud-controller\n"
        elif command[:2] == ["podman", "exec"] and "/proc/1/comm" in command:
            output = "postgres"
        return subprocess.CompletedProcess(command, code, output + "\n", "")

    def exists(self, command):
        return False


def write(root: Path, path: str, text: str) -> Path:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def legacy_all_in_one(root: Path) -> dict[str, str]:
    configuration = {
        "role": "all-in-one",
        "public_url": "https://aifactory.example.com",
        "controller_runtime": "container",
        "worker_runtime": "container",
        "database_mode": "bundled-postgresql",
        "service_user": "devcloud",
        "workspace_root": "/var/lib/devcloud/workspaces",
        "install_root": "/opt/devcloud",
        "state_root": "/var/lib/devcloud/installer",
        "releases_root": "/var/lib/devcloud/releases",
        "downloads_root": "/srv/devcloud-downloads",
    }
    files = {
        "/var/lib/devcloud/installer/install-state.json": json.dumps(
            {
                "schema": 1,
                "role": "all-in-one",
                "version": "3.29.2",
                "installed_at": "2026-10-01T00:00:00+00:00",
                "updated_at": "2026-11-01T00:00:00+00:00",
                "configuration": configuration,
            }
        ),
        "/etc/devcloud/controller.env": (
            "DATABASE_URL=postgresql+asyncpg://devcloud:pw@devcloud-postgresql/devcloud\n"
            "DEVCLOUD_DEPLOYMENT_ROLE=all-in-one\n"
            "DOWNLOADS_ROOT=/srv/devcloud-downloads\n"
            "SECRET_KEY=keep-me\n"
        ),
        "/etc/devcloud/worker.env": (
            "DEVCLOUD_NODE_ID=node-1\nDEVCLOUD_NODE_TOKEN=token\n"
            "STORAGE_ROOT=/var/lib/devcloud/workspaces\n"
        ),
        "/etc/devcloud/postgresql.env": (
            "POSTGRESQL_DATABASE=devcloud\nPOSTGRESQL_PASSWORD=pw\nPOSTGRESQL_USER=devcloud\n"
        ),
        "/var/lib/devcloud/releases/3.29.2-abc/.devcloud-release-id": "3.29.2-abc\n",
        "/var/lib/devcloud/workspaces/.devcloud-agent-registry.json": json.dumps(
            {"devcloud-3-my-notebook-1a2b3c4d": {"storage_path": "/var/lib/devcloud/workspaces/3/ws"}}
        ),
        "/srv/devcloud-downloads/releases/bundle.tar.gz": "bundle",
        "/etc/systemd/system/devcloud-update.path": "[Path]\n",
        "/etc/systemd/system/devcloud-update.service": "[Service]\n",
        "/etc/systemd/system/devcloud-ingress.path": "[Path]\n",
        "/etc/containers/systemd/devcloud-controller.container": "[Container]\n",
        "/etc/containers/systemd/devcloud-postgresql.container": "[Container]\n",
        "/usr/local/libexec/devcloud-apply-ingress": "#!/usr/bin/env python3\n",
        "/etc/nginx/conf.d/devcloud.conf": (
            "map $http_upgrade $devcloud_connection_upgrade {}\n"
            "ssl_certificate /etc/devcloud/tls/certificate.pem;\n"
        ),
        "/etc/hosts": "10.0.0.5\taifactory.example.com\t# devcloud-controller-fallback x\n",
    }
    for path, text in files.items():
        write(root, path, text)
    (root / "opt/devcloud").mkdir(parents=True)
    (root / "opt/devcloud/current").symlink_to(
        "../../var/lib/devcloud/releases/3.29.2-abc", target_is_directory=True
    )
    return files


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): (
            "-> " + os.readlink(path) if path.is_symlink() else path.read_text(encoding="utf-8")
        )
        for path in sorted(root.rglob("*"))
        if path.is_symlink() or path.is_file()
    }


def test_legacy_host_moves_to_aifactory_names_and_undo_restores_it(tmp_path):
    legacy_all_in_one(tmp_path)
    before = snapshot(tmp_path)
    runner = FakeHost()
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    state = json.loads(
        (tmp_path / "var/lib/devcloud/installer/install-state.json").read_text()
    )
    values = {**state["configuration"], "role": state["role"]}
    legacy_config = InstallConfig.from_dict(values)
    config = InstallConfig.from_dict(migrated_configuration(values))
    migration = LegacyMigration(engine, legacy_config, config, legacy_version="3.29.2")

    plan = engine.build_update_plan(config, migration=migration)
    keys = [step.key for step in plan.steps]
    assert keys.index("migrate-units") < keys.index("release") < keys.index("selinux")
    assert keys[-1] == "migrate-finish"

    for step in migration.steps():
        if step.key != "backup":
            step.apply()

    assert not needs_migration(engine.host_path)
    controller_env = (tmp_path / "etc/aifactory/controller.env").read_text()
    assert "AIFACTORY_DEPLOYMENT_ROLE=all-in-one" in controller_env
    assert "DOWNLOADS_ROOT=/srv/aifactory-downloads" in controller_env
    assert "SECRET_KEY=keep-me" in controller_env
    assert "DEVCLOUD_" not in controller_env
    assert "AIFACTORY_NODE_ID=node-1" in (tmp_path / "etc/aifactory/worker.env").read_text()
    assert "POSTGRESQL_USER=aifactory" in (tmp_path / "etc/aifactory/postgresql.env").read_text()
    migrated = json.loads(
        (tmp_path / "var/lib/aifactory/installer/install-state.json").read_text()
    )["configuration"]
    assert migrated["install_root"] == "/opt/aifactory"
    assert migrated["service_user"] == "aifactory"
    for old in ("etc/devcloud", "var/lib/devcloud", "opt/devcloud", "srv/devcloud-downloads"):
        assert (tmp_path / old).is_symlink()
    assert (tmp_path / "opt/aifactory/current").resolve() == (
        tmp_path / "var/lib/aifactory/releases/3.29.2-abc"
    ).resolve()
    registry = json.loads(
        (tmp_path / "var/lib/aifactory/workspaces/.aifactory-agent-registry.json").read_text()
    )
    assert registry["devcloud-3-my-notebook-1a2b3c4d"]["storage_path"] == (
        "/var/lib/aifactory/workspaces/3/ws"
    )
    nginx = (tmp_path / "etc/nginx/conf.d/aifactory.conf").read_text()
    assert "$aifactory_connection_upgrade" in nginx
    assert "/etc/aifactory/tls/certificate.pem" in nginx
    assert not (tmp_path / "etc/nginx/conf.d/devcloud.conf").exists()
    assert "# aifactory-controller-fallback" in (tmp_path / "etc/hosts").read_text()
    assert not (tmp_path / "etc/systemd/system/devcloud-update.path").exists()
    assert not (tmp_path / "etc/containers/systemd/devcloud-controller.container").exists()
    commands = [" ".join(command) for command in runner.commands]
    assert "podman rm -f devcloud-3-my-notebook-1a2b3c4d" in commands
    assert "podman rm -f devcloud-controller" not in commands
    assert any("ALTER DATABASE devcloud RENAME TO aifactory" in command for command in commands)
    assert "usermod -l aifactory -d /var/lib/aifactory devcloud" in commands
    assert "groupmod -n aifactory devcloud" in commands

    runner.commands.clear()
    migration.undo()

    assert snapshot(tmp_path) == before
    commands = [" ".join(command) for command in runner.commands]
    assert any("ALTER DATABASE aifactory RENAME TO devcloud" in command for command in commands)
    assert "usermod -l devcloud -d /var/lib/devcloud aifactory" in commands
    assert "systemctl enable devcloud-update.path" in commands
    for unit in ("devcloud-postgresql.service", "devcloud-controller.service"):
        assert f"systemctl start {unit}" in commands


def test_migration_refuses_before_any_change_when_a_new_directory_exists(tmp_path):
    legacy_all_in_one(tmp_path)
    (tmp_path / "etc/aifactory").mkdir()
    before = snapshot(tmp_path)
    runner = FakeHost()
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    state = json.loads(
        (tmp_path / "var/lib/devcloud/installer/install-state.json").read_text()
    )
    values = {**state["configuration"], "role": state["role"]}
    migration = LegacyMigration(
        engine,
        InstallConfig.from_dict(values),
        InstallConfig.from_dict(migrated_configuration(values)),
    )

    with pytest.raises(InstallerError, match="/etc/aifactory already exists"):
        migration.preflight()
    assert snapshot(tmp_path) == before


def _migration(tmp_path, runner):
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    state = json.loads(
        (tmp_path / "var/lib/devcloud/installer/install-state.json").read_text()
    )
    values = {**state["configuration"], "role": state["role"]}
    return engine, LegacyMigration(
        engine,
        InstallConfig.from_dict(values),
        InstallConfig.from_dict(migrated_configuration(values)),
    )


def test_move_started_by_the_old_updater_keeps_its_unit_until_it_exits(tmp_path):
    legacy_all_in_one(tmp_path)
    runner = FakeHost()
    runner.active.add("devcloud-update.service")
    engine, migration = _migration(tmp_path, runner)

    engine.build_update_plan(migration.config, migration=migration)
    for step in migration.steps():
        if step.key != "backup":
            step.apply()
    migration.commit()
    migration.finish()

    # Deleting or stopping the running unit would kill this update, and
    # starting the new watcher now would replay the old updater's request.
    assert (tmp_path / "etc/systemd/system/devcloud-update.service").is_file()
    assert engine.defer_update_watcher
    commands = [" ".join(command) for command in runner.commands]
    assert not any(command.endswith("stop devcloud-update.service") for command in commands)
    assert any(command.startswith("systemd-run --unit aifactory-update-handover") for command in commands)
    assert not (tmp_path / "var/lib/aifactory-migration").exists()


def test_a_committed_move_is_never_undone(tmp_path):
    legacy_all_in_one(tmp_path)
    runner = FakeHost()
    engine, migration = _migration(tmp_path, runner)
    for step in migration.steps():
        if step.key != "backup":
            step.apply()
    migration.commit()
    after = snapshot(tmp_path)

    # For example a crash during the cleanup that follows the saved state.
    migration.undo()

    assert (tmp_path / "etc/aifactory/controller.env").is_file()
    assert snapshot(tmp_path) == {
        path: value for path, value in after.items() if not path.startswith("var/lib/aifactory-migration")
    }
