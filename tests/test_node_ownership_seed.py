"""Integration tests for the node-ownership seed mechanism (Contract A §9.1).

Validates:
  a. After seeding, assert_owner passes for the correct engine.
  b. After seeding, assert_owner raises OwnershipError for the wrong engine.
  c. An unseeded namespace raises OwnershipError (deny-by-default).
  d. seed_node_ownership_registry is idempotent (second call returns 0; row
     count is unchanged).
  e. End-to-end via the real product writer: seeded namespace succeeds;
     unseeded namespace raises OwnershipError. emit_graph_write is mocked to
     isolate ownership behaviour from outbox infrastructure.

Runs as @pytest.mark.integration — requires a live Postgres with schema.sql
and migration 032 applied (run scratch/_apply_probe_b032.py first).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg  # type: ignore[import-untyped]
import pytest  # noqa: F401 # type: ignore[import-untyped]

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership import OwnershipError, assert_owner
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry


@pytest.mark.integration
@pytest.mark.asyncio
class TestNodeOwnershipSeed:
    """Integration test suite for seed_node_ownership_registry."""

    # ------------------------------------------------------------------
    # a. Seeded namespace: assert_owner passes for correct engine
    # ------------------------------------------------------------------

    async def test_seeded_namespace_correct_engine_passes(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """After seeding, assert_owner does NOT raise for the registered engine."""
        ns = await make_namespace()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await seed_node_ownership_registry(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            # Must not raise.
            await assert_owner(pg_app_conn, ns, "PRODUCT_SKU", "product")

    # ------------------------------------------------------------------
    # b. Seeded namespace: assert_owner raises for wrong engine
    # ------------------------------------------------------------------

    async def test_seeded_namespace_wrong_engine_raises(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """After seeding, assert_owner raises OwnershipError for a different engine."""
        ns = await make_namespace()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await seed_node_ownership_registry(pg_app_conn, ns)

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(pg_app_conn, ns, "PRODUCT_SKU", "sales")

        err = exc_info.value
        assert err.node_type == "PRODUCT_SKU"
        assert err.writer_engine == "sales"
        assert err.owner_engine == "product"

    # ------------------------------------------------------------------
    # c. Unseeded namespace: deny-by-default
    # ------------------------------------------------------------------

    async def test_unseeded_namespace_raises_ownership_error(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """An unseeded namespace raises OwnershipError — deny-by-default enforced."""
        ns2 = await make_namespace()

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns2)
            with pytest.raises(OwnershipError) as exc_info:
                await assert_owner(pg_app_conn, ns2, "PRODUCT_SKU", "product")

        err = exc_info.value
        assert err.owner_engine is None, (
            "Deny-by-default: owner_engine must be None when no registry row exists"
        )
        assert err.node_type == "PRODUCT_SKU"

    # ------------------------------------------------------------------
    # d. Idempotency: second call inserts 0 rows; row count unchanged
    # ------------------------------------------------------------------

    async def test_seed_is_idempotent(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """Calling seed_node_ownership_registry twice inserts rows only once."""
        ns = await make_namespace()

        # First call — inserts the rows.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            first_inserted = await seed_node_ownership_registry(pg_app_conn, ns)

        assert first_inserted > 0, "First seed call must insert at least one row"

        # Second call — must be a no-op.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            second_inserted = await seed_node_ownership_registry(pg_app_conn, ns)

        assert second_inserted == 0, (
            f"Second seed call must insert 0 rows (idempotent), got {second_inserted}"
        )

        # Row count must equal what the first call inserted.
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            count = await pg_app_conn.fetchval(
                "SELECT COUNT(*) FROM node_ownership_registry WHERE namespace_id = $1",
                ns,
            )
        assert count == first_inserted, (
            f"Row count after two seed calls ({count}) must equal first-call insertions "
            f"({first_inserted})"
        )

    # ------------------------------------------------------------------
    # e. End-to-end via the real product writer
    # ------------------------------------------------------------------

    async def test_product_writer_succeeds_after_seed(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """upsert_product_node FULLY succeeds after seeding — a real kg_nodes row lands.

        assert_owner is NOT mocked (the seed must satisfy it for real); only the
        C4 outbox emission is mocked to isolate the write path.  This asserts the
        complete write succeeds with NO exception (the regression that previously
        failed silently on the non-existent kg_nodes.metadata column) AND that the
        PRODUCT_SKU node row actually exists afterwards.
        """
        from nce.vertical_modules.product.graph import _product_label, upsert_product_node

        ns = await make_namespace()
        label = _product_label("Biamp", "TST-1")

        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            await seed_node_ownership_registry(pg_app_conn, ns)

        # Mock only the outbox; the real INSERT must execute against kg_nodes.
        with patch(
            "nce.vertical_modules.product.graph.emit_graph_write",
            new_callable=AsyncMock,
        ):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns)
                # Must not raise anything (no OwnershipError, no missing-column error).
                await upsert_product_node(
                    pg_app_conn,
                    ns,
                    manufacturer="Biamp",
                    mfr_part_no="TST-1",
                )

        # The node row must actually exist (proves the INSERT ran, not just the guard).
        async with pg_app_conn.transaction():
            await set_namespace_context(pg_app_conn, ns)
            row = await pg_app_conn.fetchrow(
                "SELECT entity_type FROM kg_nodes WHERE label = $1 AND namespace_id = $2",
                label,
                ns,
            )
        assert row is not None, "upsert_product_node did not write a kg_nodes row after seeding"
        assert row["entity_type"] == "PRODUCT_SKU"

    async def test_product_writer_raises_for_unseeded_namespace(
        self,
        pg_app_conn: asyncpg.Connection,
        make_namespace,
    ) -> None:
        """upsert_product_node raises OwnershipError on an unseeded namespace."""
        from nce.vertical_modules.product.graph import upsert_product_node

        ns2 = await make_namespace()

        with patch(
            "nce.vertical_modules.product.graph.emit_graph_write",
            new_callable=AsyncMock,
        ):
            async with pg_app_conn.transaction():
                await set_namespace_context(pg_app_conn, ns2)
                with pytest.raises(OwnershipError):
                    await upsert_product_node(
                        pg_app_conn,
                        ns2,
                        manufacturer="Biamp",
                        mfr_part_no="TST-NOSEED",
                    )
