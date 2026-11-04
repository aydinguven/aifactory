import hashlib
import json
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from httpx import AsyncClient
from sqlalchemy import update

from app.models.node import Node, NodeStatus
from app import __version__
from app.config import settings
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor
from app.orchestrator.scheduler import (
    NoSchedulableNode,
    accelerator_availability_details,
    gpu_slots_for_device,
    select_worker_node,
    select_workspace_placement,
)
from app.placement import PlacementConstraints
from tests.conftest import TEST_WORKER_ID
from app.routes.agent_routes import connect_agent, normalize_worker_capabilities
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={"username": "node_admin", "email": "node-admin@test.com", "password": "Password123!"},
    )
    token = response.json()["access_token"]
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(User).where(User.id == user_id).values(role=UserRole.ADMIN))
        await session.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_can_enroll_and_drain_worker_without_revealing_token_later(client: AsyncClient):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-01", "schedulable": True, "labels": {"zone": "dc1"}},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["enrollment_token"]
    assert body["status"] == "pending"

    listed = await client.get("/api/admin/nodes", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "cpu-worker-01"
    assert "enrollment_token" not in listed.json()[0]

    drained = await client.patch(
        f"/api/admin/nodes/{body['id']}",
        headers=headers,
        json={"schedulable": False},
    )
    assert drained.status_code == 200
    assert drained.json()["schedulable"] is False


@pytest.mark.asyncio
async def test_scheduler_uses_online_cpu_worker_and_never_falls_back_when_workers_exist(db_session, monkeypatch):
    worker = Node(
        name="cpu-worker-02",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="0" * 64,
    )
    db_session.add(worker)
    await db_session.commit()

    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected",
        lambda node_id: node_id == worker.id,
    )

    selected = await select_worker_node(db_session, get_flavor("t1.small"))
    assert selected.id == worker.id

    worker.status = NodeStatus.OFFLINE
    db_session.add(worker)
    await db_session.commit()
    with pytest.raises(NoSchedulableNode):
        await select_worker_node(db_session, get_flavor("t1.small"))


def test_gpu_slot_policy_auto_and_mig_isolation():
    node = Node(name="gpu-policy", gpu_slots_per_device=0)
    assert gpu_slots_for_device(node, {"kind": "physical", "model": "RTX 4090"}) == 2
    assert gpu_slots_for_device(node, {"kind": "physical", "model": "RTX 5090"}) == 3
    assert gpu_slots_for_device(node, {"kind": "physical", "model": "B300"}) == 1
    node.gpu_slots_per_device = 3
    assert gpu_slots_for_device(node, {"kind": "physical", "model": "RTX 4090"}) == 3
    assert gpu_slots_for_device(node, {"kind": "mig", "model": "B300 MIG"}) == 1


@pytest.mark.asyncio
async def test_gpu_scheduler_reserves_stopped_4090_slots(db_session, monkeypatch):
    user = User(
        username="gpu_user", email="gpu@test.local", hashed_password="x", gpu_quota_override=3
    )
    worker = Node(
        name="gpu-worker",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=32,
        memory_total_mb=131072,
        disk_total_mb=100000,
        agent_token_hash="9" * 64,
        capabilities_json=json.dumps({
            "accelerators": [{
                "vendor": "nvidia",
                "kind": "physical",
                "id": "GPU-test-4090",
                "cdi_name": "nvidia.com/gpu=GPU-test-4090",
                "model": "NVIDIA GeForce RTX 4090",
                "memory_mb": 24564,
                "healthy": True,
                "allocatable": True,
            }]
        }),
    )
    db_session.add_all([user, worker])
    await db_session.commit()
    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected", lambda _node_id: True
    )

    first = await select_workspace_placement(db_session, get_flavor("g1.shared"))
    assert first.accelerator.slot == 0
    assert first.accelerator.shared_slots == 2
    summary = await accelerator_availability_details(
        db_session, get_flavor("g1.shared")
    )
    assert summary == {
        "available_slots": 2,
        "eligible_accelerator_models": ["NVIDIA GeForce RTX 4090"],
        "allocation_modes": ["shared"],
    }

    for index in (0, 1):
        db_session.add(Workspace(
            name=f"gpu-{index}",
            user_id=user.id,
            node_id=worker.id,
            template_id="vscode-python",
            flavor_id="g1.shared",
            accelerator_device_id="GPU-test-4090",
            accelerator_cdi_name="nvidia.com/gpu=GPU-test-4090",
            accelerator_model="NVIDIA GeForce RTX 4090",
            accelerator_kind="physical",
            accelerator_slot=index,
            accelerator_memory_mb=8192,
            accelerator_shared_slots=2,
            container_name=f"gpu-container-{index}",
            host_port=11000 + index,
            container_port=8080,
            storage_path=f"/tmp/gpu-{index}",
            status=WorkspaceStatus.STOPPED,
        ))
        await db_session.commit()
        if index == 0:
            second = await select_workspace_placement(db_session, get_flavor("g1.shared"))
            assert second.accelerator.slot == 1
            summary = await accelerator_availability_details(
                db_session, get_flavor("g1.shared")
            )
            assert summary["available_slots"] == 1

    with pytest.raises(NoSchedulableNode):
        await select_workspace_placement(db_session, get_flavor("g1.shared"))
    summary = await accelerator_availability_details(
        db_session, get_flavor("g1.shared")
    )
    assert summary["available_slots"] == 0


@pytest.mark.asyncio
async def test_agent_heartbeat_preserves_admin_drain_change(db_session):
    token = "node-secret"
    worker = Node(
        name="cpu-worker-drain",
        status=NodeStatus.PENDING,
        enabled=True,
        schedulable=True,
        agent_token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    db_session.add(worker)
    await db_session.commit()
    await db_session.refresh(worker)

    class FakeWebSocket:
        headers = {"authorization": f"Bearer {token}"}

        def __init__(self):
            self.receive_count = 0
            self.status_during_connection = None

        async def accept(self):
            return None

        async def close(self, **_kwargs):
            return None

        async def receive_json(self):
            self.receive_count += 1
            if self.receive_count == 1:
                async with TestingSessionLocal() as admin_session:
                    await admin_session.execute(
                        update(Node)
                        .where(Node.id == worker.id)
                        .values(schedulable=False, status=NodeStatus.DRAINING)
                    )
                    await admin_session.commit()
                return {
                    "type": "heartbeat",
                    "payload": {
                        "hostname": "worker-drain",
                        "cpu_total": 8,
                        "memory_total_mb": 16384,
                        "disk_total_mb": 100000,
                        "capabilities": {
                            "runtime": "podman",
                            "upgrade": {
                                "state": "downloading",
                                "target_version": "3.4.5",
                            },
                        },
                        "agent_version": "3.4.4",
                    },
                }
            async with TestingSessionLocal() as observer:
                fresh = await observer.get(Node, worker.id)
                self.status_during_connection = fresh.status
            raise WebSocketDisconnect()

    websocket = FakeWebSocket()
    await connect_agent(websocket, worker.id, db_session)

    assert websocket.status_during_connection == NodeStatus.DRAINING
    async with TestingSessionLocal() as observer:
        fresh = await observer.get(Node, worker.id)
        capabilities = json.loads(fresh.capabilities_json)
        assert fresh.agent_version == "3.4.4"
        assert capabilities["upgrade"]["state"] == "downloading"
        assert capabilities["upgrade"]["target_version"] == "3.4.5"


def test_controller_closes_stale_same_version_worker_error():
    capabilities = normalize_worker_capabilities(
        {
            "runtime": "podman",
            "upgrade": {
                "state": "failed",
                "target_version": "3.4.5",
                "message": "legacy updater error",
            },
        },
        "3.4.5",
    )

    assert capabilities["upgrade"]["state"] == "succeeded"
    assert "hedef sürümü çalıştırıyor" in capabilities["upgrade"]["message"]


@pytest.mark.asyncio
async def test_admin_can_delete_worker(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-delete-me", "schedulable": True, "labels": {}},
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 200
    assert "başarıyla silindi" in delete_resp.json()["message"]

    get_resp = await client.get("/api/admin/nodes", headers=headers)
    assert get_resp.status_code == 200
    assert not any(n["id"] == node_id for n in get_resp.json())


@pytest.mark.asyncio
async def test_admin_delete_worker_not_found(client: AsyncClient):
    headers = await _admin_headers(client)
    delete_resp = await client.delete("/api/admin/nodes/non-existent-id", headers=headers)
    assert delete_resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_cannot_delete_worker_with_active_workspaces(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-busy", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create active workspace assigned to this node
    ws = Workspace(
        name="test-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-ws",
        host_port=20001,
        storage_path="/tmp/test-ws",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 409
    assert "atanmış" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_cannot_delete_worker_with_stopped_workspaces(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-idle", "schedulable": True, "labels": {}},
    )
    node_id = created.json()["id"]

    # Create stopped workspace assigned to this node
    ws = Workspace(
        name="test-stopped-ws",
        user_id=1,
        node_id=node_id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-test-stopped-ws",
        host_port=20002,
        storage_path="/tmp/test-stopped-ws",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    delete_resp = await client.delete(f"/api/admin/nodes/{node_id}", headers=headers)
    assert delete_resp.status_code == 409
    assert "atanmış" in delete_resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_can_update_node_labels(client: AsyncClient):
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/nodes",
        headers=headers,
        json={"name": "cpu-worker-labels", "schedulable": True, "labels": {"zone": "dc1"}},
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    updated = await client.put(
        f"/api/admin/nodes/{node_id}/labels",
        headers=headers,
        json={"labels": {"zone": "dc2", "gpu": "true", "tier": "fast"}},
    )
    assert updated.status_code == 200
    assert updated.json()["labels"] == {"zone": "dc2", "gpu": "true", "tier": "fast"}


@pytest.mark.asyncio
async def test_scheduler_load_balancing_and_affinity(db_session, monkeypatch):
    # Worker 1: Heavily loaded (high CPU/RAM utilization), zone=dmz
    w1 = Node(
        name="worker-heavy",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=85.0,
        memory_used_mb=14000,
        labels_json='{"zone": "dmz"}',
        agent_token_hash="1" * 64,
    )
    # Worker 2: Lightly loaded (low utilization), zone=dmz
    w2 = Node(
        name="worker-light",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=10.0,
        memory_used_mb=2000,
        labels_json='{"zone": "dmz"}',
        agent_token_hash="2" * 64,
    )
    # Worker 3: Lightly loaded, but zone=internal
    w3 = Node(
        name="worker-internal",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        cpu_percent=5.0,
        memory_used_mb=1000,
        labels_json='{"zone": "internal"}',
        agent_token_hash="3" * 64,
    )
    db_session.add_all([w1, w2, w3])
    await db_session.commit()

    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected",
        lambda node_id: True,
    )

    # 1. Without selector: scheduler picks worker-internal (lowest composite load)
    selected = await select_worker_node(db_session, get_flavor("t1.small"))
    assert selected.id == w3.id

    # 2. With affinity selector {"zone": "dmz"}: scheduler picks worker-light over worker-heavy
    selected_dmz = await select_worker_node(
        db_session, get_flavor("t1.small"), node_selector={"zone": "dmz"}
    )
    assert selected_dmz.id == w2.id

    # 3. With non-existent affinity selector: raises NoSchedulableNode
    with pytest.raises(NoSchedulableNode):
        await select_worker_node(
            db_session, get_flavor("t1.small"), node_selector={"zone": "gpu-farm"}
        )


@pytest.mark.asyncio
async def test_scheduler_enforces_tags_and_gpu_worker_class(
    db_session, monkeypatch
):
    def worker(name, tags=(), gpu=False, token="a"):
        accelerators = [{
            "vendor": "nvidia", "kind": "physical", "id": f"GPU-{name}",
            "cdi_name": f"nvidia.com/gpu=GPU-{name}", "model": "NVIDIA L40S",
            "memory_mb": 46068, "healthy": True, "allocatable": True,
        }] if gpu else []
        return Node(
            name=name, status=NodeStatus.ONLINE, enabled=True, schedulable=True,
            cpu_total=32, memory_total_mb=131072, disk_total_mb=100000,
            tags_json=json.dumps(list(tags)),
            capabilities_json=json.dumps({"accelerators": accelerators}),
            agent_token_hash=token * 64,
        )

    plain_cpu = worker("plain-cpu", token="b")
    secure_cpu = worker("secure-cpu", ("secure",), token="c")
    secure_gpu = worker("secure-gpu", ("secure",), gpu=True, token="d")
    db_session.add_all([plain_cpu, secure_cpu, secure_gpu])
    await db_session.commit()
    monkeypatch.setattr(
        "app.orchestrator.scheduler.agent_manager.is_connected", lambda _node_id: True
    )
    cpu, gpu = get_flavor("t1.small"), get_flavor("g1.shared")

    async def place(flavor, tags=()):
        constraints = PlacementConstraints(tags=tuple(tags))
        return (await select_workspace_placement(db_session, flavor, constraints=constraints)).node

    assert (await place(cpu, ["secure"])).id == secure_cpu.id
    assert (await place(gpu, ["secure"])).id == secure_gpu.id
    assert (await place(gpu)).id == secure_gpu.id
    # Without tags a CPU workspace still never lands on the GPU worker.
    assert (await place(cpu)).id in {plain_cpu.id, secure_cpu.id}
    with pytest.raises(NoSchedulableNode):
        await place(cpu, ["missing-tag"])

    secure_cpu.schedulable = False
    await db_session.commit()
    with pytest.raises(NoSchedulableNode, match="GPU worker"):
        await place(cpu, ["secure"])
    # Internal CPU jobs (image builds) may still use a GPU worker.
    plain_cpu.schedulable = False
    await db_session.commit()
    assert (await select_worker_node(db_session, cpu)).id == secure_gpu.id


@pytest.mark.asyncio
async def test_admin_cannot_metadata_only_migrate_workspace(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    n1 = Node(
        name="worker-mig-1",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="a" * 64,
    )
    n2 = Node(
        name="worker-mig-2",
        status=NodeStatus.ONLINE,
        enabled=True,
        schedulable=True,
        cpu_total=8,
        memory_total_mb=16384,
        disk_total_mb=100000,
        agent_token_hash="b" * 64,
    )
    db_session.add_all([n1, n2])
    await db_session.commit()

    ws = Workspace(
        name="migrate-ws",
        user_id=1,
        node_id=n1.id,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-mig-ws",
        host_port=20010,
        storage_path="/tmp/mig-ws",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    # Reassigning only node_id would strand the data on n1, so the API refuses.
    res = await client.post(
        f"/api/admin/workspaces/{ws.id}/migrate?target_node_id={n2.id}",
        headers=headers,
    )
    assert res.status_code == 501

    await db_session.refresh(ws)
    assert ws.node_id == n1.id


@pytest.mark.asyncio
async def test_admin_cannot_migrate_running_workspace(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    ws = Workspace(
        name="active-mig-ws",
        user_id=1,
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.small",
        container_name="cnt-active-mig",
        host_port=20011,
        storage_path="/tmp/active-mig",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    res = await client.post(
        f"/api/admin/workspaces/{ws.id}/migrate",
        headers=headers,
    )
    assert res.status_code == 501
    assert "taşıma" in res.json()["detail"]


@pytest.mark.asyncio
async def test_admin_upgrade_node_offline_error(client: AsyncClient, db_session):
    headers = await _admin_headers(client)
    n = Node(
        name="worker-upgrade-test",
        status=NodeStatus.OFFLINE,
        enabled=True,
        schedulable=True,
        agent_token_hash="u" * 64,
    )
    db_session.add(n)
    await db_session.commit()

    res = await client.post(f"/api/admin/nodes/{n.id}/upgrade", headers=headers)
    assert res.status_code == 400
    assert "çevrimdışı" in res.json()["detail"]


@pytest.mark.asyncio
async def test_admin_worker_upgrade_skips_same_version(
    client: AsyncClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
):
    headers = await _admin_headers(client)
    releases = tmp_path / "releases"
    releases.mkdir()
    bundle = releases / f"devcloud-platform-update-v{__version__}-abcdef1.tar.gz"
    bundle.write_bytes(b"same-version-release")
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path))
    worker = await db_session.get(Node, TEST_WORKER_ID)
    worker.agent_version = __version__
    db_session.add(worker)
    await db_session.commit()

    response = await client.post(
        f"/api/admin/nodes/{TEST_WORKER_ID}/upgrade",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["detail"]["status"] == "already_current"
    assert response.json()["detail"]["target_version"] == __version__
    assert "kuyruğu oluşturulmadı" in response.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_admin_worker_upgrade_check_compares_versions(
    client: AsyncClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
):
    headers = await _admin_headers(client)
    releases = tmp_path / "releases"
    releases.mkdir()
    bundle = releases / "devcloud-platform-update-v99.0.0-abcdef1.tar.gz"
    bundle.write_bytes(b"future-release")
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path))
    worker = await db_session.get(Node, TEST_WORKER_ID)
    worker.agent_version = "3.4.9"
    db_session.add(worker)
    await db_session.commit()

    response = await client.get(
        f"/api/admin/nodes/{TEST_WORKER_ID}/upgrade-check",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["installed_version"] == "3.4.9"
    assert response.json()["published_version"] == "99.0.0"
    assert response.json()["update_available"] is True


@pytest.mark.asyncio
async def test_agent_manager_event_broadcasting():
    from app.agents.manager import agent_manager
    queue = agent_manager.subscribe_events()
    try:
        await agent_manager.broadcast_event(
            "node.connected", {"node_id": "test-node-1", "status": "online"}
        )
        event = queue.get_nowait()
        assert event["type"] == "node.connected"
        assert event["data"]["node_id"] == "test-node-1"
        assert event["data"]["status"] == "online"
    finally:
        agent_manager.unsubscribe_events(queue)



