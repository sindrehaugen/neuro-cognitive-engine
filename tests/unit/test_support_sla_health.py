"""
tests/unit/test_support_sla_health.py
=====================================
Unit test suite for M10 Support Engine SLA clocks & customer health (ML10-B3):
  - Deterministic SLA calculations and room profiles in sla.py
  - do_sla_clock: countdowns, pause intervals, breach risk detection
  - Customer health passive scoring and churn risk in health.py
  - do_record_touchpoint: ÉT-spørsmål capture and cognitive ledger append
  - Cross-tenant isolation swap-mutant tests (Charter §5.5 & §6.2)

Pure unit tests with mock asyncpg pool/connection.
"""

from __future__ import annotations

import datetime
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from nce.vertical_modules.support.health import (
    compute_health_score,
    do_health_score,
    do_record_touchpoint,
    load_health_weights,
)
from nce.vertical_modules.support.sla import (
    calculate_sla_targets,
    do_sla_clock,
    evaluate_sla_status,
    load_sla_profiles,
)
from nce.vertical_modules.support.tickets import TicketNotFoundError

_NS_A = "00000000-0000-4000-8000-000000000001"
_NS_B = "00000000-0000-4000-8000-000000000002"
_TICKET_ID_1 = "11111111-1111-4111-8111-111111111111"
_CUST_1 = "CUST-42"


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
    """Replace scoped_pg_session in both sla and health modules."""

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
        "nce.vertical_modules.support.sla.scoped_pg_session",
        _fake_scoped,
    )
    monkeypatch.setattr(
        "nce.vertical_modules.support.health.scoped_pg_session",
        _fake_scoped,
    )


# ============================================================================
# 1. Deterministic SLA Profile & Math Tests
# ============================================================================


def test_load_sla_profiles() -> None:
    profiles = load_sla_profiles()
    assert "standard" in profiles
    assert "mission_critical" in profiles
    assert "critical" in profiles["mission_critical"]
    assert profiles["mission_critical"]["critical"]["resolution_hours"] <= 4.0


def test_calculate_sla_targets() -> None:
    base = datetime.datetime(2026, 9, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)
    resp_due, res_due = calculate_sla_targets("standard", "critical", base)
    assert resp_due == base + datetime.timedelta(hours=1.0)
    assert res_due == base + datetime.timedelta(hours=4.0)


def test_evaluate_sla_status_within_targets() -> None:
    now = datetime.datetime(2026, 9, 4, 12, 30, 0, tzinfo=datetime.timezone.utc)
    first_due = datetime.datetime(2026, 9, 4, 13, 0, 0, tzinfo=datetime.timezone.utc)
    res_due = datetime.datetime(2026, 9, 4, 16, 0, 0, tzinfo=datetime.timezone.utc)

    status = evaluate_sla_status(
        first_response_due=first_due,
        resolution_due=res_due,
        first_response_at=None,
        resolved_at=None,
        paused_intervals=[],
        now=now,
    )
    assert status["breached"] is False
    assert status["breach_type"] is None
    assert status["remaining_first_response_seconds"] == 1800.0
    assert status["remaining_resolution_seconds"] == 12600.0


def test_evaluate_sla_status_breached_first_response() -> None:
    now = datetime.datetime(2026, 9, 4, 13, 30, 0, tzinfo=datetime.timezone.utc)
    first_due = datetime.datetime(2026, 9, 4, 13, 0, 0, tzinfo=datetime.timezone.utc)
    res_due = datetime.datetime(2026, 9, 4, 16, 0, 0, tzinfo=datetime.timezone.utc)

    status = evaluate_sla_status(
        first_response_due=first_due,
        resolution_due=res_due,
        first_response_at=None,
        resolved_at=None,
        paused_intervals=[],
        now=now,
    )
    assert status["breached"] is True
    assert status["breach_type"] == "first_response"


def test_evaluate_sla_status_with_paused_intervals() -> None:
    now = datetime.datetime(2026, 9, 4, 13, 30, 0, tzinfo=datetime.timezone.utc)
    first_due = datetime.datetime(2026, 9, 4, 13, 0, 0, tzinfo=datetime.timezone.utc)
    res_due = datetime.datetime(2026, 9, 4, 16, 0, 0, tzinfo=datetime.timezone.utc)

    # 45 minutes paused waiting for customer
    paused = [
        {
            "paused_at": "2026-09-04T12:15:00+00:00",
            "resumed_at": "2026-09-04T13:00:00+00:00",
            "duration_seconds": 2700,
        }
    ]

    status = evaluate_sla_status(
        first_response_due=first_due,
        resolution_due=res_due,
        first_response_at=None,
        resolved_at=None,
        paused_intervals=paused,
        now=now,
    )
    # Effective elapsed time is reduced by 2700s, so deadline effectively pushed by 45 mins!
    # At 13:30, normal elapsed is 1h30m, effective elapsed is 45m -> NOT breached!
    assert status["breached"] is False
    assert status["paused_seconds"] == 2700


# ============================================================================
# 2. do_sla_clock Core Function Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_sla_clock_success_and_db_update() -> None:
    conn = AsyncMock()
    ticket_row = {
        "id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "status": "open",
        "priority": "high",
        "first_response_at": None,
        "resolved_at": None,
        "created_at": datetime.datetime(2026, 9, 4, 10, 0, 0, tzinfo=datetime.timezone.utc),
    }
    sla_row = {
        "ticket_id": UUID(_TICKET_ID_1),
        "namespace_id": UUID(_NS_A),
        "sla_profile": "standard",
        "first_response_due": datetime.datetime(2026, 9, 4, 12, 0, 0, tzinfo=datetime.timezone.utc),
        "resolution_due": datetime.datetime(2026, 9, 4, 18, 0, 0, tzinfo=datetime.timezone.utc),
        "breached": False,
        "breach_type": None,
        "paused_intervals": [],
    }

    conn.fetchrow.side_effect = [ticket_row, sla_row]
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = _make_mock_pool(conn)

    now = datetime.datetime(2026, 9, 4, 13, 0, 0, tzinfo=datetime.timezone.utc)
    res = await do_sla_clock(
        pool,
        {
            "namespace_id": _NS_A,
            "ticket_id": _TICKET_ID_1,
            "now": now,
        },
    )

    assert res["ticket_id"] == _TICKET_ID_1
    assert res["breached"] is True
    assert res["breach_type"] == "first_response"

    # Verify strict namespace_id predicate in fetch
    fetch_sql = conn.fetchrow.call_args_list[0][0][0]
    assert "WHERE id = $1::uuid AND namespace_id = $2::uuid" in fetch_sql


@pytest.mark.asyncio
async def test_do_sla_clock_cross_tenant_isolation() -> None:
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # Not found for other namespace
    pool = _make_mock_pool(conn)

    with pytest.raises(TicketNotFoundError):
        await do_sla_clock(
            pool,
            {
                "namespace_id": _NS_B,
                "ticket_id": _TICKET_ID_1,
            },
        )


# ============================================================================
# 3. Customer Health Calculation & do_health_score Tests
# ============================================================================


def test_load_health_weights() -> None:
    weights = load_health_weights()
    assert "ticket_cadence_weight" in weights
    assert "sla_breach_weight" in weights
    total_weights = sum(weights.values())
    assert abs(total_weights - 1.0) < 1e-4


def test_compute_health_score_clean_customer() -> None:
    res = compute_health_score(
        ticket_count=0,
        recent_ticket_count=0,
        breach_count=0,
        frustration_score=0.0,
        touchpoint_avg=None,
        previous_score=None,
    )
    assert res["score"] == 100.0
    assert res["churn_risk"] == "low"
    assert res["trend"]["direction"] == "stable"


def test_compute_health_score_degraded_customer() -> None:
    res = compute_health_score(
        ticket_count=8,
        recent_ticket_count=4,
        breach_count=2,
        frustration_score=0.8,
        touchpoint_avg=20.0,
        previous_score=85.0,
    )
    assert res["score"] < 60.0
    assert res["churn_risk"] in ("medium", "high")
    assert res["trend"]["direction"] == "degrading"
    assert len(res["drivers"]) >= 2


@pytest.mark.asyncio
async def test_do_health_score_upsert_success() -> None:
    conn = AsyncMock()
    # 1. fetch tickets for customer
    tickets = [
        {
            "id": UUID(_TICKET_ID_1),
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }
    ]
    conn.fetch.side_effect = [
        tickets,  # service_tickets
        [],  # breached sla_clocks
    ]
    # previous health row: None
    upserted_row = {
        "customer_id": _CUST_1,
        "namespace_id": UUID(_NS_A),
        "score": 92.5,
        "trend": {"direction": "stable"},
        "churn_risk": "low",
        "drivers": [],
        "last_touchpoint_at": None,
        "computed_at": datetime.datetime.now(datetime.timezone.utc),
    }
    conn.fetchrow.side_effect = [None, upserted_row]
    pool = _make_mock_pool(conn)

    res = await do_health_score(pool, {"namespace_id": _NS_A, "customer_id": _CUST_1})
    assert res["customer_id"] == _CUST_1
    assert res["score"] == 92.5
    assert res["churn_risk"] == "low"

    # Verify SQL carries namespace_id predicate
    tickets_query_sql = conn.fetch.call_args_list[0][0][0]
    assert "WHERE namespace_id = $1::uuid" in tickets_query_sql
    assert "customer_id = $2" in tickets_query_sql


# ============================================================================
# 4. do_record_touchpoint Tests
# ============================================================================


@pytest.mark.asyncio
async def test_do_record_touchpoint_success() -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 1")
    # Touchpoint re-triggers health score
    conn.fetch.side_effect = [[], []]
    upserted_row = {
        "customer_id": _CUST_1,
        "namespace_id": UUID(_NS_A),
        "score": 98.0,
        "trend": {"direction": "stable"},
        "churn_risk": "low",
        "drivers": [],
        "last_touchpoint_at": datetime.datetime.now(datetime.timezone.utc),
        "computed_at": datetime.datetime.now(datetime.timezone.utc),
    }
    conn.fetchrow.side_effect = [None, upserted_row]
    pool = _make_mock_pool(conn)

    res = await do_record_touchpoint(
        pool,
        {
            "namespace_id": _NS_A,
            "customer_id": _CUST_1,
            "question_id": "et_sporsmal_v1",
            "answer": "Alt fungerer utmerket!",
            "score": 95.0,
        },
    )
    assert res["ok"] is True
    assert res["customer_id"] == _CUST_1

    # Verify cognitive ledger write
    ledger_call = conn.execute.call_args
    assert "INSERT INTO v3_cognitive_ledger" in ledger_call[0][0]
    assert ledger_call[0][2] == UUID(_NS_A)
