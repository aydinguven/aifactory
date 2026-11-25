from datetime import datetime, timedelta, timezone
from typing import Any
import bcrypt
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.base import AuthProvider
from app.config import settings
from app.models.user import User, UserRole
from app.schemas.user import UserCreate


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    salt = bcrypt.gensalt(rounds=min(max(settings.BCRYPT_ROUNDS, 4), 31))
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except Exception:
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT access token."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except (jwt.PyJWTError, Exception):
        return None


class InternalAuthProvider(AuthProvider):
    """Internal database-backed authentication provider."""

    @property
    def provider_name(self) -> str:
        return "internal"

    async def authenticate(
        self, username: str, password: str, db: AsyncSession
    ) -> User | None:
        """Authenticate user by username/email and password."""
        stmt = select(User).where(
            (User.username == username) | (User.email == username)
        )
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            return None

        if not verify_password(password, user.hashed_password):
            return None

        return user

    async def create_user(
        self, user_in: UserCreate, db: AsyncSession, is_admin: bool = False
    ) -> User:
        """Create a new local user."""
        hashed = hash_password(user_in.password)
        role = UserRole.ADMIN if is_admin else UserRole.USER
        
        user = User(
            username=user_in.username.strip(),
            email=user_in.email.strip().lower(),
            hashed_password=hashed,
            full_name=user_in.full_name.strip(),
            role=role,
            auth_source="internal",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    async def change_password(
        self, user: User, new_password: str, db: AsyncSession
    ) -> bool:
        """Update password for user."""
        user.hashed_password = hash_password(new_password)
        db.add(user)
        await db.commit()
        return True
