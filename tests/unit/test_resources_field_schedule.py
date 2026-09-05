"""Unit tests for Module 15 Field Webapp Backend (do_field_schedule)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.config import cfg
from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
)
from nce.vertical_modules.resources.field_schedule import do_field_schedule


class MockEngine:
    """Mock engine holding in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.travel_legs: dict[str, dict[str, Any]] = {}
        self.stays: dict[str, dict[str, Any]] = {}
        self.per_diems: dict[str, dict[str, Any]] = {}
        self.work_orders: dict[str, dict[str, Any]] = {}
        self.checklists: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "from resources" in q and "where id = $1 and namespace_id = $2" in q:
            r_id, ns_id = str(args[0]), str(args[1])
            for r in engine.resources.values():
                if str(r["id"]) == r_id and str(r["namespace_id"]) == ns_id:
                    return r
            return None

        return None

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "from allocations" in q and "where namespace_id = $1 and resource_id = $2" in q:
            ns_id, r_id = str(args[0]), str(args[1])
            res = []
            for a in engine.allocations.values():
                if (
                    str(a["namespace_id"]) == ns_id
                    and str(a["resource_id"]) == r_id
                    and a.get("status") != "released"
                ):
                    # Check date window if passed
                    if len(args) >= 4 and "tstzrange" in q:
                        start_bound = args[2]
                        end_bound = args[3]
                        a_start = (
                            a["starts_at"]
                            if isinstance(a["starts_at"], datetime)
                            else datetime.fromisoformat(str(a["starts_at"]).replace("Z", "+00:00"))
                        )
                        a_end = (
                            a["ends_at"]
                            if isinstance(a["ends_at"], datetime)
                            else datetime.fromisoformat(str(a["ends_at"]).replace("Z", "+00:00"))
                        )
                        # Overlap: a_start < end_bound and a_end > start_bound
                        if not (a_start < end_bound and a_end > start_bound):
                            continue
                    res.append(a)
            return sorted(res, key=lambda x: str(x["starts_at"]))

        if "from travel_legs" in q and "allocation_id = any($2)" in q:
            ns_id = str(args[0])
            alloc_ids = [str(aid) for aid in args[1]]
            res = [
                tl
                for tl in engine.travel_legs.values()
                if str(tl["namespace_id"]) == ns_id and str(tl["allocation_id"]) in alloc_ids
            ]
            return sorted(res, key=lambda x: str(x["departure_at"]))

        if "from stays" in q and "allocation_id = any($2)" in q:
            ns_id = str(args[0])
            alloc_ids = [str(aid) for aid in args[1]]
            res = [
                s
                for s in engine.stays.values()
                if str(s["namespace_id"]) == ns_id and str(s["allocation_id"]) in alloc_ids
            ]
            return sorted(res, key=lambda x: str(x["check_in"]))

        if "from per_diems" in q and "allocation_id = any($2)" in q:
            ns_id = str(args[0])
            alloc_ids = [str(aid) for aid in args[1]]
            res = [
                pd
                for pd in engine.per_diems.values()
                if str(pd["namespace_id"]) == ns_id and str(pd["allocation_id"]) in alloc_ids
            ]
            return sorted(res, key=lambda x: str(x["date"]))

        if "from work_orders" in q and "assignee_id = $2 or work_order_id = any($3)" in q:
            ns_id = str(args[0])
            assignee_id = str(args[1])
            wo_ids = [str(wid) for wid in args[2]]
            res = []
            for wo in engine.work_orders.values():
                if str(wo["namespace_id"]) == ns_id:
                    if (
                        str(wo.get("assignee_id")) == assignee_id
                        or str(wo.get("work_order_id")) in wo_ids
                    ):
                        res.append(wo)
            return sorted(res, key=lambda x: str(x.get("created_at", "")), reverse=True)

        if "from checklists" in q and "work_order_id = any($2)" in q:
            ns_id = str(args[0])
            wo_ids = [str(wid) for wid in args[1]]
            res = [
                cl
                for cl in engine.checklists.values()
                if str(cl["namespace_id"]) == ns_id and str(cl["work_order_id"]) in wo_ids
            ]
            return res

        return []

    class MockConn:
        async def fetchrow(self, query, *args):
            return await mock_fetchrow(query, *args)

        async def fetch(self, query, *args):
            return await mock_fetch(query, *args)

    class MockContextManager:
        async def __aenter__(self):
            return MockConn()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "nce.vertical_modules.resources.field_schedule.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


@pytest.mark.asyncio
async def test_do_field_schedule_composed_success(mock_engine):
    """Verify composed field schedule for technician including allocations, travel, stays, and work orders."""
    ns_id = uuid4()
    tech_id = uuid4()
    van_id = uuid4()

    # Setup Van Resource (RS-2)
    mock_engine.resources[str(van_id)] = {
        "id": str(van_id),
        "namespace_id": str(ns_id),
        "name": "Service Van 01",
        "kind": "vehicle",
        "metadata": {
            "registration_no": "EL-12345",
            "stock_location_id": "LOC-VAN-01",
        },
    }

    # Setup Technician Resource
    mock_engine.resources[str(tech_id)] = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "kind": "employee",
        "name": "Tech Alpha",
        "email": "tech.alpha@example.test",
        "phone": "+47 99999999",
        "capacity_pct": 100.0,
        "cost_rate_nok": 650.0,
        "hourly_rate_nok": 950.0,
        "active": True,
        "metadata": {
            "assigned_vehicle_id": str(van_id),
            "stock_location_id": "LOC-VAN-01",
        },
    }

    # Setup Allocation
    alloc_id = uuid4()
    mock_engine.allocations[str(alloc_id)] = {
        "id": str(alloc_id),
        "namespace_id": str(ns_id),
        "resource_id": str(tech_id),
        "demand_kind": "work_order",
        "demand_id": "WO-2026-001",
        "functional_location_id": str(uuid4()),
        "starts_at": datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc),
        "status": "confirmed",
        "confidence": 1.0,
        "attrs": {"work_order_id": "WO-2026-001"},
        "created_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
    }

    # Setup Travel Leg & Stay & Per Diem
    tl_id = uuid4()
    mock_engine.travel_legs[str(tl_id)] = {
        "id": str(tl_id),
        "namespace_id": str(ns_id),
        "allocation_id": str(alloc_id),
        "origin": "Oslo",
        "destination": "Drammen",
        "departure_at": datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc),
        "arrival_at": datetime(2026, 9, 21, 7, 45, tzinfo=timezone.utc),
        "mode": "van",
        "cost_nok": 150.0,
        "booking_ref": None,
        "status": "confirmed",
        "attrs": {},
    }

    stay_id = uuid4()
    mock_engine.stays[str(stay_id)] = {
        "id": str(stay_id),
        "namespace_id": str(ns_id),
        "allocation_id": str(alloc_id),
        "location": "Drammen Hotel",
        "check_in": datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
        "check_out": datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc),
        "cost_nok": 1400.0,
        "booking_ref": "HOTEL-998",
        "status": "confirmed",
        "attrs": {},
    }

    pd_id = uuid4()
    mock_engine.per_diems[str(pd_id)] = {
        "id": str(pd_id),
        "namespace_id": str(ns_id),
        "allocation_id": str(alloc_id),
        "date": "2026-09-21",
        "rate_nok": 940.0,
        "diet_type": "overnight_hotel",
        "meals_provided": {"breakfast": True},
        "attrs": {},
    }

    # Setup Field Tech Work Order & Checklist
    wo_id = uuid4()
    mock_engine.work_orders[str(wo_id)] = {
        "id": str(wo_id),
        "work_order_id": "WO-2026-001",
        "namespace_id": str(ns_id),
        "partner_scope_id": None,
        "kind": "install",
        "source_kind": "project",
        "source_ref": "PRJ-771",
        "location_id": "LOC-DRAMMEN-MAIN",
        "assignee_id": str(tech_id),
        "assignee_kind": "employee",
        "status": "scheduled",
        "priority": "high",
        "summary": "Audio DSP Commissioning in Room A",
        "due_at": datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc),
        "raw": {},
        "created_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
    }

    cl_id = uuid4()
    mock_engine.checklists[str(cl_id)] = {
        "id": str(cl_id),
        "checklist_id": "CL-001",
        "work_order_id": "WO-2026-001",
        "namespace_id": str(ns_id),
        "template_id": "DSP_COMMISSIONING_V1",
        "items": [{"id": 1, "text": "Verify 48V phantom power", "checked": True}],
        "completed_at": None,
    }

    # Execute do_field_schedule
    res = await do_field_schedule(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": tech_id,
        },
    )

    assert res["namespace_id"] == str(ns_id)
    assert res["technician"]["id"] == str(tech_id)
    assert res["technician"]["hourly_rate_nok"] == 950.0
    assert res["contractor_view"] is False

    # Schedule item verification
    assert len(res["schedule"]) == 1
    item = res["schedule"][0]
    assert item["id"] == str(alloc_id)
    assert item["status"] == "confirmed"

    # Work order composition
    wo = item["work_order"]
    assert wo is not None
    assert wo["work_order_id"] == "WO-2026-001"
    assert wo["priority"] == "high"
    assert len(wo["checklists"]) == 1
    assert wo["checklists"][0]["checklist_id"] == "CL-001"

    # Travel & Hospitality composition
    assert len(item["travel_legs"]) == 1
    assert item["travel_legs"][0]["origin"] == "Oslo"
    assert item["travel_legs"][0]["cost_nok"] == 150.0

    assert len(item["stays"]) == 1
    assert item["stays"][0]["location"] == "Drammen Hotel"
    assert item["stays"][0]["cost_nok"] == 1400.0

    assert len(item["per_diems"]) == 1
    assert item["per_diems"][0]["rate_nok"] == 940.0

    # Assigned Equipment (RS-2)
    eq = res["assigned_equipment"]
    assert eq["vehicle"] is not None
    assert eq["vehicle"]["registration_number"] == "EL-12345"
    assert eq["van_stock_location"]["stock_location_id"] == "LOC-VAN-01"
    assert eq["van_stock_location"]["role"] == "STOCK_LOCATION"

    # Calendar sync disabled by config
    assert res["calendar_sync"]["enabled"] is False
    assert res["calendar_sync"]["status"] == "disabled_by_config"


@pytest.mark.asyncio
async def test_do_field_schedule_contractor_redaction(mock_engine):
    """External contractors must have internal cost, pricing, and margin strictly stripped."""
    ns_id = uuid4()
    contractor_id = uuid4()

    mock_engine.resources[str(contractor_id)] = {
        "id": str(contractor_id),
        "namespace_id": str(ns_id),
        "kind": "contractor",
        "name": "Contractor Partner AS",
        "email": "partner@example.test",
        "phone": "+47 88888888",
        "capacity_pct": 100.0,
        "cost_rate_nok": 500.0,
        "hourly_rate_nok": 1200.0,
        "active": True,
        "metadata": {},
    }

    alloc_id = uuid4()
    mock_engine.allocations[str(alloc_id)] = {
        "id": str(alloc_id),
        "namespace_id": str(ns_id),
        "resource_id": str(contractor_id),
        "demand_kind": "work_order",
        "demand_id": "WO-EXT-002",
        "functional_location_id": str(uuid4()),
        "starts_at": datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc),
        "status": "reserved",
        "confidence": 1.0,
        "attrs": {"internal_cost_center": "CC-99", "margin_target": 0.4},
        "created_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
    }

    tl_id = uuid4()
    mock_engine.travel_legs[str(tl_id)] = {
        "id": str(tl_id),
        "namespace_id": str(ns_id),
        "allocation_id": str(alloc_id),
        "origin": "Bergen",
        "destination": "Voss",
        "departure_at": datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
        "arrival_at": datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc),
        "mode": "train",
        "cost_nok": 350.0,
        "booking_ref": "TRAIN-123",
        "status": "planned",
        "attrs": {},
    }

    res = await do_field_schedule(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": contractor_id,
        },
    )

    assert res["contractor_view"] is True
    # Sensitive pricing/cost stripped from profile
    assert "cost_rate_nok" not in res["technician"]
    assert "hourly_rate_nok" not in res["technician"]

    # Redacted allocation view enforced
    item = res["schedule"][0]
    assert item["redaction"] == "contractor_allow_list_enforced"
    assert "attrs" not in item  # attrs stripped per CONTRACTOR_ALLOWED_ALLOCATION_FIELDS

    # Travel costs stripped from contractor view
    assert "cost_nok" not in item["travel_legs"][0]


@pytest.mark.asyncio
async def test_do_field_schedule_calendar_sync_enabled(mock_engine, monkeypatch):
    """When NCE_RESOURCES_CALENDAR_SYNC_ENABLED=True, calendar_sync reports active sync."""
    monkeypatch.setattr(cfg, "NCE_RESOURCES_CALENDAR_SYNC_ENABLED", True)

    ns_id = uuid4()
    tech_id = uuid4()

    mock_engine.resources[str(tech_id)] = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "kind": "employee",
        "name": "Tech Beta",
        "email": "tech.beta@example.test",
        "phone": "+47 99999999",
        "capacity_pct": 100.0,
        "cost_rate_nok": 650.0,
        "hourly_rate_nok": 950.0,
        "active": True,
        "metadata": {},
    }

    res = await do_field_schedule(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": tech_id,
        },
    )

    assert res["calendar_sync"]["enabled"] is True
    assert res["calendar_sync"]["status"] == "synced"
    assert res["calendar_sync"]["upn"] == "tech.beta@example.test"
    assert res["calendar_sync"]["provider"] == "microsoft_365_graph"


@pytest.mark.asyncio
async def test_do_field_schedule_resource_not_found(mock_engine):
    """Non-existent resource raises ResourceNotFoundError."""
    ns_id = uuid4()
    missing_id = uuid4()

    with pytest.raises(ResourceNotFoundError, match="Resource.*not found"):
        await do_field_schedule(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": missing_id,
            },
        )


@pytest.mark.asyncio
async def test_do_field_schedule_date_window_filtering(mock_engine):
    """Window filtering correctly restricts allocations returned to overlapping range."""
    ns_id = uuid4()
    tech_id = uuid4()

    mock_engine.resources[str(tech_id)] = {
        "id": str(tech_id),
        "namespace_id": str(ns_id),
        "kind": "employee",
        "name": "Tech Gamma",
        "email": "tech.gamma@example.test",
        "phone": "+47 99999999",
        "capacity_pct": 100.0,
        "cost_rate_nok": 650.0,
        "hourly_rate_nok": 950.0,
        "active": True,
        "metadata": {},
    }

    # Allocation 1: Sept 10
    a1_id = uuid4()
    mock_engine.allocations[str(a1_id)] = {
        "id": str(a1_id),
        "namespace_id": str(ns_id),
        "resource_id": str(tech_id),
        "demand_kind": "service",
        "demand_id": None,
        "functional_location_id": None,
        "starts_at": datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc),
        "status": "reserved",
        "confidence": 1.0,
        "attrs": {},
        "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
    }

    # Allocation 2: Sept 25
    a2_id = uuid4()
    mock_engine.allocations[str(a2_id)] = {
        "id": str(a2_id),
        "namespace_id": str(ns_id),
        "resource_id": str(tech_id),
        "demand_kind": "service",
        "demand_id": None,
        "functional_location_id": None,
        "starts_at": datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc),
        "ends_at": datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc),
        "status": "reserved",
        "confidence": 1.0,
        "attrs": {},
        "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
    }

    # Filter for Sept 20 to Sept 30 -> Only a2 returned!
    res = await do_field_schedule(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": tech_id,
            "starts_at": "2026-09-20T00:00:00Z",
            "ends_at": "2026-09-30T00:00:00Z",
        },
    )
    assert len(res["schedule"]) == 1
    assert res["schedule"][0]["id"] == str(a2_id)
