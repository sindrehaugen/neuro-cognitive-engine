"""
tests/integration/test_product_lifecycle_status_check.py
===========================================================
Live-Postgres verification of migration 106 (Q-49): product_catalog's
lifecycle_status becomes a closed, checked vocabulary
('coming' | 'active' | 'EOL' | 'EOS') instead of free text.

Two things proven here, not just asserted:
  1. A value outside the four is rejected at the database level
     (test_lifecycle_status_check_rejects_a_fifth_value) -- bypasses any
     Python-level validation entirely, a raw INSERT against the real
     constraint.
  2. Each of the four valid values is actually accepted
     (test_lifecycle_status_check_accepts_each_valid_value) -- the third
     test this estate's own standard requires: an unconditional reject
     would satisfy test 1 alone.

product_catalog is GLOBAL (no namespace_id, RLS disabled -- Sindre's ruling,
migration 064), so these tests use pg_pool directly, no namespace fixture.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration


def _unique_identity() -> tuple[str, str]:
    """A fresh (manufacturer, mfr_part_no) pair -- product_catalog's own
    UNIQUE identity -- so parallel test runs never collide."""
    suffix = uuid.uuid4().hex[:12]
    return f"TESTMFR-{suffix}", f"TESTPART-{suffix}"


async def _insert_product(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    *,
    lifecycle_status: str,
) -> None:
    manufacturer, mfr_part_no = _unique_identity()
    await conn.execute(
        """
        INSERT INTO product_catalog
            (manufacturer, mfr_part_no, product_source_id, lifecycle_status)
        VALUES ($1, $2, 'test-source', $3)
        """,
        manufacturer,
        mfr_part_no,
        lifecycle_status,
    )


@pytest.mark.asyncio
async def test_lifecycle_status_check_rejects_a_fifth_value(pg_pool: asyncpg.Pool) -> None:
    """A value outside the closed set must be refused at the storage level,
    not merely at whatever Python boundary happens to validate it first."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await _insert_product(conn, lifecycle_status="discontinued")


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_status", ["coming", "active", "EOL", "EOS"])
async def test_lifecycle_status_check_accepts_each_valid_value(
    pg_pool: asyncpg.Pool, valid_status: str
) -> None:
    """The valid case must still pass for every one of the four values --
    an unconditional CHECK-violation reject would satisfy the test above
    without the constraint actually being ('coming','active','EOL','EOS')."""
    manufacturer, mfr_part_no = _unique_identity()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO product_catalog
                (manufacturer, mfr_part_no, product_source_id, lifecycle_status)
            VALUES ($1, $2, 'test-source', $3)
            """,
            manufacturer,
            mfr_part_no,
            valid_status,
        )
        stored = await conn.fetchval(
            "SELECT lifecycle_status FROM product_catalog WHERE manufacturer = $1 AND mfr_part_no = $2",
            manufacturer,
            mfr_part_no,
        )
        assert stored == valid_status

        await conn.execute(
            "DELETE FROM product_catalog WHERE manufacturer = $1 AND mfr_part_no = $2",
            manufacturer,
            mfr_part_no,
        )


@pytest.mark.asyncio
async def test_lifecycle_status_default_is_active(pg_pool: asyncpg.Pool) -> None:
    """The column's own DEFAULT ('active', migration 031) is itself one of
    the four valid values -- omitting lifecycle_status entirely must still
    satisfy the new constraint, not just an explicit value."""
    manufacturer, mfr_part_no = _unique_identity()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO product_catalog (manufacturer, mfr_part_no, product_source_id)
            VALUES ($1, $2, 'test-source')
            """,
            manufacturer,
            mfr_part_no,
        )
        stored = await conn.fetchval(
            "SELECT lifecycle_status FROM product_catalog WHERE manufacturer = $1 AND mfr_part_no = $2",
            manufacturer,
            mfr_part_no,
        )
        assert stored == "active"

        await conn.execute(
            "DELETE FROM product_catalog WHERE manufacturer = $1 AND mfr_part_no = $2",
            manufacturer,
            mfr_part_no,
        )
