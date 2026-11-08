import json

import pytest

from app import __version__
from app.config import settings
from app.worker_agent import WorkerAgent, _unsigned_ota_approved


def test_worker_unsigned_ota_requires_explicit_controller_approval():
    assert _unsigned_ota_approved({"allow_unsigned": True}) is True
    assert _unsigned_ota_approved({"allow_unsigned": False}) is False
    assert _unsigned_ota_approved({"allow_unsigned": "true"}) is False
    assert _unsigned_ota_approved({}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,payload",
    [
        ("container.exists", {}),
        ("container.start", {}),
        ("container.stop", {}),
        ("container.status", {}),
        ("container.logs", {}),
        ("container.port_ready", {"host_port": 10100}),
        ("container.stats", {}),
        ("container.storage_size", {}),
        ("container.delete", {}),
    ],
)
async def test_worker_rejects_lifecycle_commands_for_unregistered_containers(
    action,
    payload,
):
    agent = WorkerAgent()
    agent.registry = {}

    with pytest.raises(PermissionError, match="kayıtlı değil"):
        await agent.handle_container_command(
            action,
            {"container_name": "unmanaged-container", **payload},
        )


@pytest.mark.asyncio
async def test_worker_proxy_requires_workspace_container_match():
    agent = WorkerAgent()
    agent.registry = {
        "aifactory-1-aabbccdd": {
            "workspace_id": "workspace-a",
            "storage_path": "/tmp/workspace-a",
            "host_port": 10100,
        }
    }

    with pytest.raises(PermissionError, match="eşleşmesi"):
        await agent._target_url(
            {
                "workspace_id": "workspace-b",
                "container_name": "aifactory-1-aabbccdd",
                "host_port": 10100,
                "path": "",
                "query": "",
            },
            websocket=False,
        )


@pytest.mark.asyncio
async def test_worker_agent_system_upgrade_initiates_upgrade(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MASTER_URL", "https://master.aifactory.local")
    monkeypatch.setenv("AIFACTORY_NODE_ID", "node-upgrade-123")
    monkeypatch.setenv("AIFACTORY_NODE_TOKEN", "token-xyz")

    agent = WorkerAgent()
    # Stub _execute_upgrade to not run shell subprocess in test
    executed = False

    async def fake_execute(master_url):
        nonlocal executed
        executed = True

    monkeypatch.setattr(agent, "_execute_upgrade", fake_execute)

    res = await agent.handle_system_command("system.upgrade", {})
    assert res["status"] == "upgrade_started"
    assert agent.upgrade_status["state"] == "preparing"


def test_worker_reports_durable_upgrade_queue_status(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "pending.json").write_text(
        json.dumps(
            {
                "state": "queued",
                "target_version": "3.4.5",
                "queued_at": "2026-08-29T20:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()

    assert status["state"] == "queued"
    assert status["target_version"] == "3.4.5"


def test_worker_reports_root_updater_failure_output(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "status.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "target_version": "99.0.0",
                "return_code": 1,
                "output": "setup started\nimage archive missing\n",
                "finished_at": "2026-08-30T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()

    assert status["state"] == "failed"
    assert status["return_code"] == "1"
    assert "image archive missing" in status["message"]


def test_worker_explains_missing_signature_configuration(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "status.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "target_version": "99.0.0",
                "return_code": 1,
                "output": "release manifest signature is missing\n",
            }
        ),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()

    assert status["state"] == "failed"
    assert "WORKER_OTA_ALLOW_UNSIGNED=true" in status["message"]
    assert "aifactory-controller" in status["message"]


def test_worker_closes_stale_same_version_failure(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "status.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "target_version": __version__,
                "return_code": 1,
                "output": "old same-version error",
            }
        ),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()

    assert status["state"] == "succeeded"
    assert "zaten hedef sürümde" in status["message"]

