"""Outbound HTTP resilience for non-LLM integrations (OAuth, webhooks, …).

Uses :mod:`tenacity` with exponential backoff and *full jitter* (same strategy as
:class:`nce.providers.base.RetryPolicy`) so concurrent workers do not retry in lockstep
after shared outages or rate limits — avoiding thundering herds toward third-party APIs.

LLM traffic should continue to use :meth:`nce.providers.base.LLMProvider.execute_with_retry`;
this module covers other ``httpx`` call sites that previously retried not at all or surfaced only
generic exceptions.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    stop_after_delay,
)

from nce.config import redact_secrets_in_text
from nce.observability import (
    EXTERNAL_HTTP_ATTEMPTS_TOTAL,
    EXTERNAL_HTTP_FAILURES_TOTAL,
    EXTERNAL_HTTP_LATENCY_SECONDS,
    EXTERNAL_HTTP_RETRIES_TOTAL,
)

log = logging.getLogger(__name__)

T = TypeVar("T")


class ExternalAPIError(Exception):
    """Base class for outbound HTTP integration failures (non-LLM)."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "http",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code


class ExternalAPITransientError(ExternalAPIError):
    """Transient failure — timeouts, transport errors, HTTP 429 / 5xx.

    ``retry_after_s`` may be set when the upstream returned ``Retry-After`` (seconds).
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str = "http",
        status_code: int | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(message, operation=operation, status_code=status_code)
        self.retry_after_s = retry_after_s


class ExternalAPIClientError(ExternalAPIError):
    """Non-retryable client or upstream rejection (typical 4xx except 429)."""


class ExternalAPIRetriesExhaustedError(ExternalAPIError):
    """All retry attempts failed for a transient error chain."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        last_error: Exception,
        attempts: int,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, operation=operation, status_code=status_code)
        self.last_error = last_error
        self.attempts = attempts
        if isinstance(last_error, ExternalAPIError) and last_error.status_code is not None:
            self.status_code = last_error.status_code


def _backoff_cap_ms(
    attempt: int,
    *,
    base_delay_ms: int,
    max_delay_ms: int,
    backoff_factor: float,
) -> int:
    return min(int(base_delay_ms * (backoff_factor ** (attempt - 1))), max_delay_ms)


def _wait_seconds_policy(
    *,
    base_delay_ms: int,
    max_delay_ms: int,
    backoff_factor: float,
):
    """Build tenacity ``wait`` callable with full jitter and optional Retry-After merge."""

    def wait_policy(retry_state):  # type: ignore[no-untyped-def]
        attempt = retry_state.attempt_number
        cap_ms = _backoff_cap_ms(
            attempt,
            base_delay_ms=base_delay_ms,
            max_delay_ms=max_delay_ms,
            backoff_factor=backoff_factor,
        )
        exc: BaseException | None = None
        if retry_state.outcome is not None and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
        if isinstance(exc, ExternalAPITransientError) and exc.retry_after_s is not None:
            hint_ms = min(max_delay_ms, int(exc.retry_after_s * 1000))
            cap_ms = max(cap_ms, hint_ms)
        # Full jitter in [0, cap_ms] — spreads wakeup times across workers (AWS pattern).
        delay_ms = max(1, int(random.uniform(0, max(1, cap_ms))))
        return delay_ms / 1000.0

    return wait_policy


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` response header.

    Handles both integer seconds (``Retry-After: 30``) and HTTP-date format
    (``Retry-After: Wed, 21 Oct 2025 07:28:00 GMT``).
    Returns None if the value is absent or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0, int(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        return max(0, int(delta))
    except Exception:
        return None


def _make_before_sleep_safe(operation_name: str):
    """Return a tenacity before-sleep hook that logs only error type, never raw text."""

    def _hook(retry_state) -> None:  # type: ignore[no-untyped-def]
        EXTERNAL_HTTP_RETRIES_TOTAL.labels(operation=operation_name).inc()
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        log.info(
            "Retrying operation=%s attempt=%s error_type=%s",
            operation_name,
            retry_state.attempt_number,
            type(exc).__name__ if exc else "unknown",
        )

    return _hook


async def execute_http_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str = "http",
    max_retries: int = 3,
    base_delay_ms: int = 1_000,
    max_delay_ms: int = 30_000,
    max_total_ms: int = 60_000,
    backoff_factor: float = 2.0,
) -> T:
    """Run an async *operation* under tenacity retry + full jitter (no circuit breaker).

    Args:
        max_retries: Number of retries *after* the initial attempt.
            Total attempts = ``max_retries + 1``.
        operation_name: Stable, low-cardinality label used in logs and metrics
            (e.g. ``"oauth_refresh:sharepoint"``).  Do NOT include URLs, tenant
            IDs, user IDs, or secrets.  Maximum 128 characters.

    Raises
    ------
    ExternalAPIRetriesExhaustedError
        After retry budget is exhausted for :class:`ExternalAPITransientError` subclasses.
    ExternalAPIClientError
        Propagated immediately without retry.
    ValueError
        If any retry config parameter is out of range.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if base_delay_ms < 1:
        raise ValueError("base_delay_ms must be >= 1")
    if max_delay_ms < base_delay_ms:
        raise ValueError("max_delay_ms must be >= base_delay_ms")
    if max_total_ms < 1:
        raise ValueError("max_total_ms must be >= 1")
    if backoff_factor < 1.0:
        raise ValueError("backoff_factor must be >= 1.0")
    if len(operation_name) > 128:
        raise ValueError("operation_name must be <= 128 characters")

    stop = stop_after_attempt(max_retries + 1) | stop_after_delay(max_total_ms / 1000.0)
    retry_predicate = retry_if_exception(lambda exc: isinstance(exc, ExternalAPITransientError))

    async def _run_once() -> T:
        EXTERNAL_HTTP_ATTEMPTS_TOTAL.labels(operation=operation_name).inc()
        return await operation()

    _t0 = time.perf_counter()
    try:
        result = await AsyncRetrying(
            stop=stop,
            wait=_wait_seconds_policy(
                base_delay_ms=base_delay_ms,
                max_delay_ms=max_delay_ms,
                backoff_factor=backoff_factor,
            ),
            retry=retry_predicate,
            before_sleep=_make_before_sleep_safe(operation_name),
            reraise=False,
        )(_run_once)
        EXTERNAL_HTTP_LATENCY_SECONDS.labels(operation=operation_name).observe(
            time.perf_counter() - _t0
        )
        return result
    except RetryError as re:
        EXTERNAL_HTTP_LATENCY_SECONDS.labels(operation=operation_name).observe(
            time.perf_counter() - _t0
        )
        last_exc = re.last_attempt.exception()
        attempts = re.last_attempt.attempt_number
        if last_exc is None:
            EXTERNAL_HTTP_FAILURES_TOTAL.labels(
                operation=operation_name, error_type="no_exception"
            ).inc()
            raise ExternalAPIRetriesExhaustedError(
                f"{operation_name}: retries exhausted after {attempts} attempt(s) (no exception)",
                operation=operation_name,
                last_error=RuntimeError("retry exhausted without captured exception"),
                attempts=attempts,
            ) from None
        if isinstance(last_exc, ExternalAPIClientError):
            EXTERNAL_HTTP_FAILURES_TOTAL.labels(
                operation=operation_name, error_type="client_error"
            ).inc()
            raise last_exc
        if isinstance(last_exc, ExternalAPIRetriesExhaustedError):
            raise last_exc
        safe_error = redact_secrets_in_text(str(last_exc))
        EXTERNAL_HTTP_FAILURES_TOTAL.labels(
            operation=operation_name, error_type=type(last_exc).__name__
        ).inc()
        log.warning(
            "%s: HTTP retries exhausted after %d attempt(s) — last error: %s",
            operation_name,
            attempts,
            safe_error,
        )
        raise ExternalAPIRetriesExhaustedError(
            f"{operation_name}: retries exhausted after {attempts} attempt(s): {safe_error}",
            operation=operation_name,
            last_error=last_exc if isinstance(last_exc, Exception) else RuntimeError(str(last_exc)),
            attempts=attempts,
        ) from last_exc


def classify_httpx_response(
    resp: httpx.Response,
    *,
    operation: str,
    error_detail: str = "",
) -> None:
    """Raise typed API errors for a finished ``httpx`` response (no network I/O)."""
    status = resp.status_code
    tail = redact_secrets_in_text((error_detail or getattr(resp, "text", "")[:500]).strip())

    if status == 429:
        retry_after_s = _parse_retry_after(resp.headers.get("retry-after"))
        raise ExternalAPITransientError(
            f"{operation}: rate limited (HTTP 429)" + (f" — {tail}" if tail else ""),
            operation=operation,
            status_code=429,
            retry_after_s=retry_after_s,
        )

    if status >= 500:
        raise ExternalAPITransientError(
            f"{operation}: upstream error (HTTP {status})" + (f" — {tail}" if tail else ""),
            operation=operation,
            status_code=status,
        )

    if not resp.is_success:
        raise ExternalAPIClientError(
            f"{operation}: HTTP {status}" + (f" — {tail}" if tail else ""),
            operation=operation,
            status_code=status,
        )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    operation_name: str,
    max_retries: int = 3,
    base_delay_ms: int = 1_000,
    max_delay_ms: int = 30_000,
    max_total_ms: int = 60_000,
    backoff_factor: float = 2.0,
    **kwargs: Any,
) -> httpx.Response:
    """Send an HTTP request using AsyncClient with retries on transient errors."""

    async def once() -> httpx.Response:
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ExternalAPITransientError(
                f"{operation_name}: request timed out",
                operation=operation_name,
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalAPITransientError(
                f"{operation_name}: transport error: {redact_secrets_in_text(str(exc))}",
                operation=operation_name,
            ) from exc
        classify_httpx_response(resp, operation=operation_name)
        return resp

    return await execute_http_with_retry(
        once,
        operation_name=operation_name,
        max_retries=max_retries,
        base_delay_ms=base_delay_ms,
        max_delay_ms=max_delay_ms,
        max_total_ms=max_total_ms,
        backoff_factor=backoff_factor,
    )


def request_with_retry_sync(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    operation_name: str,
    max_retries: int = 3,
    base_delay_ms: int = 1_000,
    max_delay_ms: int = 30_000,
    max_total_ms: int = 60_000,
    backoff_factor: float = 2.0,
    **kwargs: Any,
) -> httpx.Response:
    """Send an HTTP request using httpx.Client with retries on transient errors (sync)."""
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    if base_delay_ms < 1:
        raise ValueError("base_delay_ms must be >= 1")
    if max_delay_ms < base_delay_ms:
        raise ValueError("max_delay_ms must be >= base_delay_ms")
    if max_total_ms < 1:
        raise ValueError("max_total_ms must be >= 1")
    if backoff_factor < 1.0:
        raise ValueError("backoff_factor must be >= 1.0")
    if len(operation_name) > 128:
        raise ValueError("operation_name must be <= 128 characters")

    stop = stop_after_attempt(max_retries + 1) | stop_after_delay(max_total_ms / 1000.0)
    retry_predicate = retry_if_exception(lambda exc: isinstance(exc, ExternalAPITransientError))

    def once() -> httpx.Response:
        EXTERNAL_HTTP_ATTEMPTS_TOTAL.labels(operation=operation_name).inc()
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ExternalAPITransientError(
                f"{operation_name}: request timed out",
                operation=operation_name,
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalAPITransientError(
                f"{operation_name}: transport error: {redact_secrets_in_text(str(exc))}",
                operation=operation_name,
            ) from exc
        classify_httpx_response(resp, operation=operation_name)
        return resp

    _t0 = time.perf_counter()
    try:
        result = Retrying(
            stop=stop,
            wait=_wait_seconds_policy(
                base_delay_ms=base_delay_ms,
                max_delay_ms=max_delay_ms,
                backoff_factor=backoff_factor,
            ),
            retry=retry_predicate,
            before_sleep=_make_before_sleep_safe(operation_name),
            reraise=False,
        )(once)
        EXTERNAL_HTTP_LATENCY_SECONDS.labels(operation=operation_name).observe(
            time.perf_counter() - _t0
        )
        return result
    except RetryError as re:
        EXTERNAL_HTTP_LATENCY_SECONDS.labels(operation=operation_name).observe(
            time.perf_counter() - _t0
        )
        last_exc = re.last_attempt.exception()
        attempts = re.last_attempt.attempt_number
        if last_exc is None:
            EXTERNAL_HTTP_FAILURES_TOTAL.labels(
                operation=operation_name, error_type="no_exception"
            ).inc()
            raise ExternalAPIRetriesExhaustedError(
                f"{operation_name}: retries exhausted after {attempts} attempt(s) (no exception)",
                operation=operation_name,
                last_error=RuntimeError("retry exhausted without captured exception"),
                attempts=attempts,
            ) from None
        if isinstance(last_exc, ExternalAPIClientError):
            EXTERNAL_HTTP_FAILURES_TOTAL.labels(
                operation=operation_name, error_type="client_error"
            ).inc()
            raise last_exc
        if isinstance(last_exc, ExternalAPIRetriesExhaustedError):
            raise last_exc
        safe_error = redact_secrets_in_text(str(last_exc))
        EXTERNAL_HTTP_FAILURES_TOTAL.labels(
            operation=operation_name, error_type=type(last_exc).__name__
        ).inc()
        log.warning(
            "%s: HTTP retries exhausted after %d attempt(s) — last error: %s",
            operation_name,
            attempts,
            safe_error,
        )
        raise ExternalAPIRetriesExhaustedError(
            f"{operation_name}: retries exhausted after {attempts} attempt(s): {safe_error}",
            operation=operation_name,
            last_error=last_exc if isinstance(last_exc, Exception) else RuntimeError(str(last_exc)),
            attempts=attempts,
        ) from last_exc


async def oauth_token_post_form(
    url: str,
    data: dict[str, str],
    *,
    operation: str,
    timeout: float | httpx.Timeout = 30.0,
    headers: dict[str, str] | None = None,
) -> dict:
    """POST ``application/x-www-form-urlencoded`` to an OAuth token endpoint with retries.

    The ``httpx.AsyncClient`` is created once and reused across all retry attempts so
    connection pooling is preserved.  Pass an ``httpx.Timeout`` for per-phase control
    (connect / read / write / pool).
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    timeout_config = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
    async with httpx.AsyncClient(timeout=timeout_config) as client:

        async def once() -> dict:
            try:
                resp = await client.post(url, headers=hdrs, data=data)
            except httpx.TimeoutException as exc:
                raise ExternalAPITransientError(
                    f"{operation}: request timed out",
                    operation=operation,
                ) from exc
            except httpx.RequestError as exc:
                raise ExternalAPITransientError(
                    f"{operation}: transport error: {redact_secrets_in_text(str(exc))}",
                    operation=operation,
                ) from exc
            classify_httpx_response(resp, operation=operation)
            try:
                return resp.json()
            except ValueError as exc:
                raise ExternalAPIClientError(
                    f"{operation}: token endpoint returned non-JSON body",
                    operation=operation,
                    status_code=resp.status_code,
                ) from exc

        return await execute_http_with_retry(once, operation_name=operation)


async def post_json_with_retry(
    url: str,
    json_data: dict[str, Any],
    *,
    operation: str,
    timeout: float | httpx.Timeout = 60.0,
    headers: dict[str, str] | None = None,
) -> dict:
    """POST JSON data to an endpoint with retries and connection pooling.

    Reuses connection pooling across tenacity retry attempts under external timeouts
    or transport drops.
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Type", "application/json")
    timeout_config = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
    async with httpx.AsyncClient(timeout=timeout_config) as client:

        async def once() -> dict:
            try:
                resp = await client.post(url, headers=hdrs, json=json_data)
            except httpx.TimeoutException as exc:
                raise ExternalAPITransientError(
                    f"{operation}: request timed out",
                    operation=operation,
                ) from exc
            except httpx.RequestError as exc:
                raise ExternalAPITransientError(
                    f"{operation}: transport error: {redact_secrets_in_text(str(exc))}",
                    operation=operation,
                ) from exc
            classify_httpx_response(resp, operation=operation)
            try:
                return resp.json()
            except ValueError as exc:
                raise ExternalAPIClientError(
                    f"{operation}: endpoint returned non-JSON body",
                    operation=operation,
                    status_code=resp.status_code,
                ) from exc

        return await execute_http_with_retry(once, operation_name=operation)
