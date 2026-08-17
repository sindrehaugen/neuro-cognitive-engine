"""Migration 003: resource_quotas used_amount lower-bound enforcement."""

from __future__ import annotations

import uuid
from pathlib import Path

import asyncpg
import pytest

MIGRATION_PATH = Path("nce/migrations/003_quota_check.sql")


def _migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


async def _require_resource_quotas(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'resource_quotas'
        )
        """
    )
    if not exists:
        pytest.skip("resource_quotas table missing — apply nce/schema.sql first")


async def _drop_nonnegative_used_amount_checks(conn: asyncpg.Connection) -> list[str]:
    """Drop CHECK constraints that enforce used_amount >= 0 (test setup only)."""

    rows = await conn.fetch(
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'resource_quotas'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ~ 'used_amount\\s*>=\\s*0'
        """
    )
    dropped = [r["conname"] for r in rows]
    for name in dropped:
        await conn.execute(f'ALTER TABLE resource_quotas DROP CONSTRAINT "{name}"')
    return dropped


async def _constraint_present(conn: asyncpg.Connection) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'resource_quotas'::regclass
                  AND conname = 'chk_resource_quotas_used_amount_nonnegative'
            )
            """
        )
    )


# ---------------------------------------------------------------------------
# Unit — migration script contract (no Postgres required)
# ---------------------------------------------------------------------------


def test_migration_sql_contract() -> None:
    sql = _migration_sql()
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "NOT VALID" in sql
    assert "VALIDATE CONSTRAINT chk_resource_quotas_used_amount_nonnegative" in sql
    assert "UPDATE resource_quotas" in sql and "used_amount = 0" in sql
    assert "WHERE used_amount < 0" in sql
    assert "RAISE EXCEPTION" in sql
    assert "chk_resource_quotas_used_amount_nonnegative" in sql


# ---------------------------------------------------------------------------
# Integration — repair, validate, enforce
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_003_repairs_negative_rows_and_enforces(
    pg_admin_conn: asyncpg.Connection,
    namespace_id: uuid.UUID,
) -> None:
    await _require_resource_quotas(pg_admin_conn)

    resource_type = f"pytest-003-{uuid.uuid4().hex[:8]}"
    await _drop_nonnegative_used_amount_checks(pg_admin_conn)

    row_id = await pg_admin_conn.fetchval(
        """
        INSERT INTO resource_quotas (
            namespace_id, resource_type, limit_amount, used_amount
        )
        VALUES ($1, $2, 100, -7)
        RETURNING id
        """,
        namespace_id,
        resource_type,
    )
    assert row_id is not None

    try:
        await pg_admin_conn.execute(_migration_sql())

        used = await pg_admin_conn.fetchval(
            "SELECT used_amount FROM resource_quotas WHERE id = $1",
            row_id,
        )
        assert used == 0
        assert await _constraint_present(pg_admin_conn)

        with pytest.raises(asyncpg.CheckViolationError):
            await pg_admin_conn.execute(
                """
                UPDATE resource_quotas
                SET used_amount = -1
                WHERE id = $1
                """,
                row_id,
            )
    finally:
        await pg_admin_conn.execute(
            "DELETE FROM resource_quotas WHERE id = $1",
            row_id,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_003_idempotent_second_run(
    pg_admin_conn: asyncpg.Connection,
) -> None:
    await _require_resource_quotas(pg_admin_conn)

    await pg_admin_conn.execute(_migration_sql())
    await pg_admin_conn.execute(_migration_sql())

    assert await _constraint_present(pg_admin_conn)

    negative = await pg_admin_conn.fetchval(
        "SELECT count(*) FROM resource_quotas WHERE used_amount < 0"
    )
    assert negative == 0
