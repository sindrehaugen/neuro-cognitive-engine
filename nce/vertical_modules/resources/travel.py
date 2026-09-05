"""
nce.vertical_modules.resources.travel
=====================================
Travel, Lodging & Statutory Norwegian Subsistence (Diett) Planner for Module 15 (Staff & Resources Engine).
Enforces Contract-B Autonomous Spend Gate (Spec §62, §126, RS-5: idempotency key + ceiling + confirm)
and jurisdiction-specific Norwegian statutory tax rules (Statens satser / Skatteetaten) from resources-travel-policy.json.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from nce.config import cfg
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
    require_resources_enabled,
)
from nce.vertical_modules.resources.allocations import _parse_datetime
from nce.vertical_modules.resources.registry import _extract_pool, _parse_uuid

log = logging.getLogger("nce.vertical_modules.resources.travel")

POLICY_FILE = Path(__file__).parent / "resources-travel-policy.json"


def load_travel_policy() -> dict[str, Any]:
    """Load Norwegian travel & subsistence policy from JSON."""
    if POLICY_FILE.exists():
        try:
            with open(POLICY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Failed to load %s, using fallback: %s", POLICY_FILE, exc)

    return {
        "version": "1.0",
        "jurisdiction": "NO",
        "currency": "NOK",
        "rules": {
            "statutory_rates_2026": {
                "day_trip_short_6_to_12h": 360.0,
                "day_trip_long_over_12h": 680.0,
                "overnight_hotel": 940.0,
                "overnight_unspecified": 450.0,
            },
            "meal_deductions_pct": {"breakfast": 0.20, "lunch": 0.30, "dinner": 0.50},
        },
        "spend_gate": {
            "default_max_autonomous_spend_nok": 10000.0,
            "requires_idempotency_key": True,
        },
    }


def calculate_norwegian_diett(
    diet_date: str | date,
    diet_type: str,
    meals_provided: dict[str, bool] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Calculate statutory Norwegian subsistence allowance with meal deductions (Statens satser / Skatteetaten).
    Deductions: Breakfast 20%, Lunch 30%, Dinner 50%.
    """
    if not policy:
        policy = load_travel_policy()

    rates = policy.get("rules", {}).get("statutory_rates_2026", {})
    deductions = policy.get("rules", {}).get(
        "meal_deductions_pct",
        {
            "breakfast": 0.20,
            "lunch": 0.30,
            "dinner": 0.50,
        },
    )

    base_rate = float(rates.get(diet_type, rates.get("overnight_hotel", 940.0)))
    meals = meals_provided or {}

    total_deduction_pct = 0.0
    deduction_breakdown = {}
    for meal, pct in deductions.items():
        if meals.get(meal):
            total_deduction_pct += pct
            deduction_breakdown[meal] = round(base_rate * pct, 2)
        else:
            deduction_breakdown[meal] = 0.0

    total_deduction_pct = min(1.0, total_deduction_pct)
    net_rate = max(0.0, round(base_rate * (1.0 - total_deduction_pct), 2))

    date_str = diet_date.isoformat() if hasattr(diet_date, "isoformat") else str(diet_date)
    return {
        "date": date_str,
        "diet_type": diet_type,
        "jurisdiction": "NO",
        "currency": "NOK",
        "base_rate_nok": base_rate,
        "meals_provided": {
            "breakfast": bool(meals.get("breakfast", False)),
            "lunch": bool(meals.get("lunch", False)),
            "dinner": bool(meals.get("dinner", False)),
        },
        "deductions_nok": deduction_breakdown,
        "net_rate_nok": net_rate,
    }


async def do_plan_travel(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Plan or book travel, lodging, and statutory per-diem allowances for an allocation.
    Governed by Contract-B Spend Gate (RS-5):
      - Booking real money spend requires idempotency_key.
      - Spend over ceiling strictly refused unless confirm=True.
      - Idempotent execution: duplicate calls with same idempotency_key return existing booking.
    """
    require_resources_enabled(params.get("namespace_metadata"))
    ns_id = _parse_uuid(params.get("namespace_id"), "namespace_id")
    alloc_id = _parse_uuid(params.get("allocation_id"), "allocation_id")

    action = str(params.get("action") or "plan").strip().lower()
    if action not in ("plan", "book"):
        raise ResourceValidationError(
            f"Invalid travel action: {action!r}. Expected 'plan' or 'book'."
        )

    policy = load_travel_policy()
    pool = _extract_pool(engine)

    # 1. Verify allocation exists
    async with scoped_pg_session(pool, ns_id) as conn:
        alloc_row = await conn.fetchrow(
            """
            SELECT id, resource_id, demand_kind, demand_id, functional_location_id, starts_at, ends_at, status
            FROM allocations
            WHERE id = $1 AND namespace_id = $2
            """,
            alloc_id,
            ns_id,
        )
    if not alloc_row:
        raise ResourceNotFoundError(f"Allocation {alloc_id} not found in namespace {ns_id}.")

    # 2. Parse and evaluate itinerary
    itinerary = params.get("itinerary") or {}
    raw_legs = itinerary.get("travel_legs") or []
    raw_stays = itinerary.get("stays") or []
    raw_diems = itinerary.get("per_diems") or []

    evaluated_legs = []
    legs_cost = Decimal("0.00")
    for leg in raw_legs:
        cost = Decimal(str(leg.get("cost_nok", leg.get("estimated_cost_nok", 0.0))))
        legs_cost += cost
        dep = _parse_datetime(leg.get("departure_at"), "departure_at")
        arr = (
            _parse_datetime(leg.get("arrival_at"), "arrival_at") if leg.get("arrival_at") else None
        )
        evaluated_legs.append(
            {
                "origin": str(leg.get("origin") or "HQ"),
                "destination": str(leg.get("destination") or "Site"),
                "departure_at": dep.isoformat(),
                "arrival_at": arr.isoformat() if arr else None,
                "mode": str(leg.get("mode") or "flight"),
                "cost_nok": float(cost),
                "booking_ref": leg.get("booking_ref"),
            }
        )

    evaluated_stays = []
    stays_cost = Decimal("0.00")
    for stay in raw_stays:
        cost = Decimal(str(stay.get("cost_nok", stay.get("estimated_cost_nok", 0.0))))
        stays_cost += cost
        ci = _parse_datetime(stay.get("check_in"), "check_in")
        co = _parse_datetime(stay.get("check_out"), "check_out")
        if co <= ci:
            raise ResourceValidationError("Stay check_out must be after check_in.")
        evaluated_stays.append(
            {
                "location": str(stay.get("location") or "Hotel"),
                "check_in": ci.isoformat(),
                "check_out": co.isoformat(),
                "cost_nok": float(cost),
                "booking_ref": stay.get("booking_ref"),
            }
        )

    evaluated_diems = []
    diems_cost = Decimal("0.00")
    for d in raw_diems:
        d_res = calculate_norwegian_diett(
            diet_date=d.get("date") or "2026-09-01",
            diet_type=d.get("diet_type") or "overnight_hotel",
            meals_provided=d.get("meals_provided"),
            policy=policy,
        )
        cost = Decimal(str(d_res["net_rate_nok"]))
        diems_cost += cost
        evaluated_diems.append(d_res)

    total_cost_nok = float(legs_cost + stays_cost + diems_cost)

    # 3. Action: PLAN (Advisor Mode)
    if action == "plan":
        return {
            "status": "planned",
            "namespace_id": str(ns_id),
            "allocation_id": str(alloc_id),
            "currency": "NOK",
            "jurisdiction": "NO",
            "total_estimated_cost_nok": total_cost_nok,
            "cost_breakdown": {
                "travel_legs_nok": float(legs_cost),
                "stays_nok": float(stays_cost),
                "per_diems_nok": float(diems_cost),
            },
            "travel_legs": evaluated_legs,
            "stays": evaluated_stays,
            "per_diems": evaluated_diems,
            "notes": "Norwegian statutory subsistence and travel itinerary formulated.",
        }

    # 4. Action: BOOK (Actor Mode with Contract-B Spend Gate)
    idempotency_key = str(params.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise ResourceValidationError(
            "Contract-B Spend Gate Refusal: idempotency_key is required when booking travel (RS-5)."
        )

    # Spend ceiling check
    ceiling = float(params.get("spend_ceiling_nok") or cfg.NCE_RESOURCES_TRAVEL_MAX_AUTO_SPEND)
    confirm = bool(params.get("confirm", False))

    if total_cost_nok > ceiling and not confirm:
        raise ResourceValidationError(
            f"Contract-B Spend Gate Refusal: Total travel spend ({total_cost_nok:,.2f} NOK) exceeds ceiling ({ceiling:,.2f} NOK). Explicit confirm=True required."
        )

    # Idempotency check: check if booking already exists for this idempotency_key
    async with scoped_pg_session(pool, ns_id) as conn:
        existing_leg = await conn.fetchrow(
            """
            SELECT id, allocation_id, attrs FROM travel_legs
            WHERE namespace_id = $1 AND attrs->>'idempotency_key' = $2
            LIMIT 1
            """,
            ns_id,
            idempotency_key,
        )
        if existing_leg:
            # Return existing booking idempotently
            return {
                "status": "booked",
                "idempotent_replay": True,
                "namespace_id": str(ns_id),
                "allocation_id": str(alloc_id),
                "idempotency_key": idempotency_key,
                "total_cost_nok": total_cost_nok,
                "travel_legs_count": len(evaluated_legs),
                "stays_count": len(evaluated_stays),
                "per_diems_count": len(evaluated_diems),
            }

        # Commit rows to database
        persisted_legs = []
        for leg in evaluated_legs:
            leg_id = uuid4()
            await conn.execute(
                """
                INSERT INTO travel_legs (
                    id, namespace_id, allocation_id, origin, destination,
                    departure_at, arrival_at, mode, cost_nok, booking_ref, status, attrs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'booked', $11::jsonb)
                """,
                leg_id,
                ns_id,
                alloc_id,
                leg["origin"],
                leg["destination"],
                datetime.fromisoformat(leg["departure_at"]),
                datetime.fromisoformat(leg["arrival_at"]) if leg["arrival_at"] else None,
                leg["mode"],
                Decimal(str(leg["cost_nok"])),
                leg.get("booking_ref"),
                json.dumps({"idempotency_key": idempotency_key}),
            )
            persisted_legs.append(str(leg_id))

        persisted_stays = []
        for stay in evaluated_stays:
            stay_id = uuid4()
            await conn.execute(
                """
                INSERT INTO stays (
                    id, namespace_id, allocation_id, location, check_in,
                    check_out, cost_nok, booking_ref, status, attrs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'booked', $9::jsonb)
                """,
                stay_id,
                ns_id,
                alloc_id,
                stay["location"],
                datetime.fromisoformat(stay["check_in"]),
                datetime.fromisoformat(stay["check_out"]),
                Decimal(str(stay["cost_nok"])),
                stay.get("booking_ref"),
                json.dumps({"idempotency_key": idempotency_key}),
            )
            persisted_stays.append(str(stay_id))

        persisted_diems = []
        for diem in evaluated_diems:
            diem_id = uuid4()
            d_date = date.fromisoformat(diem["date"])
            await conn.execute(
                """
                INSERT INTO per_diems (
                    id, namespace_id, allocation_id, date, rate_nok,
                    diet_type, meals_provided, attrs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
                """,
                diem_id,
                ns_id,
                alloc_id,
                d_date,
                Decimal(str(diem["net_rate_nok"])),
                diem["diet_type"],
                json.dumps(diem["meals_provided"]),
                json.dumps({"idempotency_key": idempotency_key}),
            )
            persisted_diems.append(str(diem_id))

    return {
        "status": "booked",
        "idempotent_replay": False,
        "namespace_id": str(ns_id),
        "allocation_id": str(alloc_id),
        "idempotency_key": idempotency_key,
        "total_cost_nok": total_cost_nok,
        "cost_breakdown": {
            "travel_legs_nok": float(legs_cost),
            "stays_nok": float(stays_cost),
            "per_diems_nok": float(diems_cost),
        },
        "booked_records": {
            "travel_leg_ids": persisted_legs,
            "stay_ids": persisted_stays,
            "per_diem_ids": persisted_diems,
        },
        "economy_feed": {
            "accrual_type": "travel_hospitality_expense",
            "total_nok": total_cost_nok,
            "currency": "NOK",
        },
    }
