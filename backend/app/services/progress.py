"""Live ingestion progress over Redis pub/sub.

The worker publishes one small JSON event per stage; the API's SSE endpoint is subscribed and
forwards them to the browser. Pub/sub stores nothing: a listener that isn't connected misses
the event, which is why the SSE stream starts with a snapshot from Postgres (the source of truth).

Event: {"document_id", "session_id", "stage", ...extras}
  stage "parsing"                 the worker started reading the file
  stage "reading_scans"  pages_done, pages_total   OCR of scanned pages (only if there are any)
  stage "finding_topics"          one Gemini call listing the document's topics
  stage "indexing"   page_count   text extracted; embedding and indexing the chunks
  stage "ready"      page_count, chunk_count
  stage "failed"     error        user-facing message
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

PARSING = "parsing"
READING_SCANS = "reading_scans"
FINDING_TOPICS = "finding_topics"
INDEXING = "indexing"
READY = "ready"
FAILED = "failed"

# How the pipeline reports progress; tests pass a function that records events instead.
Publisher = Callable[[uuid.UUID, dict], Awaitable[None]]


def channel(user_id: uuid.UUID) -> str:
    return f"progress:{user_id}"


async def publish(redis: Redis, user_id: uuid.UUID, event: dict) -> None:
    """Broadcast an event. Never raises: progress is nice to have, the job must not fail over it."""
    try:
        await redis.publish(channel(user_id), json.dumps(event))
    except Exception:
        logger.warning("Could not publish progress event %s", event, exc_info=True)


def sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event. json.dumps keeps it on one line, as SSE requires."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


SSE_HEARTBEAT = ": ping\n\n"  # a comment line: ignored by browsers, keeps the connection alive
