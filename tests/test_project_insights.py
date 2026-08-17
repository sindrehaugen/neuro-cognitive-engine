"""Integration tests for project/insights.py — Wave 9 (scope-creep-status).

Validates:
  1. do_detect_scope_creep(engine, params) returns correct change orders list
     and total delta value against a seeded baseline.
  2. do_status_report(engine, params) generates a retrieval-grounded narrative
     where every fact claim is backed by a transiently upserted status node
     and cited via C9a, ensuring no facts are free-generated.
  3. Graceful degradation when the Sales baseline is unbuilt or unavailable.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.insights import do_detect_scope_creep, do_status_report

# Mock paths
_MOCK_BASELINE = "nce.vertical_modules.project.baseline._read_signed_baseline"
_MOCK_INSIGHTS_BASELINE = "nce.vertical_modules.project.insights._read_signed_baseline"


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def _seed_test_project_data(
    pg_pool: asyncpg.Pool,
    ns_uuid: uuid.UUID,
    quote_id: str,
    bom_refs: list[str],
    change_orders: list[dict[str, Any]],
    current_phase: str = "G0",
    gate_created_at: datetime.datetime | None = None,
) -> tuple[str, list[str]]:
    project_lbl = f"PROJECT:{quote_id.upper()}"
    bom_labels = [f"BOM_LINE:{quote_id.upper()}:{r.upper()}" for r in bom_refs]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_uuid)
            await seed_node_ownership_registry(conn, ns_uuid)

            # 1. Project node
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                project_lbl,
                str(ns_uuid),
            )

            # 2. Gate node
            gate_lbl = f"GATE:{quote_id.upper()}:{current_phase}"
            created_time = gate_created_at or datetime.datetime.now(datetime.timezone.utc)
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, created_at)
                VALUES ($1, 'PROJECT_GATE', $2::uuid, $3)
                ON CONFLICT (label, namespace_id) DO UPDATE SET updated_at = NOW()
                """,
                gate_lbl,
                str(ns_uuid),
                created_time,
            )

            # 3. in_phase edge
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id)
                VALUES ($1, 'in_phase', $2, 1.0, $3::uuid)
                ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                """,
                project_lbl,
                gate_lbl,
                str(ns_uuid),
            )

            # 4. BOM lines and contains edges
            for bom_lbl in bom_labels:
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'BOM_LINE', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    bom_lbl,
                    str(ns_uuid),
                )
                await conn.execute(
                    """
                    INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'contains', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                    """,
                    project_lbl,
                    bom_lbl,
                    str(ns_uuid),
                )

            # 5. Change orders
            for co in change_orders:
                co_lbl = co["label"]
                await conn.execute(
                    """
                    INSERT INTO kg_nodes (label, entity_type, namespace_id)
                    VALUES ($1, 'PROJECT_CHANGE_ORDER', $2::uuid)
                    ON CONFLICT (label, namespace_id) DO NOTHING
                    """,
                    co_lbl,
                    str(ns_uuid),
                )

                # amends edge
                bom_lbl = f"BOM_LINE:{quote_id.upper()}:{co['amends'].upper()}"
                await conn.execute(
                    """
                    INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id)
                    VALUES ($1, 'amends', $2, 1.0, $3::uuid)
                    ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                    """,
                    co_lbl,
                    bom_lbl,
                    str(ns_uuid),
                )

                # has_value edge
                if "value" in co:
                    await conn.execute(
                        """
                        INSERT INTO kg_edges (subject_label, predicate, object_label, confidence, namespace_id)
                        VALUES ($1, 'has_value', $2, 1.0, $3::uuid)
                        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
                        """,
                        co_lbl,
                        f"VALUE:{co['value']}",
                        str(ns_uuid),
                    )

    return project_lbl, bom_labels


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detect_scope_creep_calculates_correct_delta(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"Q-CREEP-001-{uuid.uuid4().hex[:8]}"
    change_orders = [
        {"label": f"CHANGE_ORDER:{quote_id}:CO1", "amends": "LINE-A", "value": 50000.0},
        {"label": f"CHANGE_ORDER:{quote_id}:CO2", "amends": "LINE-B", "value": -15000.0},
    ]
    project_lbl, _ = await _seed_test_project_data(
        pg_pool, namespace_id, quote_id, ["LINE-A", "LINE-B"], change_orders
    )
    engine = _make_engine_stub(pg_pool)

    fake_baseline = {
        "id": "baseline-001",
        "quote_id": quote_id,
        "signed_margin_pct": 0.35,
        "signed_total_nok": 500000.0,
        "signed_at": "2026-06-22T10:00:00Z",
    }

    async def _fake_read(*args, **kwargs):
        return fake_baseline

    with (
        patch(_MOCK_BASELINE, side_effect=_fake_read),
        patch(_MOCK_INSIGHTS_BASELINE, side_effect=_fake_read),
    ):
        res = await do_detect_scope_creep(
            engine,
            {
                "namespace_id": str(namespace_id),
                "project_id": project_lbl,
            },
        )

    assert res["ok"] is True
    assert res["sales_available"] is True
    assert res["signed_total_nok"] == 500000.0
    # Delta should be 50000.0 + (-15000.0) = 35000.0
    assert abs(res["delta_signed_vs_current"] - 35000.0) < 1e-6
    assert abs(res["current_total_nok"] - 535000.0) < 1e-6

    # Verify change orders list
    co_list = res["change_orders"]
    assert len(co_list) == 2
    assert co_list[0]["label"] == f"CHANGE_ORDER:{quote_id}:CO1"
    assert co_list[0]["value"] == 50000.0
    assert co_list[0]["amended_bom_line"] == f"BOM_LINE:{quote_id.upper()}:LINE-A"

    assert co_list[1]["label"] == f"CHANGE_ORDER:{quote_id}:CO2"
    assert co_list[1]["value"] == -15000.0
    assert co_list[1]["amended_bom_line"] == f"BOM_LINE:{quote_id.upper()}:LINE-B"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detect_scope_creep_degrades_when_baseline_unavailable(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"Q-CREEP-002-{uuid.uuid4().hex[:8]}"
    change_orders = [
        {"label": f"CHANGE_ORDER:{quote_id}:CO1", "amends": "LINE-A", "value": 10000.0},
    ]
    project_lbl, _ = await _seed_test_project_data(
        pg_pool, namespace_id, quote_id, ["LINE-A"], change_orders
    )
    engine = _make_engine_stub(pg_pool)

    # Do not mock, let baseline read fail/NotImplementedError
    res = await do_detect_scope_creep(
        engine,
        {
            "namespace_id": str(namespace_id),
            "project_id": project_lbl,
        },
    )

    assert res["ok"] is True
    assert res["sales_available"] is False
    assert res["signed_total_nok"] is None
    assert abs(res["delta_signed_vs_current"] - 10000.0) < 1e-6
    assert abs(res["current_total_nok"] - 10000.0) < 1e-6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_report_generates_grounded_prose(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"Q-REPORT-001-{uuid.uuid4().hex[:8]}"
    change_orders = [
        {"label": f"CHANGE_ORDER:{quote_id}:CO1", "amends": "LINE-A", "value": 20000.0},
    ]
    # Dwell = 5 days ago
    dwell_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
    project_lbl, _ = await _seed_test_project_data(
        pg_pool, namespace_id, quote_id, ["LINE-A"], change_orders, "G1", dwell_time
    )
    engine = _make_engine_stub(pg_pool)

    fake_baseline = {
        "id": "baseline-002",
        "quote_id": quote_id,
        "signed_margin_pct": 0.40,
        "signed_total_nok": 200000.0,
        "signed_at": "2026-06-22T10:00:00Z",
    }

    async def _fake_read(*args, **kwargs):
        return fake_baseline

    with (
        patch(_MOCK_BASELINE, side_effect=_fake_read),
        patch(_MOCK_INSIGHTS_BASELINE, side_effect=_fake_read),
    ):
        res = await do_status_report(
            engine,
            {
                "namespace_id": str(namespace_id),
                "project_id": project_lbl,
                "estimated_cost_nok": 110000.0,  # margin = (200K - 110K)/200K = 45%
                "estimated_revenue_nok": 200000.0,
            },
        )

    assert res["ok"] is True
    assert res["margin_trinity"]["signed"] == 0.40
    assert res["margin_trinity"]["estimated"] == 0.45

    narrative = res["narrative"]
    assert narrative.startswith("Project Status Report:")
    assert "Margin trinity: signed=40.0%, estimated=45.0%, actual=None" in narrative
    assert "Gate dwell: current phase is G1 (dwell: 5 days)" in narrative
    assert "Scope creep delta: 20,000.0 NOK" in narrative

    # Asserts that citations exist and map to the transiently created nodes
    citations = res["citations"]
    assert len(citations) == 3
    for citation in citations:
        assert "node_id" in citation
        assert "fact" in citation
        assert len(citation["node_id"]) == 36  # UUID

    # Test that subsequent run cleans up old status fact nodes
    with (
        patch(_MOCK_BASELINE, side_effect=_fake_read),
        patch(_MOCK_INSIGHTS_BASELINE, side_effect=_fake_read),
    ):
        res2 = await do_status_report(
            engine,
            {
                "namespace_id": str(namespace_id),
                "project_id": project_lbl,
                "estimated_cost_nok": 100000.0,  # margin = 50%
                "estimated_revenue_nok": 200000.0,
            },
        )

    assert res2["ok"] is True
    assert "Margin trinity: signed=40.0%, estimated=50.0%" in res2["narrative"]

    # Verify that the old status nodes are gone
    old_fact_ids = {c["node_id"] for c in citations}
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            remaining_count = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_nodes WHERE id = ANY($1::uuid[])",
                list(old_fact_ids),
            )
    assert remaining_count == 0, (
        f"Stale status fact nodes were not cleaned up: {remaining_count} remained"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_report_degrades_when_gate_missing(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    quote_id = f"Q-REPORT-002-{uuid.uuid4().hex[:8]}"
    project_lbl = f"PROJECT:{quote_id.upper()}"
    engine = _make_engine_stub(pg_pool)

    # Seed ONLY the project node (no in_phase edge or gate node)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'PROJECT_PROJECT', $2::uuid)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                project_lbl,
                str(namespace_id),
            )

    res = await do_status_report(
        engine,
        {
            "namespace_id": str(namespace_id),
            "project_id": project_lbl,
        },
    )

    assert res["ok"] is True
    assert "Gate dwell: current phase is unknown (dwell: 0 days)" in res["narrative"]
