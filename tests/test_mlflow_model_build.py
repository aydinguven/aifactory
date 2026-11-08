"""Regression coverage for the MLflow serving image build path.

The build ran as ``mlflow models generate-dockerfile`` resolved from PATH, but
workers are started as ``<project>/.venv/bin/python -m app.worker_agent`` under a
systemd unit whose PATH deliberately excludes the virtualenv. The bare binary
name was therefore unresolvable on every VM install, and because the resulting
``FileNotFoundError`` escaped the build helper the deployment only ever reported
"Model deployment başarısız oldu." with no log to explain it.
"""

import asyncio
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.mlflow_deployment import (
    MlflowDeployment,
    MlflowDeploymentEvent,
    MlflowDeploymentStatus,
    MlflowModelBuild,
    MlflowModelBuildStatus,
)
from app.models.user import User
from app.orchestrator import mlflow_deployment_service as deployment_service
from app.orchestrator import podman_service as podman_module
from app.orchestrator.podman_service import (
    MlflowCliUnavailable,
    PodmanService,
    resolve_mlflow_cli,
)
from app.routes.mlflow_routes import _deployment_out

MODEL_URI = "models:/cashflow-ai/28"
IMAGE_TAG = "registry.internal:5000/devcloud-mlflow-cashflow-ai-v28:abc123def456"
TRACKING_ENVIRONMENT = {"MLFLOW_TRACKING_URI": "https://managed-mlflow.internal"}


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _build_service() -> PodmanService:
    service = PodmanService(podman_bin="podman")
    # The suite forces mock mode globally; the real build path is under test.
    service._mock_mode = False
    return service


def test_resolve_mlflow_cli_prefers_binary_on_path():
    resolved = resolve_mlflow_cli("mlflow", which=lambda name: "/usr/bin/mlflow")

    assert resolved == ["/usr/bin/mlflow"]


def test_resolve_mlflow_cli_falls_back_to_interpreter_sibling(tmp_path):
    """A virtualenv console script must be found even when PATH omits it."""
    sibling = _executable(tmp_path / "mlflow")

    resolved = resolve_mlflow_cli(
        "mlflow",
        which=lambda name: None,
        interpreter=str(tmp_path / "python"),
        module_available=lambda name: False,
    )

    assert resolved == [str(sibling)]


def test_resolve_mlflow_cli_falls_back_to_python_module(tmp_path):
    resolved = resolve_mlflow_cli(
        "mlflow",
        which=lambda name: None,
        interpreter=str(tmp_path / "python"),
        module_available=lambda name: True,
    )

    assert resolved == [str(tmp_path / "python"), "-m", "mlflow"]


def test_resolve_mlflow_cli_honours_absolute_override(tmp_path):
    override = _executable(tmp_path / "mlflow-custom")

    resolved = resolve_mlflow_cli(
        str(override),
        which=lambda name: None,
        interpreter=str(tmp_path / "python"),
        module_available=lambda name: False,
    )

    assert resolved == [str(override)]


def test_resolve_mlflow_cli_error_lists_every_attempted_location(tmp_path):
    with pytest.raises(MlflowCliUnavailable) as failure:
        resolve_mlflow_cli(
            "mlflow",
            which=lambda name: None,
            interpreter=str(tmp_path / "python"),
            module_available=lambda name: False,
        )

    message = str(failure.value)
    assert str(tmp_path / "mlflow") in message
    assert "-m mlflow" in message
    assert "requirements.txt" in message


@pytest.mark.asyncio
async def test_build_reports_missing_cli_instead_of_raising(monkeypatch):
    """A missing CLI must fail the build with a log, not crash the agent."""

    def unavailable(*args, **kwargs):
        raise MlflowCliUnavailable("MLflow komut satırı aracı bulunamadı.")

    monkeypatch.setattr(podman_module, "resolve_mlflow_cli", unavailable)

    success, logs = await _build_service().build_mlflow_model_image(
        model_uri=MODEL_URI,
        image_tag=IMAGE_TAG,
        mlflow_environment=dict(TRACKING_ENVIRONMENT),
    )

    assert success is False
    assert "MLflow komut satırı aracı bulunamadı." in logs


@pytest.mark.asyncio
async def test_build_uses_resolved_cli_argv_not_bare_binary_name(monkeypatch, tmp_path):
    recorded: list[tuple[str, ...]] = []
    interpreter = str(tmp_path / "python")

    monkeypatch.setattr(
        podman_module,
        "resolve_mlflow_cli",
        lambda *args, **kwargs: [interpreter, "-m", "mlflow"],
    )

    class FakeProcess:
        returncode = 1

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        recorded.append(args)
        kwargs["stdout"].write(b"could not reach tracking server\n")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    success, logs = await _build_service().build_mlflow_model_image(
        model_uri=MODEL_URI,
        image_tag=IMAGE_TAG,
        mlflow_environment=dict(TRACKING_ENVIRONMENT),
    )

    assert success is False
    assert recorded[0][:4] == (
        interpreter,
        "-m",
        "mlflow",
        "models",
    )
    assert "could not reach tracking server" in logs
    assert "generate-dockerfile" in logs


@pytest.mark.asyncio
async def test_build_keeps_diagnostics_when_step_cannot_launch(monkeypatch):
    """An exec failure must be reported as build output, never as an exception."""
    monkeypatch.setattr(
        podman_module, "resolve_mlflow_cli", lambda *args, **kwargs: ["/missing/mlflow"]
    )

    async def refuse_to_launch(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_to_launch)

    success, logs = await _build_service().build_mlflow_model_image(
        model_uri=MODEL_URI,
        image_tag=IMAGE_TAG,
        mlflow_environment=dict(TRACKING_ENVIRONMENT),
    )

    assert success is False
    assert "/missing/mlflow" in logs
    assert "başlatılamadı" in logs


@pytest.mark.asyncio
async def test_build_fails_when_mlflow_emits_no_dockerfile(monkeypatch):
    """A silent generate-dockerfile must not turn into a cryptic podman error."""
    monkeypatch.setattr(
        podman_module, "resolve_mlflow_cli", lambda *args, **kwargs: ["/usr/bin/mlflow"]
    )

    class FakeProcess:
        returncode = 0

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        kwargs["stdout"].write(b"Downloaded model artifacts\n")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def unexpected_podman(*args, **kwargs):
        raise AssertionError("podman build must not run without a Dockerfile")

    service = _build_service()
    monkeypatch.setattr(service, "run_cmd", unexpected_podman)

    success, logs = await service.build_mlflow_model_image(
        model_uri=MODEL_URI,
        image_tag=IMAGE_TAG,
        mlflow_environment=dict(TRACKING_ENVIRONMENT),
    )

    assert success is False
    assert "Dockerfile üretilmedi" in logs


@pytest.mark.asyncio
async def test_build_redacts_registry_password_from_push_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        podman_module, "resolve_mlflow_cli", lambda *args, **kwargs: ["/usr/bin/mlflow"]
    )

    class FakeProcess:
        returncode = 0

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        context = Path(args[-1])
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        kwargs["stdout"].write(b"Generated Dockerfile\n")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    service = _build_service()

    async def fake_run_cmd(*args, **kwargs):
        if args[0] == "push":
            return 125, "", "unauthorized for secret-registry-password"
        return 0, "COMMIT", ""

    monkeypatch.setattr(service, "run_cmd", fake_run_cmd)

    success, logs = await service.build_mlflow_model_image(
        model_uri=MODEL_URI,
        image_tag=IMAGE_TAG,
        mlflow_environment=dict(TRACKING_ENVIRONMENT),
        registry_username="aifactory",
        registry_password="secret-registry-password",
    )

    assert success is False
    assert "secret-registry-password" not in logs
    assert "<redacted>" in logs
    assert "Model Container Registry" in logs


@pytest.mark.asyncio
async def test_failure_detail_is_appended_to_the_progress_log(db_session):
    """The progress log must explain a failure, not just announce one."""
    user = User(
        username="deployer",
        email="deployer@test.com",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()
    deployment = MlflowDeployment(
        user_id=user.id,
        name="cashflow-ai",
        model_name="cashflow-ai",
        model_version="28",
        flavor_id="t1.small",
        access_token_hash="0" * 64,
        status=MlflowDeploymentStatus.FAILED,
    )
    db_session.add(deployment)
    await db_session.commit()

    await deployment_service._append_failure_detail(
        db_session,
        deployment,
        RuntimeError("podman build: no such file or directory"),
    )

    events = (
        await db_session.execute(
            select(MlflowDeploymentEvent)
            .where(MlflowDeploymentEvent.deployment_id == deployment.id)
            .order_by(MlflowDeploymentEvent.sequence)
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].level == "error"
    assert "podman build: no such file or directory" in events[0].message


@pytest.mark.asyncio
async def test_failure_detail_keeps_the_tail_of_a_long_build_log(db_session):
    user = User(username="tailuser", email="tail@test.com", hashed_password="x")
    db_session.add(user)
    await db_session.flush()
    deployment = MlflowDeployment(
        user_id=user.id,
        name="tail",
        model_name="tail",
        model_version="1",
        flavor_id="t1.small",
        access_token_hash="0" * 64,
        status=MlflowDeploymentStatus.FAILED,
    )
    db_session.add(deployment)
    await db_session.commit()

    await deployment_service._append_failure_detail(
        db_session,
        deployment,
        RuntimeError("x" * 5000 + "THE ACTUAL ERROR"),
    )

    event = (
        await db_session.execute(
            select(MlflowDeploymentEvent).where(
                MlflowDeploymentEvent.deployment_id == deployment.id
            )
        )
    ).scalar_one()
    assert "THE ACTUAL ERROR" in event.message
    assert "son 900 karakter" in event.message


def test_deployment_detail_exposes_the_build_log():
    now = datetime.now(timezone.utc)
    deployment = MlflowDeployment(
        id="d1",
        user_id=1,
        build_id="b1",
        name="cashflow-ai",
        model_name="cashflow-ai",
        model_version="28",
        run_id="run-1",
        source_uri="s3://models/cashflow-ai/28",
        flavor_id="t1.small",
        auto_stop_minutes=0,
        gunicorn_workers=1,
        access_token_hash="0" * 64,
        status=MlflowDeploymentStatus.FAILED,
        status_message="Model deployment başarısız oldu.",
        error_message="Image build başarısız.",
        created_at=now,
        updated_at=now,
    )
    build = MlflowModelBuild(
        id="b1",
        user_id=1,
        model_name="cashflow-ai",
        model_version="28",
        model_uri=MODEL_URI,
        status=MlflowModelBuildStatus.FAILED,
        status_message="Model serving image oluşturulamadı.",
        error_message="===== mlflow cli =====\nMLflow komut satırı aracı bulunamadı.",
    )

    payload = _deployment_out(deployment, build)

    assert payload.build_status == "failed"
    assert payload.build_error_message is not None
    assert "MLflow komut satırı aracı bulunamadı." in payload.build_error_message
    assert _deployment_out(deployment).build_error_message is None


def test_worker_unit_exposes_the_virtualenv_console_scripts_on_path():
    """The worker shells out to virtualenv entry points such as ``mlflow``."""
    unit = (
        Path(__file__).resolve().parents[1] / "deploy" / "aifactory-worker.service"
    ).read_text(encoding="utf-8")

    path_line = next(
        line for line in unit.splitlines() if line.startswith("Environment=PATH=")
    )
    assert "{{PROJECT_DIR}}/.venv/bin" in path_line
    assert "/usr/bin" in path_line
    assert os.sep in path_line
