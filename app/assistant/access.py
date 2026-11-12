"""Assistant settings, MCP access tokens and signed action confirmations."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.assistant import ApiToken, AssistantSettings
from app.models.user import User
from app.time_utils import ensure_utc

TOKEN_PREFIX = "aif_"
MAX_TOKENS_PER_USER = 10
SCOPES = ("read", "write")
# A confirmation card in the panel stays valid this long.
ACTION_TTL_SECONDS = 300
_ACTION_AUDIENCE = "aifactory-assistant-action"
# last_used_at is refreshed at most this often.
_LAST_USED_RESOLUTION = timedelta(minutes=1)


async def get_assistant_settings(db: AsyncSession) -> AssistantSettings:
    """Return the stored policy, or the (everything off) defaults."""
    record = await db.get(AssistantSettings, 1)
    if record is None:
        record = AssistantSettings(
            id=1, enabled=False, model="", mcp_enabled=False, allow_actions=True
        )
    return record


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_expired(token: ApiToken, now: datetime | None = None) -> bool:
    if token.expires_at is None:
        return False
    return ensure_utc(token.expires_at) <= (now or datetime.now(timezone.utc))


async def authenticate_token(db: AsyncSession, raw: str) -> tuple[User, ApiToken] | None:
    """Resolve a bearer token to its active owner, or None."""
    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 200:
        return None
    token = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if token is None or token_expired(token):
        return None
    user = await db.get(User, token.user_id)
    if user is None or not user.is_active:
        return None
    now = datetime.now(timezone.utc)
    if token.last_used_at is None or now - ensure_utc(token.last_used_at) > _LAST_USED_RESOLUTION:
        token.last_used_at = now
        db.add(token)
        await db.commit()
    return user, token


def sign_action(user: User, tool: str, arguments: dict) -> str:
    """Seal a proposed write action so only this user can confirm exactly it."""
    payload = {
        "aud": _ACTION_AUDIENCE,
        "sub": str(user.id),
        "tool": tool,
        "args": arguments,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=ACTION_TTL_SECONDS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def verify_action(user: User, token: str) -> tuple[str, dict] | None:
    """Return (tool, arguments) for a valid confirmation of this user's action."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=_ACTION_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None
    if payload.get("sub") != str(user.id):
        return None
    tool, arguments = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return None
    return tool, arguments
