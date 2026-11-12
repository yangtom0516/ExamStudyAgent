"""Celery tasks. Celery calls plain functions, so each task runs its async code with asyncio.run."""

import asyncio
import uuid

from app.config import get_settings
from app.ingest import pipeline
from app.worker.celery_app import INGEST_TASK, celery_app


@celery_app.task(name=INGEST_TASK)
def ingest_document(document_id: str) -> None:
    asyncio.run(_ingest(uuid.UUID(document_id)))


async def _ingest(document_id: uuid.UUID) -> None:
    async with pipeline.open_deps(get_settings()) as deps:
        await pipeline.ingest_document(document_id, deps)
