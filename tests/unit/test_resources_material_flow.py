"""Unit tests for Module 15 Material Flow (Warehouse-to-Project Bridge) and RS-2."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
)
from nce.vertical_modules.resources.material_flow import do_plan_material_flow
from nce.vertical_modules.resources.registry import do_create_resource


class MockEngine:
    """Mock engine holding in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.stock_locations: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "from resources" in q and "where id = $1 and namespace_id = $2" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.resources.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    return r
            return None

        if "from stock_locations" in q:
            ns_id = str(args[0])
            veh_ref = str(args[1])
            name = str(args[2])
            for sl in engine.stock_locations.values():
                if str(sl["namespace_id"]) == ns_id and (
                    str(sl.get("vehicle_ref")) == veh_ref or sl["name"] == name
                ):
                    return sl
            return None

        if "insert into allocations" in q:
            alloc_id = str(args[0])
            ns_id = str(args[1])
            res_id = str(args[2])
            row = {
                "id": alloc_id,
                "namespace_id": ns_id,
                "resource_id": res_id,
                "demand_kind": args[3],
                "demand_id": str(args[4]) if args[4] else None,
                "functional_location_id": str(args[5]) if args[5] else None,
                "starts_at": args[6],
                "ends_at": args[7],
                "status": args[8],
                "confidence": args[9],
                "attrs": json.loads(args[10]) if isinstance(args[10], str) else args[10],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            engine.allocations[alloc_id] = row
            return row

        return None

    async def mock_execute(query, *args):
        q = query.strip().lower()
        if "insert into resources" in q:
            row = {
                "id": args[0],
                "namespace_id": args[1],
                "kind": args[2],
                "ref_id": args[3],
                "display_name": args[4],
                "attrs": json.loads(args[5]) if isinstance(args[5], str) else args[5],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            engine.resources[str(row["id"])] = row
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

    monkeypatch.setattr(
        "nce.vertical_modules.resources.registry.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.resources.allocations.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.resources.material_flow.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


@pytest.mark.asyncio
async def test_plan_material_flow_success(mock_engine):
    ns_id = uuid4()
    van = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "vehicle",
            "display_name": "Service Van 101",
            "attrs": {"plate": "EK12345", "capacity_kg": 950},
        },
    )

    # Link stock location for the van in Inventory
    stock_loc_id = str(uuid4())
    mock_engine.stock_locations[stock_loc_id] = {
        "id": stock_loc_id,
        "namespace_id": str(ns_id),
        "kind": "van",
        "name": "Service Van 101",
        "vehicle_ref": van["id"],
    }

    dest_loc_id = uuid4()
    items = [
        {"sku": "CRESTRON-CP4", "quantity": 1, "description": "Control Processor"},
        {"sku": "SHURE-MXA920", "quantity": 2, "description": "Ceiling Array Mic"},
    ]

    flow = await do_plan_material_flow(
        mock_engine,
        {
            "namespace_id": ns_id,
            "van_resource_id": van["id"],
            "target_date": "2026-09-20T10:00:00Z",
            "destination_location_id": dest_loc_id,
            "items": items,
            "auto_reserve_van": True,
        },
    )

    assert flow["status"] == "staged"
    assert flow["van_resource"]["id"] == van["id"]
    assert flow["van_resource"]["stock_location_id"] == stock_loc_id
    assert len(flow["stages"]) == 3
    assert flow["stages"][0]["stage"] == "pick_and_kit"
    assert flow["stages"][1]["stage"] == "van_loading"
    assert flow["stages"][2]["stage"] == "transit_and_delivery"
    assert flow["van_allocation"] is not None
    assert flow["van_allocation"]["resource_id"] == van["id"]


@pytest.mark.asyncio
async def test_rs2_van_is_never_functional_location(mock_engine):
    """RS-2: A van is VEHICLE and STOCK_LOCATION, but NEVER a customer functional location."""
    ns_id = uuid4()
    van = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "vehicle", "display_name": "Van Alpha"},
    )

    # If destination_location_id is the van itself, RS-2 violation must be raised!
    with pytest.raises(ResourceValidationError, match="RS-2 Violation"):
        await do_plan_material_flow(
            mock_engine,
            {
                "namespace_id": ns_id,
                "van_resource_id": van["id"],
                "destination_location_id": van["id"],  # illegal: van as destination
                "target_date": "2026-09-20T10:00:00Z",
            },
        )


@pytest.mark.asyncio
async def test_material_flow_requires_vehicle_kind(mock_engine):
    ns_id = uuid4()
    tech = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "employee", "display_name": "John Doe"},
    )

    with pytest.raises(ResourceValidationError, match="expected 'vehicle'"):
        await do_plan_material_flow(
            mock_engine,
            {
                "namespace_id": ns_id,
                "van_resource_id": tech["id"],  # technician cannot be used as a van
                "target_date": "2026-09-20T10:00:00Z",
            },
        )


@pytest.mark.asyncio
async def test_material_flow_van_not_found(mock_engine):
    ns_id = uuid4()
    with pytest.raises(ResourceNotFoundError):
        await do_plan_material_flow(
            mock_engine,
            {
                "namespace_id": ns_id,
                "van_resource_id": uuid4(),
                "target_date": "2026-09-20T10:00:00Z",
            },
        )
