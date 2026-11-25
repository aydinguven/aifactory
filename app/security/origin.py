"""Reject cross-origin requests that ride on the browser's cookies.

The UI, the API and the workspace proxy share one origin and authenticate
browsers with a SameSite=Lax session cookie. Lax still sends that cookie from
any same-site page (another host under the same registrable domain, or another
port on the controller), so state-changing requests and WebSocket upgrades
must also come from the controller's own origin.

Requests without cookies, or with an Authorization header (workers, MCP and
API clients), are not affected. Browsers always send ``Origin`` on these
requests; a request with neither ``Origin`` nor ``Referer`` comes from a
non-browser client and is let through.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from app.config import settings

logger = logging.getLogger("aifactory.security")

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _origin_key(value: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an origin or URL, or None when unusable."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or DEFAULT_PORTS[scheme]


def trusted_origins() -> set[tuple[str, str, int]]:
    keys = (_origin_key(item) for item in settings.TRUSTED_ORIGINS.split(","))
    return {key for key in keys if key}


def is_same_origin(headers: Headers) -> bool:
    origin = headers.get("origin")
    if origin is None:
        origin = headers.get("referer")
        if not origin:
            return True
    key = _origin_key(origin)  # "null" and other opaque origins give None
    if key is None:
        return False
    if key in trusted_origins():
        return True
    try:
        host = urlsplit("//" + headers.get("host", ""))
        host_name, host_port = host.hostname, host.port
    except ValueError:
        return False
    if not host_name or host_name.lower() != key[1]:
        return False
    if host_port is None:
        # The generated Nginx ingress forwards Host without a port and
        # listens only on 80 and 443.
        return key[2] in (80, 443)
    return host_port == key[2]


class SameOriginMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket" or (
            kind == "http" and scope["method"].upper() in UNSAFE_METHODS
        ):
            headers = Headers(scope=scope)
            if (
                "cookie" in headers
                and "authorization" not in headers
                and not is_same_origin(headers)
            ):
                logger.warning(
                    "Cross-origin %s %s rejected (Origin: %s)",
                    scope.get("method", "WEBSOCKET"),
                    scope.get("path", ""),
                    headers.get("origin") or headers.get("referer") or "-",
                )
                if kind == "websocket":
                    await WebSocketClose(code=1008)(scope, receive, send)
                else:
                    await JSONResponse(
                        {"detail": "Bu istek başka bir kökenden geldiği için reddedildi."},
                        status_code=403,
                    )(scope, receive, send)
                return
        await self.app(scope, receive, send)
