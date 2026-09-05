"""Unit tests for Module 15 Resources Engine allocations and conflict detection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources._guard import (
    ResourceConcurrencyError,
    ResourceNotFoundError,
    ResourceValidationError,
)
from nce.vertical_modules.resources.allocations import (
    do_detect_conflicts,
    do_release,
    do_reserve,
    redact_contractor_view,
)
from nce.vertical_modules.resources.registry import do_create_resource


class MockEngine:
    """Mock engine holding an in-memory store simulating PostgreSQL tenant isolation and btree_gist exclusion."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "select id, kind from resources" in q and "where id = $1 and namespace_id = $2" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.resources.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    return r
            return None

        if "select" in q and "from resources" in q and "where id = $1 and namespace_id = $2" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.resources.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    return r
            return None

        if "insert into allocations" in q:
            # (alloc_id, ns_id, res_id, demand_kind, demand_id, functional_location_id, starts_at, ends_at, status, confidence, attrs_json)
            alloc_id = str(args[0])
            ns_id = str(args[1])
            res_id = str(args[2])
            starts_at = args[6]
            ends_at = args[7]
            status = args[8]

            # Simulate btree_gist exclusion constraint:
            # EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (status <> 'released')
            if status != "released":
                for existing in engine.allocations.values():
                    if (
                        str(existing["namespace_id"]) == ns_id
                        and str(existing["resource_id"]) == res_id
                        and existing["status"] != "released"
                    ):
                        ex_start = existing["starts_at"]
                        ex_end = existing["ends_at"]
                        # [starts_at, ends_at) overlaps with [ex_start, ex_end) if max(starts) < min(ends)
                        overlap = max(starts_at, ex_start) < min(ends_at, ex_end)
                        if overlap:
                            raise ResourceConcurrencyError(
                                f"Resource {res_id} is already allocated during the requested window."
                            )

            row = {
                "id": alloc_id,
                "namespace_id": ns_id,
                "resource_id": res_id,
                "demand_kind": args[3],
                "demand_id": str(args[4]) if args[4] else None,
                "functional_location_id": str(args[5]) if args[5] else None,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "status": status,
                "confidence": args[9],
                "attrs": json.loads(args[10]) if isinstance(args[10], str) else args[10],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            engine.allocations[alloc_id] = row
            return row

        if "update allocations" in q and "set status = 'released'" in q:
            alloc_id, ns_id = str(args[0]), str(args[1])
            for a in engine.allocations.values():
                if str(a["id"]) == alloc_id and str(a["namespace_id"]) == ns_id:
                    a["status"] = "released"
                    a["updated_at"] = datetime.now(timezone.utc)
                    return a
            return None

        return None

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "from allocations a1" in q and "join allocations a2" in q:
            ns_id = str(args[0])
            res_id = str(args[1]) if len(args) > 1 else None
            conflicts = []
            alloc_list = list(engine.allocations.values())
            for i in range(len(alloc_list)):
                for j in range(i + 1, len(alloc_list)):
                    a1 = alloc_list[i]
                    a2 = alloc_list[j]
                    if str(a1["namespace_id"]) != ns_id or str(a2["namespace_id"]) != ns_id:
                        continue
                    if str(a1["resource_id"]) != str(a2["resource_id"]):
                        continue
                    if res_id and str(a1["resource_id"]) != res_id:
                        continue
                    if a1["status"] == "released" or a2["status"] == "released":
                        continue
                    # Check range overlap
                    if max(a1["starts_at"], a2["starts_at"]) < min(a1["ends_at"], a2["ends_at"]):
                        conflicts.append(
                            {
                                "alloc_id_1": a1["id"],
                                "resource_id": a1["resource_id"],
                                "starts_at_1": a1["starts_at"],
                                "ends_at_1": a1["ends_at"],
                                "demand_kind_1": a1["demand_kind"],
                                "alloc_id_2": a2["id"],
                                "starts_at_2": a2["starts_at"],
                                "ends_at_2": a2["ends_at"],
                                "demand_kind_2": a2["demand_kind"],
                            }
                        )
            return conflicts
        return []

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

        async def fetch(self, query, *args):
            return await mock_fetch(query, *args)

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

    return engine


@pytest.mark.asyncio
async def test_reserve_and_release(mock_engine):
    ns_id = uuid4()
    res = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "employee", "display_name": "Tech Alpha"},
    )

    # Reserve 08:00 to 12:00
    alloc = await do_reserve(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": res["id"],
            "demand_kind": "project",
            "starts_at": "2026-09-06T08:00:00Z",
            "ends_at": "2026-09-06T12:00:00Z",
            "status": "reserved",
            "confidence": 0.95,
            "attrs": {"notes": "Onsite rack assembly"},
        },
    )
    assert alloc["status"] == "reserved"
    assert alloc["resource_id"] == res["id"]
    assert alloc["confidence"] == 0.95
    assert alloc["attrs"]["notes"] == "Onsite rack assembly"

    # Consecutive reservation 12:00 to 16:00 is allowed
    consec = await do_reserve(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": res["id"],
            "demand_kind": "work_order",
            "starts_at": "2026-09-06T12:00:00Z",
            "ends_at": "2026-09-06T16:00:00Z",
        },
    )
    assert consec["status"] == "reserved"

    # Release first reservation
    released = await do_release(
        mock_engine,
        {"namespace_id": ns_id, "allocation_id": alloc["id"]},
    )
    assert released["status"] == "released"

    # Now re-reserving 08:00 to 12:00 succeeds because original is released
    re_reserved = await do_reserve(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": res["id"],
            "demand_kind": "service",
            "starts_at": "2026-09-06T08:00:00Z",
            "ends_at": "2026-09-06T12:00:00Z",
        },
    )
    assert re_reserved["status"] == "reserved"


@pytest.mark.asyncio
async def test_reserve_concurrency_conflict(mock_engine):
    ns_id = uuid4()
    res = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "vehicle", "display_name": "Van Gamma"},
    )

    await do_reserve(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": res["id"],
            "demand_kind": "project",
            "starts_at": "2026-09-06T08:00:00Z",
            "ends_at": "2026-09-06T12:00:00Z",
        },
    )

    # Overlapping reservation: 10:00 to 14:00 must raise ResourceConcurrencyError
    with pytest.raises(ResourceConcurrencyError):
        await do_reserve(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": res["id"],
                "demand_kind": "service",
                "starts_at": "2026-09-06T10:00:00Z",
                "ends_at": "2026-09-06T14:00:00Z",
            },
        )


@pytest.mark.asyncio
async def test_reserve_validation_errors(mock_engine):
    ns_id = uuid4()
    res = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "tool", "display_name": "Fusion Splicer"},
    )

    # ends_at <= starts_at
    with pytest.raises(ResourceValidationError, match="must be after starts_at"):
        await do_reserve(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": res["id"],
                "demand_kind": "project",
                "starts_at": "2026-09-06T12:00:00Z",
                "ends_at": "2026-09-06T10:00:00Z",
            },
        )

    # Missing demand_kind
    with pytest.raises(ResourceValidationError, match="demand_kind is required"):
        await do_reserve(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": res["id"],
                "starts_at": "2026-09-06T10:00:00Z",
                "ends_at": "2026-09-06T12:00:00Z",
            },
        )

    # Invalid status
    with pytest.raises(ResourceValidationError, match="Invalid allocation status"):
        await do_reserve(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": res["id"],
                "demand_kind": "project",
                "starts_at": "2026-09-06T10:00:00Z",
                "ends_at": "2026-09-06T12:00:00Z",
                "status": "bogus_status",
            },
        )

    # Non-existent resource
    with pytest.raises(ResourceNotFoundError):
        await do_reserve(
            mock_engine,
            {
                "namespace_id": ns_id,
                "resource_id": uuid4(),
                "demand_kind": "project",
                "starts_at": "2026-09-06T10:00:00Z",
                "ends_at": "2026-09-06T12:00:00Z",
            },
        )


@pytest.mark.asyncio
async def test_contractor_subscope_redaction(mock_engine):
    """External contractor resources must have sensitive internal fields redacted (Charter §6.4)."""
    ns_id = uuid4()
    contractor = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "contractor",
            "display_name": "External Electrician AS",
            "attrs": {"rate_nok": 1150, "internal_margin_pct": 32.5},
        },
    )

    alloc = await do_reserve(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": contractor["id"],
            "demand_kind": "project",
            "starts_at": "2026-09-07T08:00:00Z",
            "ends_at": "2026-09-07T16:00:00Z",
            "attrs": {"secret_pricing_notes": "Do not disclose margin"},
        },
    )

    # Must be redacted
    assert alloc["redaction"] == "contractor_allow_list_enforced"
    assert "attrs" not in alloc
    assert "secret_pricing_notes" not in alloc
    assert "margin" not in alloc
    # Allowed fields present
    assert alloc["resource_id"] == contractor["id"]
    assert alloc["status"] == "reserved"


@pytest.mark.asyncio
async def test_detect_conflicts_query(mock_engine):
    ns_id = uuid4()
    res = await do_create_resource(
        mock_engine,
        {"namespace_id": ns_id, "kind": "employee", "display_name": "Conflict Tech"},
    )

    # Directly insert two overlapping allocations into engine store
    a1_id = str(uuid4())
    a2_id = str(uuid4())
    t1 = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)

    mock_engine.allocations[a1_id] = {
        "id": a1_id,
        "namespace_id": str(ns_id),
        "resource_id": res["id"],
        "starts_at": t1,
        "ends_at": t2,
        "demand_kind": "project",
        "status": "reserved",
    }
    mock_engine.allocations[a2_id] = {
        "id": a2_id,
        "namespace_id": str(ns_id),
        "resource_id": res["id"],
        "starts_at": t3,
        "ends_at": t4,
        "demand_kind": "service",
        "status": "reserved",
    }

    res_conflicts = await do_detect_conflicts(
        mock_engine,
        {"namespace_id": ns_id, "resource_id": res["id"]},
    )
    assert res_conflicts["total_conflicts"] == 1
    assert len(res_conflicts["conflicts"]) == 1

    # Direct test of redact_contractor_view
    full_view = {
        "id": a1_id,
        "namespace_id": str(ns_id),
        "resource_id": res["id"],
        "demand_kind": "project",
        "margin_nok": 5000,
        "internal_rate": 850,
    }
    redacted = redact_contractor_view(full_view)
    assert "margin_nok" not in redacted
    assert "internal_rate" not in redacted
    assert redacted["redaction"] == "contractor_allow_list_enforced"
