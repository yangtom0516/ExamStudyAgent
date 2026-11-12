"""Gemini API access shared by ingestion (and, later, the agents).

Retries happen per API call rather than per whole job, so a rate limit halfway through a
big document doesn't throw away the batches that already succeeded.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from google import genai
from google.genai import errors as genai_errors

from app.config import Settings

logger = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = (2, 4, 8, 16, 32)  # exponential backoff, at most 5 retries
MAX_SERVER_DELAY_SECONDS = 90  # cap on how long we'll wait when Google asks us to


def make_client(settings: Settings) -> genai.Client:
    api_key = settings.google_api_key.get_secret_value()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set in .env")
    return genai.Client(api_key=api_key)


def is_transient(exc: BaseException) -> bool:
    """Worth retrying: rate limits (429), Google-side errors (5xx), timeouts and dropped connections."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


def server_retry_delay(exc: BaseException) -> float | None:
    """How long Google asks us to wait, from a 429's RetryInfo (e.g. "retryDelay": "57s"), if given."""
    if not isinstance(exc, genai_errors.APIError) or not isinstance(exc.details, dict):
        return None
    details = (exc.details.get("error") or {}).get("details") or []
    for detail in details:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(detail.get("retryDelay", "")).removesuffix("s"))
            except ValueError:
                return None
    return None


async def with_retries[T](call: Callable[[], Awaitable[T]], what: str) -> T:
    """Run `call`, retrying transient failures with backoff. Other errors are raised at once.

    When a rate-limit response says how long to wait (e.g. until the per-minute quota resets),
    wait that long instead, so retries don't all land inside the same minute and fail again.
    """
    for attempt, backoff in enumerate((*RETRY_DELAYS_SECONDS, None), start=1):
        try:
            return await call()
        except Exception as exc:
            if backoff is None or not is_transient(exc):
                raise
            delay = max(backoff, min(server_retry_delay(exc) or 0, MAX_SERVER_DELAY_SECONDS))
            logger.warning("%s failed (attempt %d: %r); retrying in %.0fs", what, attempt, exc, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
