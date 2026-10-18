"""
tests/unit/test_support_proactive.py
====================================
Unit tests for Module 10 Support Engine proactive telemetry ticket creation:
  - Proactive ticket created on asset telemetry degradation
  - Origin set to 'proactive_telemetry'
  - Boundary edge TICKET -[about]-> ASSET written to kg_edges
  - Contract A boundary: assert_owner on TICKET only (never ASSET)
  - Idempotency: replaying telemetry degradation on already-open ticket is a no-op
  - Tenant isolation: strict WHERE namespace_id = $N predicate
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.vertical_modules.support.proactive import (
    correlate_lot_defect_recall,
    do_open_proactive_telemetry_ticket,
    do_proactive_ticket_from_anomaly,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = "11111111-1111-4111-8111-111111111111"
_TICKET_ID = "22222222-2222-4222-8222-222222222222"
_UPSTREAM_ASSET_ID = "33333333-3333-4333-8333-333333333333"
_UPSTREAM_TICKET_ID = "44444444-4444-4444-8444-444444444444"


@pytest.mark.asyncio
async def test_proactive_telemetry_ticket_creation_and_edge():
    """Validates that asset telemetry degradation authors proactive ticket + boundary edge."""
    mock_conn = AsyncMock()
    # 1. No existing proactive ticket
    mock_conn.fetchrow.return_value = None

    # Mock scoped_pg_session
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    mock_ticket = {
        "id": _TICKET_ID,
        "namespace_id": _NAMESPACE_ID,
        "asset_id": _ASSET_ID,
        "summary": "Proactive telemetry alert: asset degradation detected",
        "priority": "high",
        "status": "open",
        "change_origin": "agent",
    }

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.proactive.assert_owner",
            new_callable=AsyncMock,
        ) as mock_assert_owner,
        patch(
            "nce.vertical_modules.support.proactive.do_open_ticket",
            new_callable=AsyncMock,
            return_value={
                "ticket": mock_ticket,
                "sla_clock": {"resolution_due": "2026-09-05T23:00:00Z"},
            },
        ) as mock_do_open,
    ):
        res = await do_open_proactive_telemetry_ticket(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "metric_name": "packet_loss",
                "telemetry_data": {"loss_pct": 14.5, "latency_ms": 320},
            },
        )

    # 1. Returned shape
    assert res["ok"] is True
    assert res["proactive"] is True
    assert res["ticket_id"] == _TICKET_ID
    assert res["edge"] == f"TICKET:{_TICKET_ID} -[about]-> ASSET:{_ASSET_ID}"

    # 2. Contract A assert_owner: Asserted on TICKET only!
    mock_assert_owner.assert_called_once_with(
        mock_conn,
        UUID(_NAMESPACE_ID),
        "TICKET",
        "support",
    )

    # 3. do_open_ticket invoked with origin agent
    mock_do_open.assert_called_once()
    call_args = mock_do_open.call_args[0][1]
    assert call_args["change_origin"] == "agent"
    assert call_args["asset_id"] == _ASSET_ID
    assert "packet_loss" in call_args["summary"]

    # 4. Boundary edge inserted into kg_edges
    mock_conn.execute.assert_called_once()
    insert_sql = mock_conn.execute.call_args[0][0]
    assert "INSERT INTO kg_edges" in insert_sql
    assert "'about'" in insert_sql
    assert "'agent'" in insert_sql
    edge_args = mock_conn.execute.call_args[0][1:]
    assert edge_args[0] == f"TICKET:{_TICKET_ID}"
    assert edge_args[1] == f"ASSET:{_ASSET_ID}"
    assert edge_args[2] == UUID(_NAMESPACE_ID)


@pytest.mark.asyncio
async def test_proactive_telemetry_ticket_idempotent_replay():
    """Validates that when a proactive ticket is already open, it returns without duplicate creation."""
    existing_row = {
        "id": UUID(_TICKET_ID),
        "status": "open",
        "summary": "Existing active proactive ticket",
        "created_at": "2026-09-05T12:00:00Z",
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = existing_row

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.proactive.do_open_ticket",
            new_callable=AsyncMock,
        ) as mock_do_open,
    ):
        res = await do_open_proactive_telemetry_ticket(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
            },
        )

    assert res["ok"] is True
    assert res["idempotent_replay"] is True
    assert res["ticket_id"] == _TICKET_ID
    assert res["edge"] == f"TICKET:{_TICKET_ID} -[about]-> ASSET:{_ASSET_ID}"
    mock_do_open.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_ticket_correlated_to_upstream_kg_edges():
    """Validates that downstream anomaly correlates to existing upstream root-cause ticket."""
    mock_conn = AsyncMock()
    # 1. Direct asset has no active ticket
    # 2. Upstream connected asset in kg_edges has an active ticket
    upstream_row = {
        "id": UUID(_UPSTREAM_TICKET_ID),
        "asset_id": UUID(_UPSTREAM_ASSET_ID),
        "status": "open",
        "summary": "Core PoE switch degraded",
    }
    mock_conn.fetchrow.side_effect = [None, upstream_row]

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.proactive.do_open_ticket",
            new_callable=AsyncMock,
        ) as mock_do_open,
    ):
        res = await do_proactive_ticket_from_anomaly(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "correlate_upstream": True,
            },
        )

    assert res["status"] == "correlated_to_upstream"
    assert res["created"] is False
    assert res["ticket_id"] == _UPSTREAM_TICKET_ID
    assert res["root_cause_asset_id"] == _UPSTREAM_ASSET_ID
    mock_do_open.assert_not_called()


@pytest.mark.asyncio
async def test_proactive_ticket_correlated_to_upstream_via_param_ids():
    """Validates upstream correlation when explicit upstream_asset_ids are passed."""
    mock_conn = AsyncMock()
    upstream_row = {
        "id": UUID(_UPSTREAM_TICKET_ID),
        "asset_id": UUID(_UPSTREAM_ASSET_ID),
        "status": "in_progress",
        "summary": "PDU breaker tripped",
    }
    mock_conn.fetchrow.side_effect = [None, upstream_row]

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.proactive.do_open_ticket",
            new_callable=AsyncMock,
        ) as mock_do_open,
    ):
        res = await do_open_proactive_telemetry_ticket(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "upstream_asset_ids": [_UPSTREAM_ASSET_ID],
            },
        )

    assert res["status"] == "correlated_to_upstream"
    assert res["created"] is False
    assert res["ticket_id"] == _UPSTREAM_TICKET_ID
    assert res["root_cause_asset_id"] == _UPSTREAM_ASSET_ID
    mock_do_open.assert_not_called()


def test_correlate_lot_defect_recall_pure_helper():
    """Validates IPC-1782 lot and Norwegian elnummer recall correlation over scanned records."""
    records = [
        # Affected by lot code
        {
            "work_order_id": "WO-101",
            "asset_id": "ASSET-1",
            "serial": "SN-A1",
            "lot_code": "LOT-RECALL-99",
        },
        # Affected by lot code inside traceability dict
        {
            "work_order_id": "WO-101",
            "asset_id": "ASSET-2",
            "serial_number": "SN-A2",
            "traceability": {"lot_code": "lot-recall-99", "reel_id": "REEL-1"},
        },
        # Affected by Norwegian elnummer
        {
            "work_order_id": "WO-102",
            "asset_id": "ASSET-3",
            "serial": "SN-B1",
            "elnummer": "6912345",
            "lot_code": "LOT-CLEAN-01",
        },
        # Clean record - not affected
        {
            "work_order_id": "WO-103",
            "asset_id": "ASSET-4",
            "serial": "SN-C1",
            "lot_code": "LOT-CLEAN-02",
            "elnummer": "9999999",
        },
    ]

    recall = correlate_lot_defect_recall(
        scanned_records=records,
        defective_lot_codes=["LOT-RECALL-99", "LOT-RECALL-100"],
        defective_elnummers=["6912345"],
    )

    assert recall["recalled_count"] == 3
    assert recall["affected_work_orders"] == ["WO-101", "WO-102"]
    assert recall["affected_serials"] == ["SN-A1", "SN-A2", "SN-B1"]
    assert len(recall["matches"]) == 3

    # Check match structure
    m0 = recall["matches"][0]
    assert m0["work_order_id"] == "WO-101"
    assert m0["serial_number"] == "SN-A1"
    assert m0["lot_code"] == "LOT-RECALL-99"
    assert "defective_lot_code" in m0["recall_reasons"]

    m2 = recall["matches"][2]
    assert m2["work_order_id"] == "WO-102"
    assert m2["elnummer"] == "6912345"
    assert "defective_elnummer" in m2["recall_reasons"]


@pytest.mark.asyncio
async def test_do_proactive_ticket_from_anomaly_with_lot_recall_correlation():
    """Validates that do_proactive_ticket_from_anomaly attaches lot_recall_summary when recall params are passed."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    mock_ticket = {
        "id": _TICKET_ID,
        "namespace_id": _NAMESPACE_ID,
        "asset_id": _ASSET_ID,
        "summary": "Proactive ticket with recall correlation",
        "priority": "high",
        "status": "open",
        "change_origin": "agent",
    }

    scanned_records = [
        {
            "work_order_id": "WO-RECALL-01",
            "asset_id": _ASSET_ID,
            "serial": "SN-REC-001",
            "lot_code": "LOT-DEFECT-2026",
        },
        {
            "work_order_id": "WO-CLEAN-01",
            "asset_id": "OTHER-ASSET",
            "serial": "SN-CLEAN-001",
            "lot_code": "LOT-OK",
        },
    ]

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.proactive.assert_owner",
            new_callable=AsyncMock,
        ),
        patch(
            "nce.vertical_modules.support.proactive.do_open_ticket",
            new_callable=AsyncMock,
            return_value={
                "ticket": mock_ticket,
                "sla_clock": {"resolution_due": "2026-10-18T20:00:00Z"},
            },
        ),
    ):
        res = await do_proactive_ticket_from_anomaly(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "recalled_lot_codes": ["LOT-DEFECT-2026"],
                "scanned_records": scanned_records,
            },
        )

    assert res["ok"] is True
    assert res["proactive"] is True
    assert "lot_recall_summary" in res
    recall_summary = res["lot_recall_summary"]
    assert recall_summary["recalled_count"] == 1
    assert recall_summary["affected_work_orders"] == ["WO-RECALL-01"]
    assert recall_summary["affected_serials"] == ["SN-REC-001"]


@pytest.mark.asyncio
async def test_do_proactive_ticket_idempotent_replay_attaches_lot_recall_summary():
    """Idempotent replay branch preserves lot_recall_summary when recall parameters are passed."""
    existing_row = {
        "id": UUID(_TICKET_ID),
        "status": "open",
        "summary": "Existing ticket",
        "created_at": "2026-10-18T10:00:00Z",
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = existing_row

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    records = [
        {"work_order_id": "WO-EL-01", "serial": "SN-EL-01", "elnummer": "1234567"},
    ]

    with (
        patch(
            "nce.vertical_modules.support.proactive.scoped_pg_session",
            return_value=mock_session,
        ),
    ):
        res = await do_open_proactive_telemetry_ticket(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "asset_id": _ASSET_ID,
                "recalled_lot_codes": ["NO-LOT"],
                "recalled_elnummers": ["1234567"],
                "scanned_records": records,
            },
        )

    assert res["ok"] is True
    assert res["idempotent_replay"] is True
    assert "lot_recall_summary" in res
    assert res["lot_recall_summary"]["recalled_count"] == 1
    assert res["lot_recall_summary"]["affected_work_orders"] == ["WO-EL-01"]
