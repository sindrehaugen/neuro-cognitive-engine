"""Unit tests for Module 15 Staff & Resources Engine AI Planner and Tiered Autonomy."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.resources.planner import (
    do_plan_allocation,
    do_record_allocation_outcome,
    load_allocation_weights,
)
from nce.vertical_modules.resources.registry import do_create_resource


class MockEngine:
    """Mock engine holding an in-memory store simulating PostgreSQL tenant isolation."""

    def __init__(self):
        self.resources: dict[str, dict[str, Any]] = {}
        self.allocations: dict[str, dict[str, Any]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.pg_pool = MagicMock()


@pytest.fixture
def mock_engine(monkeypatch):
    engine = MockEngine()

    async def mock_fetchrow(query, *args):
        q = query.strip().lower()
        if "from v3_cognitive_ledger" in q and "tlx_scores->>'resource_id' = $2" in q:
            ns_id = str(args[0])
            res_id = str(args[1])
            matches = [
                row
                for row in engine.ledger
                if str(row["namespace_id"]) == ns_id
                and row["tlx_scores"].get("resource_id") == res_id
            ]
            if not matches:
                return {"total_jobs": 0, "avg_rating": None, "avg_quality": None}
            total = len(matches)
            avg_r = sum(float(m["tlx_scores"].get("rating", 5.0)) for m in matches) / total
            avg_q = sum(float(m["tlx_scores"].get("quality_score", 1.0)) for m in matches) / total
            return {"total_jobs": total, "avg_rating": avg_r, "avg_quality": avg_q}

        if "select id, kind from resources" in q and "where id = $1 and namespace_id = $2" in q:
            res_id, ns_id = str(args[0]), str(args[1])
            for r in engine.resources.values():
                if str(r["id"]) == res_id and str(r["namespace_id"]) == ns_id:
                    return r
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

    async def mock_fetch(query, *args):
        q = query.strip().lower()
        if "from resources" in q and "where namespace_id = $1 and kind = $2" in q:
            ns_id = str(args[0])
            kind = str(args[1])
            return [
                r
                for r in engine.resources.values()
                if str(r["namespace_id"]) == ns_id and r["kind"] == kind
            ]
        return []

    async def mock_fetchval(query, *args):
        q = query.strip().lower()
        if "select 1 from allocations" in q:
            # Conflict check
            ns_id = str(args[0])
            res_id = str(args[1])
            starts_at = args[2]
            ends_at = args[3]
            for a in engine.allocations.values():
                if (
                    str(a["namespace_id"]) == ns_id
                    and str(a["resource_id"]) == res_id
                    and a["status"] != "released"
                ):
                    if max(starts_at, a["starts_at"]) < min(ends_at, a["ends_at"]):
                        return 1
            return None

        if "select count(*) from allocations" in q:
            return 0

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

        if "insert into v3_cognitive_ledger" in q:
            row = {
                "id": args[0],
                "namespace_id": args[1],
                "tlx_scores": json.loads(args[2]) if isinstance(args[2], str) else args[2],
                "model_version": args[3],
                "created_at": datetime.now(timezone.utc),
            }
            engine.ledger.append(row)
            return "INSERT 0 1"

        return ""

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
        "nce.vertical_modules.resources.allocations.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )
    monkeypatch.setattr(
        "nce.vertical_modules.resources.planner.scoped_pg_session",
        lambda pool, ns: MockContextManager(),
    )

    return engine


def test_load_allocation_weights():
    weights = load_allocation_weights()
    assert "skill_match_weight" in weights
    assert "travel_distance_weight" in weights
    assert "load_balance_weight" in weights
    assert "internal_preference_weight" in weights
    assert "outcome_history_weight" in weights
    assert sum(weights.values()) == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_plan_allocation_skill_match_and_ranking(mock_engine):
    ns_id = uuid4()

    # Create Tech 1 (full skill match) and Tech 2 (partial skill match)
    tech_1 = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "display_name": "Expert Tech Alpha",
            "attrs": {"skills": ["av_dsp", "crestron", "dante"], "base_location": "Oslo"},
        },
    )
    _tech_2 = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "display_name": "Junior Tech Beta",
            "attrs": {"skills": ["crestron"], "base_location": "Oslo"},
        },
    )

    plan = await do_plan_allocation(
        mock_engine,
        {
            "namespace_id": ns_id,
            "demand_kind": "project",
            "required_skills": ["av_dsp", "dante"],
            "required_kinds": ["employee"],
            "starts_at": "2026-09-15T08:00:00Z",
            "ends_at": "2026-09-15T16:00:00Z",
            "location": "Oslo",
            "auto_reserve": False,
        },
    )

    assert plan["status"] == "suggested"
    assert len(plan["plan"]) == 1
    winner = plan["plan"][0]
    # Expert Tech Alpha must win due to 100% skill match vs 0%
    assert winner["resource_id"] == tech_1["id"]
    assert winner["score_breakdown"]["skill_match"] == 1.0


@pytest.mark.asyncio
async def test_plan_allocation_cognitive_recall_feedback(mock_engine):
    """Proves cognitive recall from v3_cognitive_ledger improves ranking (Spec §79)."""
    ns_id = uuid4()

    _tech_a = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "display_name": "Tech Without History",
            "attrs": {"skills": ["cabling"], "base_location": "Bergen"},
        },
    )
    tech_b = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "employee",
            "display_name": "Tech With Top History",
            "attrs": {"skills": ["cabling"], "base_location": "Bergen"},
        },
    )

    # Record 5-star outcomes for Tech B in v3_cognitive_ledger
    await do_record_allocation_outcome(
        mock_engine,
        {
            "namespace_id": ns_id,
            "resource_id": tech_b["id"],
            "rating": 5.0,
            "quality_score": 1.0,
            "demand_kind": "project",
        },
    )

    plan = await do_plan_allocation(
        mock_engine,
        {
            "namespace_id": ns_id,
            "demand_kind": "project",
            "required_skills": ["cabling"],
            "required_kinds": ["employee"],
            "starts_at": "2026-09-16T08:00:00Z",
            "ends_at": "2026-09-16T16:00:00Z",
            "location": "Bergen",
            "auto_reserve": False,
        },
    )

    winner = plan["plan"][0]
    assert winner["resource_id"] == tech_b["id"]
    assert winner["cognitive_recall"]["historical_jobs"] == 1
    assert winner["cognitive_recall"]["avg_rating"] == 5.0


@pytest.mark.asyncio
async def test_plan_allocation_tiered_autonomy(mock_engine):
    """Proves tiered autonomy gating against AUTONOMY_ALLOCATION_CEILING (Spec §78)."""
    ns_id = uuid4()

    _van = await do_create_resource(
        mock_engine,
        {
            "namespace_id": ns_id,
            "kind": "vehicle",
            "display_name": "Autonomy Van",
            "attrs": {"base_location": "Trondheim"},
        },
    )

    # 1. Sub-threshold job (20,000 NOK <= 50,000 NOK ceiling) with auto_reserve=True
    # Acts autonomously as Actor -> reserves immediately!
    sub_res = await do_plan_allocation(
        mock_engine,
        {
            "namespace_id": ns_id,
            "demand_kind": "service",
            "required_kinds": ["vehicle"],
            "starts_at": "2026-09-17T08:00:00Z",
            "ends_at": "2026-09-17T12:00:00Z",
            "location": "Trondheim",
            "estimated_value_nok": 20000.0,
            "auto_reserve": True,
        },
    )
    assert sub_res["status"] == "reserved"
    assert sub_res["autonomous"] is True
    assert sub_res["requires_approval"] is False
    assert len(sub_res["allocations"]) == 1

    # 2. Over-threshold job (150,000 NOK > 50,000 NOK ceiling) with auto_reserve=True
    # Gated as Advisor -> human approval strictly required!
    over_res = await do_plan_allocation(
        mock_engine,
        {
            "namespace_id": ns_id,
            "demand_kind": "project",
            "required_kinds": ["vehicle"],
            "starts_at": "2026-09-18T08:00:00Z",
            "ends_at": "2026-09-18T16:00:00Z",
            "location": "Trondheim",
            "estimated_value_nok": 150000.0,
            "auto_reserve": True,
        },
    )
    assert over_res["status"] == "suggested"
    assert over_res["autonomous"] is False
    assert over_res["requires_approval"] is True
    assert "exceeds autonomy ceiling" in over_res["rationale"]
