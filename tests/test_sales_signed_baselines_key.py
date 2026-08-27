"""Integration tests for sales_signed_baselines' surrogate key (migration 056).

``nce/schema.sql`` and migration 041 have declared ``id BIGSERIAL PRIMARY KEY``
since the table's first commit. Some databases nonetheless carried
``id uuid DEFAULT gen_random_uuid()`` -- created outside those declarations
before 041 landed. ``CREATE TABLE IF NOT EXISTS`` silently no-ops against such a
table, so the two forms coexisted, and 041's unconditional
``GRANT ... ON SEQUENCE sales_signed_baselines_id_seq`` aborted startup against
the uuid form because the sequence does not exist there.

  a. Catalog ratchet: the column is bigint and the sequence exists.
  b. End-to-end: recreate the uuid anomaly, run migration 056, and prove it
     converts while preserving every row in freeze order. Destructive by nature,
     so it runs only against a database where the table is empty (CI, a fresh
     probe DB) and rolls back regardless.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest  # type: ignore[import-untyped]
import pytest_asyncio  # type: ignore[import-untyped]

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "nce"
    / "migrations"
    / "056_sales_signed_baselines_bigserial.sql"
)

_UUID_FORM_DDL = """
ALTER TABLE sales_signed_baselines DROP CONSTRAINT IF EXISTS sales_signed_baselines_pkey;
ALTER TABLE sales_signed_baselines DROP COLUMN id;
ALTER TABLE sales_signed_baselines
    ADD COLUMN id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE sales_signed_baselines ADD PRIMARY KEY (id);
"""


@pytest_asyncio.fixture
async def key_pool() -> AsyncGenerator[asyncpg.Pool, None]:
    dsn = (
        os.getenv("NCE_INTEGRATION_PG_DSN")
        or os.getenv("PG_DSN")
        or os.getenv("DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        pytest.skip("Integration tests need NCE_INTEGRATION_PG_DSN, PG_DSN, or DATABASE_URL")
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=60)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable for integration tests: {exc}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_surrogate_key_is_bigserial_with_its_sequence(key_pool: asyncpg.Pool) -> None:
    async with key_pool.acquire() as conn:
        data_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'sales_signed_baselines' AND column_name = 'id'"
        )
        assert data_type == "bigint", (
            f"sales_signed_baselines.id is {data_type!r}, not bigint. schema.sql and "
            "migration 041 both declare BIGSERIAL; migration 056 converts a uuid-keyed "
            "table. If this fires, 056 did not run or was reverted."
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_class WHERE relname = 'sales_signed_baselines_id_seq'"
            )
            == 1
        ), "the BIGSERIAL sequence is missing even though id is bigint"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_056_converts_a_uuid_keyed_table_preserving_rows(
    key_pool: asyncpg.Pool,
) -> None:
    async with key_pool.acquire() as conn:
        if await conn.fetchval("SELECT count(*) FROM sales_signed_baselines") > 0:
            pytest.skip(
                "sales_signed_baselines holds rows here; this test rewrites the table's "
                "primary key and will not touch a database with real signed baselines"
            )

        ns = await conn.fetchval("SELECT id FROM namespaces LIMIT 1")
        if ns is None:
            pytest.skip("no namespace available to attach probe baselines to")

        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(_UUID_FORM_DDL)
            await conn.execute("DROP SEQUENCE IF EXISTS sales_signed_baselines_id_seq")
            assert (
                await conn.fetchval(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'sales_signed_baselines' AND column_name = 'id'"
                )
                == "uuid"
            )

            utc = datetime.timezone.utc
            for quote, total, day in (
                ("probe-c", 3000, datetime.datetime(2026, 3, 3, tzinfo=utc)),
                ("probe-a", 1000, datetime.datetime(2026, 1, 1, tzinfo=utc)),
                ("probe-b", 2000, datetime.datetime(2026, 2, 2, tzinfo=utc)),
            ):
                await conn.execute(
                    "INSERT INTO sales_signed_baselines "
                    "(namespace_id, quote_id, signed_margin_pct, signed_total_nok, signed_at) "
                    "VALUES ($1, $2, 0.3, $3, $4)",
                    ns,
                    quote,
                    total,
                    day,
                )

            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))

            assert (
                await conn.fetchval(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'sales_signed_baselines' AND column_name = 'id'"
                )
                == "bigint"
            )
            rows = await conn.fetch(
                "SELECT id, quote_id, signed_total_nok FROM sales_signed_baselines ORDER BY id"
            )
            # Rows survive, and ids follow freeze order (signed_at), not heap order.
            assert [r["quote_id"] for r in rows] == ["probe-a", "probe-b", "probe-c"]
            assert [r["id"] for r in rows] == [1, 2, 3]
            assert [int(r["signed_total_nok"]) for r in rows] == [1000, 2000, 3000]

            # The sequence is positioned so the next insert does not collide.
            next_id = await conn.fetchval(
                "INSERT INTO sales_signed_baselines "
                "(namespace_id, quote_id, signed_margin_pct, signed_total_nok) "
                "VALUES ($1, 'probe-d', 0.4, 4000) RETURNING id",
                ns,
            )
            assert next_id == 4
        finally:
            await tr.rollback()
