import gzip
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

import app.proxy.router as proxy_module
import app.worker_agent as worker_module
from app.agents.manager import AgentStream, StreamChunk
from app.models.workspace import Workspace
from sqlalchemy import update
from tests.conftest import TestingSessionLocal

@pytest.mark.asyncio
async def test_proxy_auth_guard(client: AsyncClient):
    """Test that unauthorized requests to proxy routes are blocked."""
    # Attempt to proxy without authentication
    resp = await client.get("/proxy/non-existent-ws/index.html")
    assert resp.status_code == 404 or resp.status_code == 401


@pytest.mark.asyncio
async def test_proxy_unauthorized_user_access(client: AsyncClient):
    """Test that User B cannot access User A's workspace."""
    # Register User A
    user_a_res = await client.post(
        "/api/auth/register",
        json={"username": "user_a", "email": "a@test.com", "password": "Password123!"},
    )
    token_a = user_a_res.json()["access_token"]

    # User A creates workspace
    ws_res = await client.post(
        "/api/workspaces",
        json={"name": "Alice WS", "template_id": "vscode-kilo", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    ws_id = ws_res.json()["id"]

    # Register User B
    user_b_res = await client.post(
        "/api/auth/register",
        json={"username": "user_b", "email": "b@test.com", "password": "Password123!"},
    )
    token_b = user_b_res.json()["access_token"]

    # User B attempts to access User A's workspace
    forbidden_res = await client.get(
        f"/proxy/{ws_id}/",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert forbidden_res.status_code == 403


@pytest.mark.asyncio
async def test_proxy_waiting_page_exposes_live_authenticated_diagnostics(
    client: AsyncClient,
    monkeypatch,
):
    """A slow IDE should show live, owner-only container and port diagnostics."""
    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_startup_logs_user",
            "email": "proxy-startup-logs@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace_response = await client.post(
        "/api/workspaces",
        json={
            "name": "Logging IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace = workspace_response.json()

    class UnavailableProxyClient:
        def __init__(self, *args, **kwargs):
            return None

        def build_request(self, *args, **kwargs):
            return proxy_module.httpx.Request("GET", "http://127.0.0.1/")

        async def send(self, request, stream=False):
            raise proxy_module.httpx.ConnectError("IDE is not ready", request=request)

        async def aclose(self):
            return None

    async def no_sleep(_seconds):
        return None

    async def port_not_ready(_host_port):
        return False

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", UnavailableProxyClient)
    monkeypatch.setattr(proxy_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(proxy_module, "port_is_ready", port_not_ready)

    response = await client.get(
        f"/proxy/{workspace['id']}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/html",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Son Container Çıktısı" in response.text
    assert "IDE Portu" in response.text
    assert "Kontrol" in response.text
    assert f"/proxy/{workspace['id']}/_aifactory/status?tail=120" in response.text
    assert "http-equiv=\"refresh\"" not in response.text

    status_response = await client.get(
        f"/proxy/{workspace['id']}/_aifactory/status?tail=120",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_response.status_code == 200
    diagnostics = status_response.json()
    assert diagnostics["workspace_status"] == "running"
    assert diagnostics["container_status"] == "running"
    assert diagnostics["host_port"] == workspace["host_port"]
    assert diagnostics["port_ready"] is False
    assert "başlatılıyor" in diagnostics["logs"]

    other_register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_startup_logs_other_user",
            "email": "proxy-startup-logs-other@test.com",
            "password": "Password123!",
        },
    )
    other_token = other_register.json()["access_token"]
    forbidden = await client.get(
        f"/proxy/{workspace['id']}/_aifactory/status",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_proxy_preserves_multiple_jupyter_set_cookie_headers(
    client: AsyncClient,
    monkeypatch,
):
    """Jupyter's login and XSRF cookies must reach the browser independently."""
    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_cookie_user",
            "email": "proxy-cookie@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace_response = await client.post(
        "/api/workspaces",
        json={
            "name": "Cookie IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace_response.json()["id"]
    cookie_path = f"/proxy/{workspace_id}/"
    upstream_cookies = [
        f"_xsrf=xsrf-value; Path={cookie_path}",
        f"username-jupyter=login-value; HttpOnly; Path={cookie_path}",
    ]

    class CookieUpstreamResponse:
        status_code = 200
        headers = proxy_module.httpx.Headers(
            [
                ("content-type", "text/html; charset=utf-8"),
                ("set-cookie", upstream_cookies[0]),
                ("set-cookie", upstream_cookies[1]),
            ]
        )

        async def aiter_raw(self, chunk_size=None):
            yield b"<html>JupyterLab</html>"

        async def aclose(self):
            return None

    class CookieProxyClient:
        def __init__(self, *args, **kwargs):
            return None

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=False):
            assert stream is True
            return CookieUpstreamResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", CookieProxyClient)

    response = await client.get(
        f"/proxy/{workspace_id}/",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == upstream_cookies


@pytest.mark.asyncio
async def test_proxy_preserves_content_encoding_for_raw_stream(client: AsyncClient, monkeypatch):
    """Compressed bytes and Jupyter browser-facing headers must survive proxying."""
    html_body = b"<!doctype html><html><body>Code Server</body></html>"
    compressed_body = gzip.compress(html_body)

    captured_request = {}
    class FakeUpstreamResponse:
        status_code = 200
        headers = proxy_module.httpx.Headers({
            "content-type": "text/html; charset=utf-8",
            "content-encoding": "gzip",
            "content-length": str(len(compressed_body)),
        })

        async def aiter_raw(self, chunk_size=None):
            yield compressed_body

        async def aclose(self):
            return None

    class FakeProxyClient:
        def __init__(self, *args, **kwargs):
            self.response = FakeUpstreamResponse()

        def build_request(self, *args, **kwargs):
            captured_request.update(kwargs)
            return object()

        async def send(self, request, stream=False):
            assert stream is True
            return self.response

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", FakeProxyClient)

    register = await client.post(
        "/api/auth/register",
        json={
            "username": "proxy_compression_user",
            "email": "proxy-compression@test.com",
            "password": "Password123!",
        },
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={
            "name": "Compressed IDE",
            "template_id": "jupyter-python",
            "flavor_id": "t1.nano",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]

    response = await client.get(
        f"/proxy/{workspace_id}/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
            "Origin": "http://test",
            "Cookie": f"aifactory_session={token}; _xsrf=workspace-xsrf",
        },
    )

    assert response.status_code == 200
    assert response.content == html_body
    assert response.headers["content-encoding"] == "gzip"
    assert captured_request["url"].endswith(f"/proxy/{workspace_id}/")
    captured_headers = {
        key.lower(): value for key, value in captured_request["headers"].items()
    }
    assert captured_headers["authorization"].startswith("token ")
    assert captured_headers["authorization"] != f"Bearer {token}"
    assert captured_headers["origin"] == "http://test"
    assert captured_headers["host"] == "test"
    assert captured_headers["x-forwarded-host"] == "test"
    assert captured_headers["x-forwarded-proto"] == "http"
    assert captured_headers["cookie"] == "_xsrf=workspace-xsrf"
    assert token not in captured_headers["cookie"]


@pytest.mark.asyncio
async def test_custom_port_path_is_dispatched_before_catch_all_proxy(client: AsyncClient, monkeypatch):
    register = await client.post(
        "/api/auth/register",
        json={"username": "custom_port_user", "email": "custom-port@test.com", "password": "Password123!"},
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={"name": "Custom Port", "template_id": "vscode-kilo", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]
    captured = {}

    class Upstream:
        status_code = 200
        headers = proxy_module.httpx.Headers({"content-type": "application/json"})
        async def aiter_raw(self, chunk_size=None):
            yield b'{"ok":true}'
        async def aclose(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            return None
        def build_request(self, **kwargs):
            captured.update(kwargs)
            return object()
        async def send(self, request, stream=False):
            return Upstream()
        async def aclose(self):
            return None

    async def container_ip(_name):
        return "10.88.0.42"

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(worker_module.podman_service, "get_container_ip", container_ip)
    response = await client.get(
        f"/proxy/{workspace_id}/port/5173/api/health?verbose=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert captured["url"] == "http://10.88.0.42:5173/api/health?verbose=1"


@pytest.mark.asyncio
async def test_remote_custom_port_uses_worker_tunnel_and_same_public_url(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    register = await client.post(
        "/api/auth/register",
        json={"username": "remote_proxy_user", "email": "remote-proxy@test.com", "password": "Password123!"},
    )
    token = register.json()["access_token"]
    workspace = await client.post(
        "/api/workspaces",
        json={"name": "Remote Port", "template_id": "vscode-kilo", "flavor_id": "t1.nano"},
        headers={"Authorization": f"Bearer {token}"},
    )
    workspace_id = workspace.json()["id"]
    async with TestingSessionLocal() as session:
        await session.execute(update(Workspace).where(Workspace.id == workspace_id).values(node_id="remote-node"))
        await session.commit()

    captured = {}

    class Connection:
        async def receive_stream(self, stream):
            return await stream.queue.get()
        async def close_stream(self, stream_id):
            pass
        async def open_stream(self, action, payload):
            assert db_session.in_transaction() is False
            captured["action"] = action
            captured["payload"] = payload
            stream = AgentStream("stream-1")
            await stream.queue.put(StreamChunk(b"remote-ok"))
            await stream.queue.put(None)
            return {"status_code": 200, "headers": [["content-type", "text/plain"]]}, stream

    monkeypatch.setattr(proxy_module.agent_manager, "get", lambda node_id: Connection())
    response = await client.get(
        f"/proxy/{workspace_id}/port/3000/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.text == "remote-ok"
    assert captured["action"] == "proxy.http.open"
    assert captured["payload"]["custom_port"] == 3000
    assert captured["payload"]["path"] == "/health"



class _StubClientSocket:
    """Minimal WebSocket stand-in for the browser side of the proxy."""

    def __init__(self, subprotocols):
        self.scope = {"subprotocols": list(subprotocols), "query_string": b""}
        self.accepted_with = "__not_accepted__"
        self.received = []

    async def accept(self, subprotocol=None):
        self.accepted_with = subprotocol

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def send_text(self, data):
        self.received.append(data)

    async def send_bytes(self, data):
        self.received.append(data)


def _stub_agent(captured, *, echo_subprotocol=True):
    class Connection:
        async def receive_stream(self, stream):
            return await stream.queue.get()

        async def close_stream(self, stream_id):
            pass

        async def send_stream_data(self, stream_id, data, text=False):
            pass

        async def open_stream(self, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            stream = AgentStream("ws-stream-1")
            await stream.queue.put(StreamChunk(b"0ready"))
            await stream.queue.put(None)
            offered = payload.get("subprotocols") or []
            metadata = {"connected": True}
            if echo_subprotocol and offered:
                metadata["subprotocol"] = offered[0]
            return metadata, stream

    return Connection()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offered, expected",
    [
        # A terminal workspace: ttyd only serves sockets speaking "tty" and
        # closes the rest, which the browser shows as "Press Enter to
        # Reconnect". The subprotocol has to survive both proxy legs.
        (["tty"], "tty"),
        # code-server and friends offer nothing and must stay unaffected.
        ([], None),
    ],
)
async def test_proxy_negotiates_websocket_subprotocol(
    monkeypatch, offered, expected
):
    captured = {}
    monkeypatch.setattr(
        proxy_module.agent_manager, "get", lambda node_id: _stub_agent(captured)
    )
    socket = _StubClientSocket(offered)
    workspace = SimpleNamespace(
        id="ws-1",
        node_id="remote-node",
        container_name="aifactory-1-shell-abcd1234",
        host_port=7681,
        template_id="terminal-rocky",
        workspace_token="tok",
    )

    await proxy_module.proxy_remote_websocket(socket, workspace, "/ws")

    # Offered upstream so the worker can request it from the container.
    assert captured["payload"]["subprotocols"] == offered
    # Echoed back, so the browser handshake agrees with the container.
    assert socket.accepted_with == expected


@pytest.mark.asyncio
async def test_proxy_accepts_without_subprotocol_for_legacy_worker(monkeypatch):
    """A worker predating subprotocol support must not break the handshake."""
    captured = {}
    monkeypatch.setattr(
        proxy_module.agent_manager,
        "get",
        lambda node_id: _stub_agent(captured, echo_subprotocol=False),
    )
    socket = _StubClientSocket(["tty"])
    workspace = SimpleNamespace(
        id="ws-2",
        node_id="remote-node",
        container_name="aifactory-1-shell-abcd1234",
        host_port=7681,
        template_id="terminal-rocky",
        workspace_token="tok",
    )

    await proxy_module.proxy_remote_websocket(socket, workspace, "/ws")

    assert socket.accepted_with is None


def test_worker_requests_client_subprotocols_upstream():
    """The worker must forward the offered subprotocols to the container."""
    import inspect as inspect_module

    source = inspect_module.getsource(worker_module.WorkerAgent.handle_ws_open)
    # Without this the ttyd handshake is refused and the terminal never attaches.
    assert "subprotocols=subprotocols or None" in source
    assert 'payload.get("subprotocols")' in source
    # The negotiated value has to travel back so the browser handshake matches.
    assert '"subprotocol"' in source
