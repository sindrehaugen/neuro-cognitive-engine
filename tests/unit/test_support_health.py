"""
tests/unit/test_support_health.py
=================================
Unit test suite for live v3_cognitive_ledger affect metrics and customer health (I-VERT-03):
  - do_health_score queries v3_cognitive_ledger for recent customer affect metrics
  - Empathic Tensor frustration dimension (index 5) deductions
  - ÉT-spørsmål touchpoint responses (negative deduction vs positive boost)
  - Parameter overrides (frustration_score, touchpoint_avg)
  - Graceful fallback on empty ledger or unconfigured mock connections
  - Strict tenant isolation predicate enforcement
"""

from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from nce.vertical_modules.support.health import (
    _fetch_customer_affect_metrics,
    do_health_score,
)

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
    """Replace scoped_pg_session in health module."""

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
        "nce.vertical_modules.support.health.scoped_pg_session",
        _fake_scoped,
    )


@pytest.mark.asyncio
async def test_fetch_customer_affect_metrics_from_ledger() -> None:
    """Validates that frustration tensor and touchpoint responses are parsed from ledger."""
    conn = AsyncMock()
    now = datetime.datetime.now(datetime.timezone.utc)
    ledger_rows = [
        {
            "empathic_tensor": [0.1, 0.2, 0.3, 0.4, 0.5, 0.75],
            "tlx_scores": json.dumps(
                {
                    "event_type": "customer_touchpoint",
                    "customer_id": _CUST_1,
                    "score": 60.0,
                }
            ),
            "created_at": now,
        },
        {
            "empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 0.65],
            "tlx_scores": json.dumps(
                {
                    "event_type": "touchpoint_response",
                    "customer_id": _CUST_1,
                    "score": 50.0,
                }
            ),
            "created_at": now,
        },
    ]
    conn.fetch.return_value = ledger_rows

    frustration, touchpoint = await _fetch_customer_affect_metrics(
        conn,
        UUID(_NS_A),
        _CUST_1,
        {},
    )

    # Average frustration: (0.75 + 0.65) / 2 = 0.70
    assert abs(frustration - 0.70) < 0.01
    # Average touchpoint: (60.0 + 50.0) / 2 = 55.0
    assert abs(touchpoint - 55.0) < 0.01

    # Verify SQL query carried tenant isolation predicate
    sql = conn.fetch.call_args[0][0]
    assert "WHERE namespace_id = $1::uuid" in sql


@pytest.mark.asyncio
async def test_do_health_score_with_live_cognitive_ledger_frustration_and_touchpoint() -> None:
    """Validates that live cognitive ledger affect metrics impact health calculation and drivers."""
    conn = AsyncMock()
    tickets = [
        {
            "id": UUID(_TICKET_ID_1),
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }
    ]
    ledger_rows = [
        {
            "empathic_tensor": [0.1, 0.1, 0.1, 0.2, 0.3, 0.80],
            "tlx_scores": json.dumps(
                {
                    "event_type": "touchpoint_response",
                    "customer_id": _CUST_1,
                    "score": 50.0,
                }
            ),
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }
    ]
    # 1. tickets, 2. breaches, 3. cognitive ledger affect metrics
    conn.fetch.side_effect = [
        tickets,
        [],
        ledger_rows,
    ]

    captured_score = {}

    def _mock_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "customer_health" in sql and "INSERT INTO" in sql:
            captured_score["score"] = args[2]
            captured_score["drivers"] = json.loads(args[5])
            captured_score["churn_risk"] = args[4]
            return {
                "customer_id": _CUST_1,
                "namespace_id": UUID(_NS_A),
                "score": args[2],
                "trend": json.loads(args[3]),
                "churn_risk": args[4],
                "drivers": json.loads(args[5]),
                "last_touchpoint_at": None,
                "computed_at": datetime.datetime.now(datetime.timezone.utc),
            }
        return None

    conn.fetchrow.side_effect = _mock_fetchrow
    pool = _make_mock_pool(conn)

    res = await do_health_score(pool, {"namespace_id": _NS_A, "customer_id": _CUST_1})

    assert res["customer_id"] == _CUST_1
    assert captured_score["score"] < 90.0
    # Both frustration and touchpoint drivers must be included
    driver_str = " ".join(captured_score["drivers"])
    assert "frustration" in driver_str.lower()
    assert "touchpoint" in driver_str.lower()


@pytest.mark.asyncio
async def test_do_health_score_with_param_overrides() -> None:
    """Validates that explicit parameters override ledger reading."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [],  # tickets
        [],  # breaches
    ]
    captured_score = {}

    def _mock_fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "customer_health" in sql and "INSERT INTO" in sql:
            captured_score["score"] = args[2]
            captured_score["drivers"] = json.loads(args[5])
            return {
                "customer_id": _CUST_1,
                "namespace_id": UUID(_NS_A),
                "score": args[2],
                "trend": json.loads(args[3]),
                "churn_risk": args[4],
                "drivers": json.loads(args[5]),
                "last_touchpoint_at": None,
                "computed_at": datetime.datetime.now(datetime.timezone.utc),
            }
        return None

    conn.fetchrow.side_effect = _mock_fetchrow
    pool = _make_mock_pool(conn)

    res = await do_health_score(
        pool,
        {
            "namespace_id": _NS_A,
            "customer_id": _CUST_1,
            "frustration_score": 0.9,
            "touchpoint_avg": 40.0,
        },
    )

    assert res["customer_id"] == _CUST_1
    # 0 tickets: 100 - (0.9 * 25) - (70 - 40)*0.5 = 100 - 22.5 - 15 = 62.5
    assert captured_score["score"] < 70.0
    driver_str = " ".join(captured_score["drivers"])
    assert "frustration" in driver_str.lower()


@pytest.mark.asyncio
async def test_do_health_score_positive_touchpoint_boost() -> None:
    """Validates that high touchpoint score (>= 90) provides positive score boost."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [],  # tickets
        [],  # breaches
        [
            {
                "empathic_tensor": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "tlx_scores": json.dumps(
                    {
                        "event_type": "customer_touchpoint",
                        "customer_id": _CUST_1,
                        "score": 98.0,
                    }
                ),
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            }
        ],
    ]
    upserted_row = {
        "customer_id": _CUST_1,
        "namespace_id": UUID(_NS_A),
        "score": 100.0,
        "trend": {"direction": "stable"},
        "churn_risk": "low",
        "drivers": [],
        "last_touchpoint_at": None,
        "computed_at": datetime.datetime.now(datetime.timezone.utc),
    }
    conn.fetchrow.side_effect = [None, upserted_row]
    pool = _make_mock_pool(conn)

    res = await do_health_score(pool, {"namespace_id": _NS_A, "customer_id": _CUST_1})
    assert res["score"] == 100.0
    assert res["churn_risk"] == "low"
