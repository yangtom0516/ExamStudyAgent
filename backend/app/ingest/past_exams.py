"""Past exams: extract their structure only (question types and points), not their content.

One Gemini call per past exam lists its questions. ExamWriter later uses the counts as a
reference for a realistic exam shape (see services/exam_profile.py). Past exams are not chunked
or indexed, so their questions never show up in search results.
"""

import uuid
from pathlib import Path
from typing import Protocol

from google.genai import types
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import PastExamQuestion, QuestionType
from app.ingest.parsers import ParsedPage
from app.services.gemini import make_client, with_retries

PAST_EXAM_PROMPT = (Path(__file__).parent / "prompts" / "past_exam.md").read_text(encoding="utf-8")

MAX_QUESTIONS = 300  # per exam; anything beyond is surely a mistake
MAX_POINTS = 1000
MAX_NUMBER_CHARS = 20
MAX_PAGE_CHARS = 6000  # exam pages are dense; keeps the prompt bounded


class QuestionItem(BaseModel):
    """One question's structure. Also the JSON shape Gemini must answer with."""

    number: str = Field(description='Question label as printed, e.g. "3", "Q2", "4b"')
    type: str = Field(description="multiple_choice, short_answer, long_answer or other")
    points: int | None = Field(default=None, description="Points printed for the question, if any")


class QuestionList(BaseModel):
    questions: list[QuestionItem]


class QuestionCountError(Exception):
    """The model's answer couldn't be used."""


class QuestionCounter(Protocol):
    """Anything that can list an exam's questions. Tests pass a fake one, so they never call Gemini."""

    async def count(self, pages: list[ParsedPage], title: str) -> list[QuestionItem]: ...


# --- plain helpers (unit-tested) ----------------------------------------------------------

_TYPES = {t.value for t in QuestionType}


def clean_questions(raw: list[QuestionItem]) -> list[QuestionItem]:
    """Make the model's answer safe to store: unknown types become "other", implausible points
    are dropped, labels are trimmed, and the list is capped."""
    cleaned = []
    for item in raw[:MAX_QUESTIONS]:
        kind = item.type.strip().lower().replace("-", "_").replace(" ", "_")
        points = item.points if item.points is not None and 0 < item.points <= MAX_POINTS else None
        number = " ".join(item.number.split())[:MAX_NUMBER_CHARS] or str(len(cleaned) + 1)
        cleaned.append(
            QuestionItem(number=number, type=kind if kind in _TYPES else QuestionType.OTHER, points=points)
        )
    return cleaned


def build_prompt(pages: list[ParsedPage], title: str) -> str:
    page_text = "\n\n".join(
        f"=== Page {p.number} ===\n{p.text[:MAX_PAGE_CHARS] if p.text else '(no text)'}" for p in pages
    )
    # str.replace rather than str.format: exam text may contain { and } (code, math).
    return PAST_EXAM_PROMPT.replace("{title}", title).replace("{pages}", page_text)


# --- Gemini --------------------------------------------------------------------------------


class GeminiQuestionCounter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # created on first use
        self._model = settings.topic_model  # the same cheap ingestion model as topic extraction
        self._config = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=QuestionList
        )

    async def count(self, pages: list[ParsedPage], title: str) -> list[QuestionItem]:
        if self._client is None:
            self._client = make_client(self._settings)
        prompt = build_prompt(pages, title)
        response = await with_retries(
            lambda: self._client.aio.models.generate_content(model=self._model, contents=prompt, config=self._config),
            what="Counting past-exam questions",
        )
        if isinstance(response.parsed, QuestionList):
            return response.parsed.questions
        try:
            return QuestionList.model_validate_json(response.text or "").questions
        except ValidationError as exc:
            raise QuestionCountError(f"Unusable question list: {response.text!r:.200}") from exc


# --- database ------------------------------------------------------------------------------


async def load_questions(db: AsyncSession, document_id: uuid.UUID) -> list[QuestionItem]:
    """Questions saved by an earlier run of this document (reused on Retry instead of a new call)."""
    rows = await db.scalars(
        select(PastExamQuestion)
        .where(PastExamQuestion.document_id == document_id)
        .order_by(PastExamQuestion.position)
    )
    return [QuestionItem(number=q.number, type=q.type, points=q.points) for q in rows]


async def save_questions(db: AsyncSession, document_id: uuid.UUID, questions: list[QuestionItem]) -> None:
    await db.execute(delete(PastExamQuestion).where(PastExamQuestion.document_id == document_id))
    db.add_all(
        PastExamQuestion(document_id=document_id, position=i, number=q.number, type=q.type, points=q.points)
        for i, q in enumerate(questions, start=1)
    )
