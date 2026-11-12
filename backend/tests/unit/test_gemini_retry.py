import asyncio

import httpx
import pytest
from google.genai import errors as genai_errors

from app.services import gemini


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Record backoff delays instead of sleeping through them."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(gemini.asyncio, "sleep", fake_sleep)
    return delays


def api_error(code: int) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"code": code, "message": "test", "status": "TEST"}})


def flaky(failures: list[Exception], result="ok"):
    """A call that raises the given errors in turn, then succeeds."""
    remaining = list(failures)

    async def call():
        if remaining:
            raise remaining.pop(0)
        return result

    return call


def test_rate_limits_and_server_errors_are_retried_with_backoff(no_waiting):
    call = flaky([api_error(429), api_error(503), httpx.ConnectError("dropped")])
    assert asyncio.run(gemini.with_retries(call, what="test")) == "ok"
    assert no_waiting == [2, 4, 8]


def test_gives_up_after_five_retries(no_waiting):
    call = flaky([api_error(429)] * 6)
    with pytest.raises(genai_errors.APIError):
        asyncio.run(gemini.with_retries(call, what="test"))
    assert no_waiting == [2, 4, 8, 16, 32]


def rate_limited(retry_delay: str) -> genai_errors.APIError:
    """A 429 like the real one, including Google's "retry in N seconds" hint."""
    return genai_errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}],
            }
        },
    )


def test_waits_as_long_as_google_asks(no_waiting):
    call = flaky([rate_limited("57s"), rate_limited("0s")])
    assert asyncio.run(gemini.with_retries(call, what="test")) == "ok"
    # 57 s as asked; then "0s" falls back to the normal backoff (4 s for the second retry).
    assert no_waiting == [57, 4]


def test_requested_wait_is_capped(no_waiting):
    call = flaky([rate_limited("3600s")])
    asyncio.run(gemini.with_retries(call, what="test"))
    assert no_waiting == [gemini.MAX_SERVER_DELAY_SECONDS]


def test_permanent_errors_are_not_retried(no_waiting):
    call = flaky([api_error(400)])  # e.g. a bad request: retrying won't fix it
    with pytest.raises(genai_errors.APIError):
        asyncio.run(gemini.with_retries(call, what="test"))
    assert no_waiting == []
