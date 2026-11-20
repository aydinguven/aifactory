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

    async def fake_execute(master_url, *, os_upgrade=False):
        nonlocal executed
        executed = True

    monkeypatch.setattr(agent, "_execute_upgrade", fake_execute)

    res = await agent.handle_system_command("system.upgrade", {})
    assert res["status"] == "upgrade_started"
    assert res["os_upgrade"] is False
    assert agent.upgrade_status["state"] == "preparing"
    assert "os_upgrade" not in agent.upgrade_status


@pytest.mark.asyncio
async def test_worker_agent_system_upgrade_passes_and_echoes_os_upgrade(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MASTER_URL", "https://master.aifactory.local")
    agent = WorkerAgent()
    received = {}

    async def fake_execute(master_url, *, os_upgrade=False):
        received["os_upgrade"] = os_upgrade

    monkeypatch.setattr(agent, "_execute_upgrade", fake_execute)

    res = await agent.handle_system_command("system.upgrade", {"os_upgrade": True})
    await agent.upgrade_task

    assert res["status"] == "upgrade_started"
    # The echo tells the controller this worker understands the option.
    assert res["os_upgrade"] is True
    assert received == {"os_upgrade": True}
    assert agent.upgrade_status["os_upgrade"] == {"requested": True, "state": "pending"}

    # Only an explicit boolean opts in.
    agent.upgrade_task = None
    res = await agent.handle_system_command("system.upgrade", {"os_upgrade": "true"})
    await agent.upgrade_task
    assert res["os_upgrade"] is False and received == {"os_upgrade": False}


@pytest.mark.asyncio
async def test_worker_upgrade_marker_carries_os_upgrade_flag(tmp_path, monkeypatch):
    import hashlib

    import httpx

    from app import worker_agent as worker_agent_module

    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    monkeypatch.setenv("AIFACTORY_NODE_ID", "node-1")
    monkeypatch.setenv("AIFACTORY_NODE_TOKEN", "token-1")
    bundle = b"platform-bundle"
    filename = "aifactory-platform-update-v99.0.0-abcdef1.tar.gz"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent/releases/latest":
            return httpx.Response(
                200,
                json={
                    "version": "99.0.0",
                    "filename": filename,
                    "url": f"/api/agent/releases/{filename}",
                    "sha256": hashlib.sha256(bundle).hexdigest(),
                    "size": len(bundle),
                },
            )
        return httpx.Response(200, content=bundle)

    real_client = httpx.AsyncClient

    def mocked_client(*args, **kwargs):
        kwargs.pop("verify", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(worker_agent_module.httpx, "AsyncClient", mocked_client)

    agent = WorkerAgent()
    await agent._execute_upgrade("http://controller.local", os_upgrade=True)

    assert agent.upgrade_status["state"] == "queued", agent.upgrade_status
    marker = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
    assert marker["os_upgrade"] is True
    assert marker["target_version"] == "99.0.0"


def test_worker_reports_os_upgrade_request_and_result(tmp_path, monkeypatch):
    queue = tmp_path / "update-queue"
    queue.mkdir()
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))
    (queue / "pending.json").write_text(
        json.dumps({"state": "queued", "target_version": "3.57.0", "os_upgrade": True}),
        encoding="utf-8",
    )

    status = WorkerAgent()._reported_upgrade_status()
    assert status["os_upgrade"] == {"requested": True, "state": "pending"}

    (queue / "pending.json").unlink()
    (queue / "status.json").write_text(
        json.dumps(
            {
                "state": "succeeded",
                "target_version": "3.57.0",
                "message": "OS paketleri yükseltildi. ... yeniden başlatın",
                "finished_at": "2026-11-20T10:00:00+00:00",
                "os_upgrade_result": {
                    "requested": True,
                    "state": "succeeded",
                    "reboot_required": True,
                    "message": "OS paketleri yükseltildi. ... yeniden başlatın",
                    "finished_at": "2026-11-20T10:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    from app import worker_agent as worker_agent_module

    # Booted before the upgrade: the reboot is still pending.
    monkeypatch.setattr(worker_agent_module, "_host_boot_time", lambda: 1_700_000_000.0)
    status = WorkerAgent()._reported_upgrade_status()
    assert status["os_upgrade"]["reboot_required"] is True
    assert "yeniden başlatın" in status["message"]

    # Booted after it: the notice is closed.
    monkeypatch.setattr(worker_agent_module, "_host_boot_time", lambda: 1_900_000_000.0)
    status = WorkerAgent()._reported_upgrade_status()
    assert status["os_upgrade"]["reboot_required"] is False
    assert status["message"] == "OS paketleri yükseltildi; worker yeniden başlatıldı."


def test_host_boot_time_reads_proc_stat(tmp_path):
    from app.worker_agent import _host_boot_time

    stat = tmp_path / "stat"
    stat.write_text("cpu  1 2 3\nbtime 1763600000\nprocesses 5\n", encoding="ascii")
    assert _host_boot_time(str(stat)) == 1763600000.0
    assert _host_boot_time(str(tmp_path / "missing")) is None


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

