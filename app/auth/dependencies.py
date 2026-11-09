from typing import Annotated
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.base import AuthProvider
from app.auth.internal import decode_access_token
from app.auth.ldap import HybridAuthProvider
from app.config import settings
from app.database import get_db, release_read_only_connection
from app.models.user import User, UserRole

http_bearer = HTTPBearer(auto_error=False)


def get_auth_provider() -> AuthProvider:
    """Return local authentication with optional runtime LDAP integration."""
    return HybridAuthProvider()


async def get_token_from_request(
    request: Request,
    bearer_creds: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)] = None,
) -> str | None:
    """Extract JWT token from Authorization header or cookie."""
    if bearer_creds and bearer_creds.credentials:
        return bearer_creds.credentials
    # Fallback to session cookie
    for name in session_cookie_names():
        cookie_token = request.cookies.get(name)
        if cookie_token:
            return cookie_token
    return None


def session_cookie_names() -> tuple[str, ...]:
    """The configured session cookie first, then the other release's name."""
    names = [settings.COOKIE_NAME, "aifactory_session", "devcloud_session"]
    return tuple(dict.fromkeys(names))


async def get_current_user_optional(
    token: Annotated[str | None, Depends(get_token_from_request)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Return current authenticated user if token is valid, otherwise None."""
    if not token:
        return None
    
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    
    user_id = payload.get("user_id")
    if not user_id:
        return None
    
    stmt = select(User).where(User.id == int(user_id))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    await release_read_only_connection(db)
    
    if not user or not user.is_active:
        return None
    return user


async def get_current_user(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Require an authenticated user or raise 401 Unauthorized."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Giriş yapmanız gerekiyor",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


async def get_current_admin_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require an authenticated admin user or raise 403 Forbidden."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Yönetici yetkisi gerekiyor",
        )
    return current_user
