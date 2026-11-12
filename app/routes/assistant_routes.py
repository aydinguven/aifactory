"""Assistant panel API, MCP access tokens and the admin policy."""

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.access import (
    MAX_TOKENS_PER_USER,
    get_assistant_settings,
    hash_token,
    new_token,
    token_expired,
    verify_action,
)
from app.assistant.chat import AssistantError, run_chat, validate_history
from app.assistant.tools import TOOLS, ToolError, call_tool
from app.audit import audit_event, client_ip, mark_audited
from app.auth.dependencies import get_current_admin_user, get_current_user
from app.database import get_db
from app.models.assistant import ApiToken, AssistantSettings
from app.models.user import User

assistant_router = APIRouter(prefix="/api/assistant", tags=["Assistant"])
token_router = APIRouter(prefix="/api/tokens", tags=["Assistant"])
assistant_admin_router = APIRouter(
    prefix="/api/admin/assistant-settings", tags=["Assistant"]
)


def _role(user: User) -> str:
    return getattr(user.role, "value", str(user.role))


# --- Admin policy --------------------------------------------------------


class AssistantSettingsBody(BaseModel):
    enabled: bool = False
    model: str = Field(default="", max_length=255)
    mcp_enabled: bool = False
    allow_actions: bool = True

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str) -> str:
        return value.strip()


class AssistantSettingsOut(AssistantSettingsBody):
    updated_by: str = ""
    updated_at: datetime | None = None


def _settings_out(record: AssistantSettings) -> AssistantSettingsOut:
    return AssistantSettingsOut(
        enabled=record.enabled,
        model=record.model,
        mcp_enabled=record.mcp_enabled,
        allow_actions=record.allow_actions,
        updated_by=record.updated_by or "",
        updated_at=record.updated_at,
    )


@assistant_admin_router.get("", response_model=AssistantSettingsOut)
async def read_assistant_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _settings_out(await get_assistant_settings(db))


@assistant_admin_router.put("", response_model=AssistantSettingsOut)
async def update_assistant_settings(
    body: AssistantSettingsBody,
    request: Request,
    admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if body.enabled and not body.model:
        raise HTTPException(
            status_code=400, detail="Asistanı etkinleştirmek için bir model adı girin."
        )
    record = await db.get(AssistantSettings, 1)
    if record is None:
        record = AssistantSettings(id=1)
    record.enabled = body.enabled
    record.model = body.model
    record.mcp_enabled = body.mcp_enabled
    record.allow_actions = body.allow_actions
    record.updated_by = admin.username
    db.add(record)
    await db.commit()
    await db.refresh(record)
    mark_audited(request)
    audit_event(
        "assistant.settings.update", actor=admin.username, actor_role=_role(admin),
        src_ip=client_ip(request), enabled=body.enabled, mcp_enabled=body.mcp_enabled,
        allow_actions=body.allow_actions, model=body.model,
    )
    return _settings_out(record)


# --- Panel -----------------------------------------------------------------


@assistant_router.get("/status")
async def assistant_status(
    response: Response,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Tell the page whether to show the assistant button."""
    policy = await get_assistant_settings(db)
    response.headers["Cache-Control"] = "no-store"
    return {
        "enabled": bool(policy.enabled and policy.model),
        "mcp_enabled": bool(policy.mcp_enabled),
        "actions": bool(policy.allow_actions),
    }


class ChatBody(BaseModel):
    messages: list[dict[str, Any]] = Field(max_length=200)


@assistant_router.post("/chat")
async def assistant_chat(
    body: ChatBody,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Answer one turn as newline-delimited JSON events."""
    policy = await get_assistant_settings(db)
    if not policy.enabled or not policy.model:
        raise HTTPException(status_code=404, detail="Asistan etkin değil.")
    try:
        history = validate_history(body.messages)
    except AssistantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    src_ip = client_ip(request)

    async def stream():
        async for event in run_chat(db, current_user, history, policy, src_ip=src_ip):
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class ActionBody(BaseModel):
    token: str = Field(min_length=1, max_length=8192)


@assistant_router.post("/actions")
async def confirm_assistant_action(
    body: ActionBody,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Run an action the user confirmed on a card in the panel."""
    policy = await get_assistant_settings(db)
    if not policy.enabled or not policy.allow_actions:
        raise HTTPException(status_code=403, detail="Asistan eylemleri kapalı.")
    verified = verify_action(current_user, body.token)
    if verified is None:
        raise HTTPException(
            status_code=400,
            detail="Onay kartının süresi dolmuş veya geçersiz; isteği yeniden yazın.",
        )
    name, arguments = verified
    tool = TOOLS.get(name)
    if tool is None or not tool.write:
        raise HTTPException(status_code=400, detail="Geçersiz eylem.")
    mark_audited(request)
    try:
        result = await call_tool(
            db, current_user, name, arguments,
            allow_write=True, source="assistant", src_ip=client_ip(request),
        )
    except ToolError as exc:
        return {"ok": False, "tool": name, "message": str(exc)}
    return {"ok": True, "tool": name, "title": tool.title or name, "result": result}


# --- MCP access tokens ---------------------------------------------------


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scope: Literal["read", "write"] = "read"
    expires_days: int = Field(default=90, ge=1, le=365)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ad boş olamaz")
        return value


class TokenOut(BaseModel):
    id: int
    name: str
    scope: str
    token_prefix: str
    expires_at: datetime | None
    expired: bool
    last_used_at: datetime | None
    created_at: datetime


class TokenCreated(TokenOut):
    # Returned once; only its hash is stored.
    token: str


def _token_out(token: ApiToken) -> TokenOut:
    return TokenOut(
        id=token.id,
        name=token.name,
        scope=token.scope,
        token_prefix=token.token_prefix,
        expires_at=token.expires_at,
        expired=token_expired(token),
        last_used_at=token.last_used_at,
        created_at=token.created_at,
    )


async def _own_tokens(db: AsyncSession, user: User) -> list[ApiToken]:
    return list(
        (
            await db.execute(
                select(ApiToken)
                .where(ApiToken.user_id == user.id)
                .order_by(ApiToken.created_at.desc())
            )
        ).scalars()
    )


@token_router.get("", response_model=list[TokenOut])
async def list_tokens(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    response.headers["Cache-Control"] = "no-store"
    return [_token_out(token) for token in await _own_tokens(db, current_user)]


@token_router.post("", response_model=TokenCreated, status_code=201)
async def create_token(
    body: TokenCreate,
    request: Request,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    policy = await get_assistant_settings(db)
    if not policy.mcp_enabled:
        raise HTTPException(status_code=403, detail="MCP erişimi yönetici tarafından kapatılmış.")
    if len(await _own_tokens(db, current_user)) >= MAX_TOKENS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=f"En fazla {MAX_TOKENS_PER_USER} token oluşturabilirsiniz; önce birini silin.",
        )
    raw = new_token()
    token = ApiToken(
        user_id=current_user.id,
        name=body.name,
        token_hash=hash_token(raw),
        token_prefix=raw[:12],
        scope=body.scope,
        expires_at=datetime.now(timezone.utc) + timedelta(days=body.expires_days),
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    response.headers["Cache-Control"] = "no-store"
    mark_audited(request)
    audit_event(
        "assistant.token.create", actor=current_user.username, actor_role=_role(current_user),
        src_ip=client_ip(request), target=body.name, scope=body.scope,
        expires_days=body.expires_days,
    )
    return TokenCreated(**_token_out(token).model_dump(), token=raw)


@token_router.delete("/{token_id}", status_code=204)
async def delete_token(
    token_id: int,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    token = await db.get(ApiToken, token_id)
    if token is None or token.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Token bulunamadı.")
    name = token.name
    await db.delete(token)
    await db.commit()
    mark_audited(request)
    audit_event(
        "assistant.token.delete", actor=current_user.username, actor_role=_role(current_user),
        src_ip=client_ip(request), target=name,
    )
    return Response(status_code=204)
