"""Study sessions. A session starts in `setup` (files can change); starting it is added in step 2d."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from app.api.deps import DbSession, OwnedCourse, OwnedSession
from app.db.models import SessionStatus, StudySession

router = APIRouter(tags=["sessions"])

SessionName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class SessionCreate(BaseModel):
    name: SessionName


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_id: uuid.UUID
    name: str
    status: SessionStatus
    started_at: datetime | None
    created_at: datetime


@router.get("/courses/{course_id}/sessions", response_model=list[SessionOut])
async def list_sessions(course: OwnedCourse, db: DbSession):
    """Sessions in one course, oldest first."""
    sessions = await db.scalars(
        select(StudySession)
        .where(StudySession.course_id == course.id)
        .order_by(StudySession.created_at, StudySession.id)
    )
    return sessions.all()


@router.post("/courses/{course_id}/sessions", status_code=status.HTTP_201_CREATED, response_model=SessionOut)
async def create_session(body: SessionCreate, course: OwnedCourse, db: DbSession):
    """New sessions start empty, in `setup` status."""
    study_session = StudySession(course_id=course.id, name=body.name, status=SessionStatus.SETUP)
    db.add(study_session)
    await db.commit()
    await db.refresh(study_session)
    return study_session


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(study_session: OwnedSession):
    return study_session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(study_session: OwnedSession, db: DbSession) -> None:
    # TODO(step 2b/2c): also delete the session's files from storage and its chunks from Qdrant.
    await db.delete(study_session)
    await db.commit()
