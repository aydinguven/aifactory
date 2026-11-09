import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.flavors import get_flavor
from app.orchestrator.podman_service import podman_service
from tests.conftest import TEST_WORKER_ID

async def get_authenticated_headers(client: AsyncClient, username: str = "dev_user") -> dict[str, str]:
    """Helper to register and return authorization headers."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Dev User",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_template_and_flavor_catalogs(client: AsyncClient):
    """Test retrieving template and flavor definitions."""
    tpl_resp = await client.get("/api/workspaces/templates")
    assert tpl_resp.status_code == 200
    templates = tpl_resp.json()
    tpl_ids = [t["id"] for t in templates]
    template_names = {t["id"]: t["name"] for t in templates}
    assert "vscode-kilo" in tpl_ids
    assert "jupyter-python" in tpl_ids
    assert "terminal-rocky" in tpl_ids
    for tid, tname in {
        "vscode-kilo": "Kilo Code",
        "jupyter-python": "Jupyter Notebook",
        "terminal-rocky": "Terminal (Rocky Linux 10)",
    }.items():
        assert template_names.get(tid) == tname

    flavor_resp = await client.get("/api/workspaces/flavors")
    assert flavor_resp.status_code == 200
    flavors = flavor_resp.json()
    flavor_ids = [f["id"] for f in flavors]
    assert flavor_ids == [
        "t1.nano",
        "t1.micro",
        "t1.small",
        "t1.medium",
        "t1.large",
        "t1.xlarge",
        "g1.shared",
        ]
    display_names = {flavor["id"]: flavor["display_name"] for flavor in flavors}
    assert display_names["t1.micro"] == "Standard"
    assert display_names["g1.shared"] == "GPU Workspace"
    gpu_flavor = next(flavor for flavor in flavors if flavor["id"] == "g1.shared")
    assert gpu_flavor["available_slots"] == 0
    assert gpu_flavor["eligible_accelerator_models"] == []
    assert gpu_flavor["allocation_modes"] == []
    flavor_resources = {
        flavor["id"]: (flavor["cpus"], flavor["memory_mb"])
        for flavor in flavors
    }
    assert flavor_resources == {
        "t1.nano": (0.5, 512),
        "t1.micro": (1.0, 1024),
        "t1.small": (1.0, 2048),
        "t1.medium": (2.0, 4096),
        "t1.large": (4.0, 8192),
        "t1.xlarge": (8.0, 16384),
        "g1.shared": (4.0, 16384),
    }

    legacy_mini = get_flavor("t1.mini")
    assert legacy_mini is not None
    assert legacy_mini.selectable is False


@pytest.mark.asyncio
async def test_legacy_flavor_cannot_be_used_for_new_workspace(client: AsyncClient):
    """Hidden legacy flavors remain restartable but cannot be newly deployed."""
    headers = await get_authenticated_headers(client, "legacy_flavor_tester")
    payload = {
        "name": "Legacy Flavor",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.mini",
    }

    response = await client.post("/api/workspaces", json=payload, headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Geçersiz kaynak profili ID: t1.mini"

    streamed = await client.post(
        "/api/workspaces/deploy-stream", json=payload, headers=headers
    )
    assert streamed.status_code == 200
    assert "Geçersiz kaynak profili: t1.mini" in streamed.text
    assert '"type": "done"' not in streamed.text


@pytest.mark.asyncio
async def test_admin_catalog_toggles_hide_new_choices_but_preserve_existing_workspace(
    client: AsyncClient, db_session
):
    admin_headers = await get_authenticated_headers(client, "catalog_admin")
    admin = (
        await db_session.execute(
            select(User).where(User.username == "catalog_admin")
        )
    ).scalar_one()
    await db_session.execute(
        update(User).where(User.id == admin.id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()

    user_headers = await get_authenticated_headers(client, "catalog_user")
    existing = await client.post(
        "/api/workspaces",
        json={
            "name": "Existing Catalog Workspace",
            "description": "Must remain restartable",
            "template_id": "vscode-kilo",
            "flavor_id": "t1.nano",
        },
        headers=user_headers,
    )
    assert existing.status_code == 201, existing.text
    workspace_id = existing.json()["id"]
    assert (
        await client.post(f"/api/workspaces/{workspace_id}/stop", headers=user_headers)
    ).status_code == 200

    image = WorkspaceImage(
        id="77777777-7777-7777-7777-777777777777",
        template_id="vscode-kilo",
        display_name="Python managed",
        image_ref="localhost/devcloud-vscode-kilo:latest",
        source_type="upload",
        source_ref="python.tar",
        digest="sha256:" + "7" * 64,
        sha256="8" * 64,
        filename="catalog-toggle-python.tar",
        size=1024,
        architecture="amd64",
        enabled=True,
    )
    db_session.add(image)
    await db_session.commit()

    disabled_image = await client.patch(
        f"/api/admin/workspace-images/{image.id}",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert disabled_image.status_code == 200
    assert disabled_image.json()["enabled"] is False

    disabled_flavor = await client.patch(
        "/api/admin/flavors/t1.nano",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert disabled_flavor.status_code == 200
    assert disabled_flavor.json()["enabled"] is False

    template_ids = {
        item["id"] for item in (await client.get("/api/workspaces/templates")).json()
    }
    flavor_ids = {
        item["id"] for item in (await client.get("/api/workspaces/flavors")).json()
    }
    assert "vscode-kilo" not in template_ids
    assert "t1.nano" not in flavor_ids
    hidden_dashboard = await client.get("/", headers=user_headers)
    assert 'data-template-id="vscode-kilo"' not in hidden_dashboard.text
    assert 'name="flavor_id" value="t1.nano"' not in hidden_dashboard.text

    disabled_template_create = await client.post(
        "/api/workspaces",
        json={
            "name": "Blocked Template",
            "template_id": "vscode-kilo",
            "flavor_id": "t1.micro",
        },
        headers=user_headers,
    )
    assert disabled_template_create.status_code == 400
    assert disabled_template_create.json()["detail"] == "Şablon devre dışı: vscode-kilo"

    disabled_flavor_stream = await client.post(
        "/api/workspaces/deploy-stream",
        json={
            "name": "Blocked Flavor",
            "template_id": "terminal-rocky",
            "flavor_id": "t1.nano",
        },
        headers=user_headers,
    )
    assert disabled_flavor_stream.status_code == 200
    assert "Geçersiz kaynak profili: t1.nano" in disabled_flavor_stream.text
    assert '"type": "done"' not in disabled_flavor_stream.text

    admin_page = await client.get("/admin/workspaces", headers=admin_headers)
    assert admin_page.status_code == 200
    assert 'id="admin-flavor-settings-table"' in admin_page.text
    assert 'data-flavor-toggle="t1.nano" data-enabled="false"' in admin_page.text

    restarted = await client.post(
        f"/api/workspaces/{workspace_id}/start", headers=user_headers
    )
    assert restarted.status_code == 200
    assert restarted.json()["status"] == "running"

    assert (
        await client.patch(
            f"/api/admin/workspace-images/{image.id}",
            json={"enabled": True},
            headers=admin_headers,
        )
    ).status_code == 200
    assert (
        await client.patch(
            "/api/admin/flavors/t1.nano",
            json={"enabled": True},
            headers=admin_headers,
        )
    ).status_code == 200

    template_ids = {
        item["id"] for item in (await client.get("/api/workspaces/templates")).json()
    }
    flavor_ids = {
        item["id"] for item in (await client.get("/api/workspaces/flavors")).json()
    }
    assert "vscode-kilo" in template_ids
    assert "t1.nano" in flavor_ids
    restored_dashboard = await client.get("/", headers=user_headers)
    assert 'data-template-id="vscode-kilo"' in restored_dashboard.text
    assert 'name="flavor_id" value="t1.nano"' in restored_dashboard.text


@pytest.mark.asyncio
async def test_workspace_lifecycle(client: AsyncClient):
    """Test creating, starting, stopping, getting logs, and deleting workspaces."""
    headers = await get_authenticated_headers(client, "workspace_tester")

    # 1. Create Python Workspace with t1.micro
    create_payload = {
        "name": "Python Analytics",
        "description": "My data project",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.micro",
    }
    create_resp = await client.post("/api/workspaces", json=create_payload, headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    ws = create_resp.json()
    ws_id = ws["id"]
    assert ws["name"] == "Python Analytics"
    assert ws["status"] == "running"
    assert ws["template_id"] == "vscode-kilo"
    assert ws["flavor_id"] == "t1.micro"
    assert ws["host_port"] >= 10100

    # 2. List Workspaces
    list_resp = await client.get("/api/workspaces", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 3. Get Logs
    logs_resp = await client.get(f"/api/workspaces/{ws_id}/logs", headers=headers)
    assert logs_resp.status_code == 200
    assert "başlatılıyor" in logs_resp.json()["logs"]

    # 4. Stop Workspace
    stop_resp = await client.post(f"/api/workspaces/{ws_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"

    # 5. Start Workspace
    start_resp = await client.post(f"/api/workspaces/{ws_id}/start", headers=headers)
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "running"

    # 6. Delete Workspace
    del_resp = await client.delete(f"/api/workspaces/{ws_id}", headers=headers)
    assert del_resp.status_code == 200

    # Verify deleted
    get_resp = await client.get(f"/api/workspaces/{ws_id}", headers=headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_start_recreates_missing_container_with_same_workspace(client: AsyncClient):
    """A removed container is recreated without deleting its workspace record or storage."""
    headers = await get_authenticated_headers(client, "recreate_workspace_tester")
    create_resp = await client.post(
        "/api/workspaces",
        json={
            "name": "Recreate Me",
            "description": "Persistent data stays mounted",
            "template_id": "jupyter-python",
            "flavor_id": "t1.micro",
        },
        headers=headers,
    )
    assert create_resp.status_code == 201
    workspace = create_resp.json()

    # Simulate out-of-band removal while the database still says "running".
    assert await podman_service.delete_container(workspace["container_name"]) is True
    assert await podman_service.container_exists(workspace["container_name"]) is False

    start_resp = await client.post(
        f"/api/workspaces/{workspace['id']}/start", headers=headers
    )

    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "running"
    assert start_resp.json()["storage_path"] == workspace["storage_path"]
    assert await podman_service.container_exists(workspace["container_name"]) is True


@pytest.mark.asyncio
async def test_deploy_workspace_stream(client: AsyncClient):
    """Test creating a workspace with the streaming SSE log endpoint."""
    headers = await get_authenticated_headers(client, "stream_tester")

    payload = {
        "name": "Streamed App",
        "description": "SSE test project",
        "template_id": "jupyter-python",
        "flavor_id": "t1.nano",
    }
    resp = await client.post("/api/workspaces/deploy-stream", json=payload, headers=headers)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    body = resp.text
    assert "data: " in body
    assert "kurulum süreci başlatılıyor" in body
    assert "done" in body



@pytest.mark.asyncio
async def test_delete_rejects_workspace_during_deployment(client: AsyncClient, db_session):
    """Deleting a creating workspace must not race its deployment task."""
    headers = await get_authenticated_headers(client, "creating_workspace_tester")
    result = await db_session.execute(
        select(User).where(User.username == "creating_workspace_tester")
    )
    user = result.scalar_one()

    workspace = Workspace(
        name="Still Deploying",
        description="",
        user_id=user.id,
        node_id=TEST_WORKER_ID,
        template_id="vscode-kilo",
        flavor_id="t1.micro",
        container_name="aifactory-creating-test",
        host_port=11999,
        container_port=8080,
        storage_path="",
        status=WorkspaceStatus.CREATING,
    )
    db_session.add(workspace)
    await db_session.commit()
    await db_session.refresh(workspace)

    workspace_id = workspace.id
    response = await client.delete(
        f"/api/workspaces/{workspace_id}",
        headers=headers,
    )

    assert response.status_code == 409
    assert "kurulumu devam ediyor" in response.json()["detail"]
    assert await db_session.get(Workspace, workspace_id) is not None


@pytest.mark.asyncio
async def test_workspace_creation_enforces_user_cpu_and_ram_quota(
    client: AsyncClient,
    db_session,
):
    headers = await get_authenticated_headers(client, "quota_workspace_tester")
    result = await db_session.execute(
        select(User).where(User.username == "quota_workspace_tester")
    )
    user = result.scalar_one()
    user.cpu_quota_override = 0.5
    user.memory_mb_quota_override = 512
    user.disk_mb_quota_override = 1024
    db_session.add(user)
    await db_session.commit()

    payload = {
        "name": "Within Quota",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }
    first = await client.post("/api/workspaces", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post(
        "/api/workspaces",
        json={**payload, "name": "Over Quota"},
        headers=headers,
    )
    assert second.status_code == 409
    assert "Kullanıcı kotası aşıldı" in second.json()["detail"]
    assert "CPU 1.0/0.5 olacak" in second.json()["detail"]
    assert "RAM 1024/512 MB olacak" in second.json()["detail"]

    streamed = await client.post(
        "/api/workspaces/deploy-stream",
        json={**payload, "name": "Stream Over Quota"},
        headers=headers,
    )
    assert streamed.status_code == 200
    assert "Kullanıcı kotası aşıldı" in streamed.text
    assert '"type": "done"' not in streamed.text



@pytest.mark.asyncio
async def test_stopping_a_workspace_frees_cpu_and_ram_quota(
    client: AsyncClient,
    db_session,
):
    """A paused workspace keeps its disk but releases compute quota."""
    headers = await get_authenticated_headers(client, "pause_quota_tester")
    result = await db_session.execute(
        select(User).where(User.username == "pause_quota_tester")
    )
    user = result.scalar_one()
    user.cpu_quota_override = 0.5
    user.memory_mb_quota_override = 512
    user.disk_mb_quota_override = 1024
    db_session.add(user)
    await db_session.commit()

    payload = {
        "name": "First Workspace",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }
    first = await client.post("/api/workspaces", json=payload, headers=headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    # Quota is exhausted while it runs.
    blocked = await client.post(
        "/api/workspaces", json={**payload, "name": "Second Workspace"}, headers=headers
    )
    assert blocked.status_code == 409

    stopped = await client.post(f"/api/workspaces/{first_id}/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == WorkspaceStatus.STOPPED.value

    # The freed allowance now admits a second workspace.
    second = await client.post(
        "/api/workspaces", json={**payload, "name": "Second Workspace"}, headers=headers
    )
    assert second.status_code == 201

    usage = await client.get("/api/workspaces/usage", headers=headers)
    assert usage.status_code == 200
    user_usage = usage.json()["user"]
    assert user_usage["cpu"]["used"] == 0.5
    assert user_usage["allocated"]["cpu"]["used"] == 1.0
    assert user_usage["workspace_count"] == 2
    assert user_usage["running_workspace_count"] == 1


@pytest.mark.asyncio
async def test_resuming_a_workspace_rechecks_user_quota(
    client: AsyncClient,
    db_session,
):
    """Restarting a paused workspace must not let a user exceed their quota."""
    headers = await get_authenticated_headers(client, "resume_quota_tester")
    result = await db_session.execute(
        select(User).where(User.username == "resume_quota_tester")
    )
    user = result.scalar_one()
    user.cpu_quota_override = 0.5
    user.memory_mb_quota_override = 512
    user.disk_mb_quota_override = 1024
    db_session.add(user)
    await db_session.commit()

    payload = {
        "name": "Paused Workspace",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }
    first = await client.post("/api/workspaces", json=payload, headers=headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    assert (await client.post(f"/api/workspaces/{first_id}/stop", headers=headers)).status_code == 200

    second = await client.post(
        "/api/workspaces", json={**payload, "name": "Active Workspace"}, headers=headers
    )
    assert second.status_code == 201

    # Both running at once would need 1.0 CPU against a 0.5 quota.
    resumed = await client.post(f"/api/workspaces/{first_id}/start", headers=headers)
    assert resumed.status_code == 409
    assert "Kullanıcı kotası aşıldı" in resumed.json()["detail"]

    # Freeing the other workspace lets the paused one resume.
    assert (
        await client.post(f"/api/workspaces/{second.json()['id']}/stop", headers=headers)
    ).status_code == 200
    allowed = await client.post(f"/api/workspaces/{first_id}/start", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json()["status"] == WorkspaceStatus.RUNNING.value


@pytest.mark.asyncio
async def test_resuming_is_not_blocked_by_a_full_disk_quota(
    client: AsyncClient,
    db_session,
):
    """A full disk must not trap a user out of the workspace they need to clean."""
    headers = await get_authenticated_headers(client, "disk_resume_tester")
    result = await db_session.execute(
        select(User).where(User.username == "disk_resume_tester")
    )
    user = result.scalar_one()
    user.cpu_quota_override = 4.0
    user.memory_mb_quota_override = 4096
    db_session.add(user)
    await db_session.commit()

    created = await client.post(
        "/api/workspaces",
        json={
            "name": "Disk Bound",
            "description": "",
            "template_id": "vscode-kilo",
            "flavor_id": "t1.nano",
        },
        headers=headers,
    )
    assert created.status_code == 201
    workspace_id = created.json()["id"]
    assert (await client.post(f"/api/workspaces/{workspace_id}/stop", headers=headers)).status_code == 200

    user.disk_mb_quota_override = 0
    db_session.add(user)
    await db_session.commit()

    resumed = await client.post(f"/api/workspaces/{workspace_id}/start", headers=headers)
    assert resumed.status_code == 200



@pytest.mark.asyncio
async def test_workspace_names_are_unique_per_owner_not_globally(
    client: AsyncClient,
    db_session,
):
    """Two users may both own "MyWorkspace"; one user may not own it twice."""
    first_headers = await get_authenticated_headers(client, "name_owner_one")
    second_headers = await get_authenticated_headers(client, "name_owner_two")
    for username in ("name_owner_one", "name_owner_two"):
        user = (
            await db_session.execute(select(User).where(User.username == username))
        ).scalar_one()
        user.cpu_quota_override = 8.0
        user.memory_mb_quota_override = 8192
        db_session.add(user)
    await db_session.commit()

    payload = {
        "name": "MyWorkspace",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }

    mine = await client.post("/api/workspaces", json=payload, headers=first_headers)
    assert mine.status_code == 201

    # The same name under a different owner is allowed.
    theirs = await client.post("/api/workspaces", json=payload, headers=second_headers)
    assert theirs.status_code == 201

    # A second copy for the same owner is refused.
    duplicate = await client.post(
        "/api/workspaces", json=payload, headers=first_headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Bu adla bir çalışma alanınız zaten var."

    # Case and repeated whitespace do not create a new name.
    for variant in ("myworkspace", "  MYWORKSPACE  ", "My Workspace"):
        response = await client.post(
            "/api/workspaces", json={**payload, "name": variant}, headers=first_headers
        )
        if variant == "My Workspace":
            # Distinct name: the space makes it a different workspace.
            assert response.status_code == 201
        else:
            assert response.status_code == 409

    streamed = await client.post(
        "/api/workspaces/deploy-stream", json=payload, headers=first_headers
    )
    assert streamed.status_code == 200
    assert "Bu adla bir çalışma alanınız zaten var." in streamed.text
    assert '"type": "done"' not in streamed.text


@pytest.mark.asyncio
async def test_container_name_carries_owner_and_workspace_slug(
    client: AsyncClient,
    db_session,
):
    """The runtime identifier stays unique but readable in `podman ps`."""
    headers = await get_authenticated_headers(client, "slug_tester")
    created = await client.post(
        "/api/workspaces",
        json={
            "name": "Data Analysis",
            "description": "",
            "template_id": "vscode-kilo",
            "flavor_id": "t1.nano",
        },
        headers=headers,
    )
    assert created.status_code == 201

    workspace = (
        await db_session.execute(
            select(Workspace).where(Workspace.id == created.json()["id"])
        )
    ).scalar_one()
    user = (
        await db_session.execute(select(User).where(User.username == "slug_tester"))
    ).scalar_one()

    assert workspace.name_key == "data analysis"
    assert workspace.container_name.startswith(f"aifactory-{user.id}-data-analysis-")
    assert len(workspace.container_name) <= 128


@pytest.mark.asyncio
async def test_freed_name_can_be_reused_after_deletion(
    client: AsyncClient,
):
    """Deleting a workspace releases its name for the same owner."""
    headers = await get_authenticated_headers(client, "name_reuse_tester")
    payload = {
        "name": "Recycled",
        "description": "",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }
    first = await client.post("/api/workspaces", json=payload, headers=headers)
    assert first.status_code == 201

    deleted = await client.delete(
        f"/api/workspaces/{first.json()['id']}", headers=headers
    )
    assert deleted.status_code in (200, 204)

    again = await client.post("/api/workspaces", json=payload, headers=headers)
    assert again.status_code == 201



@pytest.mark.asyncio
async def test_terminal_workspace_deploys_and_is_proxied(
    client: AsyncClient,
    db_session,
):
    """A terminal workspace reaches RUNNING and is served over the proxy."""
    headers = await get_authenticated_headers(client, "terminal_tester")
    user = (
        await db_session.execute(select(User).where(User.username == "terminal_tester"))
    ).scalar_one()
    user.cpu_quota_override = 4.0
    user.memory_mb_quota_override = 4096
    db_session.add(user)
    await db_session.commit()

    created = await client.post(
        "/api/workspaces",
        json={
            "name": "Shell Box",
            "description": "Rocky terminal",
            "template_id": "terminal-rocky",
            "flavor_id": "t1.micro",
        },
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == WorkspaceStatus.RUNNING.value
    assert body["web_url"] == f"/proxy/{body['id']}/"

    workspace = (
        await db_session.execute(select(Workspace).where(Workspace.id == body["id"]))
    ).scalar_one()
    # The published container port must match the template, or the proxy would
    # tunnel to a closed socket.
    assert workspace.container_port == 7681
    assert workspace.template_id == "terminal-rocky"

    # The persistent home is bind-mounted, so storage is provisioned.
    assert workspace.storage_path

    listed = await client.get("/api/workspaces", headers=headers)
    assert listed.status_code == 200
    assert any(item["template_id"] == "terminal-rocky" for item in listed.json())


@pytest.mark.asyncio
async def test_gateway_ca_bundle_recreates_jupyter_once_on_a_capable_worker(
    client: AsyncClient, db_session, monkeypatch
):
    """A saved CA bundle reaches the container once the worker can apply it."""
    from app.models.jupyter_ai_settings import JupyterAiSettings
    from app.models.node import Node
    from tests.test_jupyter_ai_settings import _self_signed_pem

    created: list[str] = []
    original_create = podman_service.create_workspace_container

    async def recording_create(**kwargs):
        created.append(kwargs.get("ai_ca_bundle", ""))
        return await original_create(**kwargs)

    monkeypatch.setattr(podman_service, "create_workspace_container", recording_create)
    node = await db_session.get(Node, TEST_WORKER_ID)
    node.agent_version = "3.25.0"
    await db_session.commit()

    headers = await get_authenticated_headers(client, "ca_bundle_tester")
    workspace = (
        await client.post(
            "/api/workspaces",
            json={"name": "CA", "template_id": "jupyter-python", "flavor_id": "t1.micro"},
            headers=headers,
        )
    ).json()
    bundle = _self_signed_pem("LiteLLM Gateway LAB CA", ca=True)
    db_session.add(JupyterAiSettings(id=1, ca_bundle_pem=bundle))
    await db_session.commit()

    async def restart() -> dict:
        await client.post(f"/api/workspaces/{workspace['id']}/stop", headers=headers)
        response = await client.post(
            f"/api/workspaces/{workspace['id']}/start", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "running"
        return response.json()

    # A 3.25 worker would drop the bundle, so it must not recreate in a loop.
    await restart()
    assert created == [""]

    node.agent_version = "3.26.0"
    await db_session.commit()
    await restart()
    assert created == ["", bundle]
    await restart()
    assert created == ["", bundle]
