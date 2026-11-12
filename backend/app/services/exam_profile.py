"""The exam profile of a study session: what ExamWriter needs to write a realistic exam.

- topics: what this session's ready lectures and notes cover, and how much material each has.
- past_exams: the structure of this session's ready past exams (question counts per type,
  average per exam, points when printed). All zeros when no past exam was uploaded.
"""

import uuid

from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentKind, DocumentStatus, DocumentTopic, PastExamQuestion, QuestionType, Topic

QUESTION_TYPES = [t.value for t in QuestionType]


class TopicCoverage(BaseModel):
    name: str
    documents: int
    pages: int


class PastExamStructure(BaseModel):
    files: int
    questions: dict[str, int]  # type -> total count over all past exams
    average_per_exam: dict[str, float]
    points: dict[str, int]  # type -> total printed points
    average_points_per_exam: float | None  # None when no points were printed at all


class ExamProfile(BaseModel):
    topics: list[TopicCoverage]
    past_exams: PastExamStructure


def summarize_past_exams(files: int, rows: list[tuple[str, int, int | None, int]]) -> PastExamStructure:
    """rows: (type, question count, sum of printed points or None, questions with points)."""
    counts = dict.fromkeys(QUESTION_TYPES, 0)
    points = dict.fromkeys(QUESTION_TYPES, 0)
    any_points = False
    for kind, count, point_sum, with_points in rows:
        counts[kind] += count
        points[kind] += point_sum or 0
        any_points = any_points or with_points > 0
    return PastExamStructure(
        files=files,
        questions=counts,
        average_per_exam={k: round(v / files, 1) if files else 0.0 for k, v in counts.items()},
        points=points,
        average_points_per_exam=round(sum(points.values()) / files, 1) if files and any_points else None,
    )


async def build_exam_profile(db: AsyncSession, session_id: uuid.UUID) -> ExamProfile:
    ready_in_session = (Document.session_id == session_id, Document.status == DocumentStatus.READY)

    topic_rows = await db.execute(
        select(
            Topic.name,
            func.count(distinct(DocumentTopic.document_id)),
            func.sum(DocumentTopic.page_end - DocumentTopic.page_start + 1),
        )
        .join(DocumentTopic, DocumentTopic.topic_id == Topic.id)
        .join(Document, DocumentTopic.document_id == Document.id)
        .where(*ready_in_session, Document.kind.in_([DocumentKind.LECTURE, DocumentKind.NOTES]))
        .group_by(Topic.id, Topic.name)
        .order_by(Topic.name)
    )
    topics = [TopicCoverage(name=name, documents=docs, pages=pages) for name, docs, pages in topic_rows.all()]

    exam_files = await db.scalar(
        select(func.count()).select_from(Document).where(*ready_in_session, Document.kind == DocumentKind.PAST_EXAM)
    )
    question_rows = await db.execute(
        select(
            PastExamQuestion.type,
            func.count(),
            func.sum(PastExamQuestion.points),
            func.count(PastExamQuestion.points),
        )
        .join(Document, PastExamQuestion.document_id == Document.id)
        .where(*ready_in_session, Document.kind == DocumentKind.PAST_EXAM)
        .group_by(PastExamQuestion.type)
    )
    return ExamProfile(topics=topics, past_exams=summarize_past_exams(exam_files or 0, list(question_rows.all())))
