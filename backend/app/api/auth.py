"""Username + password auth. The login token lives in an httpOnly cookie that page JavaScript can't read."""

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AUTH_COOKIE, CurrentUser, DbSession, SettingsDep
from app.config import Settings
from app.db.models import User
from app.services.security import (
    create_access_token,
    hash_password,
    verify_dummy_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Letters, digits and underscores, 3-30 characters. Stored lowercase so "Tom" and "tom" are one account.
Username = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[A-Za-z0-9_]{3,30}$"),
]


class RegisterRequest(BaseModel):
    username: Username
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    # Looser than registration on purpose: a bad login should get "invalid username or password",
    # not a validation error that hints at the rules.
    username: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=30)]
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str


def _set_auth_cookie(response: Response, user_id: uuid.UUID, settings: Settings) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        create_access_token(user_id, settings),
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,  # page JavaScript can't read it
        secure=settings.cookie_secure,  # HTTPS only in production
        samesite="lax",  # not sent on cross-site form posts, which blocks most CSRF
        path="/",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserOut)
async def register(body: RegisterRequest, response: Response, db: DbSession, settings: SettingsDep):
    """Create an account and log in."""
    user = User(username=body.username, password_hash=await asyncio.to_thread(hash_password, body.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:  # unique constraint on username
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")

    _set_auth_cookie(response, user.id, settings)
    return user


@router.post("/login", response_model=UserOut)
async def login(body: LoginRequest, response: Response, db: DbSession, settings: SettingsDep):
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None:
        await asyncio.to_thread(verify_dummy_password, body.password)
        ok = False
    else:
        ok = await asyncio.to_thread(verify_password, body.password, user.password_hash)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    _set_auth_cookie(response, user.id, settings)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, settings: SettingsDep) -> None:
    """Remove the login cookie. (The token itself stays valid until it expires; fine for v1.)"""
    response.delete_cookie(
        AUTH_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    """Who is logged in. 401 if nobody."""
    return user
