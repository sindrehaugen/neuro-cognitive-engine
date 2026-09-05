"""
tests/unit/test_support_dispatch.py
===================================
Unit tests for Module 10 (Support Engine) work order dispatch:
  - DISPATCH_CEILING autonomy gate enforcement (over-ceiling refusal)
  - Human confirmation override for over-ceiling dispatch
  - Under-ceiling autonomous execution
  - Deterministic idempotency: retry creates no second work order
  - Contract-A boundary edge: TICKET -[dispatched_as]-> WORK_ORDER
  - Ownership assertion on TICKET (Support does NOT own WORK_ORDER)
  - Business refusals on missing or resolved tickets
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nce.vertical_modules.support.dispatch import (
    DispatchCeilingExceededError,
    _derive_dispatch_idempotency_key,
    _derive_work_order_id,
    do_dispatch_work_order,
)
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketNotFoundError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = str(uuid4())


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    return pool


# ---------------------------------------------------------------------------
# 1. Autonomy Ceiling Refusal & Guard Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_ceiling_dispatch_refused(mock_pool):
    """An autonomous dispatch exceeding DISPATCH_CEILING MUST be refused."""
    with pytest.raises(DispatchCeilingExceededError) as exc_info:
        await do_dispatch_work_order(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "estimated_cost": 500.0,
                "dispatch_ceiling": 200.0,
                "confirm": False,
            },
        )
    assert exc_info.value.estimated_cost == 500.0
    assert exc_info.value.ceiling == 200.0
    assert "exceeds autonomous ceiling" in str(exc_info.value)


@pytest.mark.asyncio
async def test_under_ceiling_dispatch_autonomous_success(mock_pool):
    """An autonomous dispatch under DISPATCH_CEILING proceeds without confirm."""
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_row = {
        "id": UUID(_TICKET_ID),
        "namespace_id": UUID(_NAMESPACE_ID),
        "status": "open",
        "summary": "Display dead",
        "events": [],
    }

    mock_conn = AsyncMock()
    # 1. fetch ticket
    mock_conn.fetchrow.side_effect = [
        ticket_row,  # service_tickets lookup
        None,  # kg_edges existing check
        {"created_at": now_dt},  # kg_edges insert returning
    ]

    with (
        patch("nce.vertical_modules.support.dispatch.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.support.dispatch.assert_owner", new=AsyncMock()) as mock_owner,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_dispatch_work_order(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "estimated_cost": 150.0,
                "dispatch_ceiling": 200.0,
                "confirm": False,
            },
        )

        assert res["dispatched"] is True
        assert res["idempotent_replay"] is False
        assert res["ticket_id"] == _TICKET_ID
        assert "WORK_ORDER:" in res["edge"]
        assert "TICKET:" in res["edge"]
        # Assert ownership was asserted for TICKET and writer was support
        mock_owner.assert_awaited_once_with(mock_conn, UUID(_NAMESPACE_ID), "TICKET", "support")


@pytest.mark.asyncio
async def test_over_ceiling_dispatch_with_human_confirm_success(mock_pool):
    """An over-ceiling dispatch proceeds when human confirm override is True."""
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_row = {
        "id": UUID(_TICKET_ID),
        "namespace_id": UUID(_NAMESPACE_ID),
        "status": "open",
        "summary": "Audio rack smoke",
        "events": [],
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        ticket_row,
        None,
        {"created_at": now_dt},
    ]

    with (
        patch("nce.vertical_modules.support.dispatch.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.support.dispatch.assert_owner", new=AsyncMock()),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_dispatch_work_order(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "estimated_cost": 1500.0,
                "dispatch_ceiling": 200.0,
                "confirm": True,  # Human override
            },
        )

        assert res["dispatched"] is True
        assert res["idempotent_replay"] is False


# ---------------------------------------------------------------------------
# 2. Deterministic Idempotency & Edge Boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_idempotency_retry_creates_no_second_work_order(mock_pool):
    """A retried dispatch returns the existing work order and writes NO second edge."""
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    ticket_row = {
        "id": UUID(_TICKET_ID),
        "namespace_id": UUID(_NAMESPACE_ID),
        "status": "open",
        "summary": "Camera disconnected",
    }
    existing_wo_id = str(uuid4())
    existing_edge = {
        "object_label": f"WORK_ORDER:{existing_wo_id}",
        "created_at": now_dt,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        ticket_row,  # service_tickets lookup
        existing_edge,  # kg_edges existing check -> found!
    ]

    with (
        patch("nce.vertical_modules.support.dispatch.scoped_pg_session") as mock_scoped,
        patch("nce.vertical_modules.support.dispatch.assert_owner", new=AsyncMock()),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_dispatch_work_order(
            mock_pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "ticket_id": _TICKET_ID,
                "estimated_cost": 50.0,
                "dispatch_ceiling": 200.0,
            },
        )

        assert res["dispatched"] is True
        assert res["idempotent_replay"] is True
        assert res["work_order_id"] == existing_wo_id
        assert res["edge"] == f"TICKET:{_TICKET_ID} -[dispatched_as]-> WORK_ORDER:{existing_wo_id}"
        # Assert NO insert or update was executed on retry!
        assert mock_conn.execute.await_count == 0


def test_deterministic_key_and_work_order_id():
    """Deterministic keys must produce identical values across calls with identical inputs."""
    key1 = _derive_dispatch_idempotency_key(_NAMESPACE_ID, _TICKET_ID)
    key2 = _derive_dispatch_idempotency_key(_NAMESPACE_ID, _TICKET_ID)
    assert key1 == key2
    assert key1.startswith("dispatch:")

    wo1 = _derive_work_order_id(UUID(_NAMESPACE_ID), UUID(_TICKET_ID))
    wo2 = _derive_work_order_id(UUID(_NAMESPACE_ID), UUID(_TICKET_ID))
    assert wo1 == wo2


# ---------------------------------------------------------------------------
# 3. Domain Refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_missing_ticket_raises_not_found(mock_pool):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None

    with patch("nce.vertical_modules.support.dispatch.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        with pytest.raises(TicketNotFoundError) as exc_info:
            await do_dispatch_work_order(
                mock_pool,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "ticket_id": _TICKET_ID,
                    "estimated_cost": 10.0,
                    "dispatch_ceiling": 100.0,
                },
            )
        assert exc_info.value.ticket_id == _TICKET_ID


@pytest.mark.asyncio
async def test_dispatch_resolved_ticket_raises_invalid_status(mock_pool):
    ticket_row = {
        "id": UUID(_TICKET_ID),
        "namespace_id": UUID(_NAMESPACE_ID),
        "status": "resolved",
        "summary": "Already fixed",
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = ticket_row

    with patch("nce.vertical_modules.support.dispatch.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        with pytest.raises(InvalidTicketStatusError) as exc_info:
            await do_dispatch_work_order(
                mock_pool,
                {
                    "namespace_id": _NAMESPACE_ID,
                    "ticket_id": _TICKET_ID,
                    "estimated_cost": 10.0,
                    "dispatch_ceiling": 100.0,
                },
            )
        assert exc_info.value.status == "resolved"
