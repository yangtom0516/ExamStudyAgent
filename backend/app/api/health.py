"""GET /health: checks that every data store answers.

Returns 200 when all checks pass, 503 otherwise. Error details go to the server log;
the response shows only the error type so it never leaks hostnames or credentials.
"""

import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 3


async def _run_check(name: str, check: Awaitable[object]) -> str:
    try:
        await asyncio.wait_for(check, timeout=CHECK_TIMEOUT_SECONDS)
        return "ok"
    except Exception as exc:
        logger.warning("Health check %s failed: %r", name, exc)
        return f"error: {type(exc).__name__}"


async def _check_postgres(request: Request) -> None:
    async with request.app.state.db_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_storage(request: Request) -> None:
    if not await request.app.state.storage.bucket_exists():
        raise RuntimeError("bucket missing")


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    state = request.app.state
    checks = {
        "postgres": _check_postgres(request),
        "redis": state.redis.ping(),
        "qdrant": state.qdrant.get_collections(),
        "storage": _check_storage(request),
    }
    results = await asyncio.gather(*(_run_check(name, c) for name, c in checks.items()))
    by_name = dict(zip(checks, results))

    healthy = all(r == "ok" for r in results)
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": by_name}
