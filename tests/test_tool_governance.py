"""Unit tests for the last-known-good tool governance cache (Batch 100).

Audit Domain 1 (CWE-636 / CWE-1188). Pure-unit, mocked Redis — no Docker.

Covers all three states of ``ToolGovernanceCache``:

  (a) disabled name → blocked
  (b) Redis raises within STALE_OK → last snapshot still enforced
  (c) Redis raises past STALE_HARD → GovernanceUnavailable + degraded counter
  (d) re-enable propagates within STALE_OK after recovery
  (e) NEVER-INITIALIZED + IS_PROD=True  → GovernanceUnavailable (blocked) + counter
  (f) NEVER-INITIALIZED + IS_PROD=False → ALLOW + counter
  (g) INITIALIZED-EMPTY (fetched OK, empty set) → ALLOW

No ``importlib.reload`` — ``cfg`` is monkeypatched directly per the batch brief.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nce.config import cfg
from nce.tool_governance import (
    GOVERNANCE_DEGRADED_TOTAL,
    GovernanceUnavailable,
    ToolGovernanceCache,
)


def _redis(disabled: list[str]) -> AsyncMock:
    """Mock Redis whose ``hkeys`` returns the given disabled names (as bytes)."""
    r = AsyncMock()
    r.hkeys = AsyncMock(return_value=[k.encode("utf-8") for k in disabled])
    return r


def _failing_redis(exc: Exception | None = None) -> AsyncMock:
    r = AsyncMock()
    r.hkeys = AsyncMock(side_effect=exc or RuntimeError("Redis connection lost"))
    return r


def _counter_value() -> float:
    """Current value of the degraded counter (0.0 when prometheus is stubbed)."""
    try:
        return GOVERNANCE_DEGRADED_TOTAL._value.get()  # type: ignore[attr-defined]
    except Exception:
        return 0.0


@pytest.fixture
def cache() -> ToolGovernanceCache:
    """A fresh, isolated cache instance (never-initialized) per test."""
    return ToolGovernanceCache()


@pytest.fixture(autouse=True)
def _governance_stale_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic staleness windows; restored by monkeypatch teardown."""
    monkeypatch.setattr(cfg, "NCE_TOOL_GOVERNANCE_STALE_OK_SEC", 30, raising=False)
    monkeypatch.setattr(cfg, "NCE_TOOL_GOVERNANCE_STALE_HARD_SEC", 300, raising=False)


# ---------------------------------------------------------------------------
# (a) disabled name → blocked (INITIALIZED, fresh fetch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_name_is_blocked(cache: ToolGovernanceCache) -> None:
    redis = _redis(["store_memory"])
    assert await cache.is_disabled(redis, "store_memory") is True
    # An enabled name in the same fetched snapshot is allowed.
    assert await cache.is_disabled(redis, "search_codebase") is False
    assert cache.initialized is True


# ---------------------------------------------------------------------------
# (b) Redis raises within STALE_OK → last snapshot still enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_within_stale_ok_serves_last_snapshot(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First, a good fetch records the disabled set.
    assert await cache.is_disabled(_redis(["store_memory"]), "store_memory") is True

    # Within STALE_OK the cache must not even call Redis — a previously disabled
    # tool stays blocked. Use a redis that would raise if it were consulted.
    redis = _failing_redis()
    assert await cache.is_disabled(redis, "store_memory") is True
    redis.hkeys.assert_not_awaited()  # served straight from the fresh snapshot


@pytest.mark.asyncio
async def test_stale_ok_with_redis_error_keeps_blocking(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Good fetch, then age past STALE_OK but within STALE_HARD with Redis down.
    assert await cache.is_disabled(_redis(["store_memory"]), "store_memory") is True

    base = cache._fetched_at or 0.0
    # 60s old: past STALE_OK (30) → refresh attempted; Redis raises → keep snapshot.
    monkeypatch.setattr("nce.tool_governance.time.monotonic", lambda: base + 60.0)

    before = _counter_value()
    assert await cache.is_disabled(_failing_redis(), "store_memory") is True
    # Degraded counter MUST NOT increment while still within the hard window.
    assert _counter_value() == before


# ---------------------------------------------------------------------------
# (c) Redis raises past STALE_HARD → GovernanceUnavailable + counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_stale_hard_fails_closed_and_counts(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await cache.is_disabled(_redis(["store_memory"]), "store_memory") is True

    base = cache._fetched_at or 0.0
    # 301s old: past STALE_HARD (300) → fail closed regardless of name.
    monkeypatch.setattr("nce.tool_governance.time.monotonic", lambda: base + 301.0)

    before = _counter_value()
    with pytest.raises(GovernanceUnavailable):
        await cache.is_disabled(_failing_redis(), "an_enabled_tool")
    assert _counter_value() == before + 1.0


# ---------------------------------------------------------------------------
# (d) re-enable propagates within STALE_OK after recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reenable_propagates_after_recovery(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Initially disabled.
    assert await cache.is_disabled(_redis(["store_memory"]), "store_memory") is True

    base = cache._fetched_at or 0.0
    # Advance past STALE_OK so the next call refreshes from Redis...
    monkeypatch.setattr("nce.tool_governance.time.monotonic", lambda: base + 31.0)

    # ...Redis is now reachable and the admin has re-enabled the tool (empty set).
    assert await cache.is_disabled(_redis([]), "store_memory") is False
    # And the refreshed snapshot is served fresh on the subsequent call.
    assert await cache.is_disabled(_failing_redis(), "store_memory") is False


# ---------------------------------------------------------------------------
# (e) NEVER-INITIALIZED + IS_PROD=True → GovernanceUnavailable + counter
#     (closes the cold-boot un-revoke hole)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_initialized_in_prod_fails_closed(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "IS_PROD", True)

    before = _counter_value()
    # Redis unreachable at boot → no snapshot ever read → must block, not allow.
    with pytest.raises(GovernanceUnavailable):
        await cache.is_disabled(_failing_redis(), "store_memory")
    assert _counter_value() == before + 1.0
    assert cache.initialized is False


@pytest.mark.asyncio
async def test_never_initialized_in_prod_no_redis_client_fails_closed(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "IS_PROD", True)
    before = _counter_value()
    with pytest.raises(GovernanceUnavailable):
        await cache.is_disabled(None, "store_memory")
    assert _counter_value() == before + 1.0


# ---------------------------------------------------------------------------
# (f) NEVER-INITIALIZED + IS_PROD=False → ALLOW + counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_initialized_in_dev_allows_and_counts(
    cache: ToolGovernanceCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg, "IS_PROD", False)

    before = _counter_value()
    # Dev convenience: allow so the suite/local boot is not blocked.
    assert await cache.is_disabled(_failing_redis(), "store_memory") is False
    assert _counter_value() == before + 1.0
    assert cache.initialized is False


# ---------------------------------------------------------------------------
# (g) INITIALIZED-EMPTY (fetched OK, empty set) → ALLOW
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialized_empty_allows(cache: ToolGovernanceCache) -> None:
    # A successful fetch returning an empty hash means "nothing disabled".
    assert await cache.is_disabled(_redis([]), "store_memory") is False
    assert cache.initialized is True

    # Even with Redis down afterwards, an initialized-empty snapshot allows
    # within STALE_OK (no Redis call) — it is NOT the never-initialized state.
    redis = _failing_redis()
    assert await cache.is_disabled(redis, "store_memory") is False
    redis.hkeys.assert_not_awaited()


# ---------------------------------------------------------------------------
# warm() — startup wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_initializes_cache(cache: ToolGovernanceCache) -> None:
    assert cache.initialized is False
    assert await cache.warm(_redis(["store_memory"])) is True
    assert cache.initialized is True
    # Subsequent check served from the warmed snapshot (no Redis call needed).
    assert await cache.is_disabled(_failing_redis(), "store_memory") is True


@pytest.mark.asyncio
async def test_warm_failure_leaves_never_initialized(cache: ToolGovernanceCache) -> None:
    assert await cache.warm(_failing_redis()) is False
    assert cache.initialized is False


@pytest.mark.asyncio
async def test_warm_without_redis_client_is_noop(cache: ToolGovernanceCache) -> None:
    assert await cache.warm(None) is False
    assert cache.initialized is False


@pytest.mark.asyncio
async def test_warm_after_failure_then_success_recovers(
    cache: ToolGovernanceCache,
) -> None:
    assert await cache.warm(_failing_redis()) is False
    assert await cache.warm(_redis(["store_memory"])) is True
    assert await cache.is_disabled(_failing_redis(), "store_memory") is True
