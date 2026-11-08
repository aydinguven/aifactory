import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, inspect, update

from app.config import settings
from app.agents.manager import AgentManager
from app.migrations import _rebuild_sqlite_workspaces
from app.models.node import Node
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.podman_service import podman_service
from app.routes.agent_routes import reconcile_worker_inventory
from app.routes.admin import system as admin_system
from app.installer.update_source import ReleaseChannel
from app.worker_agent import WorkerAgent
from app.release_catalog import latest_release
from tests.conftest import TEST_WORKER_ID, TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "release_admin",
            "email": "release-admin@test.com",
            "password": "Password123!",
        },
    )
    token = response.json()["access_token"]
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(role=UserRole.ADMIN)
        )
        await session.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_worker_release_catalog_requires_enrollment_token(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    releases = tmp_path / "releases"
    releases.mkdir()
    release = releases / "aifactory-release-v3.0.0-abcdef1.zip"
    release.write_bytes(b"verified-release")
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "WORKER_OTA_ALLOW_UNSIGNED", True)
    token = "release-worker-secret"
    await db_session.execute(
        update(Node)
        .where(Node.id == TEST_WORKER_ID)
        .values(agent_token_hash=hashlib.sha256(token.encode()).hexdigest())
    )
    await db_session.commit()

    denied = await client.get(
        "/api/agent/releases/latest", params={"node_id": TEST_WORKER_ID}
    )
    assert denied.status_code == 403

    headers = {"Authorization": f"Bearer {token}"}
    metadata = await client.get(
        "/api/agent/releases/latest",
        params={"node_id": TEST_WORKER_ID},
        headers=headers,
    )
    assert metadata.status_code == 200
    checked = await client.get(
        "/api/agent/check",
        params={"node_id": TEST_WORKER_ID},
        headers=headers,
    )
    assert checked.status_code == 200
    assert checked.json()["connected"] is True
    assert metadata.json()["sha256"] == hashlib.sha256(release.read_bytes()).hexdigest()
    assert metadata.json()["allow_unsigned"] is True
    downloaded = await client.get(metadata.json()["url"], headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == b"verified-release"


@pytest.mark.asyncio
async def test_admin_release_upload_creates_atomic_root_updater_request(
    client: AsyncClient, tmp_path, monkeypatch
):
    headers = await _admin_headers(client)
    queue = tmp_path / "update-queue"
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))

    response = await client.post(
        "/api/admin/system/release-upload",
        headers=headers,
        files={"release": ("reviewed-source.zip", b"source-zip", "application/zip")},
        data={"allow_unsigned": "true"},
    )

    assert response.status_code == 202
    marker = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
    bundle = marker["bundle"]
    assert marker["allow_unsigned"] is True
    assert marker["sha256"] == hashlib.sha256(b"source-zip").hexdigest()
    assert (queue / "uploads").resolve() == Path(bundle).parent
    assert Path(bundle).read_bytes() == b"source-zip"


@pytest.mark.asyncio
async def test_admin_release_upload_requires_explicit_unsigned_opt_in(
    client: AsyncClient, tmp_path, monkeypatch
):
    headers = await _admin_headers(client)
    queue = tmp_path / "update-queue"
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))

    response = await client.post(
        "/api/admin/system/release-upload",
        headers=headers,
        files={"release": ("untrusted.zip", b"source-zip", "application/zip")},
    )

    assert response.status_code == 202
    marker = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
    assert marker["allow_unsigned"] is False


@pytest.mark.asyncio
async def test_admin_git_release_channel_creates_root_updater_request(
    client: AsyncClient, tmp_path, monkeypatch
):
    headers = await _admin_headers(client)
    queue = tmp_path / "update-queue"
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))

    response = await client.post(
        "/api/admin/system/release-source",
        headers=headers,
        data={
            "repository": "https://git.aydin.cloud/platform/aifactory.git",
            "ref": "stable",
        },
    )

    assert response.status_code == 202
    marker = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
    assert marker["source_type"] == "git"
    assert marker["repository"] == "https://git.aydin.cloud/platform/aifactory.git"
    assert marker["ref"] == "stable"
    assert marker["allow_unsigned"] is False


@pytest.mark.asyncio
async def test_admin_git_release_channel_accepts_explicit_unsigned_opt_in(
    client: AsyncClient, tmp_path, monkeypatch
):
    headers = await _admin_headers(client)
    queue = tmp_path / "update-queue"
    monkeypatch.setattr(settings, "UPDATE_QUEUE_ROOT", str(queue))

    response = await client.post(
        "/api/admin/system/release-source",
        headers=headers,
        data={
            "repository": "https://github.com/aydinguven/devcloud.git",
            "ref": "stable",
            "allow_unsigned": "true",
        },
    )

    assert response.status_code == 202
    marker = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
    assert marker["source_type"] == "git"
    assert marker["allow_unsigned"] is True


@pytest.mark.asyncio
async def test_admin_checks_installed_and_published_release_before_queueing(
    client: AsyncClient, monkeypatch
):
    headers = await _admin_headers(client)

    async def fake_channel(repository: str, ref: str):
        assert repository == "https://github.com/aydinguven/devcloud.git"
        assert ref == "stable"
        return ReleaseChannel(
            version="99.0.0",
            filename="aifactory-platform-update-v99.0.0-test.tar.gz",
            url="https://github.com/aydinguven/devcloud/releases/test.tar.gz",
            sha256="a" * 64,
            size=1024,
        )

    monkeypatch.setattr(admin_system, "_fetch_release_channel", fake_channel)
    response = await client.post(
        "/api/admin/system/release-check",
        headers=headers,
        data={
            "repository": "https://github.com/aydinguven/devcloud.git",
            "ref": "stable",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["installed_version"] == settings.APP_VERSION
    assert response.json()["published_version"] == "99.0.0"
    assert response.json()["update_available"] is True


@pytest.mark.asyncio
async def test_reconciliation_surfaces_missing_orphaned_and_mismatched_inventory(
    client: AsyncClient, db_session
):
    registered = await client.post(
        "/api/auth/register",
        json={
            "username": "reconcile_user",
            "email": "reconcile@test.com",
            "password": "Password123!",
        },
    )
    workspace = Workspace(
        id="reconcile-workspace",
        name="Reconcile",
        user_id=registered.json()["user"]["id"],
        node_id=TEST_WORKER_ID,
        template_id="vscode-python",
        flavor_id="t1.micro",
        container_name="reconcile-container",
        host_port=11001,
        storage_path="/tmp/reconcile",
        status=WorkspaceStatus.RUNNING,
    )
    db_session.add(workspace)
    await db_session.commit()
    node = await db_session.get(Node, TEST_WORKER_ID)

    report = await reconcile_worker_inventory(
        db_session,
        node,
        [
            {
                "workspace_id": "wrong-id",
                "container_name": "reconcile-container",
                "host_port": 11002,
                "storage_path": "/tmp/reconcile",
                "status": "running",
            },
            {
                "workspace_id": "orphan",
                "container_name": "orphan-container",
                "host_port": 11003,
                "storage_path": "/tmp/orphan",
                "status": "running",
            },
        ],
    )

    assert report["missing"] == []
    assert report["orphaned"] == ["orphan-container"]
    assert report["mismatched"] == ["reconcile-container"]
    assert report["healthy"] is False

    missing = await reconcile_worker_inventory(db_session, node, [])
    assert missing["missing"] == ["reconcile-container"]
    assert workspace.status == WorkspaceStatus.ERROR


@pytest.mark.asyncio
async def test_worker_create_is_idempotent(tmp_path, monkeypatch):
    agent = WorkerAgent()
    agent.registry_path = tmp_path / "registry.json"
    agent.registry = {
        "container-1": {
            "workspace_id": "workspace-1",
            "container_id": "existing-id",
            "storage_path": "/srv/workspace-1",
            "host_port": 11010,
        }
    }

    async def exists(_name):
        return True

    async def must_not_create(**_kwargs):
        raise AssertionError("idempotent replay created a second container")

    monkeypatch.setattr(podman_service, "container_exists", exists)
    monkeypatch.setattr(podman_service, "create_workspace_container", must_not_create)

    result = await agent.handle_container_command(
        "container.create",
        {
            "workspace_id": "workspace-1",
            "user_id": 1,
            "container_name": "container-1",
            "template_id": "vscode-python",
            "flavor_id": "t1.micro",
            "host_port": 11010,
            "workspace_token": "token",
        },
    )

    assert result["reused"] is True
    assert result["container_id"] == "existing-id"


def test_legacy_sqlite_workspace_table_is_rebuilt_for_worker_scoped_ports():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE workspaces ("
            "id VARCHAR(36) PRIMARY KEY, "
            "node_id VARCHAR(36), "
            "host_port INTEGER NOT NULL UNIQUE)"
        )
        assert _rebuild_sqlite_workspaces(connection) is True

        inspector = inspect(connection)
        node = next(
            column
            for column in inspector.get_columns("workspaces")
            if column["name"] == "node_id"
        )
        unique_sets = {
            tuple(item.get("column_names") or ())
            for item in (
                inspector.get_unique_constraints("workspaces")
                + [
                    index
                    for index in inspector.get_indexes("workspaces")
                    if index.get("unique")
                ]
            )
        }
        assert node["nullable"] is False
        assert ("node_id", "host_port") in unique_sets
        assert ("host_port",) not in unique_sets


def test_controller_import_does_not_load_podman_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.main; "
                "assert 'app.orchestrator.podman_service' not in sys.modules"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_release_catalog_prefers_semantic_version_over_copy_time(tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    newer_copy = releases / "aifactory-release-v2.9.9-abcdef1.zip"
    newer_copy.write_bytes(b"older-version")
    higher_version = releases / "aifactory-release-v3.0.0-abcdef2.zip"
    higher_version.write_bytes(b"higher-version")

    selected = latest_release(tmp_path)

    assert selected is not None
    assert selected.path == higher_version


@pytest.mark.asyncio
async def test_stale_agent_disconnect_does_not_remove_replacement_connection():
    class Socket:
        async def send_json(self, _message):
            return None

        async def close(self, **_kwargs):
            return None

    manager = AgentManager()
    first = await manager.register("worker-1", Socket())
    replacement = await manager.register("worker-1", Socket())

    assert await manager.unregister("worker-1", first) is False
    assert manager.get("worker-1") is replacement
