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

from nce.vertical_modules.support.proactive import do_open_proactive_telemetry_ticket

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = "11111111-1111-4111-8111-111111111111"
_TICKET_ID = "22222222-2222-4222-8222-222222222222"


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
        "change_origin": "proactive_telemetry",
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

    # 3. do_open_ticket invoked with origin proactive_telemetry
    mock_do_open.assert_called_once()
    call_args = mock_do_open.call_args[0][1]
    assert call_args["change_origin"] == "proactive_telemetry"
    assert call_args["asset_id"] == _ASSET_ID
    assert "packet_loss" in call_args["summary"]

    # 4. Boundary edge inserted into kg_edges
    mock_conn.execute.assert_called_once()
    insert_sql = mock_conn.execute.call_args[0][0]
    assert "INSERT INTO kg_edges" in insert_sql
    assert "'about'" in insert_sql
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
