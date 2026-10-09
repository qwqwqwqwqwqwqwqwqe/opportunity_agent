"""Authentication primitives.  Refresh tokens are opaque and only hashed at rest."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import settings

_passwords = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must contain at least 10 characters")
    return _passwords.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _passwords.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def make_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes), "typ": "access"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def parse_access_token(token: str) -> str:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != "access" or not isinstance(payload.get("sub"), str):
        raise jwt.InvalidTokenError("not an access token")
    return payload["sub"]


def make_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
