import os
import shutil
import tempfile
import zipfile
import io
import pytest
from httpx import AsyncClient
from app.models.workspace import Workspace, WorkspaceStatus
from tests.conftest import TEST_WORKER_ID


async def get_authenticated_headers(client: AsyncClient, username: str = "backup_user") -> tuple[dict[str, str], int]:
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Backup User",
        },
    )
    data = resp.json()
    token = data["access_token"]
    user_id = data["user"]["id"]
    return {"Authorization": f"Bearer {token}"}, user_id


@pytest.mark.asyncio
async def test_download_backup_zip(client: AsyncClient, db_session):
    headers, user_id = await get_authenticated_headers(client, "backup_user_1")

    tmp_storage = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmp_storage, "app.py"), "w") as f:
            f.write("# sample aifactory app")

        ws = Workspace(
            id="test-ws-backup-1",
            name="Backup WS",
            description="Backup download test",
            user_id=user_id,
            node_id=TEST_WORKER_ID,
            template_id="vscode-python",
            flavor_id="t1.micro",
            container_name="aifactory-1-test-ws-backup-1",
            host_port=10210,
            container_port=8080,
            storage_path=tmp_storage,
            status=WorkspaceStatus.RUNNING,
        )
        db_session.add(ws)
        await db_session.commit()

        res = await client.get("/api/workspaces/test-ws-backup-1/backup/download", headers=headers)
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/zip"

        # Verify it's a valid zip file containing app.py
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            assert "app.py" in z.namelist()
    finally:
        shutil.rmtree(tmp_storage, ignore_errors=True)


@pytest.mark.asyncio
async def test_snapshot_workspace_to_template(client: AsyncClient, db_session):
    headers, user_id = await get_authenticated_headers(client, "backup_user_2")

    ws = Workspace(
        id="test-ws-snap-1",
        name="Snapshot WS",
        description="Snapshot test",
        user_id=user_id,
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.micro",
        container_name="aifactory-1-test-ws-snap-1",
        host_port=10211,
        container_port=8080,
        storage_path="/tmp/aifactory_snap_test",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(ws)
    await db_session.commit()

    res = await client.post(
        "/api/workspaces/test-ws-snap-1/snapshot",
        data={
            "template_name": "Custom ML Suite",
            "template_description": "Pre-installed PyTorch and Pandas",
        },
        headers=headers,
    )
    assert res.status_code == 200
    data = res.json()
    assert "template_id" in data
    assert "Custom ML Suite" in data["template_name"]
