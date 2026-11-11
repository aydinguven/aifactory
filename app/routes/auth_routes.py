from datetime import timedelta
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthProvider,
    get_auth_provider,
    get_current_user,
    create_access_token,
)
from app import genai
from app.audit import audit_event, client_ip
from app.config import settings
from app.session_settings import session_timeout_minutes
from app.database import get_db
from app.models.user import User
from app.models.directory_settings import DirectorySettings
from app.auth.dependencies import session_cookie_names
from app.auth.ldap import DirectoryLoginRejected, DirectoryUnavailableError
from app.schemas.auth import TokenResponse
from app.quotas import user_out_for
from app.schemas.user import UserCreate, UserLogin, UserOut, UserUpdate

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def _secure_cookie(request: Request) -> bool:
    return settings.COOKIE_SECURE or request.url.scheme == "https"


def set_session_cookie(
    response: Response, token: str, request: Request, timeout_minutes: int | None = None
) -> None:
    """Set the browser session cookie with production-safe options."""
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=(timeout_minutes if timeout_minutes is not None else settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Expire the session cookie using the same attributes used to set it.

    Every name a session is read from is expired, so a cookie under the other
    release's name cannot outlive a logout.
    """
    for name in session_cookie_names():
        response.delete_cookie(
            key=name,
            httponly=True,
            samesite="lax",
            secure=_secure_cookie(request),
            path="/",
        )


@auth_router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    user_in: UserCreate,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Register a new user account."""
    directory_settings = await db.get(DirectorySettings, 1)
    if directory_settings and directory_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kurumsal dizin etkin; yeni hesaplar ilk LDAP girişinde otomatik oluşturulur.",
        )

    # Check if username exists
    stmt_user = select(User).where(User.username == user_in.username.strip())
    existing_user = (await db.execute(stmt_user)).scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bu kullanıcı adı zaten kullanılıyor.",
        )

    # Check if email exists
    stmt_email = select(User).where(User.email == user_in.email.strip().lower())
    existing_email = (await db.execute(stmt_email)).scalar_one_or_none()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bu e-posta adresi zaten kayıtlı.",
        )

    user = await auth_provider.create_user(user_in, db)
    audit_event(
        "auth.register",
        actor=user.username,
        actor_role=user.role.value,
        src_ip=client_ip(request),
        target=user.username,
    )

    # Generate JWT token
    timeout = await session_timeout_minutes(db)
    token = create_access_token(
        {"sub": user.username, "user_id": user.id, "role": user.role.value},
        expires_delta=timedelta(minutes=timeout),
    )
    
    set_session_cookie(response, token, request, timeout)

    return TokenResponse(access_token=token, token_type="bearer", user=await user_out_for(db, user))


@auth_router.post("/login", response_model=TokenResponse)
async def login_user(
    login_data: UserLogin,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Authenticate user and return JWT access token."""
    attempted = login_data.username.strip()[:255]
    src_ip = client_ip(request)
    try:
        user = await auth_provider.authenticate(
            login_data.username, login_data.password, db
        )
    except DirectoryLoginRejected as exc:
        audit_event(
            "auth.login", outcome="failure", actor=attempted, src_ip=src_ip,
            reason="directory_rejected",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except DirectoryUnavailableError as exc:
        audit_event(
            "auth.login", outcome="failure", actor=attempted, src_ip=src_ip,
            reason="directory_unavailable",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Kurumsal dizine şu anda ulaşılamıyor veya servis hesabı "
                "doğrulanamadı. Yöneticinize başvurun; yerel yönetici hesabıyla "
                "giriş yapılabilir."
            ),
        ) from exc
    if not user:
        audit_event(
            "auth.login", outcome="failure", actor=attempted, src_ip=src_ip,
            reason="invalid_credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kullanıcı adı veya parola hatalı.",
        )
    audit_event(
        "auth.login",
        actor=user.username,
        actor_role=user.role.value,
        src_ip=src_ip,
        auth_source=user.auth_source,
    )

    timeout = await session_timeout_minutes(db)
    token = create_access_token(
        {"sub": user.username, "user_id": user.id, "role": user.role.value},
        expires_delta=timedelta(minutes=timeout),
    )

    set_session_cookie(response, token, request, timeout)

    user_out = await user_out_for(db, user)
    # Directory users get a LiteLLM user and workspace key without opting in.
    await genai.auto_provision(db, user)
    return TokenResponse(access_token=token, token_type="bearer", user=user_out)


@auth_router.post("/logout")
async def logout_user(request: Request, response: Response):
    """Log out user by clearing the session cookie."""
    clear_session_cookie(response, request)
    return {"message": "Çıkış yapıldı."}


@auth_router.get("/me", response_model=UserOut)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get current authenticated user profile."""
    return await user_out_for(db, current_user)


@auth_router.put("/profile", response_model=UserOut)
async def update_profile(
    update_data: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Update current user profile information."""
    if current_user.auth_source != "internal":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kurumsal dizin profilleri AI Factory üzerinden değiştirilemez.",
        )
    if update_data.email:
        stmt = select(User).where(User.email == update_data.email.lower(), User.id != current_user.id)
        if (await db.execute(stmt)).scalar_one_or_none():
            raise HTTPException(status_code=400, detail="E-posta adresi başka bir hesap tarafından kullanılıyor.")
        current_user.email = update_data.email.lower()

    if update_data.full_name is not None:
        current_user.full_name = update_data.full_name.strip()

    if update_data.password:
        changed = await auth_provider.change_password(
            current_user, update_data.password, db
        )
        if not changed:
            raise HTTPException(
                status_code=400,
                detail="Kurumsal dizin parolası AI Factory üzerinden değiştirilemez.",
            )

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    return await user_out_for(db, current_user)
