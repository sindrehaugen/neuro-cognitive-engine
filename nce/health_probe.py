"""Shared lightweight HTTP liveness and singleflight readiness helpers.

Separates:
  - ``GET /healthz``: sub-millisecond in-memory liveness response (zero DB/Redis/crypto calls)
    to prevent unauthenticated probe amplification or readiness-induced restart storms.
  - ``GET /readyz``: pooled connectivity probe coalesced across concurrent callers and cached
    for a bounded TTL (default 5.0 seconds) via :class:`SingleflightReadinessCache`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


def build_liveness_payload(
    service: str,
    *,
    shutting_down: bool = False,
    version: str | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Return an in-memory liveness payload and HTTP status code without external I/O."""
    payload: dict[str, Any] = {
        "status": "shutting_down" if shutting_down else "ok",
        "service": service,
    }
    if version:
        payload["version"] = version
    if extra:
        payload.update(extra)
    return payload, (503 if shutting_down else 200)


class SingleflightReadinessCache:
    """Coalesce concurrent readiness checks and cache the result for ``ttl_seconds``."""

    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self._ttl_seconds = max(0.1, float(ttl_seconds))
        self._cached_payload: dict[str, Any] | None = None
        self._cached_status: int = 503
        self._expires_monotonic: float = 0.0
        self._inflight: asyncio.Task[tuple[dict[str, Any], int]] | None = None
        self._inflight_loop: asyncio.AbstractEventLoop | None = None

    def invalidate(self) -> None:
        """Clear cached readiness state (e.g., on shutdown or test isolation)."""
        self._cached_payload = None
        self._cached_status = 503
        self._expires_monotonic = 0.0
        self._inflight = None
        self._inflight_loop = None

    async def get_or_probe(
        self,
        probe_fn: Callable[[], Awaitable[tuple[dict[str, Any], int]]],
    ) -> tuple[dict[str, Any], int]:
        """Return a fresh cached readiness verdict or execute *probe_fn* once in-flight."""
        now = time.monotonic()
        if self._cached_payload is not None and now < self._expires_monotonic:
            return dict(self._cached_payload), self._cached_status

        loop = asyncio.get_running_loop()
        if self._inflight is not None and not self._inflight.done() and self._inflight_loop is loop:
            payload, status_code = await asyncio.shield(self._inflight)
            return dict(payload), status_code

        async def _run_and_cache() -> tuple[dict[str, Any], int]:
            payload, status_code = await probe_fn()
            self._cached_payload = dict(payload)
            self._cached_status = int(status_code)
            self._expires_monotonic = time.monotonic() + self._ttl_seconds
            return payload, status_code

        task = loop.create_task(_run_and_cache())
        self._inflight = task
        self._inflight_loop = loop
        try:
            payload, status_code = await asyncio.shield(task)
            return dict(payload), status_code
        finally:
            if self._inflight is task and task.done():
                self._inflight = None


async def probe_engine_readiness(
    engine: Any,
    *,
    service: str,
    shutting_down: bool = False,
    version: str | None = None,
    require_connected: bool = True,
) -> tuple[dict[str, Any], int]:
    """Check pooled PostgreSQL, Redis, and optional Mongo connectivity using bounded timeouts."""
    if shutting_down:
        return (
            {
                "status": "down",
                "service": service,
                "reason": "shutting_down",
            },
            503,
        )

    if engine is None:
        return (
            {
                "status": "down" if require_connected else "ok",
                "service": service,
                "reason": "engine_not_initialised",
            },
            503 if require_connected else 200,
        )

    checks: dict[str, str] = {}
    reasons: list[dict[str, Any]] = []

    pg_pool = getattr(engine, "pg_pool", None)
    if pg_pool is None:
        if require_connected:
            checks["postgres"] = "uninitialised"
            reasons.append({"reason": "postgres_uninitialised", "blocking": True})
    else:
        try:
            async with pg_pool.acquire(timeout=3.0) as conn:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=3.0)
            checks["postgres"] = "up"
        except Exception:
            checks["postgres"] = "down"
            reasons.append({"reason": "postgres_unreachable", "blocking": True})

    redis_client = getattr(engine, "redis_client", None)
    if redis_client is None:
        if require_connected:
            checks["redis"] = "uninitialised"
            reasons.append({"reason": "redis_uninitialised", "blocking": True})
    else:
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=2.0)
            checks["redis"] = "up"
        except Exception:
            checks["redis"] = "down"
            reasons.append({"reason": "redis_unreachable", "blocking": True})

    mongo_client = getattr(engine, "mongo_client", None)
    if mongo_client is not None:
        try:
            await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=2.0)
            checks["mongo"] = "up"
        except Exception:
            checks["mongo"] = "down"
            reasons.append({"reason": "mongo_unreachable", "blocking": True})

    ready = not any(r.get("blocking") for r in reasons)
    payload: dict[str, Any] = {
        "status": "ok" if ready else "degraded",
        "service": service,
        "databases": checks,
        "degraded_reasons": reasons,
    }
    if version:
        payload["version"] = version
    return payload, (200 if ready else 503)
