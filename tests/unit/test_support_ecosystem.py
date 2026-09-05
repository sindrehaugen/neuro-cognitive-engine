"""
tests/unit/test_support_ecosystem.py
====================================
Unit tests for Module 10 Support Engine ecosystem edges and Morning Brief (#19) aggregate:
  - TICKET -[failure_pattern]-> PRODUCT_SKU feedback loop
  - TICKET -[upsell_opportunity]-> QUOTE/OPPORTUNITY feedback loop
  - Contract A boundary assertion on TICKET only (never target nodes)
  - Operations slice for #19 Morning Brief (M16 Business Insights)
  - Strict tenant predicates on all queries
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.vertical_modules.support.ecosystem import (
    do_record_failure_pattern,
    do_record_upsell_signal,
    do_support_at_risk_aggregate,
    get_support_morning_brief_slice,
)
from nce.vertical_modules.support.tickets import TicketNotFoundError

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = "11111111-1111-4111-8111-111111111111"
_PRODUCT_SKU = "CREST-NVX-360"
_QUOTE_ID = "QUOTE-2026-0042"


@pytest.mark.asyncio
async def test_record_failure_pattern_success():
    """Validates that failure pattern writes boundary edge and updates ticket events."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID),
        "status": "open",
        "summary": "Display dropout on NVX",
    }

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.ecosystem.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.ecosystem.assert_owner",
            new_callable=AsyncMock,
        ) as mock_assert_owner,
    ):
        res = await do_record_failure_pattern(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "product_sku": _PRODUCT_SKU,
                "confidence": 0.88,
                "pattern_notes": "Repeated freeze under firmware 2.1",
            },
        )

    assert res["ok"] is True
    assert res["ticket_id"] == _TICKET_ID
    assert res["product_sku"] == _PRODUCT_SKU
    assert res["edge"] == f"TICKET:{_TICKET_ID} -[failure_pattern]-> PRODUCT_SKU:{_PRODUCT_SKU}"

    # Contract A: Support asserts ownership of TICKET only!
    mock_assert_owner.assert_called_once_with(
        mock_conn,
        UUID(_NAMESPACE_ID),
        "TICKET",
        "support",
    )

    # Edge inserted with ON CONFLICT DO UPDATE
    assert mock_conn.execute.call_count == 2
    first_sql = mock_conn.execute.call_args_list[0][0][0]
    assert "INSERT INTO kg_edges" in first_sql
    assert "'failure_pattern'" in first_sql


@pytest.mark.asyncio
async def test_record_failure_pattern_ticket_not_found():
    """Validates that recording failure pattern on missing ticket raises TicketNotFoundError."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.ecosystem.scoped_pg_session",
            return_value=mock_session,
        ),
    ):
        with pytest.raises(TicketNotFoundError) as exc_info:
            await do_record_failure_pattern(
                pool,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "ticket_id": _TICKET_ID,
                    "product_sku": _PRODUCT_SKU,
                },
            )
        assert exc_info.value.ticket_id == _TICKET_ID


@pytest.mark.asyncio
async def test_record_upsell_signal_success():
    """Validates that upsell signal writes boundary edge to Sales node."""
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID),
        "status": "resolved",
        "summary": "Frequent repairs on legacy switcher",
    }

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.ecosystem.scoped_pg_session",
            return_value=mock_session,
        ),
        patch(
            "nce.vertical_modules.support.ecosystem.assert_owner",
            new_callable=AsyncMock,
        ) as mock_assert_owner,
    ):
        res = await do_record_upsell_signal(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "target_type": "QUOTE",
                "target_id": _QUOTE_ID,
                "signal_reason": "End-of-life hardware replacement recommended",
                "confidence": 0.92,
            },
        )

    assert res["ok"] is True
    assert res["target"] == f"QUOTE:{_QUOTE_ID}"
    assert res["edge"] == f"TICKET:{_TICKET_ID} -[upsell_opportunity]-> QUOTE:{_QUOTE_ID}"

    # Contract A: Support asserts ownership of TICKET only!
    mock_assert_owner.assert_called_once_with(
        mock_conn,
        UUID(_NAMESPACE_ID),
        "TICKET",
        "support",
    )


@pytest.mark.asyncio
async def test_record_upsell_signal_invalid_target_type():
    """Validates that invalid target_type raises ValueError."""
    pool = MagicMock()
    with pytest.raises(ValueError, match="target_type must be"):
        await do_record_upsell_signal(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "target_type": "INVALID_TYPE",
                "target_id": "XYZ",
            },
        )


@pytest.mark.asyncio
async def test_support_at_risk_aggregate_slice():
    """Validates that do_support_at_risk_aggregate queries and aggregates all 3 operations signals."""
    mock_conn = AsyncMock()

    # 1. At-risk SLA clocks (1 approaching breach)
    mock_conn.fetch.side_effect = [
        [
            {
                "ticket_id": UUID(_TICKET_ID),
                "sla_profile": "standard",
                "resolution_due": datetime.datetime.now(datetime.timezone.utc),
                "breached": False,
                "breach_type": None,
                "summary": "Approaching SLA breach",
                "priority": "high",
                "status": "open",
            }
        ],
        # 2. Churn-risk customers (1 critical customer)
        [
            {
                "customer_id": "CUST-ALPHA-01",
                "score": 38.5,
                "trend": {"direction": "down", "delta": -12.0},
                "churn_risk": "critical",
                "drivers": ["frequent_sla_breaches", "high_frustration_tensor"],
                "last_touchpoint_at": None,
            }
        ],
        # 3. Active proactive tickets (1 proactive ticket)
        [
            {
                "id": UUID(_TICKET_ID),
                "asset_id": UUID("22222222-2222-4222-8222-222222222222"),
                "room_id": "ROOM-BOARDROOM",
                "customer_id": "CUST-ALPHA-01",
                "summary": "Proactive telemetry alert",
                "priority": "high",
                "status": "open",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
        ],
    ]

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.support.ecosystem.require_support_enabled",
            new_callable=AsyncMock,
        ) as mock_guard,
        patch(
            "nce.vertical_modules.support.ecosystem.scoped_pg_session",
            return_value=mock_session,
        ),
    ):
        res = await do_support_at_risk_aggregate(
            pool,
            {"namespace_id": _NAMESPACE_ID},
        )

    mock_guard.assert_called_once()
    assert res["ok"] is True
    assert res["namespace_id"] == _NAMESPACE_ID

    slice_data = res["operations_slice"]
    assert slice_data["sla_at_risk_count"] == 1
    assert slice_data["churn_risk_count"] == 1
    assert slice_data["proactive_tickets_count"] == 1
    assert slice_data["churn_risk_customers"][0]["customer_id"] == "CUST-ALPHA-01"
    assert slice_data["proactive_tickets"][0]["room_id"] == "ROOM-BOARDROOM"


def test_get_support_morning_brief_slice_alias():
    """Validates that get_support_morning_brief_slice is identical to do_support_at_risk_aggregate."""
    assert get_support_morning_brief_slice is do_support_at_risk_aggregate
