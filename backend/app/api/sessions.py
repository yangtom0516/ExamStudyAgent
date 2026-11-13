"""Study sessions. A session starts in `setup` (files can change); Start makes it `active`
(files locked for good; chat and exams allowed)."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select

from app.api.deps import DbSession, OwnedCourse, OwnedSession, StorageDep, VectorsDep, lock_session
from app.db.models import Document, DocumentStatus, SessionStatus, StudySession
from app.services.exam_profile import ExamProfile, build_exam_profile
from app.services.storage import Storage, session_prefix
from app.services.vectors import VectorStore

router = APIRouter(tags=["sessions"])
logger = logging.getLogger(__name__)

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


@router.get("/sessions/{session_id}/exam-profile", response_model=ExamProfile)
async def get_exam_profile(study_session: OwnedSession, db: DbSession):
    """Topics covered by this session's lectures/notes, and the structure of its past exams
    (question counts per type, points). ExamWriter uses the same data to shape its exams."""
    return await build_exam_profile(db, study_session.id)


def _files(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


@router.post("/sessions/{session_id}/start", response_model=SessionOut)
async def start_session(study_session: OwnedSession, db: DbSession):
    """Lock the file list and allow chat/exams. Only when there's at least one file and all are ready.

    The UI disables the button until then; this re-check is the guarantee (stale tabs, direct API calls).
    """
    study_session = await lock_session(db, study_session.id)  # no upload/delete can slip in meanwhile
    if study_session.status != SessionStatus.SETUP:
        raise HTTPException(status.HTTP_409_CONFLICT, "Session already started")

    rows = await db.execute(
        select(Document.status, func.count()).where(Document.session_id == study_session.id).group_by(Document.status)
    )
    counts: dict[str, int] = dict(rows.all())
    total = sum(counts.values())
    failed = counts.get(DocumentStatus.FAILED, 0)
    not_ready = total - counts.get(DocumentStatus.READY, 0) - failed
    if total == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload at least one file first")
    if failed:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{_files(failed)} failed; retry or delete them first")
    if not_ready:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{_files(not_ready)} still processing")

    study_session.status = SessionStatus.ACTIVE
    study_session.started_at = datetime.now(UTC)
    await db.commit()
    return study_session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    study_session: OwnedSession, db: DbSession, storage: StorageDep, vectors: VectorsDep
) -> None:
    """Delete the session, its rows (ON DELETE CASCADE), stored files and Qdrant chunks."""
    session_id = study_session.id
    await db.delete(study_session)
    await db.commit()
    await delete_session_data(storage, vectors, [session_id])


async def delete_session_data(storage: Storage, vectors: VectorStore, session_ids: list[uuid.UUID]) -> None:
    """Remove stored files and Qdrant chunks after the rows are gone. Failures are logged, not
    raised: nothing points at the leftovers any more, so they're harmless clutter."""
    try:
        await vectors.delete_sessions(session_ids)
    except Exception:
        logger.exception("Could not delete Qdrant chunks of sessions %s", session_ids)
    for session_id in session_ids:
        try:
            await storage.delete_prefix(session_prefix(session_id))
        except Exception:
            logger.exception("Could not delete stored files of session %s", session_id)
