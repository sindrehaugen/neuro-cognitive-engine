"""Process-local last-known-good governance cache for tool / skill disabling.

Audit Domain 1 (CWE-636 / CWE-1188): the original governance checks in
``mcp_stdio_dispatch.py`` and ``a2a_server.py`` read the ``nce:tools:disabled``
Redis hash inside a ``try/except`` that *defaulted to enabled* on any Redis
error. A revoked tool therefore executed during a Redis blip — a silent
un-revoke. The HMAC nonce store (:mod:`nce.auth`) already fails *closed*; this
module aligns governance with that posture while preserving availability.

Design — three states (not two)
-------------------------------
``ToolGovernanceCache`` distinguishes:

* **INITIALIZED** — a fetch succeeded at least once (``_snapshot`` is a real
  ``frozenset``, possibly empty; ``_fetched_at`` is set). Served from the
  snapshot under the staleness rules below. An initialized-EMPTY snapshot means
  "nothing is disabled" → ALLOW.
* **NEVER-INITIALIZED** — no fetch ever succeeded (``_snapshot is None``):

  - In **production** (``cfg.IS_PROD``) → fail-closed: raise
    :class:`GovernanceUnavailable` (block dispatch) + bump the degraded
    counter. This closes the cold-boot un-revoke hole: a process restart while
    Redis is unreachable must not let an admin-disabled tool execute because its
    disabled-state was never read.
  - In **dev / test** (``not cfg.IS_PROD``) → ALLOW (dev convenience) + bump the
    degraded counter, so the suite is not blocked.

Staleness on an INITIALIZED cache when Redis is unreachable:

* within ``STALE_OK`` → serve the last snapshot without a Redis call;
* past ``STALE_OK`` → attempt a refresh; on Redis error keep serving the last
  snapshot until age exceeds ``STALE_HARD``;
* past ``STALE_HARD`` → raise :class:`GovernanceUnavailable` (fail-closed) +
  bump the degraded counter.

When Redis is reachable past ``STALE_OK`` the snapshot + ``_fetched_at`` are
refreshed, so an admin re-enable/disable propagates within ``STALE_OK`` after
recovery.

The TTL uses :func:`time.monotonic` — never wall-clock — so clock adjustments
cannot make a stale snapshot look fresh (or vice versa).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any

from nce.config import cfg
from nce.observability import _safe_counter

# ``engine.redis_client`` is an ``redis.asyncio.Redis`` at runtime but is held
# untyped (``Any``) across the package; redis-py's ``hkeys`` overload reports a
# sync/async union return that does not type-check under ``await``. Mirror the
# rest of the package and treat the client as ``Any`` at this boundary.
RedisClient = Any
PgPool = Any

log = logging.getLogger("nce-governance")

_DISABLED_HASH_KEY = "nce:tools:disabled"
_DISABLED_ENGINES_HASH_KEY = "nce:engines:disabled"
_HYDRATED_SENTINEL_KEY = "nce:governance:cache_hydrated"
_HYDRATED_SENTINEL_TTL_SEC = 300
_GOVERNANCE_OP_TIMEOUT_SEC = 1.5

_SCOPE_HASH_KEYS: dict[str, str] = {
    "tool": _DISABLED_HASH_KEY,
    "engine": _DISABLED_ENGINES_HASH_KEY,
}

# Bumped whenever the cache serves a degraded decision (hard-stale fail-closed,
# never-initialized prod block, or never-initialized dev allow).
GOVERNANCE_DEGRADED_TOTAL = _safe_counter(
    "nce_tool_governance_degraded_total",
    "Total governance decisions served in a degraded mode "
    "(hard-stale fail-closed, or never-initialized boot before first fetch).",
)


def _is_prod_mode() -> bool:
    cfg_dict = getattr(cfg, "__dict__", {})
    if "IS_PROD" in cfg_dict:
        return bool(cfg_dict["IS_PROD"])
    env = os.getenv("NCE_ENV", "").strip().lower()
    if env in {"prod", "production"}:
        return True
    return bool(getattr(cfg, "IS_PROD", False))


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


class GovernanceUnavailable(Exception):
    """Raised when governance cannot be evaluated and the safe action is to block.

    The caller maps this to its existing strict scope error (``-32005`` on the
    MCP stdio surface, :class:`~nce.a2a.A2AScopeViolationError` → ``-32011`` on
    A2A) with a "governance registry unavailable" detail — never ``-32603`` and
    never an internal stack frame.
    """


class ToolGovernanceCache:
    """Last-known-good cache of admin-disabled tool / skill names.

    A single module singleton (:data:`GOVERNANCE`) is shared by both dispatch
    surfaces so a refresh on either warms both.
    """

    __slots__ = ("_snapshot", "_fetched_at")

    def __init__(self) -> None:
        # ``None`` == NEVER-INITIALIZED. A frozenset (even empty) == INITIALIZED.
        self._snapshot: frozenset[str] | None = None
        self._fetched_at: float | None = None

    # -- state inspection ---------------------------------------------------

    @property
    def initialized(self) -> bool:
        """True once at least one fetch has succeeded."""
        return self._snapshot is not None

    def _age(self) -> float:
        """Monotonic age (seconds) of the current snapshot.

        Returns ``inf`` when never initialized so age-based branches treat an
        un-warmed cache as maximally stale.
        """
        if self._fetched_at is None:
            return float("inf")
        return time.monotonic() - self._fetched_at

    # -- snapshot maintenance ----------------------------------------------

    def _store(self, names: frozenset[str]) -> None:
        self._snapshot = names
        self._fetched_at = time.monotonic()

    @staticmethod
    async def _fetch(
        redis_client: RedisClient, hash_key: str = _DISABLED_HASH_KEY
    ) -> frozenset[str]:
        """Read the disabled-name set from Redis (``hkeys`` of the hash)."""
        raw = await _maybe_await(redis_client.hkeys(hash_key))
        return frozenset(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in raw)

    # -- public API ---------------------------------------------------------

    async def warm(
        self,
        redis_client: RedisClient | None,
        *,
        pg_pool: PgPool | None = None,
    ) -> bool:
        """Attempt the initial (or a refreshing) fetch — used at startup.

        Returns ``True`` when the snapshot was (re)loaded, ``False`` on failure
        (no Redis client, or Redis raised). On failure the cache stays in its
        current state and the never-initialized prod-block / next-call retry
        still applies; the caller should log and continue booting.
        """
        if pg_pool is not None:
            try:
                disabled = await get_disabled_tools_with_pg_hydration(
                    redis_client,
                    pg_pool=pg_pool,
                    cache=self,
                )
                self._store(disabled)
                return True
            except Exception as exc:  # noqa: BLE001 - any PG/Redis error
                log.warning("Governance warm fetch (PG hydration) failed: %s", exc)
                return False

        if redis_client is None:
            return False
        try:
            self._store(await self._fetch(redis_client))
            return True
        except Exception as exc:  # noqa: BLE001 - any Redis/transport error
            log.warning("Governance warm fetch failed: %s", exc)
            return False

    # Alias for callers that prefer the ``initialize`` name.
    initialize = warm

    async def is_disabled(
        self,
        redis_client: RedisClient | None,
        name: str,
        *,
        pg_pool: PgPool | None = None,
    ) -> bool:
        """Return whether *name* is currently admin-disabled.

        Raises :class:`GovernanceUnavailable` when the safe action is to block
        dispatch (hard-stale or never-initialized-in-prod).
        """
        if pg_pool is not None:
            disabled_set = await get_disabled_tools_with_pg_hydration(
                redis_client,
                pg_pool=pg_pool,
                cache=self,
            )
            return name in disabled_set

        # Fresh enough to serve straight from the snapshot — no Redis call.
        if self.initialized and self._age() < cfg.NCE_TOOL_GOVERNANCE_STALE_OK_SEC:
            assert self._snapshot is not None  # narrowing for type-checkers
            return name in self._snapshot

        # Need a refresh (stale snapshot, or never initialized): try Redis.
        if redis_client is not None:
            try:
                names = await self._fetch(redis_client)
            except Exception as exc:  # noqa: BLE001 - any Redis/transport error
                return self._serve_degraded(name, exc)
            else:
                self._store(names)
                return name in names

        # No Redis client available at all — treat as a fetch failure.
        return self._serve_degraded(name, None)

    # -- degraded-path decision --------------------------------------------

    def _serve_degraded(self, name: str, exc: Exception | None) -> bool:
        """Decide what to do when a live fetch was impossible.

        Encapsulates the three-state policy so :meth:`is_disabled` reads as a
        single level of abstraction.
        """
        if self.initialized:
            assert self._snapshot is not None  # narrowing for type-checkers
            if self._age() <= cfg.NCE_TOOL_GOVERNANCE_STALE_HARD_SEC:
                # Within the hard window — keep enforcing the last snapshot.
                return name in self._snapshot
            # Past STALE_HARD — fail closed.
            GOVERNANCE_DEGRADED_TOTAL.inc()
            log.error(
                "Governance snapshot hard-stale (age>%ds); failing closed: %s",
                cfg.NCE_TOOL_GOVERNANCE_STALE_HARD_SEC,
                exc,
            )
            raise GovernanceUnavailable("governance registry unavailable (snapshot hard-stale)")

        # NEVER-INITIALIZED: no snapshot has ever been read.
        GOVERNANCE_DEGRADED_TOTAL.inc()
        if _is_prod_mode():
            # Cold-boot un-revoke hole: must not allow without ever reading state.
            log.error("Governance never initialized in production; failing closed: %s", exc)
            raise GovernanceUnavailable("governance registry unavailable (never initialized)")
        # Dev / test convenience: allow so the suite / local boot is not blocked.
        log.warning("Governance never initialized (dev/test); allowing: %s", exc)
        return False


# Module singleton — both dispatch surfaces share this instance.
GOVERNANCE = ToolGovernanceCache()


async def _fetch_disabled_by_scope_from_pg(pg_pool: PgPool) -> dict[str, set[str]]:
    """Query ``nce_get_disabled_runtime_tools(NULL)`` within bounded timeout."""
    from nce.db_utils import unmanaged_pg_connection

    async def _query() -> list[Any]:
        async with unmanaged_pg_connection(
            pg_pool,
            site="ops.governance.control_plane",
        ) as conn:
            return list(
                await conn.fetch(
                    "SELECT scope_key, tool_name FROM nce_get_disabled_runtime_tools(NULL)"
                )
            )

    rows = await asyncio.wait_for(_query(), timeout=_GOVERNANCE_OP_TIMEOUT_SEC)
    by_scope: dict[str, set[str]] = {"tool": set(), "engine": set()}
    for row in rows:
        scope = str(row["scope_key"] if isinstance(row, dict) else row["scope_key"]).strip()
        tool_name = str(row["tool_name"] if isinstance(row, dict) else row["tool_name"]).strip()
        if scope in by_scope and tool_name:
            by_scope[scope].add(tool_name)
    return by_scope


async def _sync_redis_from_pg_snapshot(
    redis_client: RedisClient | None,
    by_scope: dict[str, set[str]],
) -> None:
    """Repopulate Redis disabled hashes and set the hydration sentinel TTL."""
    if redis_client is None:
        return

    async def _write() -> None:
        for scope, hash_key in _SCOPE_HASH_KEYS.items():
            names = by_scope.get(scope, set())
            existing = set(
                k.decode("utf-8") if isinstance(k, bytes) else str(k)
                for k in (await _maybe_await(redis_client.hkeys(hash_key)) or [])
            )
            stale = existing - names
            if stale and callable(getattr(redis_client, "hdel", None)):
                await _maybe_await(redis_client.hdel(hash_key, *sorted(stale)))
            missing = names - existing
            if missing and callable(getattr(redis_client, "hset", None)):
                for item in sorted(missing):
                    await _maybe_await(redis_client.hset(hash_key, item, "1"))

        if callable(getattr(redis_client, "setex", None)):
            await _maybe_await(
                redis_client.setex(_HYDRATED_SENTINEL_KEY, _HYDRATED_SENTINEL_TTL_SEC, "1")
            )
        elif callable(getattr(redis_client, "set", None)):
            await _maybe_await(
                redis_client.set(_HYDRATED_SENTINEL_KEY, "1", ex=_HYDRATED_SENTINEL_TTL_SEC)
            )

    await asyncio.wait_for(_write(), timeout=_GOVERNANCE_OP_TIMEOUT_SEC)


async def set_runtime_tool_enabled(
    pg_pool: PgPool | None,
    redis_client: RedisClient | None,
    scope_key: str,
    name: str,
    enabled: bool,
    *,
    updated_by: str = "system",
    reason: str = "",
    note: str | None = None,
) -> dict[str, Any]:
    """Persist runtime tool/engine enablement in PG and write-through to Redis."""
    if scope_key not in _SCOPE_HASH_KEYS:
        raise ValueError(f"Unsupported governance scope_key: {scope_key!r}")
    tool_name = str(name or "").strip()
    if not tool_name:
        raise ValueError("Governance tool/engine name must be non-empty")

    effective_reason = str(reason if reason else (note or "")).strip()
    actor = str(updated_by or "system").strip() or "system"
    hash_key = _SCOPE_HASH_KEYS[scope_key]

    if pg_pool is not None:
        from nce.db_utils import unmanaged_pg_connection

        async def _persist() -> None:
            async with unmanaged_pg_connection(
                pg_pool,
                site="ops.governance.control_plane",
            ) as conn:
                await conn.execute(
                    """
                    INSERT INTO runtime_tool_governance
                        (scope_key, tool_name, enabled, reason, updated_by, updated_at)
                    VALUES ($1, $2, $3, $4, $5, now())
                    ON CONFLICT (scope_key, tool_name) DO UPDATE
                        SET enabled = EXCLUDED.enabled,
                            reason = EXCLUDED.reason,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = now()
                    """,
                    scope_key,
                    tool_name,
                    bool(enabled),
                    effective_reason,
                    actor,
                )

        await asyncio.wait_for(_persist(), timeout=_GOVERNANCE_OP_TIMEOUT_SEC)

    if redis_client is not None:

        async def _update_redis() -> None:
            if enabled:
                hdel_fn = getattr(redis_client, "hdel", None)
                if callable(hdel_fn):
                    await _maybe_await(hdel_fn(hash_key, tool_name))
            else:
                hset_fn = getattr(redis_client, "hset", None)
                if callable(hset_fn):
                    await _maybe_await(hset_fn(hash_key, tool_name, "1"))

            get_fn = getattr(redis_client, "get", None)
            sentinel_present = False
            if callable(get_fn):
                sentinel_present = bool(await _maybe_await(get_fn(_HYDRATED_SENTINEL_KEY)))
            if sentinel_present and callable(getattr(redis_client, "expire", None)):
                await _maybe_await(
                    redis_client.expire(_HYDRATED_SENTINEL_KEY, _HYDRATED_SENTINEL_TTL_SEC)
                )

        await asyncio.wait_for(_update_redis(), timeout=_GOVERNANCE_OP_TIMEOUT_SEC)

    if scope_key == "tool" and GOVERNANCE.initialized and GOVERNANCE._snapshot is not None:
        current = set(GOVERNANCE._snapshot)
        if enabled:
            current.discard(tool_name)
        else:
            current.add(tool_name)
        GOVERNANCE._store(frozenset(current))

    return {
        "scope_key": scope_key,
        "tool_name": tool_name,
        "enabled": bool(enabled),
        "reason": effective_reason,
        "updated_by": actor,
    }


async def get_disabled_tools_with_pg_hydration(
    redis_client: RedisClient | None,
    pg_pool: PgPool | None = None,
    *,
    scope_key: str = "tool",
    cache: ToolGovernanceCache | None = None,
) -> frozenset[str]:
    """Return disabled tools/engines, hydrating Redis from PG on cold start or FLUSHALL.

    Fail-closed: raises :class:`GovernanceUnavailable` within ``< 2.0s`` in
    production when both Redis and PostgreSQL are unavailable.
    """
    if scope_key not in _SCOPE_HASH_KEYS:
        raise ValueError(f"Unsupported governance scope_key: {scope_key!r}")
    hash_key = _SCOPE_HASH_KEYS[scope_key]
    target_cache = cache or GOVERNANCE

    redis_exc: Exception | None = None
    if redis_client is not None:
        try:

            async def _read_redis() -> tuple[bool, frozenset[str] | None]:
                get_fn = getattr(redis_client, "get", None)
                sentinel_ok = True
                if pg_pool is not None and callable(get_fn):
                    sentinel_ok = bool(await _maybe_await(get_fn(_HYDRATED_SENTINEL_KEY)))
                if not sentinel_ok:
                    return False, None
                names = await ToolGovernanceCache._fetch(redis_client, hash_key=hash_key)
                return True, names

            sentinel_ok, redis_names = await asyncio.wait_for(
                _read_redis(),
                timeout=_GOVERNANCE_OP_TIMEOUT_SEC,
            )
            if sentinel_ok and redis_names is not None:
                if scope_key == "tool":
                    target_cache._store(redis_names)
                return redis_names
        except Exception as exc:  # noqa: BLE001 - timeout/connection fallback
            redis_exc = exc

    # Redis sentinel missed (cold boot or FLUSHALL), redis_client is None, or Redis failed.
    if pg_pool is not None:
        try:
            by_scope = await _fetch_disabled_by_scope_from_pg(pg_pool)
            try:
                await _sync_redis_from_pg_snapshot(redis_client, by_scope)
            except Exception as sync_exc:  # noqa: BLE001 - PG already succeeded
                log.warning("Governance Redis write-back failed during hydration: %s", sync_exc)
            tool_set = frozenset(by_scope.get("tool", set()))
            target_cache._store(tool_set)
            return frozenset(by_scope.get(scope_key, set()))
        except Exception as pg_exc:  # noqa: BLE001
            GOVERNANCE_DEGRADED_TOTAL.inc()
            if _is_prod_mode():
                raise GovernanceUnavailable(
                    f"governance registry unavailable (redis={redis_exc}, pg={pg_exc})"
                ) from pg_exc
            if (
                scope_key == "tool"
                and target_cache.initialized
                and target_cache._snapshot is not None
                and target_cache._age() <= cfg.NCE_TOOL_GOVERNANCE_STALE_HARD_SEC
            ):
                return target_cache._snapshot
            return frozenset()

    # pg_pool is None and Redis failed or was absent: apply strict fail-closed posture.
    GOVERNANCE_DEGRADED_TOTAL.inc()
    if _is_prod_mode():
        if (
            scope_key == "tool"
            and target_cache.initialized
            and target_cache._snapshot is not None
            and target_cache._age() <= cfg.NCE_TOOL_GOVERNANCE_STALE_HARD_SEC
        ):
            return target_cache._snapshot
        raise GovernanceUnavailable(
            f"governance registry unavailable (redis={redis_exc}, pg_pool=None)"
        )
    if (
        scope_key == "tool"
        and target_cache.initialized
        and target_cache._snapshot is not None
        and target_cache._age() <= cfg.NCE_TOOL_GOVERNANCE_STALE_HARD_SEC
    ):
        return target_cache._snapshot
    return frozenset()


async def get_disabled_engines_with_pg_hydration(
    redis_client: RedisClient | None,
    pg_pool: PgPool | None = None,
) -> frozenset[str]:
    """Convenience wrapper returning disabled engine names with PG hydration."""
    return await get_disabled_tools_with_pg_hydration(
        redis_client,
        pg_pool=pg_pool,
        scope_key="engine",
    )
