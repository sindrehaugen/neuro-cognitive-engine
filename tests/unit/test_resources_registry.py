"""Unit tests for Module 15 Resources Engine registry and capacity resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources._guard import (
    ResourceNotFoundError,
    ResourceValidationError,
)
from nce.vertical_modules.resources.capacity import do_resolve_capacity
from nce.vertical_modules.resources.registry import (
    do_create_resource,
    do_get_resource,
    do_list_resources,
    do_update_resource,
)


class MockEngine:
    """Mock engine holding an in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        # Query parsing for mock
        q = query.strip().lower()
        if "select" in q and "from resources" in q and "where id = $1 and namespace_id = $2" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.rows.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    return r
            return None
        return None

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "select" in q and "from resources" in q:
            ns_id = str(args[0])
            matched = [r for r in engine.rows.values() if str(r["namespace_id"]) == ns_id]
            if "and kind = $2" in q and len(args) > 1:
                kind = str(args[1])
                matched = [r for r in matched if r["kind"] == kind]
            return matched
        return []

    async def mock_execute(query, *args):
        import json

        q = query.strip().lower()
        if "insert into resources" in q:
            # (id, namespace_id, kind, ref_id, display_name, attrs, created_at, updated_at)
            raw_attrs = args[5]
            if isinstance(raw_attrs, str):
                parsed_attrs = json.loads(raw_attrs)
            elif isinstance(raw_attrs, dict):
                parsed_attrs = raw_attrs
            else:
                parsed_attrs = {}
            row = {
                "id": args[0],
                "namespace_id": args[1],
                "kind": args[2],
                "ref_id": args[3],
                "display_name": args[4],
                "attrs": parsed_attrs,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            engine.rows[str(row["id"])] = row
            return "INSERT 0 1"
        if "update resources" in q and "set" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.rows.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    if len(args) > 2 and args[2] is not None:
                        r["display_name"] = args[2]
                    if len(args) > 3 and args[3] is not None:
                        raw_attrs = args[3]
                        if isinstance(raw_attrs, str):
                            raw_attrs = json.loads(raw_attrs)
                        if isinstance(raw_attrs, dict):
                            r["attrs"].update(raw_attrs)
                    r["updated_at"] = datetime.now(timezone.utc)
                    return "UPDATE 1"
            return "UPDATE 0"
        return ""

    async def mock_fetchval(query, *args):
        q = query.strip().lower()
        if "select count(*)" in q and "from resources" in q:
            ns_id = str(args[0])
            matched = [r for r in engine.rows.values() if str(r["namespace_id"]) == ns_id]
            if "and kind = $2" in q and len(args) > 1:
                kind = str(args[1])
                matched = [r for r in matched if r["kind"] == kind]
            return len(matched)
        return 0

    class MockConn:
        async def fetchrow(self, query, *args):
            return await mock_fetchrow(query, *args)

        async def fetch(self, query, *args):
            return await mock_fetch(query, *args)

        async def fetchval(self, query, *args):
            return await mock_fetchval(query, *args)

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
        "nce.vertical_modules.resources.capacity.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


@pytest.mark.asyncio
async def test_create_and_get_resource(mock_engine):
    ns_id = uuid4()
    res = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "ref_id": "EMP-001",
            "display_name": "Lead Technician",
            "attrs": {"cert": "CTS-D"},
        },
    )
    assert res["id"] is not None
    assert res["kind"] == "employee"
    assert res["display_name"] == "Lead Technician"

    fetched = await do_get_resource(
        mock_engine,
        {"namespace_id": ns_id, "resource_id": res["id"]},
    )
    assert fetched["id"] == res["id"]
    assert fetched["ref_id"] == "EMP-001"


@pytest.mark.asyncio
async def test_rs2_van_triple_role_and_kind_validation(mock_engine):
    ns_id = uuid4()
    # RS-2: Van is a VEHICLE
    van = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "vehicle",
            "ref_id": "VAN-42",
            "display_name": "Service Van 42",
            "attrs": {"plate": "EL12345", "stock_location_id": "LOC-VAN-42"},
        },
    )
    assert van["kind"] == "vehicle"

    # RS-2: Functional location is NOT a valid resource kind
    with pytest.raises(ResourceValidationError, match="FUNCTIONAL_LOCATION"):
        await do_create_resource(
            mock_engine,
            {
                "namespace_id": ns_id,
                "kind": "functional_location",
                "display_name": "Customer Site Rack 1",
            },
        )

    # Invalid random kind rejected
    with pytest.raises(ResourceValidationError, match="Invalid resource kind"):
        await do_create_resource(
            mock_engine,
            {
                "namespace_id": ns_id,
                "kind": "spaceship",
                "display_name": "Apollo",
            },
        )


@pytest.mark.asyncio
async def test_tenant_isolation_get(mock_engine):
    ns_a = uuid4()
    ns_b = uuid4()

    res = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_a,
            "kind": "tool",
            "display_name": "OTDR Fusion Splicer",
        },
    )

    # Fetch with wrong namespace must fail
    with pytest.raises(ResourceNotFoundError):
        await do_get_resource(
            mock_engine,
            {"namespace_id": ns_b, "resource_id": res["id"]},
        )


@pytest.mark.asyncio
async def test_list_and_update_resources(mock_engine):
    ns_id = uuid4()
    r1 = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "contractor", "display_name": "Contractor Alpha"},
    )
    _r2 = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "vehicle", "display_name": "Van Beta"},
    )

    all_res = await do_list_resources(mock_engine, {"namespace_id": ns_id})
    assert len(all_res["resources"]) == 2

    vehicles = await do_list_resources(mock_engine, {"namespace_id": ns_id, "kind": "vehicle"})
    assert len(vehicles["resources"]) == 1
    assert vehicles["resources"][0]["display_name"] == "Van Beta"

    # Update display_name
    updated = await do_update_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": r1["id"],
            "display_name": "Contractor Alpha Updated",
            "attrs": {"tier": "gold"},
        },
    )
    assert updated["display_name"] == "Contractor Alpha Updated"
    assert updated["attrs"]["tier"] == "gold"


@pytest.mark.asyncio
async def test_do_resolve_capacity_pure_read(mock_engine):
    ns_id = uuid4()
    await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "employee", "display_name": "Tech 1"},
    )
    await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "employee", "display_name": "Tech 2"},
    )
    await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "vehicle", "display_name": "Van 1"},
    )

    # Resolve capacity for all resources
    cap = await do_resolve_capacity(
        mock_engine,
        {
            "namespace_id": ns_id,
            "window": {
                "starts_at": "2026-09-10T08:00:00Z",
                "ends_at": "2026-09-10T16:00:00Z",
            },
        },
    )
    assert cap["total_resources"] == 3
    assert len(cap["available_resources"]) == 3
    assert cap["utilization_pct"] == 0.0

    # Resolve capacity filtered by kind="employee"
    cap_emp = await do_resolve_capacity(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "window": {
                "starts_at": "2026-09-10T08:00:00Z",
                "ends_at": "2026-09-10T16:00:00Z",
            },
        },
    )
    assert cap_emp["total_resources"] == 2
    assert len(cap_emp["available_resources"]) == 2
