"""Shared FastAPI dependencies. Endpoints declare what they need, e.g. `user: CurrentUser`."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db
from app.services.security import read_access_token

AUTH_COOKIE = "access_token"

DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(request: Request, db: DbSession, settings: SettingsDep) -> User:
    token = request.cookies.get(AUTH_COOKIE)
    user_id = read_access_token(token, settings) if token else None
    user = await db.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
