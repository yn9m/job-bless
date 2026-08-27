"""Shared HTTP machinery for the LLM adapters: retries, SSE, error mapping.

Adapters only build request payloads and parse responses; everything network
related lives here so the three standards behave identically on failures.
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from src.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)


@dataclass
class SSEEvent:
    """One decoded server-sent event."""

    event: str
    data: Dict[str, Any]


class HttpTransport:
    """Thin async HTTP client with provider-uniform retries and error mapping."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        headers: Dict[str, str],
        timeout_sec: float = 120.0,
        max_retries: int = 3,
        retry_backoff_sec: float = 1.0,
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, max_retries)
        self.retry_backoff_sec = retry_backoff_sec
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_sec),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- plain JSON calls -------------------------------------------------

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    # None would disable the timeout entirely, hence the sentinel.
                    timeout=httpx.Timeout(timeout_sec) if timeout_sec else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.TimeoutException as e:
                error: LLMError = LLMTimeoutError(f"request to {path} timed out: {e}", provider=self.provider)
            except httpx.HTTPError as e:
                error = LLMConnectionError(f"request to {path} failed: {e}", provider=self.provider)
            else:
                if response.status_code < 400:
                    return self._decode_json(response)
                error = self._map_status_error(response)

            if not self._should_retry(error, attempt):
                raise error
            await self._sleep_before_retry(attempt, error, path)
            attempt += 1

    # --- server-sent events ----------------------------------------------

    async def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: Optional[float] = None,
    ) -> AsyncIterator[SSEEvent]:
        """Yield decoded SSE events; `data: [DONE]` terminates the stream.

        Retries only happen before the first event is delivered — once the
        caller has seen a chunk, replaying the request would duplicate output.
        """
        attempt = 0
        while True:
            delivered = False
            try:
                async with self._client.stream(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    # None would disable the timeout entirely, hence the sentinel.
                    timeout=httpx.Timeout(timeout_sec) if timeout_sec else httpx.USE_CLIENT_DEFAULT,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        error: LLMError = self._map_status_error(response)
                    else:
                        async for event in self._iter_sse(response):
                            delivered = True
                            yield event
                        return
            except httpx.TimeoutException as e:
                error = LLMTimeoutError(f"stream {path} timed out: {e}", provider=self.provider)
            except httpx.HTTPError as e:
                error = LLMConnectionError(f"stream {path} failed: {e}", provider=self.provider)

            if delivered or not self._should_retry(error, attempt):
                raise error
            await self._sleep_before_retry(attempt, error, path)
            attempt += 1

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[SSEEvent]:
        event_name = ""
        data_lines: list[str] = []

        async for line in response.aiter_lines():
            line = line.rstrip("\r")
            if line.startswith(":"):
                continue  # comment / keep-alive
            if line:
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
                continue

            # blank line -> dispatch the accumulated event
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            event_name, data_lines = "", []
            if payload == "[DONE]":
                return
            try:
                yield SSEEvent(event=event_name, data=json.loads(payload))
            except json.JSONDecodeError:
                logger.warning("skipping malformed SSE payload from %s: %.200s", self.provider, payload)

        if data_lines:
            payload = "\n".join(data_lines)
            if payload != "[DONE]":
                try:
                    yield SSEEvent(event=event_name, data=json.loads(payload))
                except json.JSONDecodeError:
                    logger.warning("skipping malformed SSE tail from %s: %.200s", self.provider, payload)

    # --- helpers ----------------------------------------------------------

    def _decode_json(self, response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as e:
            raise LLMResponseError(
                f"provider returned non-JSON body: {e}",
                provider=self.provider,
                status_code=response.status_code,
                raw=response.text[:2000],
            ) from e
        if not isinstance(data, dict):
            raise LLMResponseError(
                f"expected a JSON object, got {type(data).__name__}",
                provider=self.provider,
                status_code=response.status_code,
                raw=data,
            )
        return data

    def _map_status_error(self, response: httpx.Response) -> LLMError:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text[:2000]

        message = _extract_error_message(body) or f"HTTP {response.status_code}"
        request_id = response.headers.get("x-request-id", "")
        status = response.status_code
        kwargs = {"provider": self.provider, "status_code": status, "request_id": request_id, "raw": body}

        if status in (401, 403):
            return LLMAuthError(message, **kwargs)
        if status == 429:
            retry_after = response.headers.get("retry-after")
            return LLMRateLimitError(
                message,
                retry_after_sec=float(retry_after) if _is_number(retry_after) else None,
                **kwargs,
            )
        if status >= 500:
            return LLMServerError(message, **kwargs)
        return LLMBadRequestError(message, **kwargs)

    def _should_retry(self, error: LLMError, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        if isinstance(error, (LLMTimeoutError, LLMConnectionError, LLMServerError, LLMRateLimitError)):
            return True
        return isinstance(error, LLMBadRequestError) and error.status_code in RETRYABLE_STATUS

    async def _sleep_before_retry(self, attempt: int, error: LLMError, path: str) -> None:
        delay = self.retry_backoff_sec * (2 ** attempt)
        if isinstance(error, LLMRateLimitError) and error.retry_after_sec:
            delay = max(delay, error.retry_after_sec)
        delay += random.uniform(0, self.retry_backoff_sec / 2)  # jitter
        logger.warning(
            "llm request failed (provider=%s path=%s attempt=%d): %s; retrying in %.1fs",
            self.provider, path, attempt + 1, error, delay,
        )
        await asyncio.sleep(delay)


def _extract_error_message(body: Any) -> str:
    """Pull a human message out of OpenAI / Gemini / Anthropic error shapes."""
    if isinstance(body, str):
        return body.strip()
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    if isinstance(error, str):
        return error
    return str(body.get("message") or "")


def _is_number(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True
