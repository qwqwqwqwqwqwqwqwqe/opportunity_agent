from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.security import hash_password, make_access_token, make_refresh_token, token_hash, verify_password
from ..db.models import AuthSession, User
from ..repositories import Repository


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = Repository(session)

    async def register(self, email: str, password: str) -> User:
        normalized = email.strip().lower()
        if await self.repo.get_user_by_email(normalized):
            raise ValueError("email is already registered")
        user = User(email=normalized, password_hash=hash_password(password))
        self.session.add(user)
        await self.session.flush()
        return user

    async def authenticate(self, email: str, password: str) -> User | None:
        user = await self.repo.get_user_by_email(email.strip().lower())
        return user if user and user.is_active and verify_password(password, user.password_hash) else None

    async def issue_tokens(self, user: User) -> tuple[str, str]:
        refresh = make_refresh_token()
        self.session.add(AuthSession(
            user_id=user.id,
            token_hash=token_hash(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_days),
        ))
        await self.session.flush()
        return make_access_token(user.id), refresh

    async def rotate_refresh(self, raw_refresh: str) -> tuple[User, str, str] | None:
        now = datetime.now(timezone.utc)
        # The database compares timestamps and atomically consumes the token;
        # SQLite does not preserve datetime tzinfo on Python round trips.
        consumed = await self.session.execute(update(AuthSession).where(
            AuthSession.token_hash == token_hash(raw_refresh),
            AuthSession.revoked_at.is_(None), AuthSession.expires_at > now,
        ).values(revoked_at=now).returning(AuthSession.user_id))
        user_id = consumed.scalar_one_or_none()
        if user_id is None:
            return None
        user = await self.repo.get_user(user_id)
        if not user or not user.is_active:
            return None
        access, refresh = await self.issue_tokens(user)
        return user, access, refresh

    async def revoke_refresh(self, raw_refresh: str) -> None:
        item = await self.session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw_refresh)))
        if item and item.revoked_at is None:
            item.revoked_at = datetime.now(timezone.utc)
