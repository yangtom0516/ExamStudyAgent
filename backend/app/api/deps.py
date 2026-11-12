"""Shared FastAPI dependencies. Endpoints declare what they need, e.g. `user: CurrentUser`.

Ownership rule: an item that exists but belongs to someone else gets the same 404 as one that
doesn't exist, so other users' IDs can't be probed.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Course, Document, SessionStatus, StudySession, User
from app.db.session import get_db
from app.services.security import read_access_token
from app.services.storage import Storage
from app.services.vectors import VectorStore

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


async def get_owned_course(course_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Course:
    """The course from the URL's {course_id}, if it belongs to the logged-in user."""
    course = await db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id))
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Course not found")
    return course


async def get_owned_session(session_id: uuid.UUID, user: CurrentUser, db: DbSession) -> StudySession:
    """The study session from the URL's {session_id}, if its course belongs to the logged-in user."""
    study_session = await db.scalar(
        select(StudySession)
        .join(Course, StudySession.course_id == Course.id)
        .where(StudySession.id == session_id, Course.user_id == user.id)
    )
    if study_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return study_session


async def get_owned_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Document:
    """The document from the URL's {document_id}, if its session's course belongs to the logged-in user."""
    document = await db.scalar(
        select(Document)
        .join(StudySession, Document.session_id == StudySession.id)
        .join(Course, StudySession.course_id == Course.id)
        .where(Document.id == document_id, Course.user_id == user.id)
    )
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return document


OwnedCourse = Annotated[Course, Depends(get_owned_course)]
OwnedSession = Annotated[StudySession, Depends(get_owned_session)]
OwnedDocument = Annotated[Document, Depends(get_owned_document)]


def require_setup(study_session: StudySession) -> None:
    """Files can only change before the session is started."""
    if study_session.status != SessionStatus.SETUP:
        raise HTTPException(status.HTTP_409_CONFLICT, "Session already started; its files are locked")


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


StorageDep = Annotated[Storage, Depends(get_storage)]


def get_vectors(request: Request) -> VectorStore:
    return request.app.state.vectors


VectorsDep = Annotated[VectorStore, Depends(get_vectors)]
