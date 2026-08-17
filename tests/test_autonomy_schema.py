"""Tests for C2 autonomy schema verification (Wave 14).

Unit tests (no live DB required)
---------------------------------
These use a lightweight stub that replaces asyncpg.Connection.fetch() so the
failure paths of check_autonomy_schema() can be exercised deterministically
without a running Postgres instance.  They are plain pytest tests — no mark,
no skip.

Integration tests (DB required)
--------------------------------
Asserts that migration 022_muscles_schema_contract.sql has been applied and
that check_autonomy_schema() returns cleanly against a migrated database.

Three tables are verified:
  - action_approval_queue   (key column: status)
  - action_idempotency      (key column: idempotency_key)
                            + PRIMARY KEY/UNIQUE on (namespace_id, idempotency_key)
  - processed_outbox_events (key column: event_id)

DB-dependent tests carry ``@pytest.mark.integration`` and skip automatically
when no Postgres DSN is available.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio  # noqa: F401  # ensures asyncio fixtures are loaded

from nce.autonomy.schema_check import _REQUIRED, check_autonomy_schema

# ---------------------------------------------------------------------------
# Stub helpers for unit tests
# ---------------------------------------------------------------------------


def _make_col_record(table: str, column: str) -> dict[str, str]:
    """Minimal mapping that mimics an asyncpg.Record for information_schema.columns."""
    return {"table_name": table, "column_name": column}


def _make_constraint_record(constraint_name: str, column_name: str) -> dict[str, str]:
    """Minimal mapping that mimics an asyncpg.Record for the constraint query."""
    return {"constraint_name": constraint_name, "column_name": column_name}


def _full_col_rows() -> list[dict[str, str]]:
    """Return all (table, column) rows that would satisfy the column check."""
    rows: list[dict[str, str]] = []
    # Include all key columns AND namespace_id so the constraint check has
    # something to work with even if we vary the constraint rows separately.
    for table, key_col in _REQUIRED.items():
        rows.append(_make_col_record(table, key_col))
        # Extra columns for realistic coverage (namespace_id is needed by constraint check).
        rows.append(_make_col_record(table, "namespace_id"))
    return rows


def _good_constraint_rows() -> list[dict[str, str]]:
    """Constraint rows that satisfy the (namespace_id, idempotency_key) PK check."""
    return [
        _make_constraint_record("action_idempotency_pkey", "namespace_id"),
        _make_constraint_record("action_idempotency_pkey", "idempotency_key"),
    ]


class _StubConn:
    """Minimal asyncpg.Connection stub; fetch() returns pre-canned rows."""

    def __init__(
        self,
        col_rows: list[dict[str, Any]],
        constraint_rows: list[dict[str, Any]],
    ) -> None:
        # fetch is called twice: first for column check, then for constraint check.
        self._fetch = AsyncMock(side_effect=[col_rows, constraint_rows])

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return await self._fetch(query, *args)


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no live DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unit_raises_when_table_absent() -> None:
    """Raises RuntimeError when a required table is completely absent from columns."""
    # Return empty list — no tables at all.
    conn = _StubConn(col_rows=[], constraint_rows=[])
    with pytest.raises(RuntimeError, match="Required autonomy table"):
        await check_autonomy_schema(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unit_raises_when_key_column_absent() -> None:
    """Raises RuntimeError when the table exists but the key column is missing."""
    # Provide the table with an unrelated column — no key column.
    col_rows = [_make_col_record("action_approval_queue", "some_other_col")]
    conn = _StubConn(col_rows=col_rows, constraint_rows=[])
    with pytest.raises(RuntimeError, match="Required column 'status'"):
        await check_autonomy_schema(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unit_raises_when_constraint_absent() -> None:
    """Raises RuntimeError when columns pass but the idempotency PK/UNIQUE is absent."""
    # All columns present, but no constraints returned.
    conn = _StubConn(col_rows=_full_col_rows(), constraint_rows=[])
    with pytest.raises(RuntimeError, match="PRIMARY KEY or UNIQUE constraint"):
        await check_autonomy_schema(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unit_raises_when_constraint_covers_only_one_col() -> None:
    """Raises RuntimeError when constraint covers only one of the two required columns."""
    # Constraint exists but covers only idempotency_key, not namespace_id.
    partial_constraints = [
        _make_constraint_record("action_idempotency_partial_uq", "idempotency_key"),
    ]
    conn = _StubConn(col_rows=_full_col_rows(), constraint_rows=partial_constraints)
    with pytest.raises(RuntimeError, match="PRIMARY KEY or UNIQUE constraint"):
        await check_autonomy_schema(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unit_passes_when_schema_is_correct() -> None:
    """Does not raise when all columns and the idempotency constraint are present."""
    conn = _StubConn(col_rows=_full_col_rows(), constraint_rows=_good_constraint_rows())
    await check_autonomy_schema(conn)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unit_passes_with_unique_constraint_instead_of_pk() -> None:
    """A UNIQUE constraint (not PK) covering both cols is also acceptable."""
    unique_constraints = [
        _make_constraint_record("action_idempotency_ns_key_uq", "namespace_id"),
        _make_constraint_record("action_idempotency_ns_key_uq", "idempotency_key"),
    ]
    conn = _StubConn(col_rows=_full_col_rows(), constraint_rows=unique_constraints)
    await check_autonomy_schema(conn)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    """Return True when ``public.<table>.<column>`` is present in information_schema."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.columns
                WHERE  table_schema = 'public'
                  AND  table_name   = $1
                  AND  column_name  = $2
            )
            """,
            table,
            column,
        )
    )


# ---------------------------------------------------------------------------
# Integration tests — require a migrated DB (pg_pool fixture)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_approval_queue_exists(pg_pool: asyncpg.Pool) -> None:
    """action_approval_queue table and its 'status' key column are present."""
    async with pg_pool.acquire() as conn:
        assert await _column_exists(conn, "action_approval_queue", "status"), (
            "public.action_approval_queue.status absent — migration 022 not applied"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_idempotency_exists(pg_pool: asyncpg.Pool) -> None:
    """action_idempotency table and its 'idempotency_key' key column are present."""
    async with pg_pool.acquire() as conn:
        assert await _column_exists(conn, "action_idempotency", "idempotency_key"), (
            "public.action_idempotency.idempotency_key absent — migration 022 not applied"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_processed_outbox_events_exists(pg_pool: asyncpg.Pool) -> None:
    """processed_outbox_events table and its 'event_id' PK column are present."""
    async with pg_pool.acquire() as conn:
        assert await _column_exists(conn, "processed_outbox_events", "event_id"), (
            "public.processed_outbox_events.event_id absent — migration 022 not applied"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_autonomy_schema_returns_cleanly(pg_pool: asyncpg.Pool) -> None:
    """check_autonomy_schema() does not raise when all three tables are present."""
    async with pg_pool.acquire() as conn:
        # Raises RuntimeError if any required table/column/constraint is absent.
        await check_autonomy_schema(conn)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_required_sentinels_covered(pg_pool: asyncpg.Pool) -> None:
    """Every (table, key_column) pair in _REQUIRED is reachable in the live schema.

    This test mirrors what check_autonomy_schema() validates but drives it
    via _REQUIRED directly so adding a new sentinel automatically extends
    coverage without changing test code.
    """
    async with pg_pool.acquire() as conn:
        for table, key_column in _REQUIRED.items():
            ok = await _column_exists(conn, table, key_column)
            assert ok, (
                f"public.{table}.{key_column} missing from live schema — "
                "migration 022 has not been applied"
            )
