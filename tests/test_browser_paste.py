"""Panel paste into a browser workspace: DLP hook, audit and the token path."""

import hashlib
import logging

import pytest
from sqlalchemy import update

import app.paste_dlp as paste_dlp
import app.proxy.router as proxy_router
from app.log_forwarding import AUDIT_LOGGER
from app.models.user import User, UserRole
from app.paste_dlp import PasteDecision


async def _register(client, db_session, username):
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "Password123!"},
    )
    body = response.json()
    # The browser flavor needs 2 GB, above the default per-user quota.
    await db_session.execute(
        update(User)
        .where(User.id == body["user"]["id"])
        .values(cpu_quota_override=8, memory_mb_quota_override=8192)
    )
    await db_session.commit()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


async def _workspace(client, headers, template_id="browser-chromium", flavor_id="t1.small"):
    created = await client.post(
        "/api/workspaces",
        headers=headers,
        json={"name": f"Ws {template_id}", "template_id": template_id, "flavor_id": flavor_id},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.fixture
def container(monkeypatch):
    """Records what the controller sends to the container; answers 200."""
    calls = []
    reply = {"status": 200}

    async def fake_request(workspace, method, path, body=b"", headers=None):
        calls.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})
        return reply["status"], b"{}"

    monkeypatch.setattr(proxy_router, "request_workspace", fake_request)
    return {"calls": calls, "reply": reply}


@pytest.mark.asyncio
async def test_allowed_paste_reaches_the_container_with_the_token_and_is_audited(
    client, db_session, container, caplog
):
    _, owner = await _register(client, db_session, "paste-owner")
    workspace_id = await _workspace(client, owner)
    text = "Merhaba, bu bir deneme metnidir."

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        response = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=owner, json={"text": text})

    assert response.status_code == 200, response.text
    assert response.json() == {"chars": len(text)}
    [call] = container["calls"]
    assert (call["method"], call["path"]) == ("POST", "/clipboard")
    assert call["body"] == text.encode("utf-8")
    assert call["headers"][proxy_router.PASTE_TOKEN_HEADER]
    events = [record.event for record in caplog.records if getattr(record, "event", {}).get("action") == "workspace.browser_paste"]
    assert events and events[-1]["outcome"] == "success"
    assert events[-1]["details"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    # The audit log never holds the pasted text.
    assert all(text not in record.getMessage() and text not in str(getattr(record, "event", "")) for record in caplog.records)


@pytest.mark.asyncio
async def test_dlp_refusal_and_dlp_errors_stop_the_paste(client, db_session, container, monkeypatch):
    _, owner = await _register(client, db_session, "paste-dlp")
    workspace_id = await _workspace(client, owner)

    async def refuse(**_):
        return PasteDecision(False, "Kişisel veri içeriyor.")

    monkeypatch.setattr(paste_dlp, "check_paste", refuse)
    refused = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=owner, json={"text": "TCKN 123"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Kişisel veri içeriyor."

    async def broken(**_):
        raise RuntimeError("DLP down")

    monkeypatch.setattr(paste_dlp, "check_paste", broken)
    failed = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=owner, json={"text": "x"})
    assert failed.status_code == 403  # fail closed
    assert container["calls"] == []


@pytest.mark.asyncio
async def test_paste_is_owner_only_browser_only_and_needs_a_token_in_the_container(
    client, db_session, container
):
    owner_id, owner = await _register(client, db_session, "paste-owner2")
    admin_id, admin = await _register(client, db_session, "paste-admin")
    await db_session.execute(update(User).where(User.id == admin_id).values(role=UserRole.ADMIN))
    await db_session.commit()
    workspace_id = await _workspace(client, owner)

    # Not even an admin pastes into someone else's browser.
    other = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=admin, json={"text": "x"})
    assert other.status_code == 403
    terminal_id = await _workspace(client, owner, "terminal-rocky", "t1.micro")
    terminal = await client.post(f"/api/workspaces/{terminal_id}/clipboard", headers=owner, json={"text": "x"})
    assert terminal.status_code == 400
    empty = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=owner, json={"text": ""})
    assert empty.status_code == 422
    assert container["calls"] == []

    # A container created before panel paste has no token and refuses.
    container["reply"]["status"] = 403
    old = await client.post(f"/api/workspaces/{workspace_id}/clipboard", headers=owner, json={"text": "x"})
    assert old.status_code == 409
    assert "yeniden oluştur" in old.json()["detail"]


def test_proxy_never_forwards_a_paste_token_from_the_browser():
    from types import SimpleNamespace

    request = SimpleNamespace(
        headers={
            "x-aifactory-paste-token": "guessed",
            "accept": "text/html",
        }
    )
    workspace = SimpleNamespace(template_id="browser-chromium", workspace_token="t")
    forwarded = proxy_router._forward_headers(request, workspace)
    assert "x-aifactory-paste-token" not in {key.lower() for key in forwarded}
    assert forwarded["accept"] == "text/html"
