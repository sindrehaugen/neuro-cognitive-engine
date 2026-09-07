"""Unit tests for Module 15 Demand Forecasting & Morning Brief Capacity Pulse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources._guard import ResourceValidationError
from nce.vertical_modules.resources.forecast import (
    do_forecast_demand,
    get_morning_brief_capacity_pulse,
)


class MockEngine:
    """Mock engine holding in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.kg_nodes: list[dict[str, Any]] = []
        self.kg_edges: list[dict[str, Any]] = []
        self.mongo_db = None
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "from resources" in q and "where namespace_id = $1" in q:
            ns_id = str(args[0])
            res = [
                r
                for r in engine.resources.values()
                if str(r["namespace_id"]) == ns_id and r.get("active", True)
            ]
            return res

        if "from allocations" in q and "resource_id = any($2::uuid[])" in q:
            ns_id = str(args[0])
            r_ids = [str(rid) for rid in args[1]]
            res = []
            for a in engine.allocations.values():
                if (
                    str(a["namespace_id"]) == ns_id
                    and str(a["resource_id"]) in r_ids
                    and a.get("status") != "released"
                ):
                    res.append(a)
            return res

        if "select distinct resource_id" in q and "from allocations" in q:
            ns_id = str(args[0])
            res = []
            seen = set()
            for a in engine.allocations.values():
                if str(a["namespace_id"]) == ns_id and a.get("status") != "released":
                    rid = str(a["resource_id"])
                    if rid not in seen:
                        seen.add(rid)
                        res.append({"resource_id": rid})
            return res

        if "from kg_nodes" in q and "entity_type = 'project_task'" in q:
            ns_id = str(args[0])
            return [n for n in engine.kg_nodes if str(n.get("namespace_id")) == ns_id]

        if "from kg_edges" in q:
            ns_id = str(args[1])
            labels = set(args[0])
            return [
                e
                for e in engine.kg_edges
                if str(e.get("namespace_id")) == ns_id
                and (e.get("subject_label") in labels or e.get("object_label") in labels)
            ]

        return []

    class MockConn:
        async def fetch(self, query, *args):
            return await mock_fetch(query, *args)

    class MockContextManager:
        async def __aenter__(self):
            return MockConn()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "nce.vertical_modules.resources.forecast.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


@pytest.mark.asyncio
async def test_do_forecast_demand_deficit_and_hiring_signal(mock_engine):
    """Heavy pipeline demand exceeding staff capacity produces capacity gap and hiring recommendation."""
    ns_id = uuid4()
    tech1 = uuid4()

    mock_engine.resources[str(tech1)] = {
        "id": str(tech1),
        "namespace_id": str(ns_id),
        "name": "Tech Alpha",
        "kind": "employee",
        "capacity_pct": 100.0,
        "active": True,
        "metadata": {"role": "technician", "skills": ["crestron"]},
    }

    # 30-day horizon: 1 tech = ~21 days * 7.5 = ~157.5 available hours
    # Pass 400 hours of pipeline project demand -> clear deficit
    res = await do_forecast_demand(
        mock_engine,
        {
            "namespace_id": ns_id,
            "horizon_days": 30,
            "pipeline_demands": [{"project_id": "PRJ-901", "hours": 400.0, "role": "technician"}],
        },
    )

    assert res["status"] == "deficit"
    assert res["capacity_gap_hours"] > 200.0
    assert res["utilization_pct"] > 100.0
    assert any("contractor" in r.lower() or "hiring" in r.lower() for r in res["recommendations"])


@pytest.mark.asyncio
async def test_do_forecast_demand_surplus(mock_engine):
    """Low demand with available capacity produces surplus status and Sales push recommendation."""
    ns_id = uuid4()
    for i in range(3):
        t_id = uuid4()
        mock_engine.resources[str(t_id)] = {
            "id": str(t_id),
            "namespace_id": str(ns_id),
            "name": f"Tech {i}",
            "kind": "employee",
            "capacity_pct": 100.0,
            "active": True,
            "metadata": {"role": "technician"},
        }

    # 3 techs = ~472 hours; demand = 50 hours (< 60% utilization)
    res = await do_forecast_demand(
        mock_engine,
        {
            "namespace_id": ns_id,
            "horizon_days": 30,
            "pipeline_demands": [{"project_id": "PRJ-SMALL", "hours": 50.0}],
        },
    )

    assert res["status"] == "surplus"
    assert res["capacity_gap_hours"] == 0.0
    assert res["utilization_pct"] < 60.0
    assert any("sales" in r.lower() for r in res["recommendations"])


@pytest.mark.asyncio
async def test_do_forecast_demand_horizon_validation(mock_engine):
    """Invalid horizon_days raises ResourceValidationError."""
    ns_id = uuid4()
    with pytest.raises(ResourceValidationError, match="horizon_days"):
        await do_forecast_demand(mock_engine, {"namespace_id": ns_id, "horizon_days": 0})

    with pytest.raises(ResourceValidationError, match="horizon_days"):
        await do_forecast_demand(mock_engine, {"namespace_id": ns_id, "horizon_days": 500})


@pytest.mark.asyncio
async def test_get_morning_brief_capacity_pulse(mock_engine):
    """Morning brief pulse summarizes daily active resources, allocations, and health."""
    ns_id = uuid4()
    t1 = uuid4()
    t2 = uuid4()

    mock_engine.resources[str(t1)] = {
        "id": str(t1),
        "namespace_id": str(ns_id),
        "kind": "employee",
        "active": True,
    }
    mock_engine.resources[str(t2)] = {
        "id": str(t2),
        "namespace_id": str(ns_id),
        "kind": "employee",
        "active": True,
    }

    # Allocate t1 today
    mock_engine.allocations[str(uuid4())] = {
        "id": str(uuid4()),
        "namespace_id": str(ns_id),
        "resource_id": str(t1),
        "starts_at": datetime.now(timezone.utc),
        "ends_at": datetime.now(timezone.utc) + timedelta(hours=6),
        "status": "reserved",
    }

    pulse = await get_morning_brief_capacity_pulse(mock_engine, {"namespace_id": ns_id})
    assert pulse["total_active_resources"] == 2
    assert pulse["allocated_today_resources"] == 1
    assert pulse["available_today_resources"] == 1
    assert pulse["daily_utilization_pct"] == 50.0
    assert pulse["capacity_health"] == "optimal"


@pytest.mark.asyncio
async def test_do_forecast_demand_pipeline_tasks_via_edges(mock_engine):
    """Pipeline demand calculates hours from graph PROJECT_TASK nodes and kg_edges."""
    ns_id = uuid4()
    tech1 = uuid4()

    mock_engine.resources[str(tech1)] = {
        "id": str(tech1),
        "namespace_id": str(ns_id),
        "name": "Tech Alpha",
        "kind": "employee",
        "capacity_pct": 100.0,
        "active": True,
        "metadata": {"role": "technician"},
    }

    # Baseline: without planned tasks in graph, demand is 0 and status is surplus
    res_empty = await do_forecast_demand(
        mock_engine,
        {"namespace_id": ns_id, "horizon_days": 30},
    )
    assert res_empty["pipeline_demand_hours"] == 0.0
    assert res_empty["total_demand_hours"] == 0.0
    assert res_empty["status"] == "surplus"

    # Add planned PROJECT_TASK node and kg_edges with effort and status
    task_label = "TASK:PRJ-100:001"
    mock_engine.kg_nodes.append(
        {
            "label": task_label,
            "entity_type": "PROJECT_TASK",
            "namespace_id": str(ns_id),
            "payload_ref": None,
        }
    )
    mock_engine.kg_edges.extend(
        [
            {
                "namespace_id": str(ns_id),
                "subject_label": "BOM_LINE:001",
                "predicate": "generates",
                "object_label": task_label,
            },
            {
                "namespace_id": str(ns_id),
                "subject_label": task_label,
                "predicate": "has_status",
                "object_label": "STATUS:PLANNED",
            },
            {
                "namespace_id": str(ns_id),
                "subject_label": task_label,
                "predicate": "effort",
                "object_label": "200.0",
            },
        ]
    )

    res_with_task = await do_forecast_demand(
        mock_engine,
        {"namespace_id": ns_id, "horizon_days": 30},
    )
    # Assert demand increased and forecast changed from surplus to deficit
    assert res_with_task["pipeline_demand_hours"] == 200.0
    assert res_with_task["total_demand_hours"] == 200.0
    assert res_with_task["status"] == "deficit"
    assert res_with_task["capacity_gap_hours"] > 0.0
    assert res_with_task["total_demand_hours"] > res_empty["total_demand_hours"]


@pytest.mark.asyncio
async def test_do_forecast_demand_pipeline_tasks_via_payload_ref(mock_engine, monkeypatch):
    """Pipeline demand calculates hours from PROJECT_TASK nodes resolving payload_ref."""
    ns_id = uuid4()
    tech1 = uuid4()

    mock_engine.resources[str(tech1)] = {
        "id": str(tech1),
        "namespace_id": str(ns_id),
        "name": "Tech Beta",
        "kind": "employee",
        "capacity_pct": 100.0,
        "active": True,
        "metadata": {"role": "technician"},
    }

    mock_engine.mongo_db = MagicMock()
    fake_ref = "60d5ecb8b5c9c61234567890"
    mock_engine.kg_nodes.append(
        {
            "label": "TASK:PRJ-200:001",
            "entity_type": "PROJECT_TASK",
            "namespace_id": str(ns_id),
            "payload_ref": fake_ref,
        }
    )

    async def _fake_fetch_raw(db, refs):
        return {fake_ref: {"status": "scheduled", "estimated_hours": 250.0}}

    monkeypatch.setattr("nce.mongo_bulk.fetch_episodes_raw_by_ref", _fake_fetch_raw)

    res = await do_forecast_demand(
        mock_engine,
        {"namespace_id": ns_id, "horizon_days": 30},
    )
    assert res["pipeline_demand_hours"] == 250.0
    assert res["total_demand_hours"] == 250.0
    assert res["status"] == "deficit"
