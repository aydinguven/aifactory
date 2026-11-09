from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from app.installer.engine import InstallerEngine
from app.installer.layout import CURRENT, LEGACY
from app.installer.legacy_layout import (
    legacy_state_file,
    migrated_configuration,
    needs_migration,
    recover_interrupted_migration,
)
from app.installer.state import InstallationState
from app.installer.models import (
    DeploymentRole,
    InstallConfig,
    UpdateSourceType,
    WorkerRuntime,
)
from app.installer.platform import CommandRunner, InstallerError
from app.installer.progress import ProgressReporter
from app.installer.release import prepare_release
from app.installer.ui import InstallerUI
from app.installer.update_source import resolve_update_bundle
from app.platform_release import (
    load_platform_release,
    publish_platform_bundle,
    publish_platform_root,
)
from deploy.package_offline import PackageError, verify_staged_bundle


DEFAULT_STATE_ROOT = CURRENT.state_root
DEFAULT_KEYRING = Path(f"{CURRENT.etc_dir}/release-keyring.gpg")
CURRENT_REPOSITORY_URL = "https://github.com/aydinguven/aifactory.git"
LEGACY_REPOSITORY_URLS = {
    "https://github.com/aydinguven/devcloud.git",
    "https://github.com/aydinguven/devcloud",
}
# The update was applied, but publishing the worker bundle failed. The root
# updater reports this as a successful update with a warning.
EXIT_APPLIED_PUBLISH_FAILED = 3
REPUBLISH_HINT = (
    "Free disk space, then run: bash /opt/aifactory/current/deploy/aifactory-setup.sh "
    "--yes publish-worker-bundle"
)


def _verify_offline_release(root: Path) -> None:
    """Verify every embedded artifact before an offline update can load it."""
    manifest = root / "offline" / "MANIFEST.json"
    if not manifest.is_file():
        return
    try:
        verify_staged_bundle(root, expected_role="server")
    except PackageError as exc:
        raise InstallerError(f"Offline release verification failed: {exc}") from exc


def _worker_config_from_environment(
    role: DeploymentRole | None,
) -> InstallConfig | None:
    """Build a non-interactive worker config for the shell bootstrap.

    The shell bootstrap deliberately cannot depend on Python being present:
    aifactory-setup installs Python after subscription-manager is available.
    Environment variables carry the enrollment answers across that boundary.
    """
    if role != DeploymentRole.WORKER:
        return None
    names = {
        "controller_url": "AIFACTORY_INSTALL_CONTROLLER_URL",
        "worker_id": "AIFACTORY_INSTALL_WORKER_ID",
        "enrollment_token_file": "AIFACTORY_INSTALL_TOKEN_FILE",
    }
    values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    if not any(values.values()):
        return None
    missing = [name for key, name in names.items() if not values[key]]
    if missing:
        raise InstallerError(
            "Incomplete non-interactive worker enrollment: "
            + ", ".join(missing)
        )
    runtime_value = (
        os.environ.get("AIFACTORY_INSTALL_WORKER_RUNTIME", "").strip()
        or WorkerRuntime.CONTAINER.value
    )
    try:
        worker_runtime = WorkerRuntime(runtime_value)
    except ValueError as exc:
        raise InstallerError(
            "AIFACTORY_INSTALL_WORKER_RUNTIME must be container or native"
        ) from exc
    return InstallConfig(
        role=role,
        controller_url=values["controller_url"],
        controller_fallback_ipv4=os.environ.get(
            "AIFACTORY_INSTALL_CONTROLLER_FALLBACK_IPV4", ""
        ).strip(),
        agent_ca_file=os.environ.get(
            "AIFACTORY_INSTALL_AGENT_CA_FILE", ""
        ).strip(),
        worker_id=values["worker_id"],
        enrollment_token_file=values["enrollment_token_file"],
        worker_name=(
            os.environ.get("AIFACTORY_INSTALL_WORKER_NAME", "").strip()
            or socket.gethostname()
        ),
        workspace_root=(
            os.environ.get("AIFACTORY_INSTALL_WORKSPACE_ROOT", "").strip()
            or "/var/lib/aifactory/workspaces"
        ),
        preload_images=os.environ.get(
            "AIFACTORY_INSTALL_PRELOAD_IMAGES", "false"
        ).strip().lower()
        not in {"0", "false", "no"},
        worker_runtime=worker_runtime,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aifactory-setup",
        description="Interactive installer and lifecycle manager for AI Factory",
    )
    parser.add_argument("--dry-run", action="store_true", help="show and record commands without changing the host")
    parser.add_argument("--yes", action="store_true", help="accept the displayed plan")
    parser.add_argument("--filesystem-root", default="/", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")

    install = commands.add_parser("install", help="install a supported deployment role")
    install.add_argument(
        "role",
        nargs="?",
        choices=[role.value for role in DeploymentRole],
    )
    install.add_argument("--answers", type=Path, help="JSON answer file")

    update = commands.add_parser("update", help="apply a connected or uploaded release")
    update.add_argument("--bundle", type=Path)
    update.add_argument(
        "--source-type", choices=[item.value for item in UpdateSourceType]
    )
    update.add_argument("--repository", help="Git repository URL or local path")
    update.add_argument("--ref", help="Git branch, tag, or commit")
    update.add_argument(
        "--token-file", type=Path, help="Bearer token file for a private HTTPS bundle"
    )
    update.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="allow an unsigned source ZIP after explicit operator review",
    )
    update.add_argument(
        "--keyring",
        type=Path,
        default=None,
        help=f"release verification keyring (default: {DEFAULT_KEYRING})",
    )
    # Internal: apply an already verified, extracted release with this code.
    update.add_argument("--prepared-root", type=Path, help=argparse.SUPPRESS)

    commands.add_parser("repair", help="repair configuration, permissions, SELinux, and services")
    commands.add_parser(
        "publish-worker-bundle",
        help="republish the installed release for worker upgrades",
    )
    commands.add_parser("status", help="show installation and service state")
    backup = commands.add_parser("backup", help="back up configuration and data")
    backup.add_argument("--output", type=Path)
    backup.add_argument(
        "--include-workspaces",
        action="store_true",
        help="include persistent workspace files (may be large)",
    )
    restore = commands.add_parser("restore", help="restore a verified backup")
    restore.add_argument("--bundle", type=Path)
    uninstall = commands.add_parser("uninstall", help="remove installed services")
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="also permanently remove configuration and data",
    )
    return parser


def _should_delegate(target_root: Path, args) -> bool:
    """Run the target release's installer unless it is this very code."""
    if args.dry_run or "1" in {
        os.environ.get("AIFACTORY_INSTALLER_DELEGATED"),
        # Set by the devcloud installer (before 3.49.1).
        os.environ.get("DEVCLOUD_INSTALLER_DELEGATED"),
    }:
        return False
    own_root = Path(__file__).resolve().parents[2]
    return (
        target_root.resolve() != own_root
        and (target_root / "app" / "installer" / "cli.py").is_file()
    )


def _delegate_update(target_root: Path, args) -> int:
    import subprocess
    import sys

    command = [sys.executable, "-m", "app.installer"]
    if args.yes:
        command.append("--yes")
    if args.filesystem_root != "/":
        command.extend(["--filesystem-root", str(args.filesystem_root)])
    command.extend(["update", "--prepared-root", str(target_root.resolve())])
    env = {
        **os.environ,
        "PYTHONPATH": str(target_root.resolve()),
        "AIFACTORY_INSTALLER_DELEGATED": "1",
    }
    return subprocess.run(command, cwd="/", env=env, check=False).returncode


def _config_from_state(engine: InstallerEngine) -> InstallConfig:
    state = engine.current_state(DEFAULT_STATE_ROOT)
    if state is None:
        raise InstallerError("No managed AI Factory installation was found")
    config = InstallConfig.from_dict({**state.configuration, "role": state.role})
    # The Git repository was renamed after 3.49.2; GitHub redirects the old
    # URL, but new installs and the admin form use the new one.
    if config.update_source.rstrip("/") in LEGACY_REPOSITORY_URLS:
        config.update_source = CURRENT_REPOSITORY_URL
    return config


def _legacy_state(engine: InstallerEngine) -> InstallationState | None:
    return InstallationState.load(legacy_state_file(engine.host_path))


def _legacy_configs(engine: InstallerEngine) -> tuple[InstallConfig, InstallConfig]:
    """Installed devcloud configuration and the same one with the new names."""
    state = _legacy_state(engine) or engine.current_state(DEFAULT_STATE_ROOT)
    if state is None:
        raise InstallerError("No managed AI Factory installation was found")
    values = {**state.configuration, "role": state.role}
    return (
        InstallConfig.from_dict(values),
        InstallConfig.from_dict(migrated_configuration(values)),
    )


def _execute_plan(plan, ui: InstallerUI, *, assume_yes: bool) -> bool:
    ui.show_plan(plan.title, plan.descriptions)
    if not assume_yes and not ui.confirm("Proceed with this plan?", False):
        ui.write("No changes were made.")
        return False
    plan.execute()
    ui.write(f"\n{plan.title} completed successfully.")
    return True


def _interactive_command(ui: InstallerUI, installed: bool) -> str:
    options = [
        ("install", "Install Controller, All-in-one, or Worker"),
        ("update", "Update existing installation from a release bundle"),
        ("repair", "Repair or reconfigure existing installation"),
        ("status", "Show installation status"),
        ("backup", "Back up configuration and data"),
        ("restore", "Restore a backup"),
        ("uninstall", "Uninstall"),
        ("quit", "Quit"),
    ]
    if not installed:
        options = [options[0], options[-1]]
    return ui.choose("AI Factory Setup", options)


def main(argv: list[str] | None = None) -> int:
    # Hardened hosts often give root a 027/077 umask. The release tree, venv,
    # and /opt and /var/lib parents created here must stay traversable by the
    # unprivileged service user; secrets are written with explicit modes.
    os.umask(0o022)
    parser = _parser()
    args = parser.parse_args(argv)
    ui = InstallerUI()
    runner = CommandRunner(dry_run=args.dry_run)
    engine = InstallerEngine(
        filesystem_root=Path(args.filesystem_root),
        runner=runner,
    )

    try:
        command = args.command
        if command is None:
            command = _interactive_command(
                ui,
                engine.current_state(DEFAULT_STATE_ROOT) is not None
                or needs_migration(engine.host_path),
            )
            if command == "quit":
                return 0

        if command == "install":
            if getattr(args, "answers", None):
                config = InstallConfig.from_json_file(args.answers)
            else:
                role = DeploymentRole(args.role) if getattr(args, "role", None) else None
                config = (
                    _worker_config_from_environment(role)
                    or ui.collect_install_config(role)
                )
            applied = _execute_plan(
                engine.build_install_plan(config),
                ui,
                assume_yes=args.yes,
            )
            if (
                applied
                and not args.dry_run
                and config.installs_controller
                and (engine.project_root / "platform-release.json").is_file()
            ):
                published = publish_platform_root(
                    engine.project_root,
                    engine.host_path(config.downloads_root),
                )
                ui.write(
                    f"Worker installation artifact published: {published.name}"
                )
            return 0

        if command == "status":
            state_root = (
                LEGACY.state_root if needs_migration(engine.host_path) else DEFAULT_STATE_ROOT
            )
            ui.write(json.dumps(engine.status(state_root), indent=2))
            return 0

        if not args.dry_run and recover_interrupted_migration(
            engine, lambda: _legacy_configs(engine)
        ):
            ui.write("An interrupted move to the aifactory names was undone.")
        if needs_migration(engine.host_path):
            # 3.50.1 no longer moves hosts; 3.49.x does, and is still offered
            # to them through the frozen devcloud-update-channel.json.
            raise InstallerError(
                "This host still uses the devcloud names. Update it to 3.49.2 "
                "first; that update moves it to the aifactory names."
            )
        config = _config_from_state(engine)

        if command == "publish-worker-bundle":
            if not config.installs_controller:
                raise InstallerError("Only a controller publishes worker update bundles")
            release_root = engine.host_path(config.install_root) / "current"
            published = publish_platform_root(
                release_root.resolve(), engine.host_path(config.downloads_root)
            )
            ui.write(f"Worker update artifact published: {published.name}")
            return 0

        if command == "repair":
            _execute_plan(
                engine.build_repair_plan(config),
                ui,
                assume_yes=args.yes,
            )
            return 0

        if command == "backup":
            default_output = Path.cwd() / (
                "aifactory-backup-"
                + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                + ".tar.gz"
            )
            output = getattr(args, "output", None)
            if output is None and args.command is None:
                output = Path(ui.ask("Backup output", str(default_output)))
            output = output or default_output
            include_workspaces = bool(
                getattr(args, "include_workspaces", False)
            )
            if args.command is None:
                include_workspaces = ui.confirm(
                    "Include persistent workspace data?", False
                )
            _execute_plan(
                engine.build_backup_plan(
                    config,
                    output=output,
                    include_workspaces=include_workspaces,
                ),
                ui,
                assume_yes=args.yes,
            )
            return 0

        if command == "restore":
            bundle = getattr(args, "bundle", None)
            if bundle is None:
                bundle = Path(ui.ask("Backup archive"))
            if not args.yes:
                expected = config.worker_name or config.role.value
                typed = ui.ask(
                    f"Restore replaces managed state. Type {expected!r} to continue"
                )
                if typed != expected:
                    ui.write("Confirmation did not match; no changes were made.")
                    return 1
            _execute_plan(
                engine.build_restore_plan(config, archive=bundle.resolve()),
                ui,
                assume_yes=True,
            )
            return 0

        if command == "update" and getattr(args, "prepared_root", None):
            update_engine = InstallerEngine(
                project_root=args.prepared_root.resolve(),
                filesystem_root=Path(args.filesystem_root),
                runner=runner,
            )
            applied = _execute_plan(
                update_engine.build_update_plan(
                    config
                ),
                ui,
                assume_yes=args.yes,
            )
            return 0 if applied else 1

        if command == "update":
            bundle = getattr(args, "bundle", None)
            source_type = getattr(args, "source_type", None)
            repository = getattr(args, "repository", None)
            update_ref = getattr(args, "ref", None)
            token_file = getattr(args, "token_file", None)
            if args.command is None:
                source_type = ui.choose(
                    "Update source",
                    [
                        (UpdateSourceType.GIT.value, "Configured Git release channel"),
                        (UpdateSourceType.BUNDLE.value, "Local/uploaded platform bundle"),
                    ],
                    default=(
                        1
                        if config.update_source_type == UpdateSourceType.GIT
                        else 2
                    ),
                )
            if bundle is not None:
                source_type = UpdateSourceType.BUNDLE.value
                location = str(bundle)
            elif source_type == UpdateSourceType.GIT.value or (
                source_type is None
                and config.update_source_type == UpdateSourceType.GIT
            ):
                source_type = UpdateSourceType.GIT.value
                repository = repository or config.update_source
                update_ref = update_ref or config.update_ref
                if args.command is None:
                    repository = ui.ask(
                        "Git repository URL or local path", repository
                    )
                    update_ref = ui.ask("Release branch/tag/commit", update_ref)
                location = repository or ""
                if token_file is None and config.update_token_file:
                    token_file = Path(config.update_token_file)
            else:
                source_type = UpdateSourceType.BUNDLE.value
                location = ui.ask("Release ZIP or tar archive") if args.command is None else ""
            if not location:
                raise InstallerError(
                    "Update requires --bundle or a configured Git repository"
                )
            allow_unsigned = bool(getattr(args, "allow_unsigned", False))
            if args.command is None:
                allow_unsigned = ui.confirm(
                    "This is an unsigned source ZIP?", False
                )
            if allow_unsigned and not args.yes:
                ui.write(
                    "WARNING: unsigned source archives have no publisher authenticity guarantee."
                )
                if not ui.confirm("Continue with this unsigned source?", False):
                    return 0
            keyring = getattr(args, "keyring", None)
            if keyring is None:
                keyring = DEFAULT_KEYRING
            keyring = engine.host_path(keyring)
            with resolve_update_bundle(
                source_type=source_type,
                location=location,
                ref=update_ref or "stable",
                runner=runner,
                token_file=token_file,
            ) as resolved_bundle:
                ProgressReporter.from_environment().verify()
                with prepare_release(
                    resolved_bundle,
                    runner=runner,
                    keyring=keyring,
                    require_signature=not allow_unsigned,
                ) as prepared:
                    _verify_offline_release(prepared.root)
                    ProgressReporter.from_environment().verify("Bundle doğrulandı")
                    platform_release = (
                        load_platform_release(prepared.root)
                        if (prepared.root / "platform-release.json").is_file()
                        else None
                    )
                    delegated = _should_delegate(prepared.root, args)
                    if delegated:
                        # The target release renders its own templates and
                        # units; the installed code may predate them.
                        applied = _delegate_update(prepared.root, args) == 0
                    else:
                        update_engine = InstallerEngine(
                            project_root=prepared.root,
                            filesystem_root=Path(args.filesystem_root),
                            runner=runner,
                        )
                        applied = _execute_plan(
                            update_engine.build_update_plan(
                                config,
                            ),
                            ui,
                            assume_yes=args.yes,
                        )
                    if delegated and not applied:
                        raise InstallerError(
                            "The target release installer did not apply the update; "
                            "see its output above."
                        )
                    if applied and platform_release and config.installs_controller:
                        try:
                            published = publish_platform_bundle(
                                resolved_bundle,
                                engine.host_path(config.downloads_root),
                            )
                        except (InstallerError, OSError) as exc:
                            # The platform is updated; only workers cannot
                            # fetch it yet. Say so instead of "failed".
                            ui.write(
                                "WARNING: The platform update was applied, but the worker "
                                f"update bundle could not be published: {exc}\n"
                                f"{REPUBLISH_HINT}"
                            )
                            return EXIT_APPLIED_PUBLISH_FAILED
                        ui.write(
                            f"Worker update artifact published: {published.name}"
                        )
            return 0

        if command == "uninstall":
            purge = bool(getattr(args, "purge", False))
            if args.command is None:
                purge = ui.confirm(
                    "Permanently remove configuration and managed data?", False
                )
            if purge and not args.yes:
                expected = config.worker_name or config.role.value
                typed = ui.ask(
                    f"Type {expected!r} to permanently delete managed data"
                )
                if typed != expected:
                    ui.write("Confirmation did not match; no changes were made.")
                    return 1
            _execute_plan(
                engine.build_uninstall_plan(config, purge_data=purge),
                ui,
                assume_yes=args.yes or purge,
            )
            return 0

        parser.error(f"Unsupported command: {command}")
    except (InstallerError, ValueError, OSError) as exc:
        ui.write(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
