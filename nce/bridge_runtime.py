"""
Sync entrypoints for bridge workers (RQ) — fetch OAuth tokens encrypted at rest.
"""

from __future__ import annotations

import asyncio
import logging

from nce import bridge_repo
from nce.bridge_providers import BRIDGE_PROVIDERS
from nce.bridge_renewal import ensure_fresh_oauth_token
from nce.config import cfg
from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.bridge_runtime")

_RESOLVE_TIMEOUT_S: float = cfg.BRIDGE_RESOLVE_TIMEOUT_S

# Explicit allowlist — rejects unknown providers before any DB/network I/O.
PROVIDERS: frozenset[str] = BRIDGE_PROVIDERS


async def resolve_stored_oauth_access_token_async(
    provider: str,
    *,
    client_state: str | None = None,
    subscription_id: str | None = None,
    resource_id: str | None = None,
) -> str | None:
    """Async core: load and decrypt OAuth access token from ``bridge_subscriptions``.

    Returns ``None`` if no matching subscription is found or on any error.
    Callers that already own an event loop (e.g. FastAPI handlers) should await
    this directly rather than going through the sync wrapper.
    """
    provider_lower = provider.strip().lower()
    if provider_lower not in PROVIDERS:
        log.warning(
            "resolve_stored_oauth_access_token: unknown provider=%r — rejected.",
            provider,
        )
        return None

    if not any((client_state, subscription_id, resource_id)):
        return None

    engine = NCEEngine()
    await engine.connect()
    try:
        async with engine.pg_pool.acquire(timeout=10.0) as conn:
            row = await bridge_repo.fetch_active_subscription(
                conn,
                provider_lower,
                client_state=client_state,
                subscription_id=subscription_id,
                resource_id=resource_id,
            )
        if not row:
            return None
        try:
            return await ensure_fresh_oauth_token(engine.pg_pool, row, "")
        except Exception as exc:
            log.warning(
                "%s: Stored bridge OAuth token fetch/refresh failed for provider=%r: %s",
                type(exc).__name__,
                provider,
                exc,
            )
            return None
    finally:
        try:
            await asyncio.wait_for(engine.disconnect(), timeout=5.0)
        except Exception as exc:
            log.warning(
                "%s: engine.disconnect() failed for provider=%r: %s",
                type(exc).__name__,
                provider,
                exc,
            )


def resolve_stored_oauth_access_token(
    provider: str,
    *,
    client_state: str | None = None,
    subscription_id: str | None = None,
    resource_id: str | None = None,
) -> str | None:
    """Sync wrapper: safe to call from RQ workers (no running event loop).

    Raises ``RuntimeError`` if called from a thread that already owns a running
    event loop — use ``resolve_stored_oauth_access_token_async`` there instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        raise RuntimeError(
            "resolve_stored_oauth_access_token() called from a running event loop. "
            "Await resolve_stored_oauth_access_token_async() instead."
        )

    async def _run_with_timeout() -> str | None:
        try:
            return await asyncio.wait_for(
                resolve_stored_oauth_access_token_async(
                    provider,
                    client_state=client_state,
                    subscription_id=subscription_id,
                    resource_id=resource_id,
                ),
                timeout=_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            log.warning(
                "resolve_stored_oauth_access_token timed out after %.1fs for "
                "provider=%r — upstream vendor did not respond. "
                "Returning None so worker can degrade gracefully.",
                _RESOLVE_TIMEOUT_S,
                provider,
            )
            return None
        except Exception as exc:
            log.warning(
                "%s: resolve_stored_oauth_access_token failed for provider=%r: %s",
                type(exc).__name__,
                provider,
                exc,
            )
            return None

    return asyncio.run(_run_with_timeout())
