"""Topic extraction: one Gemini call per document -> topics with page ranges -> merged into the course.

Topics are matched across documents by normalized name ("Binary Heaps" == "binary heaps."). The
course's existing topic names are also given to the model, so it reuses "Heaps" instead of
inventing "Binary Heaps" for the same thing.
"""

import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from google.genai import types
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import DocumentTopic, Topic
from app.ingest.parsers import ParsedPage
from app.services.gemini import make_client, with_retries

TOPICS_PROMPT = (Path(__file__).parent / "prompts" / "topics.md").read_text(encoding="utf-8")

MAX_TOPICS = 20  # distinct topics kept per document
MAX_NAME_CHARS = 100
MAX_PAGE_CHARS = 3000  # text sent per page; keeps the prompt bounded for very dense pages


class TopicRange(BaseModel):
    """One topic and the pages that cover it. Also the JSON shape Gemini must answer with."""

    name: str = Field(description="Short topic name in Title Case")
    page_start: int = Field(description="First page covering the topic (1-based)")
    page_end: int = Field(description="Last page covering the topic (inclusive)")


class TopicList(BaseModel):
    topics: list[TopicRange]


class TopicExtractionError(Exception):
    """The model's answer couldn't be used."""


class TopicExtractor(Protocol):
    """Anything that can find a document's topics. Tests pass a fake one, so they never call Gemini."""

    async def extract(self, pages: list[ParsedPage], existing: list[str], title: str) -> list[TopicRange]: ...


# --- plain helpers (unit-tested) ----------------------------------------------------------


def normalize_topic_name(name: str) -> str:
    """The merge key: lowercase, punctuation and extra spaces removed. Keeps + and # (C++, C#)."""
    return re.sub(r"[^\w+#]+", " ", name.lower()).strip()


def clean_topics(raw: list[TopicRange], page_count: int) -> list[TopicRange]:
    """Make the model's answer safe to store: clamp pages to the document, drop junk names,
    keep at most MAX_TOPICS distinct topics, and remove exact duplicates."""
    kept: list[TopicRange] = []
    names: set[str] = set()
    seen: set[tuple[str, int]] = set()
    for item in raw:
        name = " ".join(item.name.split())
        key = normalize_topic_name(name)
        if not key or len(name) > MAX_NAME_CHARS:
            continue
        if key not in names:
            if len(names) >= MAX_TOPICS:
                continue
            names.add(key)
        start, end = sorted((item.page_start, item.page_end))
        start, end = max(1, min(start, page_count)), max(1, min(end, page_count))
        if (key, start) in seen:
            continue
        seen.add((key, start))
        kept.append(TopicRange(name=name, page_start=start, page_end=end))
    return kept


def topics_by_page(ranges: list[TopicRange]) -> dict[int, list[str]]:
    """page number -> normalized names of the topics covering it (used to tag chunks)."""
    pages: dict[int, list[str]] = defaultdict(list)
    for item in ranges:
        key = normalize_topic_name(item.name)
        for page in range(item.page_start, item.page_end + 1):
            if key not in pages[page]:
                pages[page].append(key)
    return dict(pages)


def build_prompt(pages: list[ParsedPage], existing: list[str], title: str) -> str:
    page_text = "\n\n".join(
        f"=== Page {p.number} ===\n{p.text[:MAX_PAGE_CHARS] if p.text else '(no text)'}" for p in pages
    )
    existing_text = "\n".join(f"- {name}" for name in existing) or "(none yet)"
    # str.replace rather than str.format: page text may contain { and } (code, math).
    return (
        TOPICS_PROMPT.replace("{title}", title)
        .replace("{existing_topics}", existing_text)
        .replace("{pages}", page_text)
    )


# --- Gemini --------------------------------------------------------------------------------


class GeminiTopics:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # created on first use
        self._model = settings.topic_model
        # Structured output: Gemini must answer with JSON matching TopicList.
        self._config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=TopicList)

    async def extract(self, pages: list[ParsedPage], existing: list[str], title: str) -> list[TopicRange]:
        if self._client is None:
            self._client = make_client(self._settings)
        prompt = build_prompt(pages, existing, title)
        response = await with_retries(
            lambda: self._client.aio.models.generate_content(model=self._model, contents=prompt, config=self._config),
            what="Topic extraction",
        )
        if isinstance(response.parsed, TopicList):
            return response.parsed.topics
        try:
            return TopicList.model_validate_json(response.text or "").topics
        except ValidationError as exc:
            raise TopicExtractionError(f"Unusable topic list: {response.text!r:.200}") from exc


# --- database ------------------------------------------------------------------------------


async def course_topic_names(db: AsyncSession, course_id: uuid.UUID) -> list[str]:
    return list(await db.scalars(select(Topic.name).where(Topic.course_id == course_id).order_by(Topic.name)))


async def load_document_topics(db: AsyncSession, document_id: uuid.UUID) -> list[TopicRange]:
    """Topics saved for this document by an earlier run (reused on Retry instead of a new call)."""
    rows = await db.execute(
        select(Topic.name, DocumentTopic.page_start, DocumentTopic.page_end)
        .join(Topic, DocumentTopic.topic_id == Topic.id)
        .where(DocumentTopic.document_id == document_id)
    )
    return [TopicRange(name=name, page_start=start, page_end=end) for name, start, end in rows.all()]


async def save_document_topics(
    db: AsyncSession, course_id: uuid.UUID, document_id: uuid.UUID, ranges: list[TopicRange]
) -> None:
    """Merge the topics into the course and record this document's page ranges (replacing old ones).

    New topics are inserted with ON CONFLICT DO NOTHING on (course_id, normalized_name), so two
    documents finishing at the same moment can't create the same topic twice. Existing topics
    keep their original name.
    """
    display = {}
    for item in ranges:
        display.setdefault(normalize_topic_name(item.name), item.name)

    if display:
        await db.execute(
            insert(Topic)
            .values(
                [
                    {"id": uuid.uuid4(), "course_id": course_id, "name": name, "normalized_name": key}
                    for key, name in display.items()
                ]
            )
            .on_conflict_do_nothing(index_elements=["course_id", "normalized_name"])
        )
    ids = dict(
        (
            await db.execute(
                select(Topic.normalized_name, Topic.id).where(
                    Topic.course_id == course_id, Topic.normalized_name.in_(list(display))
                )
            )
        ).all()
    )

    await db.execute(delete(DocumentTopic).where(DocumentTopic.document_id == document_id))
    db.add_all(
        DocumentTopic(
            document_id=document_id,
            topic_id=ids[normalize_topic_name(item.name)],
            page_start=item.page_start,
            page_end=item.page_end,
        )
        for item in ranges
    )
