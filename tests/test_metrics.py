import pytest
from datetime import datetime, timedelta, timezone
from httpx import AsyncClient
from app.models.workspace import Workspace, WorkspaceStatus
from tests.conftest import TEST_WORKER_ID


async def get_authenticated_headers(client: AsyncClient, username: str = "metric_user") -> tuple[dict[str, str], int]:
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Metric User",
        },
    )
    data = resp.json()
    token = data["access_token"]
    user_id = data["user"]["id"]
    return {"Authorization": f"Bearer {token}"}, user_id


@pytest.mark.asyncio
async def test_get_workspace_stats_summary(client: AsyncClient, db_session):
    headers, user_id = await get_authenticated_headers(client, "metric_user_1")

    ws = Workspace(
        id="test-ws-metrics-1",
        name="Metrics WS",
        description="Stats test",
        user_id=user_id,
        node_id=TEST_WORKER_ID,
        template_id="jupyter-python",
        flavor_id="t1.micro",
        container_name="aifactory-1-test-ws-metrics-1",
        host_port=10200,
        container_port=8888,
        storage_path="/tmp/aifactory_test_metrics",
        status=WorkspaceStatus.RUNNING,
        last_started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(ws)
    await db_session.commit()

    response = await client.get("/api/workspaces/stats/summary", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "stats" in data
    assert len(data["stats"]) >= 1
    stat = next((s for s in data["stats"] if s["workspace_id"] == "test-ws-metrics-1"), None)
    assert stat is not None
    assert "cpu_percent" in stat
    assert "disk_usage_display" in stat
    assert stat["uptime_seconds"] >= 299
    assert stat["uptime_display"].startswith("5m")


@pytest.mark.asyncio
async def test_get_single_workspace_stats(client: AsyncClient, db_session):
    headers, user_id = await get_authenticated_headers(client, "metric_user_2")

    ws = Workspace(
        id="test-ws-metrics-2",
        name="Metrics WS 2",
        description="Single stats test",
        user_id=user_id,
        node_id=TEST_WORKER_ID,
        template_id="jupyter-python",
        flavor_id="t1.micro",
        container_name="aifactory-1-test-ws-metrics-2",
        host_port=10201,
        container_port=8888,
        storage_path="/tmp/aifactory_test_metrics_2",
        status=WorkspaceStatus.STOPPED,
    )
    db_session.add(ws)
    await db_session.commit()

    response = await client.get("/api/workspaces/test-ws-metrics-2/stats", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["workspace_id"] == "test-ws-metrics-2"
    assert data["status"] == "stopped"
