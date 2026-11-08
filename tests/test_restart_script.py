from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESTART_SCRIPT = PROJECT_ROOT / "deploy" / "restart.sh"


def test_restart_script_reloads_workers_and_is_health_checked():
    script = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert 'systemctl_cmd kill --kill-who=main --signal=SIGHUP' in script
    assert "systemctl_cmd start --no-block" in script
    assert "AIFACTORY_RELOAD_TIMEOUT_SECONDS" in script
    assert "AIFACTORY_START_TIMEOUT_SECONDS" in script
    assert "http://127.0.0.1:8000/login" in script
    assert 'journalctl -u "${SERVICE_NAME}"' in script
    assert "systemctl_cmd stop" not in script


def test_restart_script_preserves_workspace_processes():
    script = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert 'worker_pids "${OLD_MAIN_PID}"' in script
    assert "multiprocessing.resource_tracker" in script
    assert "podman rm" not in script
    assert "podman stop" not in script
    assert "pkill" not in script
    assert "killall" not in script


def test_service_supports_standard_systemd_reload():
    service = (PROJECT_ROOT / "deploy" / "aifactory.service").read_text(encoding="utf-8")

    assert "ExecReload=/bin/kill -HUP $MAINPID" in service
    assert ".venv/bin/python -m uvicorn" in service
    assert ".venv/bin/uvicorn" not in service


def test_only_worker_service_prepares_rootless_runtime_directory():
    controller = (PROJECT_ROOT / "deploy" / "aifactory.service").read_text(
        encoding="utf-8"
    )
    worker = (PROJECT_ROOT / "deploy" / "aifactory-worker.service").read_text(
        encoding="utf-8"
    )

    assert "XDG_RUNTIME_DIR" not in controller
    assert "ExecStartPre" not in controller
    assert "XDG_RUNTIME_DIR" in worker


def test_installed_worker_unit_uses_the_service_accounts_runtime_directory(tmp_path):
    # systemd's %U is 0 for system units regardless of User=, which pointed
    # the rootless worker at root's /run/user/0 and broke every podman call.
    from types import SimpleNamespace

    from app.installer.engine import InstallerEngine
    from app.installer.models import DeploymentRole, WorkerRuntime
    from app.installer.platform import CommandRunner
    from tests.test_installer import config

    runner = CommandRunner()
    runner.run = lambda command, **_kwargs: SimpleNamespace(
        returncode=0, stdout="982\n" if command[:2] == ["id", "-u"] else "", stderr=""
    )
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.WORKER)
    candidate.worker_runtime = WorkerRuntime.NATIVE

    engine._install_services(candidate)

    unit = (tmp_path / "etc/systemd/system/aifactory-worker.service").read_text(
        encoding="utf-8"
    )
    assert "Environment=XDG_RUNTIME_DIR=/run/user/982" in unit
    assert (
        "ExecStartPre=+/usr/bin/install -d -o aifactory -g aifactory -m 0700 /run/user/982"
        in unit
    )
    assert "%U" not in unit and "{{UID}}" not in unit


def test_offline_worker_installer_requires_worker_manifest_and_enrollment():
    installer = (PROJECT_ROOT / "deploy" / "deploy_worker_offline.sh").read_text(
        encoding="utf-8"
    )

    assert "--expected-role worker" in installer
    assert "AIFACTORY_MASTER_URL" in installer
    assert "AIFACTORY_NODE_ID" in installer
    assert "AIFACTORY_NODE_TOKEN" in installer
    assert "aifactory-worker.service" in installer
    assert "offline/worker-images" in installer
    assert "aifactory-worker.container" in installer
    assert "podman.socket" in installer


def test_master_and_worker_offline_installers_bootstrap_system_rpms():
    for relative_path in (
        "deploy/deploy_offline.sh",
        "deploy/deploy_worker_offline.sh",
    ):
        installer = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        bootstrap_position = installer.index("install_offline_system_packages.sh")
        connected_install_position = installer.index("dnf install -y")
        podman_check_position = installer.index("command -v podman")
        assert bootstrap_position < connected_install_position < podman_check_position


def test_system_rpm_installer_is_offline_and_distribution_scoped():
    installer = (
        PROJECT_ROOT / "deploy" / "install_offline_system_packages.sh"
    ).read_text(encoding="utf-8")

    assert "rocky|rhel" in installer
    assert '[[ "${MAJOR_VERSION}" == "9" || "${MAJOR_VERSION}" == "10" ]]' in installer
    assert "sha256sum -c SHA256SUMS" in installer
    assert '"--disablerepo=*"' in installer
    assert '"--disable-repo=*"' in installer
    assert '"--enablerepo=aifactory-offline"' in installer
    assert '"--enable-repo=aifactory-offline"' in installer
    assert '"--nogpgcheck"' in installer
    assert '"--no-gpgchecks"' in installer
    assert "--repofrompath=aifactory-offline,file://" in installer
    assert "REQUESTED_PACKAGES" in installer
    assert "repodata/repomd.xml" in installer
    assert "subscription-manager" in installer
    assert "install -y subscription-manager" in installer
    assert 'install -y "${REQUESTED_PACKAGES[@]}"' not in installer


def test_unified_setup_prefers_and_verifies_offline_artifacts_before_ui():
    setup = (PROJECT_ROOT / "deploy" / "aifactory-setup.sh").read_text(
        encoding="utf-8"
    )

    offline_install = setup.index("install_offline_system_packages.sh")
    connected_install = setup.index("dnf install -y subscription-manager")
    assert offline_install < connected_install
    assert "AIFACTORY_OFFLINE_INSTALL=1" in setup
    assert '--verify "${PROJECT_DIR}"' in setup
    assert "--check-runtime" in setup
    assert "Register this VM with Satellite/Foreman now" in setup
    assert "subscription-manager identity" in setup
    assert '\ncd /\nexec "${PYTHON_BIN}" -m app.installer' in setup


def test_installers_use_bounded_restart_helper():
    for relative_path in ("deploy/deploy.sh", "deploy/deploy_offline.sh"):
        installer = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert 'bash "${PROJECT_DIR}/deploy/restart.sh"' in installer
        assert "systemctl restart aifactory" not in installer


def test_clean_installers_configure_nginx_ingress_on_port_80():
    online = (PROJECT_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    offline = (PROJECT_ROOT / "deploy" / "deploy_offline.sh").read_text(
        encoding="utf-8"
    )
    ingress_installer = (
        PROJECT_ROOT / "deploy" / "install_ingress.sh"
    ).read_text(encoding="utf-8")
    path_unit = (PROJECT_ROOT / "deploy" / "aifactory-ingress.path").read_text(
        encoding="utf-8"
    )
    ingress_unit = (
        PROJECT_ROOT / "deploy" / "aifactory-ingress.service"
    ).read_text(encoding="utf-8")

    assert "nginx" in next(line for line in online.splitlines() if "apt-get install" in line)
    assert "nginx" in next(line for line in online.splitlines() if "dnf install" in line)
    assert 'install_ingress.sh" "$USER"' in online
    assert 'install_ingress.sh" root' in offline
    assert "--add-service=http" in (
        PROJECT_ROOT / "deploy" / "apply_ingress.py"
    ).read_text(encoding="utf-8")
    assert "aifactory-ingress.path" in ingress_installer
    assert "setsebool -P httpd_can_network_connect on" in ingress_installer
    assert "PathChanged=/var/lib/aifactory/ingress/apply.request" in path_unit
    assert "ProtectSystem=strict" in ingress_unit
    writable_paths = next(
        line for line in ingress_unit.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "/var/log/nginx" in writable_paths
    assert "/run" in writable_paths
    assert "NOPASSWD" not in ingress_installer


def test_installers_keep_distribution_specific_python_packages():
    installer = (PROJECT_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    apt_install = next(line for line in installer.splitlines() if "apt-get install" in line)
    dnf_install = next(line for line in installer.splitlines() if "dnf install" in line)

    assert "python3-venv" in apt_install
    assert "python3-venv" not in dnf_install


def test_offline_installer_requires_documented_sudo_and_does_not_alias_runtimes():
    installer = (PROJECT_ROOT / "deploy" / "deploy_offline.sh").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "AIRGAP.md").read_text(encoding="utf-8")

    assert "sudo bash deploy/aifactory-setup.sh" in readme
    assert "sudo bash deploy/aifactory-setup.sh" in runbook
    assert "ln -sf /usr/bin/runc /usr/bin/crun" not in installer


def test_platform_updater_is_atomic_and_uses_bounded_restart_helper():
    script = (PROJECT_ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "flock -n 9" in script
    assert "sudo -n true" in script
    assert 'TARGET_BRANCH="${AIFACTORY_UPDATE_BRANCH:-main}"' in script
    assert 'git fetch origin "${TARGET_BRANCH}"' in script
    assert 'git switch "${TARGET_BRANCH}"' in script
    assert 'git merge --ff-only "origin/${TARGET_BRANCH}"' in script
    assert "git status --porcelain --untracked-files=no" in script
    assert "systemctl show --property User --value aifactory" in script
    assert ".venv/bin/python -m pip install" in script
    assert "AIFACTORY_INGRESS_APPLY_INITIAL=0" in script
    assert 'install_ingress.sh" "${SERVICE_USER}"' in script
    assert "platform-update-restart.log" in script
    assert '"${PROJECT_DIR}/deploy/restart.sh"' in script
    assert "systemctl restart aifactory" not in script


def test_platform_update_api_streams_script_and_checks_exit_code():
    routes = (PROJECT_ROOT / "app/routes/admin/system.py").read_text(encoding="utf-8")

    assert 'project_dir / "deploy" / "update.sh"' in routes
    assert "stderr=asyncio.subprocess.STDOUT" in routes
    assert "if return_code != 0" in routes
    assert "systemctl restart aifactory" not in routes
