"""Unit tests for Module 15 Travel, Norwegian Diett, and RS-5 Contract-B Spend Gate."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources._guard import (
    ResourceValidationError,
)
from nce.vertical_modules.resources.travel import (
    calculate_norwegian_diett,
    do_plan_travel,
    load_travel_policy,
)


class MockEngine:
    """Mock engine holding in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.allocations: dict[str, dict[str, Any]] = {}
        self.travel_legs: dict[str, dict[str, Any]] = {}
        self.stays: dict[str, dict[str, Any]] = {}
        self.per_diems: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "from allocations" in q and "where id = $1 and namespace_id = $2" in q:
            alloc_id, ns_id = str(args[0]), str(args[1])
            for a in engine.allocations.values():
                if str(a["id"]) == alloc_id and str(a["namespace_id"]) == ns_id:
                    return a
            return None

        if "from travel_legs" in q and "attrs->>'idempotency_key' = $2" in q:
            ns_id = str(args[0])
            idem = str(args[1])
            for leg in engine.travel_legs.values():
                if (
                    str(leg["namespace_id"]) == ns_id
                    and leg.get("attrs", {}).get("idempotency_key") == idem
                ):
                    return leg
            return None

        return None

    async def mock_execute(query, *args):
        q = query.strip().lower()
        if "insert into travel_legs" in q:
            row = {
                "id": str(args[0]),
                "namespace_id": str(args[1]),
                "allocation_id": str(args[2]),
                "origin": args[3],
                "destination": args[4],
                "departure_at": args[5],
                "arrival_at": args[6],
                "mode": args[7],
                "cost_nok": args[8],
                "booking_ref": args[9],
                "attrs": json.loads(args[10]) if isinstance(args[10], str) else args[10],
            }
            engine.travel_legs[str(row["id"])] = row
            return "INSERT 0 1"

        if "insert into stays" in q:
            row = {
                "id": str(args[0]),
                "namespace_id": str(args[1]),
                "allocation_id": str(args[2]),
                "location": args[3],
                "check_in": args[4],
                "check_out": args[5],
                "cost_nok": args[6],
                "booking_ref": args[7],
                "attrs": json.loads(args[8]) if isinstance(args[8], str) else args[8],
            }
            engine.stays[str(row["id"])] = row
            return "INSERT 0 1"

        if "insert into per_diems" in q:
            row = {
                "id": str(args[0]),
                "namespace_id": str(args[1]),
                "allocation_id": str(args[2]),
                "date": args[3],
                "rate_nok": args[4],
                "diet_type": args[5],
                "meals_provided": json.loads(args[6]) if isinstance(args[6], str) else args[6],
                "attrs": json.loads(args[7]) if isinstance(args[7], str) else args[7],
            }
            engine.per_diems[str(row["id"])] = row
            return "INSERT 0 1"

        return ""

    class MockConn:
        async def fetchrow(self, query, *args):
            return await mock_fetchrow(query, *args)

        async def execute(self, query, *args):
            return await mock_execute(query, *args)

    class MockContextManager:
        async def __aenter__(self):
            return MockConn()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    import json

    monkeypatch.setattr(
        "nce.vertical_modules.resources.travel.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


def test_load_travel_policy():
    """Verify loading Norwegian travel policy config."""
    policy = load_travel_policy()
    assert policy["jurisdiction"] == "NO"
    assert "statutory_rates_2026" in policy["rules"]


def test_calculate_norwegian_diett_statutory():
    """Verify Norwegian statutory per-diem and meal deductions (Statens satser)."""
    # 1. Full overnight hotel without meals: 940 NOK
    d1 = calculate_norwegian_diett("2026-09-20", "overnight_hotel", {})
    assert d1["net_rate_nok"] == 940.0
    assert d1["jurisdiction"] == "NO"

    # 2. Breakfast provided: -20% (188 NOK) -> 752 NOK
    d2 = calculate_norwegian_diett("2026-09-20", "overnight_hotel", {"breakfast": True})
    assert d2["net_rate_nok"] == 752.0
    assert d2["deductions_nok"]["breakfast"] == 188.0

    # 3. Breakfast + Lunch + Dinner provided: -100% -> 0.0 NOK
    d3 = calculate_norwegian_diett(
        "2026-09-20",
        "overnight_hotel",
        {"breakfast": True, "lunch": True, "dinner": True},
    )
    assert d3["net_rate_nok"] == 0.0

    # 4. Day trip 6-12h: 360 NOK
    d4 = calculate_norwegian_diett("2026-09-20", "day_trip_short_6_to_12h", {})
    assert d4["net_rate_nok"] == 360.0

    # 5. Day trip >12h: 680 NOK
    d5 = calculate_norwegian_diett("2026-09-20", "day_trip_long_over_12h", {})
    assert d5["net_rate_nok"] == 680.0


@pytest.mark.asyncio
async def test_do_plan_travel_advisor_mode(mock_engine):
    """Action 'plan' computes itinerary and costs without booking or requiring idempotency."""
    ns_id = uuid4()
    alloc_id = uuid4()
    mock_engine.allocations[str(alloc_id)] = {
        "id": str(alloc_id),
        "namespace_id": str(ns_id),
        "resource_id": str(uuid4()),
        "status": "reserved",
    }

    plan = await do_plan_travel(
        mock_engine,
        {
            "namespace_id": ns_id,
            "allocation_id": alloc_id,
            "action": "plan",
            "itinerary": {
                "travel_legs": [
                    {
                        "origin": "Oslo",
                        "destination": "Bergen",
                        "departure_at": "2026-09-21T07:00:00Z",
                        "mode": "flight",
                        "cost_nok": 1850.0,
                    }
                ],
                "stays": [
                    {
                        "location": "Bergen Hotel",
                        "check_in": "2026-09-21T14:00:00Z",
                        "check_out": "2026-09-22T10:00:00Z",
                        "cost_nok": 1950.0,
                    }
                ],
                "per_diems": [
                    {
                        "date": "2026-09-21",
                        "diet_type": "overnight_hotel",
                        "meals_provided": {"breakfast": False, "lunch": True},  # -30%
                    }
                ],
            },
        },
    )

    assert plan["status"] == "planned"
    assert plan["total_estimated_cost_nok"] == 1850.0 + 1950.0 + (940.0 * 0.7)
    assert len(plan["travel_legs"]) == 1
    assert len(plan["stays"]) == 1
    assert len(plan["per_diems"]) == 1


@pytest.mark.asyncio
async def test_rs5_contract_b_spend_ceiling_refusal(mock_engine):
    """RS-5: Over-ceiling travel booking is strictly REFUSED without explicit confirm=True."""
    ns_id = uuid4()
    alloc_id = uuid4()
    mock_engine.allocations[str(alloc_id)] = {
        "id": str(alloc_id),
        "namespace_id": str(ns_id),
        "resource_id": str(uuid4()),
        "status": "reserved",
    }

    # High-spend itinerary: 18,000 NOK flight + 8,000 NOK hotel = 26,000 NOK (> 10,000 NOK ceiling)
    itinerary = {
        "travel_legs": [
            {
                "origin": "Oslo",
                "destination": "Tromso",
                "departure_at": "2026-09-21T08:00:00Z",
                "cost_nok": 18000.0,
            }
        ],
        "stays": [
            {
                "location": "Tromso Hotel",
                "check_in": "2026-09-21T15:00:00Z",
                "check_out": "2026-09-25T11:00:00Z",
                "cost_nok": 8000.0,
            }
        ],
    }

    # Attempt booking without confirm=True -> Must be REFUSED!
    with pytest.raises(
        ResourceValidationError, match="Contract-B Spend Gate Refusal.*exceeds ceiling"
    ):
        await do_plan_travel(
            mock_engine,
            {
                "namespace_id": ns_id,
                "allocation_id": alloc_id,
                "action": "book",
                "idempotency_key": "IDEM-OVER-CEILING-001",
                "itinerary": itinerary,
                "spend_ceiling_nok": 10000.0,
                "confirm": False,  # unconfirmed over ceiling
            },
        )

    # Positive control: Providing explicit confirm=True admits the booking!
    booked = await do_plan_travel(
        mock_engine,
        {
            "namespace_id": ns_id,
            "allocation_id": alloc_id,
            "action": "book",
            "idempotency_key": "IDEM-OVER-CEILING-CONFIRMED",
            "itinerary": itinerary,
            "spend_ceiling_nok": 10000.0,
            "confirm": True,  # confirmed!
        },
    )
    assert booked["status"] == "booked"
    assert booked["total_cost_nok"] == 26000.0
    assert len(mock_engine.travel_legs) == 1
    assert len(mock_engine.stays) == 1


@pytest.mark.asyncio
async def test_contract_b_idempotency_and_missing_key(mock_engine):
    """Booking requires idempotency key, and replay with same key returns existing booking."""
    ns_id = uuid4()
    alloc_id = uuid4()
    mock_engine.allocations[str(alloc_id)] = {
        "id": str(alloc_id),
        "namespace_id": str(ns_id),
        "resource_id": str(uuid4()),
        "status": "reserved",
    }

    itinerary = {
        "travel_legs": [
            {
                "origin": "Oslo",
                "destination": "Stavanger",
                "departure_at": "2026-09-22T09:00:00Z",
                "cost_nok": 2200.0,
            }
        ]
    }

    # Missing idempotency_key on book raises error
    with pytest.raises(ResourceValidationError, match="idempotency_key is required"):
        await do_plan_travel(
            mock_engine,
            {
                "namespace_id": ns_id,
                "allocation_id": alloc_id,
                "action": "book",
                "itinerary": itinerary,
            },
        )

    # First booking
    res1 = await do_plan_travel(
        mock_engine,
        {
            "namespace_id": ns_id,
            "allocation_id": alloc_id,
            "action": "book",
            "idempotency_key": "IDEM-STAVANGER-123",
            "itinerary": itinerary,
            "spend_ceiling_nok": 10000.0,
        },
    )
    assert res1["status"] == "booked"
    assert res1["idempotent_replay"] is False

    # Second booking with same idempotency key
    res2 = await do_plan_travel(
        mock_engine,
        {
            "namespace_id": ns_id,
            "allocation_id": alloc_id,
            "action": "book",
            "idempotency_key": "IDEM-STAVANGER-123",
            "itinerary": itinerary,
            "spend_ceiling_nok": 10000.0,
        },
    )
    assert res2["status"] == "booked"
    assert res2["idempotent_replay"] is True
