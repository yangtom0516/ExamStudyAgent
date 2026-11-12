"""Test setup shared by all tests.

API tests run against the real Postgres/Redis/Qdrant/SeaweedFS containers, but never touch
dev data: they use the database `study_test`, Redis DB 15 and the bucket `<S3_BUCKET>-test`.
Unit tests (tests/unit) need no containers.
"""

import asyncio
import os
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
    get_settings.cache_clear()
    return dev.database_url


# Must run before the app reads settings, so it happens at import time.
DEV_DATABASE_URL = _switch_to_test_resources()

from fastapi.testclient import TestClient  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.main import app  # noqa: E402


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


async def _execute(sql: str, params: dict) -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)
    await engine.dispose()


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
        yield test_client


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
def db_execute(test_database):
    """Run a write statement against the test database, e.g. to set up a state the API can't reach yet."""
    return lambda sql, **params: asyncio.run(_execute(sql, params))


@pytest.fixture
def stored_file_exists(client):
    """Check whether a key exists in the test bucket (depends on `client`, whose startup creates the bucket)."""
    from app.services.storage import Storage

    storage = Storage(get_settings())
    return lambda key: asyncio.run(storage.exists(key))
