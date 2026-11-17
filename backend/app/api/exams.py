"""Mock exams written by ExamWriter. Exams are created by the agent (services/exams.py,
save_exam), not through this API; these endpoints only read them.

The answer key never leaves the server here: GET /exams/{id} returns each question without its
answer, rubric or sources (step 5 shows them on the results page, once the attempt is graded).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession, OwnedExam, OwnedSession
from app.db.models import Exam
from app.services.exams import PublicQuestion

router = APIRouter(tags=["exams"])


class ExamSummary(BaseModel):
    id: uuid.UUID
    title: str
    question_count: int
    total_points: int
    created_at: datetime


class ExamOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    title: str
    request: str
    created_at: datetime
    questions: list[PublicQuestion]


@router.get("/sessions/{session_id}/exams", response_model=list[ExamSummary])
async def list_exams(study_session: OwnedSession, db: DbSession):
    """The session's exams, newest first."""
    exams = await db.scalars(
        select(Exam).where(Exam.session_id == study_session.id).order_by(Exam.created_at.desc(), Exam.id)
    )
    return [
        ExamSummary(
            id=exam.id,
            title=exam.title,
            question_count=len(exam.questions),
            total_points=sum(q["points"] for q in exam.questions),
            created_at=exam.created_at,
        )
        for exam in exams
    ]


@router.get("/exams/{exam_id}", response_model=ExamOut)
async def get_exam(exam: OwnedExam):
    """One exam, ready to take: questions without the answer key."""
    return ExamOut(
        id=exam.id,
        session_id=exam.session_id,
        title=exam.title,
        request=exam.request,
        created_at=exam.created_at,
        questions=[PublicQuestion.model_validate(q) for q in exam.questions],
    )
