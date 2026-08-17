"""Integration tests for Sales Stand-alone Flip and Watchers (Batch 093 — standalone-flip)."""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.orchestrator import NCEEngine
from nce.vertical_modules.sales.flip import (
    do_flip_function,
    do_morning_brief_slice,
    do_stalled_deal_watcher,
)


def _make_engine_stub(pg_pool: asyncpg.Pool) -> NCEEngine:
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub  # type: ignore[return-value]


async def _seed_divergence(
    conn: asyncpg.Connection,
    *,
    ns_id: uuid.UUID,
    engine: str,
    entity: str,
    field: str,
    nce_val: str,
    ext_val: str,
    materiality: float,
    detected_at: datetime.datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO divergence_log (namespace_id, engine, entity, field, nce_value, ext_value, materiality, detected_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::numeric, $8::timestamptz)
        """,
        ns_id,
        engine,
        entity,
        field,
        nce_val,
        ext_val,
        materiality,
        detected_at,
    )


async def _seed_opportunity(
    conn: asyncpg.Connection,
    *,
    ns_id: uuid.UUID,
    source_id: str,
    name: str,
    estimatedvalue: float | None = None,
    actualvalue: float | None = None,
    statecode: int = 0,
    updated_at: datetime.datetime,
) -> None:
    source_json = {
        "estimatedvalue": estimatedvalue,
        "actualvalue": actualvalue,
        "statecode": statecode,
    }
    await conn.execute(
        """
        INSERT INTO sales_read_model (namespace_id, entity, source_id, name, source_json, updated_at)
        VALUES ($1::uuid, 'opportunities', $2, $3, $4::jsonb, $5::timestamptz)
        ON CONFLICT (namespace_id, entity, source_id) DO UPDATE
            SET name = EXCLUDED.name,
                source_json = EXCLUDED.source_json,
                updated_at = EXCLUDED.updated_at
        """,
        ns_id,
        source_id,
        name,
        json.dumps(source_json),
        updated_at,
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestSalesFlipAndWatchers:
    """Integration tests for Sales source mode flip gate, stalled-deal watcher, and morning brief."""

    async def test_gated_flip_function(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify do_flip_function is refused when the divergence log is dirty, and allowed when clean."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # 1. Flip should succeed initially since the divergence log is clean
        res_clean = await do_flip_function(
            engine, {"namespace_id": str(ns), "function": "read_customers", "window_days": 7}
        )
        assert res_clean.get("ok") is True
        assert res_clean.get("mode") == "nce"

        # Verify active mode in DB is indeed nce
        async with pg_pool.acquire() as conn:
            await set_namespace_context(conn, ns)
            mode = await conn.fetchval(
                """
                SELECT mode FROM source_mode_config
                WHERE namespace_id = $1::uuid AND engine = 'sales' AND function = 'read_customers'
                """,
                ns,
            )
            assert mode == "nce"

        # 2. Reset config back to 'both' to test the gate
        async with pg_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE source_mode_config
                SET mode = 'both'
                WHERE namespace_id = $1::uuid AND engine = 'sales' AND function = 'read_customers'
                """,
                ns,
            )

        # 3. Seed a divergence log record in the 7-day window
        now = datetime.datetime.now(datetime.timezone.utc)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)
                await _seed_divergence(
                    conn,
                    ns_id=ns,
                    engine="sales",
                    entity="accounts",
                    field="name",
                    nce_val="NCE Corp",
                    ext_val="D365 Corp",
                    materiality=1.0,
                    detected_at=now - datetime.timedelta(days=2),
                )

        # 4. Flip should now be refused
        res_dirty = await do_flip_function(
            engine, {"namespace_id": str(ns), "function": "read_customers", "window_days": 7}
        )
        assert res_dirty.get("ok") is False
        assert "Refused" in res_dirty.get("reason", "")
        assert res_dirty.get("divergences_count") == 1

        # 5. Flip should succeed if the window is smaller than the age of the divergence
        res_narrow_window = await do_flip_function(
            engine, {"namespace_id": str(ns), "function": "read_customers", "window_days": 1}
        )
        assert res_narrow_window.get("ok") is True
        assert res_narrow_window.get("mode") == "nce"

    async def test_stalled_deal_watcher_and_morning_brief(
        self,
        pg_pool: asyncpg.Pool,
        make_namespace: Any,
    ) -> None:
        """Verify stalled-deal Watcher and Morning-brief slice work correctly."""
        ns = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        now = datetime.datetime.now(datetime.timezone.utc)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns)

                # Deal A: Open, updated 35 days ago (stalled, at risk)
                await _seed_opportunity(
                    conn,
                    ns_id=ns,
                    source_id="OPP-A",
                    name="Stalled Deal A",
                    estimatedvalue=200000.0,
                    statecode=0,
                    updated_at=now - datetime.timedelta(days=35),
                )

                # Deal B: Open, updated 5 days ago (active, not stalled)
                await _seed_opportunity(
                    conn,
                    ns_id=ns,
                    source_id="OPP-B",
                    name="Active Deal B",
                    estimatedvalue=150000.0,
                    statecode=0,
                    updated_at=now - datetime.timedelta(days=5),
                )

                # Deal C: Won, updated 2 days ago (won this period)
                await _seed_opportunity(
                    conn,
                    ns_id=ns,
                    source_id="OPP-C",
                    name="Won Deal C",
                    estimatedvalue=300000.0,
                    actualvalue=280000.0,
                    statecode=1,
                    updated_at=now - datetime.timedelta(days=2),
                )

        # 1. Test Stalled Deal Watcher (threshold 30 days)
        res_watcher = await do_stalled_deal_watcher(
            engine, {"namespace_id": str(ns), "slip_days": 30}
        )
        assert res_watcher.get("ok") is True
        assert res_watcher.get("stalled_deals_count") == 1
        assert res_watcher["stalled_deals"][0]["deal_id"] == "OPP-A"

        # 2. Test Morning Brief Slice (period 7 days)
        res_brief = await do_morning_brief_slice(
            engine, {"namespace_id": str(ns), "period_days": 7}
        )
        assert res_brief.get("ok") is True
        # Pipeline value should include open deals A and B (200k + 150k = 350k)
        assert abs(res_brief.get("pipeline_value", 0.0) - 350000.0) < 1e-5
        # At risk should count Deal A (updated >14 days ago)
        assert res_brief.get("at_risk_deals_count") == 1
        # Won this period should count Deal C (280k actual value, 1 count)
        assert abs(res_brief.get("won_value_this_period", 0.0) - 280000.0) < 1e-5
        assert res_brief.get("won_count_this_period") == 1
