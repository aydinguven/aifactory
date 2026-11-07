import asyncio
import hashlib
import hmac
from app.agents.transfers import upload_transfer, abort_transfer
import base64
import logging
from html import escape
from http.cookies import SimpleCookie
from typing import Annotated
from urllib.parse import parse_qsl, urlencode
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, StreamingResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import websockets

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.shares import (
    COOKIE_PREFIX,
    ShareAccessError,
    cookie_name,
    shared_workspace,
)
from app.share_pages import render_share_error, render_share_page
from app.database import get_db, release_read_only_connection
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.mlflow_deployment import MlflowDeployment, MlflowDeploymentStatus
from app.orchestrator.runtime_backend import runtime_for_node
from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager

logger = logging.getLogger("devcloud.proxy")
proxy_router = APIRouter(prefix="/proxy", tags=["Proxy"])
model_endpoint_router = APIRouter(
    prefix="/api/model-endpoints", tags=["Model Endpoints"]
)


async def get_authorized_workspace(
    workspace_id: str,
    db: AsyncSession,
    current_user: User | None,
    require_running: bool = True,
) -> Workspace:
    """Validate that the workspace exists and user has permission to access it."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")

    if not current_user:
        raise HTTPException(status_code=401, detail="Çalışma alanına erişmek için oturum açmalısınız.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Bu çalışma alanı için erişim reddedildi.")

    if require_running and workspace.status != WorkspaceStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Çalışma alanı çalışmıyor (mevcut durum: {workspace.status}). Önce çalışma alanını başlatın.",
        )

    await release_read_only_connection(db)
    return workspace


def get_upstream_path(workspace_id: str, template_id: str, path: str) -> str:
    """Keep Jupyter's configured proxy prefix while other IDEs use root paths."""
    normalized_path = path.lstrip("/")
    if "jupyter" in template_id:
        return f"/proxy/{workspace_id}/{normalized_path}"
    return f"/{normalized_path}"


STARTING_PAGE_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>__WORKSPACE_NAME__ başlatılıyor - AI Factory</title>
    <style>
        :root { color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; color: #323a47; }
        * { box-sizing: border-box; }
        body { min-height: 100vh; margin: 0; padding: 28px; background: #e6ecf5; color: #323a47; border-top: 5px solid #d50032; }
        .shell { width: min(940px, 100%); margin: 0 auto; }
        .header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
        .spinner { flex: 0 0 auto; width: 42px; height: 42px; border: 4px solid #cfd7e5; border-top-color: #d50032; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        h1 { margin: 0 0 4px; font-size: clamp(1.3rem, 3vw, 1.8rem); }
        .subtitle { margin: 0; color: #667085; }
        .panel { background: #fff; border: 1px solid #cfd7e5; border-radius: 2px; box-shadow: 0 10px 30px rgba(50, 58, 71, .12); overflow: hidden; }
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px; background: #cfd7e5; border-bottom: 1px solid #cfd7e5; }
        .metric { min-height: 88px; padding: 16px; background: #f8fafc; }
        .metric-label { display: block; margin-bottom: 9px; color: #667085; font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
        .metric-value { color: #323a47; font-size: .94rem; font-weight: 650; overflow-wrap: anywhere; }
        .pill { display: inline-flex; align-items: center; gap: 7px; }
        .pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #a3953b; }
        .pill.ready::before { background: #22c55e; }
        .pill.error::before { background: #d50032; }
        .log-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid #cfd7e5; }
        .log-head strong { font-size: .88rem; }
        .log-head span { color: #667085; font-size: .76rem; }
        pre { min-height: 280px; max-height: 48vh; margin: 0; padding: 18px; overflow: auto; background: #323a47; color: #f8fafc; font: 12px/1.55 "SFMono-Regular", Consolas, "Liberation Mono", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
        .footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 16px 18px; }
        .message { margin: 0; color: #667085; font-size: .84rem; }
        .actions { display: flex; gap: 9px; flex-wrap: wrap; }
        button, a { border: 1px solid #323a47; border-radius: 2px; padding: 8px 12px; background: #323a47; color: #fff; font: inherit; font-size: .82rem; text-decoration: none; cursor: pointer; }
        button:hover, a:hover { border-color: #d50032; background: #d50032; color: #fff; }
        code { color: #d50032; }
        @media (max-width: 620px) { body { padding: 18px 12px; } .footer { align-items: flex-start; flex-direction: column; } pre { min-height: 230px; } }
    </style>
</head>
<body>
<main class="shell" id="startup" data-status-url="__STATUS_URL__">
    <div class="header">
        <div class="spinner" id="spinner"></div>
        <div>
            <h1 id="headline">__WORKSPACE_NAME__ başlatılıyor...</h1>
            <p class="subtitle">IDE servisinin <code>__HOST_PORT__</code> host portunda hazır olması bekleniyor.</p>
        </div>
    </div>
    <section class="panel" aria-live="polite">
        <div class="status-grid">
            <div class="metric"><span class="metric-label">Çalışma Alanı</span><span class="metric-value pill" id="workspace-status">çalışıyor</span></div>
            <div class="metric"><span class="metric-label">Container</span><span class="metric-value pill" id="container-status">kontrol ediliyor</span></div>
            <div class="metric"><span class="metric-label">IDE Portu</span><span class="metric-value pill" id="port-status">kontrol ediliyor :__HOST_PORT__</span></div>
            <div class="metric"><span class="metric-label">Geçen Süre</span><span class="metric-value" id="elapsed">0 sn</span></div>
            <div class="metric"><span class="metric-label">Kontrol</span><span class="metric-value" id="attempts">0</span></div>
        </div>
        <div class="log-head"><strong>Son Container Çıktısı</strong><span>__TEMPLATE_ID__ · 2 saniyede bir yenilenir</span></div>
        <pre id="logs">Container logları alınıyor...</pre>
        <div class="footer">
            <p class="message" id="message">IDE portu bağlantı kabul ettiğinde sayfa otomatik olarak açılacaktır.</p>
            <div class="actions">
                <button type="button" id="retry">Şimdi Yeniden Dene</button>
                <a href="__DETAIL_URL__">Çalışma Alanı Ayrıntıları</a>
            </div>
        </div>
    </section>
</main>
<script>
(() => {
    const root = document.getElementById("startup");
    const statusUrl = root.dataset.statusUrl;
    const logs = document.getElementById("logs");
    const message = document.getElementById("message");
    const headline = document.getElementById("headline");
    const spinner = document.getElementById("spinner");
    const startedAt = Date.now();
    let attempts = 0;
    let opening = false;

    const setPill = (id, text, state = "") => {
        const node = document.getElementById(id);
        node.textContent = text;
        node.className = `metric-value pill ${state}`.trim();
    };

    const updateElapsed = () => {
        const seconds = Math.floor((Date.now() - startedAt) / 1000);
        document.getElementById("elapsed").textContent = `${seconds} sn`;
        if (seconds >= 60 && !opening) {
            message.textContent = "Başlatma beklenenden uzun sürüyor. Takılan adım için aşağıdaki son Container çıktısını inceleyin.";
        }
    };

    async function refreshStatus() {
        attempts += 1;
        document.getElementById("attempts").textContent = String(attempts);
        try {
            const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) throw new Error(`tanılama HTTP ${response.status} döndürdü`);
            const data = await response.json();
            const containerReady = data.container_status === "running";
            setPill("workspace-status", data.workspace_status === "running" ? "çalışıyor" : data.workspace_status, data.workspace_status === "running" ? "ready" : "error");
            setPill("container-status", containerReady ? "çalışıyor" : data.container_status, containerReady ? "ready" : "error");
            setPill("port-status", data.port_ready ? `erişilebilir :${data.host_port}` : `bekleniyor :${data.host_port}`, data.port_ready ? "ready" : "");
            logs.textContent = data.logs || "Container çalışıyor ancak henüz çıktı üretmedi.";
            logs.scrollTop = logs.scrollHeight;
            if (data.error_message) {
                message.textContent = `Çalışma alanı hatası: ${data.error_message}`;
            }

            if (data.port_ready && containerReady && !opening) {
                opening = true;
                headline.textContent = "IDE hazır";
                message.textContent = "Bağlantı kuruldu. Çalışma alanı açılıyor...";
                spinner.style.animationDuration = ".35s";
                setTimeout(() => window.location.reload(), 650);
            }
        } catch (error) {
            setPill("port-status", "tanılama kullanılamıyor", "error");
            message.textContent = `Tanılama yenilenemedi: ${error.message}. Otomatik olarak yeniden deneniyor.`;
        }
    }

    document.getElementById("retry").addEventListener("click", () => window.location.reload());
    updateElapsed();
    refreshStatus();
    setInterval(updateElapsed, 1000);
    setInterval(refreshStatus, 2000);
})();
</script>
</body>
</html>"""


def render_starting_page(workspace: Workspace) -> str:
    """Render a token-free, live startup diagnostics page."""
    return (
        STARTING_PAGE_HTML
        .replace("__WORKSPACE_NAME__", escape(workspace.name))
        .replace("__WORKSPACE_ID__", escape(workspace.id))
        .replace("__HOST_PORT__", str(workspace.host_port))
        .replace("__TEMPLATE_ID__", escape(workspace.template_id))
        .replace(
            "__STATUS_URL__",
            escape(f"/proxy/{workspace.id}/_devcloud/status?tail=120", quote=True),
        )
        .replace("__DETAIL_URL__", escape(f"/workspaces/{workspace.id}", quote=True))
    )


async def port_is_ready(host_port: int) -> bool:
    """Check whether the loopback-only IDE port is accepting TCP connections."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", host_port),
            timeout=0.6,
        )
    except (OSError, TimeoutError):
        return False

    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def _forward_headers(request: Request, workspace: Workspace, custom_port: int | None = None) -> dict[str, str]:
    excluded = {"host", "content-length", "transfer-encoding", "connection", "authorization", "cookie"}
    headers = {key: value for key, value in request.headers.items() if key.lower() not in excluded}
    incoming_cookie = request.headers.get("cookie", "")
    if incoming_cookie:
        parsed = SimpleCookie()
        parsed.load(incoming_cookie)
        parsed.pop(settings.COOKIE_NAME, None)
        for name in list(parsed):
            if name.startswith(COOKIE_PREFIX):
                parsed.pop(name, None)
        if parsed:
            headers["Cookie"] = "; ".join(
                f"{name}={morsel.value}" for name, morsel in parsed.items()
            )
    if custom_port is None and "jupyter" in workspace.template_id:
        public_host = request.headers.get("host")
        if public_host:
            headers["Host"] = public_host
            headers["X-Forwarded-Host"] = public_host
        headers["X-Forwarded-Proto"] = request.url.scheme
        headers["Authorization"] = f"token {workspace.workspace_token}"
    return headers


def _workspace_websocket_query(websocket: WebSocket) -> str:
    """Do not forward the AI Factory query-token credential into the container."""
    raw = websocket.scope.get("query_string", b"").decode()
    return urlencode([(key, value) for key, value in parse_qsl(raw, keep_blank_values=True) if key != "token"])


def _client_subprotocols(websocket: WebSocket) -> list[str]:
    """Return the WebSocket subprotocols the browser offered, in preference order."""
    return [str(item) for item in websocket.scope.get("subprotocols") or []]


async def proxy_remote_http(
    workspace: Workspace,
    request: Request,
    path: str,
    custom_port: int | None = None,
    public_share: bool = False,
):
    """Stream an HTTP request through the worker-initiated tunnel."""
    if not workspace.node_id:
        raise RuntimeError("Remote proxy için node_id gereklidir.")
    connection = agent_manager.get(workspace.node_id)
    payload = {
        "workspace_id": workspace.id,
        "container_name": workspace.container_name,
        "host_port": workspace.host_port,
        "custom_port": custom_port,
        "method": request.method,
        "path": path,
        "query": request.url.query,
        "headers": _forward_headers(request, workspace, custom_port),
        "body": "",
    }
    body_transfer_id = None
    try:
        async def request_chunks():
            async for chunk in request.stream():
                yield chunk
        # Empty GET/HEAD requests need no spool or protocol extension.
        if request.method not in {"GET", "HEAD"} or request.headers.get("content-length", "0") != "0" or request.headers.get("transfer-encoding"):
            result = await upload_transfer(connection, request_chunks(), payload={"purpose": "http"}, limit=settings.PROXY_MAX_REQUEST_BYTES)
            body_transfer_id = result["transfer_id"]
            payload["body_transfer_id"] = body_transfer_id
            payload["headers"]["Content-Length"] = str(result["size"])
        metadata, stream = await connection.open_stream("proxy.http.open", payload)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (AgentUnavailable, AgentCommandError, TimeoutError) as exc:
        if request.method == "GET" and "text/html" in request.headers.get(
            "accept", ""
        ):
            logger.info(
                "Workspace %s worker proxy is not ready: %s",
                workspace.id,
                exc,
            )
            if public_share:
                return render_share_page(
                    request,
                    state="unavailable",
                    title="Paylaşılan uygulamaya şu anda erişilemiyor",
                    message="Uygulama henüz hazır olmayabilir veya geçici olarak çevrim dışı olabilir.",
                    icon="…",
                    status_code=503,
                    retryable=True,
                    extra_headers={"Retry-After": "5"},
                )
            return HTMLResponse(
                content=render_starting_page(workspace),
                status_code=200,
                headers={"Cache-Control": "no-store", "Retry-After": "2"},
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if body_transfer_id:
            await abort_transfer(connection, body_transfer_id)

    header_pairs = metadata.get("headers") or []
    response_headers: dict[str, str] = {}
    set_cookies: list[str] = []
    for key, value in header_pairs:
        lower = key.lower()
        if lower == "set-cookie":
            set_cookies.append(value)
        elif lower not in {"transfer-encoding", "content-length", "connection"}:
            response_headers[key] = value

    async def response_stream():
        try:
            while True:
                item = await connection.receive_stream(stream)
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item.data
        finally:
            await connection.close_stream(stream.id)

    response = StreamingResponse(
        response_stream(),
        status_code=int(metadata.get("status_code", 502)),
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )
    for cookie in set_cookies:
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


async def proxy_remote_websocket(
    websocket: WebSocket,
    workspace: Workspace,
    path: str,
    custom_port: int | None = None,
) -> None:
    if not workspace.node_id:
        raise RuntimeError("Remote proxy için node_id gereklidir.")
    connection = agent_manager.get(workspace.node_id)
    headers = {}
    if custom_port is None and "jupyter" in workspace.template_id:
        headers["Authorization"] = f"token {workspace.workspace_token}"
    requested_subprotocols = _client_subprotocols(websocket)
    metadata, stream = await connection.open_stream(
        "proxy.websocket.open",
        {
            "workspace_id": workspace.id,
            "container_name": workspace.container_name,
            "host_port": workspace.host_port,
            "custom_port": custom_port,
            "path": path,
            "query": _workspace_websocket_query(websocket),
            "headers": headers,
            "subprotocols": requested_subprotocols,
        },
    )
    if not metadata.get("connected"):
        raise AgentCommandError("Worker WebSocket hedefine bağlanamadı.")

    # Accept only once the container has agreed, so the subprotocol reported to
    # the browser is the one actually negotiated upstream. Terminal workspaces
    # depend on this: ttyd closes any socket that does not speak "tty".
    negotiated = metadata.get("subprotocol") or None
    if negotiated is not None and negotiated not in requested_subprotocols:
        # A worker predating subprotocol support, or a confused upstream.
        negotiated = None
    await websocket.accept(subprotocol=negotiated)

    async def forward_to_worker():
        try:
            while True:
                message = await websocket.receive()
                if message.get("text") is not None:
                    await connection.send_stream_data(
                        stream.id, message["text"].encode("utf-8"), text=True
                    )
                elif message.get("bytes") is not None:
                    await connection.send_stream_data(stream.id, message["bytes"])
                elif message.get("type") == "websocket.disconnect":
                    break
        finally:
            await connection.close_stream(stream.id)

    async def forward_to_client():
        while True:
            item = await connection.receive_stream(stream)
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if item.is_text:
                await websocket.send_text(item.data.decode("utf-8"))
            else:
                await websocket.send_bytes(item.data)

    tasks = [asyncio.create_task(forward_to_worker()), asyncio.create_task(forward_to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.close_stream(stream.id)


@proxy_router.get("/{workspace_id}/_devcloud/status")
async def proxy_workspace_status(
    workspace_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    response: Response,
    tail: int = 120,
):
    """Return authenticated startup diagnostics without exposing workspace tokens."""
    workspace = await get_authorized_workspace(
        workspace_id,
        db,
        current_user,
        require_running=False,
    )
    response.headers["Cache-Control"] = "no-store"
    safe_tail = max(20, min(tail, 200))
    runtime = runtime_for_node(workspace.node_id)
    try:
        container_status, ready, logs = await asyncio.gather(
            runtime.get_container_status(workspace.container_name),
            runtime.port_ready(workspace.container_name, workspace.host_port),
            runtime.get_logs(workspace.container_name, tail=safe_tail),
        )
    except AgentUnavailable:
        container_status, ready, logs = "worker-offline", False, "Worker tunnel bağlantısı yok."
    return {
        "workspace_id": workspace.id,
        "workspace_status": workspace.status.value,
        "container_status": container_status,
        "host_port": workspace.host_port,
        "port_ready": ready,
        "logs": logs,
        "error_message": workspace.error_message,
        "last_started_at": (
            workspace.last_started_at.isoformat() if workspace.last_started_at else None
        ),
    }


@proxy_router.api_route(
    "/{workspace_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_http_request(
    workspace_id: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Proxy HTTP requests to the target container's local port."""
    custom_parts = path.split("/", 2)
    if len(custom_parts) >= 2 and custom_parts[0] == "port" and custom_parts[1].isdigit():
        return await proxy_custom_port_http(
            workspace_id=workspace_id,
            port=int(custom_parts[1]),
            request=request,
            db=db,
            current_user=current_user,
            path=custom_parts[2] if len(custom_parts) == 3 else "",
        )
    workspace = await get_authorized_workspace(workspace_id, db, current_user)
    upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)
    return await proxy_remote_http(workspace, request, upstream_path)


@proxy_router.websocket("/{workspace_id}/{path:path}")
async def proxy_websocket(
    websocket: WebSocket,
    workspace_id: str,
    path: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Proxy WebSocket traffic (critical for code-server terminals and IDE sync).

    The client socket is accepted inside `proxy_remote_websocket`, once the
    container has negotiated a subprotocol, so the handshake can echo it back.
    Rejection paths accept first and then close, which is what lets the browser
    read the close code and reason.
    """

    async def reject(code: int, reason: str) -> None:
        await websocket.accept()
        await websocket.close(code=code, reason=reason)

    # Extract auth token from query params or cookies
    token = websocket.query_params.get("token") or websocket.cookies.get(settings.COOKIE_NAME)
    from app.auth.internal import decode_access_token
    payload = decode_access_token(token) if token else None
    user_id = payload.get("user_id") if payload else None

    current_user = None
    if user_id:
        stmt = select(User).where(User.id == int(user_id))
        res = await db.execute(stmt)
        current_user = res.scalar_one_or_none()

    custom_port = None
    custom_parts = path.split("/", 2)
    if len(custom_parts) >= 2 and custom_parts[0] == "port" and custom_parts[1].isdigit():
        custom_port = int(custom_parts[1])
    using_share = False
    try:
        try:
            workspace = await get_authorized_workspace(workspace_id, db, current_user if current_user and current_user.is_active else None)
        except HTTPException as exc:
            if custom_port is None or exc.status_code not in {401, 403} or not websocket.cookies.get(cookie_name(workspace_id, custom_port)):
                raise
            workspace = await shared_workspace(db, websocket.cookies, workspace_id, custom_port)
            using_share = True
    except HTTPException as exc:
        await reject(4003, str(exc.detail))
        return
    if custom_port is not None:
        if not 1 <= custom_port <= 65535:
            await reject(4003, "Geçersiz port")
            return
        upstream_path = "/" + (custom_parts[2] if len(custom_parts) == 3 else "")
    else:
        upstream_path = get_upstream_path(workspace.id, workspace.template_id, path)

    async def watch_share():
        # Recheck grants during long-lived sockets so revoke/expiry closes access.
        while True:
            await asyncio.sleep(2)
            await shared_workspace(db, websocket.cookies, workspace_id, custom_port)

    close_code = 1000
    close_reason = ""
    try:
        if using_share:
            tasks = [asyncio.create_task(proxy_remote_websocket(websocket, workspace, upstream_path, custom_port=custom_port)),
                     asyncio.create_task(watch_share())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        else:
            await proxy_remote_websocket(websocket, workspace, upstream_path, custom_port=custom_port)
    except Exception as exc:
        logger.warning(
            "Worker WebSocket proxy closed for workspace=%s path=%s: %s",
            workspace_id,
            path,
            exc,
        )
        close_code = 1011
        close_reason = "Workspace proxy stream failed"
    finally:
        try:
            # Safe whether or not the handshake completed: ASGI allows
            # `websocket.close` while still connecting, which rejects it.
            await websocket.close(code=close_code, reason=close_reason)
        except Exception:
            pass


@proxy_router.api_route(
    "/{workspace_id}/port/{port}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@proxy_router.api_route(
    "/{workspace_id}/port/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_custom_port_http(
    workspace_id: str,
    port: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    path: str = "",
):
    """Proxy HTTP traffic to a custom secondary port running inside the container (e.g. 5173, 5000, 3000)."""
    if not 1 <= port <= 65535:
        raise HTTPException(422, "Geçersiz port.")
    using_share = False
    try:
        workspace = await get_authorized_workspace(workspace_id, db, current_user)
    except HTTPException as exc:
        if exc.status_code not in {401, 403} or not request.cookies.get(
            cookie_name(workspace_id, port)
        ):
            raise
        try:
            workspace = await shared_workspace(
                db, request.cookies, workspace_id, port
            )
        except ShareAccessError as share_error:
            if request.method == "GET" and "text/html" in request.headers.get(
                "accept", ""
            ):
                return render_share_error(request, share_error)
            raise
        using_share = True

    subpath = f"/{path.lstrip('/')}"
    response = await proxy_remote_http(
        workspace,
        request,
        subpath,
        custom_port=port,
        public_share=using_share,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response



async def _authorized_model_endpoint(
    deployment_id: str,
    request: Request,
    db: AsyncSession,
    current_user: User | None,
) -> tuple[MlflowDeployment, Workspace]:
    deployment = await db.get(MlflowDeployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Model endpoint bulunamadı.")

    authorized = bool(
        current_user
        and current_user.is_active
        and (
            current_user.id == deployment.user_id
            or current_user.role == UserRole.ADMIN
        )
    )
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not authorized and scheme.lower() == "bearer" and token:
        supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        authorized = hmac.compare_digest(supplied_hash, deployment.access_token_hash)
    if not authorized:
        raise HTTPException(
            status_code=401,
            detail="Model endpoint tokenı veya kullanıcı oturumu gereklidir.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if deployment.status != MlflowDeploymentStatus.RUNNING or not deployment.workspace_id:
        raise HTTPException(
            status_code=503,
            detail=f"Model servisi hazır değil (durum: {deployment.status.value}).",
            headers={"Retry-After": "5"},
        )
    workspace = await db.get(Workspace, deployment.workspace_id)
    if workspace is None or workspace.status != WorkspaceStatus.RUNNING:
        raise HTTPException(
            status_code=503,
            detail="Model servis workspace'i çalışmıyor.",
            headers={"Retry-After": "5"},
        )
    await release_read_only_connection(db)
    return deployment, workspace


@model_endpoint_router.api_route(
    "/{deployment_id}/{path:path}",
    methods=["GET", "POST", "OPTIONS", "HEAD"],
)
async def invoke_model_endpoint(
    deployment_id: str,
    path: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Expose a stable, token-authenticated path to one MLflow scoring service."""
    normalized_path = path.strip("/")
    if normalized_path not in {"ping", "health", "invocations"}:
        raise HTTPException(status_code=404, detail="Model endpoint yolu desteklenmiyor.")
    _deployment, workspace = await _authorized_model_endpoint(
        deployment_id, request, db, current_user
    )
    response = await proxy_remote_http(
        workspace,
        request,
        f"/{normalized_path}",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
