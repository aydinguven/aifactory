import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.config import settings
from app.models.download_settings import DownloadSettings
from app.models.node import Node
from app.models.user import User, UserRole
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.worker_bootstrap import ticket_hash
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "bootstrap_admin",
            "email": "bootstrap-admin@test.com",
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


def _publish_fake_release(tmp_path, monkeypatch):
    downloads = tmp_path / "downloads"
    releases = downloads / "releases"
    releases.mkdir(parents=True)
    bundle = releases / "aifactory-platform-update-v3.4.5-abcdef123456.tar.gz"
    bundle.write_bytes(b"authenticated-platform-release")
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(downloads))
    monkeypatch.setattr(settings, "DOWNLOAD_PUBLIC_BASE_URL", "")
    return bundle


@pytest.mark.asyncio
async def test_admin_ticket_enrolls_exactly_one_worker_and_renders_name_only_script(
    client: AsyncClient,
    db_session,
    tmp_path,
    monkeypatch,
):
    _publish_fake_release(tmp_path, monkeypatch)
    db_session.add(
        DownloadSettings(
            id=1,
            public_base_url="https://aifactory.test",
            https_enabled=True,
            https_hostname="aifactory.test",
        )
    )
    await db_session.commit()
    headers = await _admin_headers(client)
    captured_creator_ids = []
    ticket_model = WorkerBootstrapTicket

    def ticket_factory(**values):
        captured_creator_ids.append(values["created_by_user_id"])
        return ticket_model(**values)

    monkeypatch.setattr(
        "app.routes.admin.nodes.WorkerBootstrapTicket",
        ticket_factory,
    )

    created = await client.post(
        "https://aifactory.test/api/admin/worker-bootstrap-tickets",
        headers=headers,
    )

    assert created.status_code == 201
    assert captured_creator_ids
    assert isinstance(captured_creator_ids[0], str)
    ticket = created.json()
    assert ticket["command"].startswith("curl -fsSL ")
    assert ticket["command"].endswith(" | sudo bash")
    assert "/api/bootstrap/workers/" in ticket["install_url"]
    script = await client.get(ticket["install_url"])
    assert script.status_code == 200
    assert "Worker name [" in script.text
    assert "Worker node ID" not in script.text
    assert "Worker enrollment token" not in script.text
    assert "api/agent/releases/latest" in script.text
    assert '--config "${CURL_AUTH_CONFIG}"' in script.text
    assert '--header "${AUTH_HEADER}"' not in script.text
    assert "--location" not in script.text
    assert "__CONTROLLER_URL__" not in script.text
    assert "__ENROLLMENT_URL__" not in script.text
    assert 'AIFACTORY_WORKER_GPU_MODE' in script.text
    assert 'command -v nvidia-smi' in script.text
    assert 'command -v nvidia-ctk' in script.text
    assert 'nvidia-ctk cdi list' in script.text
    assert 'WORKER_RUNTIME=native' in script.text
    assert 'AIFACTORY_INSTALL_WORKER_RUNTIME="$WORKER_RUNTIME"' in script.text
    assert script.text.index('NVIDIA GPU detected') < script.text.index(
        'Enrolling $' + '{WORKER_NAME}'
    )
    assert "dnf install -y nvidia" not in script.text.lower()

    raw_ticket = ticket["install_url"].split("/")[-2]
    enrolled = await client.post(
        f"https://aifactory.test/api/bootstrap/workers/{raw_ticket}/enroll",
        json={"name": "company-worker-01"},
    )

    assert enrolled.status_code == 201
    credentials = enrolled.json()
    assert credentials["node_id"]
    assert credentials["enrollment_token"]
    assert credentials["controller_url"] == "https://aifactory.test"
    check = await client.get(
        "/api/agent/check",
        params={"node_id": credentials["node_id"]},
        headers={"Authorization": f"Bearer {credentials['enrollment_token']}"},
    )
    assert check.status_code == 200
    assert check.json()["accepted"] is True

    reused = await client.post(
        f"https://aifactory.test/api/bootstrap/workers/{raw_ticket}/enroll",
        json={"name": "company-worker-02"},
    )
    assert reused.status_code == 410

    record = (
        await db_session.execute(
            select(WorkerBootstrapTicket).where(
                WorkerBootstrapTicket.token_hash == ticket_hash(raw_ticket)
            )
        )
    ).scalar_one()
    assert record.used_at is not None
    assert record.node_id == credentials["node_id"]
    assert raw_ticket not in record.token_hash
    node = await db_session.get(Node, credentials["node_id"])
    assert node is not None
    assert node.name == "company-worker-01"
    assert node.agent_token_hash == hashlib.sha256(
        credentials["enrollment_token"].encode("utf-8")
    ).hexdigest()


@pytest.mark.asyncio
async def test_ticket_requires_admin_and_a_published_release(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
    downloads = tmp_path / "empty-downloads"
    downloads.mkdir()
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(downloads))
    assert (
        await client.post("/api/admin/worker-bootstrap-tickets")
    ).status_code in {401, 403}
    headers = await _admin_headers(client)
    response = await client.post(
        "/api/admin/worker-bootstrap-tickets", headers=headers
    )
    assert response.status_code == 409
    assert "platform release" in response.json()["detail"]


@pytest.mark.asyncio
async def test_expired_ticket_cannot_render_or_enroll(
    client: AsyncClient,
    db_session,
    tmp_path,
    monkeypatch,
):
    _publish_fake_release(tmp_path, monkeypatch)
    raw_ticket = "expired-bootstrap-ticket"
    db_session.add(
        WorkerBootstrapTicket(
            token_hash=ticket_hash(raw_ticket),
            created_by_user_id="test-admin",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    await db_session.commit()

    script = await client.get(
        f"/api/bootstrap/workers/{raw_ticket}/install.sh"
    )
    enrolled = await client.post(
        f"/api/bootstrap/workers/{raw_ticket}/enroll",
        json={"name": "expired-worker"},
    )
    assert script.status_code == 410
    assert enrolled.status_code == 410


@pytest.mark.asyncio
async def test_bootstrap_uses_configured_public_controller_url(
    client: AsyncClient,
    db_session,
    tmp_path,
    monkeypatch,
):
    _publish_fake_release(tmp_path, monkeypatch)
    db_session.add(
        DownloadSettings(id=1, public_base_url="https://aifactory.example.com")
    )
    await db_session.commit()
    headers = await _admin_headers(client)

    created = await client.post(
        "https://aifactory.example.com/api/admin/worker-bootstrap-tickets",
        headers=headers,
        json={},
    )

    assert created.status_code == 201
    assert created.json()["install_url"].startswith(
        "https://aifactory.example.com/api/bootstrap/workers/"
    )
