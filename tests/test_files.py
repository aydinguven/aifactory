import os
import io
import shutil
import tempfile
import pytest
from httpx import AsyncClient
from app.agents.manager import agent_manager
from app.models.workspace import Workspace, WorkspaceStatus
from tests.conftest import TEST_WORKER_ID


async def get_authenticated_headers(client: AsyncClient, username: str = "file_user") -> tuple[dict[str, str], int]:
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "File User",
        },
    )
    data = resp.json()
    token = data["access_token"]
    user_id = data["user"]["id"]
    return {"Authorization": f"Bearer {token}"}, user_id


@pytest.mark.asyncio
async def test_file_manager_operations(client: AsyncClient, db_session, monkeypatch):
    headers, user_id = await get_authenticated_headers(client, "file_user_1")

    tmp_storage = tempfile.mkdtemp()
    try:
        # Create a sample file in the storage directory
        with open(os.path.join(tmp_storage, "hello.py"), "w") as f:
            f.write("print('hello aifactory')\n")

        ws = Workspace(
            id="test-ws-files-1",
            name="Files WS",
            description="File manager test",
            user_id=user_id,
            node_id=TEST_WORKER_ID,
            template_id="vscode-python",
            flavor_id="t1.micro",
            container_name="aifactory-1-test-ws-files-1",
            host_port=10205,
            container_port=8080,
            storage_path=tmp_storage,
            status=WorkspaceStatus.RUNNING,
        )
        db_session.add(ws)
        await db_session.commit()
        connection = agent_manager.get(TEST_WORKER_ID)
        original_request = connection.request
        upload_transaction_states = []

        async def observe_request(action, payload, timeout=60):
            if action == "transfer.open" and payload.get("purpose") == "upload":
                upload_transaction_states.append(db_session.in_transaction())
            return await original_request(action, payload, timeout)

        monkeypatch.setattr(connection, "request", observe_request)

        # 1. List directory
        list_res = await client.get("/api/workspaces/test-ws-files-1/files", headers=headers)
        assert list_res.status_code == 200
        data = list_res.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "hello.py"
        assert data["items"][0]["is_dir"] is False

        # 2. Make directory
        mkdir_res = await client.post(
            "/api/workspaces/test-ws-files-1/files/mkdir?path=src",
            headers=headers,
        )
        assert mkdir_res.status_code == 200
        assert os.path.isdir(os.path.join(tmp_storage, "src"))

        # 3. Upload file into src
        file_content = b"const x = 1;"
        upload_res = await client.post(
            "/api/workspaces/test-ws-files-1/files/upload",
            data={"path": "src"},
            files=[("files", ("index.js", io.BytesIO(file_content), "application/javascript"))],
            headers=headers,
        )
        assert upload_res.status_code == 200
        assert upload_transaction_states == [False]

        # 4. Download uploaded file
        download_res = await client.get(
            "/api/workspaces/test-ws-files-1/files/download?path=src/index.js",
            headers=headers,
        )
        assert download_res.status_code == 200
        assert download_res.content == file_content

        # 5. Delete file
        del_res = await client.delete(
            "/api/workspaces/test-ws-files-1/files?path=hello.py",
            headers=headers,
        )
        assert del_res.status_code == 200
        assert not os.path.exists(os.path.join(tmp_storage, "hello.py"))
    finally:
        shutil.rmtree(tmp_storage, ignore_errors=True)
