"""Shared HTTP helpers for the STT/TTS providers: one long-lived connection, pre-warming, retries."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("xenio.voice.http")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Measured against nabh.cloud: setting up a new HTTPS connection costs 0.4-0.8 s per request. httpx closes idle
# connections after 5 s, so nearly every spoken request paid that. Keep them for two minutes instead.
KEEPALIVE_SECONDS = 120.0
_WARM_INTERVAL = 20.0


def new_client(timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        transport=transport,
        limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=KEEPALIVE_SECONDS),
    )


class ConnectionWarmer:
    """Opens the HTTPS connection before it is needed (when the user starts talking), so the request that
    follows skips the connection setup. Sends only a HEAD to the server root: no credentials, no audio."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        parts = urlsplit(url)
        self._origin = f"{parts.scheme}://{parts.netloc}/" if parts.scheme and parts.netloc else ""
        self._client = client
        self._last = -_WARM_INTERVAL
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        """A real request just used the connection."""
        self._last = time.monotonic()

    def warm(self) -> None:
        """Fire-and-forget; at most once per 20 s, and never while a warm-up is already running."""
        if not self._origin or time.monotonic() - self._last < _WARM_INTERVAL:
            return
        if self._task is not None and not self._task.done():
            return
        self._last = time.monotonic()
        try:
            self._task = asyncio.ensure_future(self._head())
        except RuntimeError:  # no running event loop
            self._task = None

    async def _head(self) -> None:
        try:
            await self._client.head(self._origin, timeout=5.0)
        except (httpx.HTTPError, OSError) as exc:  # only an optimisation: the real request will connect itself
            log.debug("Connection warm-up failed: %s", type(exc).__name__)

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


class ProviderTimeout(Exception):
    pass


class ProviderConnectionError(Exception):
    pass


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit():
            return min(float(retry_after), 5.0)
    return min(0.5 * (2**attempt), 4.0)


async def post_with_retries(
    client: httpx.AsyncClient, url: str, *, what: str, max_retries: int, **kwargs: Any
) -> httpx.Response:
    """POST, retrying timeouts, connection errors and 408/429/5xx with backoff.
    Returns the final response (which may still be an error status)."""
    attempt = 0
    while True:
        try:
            response = await client.post(url, **kwargs)
        except httpx.TimeoutException as exc:
            if attempt >= max_retries:
                raise ProviderTimeout(f"{what} timed out") from exc
            delay = _retry_delay(None, attempt)
            log.warning("%s timed out; retrying", what, extra={"fields": {"attempt": attempt + 1, "delay_s": delay}})
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise ProviderConnectionError(f"{what} connection failed: {type(exc).__name__}") from exc
            delay = _retry_delay(None, attempt)
            log.warning("%s connection error; retrying", what, extra={"fields": {"attempt": attempt + 1}})
        else:
            if response.status_code not in RETRYABLE_STATUS or attempt >= max_retries:
                return response
            delay = _retry_delay(response, attempt)
            log.warning(
                "%s returned a retryable status",
                what,
                extra={"fields": {"status": response.status_code, "attempt": attempt + 1, "delay_s": delay}},
            )
        attempt += 1
        await asyncio.sleep(delay)


def error_snippet(response: httpx.Response, limit: int = 200) -> str:
    """Short, single-line error body for messages (the log redactor still masks secrets)."""
    try:
        text = response.text
    except Exception:
        return ""
    return " ".join(text.split())[:limit]
