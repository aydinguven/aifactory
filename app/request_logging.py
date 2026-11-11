"""ASGI middleware: request IDs, one access record per request, and audit
records for state-changing API calls.

It is plain ASGI so streaming responses and large uploads pass through
unbuffered. Only the top-level JSON field names of small admin and API
request bodies are recorded, never their values.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.requests import Request

from app.audit import AUDITED_SCOPE_KEY, audit_event
from app.auth.internal import decode_access_token
from app.log_forwarding import ACCESS_LOGGER
from app.logging_setup import request_id_var

access_logger = logging.getLogger(ACCESS_LOGGER)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Worker-token traffic and model inference are not user actions; login and
# registration are audited by their routes with the attempted username.
_NOT_AUDITED = (
    "/api/agent/",
    "/api/model-endpoints/",
    "/api/auth/login",
    "/api/auth/register",
)
_NAMED_ACTIONS = {
    ("POST", "/api/auth/logout"): "auth.logout",
}
_BODY_FIELD_LIMIT = 64 * 1024


def _is_audited(method: str, path: str) -> bool:
    return (
        method in _AUDITED_METHODS
        and path.startswith("/api/")
        and not path.startswith(_NOT_AUDITED)
    )


def _actor(request: Request) -> tuple[str, str]:
    token = ""
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        token = credentials.strip()
    if not token:
        from app.auth.dependencies import session_cookie_names

        for name in session_cookie_names():
            token = request.cookies.get(name, "")
            if token:
                break
    payload = decode_access_token(token) if token else None
    if not payload:
        return "", ""
    return str(payload.get("sub") or ""), str(payload.get("role") or "")


def _route_template(path: str, params: dict) -> str:
    """``/api/workspaces/{workspace_id}/start`` from the path and its parameters.

    Built from the path parameters instead of the matched route object,
    whose path is relative to the innermost router in recent FastAPI.
    """
    names = {
        str(value): name
        for name, value in params.items()
        if isinstance(value, (str, int)) and str(value)
    }
    if not names:
        return path
    return "/".join(
        "{" + names[segment] + "}" if segment in names else segment
        for segment in path.split("/")
    )


def _outcome(status: int) -> str:
    if status < 400:
        return "success"
    if status in (401, 403):
        return "denied"
    return "failure"


def _body_fields(chunks: list[bytes]) -> list[str]:
    try:
        value = json.loads(b"".join(chunks) or b"null")
    except ValueError:
        return []
    return sorted(str(key) for key in value)[:50] if isinstance(value, dict) else []


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        method = scope.get("method", "")
        path = scope.get("path", "")
        audited = _is_audited(method, path)
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 0
        collect = (
            audited
            and method in {"POST", "PUT", "PATCH"}
            and content_type.startswith("application/json")
            and 0 < length <= _BODY_FIELD_LIMIT
        )
        body: list[bytes] = []
        status = 500
        started = time.perf_counter()

        async def receive_wrapper():
            message = await receive()
            if collect and message.get("type") == "http.request":
                body.append(message.get("body", b""))
            return message

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", request_id.encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive_wrapper if collect else receive, send_wrapper)
        finally:
            try:
                self._record(scope, method, path, status, started, audited, body)
            except Exception:  # noqa: BLE001 - logging must never fail a request
                logging.getLogger("aifactory.logging").debug(
                    "Request logging failed", exc_info=True
                )
            request_id_var.reset(token)

    @staticmethod
    def _record(scope, method, path, status, started, audited, body) -> None:
        request = Request(scope)
        actor, role = _actor(request)
        client = scope.get("client")
        src_ip = client[0] if client else ""
        params = scope.get("path_params") or {}
        template = _route_template(path, params)
        # Capability URLs such as /share/{token} are logged by template.
        logged_path = template if any("token" in name for name in params) else path
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        access_logger.info(
            '%s %s %s %sms',
            method,
            logged_path,
            status,
            duration_ms,
            extra={
                "event": {
                    "method": method,
                    "path": logged_path,
                    "status": status,
                    "duration_ms": duration_ms,
                    "src_ip": src_ip,
                    "actor": actor,
                    "user_agent": request.headers.get("user-agent", "")[:200],
                }
            },
        )
        # Routes that record a more specific event set AUDITED_SCOPE_KEY.
        if not audited or scope.get(AUDITED_SCOPE_KEY):
            return
        audit_event(
            _NAMED_ACTIONS.get((method, template), f"{method} {template}"),
            outcome=_outcome(status),
            actor=actor,
            actor_role=role,
            src_ip=src_ip,
            target=logged_path,
            method=method,
            status=status,
            fields=_body_fields(body) if body else [],
        )
