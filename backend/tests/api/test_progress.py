"""Live progress: the events the worker broadcasts, and the SSE stream that forwards them.

The stream never ends on its own, so it's tested by reading from the generator directly
(with real Redis and Postgres) rather than through the test HTTP client.
"""

import asyncio
import json
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.documents import event_stream
from app.config import get_settings
from app.ingest.pipeline import NO_TEXT_ERROR
from app.services import progress
from tests.helpers import create_course, create_session, make_pdf, upload_ok


@pytest.fixture
def session(client, login_as):
    login_as("alice")
    course = create_course(client)
    return create_session(client, course["id"])


# --- what the worker broadcasts ----------------------------------------------------


def test_successful_ingestion_reports_each_stage(client, session, run_ingest, published):
    document = upload_ok(client, session["id"], content=make_pdf(["Heaps", "Graphs"]))
    run_ingest(document["id"])

    assert [e["stage"] for e in published] == ["parsing", "finding_topics", "indexing", "ready"]
    assert all(e["document_id"] == document["id"] and e["session_id"] == session["id"] for e in published)
    assert published[2]["page_count"] == 2
    assert published[3]["page_count"] == 2 and published[3]["chunk_count"] == 2


def test_failed_ingestion_reports_the_error(client, session, run_ingest, published):
    document = upload_ok(client, session["id"], content=make_pdf(["", ""]))
    run_ingest(document["id"])

    assert [e["stage"] for e in published] == ["parsing", "failed"]
    assert published[-1]["error"] == NO_TEXT_ERROR


# --- the SSE stream ------------------------------------------------------------------


def parse(chunk: str) -> tuple[str, dict]:
    """'event: x\\ndata: {...}\\n\\n' -> ("x", {...})"""
    lines = dict(line.split(": ", 1) for line in chunk.strip().split("\n"))
    return lines["event"], json.loads(lines["data"])


async def next_event(stream) -> str:
    """The next real event, skipping heartbeats."""
    for _ in range(50):
        chunk = await anext(stream)
        if chunk != progress.SSE_HEARTBEAT:
            return chunk
    raise AssertionError("no event arrived")


def run_stream(session_id: str, user_id: str, scenario):
    """Open the stream for a session, run `scenario(stream, redis)`, then close everything."""

    async def _go():
        settings = get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        engine = create_async_engine(settings.database_url)
        stream = event_stream(
            redis,
            async_sessionmaker(engine, expire_on_commit=False),
            uuid.UUID(user_id),
            uuid.UUID(session_id),
            heartbeat_seconds=0.2,
        )
        try:
            return await scenario(stream, redis)
        finally:
            await stream.aclose()  # like the browser closing the tab
            await redis.aclose()
            await engine.dispose()

    return asyncio.run(_go())


def current_user_id(client) -> str:
    return client.get("/auth/me").json()["id"]


def test_stream_starts_with_a_snapshot(client, session):
    document = upload_ok(client, session["id"], filename="Lecture 1.pdf", content=make_pdf(["Heaps"]))

    async def scenario(stream, redis):
        return parse(await anext(stream))

    event, data = run_stream(session["id"], current_user_id(client), scenario)
    assert event == "snapshot"
    assert data["session"]["status"] == "setup"
    assert [(d["id"], d["filename"], d["status"]) for d in data["documents"]] == [
        (document["id"], "Lecture 1.pdf", "queued")
    ]


def test_stream_forwards_only_this_sessions_events(client, session):
    user_id = current_user_id(client)
    other_session = create_session(client, session["course_id"], name="Final prep")

    async def scenario(stream, redis):
        await anext(stream)  # snapshot; we're subscribed from here on
        await progress.publish(redis, uuid.UUID(user_id), {"session_id": other_session["id"], "stage": "parsing"})
        await progress.publish(
            redis, uuid.UUID(user_id), {"session_id": session["id"], "document_id": "d1", "stage": "ready"}
        )
        return parse(await next_event(stream))

    event, data = run_stream(session["id"], user_id, scenario)
    assert event == "progress"
    assert data == {"session_id": session["id"], "document_id": "d1", "stage": "ready"}


def test_stream_sends_heartbeats_when_idle(client, session):
    async def scenario(stream, redis):
        await anext(stream)  # snapshot
        return await anext(stream)

    assert run_stream(session["id"], current_user_id(client), scenario) == progress.SSE_HEARTBEAT


def test_events_endpoint_checks_login_and_ownership(client, session, login_as):
    login_as("bob")
    assert client.get(f"/sessions/{session['id']}/events").status_code == 404
    client.cookies.clear()
    assert client.get(f"/sessions/{session['id']}/events").status_code == 401
