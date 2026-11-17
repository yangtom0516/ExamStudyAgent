"""Mock exams: the format ExamWriter must follow, the checks that guard it, and saving.

An exam is checked in two layers, and every problem found is reported as a plain sentence
("q3: page 57 doesn't exist in Lecture 5.pdf (41 pages)"), all at once, so the agent can fix
them and try again:

1. Shape (Pydantic, no database): question types, points, choices, rubric, at least one source.
2. Against the database: the session is started, every source is a real page of a ready
   lecture/notes file in this session, and every topic is one of the course's topics.

Question ids (q1, q2, ...) are given by the server on save, so the agent can't get them wrong.
"""

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Course, Document, DocumentKind, DocumentStatus, Exam, SessionStatus, StudySession, Topic
from app.ingest.topics import normalize_topic_name

MAX_QUESTIONS = 30
MAX_POINTS = 100
MAX_CHOICES = 6

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
QuestionKind = Literal["multiple_choice", "short_answer", "long_answer"]


# --- format -----------------------------------------------------------------------------


class Source(BaseModel):
    """A page the question is based on."""

    document_id: uuid.UUID
    page: int = Field(ge=1)


class QuestionDraft(BaseModel):
    """One question as the agent writes it (no id yet)."""

    type: QuestionKind
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    points: int = Field(ge=1, le=MAX_POINTS)
    prompt: LongText
    choices: list[Line] = Field(default_factory=list, max_length=MAX_CHOICES)  # multiple choice only
    answer: LongText  # the correct choice, or a model answer for written questions
    rubric: list[Line] = Field(default_factory=list, max_length=10)  # grading points, written only
    sources: list[Source] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _rules_per_type(self) -> "QuestionDraft":
        if self.type == "multiple_choice":
            if len(self.choices) < 2:
                raise ValueError(f"multiple choice needs 2 to {MAX_CHOICES} choices")
            if len({c.lower() for c in self.choices}) < len(self.choices):
                raise ValueError("choices must all be different")
            match = next((c for c in self.choices if c.lower() == self.answer.lower()), None)
            if match is None:
                raise ValueError("answer must be exactly one of the choices (copy the choice's text)")
            self.answer = match  # same spelling as the choice, so grading can compare exactly
            self.rubric = []  # a choice is right or wrong; nothing to grade by rubric
        else:
            if self.choices:
                raise ValueError(f"{self.type} questions have no choices")
            if not self.rubric:
                raise ValueError("written questions need at least one rubric line")
        return self


class ExamDraft(BaseModel):
    """A whole exam as the agent writes it."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    questions: list[QuestionDraft] = Field(min_length=1, max_length=MAX_QUESTIONS)


class Question(QuestionDraft):
    """A question as stored in `exams.questions`: the draft plus its id (read back when grading)."""

    id: str


class PublicQuestion(BaseModel):
    """What the student sees while taking the exam: no answer, rubric or sources (a page
    reference is a hint). Built from a stored question; the other keys are dropped."""

    id: str
    type: QuestionKind
    topic: str
    points: int
    prompt: str
    choices: list[str]


def describe_errors(exc: ValidationError) -> list[str]:
    """Pydantic errors as short sentences that name the question: "q2: answer must be ..."."""
    messages = []
    for error in exc.errors():
        location = list(error["loc"])
        where = []
        if len(location) > 1 and location[0] == "questions" and isinstance(location[1], int):
            where.append(f"q{location[1] + 1}")
            location = location[2:]
        where += [str(part) for part in location]
        message = error["msg"].removeprefix("Value error, ")
        messages.append(f"{'.'.join(where)}: {message}" if where else message)
    return messages


# --- saving -----------------------------------------------------------------------------


@dataclass
class SaveResult:
    exam_id: uuid.UUID | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exam_id is not None


async def save_exam(
    db: AsyncSession, *, user_id: uuid.UUID, session_id: uuid.UUID, exam: dict, request: str
) -> SaveResult:
    """Check `exam` (raw JSON from the agent) and save it. Nothing is saved if anything is wrong.

    `user_id` and `session_id` come from the server, never from the agent; the session must
    belong to the user.
    """
    study_session = await db.scalar(
        select(StudySession)
        .join(Course, StudySession.course_id == Course.id)
        .where(StudySession.id == session_id, Course.user_id == user_id)
    )
    if study_session is None:
        return SaveResult(errors=["Session not found"])
    if study_session.status != SessionStatus.ACTIVE:
        return SaveResult(errors=["Exams can only be saved in a started session"])

    try:
        draft = ExamDraft.model_validate(exam)
    except ValidationError as exc:
        return SaveResult(errors=describe_errors(exc))

    questions, errors = await _check_against_session(db, study_session, draft)
    if errors:
        return SaveResult(errors=errors)

    row = Exam(id=uuid.uuid4(), session_id=session_id, title=draft.title, request=request, questions=questions)
    db.add(row)
    await db.commit()
    return SaveResult(exam_id=row.id)


async def _check_against_session(
    db: AsyncSession, study_session: StudySession, draft: ExamDraft
) -> tuple[list[dict], list[str]]:
    """Sources must be real pages of this session's ready lectures/notes; topics must be course
    topics (saved under the course's spelling, so scores in step 5 add up per topic).
    Returns the questions ready to store, and the problems found."""
    files = {
        document_id: (filename, page_count)
        for document_id, filename, page_count in await db.execute(
            select(Document.id, Document.filename, Document.page_count).where(
                Document.session_id == study_session.id,
                Document.status == DocumentStatus.READY,
                Document.kind.in_([DocumentKind.LECTURE, DocumentKind.NOTES]),
            )
        )
    }
    topics = {
        normalized: name
        for normalized, name in await db.execute(
            select(Topic.normalized_name, Topic.name).where(Topic.course_id == study_session.course_id)
        )
    }

    questions: list[dict] = []
    errors: list[str] = []
    unknown_topic = False
    for number, draft_question in enumerate(draft.questions, start=1):
        label = f"q{number}"
        topic = topics.get(normalize_topic_name(draft_question.topic))
        if topic is None:
            unknown_topic = True
            errors.append(f"{label}: unknown topic {draft_question.topic!r}")
        for source in draft_question.sources:
            if source.document_id not in files:
                errors.append(f"{label}: source {source.document_id} is not a lecture or notes file in this session")
                continue
            filename, page_count = files[source.document_id]
            if page_count is not None and source.page > page_count:
                errors.append(f"{label}: page {source.page} doesn't exist in {filename} ({page_count} pages)")
        questions.append({"id": label, **draft_question.model_dump(mode="json"), "topic": topic or draft_question.topic})

    if unknown_topic:
        allowed = ", ".join(sorted(topics.values())) if topics else "(this course has no topics yet)"
        errors.append(f"Use one of the course topics: {allowed}")
    return questions, errors
