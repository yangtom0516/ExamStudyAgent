"""Ingest one document: download -> parse -> save pages -> chunk -> embed -> index in Qdrant -> ready.

Safe to run more than once for the same document (Celery may deliver a job twice, e.g. after
a worker crash): pages are replaced, not added, and Qdrant point IDs are deterministic.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db.models import Course, Document, DocumentStatus, Page, PageSource, StudySession
from app.ingest.chunking import chunk_pages
from app.ingest.embedding import Embedder, GeminiEmbedder
from app.ingest.parsers import UnreadableFileError, parse_document
from app.services.storage import Storage
from app.services.vectors import DENSE, VectorStore, chunk_point_id

logger = logging.getLogger(__name__)

GENERIC_ERROR = "Something went wrong while reading this file. Press Retry to try again."
NO_TEXT_ERROR = (
    "No text found in this file. It may be scanned images only; reading scans is coming soon."
)


@dataclass
class IngestDeps:
    """Connections the pipeline needs. Tests build it with a fake embedder."""

    sessionmaker: async_sessionmaker
    storage: Storage
    vectors: VectorStore
    embedder: Embedder


@asynccontextmanager
async def open_deps(settings: Settings, embedder: Embedder | None = None) -> AsyncIterator[IngestDeps]:
    # NullPool: each Celery job runs in its own event loop, so don't keep connections between jobs.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        vectors = VectorStore(qdrant, settings.collection_name, settings.embed_dimensions)
        await vectors.ensure_collection()
        yield IngestDeps(
            sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
            storage=Storage(settings),
            vectors=vectors,
            embedder=embedder or GeminiEmbedder(settings),
        )
    finally:
        await qdrant.close()
        await engine.dispose()


@dataclass(frozen=True)
class _Job:
    """What the pipeline needs to know about the document, copied out of the database row."""

    document_id: uuid.UUID
    session_id: uuid.UUID
    course_id: uuid.UUID
    user_id: uuid.UUID
    filename: str
    storage_key: str
    kind: str

    @property
    def extension(self) -> str:
        return PurePosixPath(self.storage_key).suffix


async def ingest_document(document_id: uuid.UUID, deps: IngestDeps) -> None:
    job = await _start(document_id, deps)
    if job is None:
        return
    try:
        await _process(job, deps)
    except UnreadableFileError as exc:
        logger.info("Document %s can't be read: %s", document_id, exc)
        await _finish(job, deps, DocumentStatus.FAILED, str(exc))
    except Exception:
        if not await _finish(job, deps, DocumentStatus.FAILED, GENERIC_ERROR):
            logger.info("Document %s was deleted during processing", document_id)
            return
        raise  # unexpected: let Celery log the full error


async def _start(document_id: uuid.UUID, deps: IngestDeps) -> _Job | None:
    """Mark the document `processing`. Returns None if there's nothing to do."""
    async with deps.sessionmaker() as db:
        row = (
            await db.execute(
                select(Document, StudySession.course_id, Course.user_id)
                .join(StudySession, Document.session_id == StudySession.id)
                .join(Course, StudySession.course_id == Course.id)
                .where(Document.id == document_id)
            )
        ).first()
        if row is None:
            logger.info("Document %s no longer exists; skipping", document_id)
            return None
        document, course_id, user_id = row
        if document.status == DocumentStatus.READY:
            logger.info("Document %s is already ready; skipping", document_id)
            return None
        document.status = DocumentStatus.PROCESSING
        document.error = None
        await db.commit()
        return _Job(
            document_id=document.id,
            session_id=document.session_id,
            course_id=course_id,
            user_id=user_id,
            filename=document.filename,
            storage_key=document.storage_key,
            kind=document.kind,
        )


async def _process(job: _Job, deps: IngestDeps) -> None:
    # 1. Parse the file into pages.
    data = await deps.storage.download(job.storage_key)
    pages = parse_document(data, job.extension)

    # 2. Save the page texts (replacing any from an earlier run).
    async with deps.sessionmaker() as db:
        await db.execute(delete(Page).where(Page.document_id == job.document_id))
        db.add_all(
            Page(document_id=job.document_id, page_number=p.number, text=p.text, source=PageSource.TEXT)
            for p in pages
        )
        await db.execute(update(Document).where(Document.id == job.document_id).values(page_count=len(pages)))
        await db.commit()

    # 3. Chunk and embed.
    chunks = chunk_pages(pages, slides=job.extension == ".pptx")
    if not chunks:
        raise UnreadableFileError(NO_TEXT_ERROR)
    title = PurePosixPath(job.filename).stem
    vectors = await deps.embedder.embed_documents([c.text for c in chunks], title=title)

    # 4. Index in Qdrant. Old points go first, in case an earlier run produced more chunks.
    payload = {
        "user_id": str(job.user_id),
        "course_id": str(job.course_id),
        "session_id": str(job.session_id),
        "document_id": str(job.document_id),
        "kind": job.kind,
        "topics": [],  # filled by topic extraction in step 3
    }
    points = [
        models.PointStruct(
            id=str(chunk_point_id(job.document_id, chunk.page_number, chunk.chunk_index)),
            vector={DENSE: vector},
            payload={
                **payload,
                "page_number": chunk.page_number,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
            },
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    await deps.vectors.delete_document(job.document_id)
    await deps.vectors.upsert(points)

    # 5. Done.
    await _finish(job, deps, DocumentStatus.READY, None)
    logger.info("Document %s ready: %d pages, %d chunks", job.document_id, len(pages), len(chunks))


async def _finish(job: _Job, deps: IngestDeps, status: DocumentStatus, error: str | None) -> bool:
    """Set the final status. Returns False if the document was deleted in the meantime."""
    async with deps.sessionmaker() as db:
        result = await db.execute(
            update(Document).where(Document.id == job.document_id).values(status=status, error=error)
        )
        await db.commit()
    if result.rowcount == 0:
        # Deleted while we were working: remove any points we just wrote for it.
        await deps.vectors.delete_document(job.document_id)
        return False
    return True
