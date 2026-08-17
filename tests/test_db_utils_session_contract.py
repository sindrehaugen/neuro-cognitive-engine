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
