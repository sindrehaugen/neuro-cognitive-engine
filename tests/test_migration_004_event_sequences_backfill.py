"""Migration 004: event_sequences backfill from event_log (FIX-068)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

MIGRATION_PATH = Path("nce/migrations/004_event_sequences_backfill.sql")


def _migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


async def _require_event_tables(conn: asyncpg.Connection) -> None:
    for table in ("event_log", "event_sequences"):
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = $1
            )
            """,
            table,
        )
        if not exists:
            pytest.skip(f"{table} missing — apply nce/schema.sql first")


async def _set_sequence_stale(conn: asyncpg.Connection, namespace_id: uuid.UUID, seq: int) -> None:
    await conn.execute(
        """
        INSERT INTO event_sequences (namespace_id, seq)
        VALUES ($1, $2)
        ON CONFLICT (namespace_id) DO UPDATE SET seq = EXCLUDED.seq
        """,
        namespace_id,
        seq,
    )


async def _insert_event_row(
    conn: asyncpg.Connection,
    *,
    namespace_id: uuid.UUID,
    event_seq: int,
    occurred_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO event_log (
            id, namespace_id, agent_id, event_type, event_seq,
            occurred_at, params, signature, signature_key_id
        )
        VALUES (
            gen_random_uuid(), $1, 'pytest-agent', 'store_memory', $2,
            $3, '{}'::jsonb, decode(repeat('ab', 32), 'hex'), 'pytest-key'
        )
        """,
        namespace_id,
        event_seq,
        occurred_at,
    )


async def _max_log_seq(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> int | None:
    return await conn.fetchval(
        """
        SELECT MAX(event_seq)::bigint
        FROM event_log
        WHERE namespace_id = $1
        """,
        namespace_id,
    )


async def _counter_seq(conn: asyncpg.Connection, namespace_id: uuid.UUID) -> int | None:
    return await conn.fetchval(
        "SELECT seq FROM event_sequences WHERE namespace_id = $1",
        namespace_id,
    )


# ---------------------------------------------------------------------------
# Unit — migration script contract (no Postgres required)
# ---------------------------------------------------------------------------


def test_migration_sql_contract() -> None:
    sql = _migration_sql()
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "LOCK TABLE public.event_log IN SHARE MODE" in sql
    assert "LOCK TABLE public.event_sequences IN EXCLUSIVE MODE" in sql
    assert "WHERE  event_seq IS NOT NULL" in sql
    assert "GREATEST(public.event_sequences.seq, EXCLUDED.seq)" in sql
    assert "RAISE EXCEPTION" in sql
    assert "RAISE NOTICE" in sql
    assert "FIX-068" in sql


# ---------------------------------------------------------------------------
# Integration — backfill alignment and idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_004_aligns_stale_counter_with_event_log(
    pg_admin_conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> None:
    await _require_event_tables(pg_admin_conn)

    occurred_at = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    await _insert_event_row(
        pg_admin_conn,
        namespace_id=namespace_id,
        event_seq=10,
        occurred_at=occurred_at,
    )
    await _insert_event_row(
        pg_admin_conn,
        namespace_id=namespace_id,
        event_seq=42,
        occurred_at=occurred_at.replace(hour=13),
    )
    await _set_sequence_stale(pg_admin_conn, namespace_id, seq=3)

    await pg_admin_conn.execute(_migration_sql())

    assert await _max_log_seq(pg_admin_conn, namespace_id) == 42
    assert await _counter_seq(pg_admin_conn, namespace_id) == 42


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_004_never_lowers_counter(
    pg_admin_conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> None:
    await _require_event_tables(pg_admin_conn)

    occurred_at = datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc)
    await _insert_event_row(
        pg_admin_conn,
        namespace_id=namespace_id,
        event_seq=5,
        occurred_at=occurred_at,
    )
    await _set_sequence_stale(pg_admin_conn, namespace_id, seq=99)

    await pg_admin_conn.execute(_migration_sql())

    assert await _counter_seq(pg_admin_conn, namespace_id) == 99


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_004_creates_counter_when_row_missing(
    pg_admin_conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> None:
    await _require_event_tables(pg_admin_conn)

    await pg_admin_conn.execute(
        "DELETE FROM event_sequences WHERE namespace_id = $1",
        namespace_id,
    )
    occurred_at = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
    await _insert_event_row(
        pg_admin_conn,
        namespace_id=namespace_id,
        event_seq=11,
        occurred_at=occurred_at,
    )

    await pg_admin_conn.execute(_migration_sql())

    assert await _counter_seq(pg_admin_conn, namespace_id) == 11


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_004_idempotent_second_run(
    pg_admin_conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> None:
    await _require_event_tables(pg_admin_conn)

    occurred_at = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    await _insert_event_row(
        pg_admin_conn,
        namespace_id=namespace_id,
        event_seq=7,
        occurred_at=occurred_at,
    )
    await _set_sequence_stale(pg_admin_conn, namespace_id, seq=1)

    await pg_admin_conn.execute(_migration_sql())
    first = await _counter_seq(pg_admin_conn, namespace_id)

    await pg_admin_conn.execute(_migration_sql())
    second = await _counter_seq(pg_admin_conn, namespace_id)

    assert first == 7
    assert second == 7
