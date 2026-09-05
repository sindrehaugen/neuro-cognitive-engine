"""
tests/unit/test_support_troubleshoot.py
=======================================
Unit test suite for M10 Support Engine AI Troubleshooter & Triage (ML10-B4):
  - do_troubleshoot: cognitive recall over v3_cognitive_ledger & memories
  - Zero-history honest fallback (confidence 0.0, no hallucinated fixes)
  - Structured resolution citations (Contract-H)
  - do_triage_ticket: skill matching, room criticality, priority recommendations
  - Cross-tenant isolation swap-mutant tests (Charter §5.5 & §6.2)

Pure unit tests with mock asyncpg pool/connection.
"""

from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from nce.vertical_modules.support.tickets import TicketNotFoundError
from nce.vertical_modules.support.triage import do_triage_ticket
from nce.vertical_modules.support.troubleshoot import do_troubleshoot

_NS_A = "00000000-0000-4000-8000-000000000001"
_NS_B = "00000000-0000-4000-8000-000000000002"
_TICKET_ID_1 = "11111111-1111-4111-8111-111111111111"
_PAST_TICKET_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session in troubleshoot and triage modules."""

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
        "nce.vertical_modules.support.troubleshoot.scoped_pg_session",
        _fake_scoped,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.support.triage.scoped_pg_session",
        _fake_scoped,
    )


# ============================================================================
# 1. Troubleshooter Zero-History & Recall Tests
# ============================================================================


@pytest.mark.asyncio
async def test_troubleshoot_zero_history_honest_fallback() -> None:
    """When tenant has no past resolutions in ledger, return honest fallback."""
    conn = AsyncMock()
    conn.fetch.return_value = []  # No cognitive ledger rows
    pool = _make_mock_pool(conn)

    res = await do_troubleshoot(
        pool,
        {
            "namespace_id": _NS_A,
            "symptom_text": "NVX video stream stuttering on display 2",
        },
    )

    assert res["confidence"] == 0.0
    assert res["cited_ticket_ids"] == []
    assert res["sources_count"] == 0
    assert res["proposed_fix"] is None
    assert "No matching past resolution patterns found" in res["diagnosis"]


@pytest.mark.asyncio
async def test_troubleshoot_with_past_structured_resolution() -> None:
    """When tenant has prior matching resolution in v3_cognitive_ledger, cite it."""
    conn = AsyncMock()
    past_resolution_row = {
        "id": UUID("99999999-9999-4999-8999-999999999999"),
        "namespace_id": UUID(_NS_A),
        "tlx_scores": json.dumps(
            {
                "event_type": "ticket_resolution",
                "ticket_id": _PAST_TICKET_A,
                "summary": "NVX video stream stuttering",
                "resolution_text": "Downgraded NVX endpoint firmware to v2.1.4 and disabled IGMP snooping fast-leave",
                "was_fix": True,
                "resolution_category": "firmware",
                "fixed_asset_id": "33333333-3333-4333-8333-333333333333",
            }
        ),
        "created_at": datetime.datetime(2026, 8, 1, 10, 0, 0, tzinfo=datetime.timezone.utc),
    }
    conn.fetch.return_value = [past_resolution_row]
    pool = _make_mock_pool(conn)

    res = await do_troubleshoot(
        pool,
        {
            "namespace_id": _NS_A,
            "symptom_text": "NVX video stream stuttering on display 2",
            "asset_id": "33333333-3333-4333-8333-333333333333",
        },
    )

    assert res["confidence"] >= 0.70
    assert _PAST_TICKET_A in res["cited_ticket_ids"]
    assert res["sources_count"] == 1
    assert "Downgraded NVX endpoint firmware" in res["proposed_fix"]

    # Verify SQL query carried namespace_id predicate
    query_sql = conn.fetch.call_args[0][0]
    assert "WHERE namespace_id = $1::uuid" in query_sql


@pytest.mark.asyncio
async def test_troubleshoot_by_ticket_id_lookup() -> None:
    """When ticket_id is provided, look up ticket then find resolutions."""
    conn = AsyncMock()
    ticket_row = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "summary": "Crestron touch panel blank",
        "description": "TSW-1070 screen black, PoE LED flashing amber",
        "asset_id": None,
    }
    past_resolution_row = {
        "id": UUID("99999999-9999-4999-8999-999999999999"),
        "namespace_id": UUID(_NS_A),
        "tlx_scores": json.dumps(
            {
                "event_type": "ticket_resolution",
                "ticket_id": _PAST_TICKET_A,
                "summary": "Touch panel black screen PoE amber",
                "resolution_text": "Power-cycled PoE switch port and re-seated Cat6 RJ45 termination",
                "was_fix": True,
                "resolution_category": "hardware",
            }
        ),
        "created_at": datetime.datetime(2026, 8, 15, 10, 0, 0, tzinfo=datetime.timezone.utc),
    }

    conn.fetchrow.return_value = ticket_row
    conn.fetch.return_value = [past_resolution_row]
    pool = _make_mock_pool(conn)

    res = await do_troubleshoot(
        pool,
        {
            "namespace_id": _NS_A,
            "ticket_id": _TICKET_ID_1,
        },
    )

    assert res["confidence"] >= 0.60
    assert _PAST_TICKET_A in res["cited_ticket_ids"]
    assert "PoE switch port" in res["proposed_fix"]


@pytest.mark.asyncio
async def test_troubleshoot_cross_tenant_swap_mutant() -> None:
    """Proves that Tenant B cannot recall Tenant A's resolution from v3_cognitive_ledger."""
    conn = AsyncMock()

    # DB returns Tenant A's resolution ONLY when namespace_id matches _NS_A
    def _mock_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "v3_cognitive_ledger" in sql and "namespace_id = $1::uuid" in sql:
            ns_id = args[0]
            if ns_id == UUID(_NS_A):
                return [
                    {
                        "id": UUID("99999999-9999-4999-8999-999999999999"),
                        "namespace_id": UUID(_NS_A),
                        "tlx_scores": json.dumps(
                            {
                                "event_type": "ticket_resolution",
                                "ticket_id": _PAST_TICKET_A,
                                "summary": "NVX video stream stuttering",
                                "resolution_text": "Tenant A proprietary fix",
                                "was_fix": True,
                            }
                        ),
                        "created_at": datetime.datetime(
                            2026, 8, 1, 10, 0, 0, tzinfo=datetime.timezone.utc
                        ),
                    }
                ]
            # Swap mutant: Tenant B passes _NS_B -> 0 rows match
            return []
        return []

    conn.fetch.side_effect = _mock_fetch
    pool = _make_mock_pool(conn)

    # Tenant A sees the fix
    res_a = await do_troubleshoot(
        pool,
        {
            "namespace_id": _NS_A,
            "symptom_text": "NVX video stream stuttering",
        },
    )
    assert res_a["confidence"] > 0.0
    assert _PAST_TICKET_A in res_a["cited_ticket_ids"]

    # Tenant B gets zero leakage (honest fallback)
    res_b = await do_troubleshoot(
        pool,
        {
            "namespace_id": _NS_B,
            "symptom_text": "NVX video stream stuttering",
        },
    )
    assert res_b["confidence"] == 0.0
    assert res_b["cited_ticket_ids"] == []
    assert res_b["proposed_fix"] is None


# ============================================================================
# 2. Ticket Triage Tests (do_triage_ticket)
# ============================================================================


@pytest.mark.asyncio
async def test_triage_executive_boardroom_offline() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "summary": "Entire video wall offline in Executive Boardroom",
        "description": "Board meeting in 30 minutes, display matrix not responding",
        "room_id": "BOARDROOM-1",
        "priority": "medium",
    }
    pool = _make_mock_pool(conn)

    res = await do_triage_ticket(
        pool,
        {
            "namespace_id": _NS_A,
            "ticket_id": _TICKET_ID_1,
        },
    )

    assert res["ticket_id"] == _TICKET_ID_1
    assert res["recommended_priority"] in ("high", "critical")
    assert res["suggested_skill"] in ("video_specialist", "systems_engineer")
    assert res["urgency"] == "critical"
    assert "Boardroom" in res["urgency_reason"] or "meeting" in res["urgency_reason"].lower()


@pytest.mark.asyncio
async def test_triage_audio_microphone_feedback() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "summary": "Wireless microphone loud screeching feedback",
        "description": "Shure wireless mic causing feedback loop through ceiling speakers",
        "room_id": "CONF-202",
        "priority": "low",
    }
    pool = _make_mock_pool(conn)

    res = await do_triage_ticket(
        pool,
        {
            "namespace_id": _NS_A,
            "ticket_id": _TICKET_ID_1,
        },
    )

    assert res["suggested_skill"] == "audio_specialist"


@pytest.mark.asyncio
async def test_triage_cross_tenant_isolation() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # Missing for Tenant B
    pool = _make_mock_pool(conn)

    with pytest.raises(TicketNotFoundError):
        await do_triage_ticket(
            pool,
            {
                "namespace_id": _NS_B,
                "ticket_id": _TICKET_ID_1,
            },
        )
