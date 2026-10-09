from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import parse_access_token
from ..db.models import User
from ..db.session import get_session

bearer = HTTPBearer(auto_error=False)
DBSession = Annotated[AsyncSession, Depends(get_session)]


async def current_user(request: Request, session: DBSession,
                       credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None) -> User:
    token = credentials.credentials if credentials else request.cookies.get("access_token")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        user_id = parse_access_token(token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired access token")
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "inactive user")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
