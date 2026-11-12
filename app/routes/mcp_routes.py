"""Model Context Protocol endpoint (Streamable HTTP, tools only).

A small stateless implementation: every POST carries one JSON-RPC message and
gets a JSON response. The server never initiates messages, so there is no SSE
stream and GET answers 405, which the protocol allows. Clients authenticate
with a personal access token (Profile > MCP erişim token'ları); session
cookies are deliberately not accepted here.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.assistant.access import authenticate_token, get_assistant_settings
from app.assistant.tools import ToolError, call_tool, tools_for
from app.audit import client_ip
from app.database import get_db

mcp_router = APIRouter(tags=["MCP"])

# Newest first. Only tools are offered, and their wire format is the same in
# all of these revisions.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 256 * 1024

INSTRUCTIONS = (
    "TCMB AI Factory: browser-based AI development workspaces, GPU/CPU quotas, MLflow "
    "experiments and model deployments. Every tool acts as the owner of the access token "
    "and returns only what that user may see. Tools that change state need a token with "
    "the write scope."
)


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="aifactory-mcp"'},
    )


async def _handle(
    message: Any, request: Request, db: AsyncSession, user, allow_write: bool
) -> dict | None:
    """Answer one JSON-RPC message; None for notifications and responses."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message or not isinstance(method, str):
        return None
    request_id = message["id"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "aifactory",
                    "title": "TCMB AI Factory",
                    "version": __version__,
                },
                "instructions": INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                        "annotations": {
                            "readOnlyHint": not tool.write,
                            "destructiveHint": False,
                            "openWorldHint": False,
                        },
                    }
                    for tool in tools_for(user, allow_write=allow_write)
                ]
            },
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or (arguments is not None and not isinstance(arguments, dict)):
            return _error(request_id, -32602, "Invalid params")
        try:
            result = await call_tool(
                db, user, name, arguments,
                allow_write=allow_write, source="mcp", src_ip=client_ip(request),
            )
        except ToolError as exc:
            if exc.code == "unknown_tool":
                return _error(request_id, -32602, str(exc))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}
                ],
                "isError": False,
            },
        }
    return _error(request_id, -32601, f"Method not found: {method}")


@mcp_router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
    policy = await get_assistant_settings(db)
    if not policy.mcp_enabled:
        return JSONResponse({"detail": "MCP endpoint is disabled."}, status_code=404)
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return _unauthorized("An AI Factory access token is required.")
    authenticated = await authenticate_token(db, credentials.strip())
    if authenticated is None:
        return _unauthorized("Invalid or expired access token.")
    user, token = authenticated
    # Read once: a failed tool call rolls the session back and expires rows.
    allow_write = token.scope == "write" and bool(policy.allow_actions)

    try:
        body = await request.body()
    except ClientDisconnect:
        return Response(status_code=400)
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(_error(None, -32600, "Request too large"), status_code=413)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

    if isinstance(payload, list):
        if not payload or len(payload) > 20:
            return JSONResponse(_error(None, -32600, "Invalid Request"), status_code=400)
        answers = [
            answer
            for item in payload
            if (answer := await _handle(item, request, db, user, allow_write)) is not None
        ]
        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers, headers={"Cache-Control": "no-store"})

    answer = await _handle(payload, request, db, user, allow_write)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer, headers={"Cache-Control": "no-store"})


@mcp_router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
async def mcp_no_stream():
    """No server-initiated stream and no sessions to terminate."""
    return Response(status_code=405, headers={"Allow": "POST"})
