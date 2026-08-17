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

import logging
import time
from typing import Any

from nce.config import cfg
from nce.observability import _safe_counter

# ``engine.redis_client`` is an ``redis.asyncio.Redis`` at runtime but is held
# untyped (``Any``) across the package; redis-py's ``hkeys`` overload reports a
# sync/async union return that does not type-check under ``await``. Mirror the
# rest of the package and treat the client as ``Any`` at this boundary.
RedisClient = Any

log = logging.getLogger("nce-governance")

_DISABLED_HASH_KEY = "nce:tools:disabled"

# Bumped whenever the cache serves a degraded decision (hard-stale fail-closed,
# never-initialized prod block, or never-initialized dev allow).
GOVERNANCE_DEGRADED_TOTAL = _safe_counter(
    "nce_tool_governance_degraded_total",
    "Total governance decisions served in a degraded mode "
    "(hard-stale fail-closed, or never-initialized boot before first fetch).",
)


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
    async def _fetch(redis_client: RedisClient) -> frozenset[str]:
        """Read the disabled-name set from Redis (``hkeys`` of the hash)."""
        raw = await redis_client.hkeys(_DISABLED_HASH_KEY)
        return frozenset(k.decode("utf-8") if isinstance(k, bytes) else str(k) for k in raw)

    # -- public API ---------------------------------------------------------

    async def warm(self, redis_client: RedisClient | None) -> bool:
        """Attempt the initial (or a refreshing) fetch — used at startup.

        Returns ``True`` when the snapshot was (re)loaded, ``False`` on failure
        (no Redis client, or Redis raised). On failure the cache stays in its
        current state and the never-initialized prod-block / next-call retry
        still applies; the caller should log and continue booting.
        """
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

    async def is_disabled(self, redis_client: RedisClient | None, name: str) -> bool:
        """Return whether *name* is currently admin-disabled.

        Raises :class:`GovernanceUnavailable` when the safe action is to block
        dispatch (hard-stale or never-initialized-in-prod).
        """
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
        if cfg.IS_PROD:
            # Cold-boot un-revoke hole: must not allow without ever reading state.
            log.error("Governance never initialized in production; failing closed: %s", exc)
            raise GovernanceUnavailable("governance registry unavailable (never initialized)")
        # Dev / test convenience: allow so the suite / local boot is not blocked.
        log.warning("Governance never initialized (dev/test); allowing: %s", exc)
        return False


# Module singleton — both dispatch surfaces share this instance.
GOVERNANCE = ToolGovernanceCache()
