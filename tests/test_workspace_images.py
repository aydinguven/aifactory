import hashlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

import app.routes.admin.images as admin_images
import app.worker_agent as worker_module
import app.workspace_image_service as image_service
from app.config import settings
from app.models.node import Node
from app.models.custom_template import CustomTemplate
from app.models.user import User, UserRole
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.podman_service import podman_service
from app.orchestrator.scheduler import _has_workspace_image
from app.orchestrator.templates import TEMPLATES
from app.worker_agent import WorkerAgent
from tests.conftest import TEST_WORKER_ID


def test_registry_import_normalizes_to_managed_oci_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_IMAGES_ROOT", str(tmp_path))
    auth_files = []
    skopeo_calls = []

    def fake_skopeo(arguments, *, environment=None):
        skopeo_calls.append(arguments)
        if arguments[0] == "copy":
            destination = arguments[-1].removeprefix("oci-archive:").split(
                ":localhost/", 1
            )[0]
            Path(destination).write_bytes(b"normalized-oci-archive")
            if environment and environment.get("REGISTRY_AUTH_FILE"):
                auth_path = Path(environment["REGISTRY_AUTH_FILE"])
                assert auth_path.is_file()
                auth_files.append(auth_path)
            return ""
        return json.dumps(
            {
                "Digest": "sha256:" + "a" * 64,
                "Architecture": "amd64",
                "Os": "linux",
            }
        )

    monkeypatch.setattr(image_service, "_run_skopeo", fake_skopeo)
    metadata = image_service.import_registry_image(
        image_ref="localhost/aifactory-vscode-python:latest",
        source_ref="quay.io/example/python:2026.08",
        username="robot",
        password="secret",
    )

    archive = image_service.image_archive_path(str(metadata["filename"]))
    assert archive.read_bytes() == b"normalized-oci-archive"
    assert metadata["image_ref"] == "localhost/aifactory-vscode-python:latest"
    assert metadata["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert auth_files and all(not path.exists() for path in auth_files)
    assert "--remove-signatures" in next(
        arguments for arguments in skopeo_calls if arguments[0] == "copy"
    )


def test_scheduler_requires_managed_image_from_new_agents():
    node = Node(id="image-node", name="image-node")
    node.capabilities_json = json.dumps({"runtime": "podman", "workspace_images": []})
    assert _has_workspace_image(node, "localhost/aifactory-vscode-python:latest") is False

    node.capabilities_json = json.dumps(
        {
            "runtime": "podman",
            "workspace_images": [
                {"image_ref": "localhost/aifactory-vscode-python:latest"}
            ],
        }
    )
    assert _has_workspace_image(node, "localhost/aifactory-vscode-python:latest") is True
    assert _has_workspace_image(
        node,
        "localhost/aifactory-vscode-python:latest",
        "f" * 64,
    ) is False

    node.capabilities_json = json.dumps({"runtime": "podman"})
    assert _has_workspace_image(node, "localhost/aifactory-vscode-python:latest") is True
    assert _has_workspace_image(
        node,
        "localhost/aifactory-vscode-python:latest",
        "f" * 64,
    ) is False

    node.capabilities_json = "not-json"
    assert _has_workspace_image(node, "localhost/aifactory-vscode-python:latest") is False


@pytest.mark.asyncio
async def test_admin_catalog_and_authenticated_worker_download(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "WORKSPACE_IMAGES_ROOT", str(tmp_path))
    archive = tmp_path / "managed.tar"
    archive.write_bytes(b"managed-image")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    record = WorkspaceImage(
        id="11111111-1111-1111-1111-111111111111",
        template_id="vscode-python",
        display_name="Python managed",
        image_ref="localhost/aifactory-vscode-python:latest",
        source_type="upload",
        source_ref="python.tar",
        digest="sha256:" + "b" * 64,
        sha256=digest,
        filename=archive.name,
        size=archive.stat().st_size,
        architecture="amd64",
        enabled=True,
    )
    db_session.add(record)
    db_session.add(
        CustomTemplate(
            id="custom-rust",
            name="Rust Custom",
            description="Managed custom template",
            category="Custom",
            image_tag="registry.example/aifactory-rust:latest",
            default_port=8080,
            ide_type="vscode",
            containerfile="FROM scratch",
            is_ready=True,
        )
    )
    worker = await db_session.get(Node, TEST_WORKER_ID)
    worker_token = "worker-image-token"
    worker.agent_token_hash = hashlib.sha256(worker_token.encode()).hexdigest()
    worker.capabilities_json = json.dumps(
        {
            "runtime": "podman",
            "workspace_images": [
                {"image_ref": record.image_ref, "sha256": record.sha256}
            ],
        }
    )
    db_session.add(worker)
    await db_session.commit()

    registration = await client.post(
        "/api/auth/register",
        json={
            "username": "image-admin",
            "email": "image-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    admin_id = registration.json()["user"]["id"]
    await db_session.execute(
        update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    admin_headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}

    page = await client.get("/admin/images", headers=admin_headers)
    assert page.status_code == 200
    assert 'id="workspace-image-registry-form"' in page.text
    assert 'id="workspace-image-upload-form"' in page.text
    assert 'value="custom-rust"' in page.text
    assert 'value="__new__"' in page.text
    assert 'id="workspace-image-new-template-fields"' in page.text

    catalog = await client.get("/api/admin/workspace-images", headers=admin_headers)
    assert catalog.status_code == 200
    assert catalog.json()[0]["synced_workers"] == 1
    assert catalog.json()[0]["workers"][0]["state"] == "ready"
    assert catalog.json()[0]["workers"][0]["percent"] == 100.0

    worker_headers = {"Authorization": f"Bearer {worker_token}"}
    desired = await client.get(
        "/api/agent/images/catalog",
        params={"node_id": TEST_WORKER_ID},
        headers=worker_headers,
    )
    assert desired.status_code == 200
    assert desired.json()["images"][0]["sha256"] == digest

    download = await client.get(
        f"/api/agent/images/{record.id}/archive",
        params={"node_id": TEST_WORKER_ID},
        headers=worker_headers,
    )
    assert download.status_code == 200
    assert download.content == b"managed-image"
    assert download.headers["x-aifactory-sha256"] == digest


@pytest.mark.asyncio
async def test_registry_import_can_create_workspace_template(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "WORKSPACE_IMAGES_ROOT", str(tmp_path))
    template_id = "custom-registry-jupyter"

    def fake_registry_import(*, image_ref, source_ref, username, password):
        assert image_ref == f"localhost/aifactory-{template_id}:latest"
        assert source_ref == "quay.io/example/notebook:2026.09"
        assert username == "robot"
        assert password == "registry-token"
        archive = image_service.image_archive_path("custom-registry-jupyter.tar")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"managed-registry-image")
        return {
            "id": "33333333-3333-3333-3333-333333333333",
            "image_ref": image_ref,
            "digest": "sha256:" + "d" * 64,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "filename": archive.name,
            "size": archive.stat().st_size,
            "architecture": "amd64",
        }

    monkeypatch.setattr(admin_images, "import_registry_image", fake_registry_import)
    registration = await client.post(
        "/api/auth/register",
        json={
            "username": "registry-template-admin",
            "email": "registry-template-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    admin_id = registration.json()["user"]["id"]
    await db_session.execute(
        update(User).where(User.id == admin_id).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}

    try:
        response = await client.post(
            "/api/admin/workspace-images/import",
            headers=headers,
            json={
                "template_id": template_id,
                "display_name": "Notebook 2026.09",
                "source_ref": "quay.io/example/notebook:2026.09",
                "username": "robot",
                "password": "registry-token",
                "new_template": {
                    "id": template_id,
                    "name": "Registry Jupyter",
                    "description": "Imported notebook environment",
                    "category": "Data Science",
                    "default_port": 8888,
                    "ide_type": "jupyter",
                },
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["template_id"] == template_id
        custom = await db_session.get(CustomTemplate, template_id)
        assert custom is not None
        assert custom.name == "Registry Jupyter"
        assert custom.image_tag == f"localhost/aifactory-{template_id}:latest"
        assert custom.default_port == 8888
        assert custom.ide_type == "jupyter"
        assert custom.containerfile == "FROM quay.io/example/notebook:2026.09"
        assert TEMPLATES[template_id].container_workdir == "/home/jovyan/work"
    finally:
        TEMPLATES.pop(template_id, None)


@pytest.mark.asyncio
async def test_worker_reconciles_and_verifies_controller_image(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_CONTROLLER_URL", "https://controller.example")
    monkeypatch.setenv("AIFACTORY_NODE_ID", "worker-1")
    monkeypatch.setenv("AIFACTORY_NODE_TOKEN", "token-1")
    content = b"verified-workspace-image"
    checksum = hashlib.sha256(content).hexdigest()
    image_ref = "localhost/aifactory-vscode-python:latest"
    image_id = "22222222-2222-2222-2222-222222222222"
    catalog = {
        "images": [
            {
                "id": image_id,
                "template_id": "vscode-python",
                "image_ref": image_ref,
                "digest": "sha256:" + "c" * 64,
                "sha256": checksum,
                "size": len(content),
                "url": f"/api/agent/images/{image_id}/archive?node_id=worker-1",
            }
        ]
    }

    class StreamContext:
        async def __aenter__(self):
            return httpx.Response(
                200,
                content=content,
                request=httpx.Request(
                    "GET",
                    "https://controller.example" + catalog["images"][0]["url"],
                ),
            )

        async def __aexit__(self, *_args):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json=catalog,
                request=httpx.Request("GET", "https://controller.example/catalog"),
            )

        def stream(self, *_args, **_kwargs):
            return StreamContext()

    calls = []

    async def fake_podman(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("image", "exists"):
            already_loaded = any(call and call[0] == "load" for call in calls[:-1])
            return (0 if already_loaded else 1), "", ""
        if args and args[0] == "load":
            assert Path(args[2]).read_bytes() == content
            return 0, "Loaded image", ""
        return 0, "", ""

    monkeypatch.setattr(worker_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(podman_service, "run_cmd", fake_podman)
    agent = WorkerAgent()

    state = await agent.sync_workspace_images()

    assert state[0]["sha256"] == checksum
    assert state[0]["image_ref"] == image_ref
    assert agent.image_state_path.is_file()
    assert any(call and call[0] == "load" for call in calls)
    assert agent.image_progress[image_id]["state"] == "ready"
    assert agent.image_progress[image_id]["downloaded_bytes"] == len(content)


def test_worker_reporting_the_devcloud_tag_still_has_the_renamed_image():
    """A 3.49 worker reports the old tag until it runs 3.50.1."""
    node = Node(
        id="worker-legacy-tag",
        name="worker-legacy-tag",
        capabilities_json=json.dumps(
            {
                "workspace_images": [
                    {"image_ref": "localhost/devcloud-vscode-kilo:latest", "sha256": "a" * 64}
                ]
            }
        ),
    )
    assert _has_workspace_image(node, "localhost/aifactory-vscode-kilo:latest", "a" * 64)
    assert not _has_workspace_image(node, "localhost/aifactory-vscode-kilo:latest", "b" * 64)
