"""
tests/unit/test_support_tickets.py
==================================
Unit test suite for M10 Support Engine tickets core (ML10-B2):
  - require_support_enabled guard (opt-in gate)
  - do_open_ticket (ticket creation + initial SLA clock)
  - do_query_ticket (single ticket detail + multi-ticket listing)
  - do_resolve_ticket (resolution lifecycle + cognitive ledger recording)
  - Cross-tenant isolation swap-mutant tests (Charter §5.5 & §6.2)

Pure unit tests with mock asyncpg pool/connection (no live DB required).
"""

from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from asyncpg.exceptions import DataError

from nce.vertical_modules.support._guard import (
    SupportDisabledError,
    require_support_enabled,
)
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
    do_open_ticket,
    do_query_ticket,
    do_resolve_ticket,
)

_NS_A = "00000000-0000-4000-8000-000000000001"
_NS_B = "00000000-0000-4000-8000-000000000002"
_TICKET_ID_1 = "11111111-1111-4111-8111-111111111111"
_TICKET_ID_2 = "22222222-2222-4222-8222-222222222222"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    """Create a mock pool that yields *conn* on acquire."""
    pool = MagicMock()
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session with a pass-through for unit tests."""

    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        ctx = pool.acquire()
        if hasattr(ctx, "__aenter__"):
            conn = await ctx.__aenter__()
            try:
                yield conn
            finally:
                if hasattr(ctx, "__aexit__"):
                    await ctx.__aexit__(None, None, None)
        else:
            yield ctx

    monkeypatch.setattr(
        "nce.vertical_modules.support.tickets.scoped_pg_session",
        _fake_scoped,
    )


# ============================================================================
# 1. Guard Tests (require_support_enabled)
# ============================================================================


@pytest.mark.asyncio
async def test_guard_passes_when_support_enabled() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"support_enabled": True}
    pool = _make_mock_pool(conn)

    # Must not raise
    await require_support_enabled(pool, _NS_A)
    assert conn.fetchrow.called
    sql = conn.fetchrow.call_args[0][0]
    assert "metadata->'support'->>'enabled'" in sql
    assert "id = $1::uuid" in sql


@pytest.mark.asyncio
async def test_guard_raises_when_disabled_or_missing() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"support_enabled": False}
    pool = _make_mock_pool(conn)

    with pytest.raises(SupportDisabledError, match="Support vertical is not enabled"):
        await require_support_enabled(pool, _NS_A)

    conn.fetchrow.return_value = None
    with pytest.raises(SupportDisabledError, match="Support vertical is not enabled"):
        await require_support_enabled(pool, _NS_A)


@pytest.mark.asyncio
async def test_guard_catches_data_error_on_malformed_namespace() -> None:
    conn = AsyncMock()
    conn.fetchrow.side_effect = DataError("invalid input syntax for type uuid")
    pool = _make_mock_pool(conn)

    with pytest.raises(SupportDisabledError, match="Invalid namespace_id"):
        await require_support_enabled(pool, "not-a-uuid")


# ============================================================================
# 2. Open Ticket Tests (do_open_ticket)
# ============================================================================


@pytest.mark.asyncio
async def test_open_ticket_parameter_validations() -> None:
    pool = MagicMock()

    # Missing namespace_id
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_open_ticket(pool, {"summary": "Issue"})

    # Blank summary
    with pytest.raises(ValueError, match="summary is required"):
        await do_open_ticket(pool, {"namespace_id": _NS_A, "summary": "   "})

    # Invalid priority
    with pytest.raises(ValueError, match="priority must be one of"):
        await do_open_ticket(
            pool, {"namespace_id": _NS_A, "summary": "Issue", "priority": "urgent"}
        )

    # Invalid source
    with pytest.raises(ValueError, match="source must be one of"):
        await do_open_ticket(pool, {"namespace_id": _NS_A, "summary": "Issue", "source": "zendesk"})

    # Invalid change_origin
    with pytest.raises(ValueError, match="change_origin must be one of"):
        await do_open_ticket(
            pool, {"namespace_id": _NS_A, "summary": "Issue", "change_origin": "bot"}
        )


@pytest.mark.asyncio
async def test_open_ticket_success_and_sql_predicates() -> None:
    conn = AsyncMock()
    ticket_row = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "source": "nce",
        "source_id": None,
        "asset_id": None,
        "room_id": "ROOM-101",
        "customer_id": "CUST-99",
        "status": "open",
        "priority": "high",
        "summary": "Video wall flickering",
        "description": "Panel 3 loses signal intermittently",
        "sla_profile": "standard",
        "first_response_at": None,
        "resolved_at": None,
        "ai_diagnosis": {},
        "events": [{"type": "ticket_opened"}],
        "support_source_id": None,
        "change_origin": "agent",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
    }
    sla_row = {
        "ticket_id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "sla_profile": "standard",
        "first_response_due": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=2),
        "resolution_due": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=8),
        "breached": False,
        "breach_type": None,
        "paused_intervals": [],
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
    }

    conn.fetchrow.side_effect = [ticket_row, sla_row]
    pool = _make_mock_pool(conn)

    params = {
        "id": _TICKET_ID_1,
        "namespace_id": _NS_A,
        "summary": "Video wall flickering",
        "description": "Panel 3 loses signal intermittently",
        "priority": "high",
        "room_id": "ROOM-101",
        "customer_id": "CUST-99",
        "create_sla_clock": True,
    }

    res = await do_open_ticket(pool, params)
    assert res["ticket"]["id"] == _TICKET_ID_1
    assert res["ticket"]["status"] == "open"
    assert res["ticket"]["priority"] == "high"
    assert res["sla_clock"]["ticket_id"] == _TICKET_ID_1

    # Verify SQL calls carry namespace_id predicates
    assert conn.fetchrow.call_count == 2
    ticket_insert_sql = conn.fetchrow.call_args_list[0][0][0]
    assert "INSERT INTO service_tickets" in ticket_insert_sql
    assert "$2::uuid" in ticket_insert_sql or "namespace_id" in ticket_insert_sql
    # Check that namespace_id argument passed is UUID(_NS_A)
    assert conn.fetchrow.call_args_list[0][0][2] == UUID(_NS_A)

    sla_insert_sql = conn.fetchrow.call_args_list[1][0][0]
    assert "INSERT INTO sla_clocks" in sla_insert_sql
    assert conn.fetchrow.call_args_list[1][0][2] == UUID(_NS_A)


# ============================================================================
# 3. Query Ticket Tests (do_query_ticket)
# ============================================================================


@pytest.mark.asyncio
async def test_query_single_ticket_success() -> None:
    conn = AsyncMock()
    ticket_row = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "summary": "Video wall flickering",
        "status": "open",
        "priority": "high",
        "events": [],
    }
    sla_row = {
        "ticket_id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "sla_profile": "standard",
        "breached": False,
    }
    conn.fetchrow.side_effect = [ticket_row, sla_row]
    pool = _make_mock_pool(conn)

    res = await do_query_ticket(pool, {"namespace_id": _NS_A, "ticket_id": _TICKET_ID_1})
    assert res["ticket"]["id"] == _TICKET_ID_1
    assert res["sla_clock"]["sla_profile"] == "standard"

    # Strict namespace predicate check
    sql = conn.fetchrow.call_args_list[0][0][0]
    assert "WHERE id = $1::uuid AND namespace_id = $2::uuid" in sql
    assert conn.fetchrow.call_args_list[0][0][1] == UUID(_TICKET_ID_1)
    assert conn.fetchrow.call_args_list[0][0][2] == UUID(_NS_A)


@pytest.mark.asyncio
async def test_query_single_ticket_not_found() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool = _make_mock_pool(conn)

    with pytest.raises(TicketNotFoundError, match=f"ticket_id='{_TICKET_ID_1}'"):
        await do_query_ticket(pool, {"namespace_id": _NS_A, "ticket_id": _TICKET_ID_1})


@pytest.mark.asyncio
async def test_query_ticket_list_with_filters() -> None:
    conn = AsyncMock()
    rows = [
        {
            "id": UUID(_TICKET_ID_1),
            "namespace_id": UUID(_NS_A),
            "status": "open",
            "priority": "high",
            "summary": "Ticket 1",
            "events": [],
            "_full_count": 1,
        }
    ]
    conn.fetch.return_value = rows
    pool = _make_mock_pool(conn)

    res = await do_query_ticket(
        pool,
        {
            "namespace_id": _NS_A,
            "status": "open",
            "priority": "high",
            "limit": 10,
            "offset": 0,
        },
    )
    assert res["total"] == 1
    assert len(res["tickets"]) == 1
    assert res["tickets"][0]["id"] == _TICKET_ID_1

    sql = conn.fetch.call_args[0][0]
    assert "WHERE namespace_id = $1::uuid" in sql
    assert "status = $2" in sql
    assert "priority = $3" in sql
    assert "LIMIT $4 OFFSET $5" in sql


# ============================================================================
# 4. Resolve Ticket Tests (do_resolve_ticket)
# ============================================================================


@pytest.mark.asyncio
async def test_resolve_ticket_validation_and_ledger_append() -> None:
    conn = AsyncMock()
    existing_ticket = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "status": "open",
        "summary": "Touch panel unresponsive",
        "events": [],
        "asset_id": UUID("33333333-3333-4333-8333-333333333333"),
    }
    updated_ticket = {
        **existing_ticket,
        "status": "resolved",
        "resolved_at": datetime.datetime.now(datetime.timezone.utc),
        "events": [{"type": "ticket_resolved"}],
    }

    # Sequence of DB queries:
    # 1. SELECT for UPDATE
    # 2. UPDATE service_tickets RETURNING *
    # 3. UPDATE sla_clocks (optional)
    # 4. INSERT into v3_cognitive_ledger
    conn.fetchrow.side_effect = [existing_ticket, updated_ticket]
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    # Test blank resolution_text rejected
    with pytest.raises(ValueError, match="resolution_text is required"):
        await do_resolve_ticket(
            pool,
            {
                "namespace_id": _NS_A,
                "ticket_id": _TICKET_ID_1,
                "resolution_text": "   ",
            },
        )

    # Successful resolve
    res = await do_resolve_ticket(
        pool,
        {
            "namespace_id": _NS_A,
            "ticket_id": _TICKET_ID_1,
            "resolution_text": "Power-cycled the Crestron processor and reloaded firmware",
            "was_fix": True,
            "resolution_category": "firmware",
            "resolved_by": "tech_alice",
        },
    )

    assert res["status"] == "resolved"
    assert "ledger_id" in res

    # Verify v3_cognitive_ledger insert
    ledger_insert_call = next(
        (c for c in conn.execute.call_args_list if "INSERT INTO v3_cognitive_ledger" in c[0][0]),
        None,
    )
    assert ledger_insert_call is not None
    ledger_sql = ledger_insert_call[0][0]
    assert "INSERT INTO v3_cognitive_ledger" in ledger_sql
    assert "model_version" in ledger_sql

    # Verify parameters passed to cognitive ledger
    assert ledger_insert_call[0][2] == UUID(_NS_A)
    assert ledger_insert_call[0][3] == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # zero tensor
    payload = json.loads(ledger_insert_call[0][4])
    assert payload["event_type"] == "ticket_resolution"
    assert payload["ticket_id"] == _TICKET_ID_1
    assert payload["was_fix"] is True
    assert payload["resolution_category"] == "firmware"
    assert payload["resolved_by"] == "tech_alice"
    assert ledger_insert_call[0][5] == "support/v1"
    assert isinstance(ledger_insert_call[0][6], datetime.datetime)


@pytest.mark.asyncio
async def test_resolve_ticket_state_machine_errors() -> None:
    conn = AsyncMock()
    # Already resolved
    conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "status": "resolved",
        "summary": "Done ticket",
        "events": [],
    }
    pool = _make_mock_pool(conn)

    with pytest.raises(TicketAlreadyResolvedError, match="is already resolved"):
        await do_resolve_ticket(
            pool,
            {
                "namespace_id": _NS_A,
                "ticket_id": _TICKET_ID_1,
                "resolution_text": "Trying to resolve again",
            },
        )

    # Cancelled ticket
    conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "status": "cancelled",
        "summary": "Cancelled ticket",
        "events": [],
    }
    with pytest.raises(InvalidTicketStatusError, match="cannot be resolved"):
        await do_resolve_ticket(
            pool,
            {
                "namespace_id": _NS_A,
                "ticket_id": _TICKET_ID_1,
                "resolution_text": "Trying to resolve cancelled",
            },
        )


# ============================================================================
# 5. Cross-Tenant Swap-Mutant Tests (Charter §5.5 & §6.2)
# ============================================================================


@pytest.mark.asyncio
async def test_cross_tenant_query_swap_mutant() -> None:
    """Proves that Tenant B cannot read Tenant A's ticket even knowing ticket_id.

    The mock connection models PostgreSQL evaluating:
      WHERE id = $1::uuid AND namespace_id = $2::uuid
    When Tenant B ($2 = _NS_B) looks up Tenant A's ticket (whose namespace_id is _NS_A),
    Postgres returns NULL.
    """
    conn = AsyncMock()

    # Emulate DB predicate behavior:
    def _mock_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "service_tickets" in sql and "WHERE id = $1::uuid AND namespace_id = $2::uuid" in sql:
            ticket_id, ns_id = args[0], args[1]
            if ticket_id == UUID(_TICKET_ID_1) and ns_id == UUID(_NS_A):
                return {
                    "id": UUID(_TICKET_ID_1),
                    "namespace_id": UUID(_NS_A),
                    "summary": "Secret ticket of Tenant A",
                    "status": "open",
                    "events": [],
                }
            # Swap mutant: Tenant B passes _NS_B -> row does NOT match predicate
            return None
        return None

    conn.fetchrow.side_effect = _mock_fetchrow
    pool = _make_mock_pool(conn)

    # Tenant A succeeds
    res_a = await do_query_ticket(pool, {"namespace_id": _NS_A, "ticket_id": _TICKET_ID_1})
    assert res_a["ticket"]["id"] == _TICKET_ID_1

    # Tenant B fails with TicketNotFoundError (isolated)
    with pytest.raises(TicketNotFoundError, match=f"ticket_id='{_TICKET_ID_1}'"):
        await do_query_ticket(pool, {"namespace_id": _NS_B, "ticket_id": _TICKET_ID_1})


@pytest.mark.asyncio
async def test_cross_tenant_resolve_swap_mutant() -> None:
    """Proves that Tenant B cannot resolve Tenant A's ticket."""
    conn = AsyncMock()

    def _mock_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "service_tickets" in sql and "WHERE id = $1::uuid AND namespace_id = $2::uuid" in sql:
            ticket_id, ns_id = args[0], args[1]
            if ticket_id == UUID(_TICKET_ID_1) and ns_id == UUID(_NS_A):
                return {
                    "id": UUID(_TICKET_ID_1),
                    "namespace_id": UUID(_NS_A),
                    "status": "open",
                    "summary": "Tenant A ticket",
                    "events": [],
                    "asset_id": None,
                }
            return None
        return None

    conn.fetchrow.side_effect = _mock_fetchrow
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = _make_mock_pool(conn)

    with pytest.raises(TicketNotFoundError):
        await do_resolve_ticket(
            pool,
            {
                "namespace_id": _NS_B,
                "ticket_id": _TICKET_ID_1,
                "resolution_text": "Tenant B trying to mutate Tenant A ticket",
            },
        )
