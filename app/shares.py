"""Port-scoped sharing credentials, separate from AI Factory login tokens."""
import time

import jwt
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import release_read_only_connection
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_share import WorkspaceShare

COOKIE_PREFIX = "aifactory_share_"
# Every prefix a release may have set; all are read and never forwarded.
COOKIE_PREFIXES = ("devcloud_share_", "aifactory_share_")


class ShareAccessError(HTTPException):
    """A classified public-share failure that remains an HTTP error."""

    def __init__(
        self,
        share_state: str,
        status_code: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ):
        self.share_state = share_state
        super().__init__(status_code=status_code, detail=detail, headers=headers)


def sign_share(record: WorkspaceShare, purpose: str = "share-link") -> str:
    payload = {"share_id": record.id, "purpose": purpose}
    if purpose == "share-grant":
        payload["exp"] = min(
            record.expires_at or int(time.time()) + 86400,
            int(time.time()) + 86400,
        )
    elif record.expires_at:
        payload["exp"] = record.expires_at
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def read_share(
    db: AsyncSession, token: str, purpose: str
) -> WorkspaceShare:
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if payload.get("purpose") != purpose:
            raise ValueError("wrong purpose")
        share_id = payload["share_id"]
    except jwt.ExpiredSignatureError as exc:
        raise ShareAccessError(
            "expired", 404, "Paylaşımın süresi doldu."
        ) from exc
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
        raise ShareAccessError(
            "invalid", 404, "Paylaşım bağlantısı geçersiz."
        ) from exc

    record = await db.get(WorkspaceShare, share_id, populate_existing=True)
    if not record:
        # Share rows are cascade-deleted with their workspace. A correctly signed
        # token with no surviving row is therefore presented as deleted.
        raise ShareAccessError(
            "deleted", 404, "Paylaşılan çalışma alanı silinmiş."
        )
    if record.revoked:
        raise ShareAccessError(
            "revoked", 404, "Paylaşım bağlantısı artık kullanılamıyor."
        )
    if record.expires_at and record.expires_at <= time.time():
        raise ShareAccessError("expired", 404, "Paylaşımın süresi doldu.")
    return record


def cookie_name(workspace_id: str, port: int) -> str:
    return f"{COOKIE_PREFIX}{workspace_id}_{port}"


def share_cookie_names(workspace_id: str, port: int) -> tuple[str, ...]:
    return tuple(f"{prefix}{workspace_id}_{port}" for prefix in COOKIE_PREFIXES)


async def share_workspace_and_owner(
    db: AsyncSession,
    record: WorkspaceShare,
    *,
    require_running: bool,
) -> tuple[Workspace, User]:
    workspace = await db.get(
        Workspace, record.workspace_id, populate_existing=True
    )
    if not workspace or workspace.status == WorkspaceStatus.DELETED:
        raise ShareAccessError(
            "deleted", 404, "Paylaşılan çalışma alanı silinmiş."
        )
    owner = await db.get(User, workspace.user_id, populate_existing=True)
    if not owner or not owner.is_active:
        raise ShareAccessError(
            "unavailable", 404, "Paylaşım kullanılamıyor."
        )
    if require_running and workspace.status != WorkspaceStatus.RUNNING:
        raise ShareAccessError(
            "unavailable",
            503,
            "Çalışma alanı çalışmıyor.",
            headers={"Retry-After": "5"},
        )
    return workspace, owner


async def shared_workspace(
    db: AsyncSession, cookies, workspace_id: str, port: int
) -> Workspace:
    token = next(
        (
            cookies[name]
            for name in share_cookie_names(workspace_id, port)
            if cookies.get(name)
        ),
        "",
    )
    record = await read_share(db, token, "share-grant")
    if record.workspace_id != workspace_id or record.port != port:
        raise ShareAccessError(
            "invalid", 403, "Paylaşım bu port için geçerli değil."
        )
    workspace, _owner = await share_workspace_and_owner(
        db, record, require_running=True
    )
    await release_read_only_connection(db)
    return workspace
