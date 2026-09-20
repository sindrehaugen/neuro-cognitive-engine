"""tests/unit/test_nettailer_source_guard.py

Unit tests for require_nettailer_source_enabled (Q-44 follow-up): a
per-source opt-in guard, deliberately narrower than require_product_enabled
so enabling Product doesn't implicitly enable every feed source. See
QUESTIONS_CHARTER.md's Q-44 closure note and
nce/vertical_modules/product/_guard.py's module docstring for why this is a
sibling function, not a reuse of the engine-wide guard.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncpg.exceptions import DataError as _PgDataError

from nce.vertical_modules.product._guard import (
    NettailerSourceDisabledError,
    require_nettailer_source_enabled,
)


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_pool(*, enabled: bool | None) -> MagicMock:
    """A pool whose namespaces-check connection mimics the real query.

    ``enabled=None`` mimics no matching row (unknown namespace).
    """

    async def _fetchrow(_query: str, namespace_id: str) -> dict[str, Any] | None:
        try:
            uuid.UUID(str(namespace_id))
        except ValueError as exc:
            raise _PgDataError(f"invalid input syntax for type uuid: {namespace_id!r}") from exc
        if enabled is None:
            return None
        return {"nettailer_enabled": enabled}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool


@pytest.mark.asyncio
async def test_enabled_namespace_passes_silently() -> None:
    """The valid case must still pass -- an unconditional refusal would
    satisfy the other tests here without actually checking anything."""
    pool = _make_pool(enabled=True)
    await require_nettailer_source_enabled(pool, str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_disabled_namespace_raises() -> None:
    pool = _make_pool(enabled=False)
    with pytest.raises(NettailerSourceDisabledError, match="not enabled"):
        await require_nettailer_source_enabled(pool, str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_unknown_namespace_raises() -> None:
    pool = _make_pool(enabled=None)
    with pytest.raises(NettailerSourceDisabledError):
        await require_nettailer_source_enabled(pool, str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_malformed_namespace_id_fails_closed_not_uncaught() -> None:
    """A DataError from asyncpg's own ::uuid cast must translate to the
    structured refusal, not escape as an unstructured driver exception."""
    pool = _make_pool(enabled=True)
    with pytest.raises(NettailerSourceDisabledError):
        await require_nettailer_source_enabled(pool, "not-a-uuid")


@pytest.mark.asyncio
async def test_enabling_product_does_not_implicitly_enable_nettailer() -> None:
    """The whole reason this is a separate function from
    require_product_enabled: a namespace with Product enabled but Nettailer
    not opted in must still be refused by this guard."""
    # `enabled=False` here stands in for "product.enabled=true but
    # product.sources.nettailer is unset/false" -- this guard only ever
    # reads the nettailer key, so it cannot see or be satisfied by the
    # product-level flag at all.
    pool = _make_pool(enabled=False)
    with pytest.raises(NettailerSourceDisabledError):
        await require_nettailer_source_enabled(pool, str(uuid.uuid4()))
