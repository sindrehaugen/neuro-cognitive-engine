"""Integration tests for Sales Quote Signing and Project Conversion (Batch 090)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.baseline import get_signed_baseline
from nce.vertical_modules.sales.signing import do_on_signed_callback, do_request_signature


async def _insert_sales_record(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    entity: str,
    source_id: str,
    name: str,
    source_json: dict[str, Any],
) -> None:
    """Helper to insert sales read model records directly for testing."""
    await conn.execute(
        """
        INSERT INTO sales_read_model
            (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
        VALUES
            ($1, $2, $3, $4, $5::jsonb, '{}'::jsonb, false, now(), now())
        ON CONFLICT (namespace_id, entity, source_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            source_json = EXCLUDED.source_json,
            updated_at = now()
        """,
        str(namespace_id),
        entity,
        source_id,
        name,
        json.dumps(source_json),
    )


async def _count_kg_nodes_by_type(conn: asyncpg.Connection, ns_id: UUID, entity_type: str) -> int:
    return int(
        await conn.fetchval(
            "SELECT COUNT(*) FROM kg_nodes WHERE namespace_id = $1 AND entity_type = $2",
            str(ns_id),
            entity_type,
        )
    )


# Project baseline read seam patch target
_PROJECT_BASELINE_SEAM = "nce.vertical_modules.project.baseline._read_signed_baseline"
# We also need to patch ownership registry assert in Project convert since we don't seed all ownership tables
_PROJECT_OWNERSHIP_SEAM = "nce.vertical_modules.project.convert.assert_owner"


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesSignToProject:
    """Integration tests for Sales Quote signing, baseline freeze, and Project conversion."""

    async def test_signing_flow_freeze_and_conversion_idempotency(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify request_signature creates session, and callback triggers freeze + project conversion exactly once."""
        ns = await make_namespace()
        quote_id = f"q-{uuid4().hex[:8]}"

        engine = NCEEngine()
        await engine.connect()

        try:
            # 1. Seed quote in read model
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns)
                    await _insert_sales_record(
                        conn,
                        ns,
                        "quotes",
                        quote_id,
                        "Signable Proposal",
                        {
                            "quoteid": quote_id,
                            "name": "Signable Proposal",
                            "margin": 0.35,
                            "total_price": 250000.0,
                            "customer_name": "Acme Corp",
                        },
                    )

            # 2. Call do_request_signature to initiate signing
            session = await do_request_signature(
                engine,
                {
                    "namespace_id": str(ns),
                    "quote_id": quote_id,
                    "method": "manual",
                },
            )

            session_id = session["session_id"]
            assert session_id is not None
            assert session["status"] == "pending"

            # Check that pending status is saved in database
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns)
                    row = await conn.fetchrow(
                        "SELECT manual FROM sales_read_model WHERE namespace_id = $1 AND entity = 'quotes' AND source_id = $2",
                        str(ns),
                        quote_id,
                    )
                    assert row is not None
                    manual = (
                        json.loads(row["manual"])
                        if isinstance(row["manual"], str)
                        else row["manual"]
                    )
                    assert manual["signing_session_id"] == session_id
                    assert manual["signing_status"] == "pending"

            # 3. Setup mocks for A2A Project baseline read seam and ownership checks
            # Mock _read_signed_baseline seam in Project to return the frozen row directly from Sales DB
            async def mock_read_signed_baseline(eng, ns_id, q_id):
                async with pg_pool.acquire() as conn:
                    return await get_signed_baseline(conn, ns_id, q_id)

            # 4. Trigger signed webhook callback
            # Use patches to bypass Project A2A seam and assert_owner guards
            with (
                patch(_PROJECT_BASELINE_SEAM, side_effect=mock_read_signed_baseline),
                patch(_PROJECT_OWNERSHIP_SEAM, new_callable=AsyncMock),
            ):
                res = await do_on_signed_callback(
                    engine,
                    {
                        "namespace_id": str(ns),
                        "session_id": session_id,
                    },
                )

            assert res["ok"] is True
            assert res["quote_id"] == quote_id
            assert res["session_id"] == session_id
            assert res["baseline_frozen"] is True
            assert res["already_processed"] is False
            project_id = res["project_id"]
            assert project_id == f"PROJECT:{quote_id.upper()}"

            # Check that the baseline is frozen in DB
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns)
                    baseline = await get_signed_baseline(conn, ns, quote_id)
                    assert baseline is not None
                    assert baseline["signed_margin_pct"] == 0.35
                    assert baseline["signed_total_nok"] == 250000.0

                    # Verify PROJECT_PROJECT node was created in graph
                    count = await _count_kg_nodes_by_type(conn, ns, "PROJECT_PROJECT")
                    assert count == 1

            # 5. Re-trigger webhook (retry scenario) - should be a no-op / idempotent
            with (
                patch(_PROJECT_BASELINE_SEAM, side_effect=mock_read_signed_baseline),
                patch(_PROJECT_OWNERSHIP_SEAM, new_callable=AsyncMock),
            ):
                res_retry = await do_on_signed_callback(
                    engine,
                    {
                        "namespace_id": str(ns),
                        "session_id": session_id,
                    },
                )

            assert res_retry["ok"] is True
            assert res_retry["already_processed"] is True
            assert res_retry["project_id"] == project_id

            # Verify no duplicate baseline or project nodes were created
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await set_namespace_context(conn, ns)
                    proj_count = await _count_kg_nodes_by_type(conn, ns, "PROJECT_PROJECT")
                    assert proj_count == 1

        finally:
            await engine.disconnect()
