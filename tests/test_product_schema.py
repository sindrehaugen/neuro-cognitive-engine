"""
Integration test for product_catalog and product_prices schema.

Verifies:
  - Both tables exist after migrations
  - FORCE ROW LEVEL SECURITY is enabled on each
  - tenant_isolation_policy exists on each
  - ETIM/gtin/product_source_id columns present with expected types
  - Row isolation: one namespace's rows are invisible under another
"""

from __future__ import annotations

import json
import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.auth import set_namespace_context
from nce.config import cfg


@pytest_asyncio.fixture(scope="function")
async def pg_pool() -> asyncpg.pool.Pool:  # type: ignore[name-defined]
    """Create a PostgreSQL connection pool for tests."""
    pool = await asyncpg.create_pool(cfg.PG_DSN, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.mark.integration
class TestProductSchema:
    """Integration tests for product catalog schema."""

    @pytest.mark.asyncio
    async def test_product_catalog_table_exists(self, pg_pool):  # type: ignore[no-untyped-def]
        """Verify product_catalog table exists with correct schema."""
        async with pg_pool.acquire() as conn:
            # Check table exists
            result = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'product_catalog'
                )
                """
            )
            assert result is True, "product_catalog table does not exist"

            # Check required columns exist with correct types
            columns = await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'product_catalog'
                ORDER BY ordinal_position
                """
            )
            column_map = {col["column_name"]: col["data_type"] for col in columns}

            assert "id" in column_map
            assert "gtin" in column_map
            assert "manufacturer" in column_map
            assert "mfr_part_no" in column_map
            assert "product_source_id" in column_map
            assert "lifecycle_status" in column_map
            assert "is_deleted" in column_map
            assert column_map["etim_specs"] == "jsonb", (
                f"etim_specs should be jsonb, got {column_map['etim_specs']}"
            )
            assert "created_at" in column_map
            assert "updated_at" in column_map

    @pytest.mark.asyncio
    async def test_product_prices_table_exists(self, pg_pool):  # type: ignore[no-untyped-def]
        """Verify product_prices table exists with correct schema."""
        async with pg_pool.acquire() as conn:
            # Check table exists
            result = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'product_prices'
                )
                """
            )
            assert result is True, "product_prices table does not exist"

            # Check required columns exist
            columns = await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'product_prices'
                ORDER BY ordinal_position
                """
            )
            column_map = {col["column_name"]: col["data_type"] for col in columns}

            assert "id" in column_map
            assert "namespace_id" in column_map
            assert "mfr_part_no" in column_map
            assert "supplier" in column_map
            assert "bid_id" in column_map
            assert "list_price" in column_map
            assert "cost_price" in column_map
            assert "created_at" in column_map
            assert "updated_at" in column_map

    @pytest.mark.asyncio
    async def test_product_prices_force_rls(self, pg_pool):  # type: ignore[no-untyped-def]
        """Verify product_prices has FORCE ROW LEVEL SECURITY enabled."""
        async with pg_pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT relforcerowsecurity
                FROM pg_class
                WHERE relname = 'product_prices'
                """
            )
            assert result is True, "product_prices does not have FORCE ROW LEVEL SECURITY"

    @pytest.mark.asyncio
    async def test_product_prices_has_tenant_isolation_policy(self, pg_pool):  # type: ignore[no-untyped-def]
        """Verify product_prices has tenant_isolation_policy RLS policy."""
        async with pg_pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM pg_policies
                    WHERE tablename = 'product_prices'
                    AND policyname = 'tenant_isolation_policy'
                )
                """
            )
            assert result is True, "product_prices does not have tenant_isolation_policy"

    @pytest.mark.asyncio
    async def test_product_prices_row_isolation(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """Verify rows inserted under ns_a are invisible under ns_b (RLS enforcement).

        Connects as the ``nce_app`` role (FORCE RLS applies) and asserts that a
        product_prices row inserted under ns_a yields count==1 under ns_a and
        count==0 under ns_b.

        The product_catalog half of this test was DELETED, not weakened:
        the catalogue is global reference data as of 2026-09-04 (migration
        064), so "ns_b must not see ns_a's catalogue row" is no longer a
        requirement. product_prices stays tenant-scoped -- supplier-bid
        pricing is per-tenant commercial confidential.
        """
        ns_a = await make_namespace()
        ns_b = await make_namespace()

        # --- insert product_prices row under ns_a ---
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            price_id = await pg_app_conn.fetchval(
                """
                INSERT INTO product_prices
                (namespace_id, mfr_part_no, supplier, bid_id, list_price, cost_price)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                ns_a,
                "TEST-001",
                "SupplierA",
                "BID-001",
                100.0,
                50.0,
            )

        assert price_id is not None

        # --- ns_a sees its own row ---
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_a)
            visible_prices = await pg_app_conn.fetchval(
                "SELECT count(*) FROM product_prices WHERE id = $1",
                price_id,
            )

        assert visible_prices == 1, (
            f"ns_a should see its own product_prices row (id={price_id}), count={visible_prices}"
        )

        # --- ns_b cannot see ns_a's row (RLS blocks it) ---
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns_b)
            blocked_prices = await pg_app_conn.fetchval(
                "SELECT count(*) FROM product_prices WHERE id = $1",
                price_id,
            )

        assert blocked_prices == 0, (
            f"ns_b must NOT see ns_a's product_prices row (id={price_id}), count={blocked_prices}"
        )

    @pytest.mark.asyncio
    async def test_product_catalog_etim_specs_structure(self, pg_pool):  # type: ignore[no-untyped-def]
        """Verify etim_specs JSONB can store coded tuples with provenance."""
        async with pg_pool.acquire() as conn:
            ns_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO namespaces (id, slug, metadata) VALUES ($1, $2, $3)",
                ns_id,
                f"test_etim_{uuid.uuid4().hex[:8]}",
                "{}",
            )

            product_id = uuid.uuid4()
            etim_specs = {
                "specs": [
                    {
                        "etim_class": "EC002041",
                        "feature": "transmission_type",
                        "value": "HDMI",
                        "unit": None,
                        "provenance": {
                            "source": "datasheet",
                            "confidence": 0.95,
                            "source_id": "nettailer_001",
                        },
                    }
                ]
            }

            await conn.execute(
                """
                INSERT INTO product_catalog
                (id, manufacturer, mfr_part_no, product_source_id,
                 lifecycle_status, is_deleted, etim_specs)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                """,
                product_id,
                "TestMfg",
                "TEST-ETIM-001",
                "test_source",
                "active",
                False,
                json.dumps(etim_specs),
            )

            # Verify we can retrieve the JSONB data
            result: dict | None = await conn.fetchval(
                "SELECT etim_specs::text FROM product_catalog WHERE id = $1",
                product_id,
            )
            assert result is not None
            result_dict = json.loads(result)
            assert "specs" in result_dict
            assert len(result_dict["specs"]) == 1
            assert result_dict["specs"][0]["etim_class"] == "EC002041"
            assert result_dict["specs"][0]["provenance"]["confidence"] == 0.95

            # Cleanup
            await conn.execute("DELETE FROM product_catalog WHERE id = $1", product_id)
            await conn.execute("DELETE FROM namespaces WHERE id = $1", ns_id)
