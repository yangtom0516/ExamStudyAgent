"""Rebuild the Qdrant collection from Postgres: `uv run python -m app.ingest.reindex`.

Qdrant isn't the source of truth. Page text lives in `pages` and topics in `document_topics`, so
the whole index can be rebuilt without re-uploading files, redoing OCR or asking for topics again.
Only the chunks are embedded again (one Gemini embedding call per 50 chunks).

Use it after changing how chunks are indexed (e.g. the keyword vectors added in step 3d) or the
embedding model. Stop the worker first: files ingested during the rebuild could be lost from the index.
"""

import asyncio
import logging
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.db.models import Course, Document, DocumentKind, DocumentStatus, Page, StudySession
from app.ingest.chunking import chunk_pages
from app.ingest.embedding import Embedder, GeminiEmbedder
from app.ingest.indexing import DocumentInfo, index_chunks
from app.ingest.parsers import ParsedPage
from app.ingest.topics import load_document_topics, topics_by_page
from app.services.vectors import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class ReindexResult:
    documents: int = 0
    chunks: int = 0


async def reindex_all(settings: Settings, embedder: Embedder | None = None) -> ReindexResult:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    embedder = embedder or GeminiEmbedder(settings)
    result = ReindexResult()
    try:
        vectors = VectorStore(qdrant, settings.collection_name, settings.embed_dimensions)
        await vectors.recreate_collection()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        async with sessionmaker() as db:
            rows = (
                await db.execute(
                    select(Document, StudySession.course_id, Course.user_id)
                    .join(StudySession, Document.session_id == StudySession.id)
                    .join(Course, StudySession.course_id == Course.id)
                    # Past exams are never indexed; unfinished files will be indexed when they finish.
                    .where(
                        Document.status == DocumentStatus.READY,
                        Document.kind.in_([DocumentKind.LECTURE, DocumentKind.NOTES]),
                    )
                    .order_by(Document.created_at)
                )
            ).all()

        for document, course_id, user_id in rows:
            doc = DocumentInfo(
                document_id=document.id,
                session_id=document.session_id,
                course_id=course_id,
                user_id=user_id,
                filename=document.filename,
                storage_key=document.storage_key,
                kind=document.kind,
            )
            async with sessionmaker() as db:
                page_rows = await db.scalars(
                    select(Page).where(Page.document_id == doc.document_id).order_by(Page.page_number)
                )
                pages = [ParsedPage(p.page_number, p.text, source=p.source) for p in page_rows]
                page_topics = topics_by_page(await load_document_topics(db, doc.document_id))

            chunks = chunk_pages(pages, slides=doc.extension == ".pptx")
            if chunks:
                await index_chunks(doc, chunks, page_topics, embedder, vectors)
            result.documents += 1
            result.chunks += len(chunks)
            logger.info("Reindexed %s (%s): %d chunks", doc.filename, doc.document_id, len(chunks))
        return result
    finally:
        await qdrant.close()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    print(f"Rebuilding Qdrant collection {settings.collection_name} from Postgres...")
    result = asyncio.run(reindex_all(settings))
    print(f"Done: {result.documents} documents, {result.chunks} chunks indexed.")


if __name__ == "__main__":
    main()
