"""Migration 026: pg_trgm extension for fuzzy-string similarity matching."""

from __future__ import annotations

from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest

MIGRATION_PATH = Path("nce/migrations/026_pg_trgm_extension.sql")


def _migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


async def _extension_present(conn: asyncpg.Connection) -> bool:
    """Check if pg_trgm extension is present in the database."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_extension
                WHERE extname = 'pg_trgm'
            )
            """
        )
    )


# ---------------------------------------------------------------------------
# Unit — migration script contract (no Postgres required)
# ---------------------------------------------------------------------------


def test_migration_sql_contract() -> None:
    """Verify the migration file contains idempotent extension creation."""
    sql = _migration_sql()
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in sql


# ---------------------------------------------------------------------------
# Integration — extension presence and similarity() function
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_trgm_extension_present(
    pg_admin_conn: asyncpg.Connection,
) -> None:
    """Verify pg_trgm extension is present after migrations run."""
    assert await _extension_present(pg_admin_conn)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_trgm_similarity_function_callable(
    pg_admin_conn: asyncpg.Connection,
) -> None:
    """Verify similarity() function is callable and returns expected values."""
    # similarity() returns a float between 0 and 1.
    result = await pg_admin_conn.fetchval("SELECT similarity('Cisco', 'Cisco Systems')")
    assert result is not None
    assert isinstance(result, (int, float))
    assert 0 <= result <= 1

    # Identical strings should have similarity of 1.
    identical = await pg_admin_conn.fetchval("SELECT similarity('test', 'test')")
    assert identical == 1.0

    # Completely different strings should have low similarity.
    different = await pg_admin_conn.fetchval("SELECT similarity('abc', 'xyz')")
    assert different is not None
    assert 0 <= different < 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_025_idempotent(
    pg_admin_conn: asyncpg.Connection,
) -> None:
    """Verify migration can be applied multiple times without error."""
    await pg_admin_conn.execute(_migration_sql())
    await pg_admin_conn.execute(_migration_sql())

    assert await _extension_present(pg_admin_conn)
