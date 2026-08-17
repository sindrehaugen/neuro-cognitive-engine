"""
tests/test_vendors_contractor_match.py
=======================================
Integration tests for contractor matching (Batch 102).
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.a2a_server import NamespaceContext, _dispatch_skill
from nce.db_utils import scoped_pg_session
from nce.vertical_modules.vendors.contractors import do_upsert_contractor
from nce.vertical_modules.vendors.matching import do_match_contractor


class EngineStub:
    """Stub representing the core engine context passed to vertical modules."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = None
        self.redis_client = None


async def _seed_ownership(
    conn: asyncpg.Connection, ns_id: uuid.UUID, node_type: str, owner_engine: str
) -> None:
    """Seed the node ownership registry for tests."""
    await conn.execute(
        """
        INSERT INTO node_ownership_registry (namespace_id, node_type, owner_engine)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        """,
        ns_id,
        node_type,
        owner_engine,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contractor_matching_logic(pg_pool: asyncpg.Pool, make_namespace: Any) -> None:
    """Verify do_match_contractor matches, scores, and ranks contractors correctly."""
    ns_id = await make_namespace()
    engine = EngineStub(pg_pool)

    # 1. Seed ownership for CONTRACTOR nodes
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await _seed_ownership(conn, ns_id, "CONTRACTOR", "vendors")

    partner_scope = uuid.uuid4()

    # 2. Seed contractors
    # Contractor A: high performance (95), matching location (Oslo), matching skills, 0 workload
    await do_upsert_contractor(
        engine,
        {
            "namespace_id": ns_id,
            "contractor_id": "CONTRACTOR:ALICE",
            "partner_scope_id": partner_scope,
            "profile": {"name": "Alice Smith", "location": "Oslo"},
            "skills": ["dsp", "crossover", "acoustics"],
            "performance_score": 95.0,
        },
    )

    # Contractor B: lower performance (75), matching location (Oslo), matching skills, 0 workload
    await do_upsert_contractor(
        engine,
        {
            "namespace_id": ns_id,
            "contractor_id": "CONTRACTOR:BOB",
            "partner_scope_id": partner_scope,
            "profile": {"name": "Bob Vance", "location": "Oslo"},
            "skills": ["dsp", "crossover"],
            "performance_score": 75.0,
        },
    )

    # Contractor C: high performance (95), non-matching location (Bergen), matching skills, 0 workload
    await do_upsert_contractor(
        engine,
        {
            "namespace_id": ns_id,
            "contractor_id": "CONTRACTOR:CHARLIE",
            "partner_scope_id": partner_scope,
            "profile": {"name": "Charlie Brown", "location": "Bergen"},
            "skills": ["dsp", "crossover", "acoustics"],
            "performance_score": 95.0,
        },
    )

    # 3. Match request
    params = {
        "namespace_id": ns_id,
        "job": {
            "skills": ["dsp", "crossover"],
            "location": "Oslo",
        },
    }

    res = await do_match_contractor(engine, params)
    assert res["ok"] is True
    matches = res["matches"]
    assert len(matches) == 3

    # Alice should be first (score = 1.0 skills, 1.0 loc, 1.0 load, 0.95 hist)
    # Bob should be second (score = 1.0 skills, 1.0 loc, 1.0 load, 0.75 hist)
    # Charlie should be third (score = 1.0 skills, 0.0 loc, 1.0 load, 0.95)
    assert matches[0]["contractor_id"] == "CONTRACTOR:ALICE"
    assert matches[1]["contractor_id"] == "CONTRACTOR:BOB"
    assert matches[2]["contractor_id"] == "CONTRACTOR:CHARLIE"

    # 4. Test load factor: Add active work order assignments in kg_edges for Alice
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, change_origin)
            VALUES ('WORK_ORDER:1', 'assigned_to', 'CONTRACTOR:ALICE', $1, 'agent'),
                   ('WORK_ORDER:2', 'assigned_to', 'CONTRACTOR:ALICE', $1, 'agent')
            ON CONFLICT DO NOTHING
            """,
            ns_id,
        )

    # Alice's load score drops to 1/(1+2) = 0.333
    # Bob has 0 load (score = 1.0), so Bob should take the #1 spot
    res2 = await do_match_contractor(engine, params)
    assert res2["ok"] is True
    matches2 = res2["matches"]
    assert matches2[0]["contractor_id"] == "CONTRACTOR:BOB"
    assert matches2[1]["contractor_id"] == "CONTRACTOR:ALICE"

    # 5. Test graceful degradation when location or skills are missing in the request
    params_no_loc = {
        "namespace_id": ns_id,
        "job": {
            "skills": ["dsp"],
        },
    }
    res3 = await do_match_contractor(engine, params_no_loc)
    assert res3["ok"] is True
    assert len(res3["matches"]) == 3

    params_no_skills = {
        "namespace_id": ns_id,
        "job": {
            "location": "Oslo",
        },
    }
    res4 = await do_match_contractor(engine, params_no_skills)
    assert res4["ok"] is True
    assert len(res4["matches"]) == 3

    # 6. Test A2A dispatch
    caller_ctx = NamespaceContext(
        namespace_id=ns_id,
        agent_id="test-agent",
        principal_kind="employee",
    )
    import nce.a2a_server as a2a_server

    a2a_server._engine = engine

    a2a_res = await _dispatch_skill(
        "vendors_match_contractor",
        {
            "namespace_id": str(ns_id),
            "job": {
                "skills": ["dsp"],
                "location": "Oslo",
            },
        },
        caller_ctx,
    )
    assert a2a_res["ok"] is True
    assert len(a2a_res["matches"]) == 3
