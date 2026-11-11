"""Contract tests for bounded pool checkout and transactional RLS (FIX-010 / FIX-011)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.db_utils import POOL_ACQUIRE_TIMEOUT, scoped_pg_session, unmanaged_pg_connection


@pytest.mark.asyncio
async def test_unmanaged_pg_connection_passes_acquire_timeout() -> None:
    captured: dict[str, float | None] = {}
    conn = AsyncMock()
    ac = AsyncMock()
    ac.__aenter__.return_value = conn
    ac.__aexit__.return_value = None

    def acquire(**kwargs: object) -> AsyncMock:
        if "timeout" in kwargs:
            captured["timeout"] = kwargs["timeout"]  # type: ignore[assignment]
        return ac

    pool = MagicMock()
    pool.acquire = acquire

    async with unmanaged_pg_connection(pool, site="tasks.code_indexing.legacy_no_namespace"):
        pass

    assert captured.get("timeout") == POOL_ACQUIRE_TIMEOUT


@pytest.mark.asyncio
async def test_scoped_pg_session_passes_acquire_timeout() -> None:
    captured: dict[str, float | None] = {}
    ns = str(uuid4())

    conn = AsyncMock()
    tx = AsyncMock()
    tx.__aenter__.return_value = None
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)

    ac = AsyncMock()
    ac.__aenter__.return_value = conn
    ac.__aexit__.return_value = None

    def acquire(**kwargs: object) -> AsyncMock:
        if "timeout" in kwargs:
            captured["timeout"] = kwargs["timeout"]  # type: ignore[assignment]
        return ac

    pool = MagicMock()
    pool.acquire = acquire

    with patch("nce.auth.set_namespace_context", new_callable=AsyncMock):
        with patch("nce.auth._reset_rls_context", new_callable=AsyncMock):
            async with scoped_pg_session(pool, ns):
                pass

    assert captured.get("timeout") == POOL_ACQUIRE_TIMEOUT


@pytest.mark.asyncio
async def test_scoped_pg_session_opens_transaction_before_namespace_and_body() -> None:
    """SET LOCAL only applies inside a transaction — transaction must wrap RLS + user work."""
    ns = str(uuid4())
    order: list[str] = []

    conn = AsyncMock()
    tx = AsyncMock()

    async def tx_enter() -> None:
        order.append("tx_enter")

    tx.__aenter__ = AsyncMock(side_effect=tx_enter)
    tx.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=tx)

    ac = AsyncMock()
    ac.__aenter__.return_value = conn
    ac.__aexit__.return_value = None
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ac)

    async def set_ns(_c: object, _nid: object) -> None:
        order.append("set_ns")

    async def reset(_c: object) -> None:
        order.append("reset")

    with patch("nce.auth.set_namespace_context", set_ns):
        with patch("nce.auth._reset_rls_context", reset):
            async with scoped_pg_session(pool, ns) as c:
                assert c is conn
                order.append("body")

    # SET LOCAL is cleared at transaction end — scoped_pg_session does not reset RLS.
    assert order == ["tx_enter", "set_ns", "body"]


@pytest.mark.asyncio
async def test_set_namespace_context_requires_active_transaction() -> None:
    from nce.db_utils import set_namespace_context

    conn = MagicMock()
    conn.is_in_transaction = MagicMock(return_value=False)
    conn.execute = AsyncMock()

    ns = uuid4()
    with pytest.raises(
        RuntimeError,
        match="SECURITY VIOLATION: set_namespace_context requires an active SQL transaction",
    ):
        await set_namespace_context(conn, ns)

    # Active transaction passes
    conn.is_in_transaction.return_value = True
    await set_namespace_context(conn, ns)
    conn.execute.assert_called_once_with("SELECT set_config('nce.namespace_id', $1, true)", str(ns))

    # Test mock without is_in_transaction also passes (backward compat with untyped mock)
    legacy_mock = MagicMock(spec=[])
    legacy_mock.execute = AsyncMock()
    await set_namespace_context(legacy_mock, ns)
    legacy_mock.execute.assert_called_once_with(
        "SELECT set_config('nce.namespace_id', $1, true)", str(ns)
    )


def test_validate_redis_namespace_key():
    from nce.db_utils import validate_redis_namespace_key

    ns = uuid4()
    ns_str = str(ns)

    # Valid simple subkey
    assert validate_redis_namespace_key(ns, "cache:user") == f"nce:ns:{ns_str}:cache:user"
    assert validate_redis_namespace_key(ns_str, "cache:user") == f"nce:ns:{ns_str}:cache:user"

    # Already fully-qualified key under same namespace
    full_key = f"nce:ns:{ns_str}:cache:user"
    assert validate_redis_namespace_key(ns, full_key) == full_key

    # Cross-tenant key attempts
    other_ns = uuid4()
    other_key = f"nce:ns:{other_ns}:cache:user"
    with pytest.raises(ValueError, match="Cross-tenant access forbidden"):
        validate_redis_namespace_key(ns, other_key)

    # Cross-namespace wildcard attempts
    with pytest.raises(ValueError, match="forbidden"):
        validate_redis_namespace_key(ns, "nce:ns:*:keys")
    with pytest.raises(ValueError, match="forbidden"):
        validate_redis_namespace_key(ns, "*")
    with pytest.raises(ValueError, match="forbidden"):
        validate_redis_namespace_key(ns, "nce:*")

    # Invalid namespace
    with pytest.raises(ValueError):
        validate_redis_namespace_key("", "key")
    with pytest.raises(ValueError):
        validate_redis_namespace_key("not-a-uuid", "key")


def test_scoped_redis_client():
    from nce.db_utils import ScopedRedisClient

    mock_raw_redis = MagicMock()
    mock_raw_redis.get.return_value = "val"
    mock_raw_redis.hgetall.return_value = {"field": "1"}

    ns = uuid4()
    client = ScopedRedisClient(mock_raw_redis, ns)

    assert client.get("mykey") == "val"
    mock_raw_redis.get.assert_called_once_with(f"nce:ns:{ns}:mykey")

    client.hgetall("hashkey")
    mock_raw_redis.hgetall.assert_called_once_with(f"nce:ns:{ns}:hashkey")

    # Cross-tenant access via client
    with pytest.raises(ValueError):
        client.get(f"nce:ns:{uuid4()}:stolen")
