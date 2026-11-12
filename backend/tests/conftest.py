"""Test setup shared by all tests.

API tests run against the real Postgres/Redis/Qdrant/SeaweedFS containers, but never touch
dev data: they use the database `study_test`, Redis DB 15, the bucket `<S3_BUCKET>-test` and
the Qdrant collection `<collection>__test`. They never call Gemini or the real job queue.
Unit tests (tests/unit) need no containers.
"""

import asyncio
import functools
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "study_test"


def _switch_to_test_resources() -> str:
    """Point settings at test resources via env vars (they override .env). Returns the dev DB URL."""
    dev = get_settings()
    dev_url = make_url(dev.database_url)
    assert dev_url.database != TEST_DB_NAME, "dev and test databases must differ"

    os.environ["DATABASE_URL"] = dev_url.set(database=TEST_DB_NAME).render_as_string(hide_password=False)
    os.environ["REDIS_URL"] = dev.redis_url.rsplit("/", 1)[0] + "/15"
    os.environ["S3_BUCKET"] = f"{dev.s3_bucket}-test"
    os.environ["QDRANT_COLLECTION"] = f"{dev.collection_name}__test"
    get_settings.cache_clear()
    return dev.database_url


# Must run before the app reads settings, so it happens at import time.
DEV_DATABASE_URL = _switch_to_test_resources()

from fastapi.testclient import TestClient  # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.ingest import pipeline  # noqa: E402
from app.main import app  # noqa: E402
from app.services.storage import Storage  # noqa: E402
from tests.helpers import FakeEmbedder, FakeOcr, FakeQuestionCounter, FakeTopics  # noqa: E402


async def _create_test_database() -> None:
    # CREATE DATABASE can't run inside a transaction, hence AUTOCOMMIT; connect via the dev DB.
    engine = create_async_engine(DEV_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_NAME}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await engine.dispose()


async def _truncate_all_tables() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@functools.cache
def _test_storage() -> Storage:
    """One storage client for the whole test run (creating one per test is slow)."""
    return Storage(get_settings())


@functools.cache
def _test_qdrant() -> QdrantClient:
    return QdrantClient(url=get_settings().qdrant_url)


def _empty_test_stores() -> None:
    """Remove files and chunks left by earlier tests (run after app startup created both stores).

    Deletes the points, not the collection: rebuilding a collection is slow disk work.
    """
    settings = get_settings()
    asyncio.run(_test_storage().delete_prefix(""))
    qdrant = _test_qdrant()
    points, _ = qdrant.scroll(settings.collection_name, limit=10_000, with_payload=False, with_vectors=False)
    if points:
        qdrant.delete(settings.collection_name, points_selector=models.PointIdsList(points=[p.id for p in points]))


async def _execute(sql: str, params: dict) -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    await engine.dispose()


async def _fetch_all(sql: str, params: dict) -> list[dict]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    await engine.dispose()
    return [dict(row) for row in rows]


async def _fetch_one(sql: str, params: dict) -> dict | None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        row = (await conn.execute(text(sql), params)).mappings().first()
    await engine.dispose()
    return dict(row) if row else None


@pytest.fixture(scope="session")
def test_database() -> None:
    """Create study_test if needed and bring it to the latest migration (also tests the migrations)."""
    from alembic import command
    from alembic.config import Config

    asyncio.run(_create_test_database())
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")


@pytest.fixture
def client(test_database):
    """A fresh API client on empty tables. It keeps cookies between requests, like a browser."""
    asyncio.run(_truncate_all_tables())
    with TestClient(app) as test_client:  # `with` runs the app's startup/shutdown code
        _empty_test_stores()
        yield test_client


@pytest.fixture(autouse=True)
def enqueued(monkeypatch) -> list[str]:
    """Uploads never reach the real job queue in tests: the IDs of queued documents land here."""
    jobs: list[str] = []
    monkeypatch.setattr("app.api.documents.enqueue_ingest", lambda document_id: jobs.append(str(document_id)))
    return jobs


@pytest.fixture
def published() -> list[dict]:
    """Progress events the pipeline broadcast during this test (via run_ingest), in order."""
    return []


@pytest.fixture
def run_ingest(client, published):
    """Run the ingestion pipeline for a document right here (what the worker would do),
    with fakes instead of Gemini: run_ingest(document_id, embedder=..., ocr=..., topics=..., questions=...).
    Progress events are recorded in `published` instead of going to Redis."""

    async def record(user_id, event) -> None:
        published.append(event)

    def _run(document_id: str, embedder=None, ocr=None, topics=None, questions=None) -> None:
        async def _go() -> None:
            settings = get_settings()
            async with pipeline.open_deps(
                settings,
                embedder or FakeEmbedder(settings.embed_dimensions),
                publish=record,
                ocr=ocr or FakeOcr(),
                topics=topics or FakeTopics(),
                questions=questions or FakeQuestionCounter(),
            ) as deps:
                await pipeline.ingest_document(uuid.UUID(document_id), deps)

        asyncio.run(_go())

    return _run


@pytest.fixture
def chunk_payloads(client):
    """Payloads of a document's Qdrant points, in page/chunk order: chunk_payloads(document_id)."""
    settings = get_settings()
    qdrant = _test_qdrant()

    def _payloads(document_id: str) -> list[dict]:
        points, _ = qdrant.scroll(
            settings.collection_name,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
            ),
            limit=1000,
        )
        return sorted((p.payload for p in points), key=lambda p: (p["page_number"], p["chunk_index"]))

    return _payloads


@pytest.fixture
def login_as(client):
    """Switch the client to a user, registering them on first use: login_as("alice")."""

    def _login_as(username: str) -> None:
        client.cookies.clear()
        credentials = {"username": username, "password": "password123"}
        response = client.post("/auth/login", json=credentials)
        if response.status_code == 401:
            response = client.post("/auth/register", json=credentials)
        assert response.status_code in (200, 201), response.text

    return _login_as


@pytest.fixture
def db_fetch_one(test_database):
    """Run a SELECT against the test database and return the first row as a dict."""
    return lambda sql, **params: asyncio.run(_fetch_one(sql, params))


@pytest.fixture
def db_fetch_all(test_database):
    """Run a SELECT against the test database and return all rows as dicts."""
    return lambda sql, **params: asyncio.run(_fetch_all(sql, params))


@pytest.fixture
def db_execute(test_database):
    """Run a write statement against the test database, e.g. to set up a state the API can't reach yet."""
    return lambda sql, **params: asyncio.run(_execute(sql, params))


@pytest.fixture
def stored_file_exists(client):
    """Check whether a key exists in the test bucket (depends on `client`, whose startup creates the bucket)."""
    return lambda key: asyncio.run(_test_storage().exists(key))
