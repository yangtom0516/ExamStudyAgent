"""FastAPI application.

Run locally from the backend/ folder:
    uv run uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import health
from app.config import get_settings
from app.services.storage import Storage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open connections once at startup, share them via app.state, close them at shutdown."""
    settings = get_settings()

    app.state.db_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.db_sessionmaker = async_sessionmaker(app.state.db_engine, expire_on_commit=False)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    app.state.storage = Storage(settings)

    try:
        await app.state.storage.ensure_bucket()
    except Exception:
        # Don't crash on startup; /health will report storage as failing.
        logger.exception("Could not create storage bucket %r", settings.s3_bucket)

    yield

    await app.state.redis.aclose()
    await app.state.qdrant.close()
    await app.state.db_engine.dispose()


app = FastAPI(title="Exam Study Agent API", lifespan=lifespan)
app.include_router(health.router)
