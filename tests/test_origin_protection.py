"""Cross-origin protection for cookie sessions and proxied workspace responses."""

import pytest
from httpx import AsyncClient
from starlette.datastructures import Headers

import app.proxy.router as proxy_module
from app.config import settings
from app.proxy.router import contain_set_cookie
from app.security.origin import SameOriginMiddleware, is_same_origin

FOREIGN = "http://intranet-other.example"


async def _register(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/auth/register",
        json={"username": name, "email": f"{name}@test.com", "password": "Password123!"},
    )
    assert response.status_code == 201
    # The client now holds the session cookie, as a browser would.
    assert client.cookies
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_no_cors_headers_are_sent_to_foreign_origins(client: AsyncClient):
    await _register(client, "origin_cors_user")
    response = await client.get("/api/auth/me", headers={"Origin": FOREIGN})
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers

    preflight = await client.options(
        "/api/auth/profile",
        headers={"Origin": FOREIGN, "Access-Control-Request-Method": "PUT"},
    )
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [FOREIGN, "null", "http://test:8080"])
async def test_cookie_write_from_another_origin_is_rejected(client: AsyncClient, origin):
    await _register(client, "origin_write_user")
    response = await client.put(
        "/api/auth/profile", json={"full_name": "Changed"}, headers={"Origin": origin}
    )
    assert response.status_code == 403
    me = await client.get("/api/auth/me")
    assert me.json()["full_name"] != "Changed"


@pytest.mark.asyncio
async def test_cookie_write_from_a_foreign_referer_is_rejected(client: AsyncClient):
    await _register(client, "origin_referer_user")
    response = await client.put(
        "/api/auth/profile",
        json={"full_name": "Changed"},
        headers={"Referer": f"{FOREIGN}/page"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_same_origin_and_non_browser_writes_still_work(client: AsyncClient):
    token = await _register(client, "origin_ok_user")
    same_origin = await client.put(
        "/api/auth/profile", json={"full_name": "Same"}, headers={"Origin": "http://test"}
    )
    assert same_origin.status_code == 200

    # Browsers always send Origin on writes; its absence means a script.
    no_origin = await client.put("/api/auth/profile", json={"full_name": "Script"})
    assert no_origin.status_code == 200

    # Bearer clients are not cookie-authenticated and are not checked.
    bearer = await client.put(
        "/api/auth/profile",
        json={"full_name": "Bearer"},
        headers={"Origin": FOREIGN, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 200


@pytest.mark.asyncio
async def test_reads_are_not_checked(client: AsyncClient):
    await _register(client, "origin_read_user")
    response = await client.get("/api/auth/me", headers={"Origin": FOREIGN})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("origin", "host", "expected"),
    [
        ("https://aifactory.example", "aifactory.example", True),  # Nginx strips the port
        ("http://aifactory.example", "aifactory.example", True),
        ("https://aifactory.example:8443", "aifactory.example", False),
        ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
        ("http://127.0.0.1:9000", "127.0.0.1:8000", False),
        ("https://other.example", "aifactory.example", False),
        ("https://AIFACTORY.example", "aifactory.example", True),
        ("null", "aifactory.example", False),
        ("not a url", "aifactory.example", False),
    ],
)
def test_same_origin_rules(origin, host, expected):
    assert is_same_origin(Headers({"origin": origin, "host": host})) is expected


def test_trusted_origins_setting(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_ORIGINS", "https://portal.example, https://x.example:8443")
    headers = Headers({"origin": "https://portal.example", "host": "aifactory.example"})
    assert is_same_origin(headers)
    headers = Headers({"origin": "https://x.example:8443", "host": "aifactory.example"})
    assert is_same_origin(headers)


@pytest.mark.asyncio
async def test_cross_origin_websocket_upgrade_is_closed_before_the_app_runs():
    called = []

    async def inner_app(scope, receive, send):
        called.append(scope)

    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/proxy/abc/terminal/ws",
        "headers": [
            (b"host", b"aifactory.example"),
            (b"origin", FOREIGN.encode()),
            (b"cookie", b"aifactory_session=token"),
        ],
    }
    await SameOriginMiddleware(inner_app)(scope, receive, send)

    assert called == []
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]

    scope["headers"][1] = (b"origin", b"https://aifactory.example")
    await SameOriginMiddleware(inner_app)(scope, receive, send)
    assert len(called) == 1


PREFIX = "/proxy/ws-1/"


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        # Jupyter's own cookies under the workspace prefix pass unchanged.
        (f"_xsrf=v; Path={PREFIX}", f"_xsrf=v; Path={PREFIX}"),
        ("username-jupyter=v; HttpOnly; Path=/proxy/ws-1", "username-jupyter=v; HttpOnly; Path=/proxy/ws-1"),
        # Paths outside the workspace are pulled back inside it.
        ("app=v; Path=/", f"app=v; Path={PREFIX}"),
        ("app=v; Path=/proxy/ws-10/", f"app=v; Path={PREFIX}"),
        ("app=v; Path=/api", f"app=v; Path={PREFIX}"),
        # Domain would reach sibling hosts; it is removed.
        ("app=v; Domain=.example.com; Secure", "app=v; Secure"),
        ("app=v", "app=v"),
        # AI Factory's own cookies can never be set by a workspace.
        (f"{settings.COOKIE_NAME}=attacker-token; Path=/", None),
        ("aifactory_session=x", None),
        ("AIFACTORY_SESSION=x", None),
        ("aifactory_share_ws-2_8080=x; Path=/", None),
        ("devcloud_share_ws-2_8080=x", None),
        ("=nameless", None),
    ],
)
def test_contain_set_cookie(upstream, expected):
    assert contain_set_cookie(upstream, PREFIX) == expected


def test_model_endpoints_drop_every_cookie():
    assert contain_set_cookie("session=v; Path=/", None) is None


@pytest.mark.asyncio
async def test_proxy_drops_dangerous_upstream_headers_and_cookies(
    client: AsyncClient, monkeypatch
):
    token = await _register(client, "origin_proxy_user")
    workspace = (
        await client.post(
            "/api/workspaces",
            json={"name": "Header IDE", "template_id": "jupyter-python", "flavor_id": "t1.nano"},
            headers={"Authorization": f"Bearer {token}"},
        )
    ).json()
    workspace_id = workspace["id"]

    class Upstream:
        status_code = 200
        headers = proxy_module.httpx.Headers(
            [
                ("content-type", "text/html; charset=utf-8"),
                ("service-worker-allowed", "/"),
                ("clear-site-data", '"cookies"'),
                ("set-cookie", f"{settings.COOKIE_NAME}=attacker; Path=/"),
                ("set-cookie", "theme=dark; Path=/"),
            ]
        )

        async def aiter_raw(self, chunk_size=None):
            yield b"<html>app</html>"

        async def aclose(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=False):
            return Upstream()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_module.httpx, "AsyncClient", Client)

    response = await client.get(
        f"/proxy/{workspace_id}/", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert "service-worker-allowed" not in response.headers
    assert "clear-site-data" not in response.headers
    assert response.headers.get_list("set-cookie") == [
        f"theme=dark; Path=/proxy/{workspace_id}/"
    ]
