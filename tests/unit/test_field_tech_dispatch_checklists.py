"""Unit tests for Field Tech AI Dispatch rework penalization and checklists."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.vertical_modules.field_tech.dispatch import do_dispatch


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
    @asynccontextmanager
    async def _fake_scoped(pool: Any, ns: Any) -> Any:
        ctx = pool.acquire()
        if hasattr(ctx, "__aenter__"):
            conn = await ctx.__aenter__()
        else:
            conn = ctx
        yield conn

    monkeypatch.setattr(
        "nce.vertical_modules.field_tech.dispatch.scoped_pg_session",
        _fake_scoped,
    )


@pytest.mark.asyncio
async def test_dispatch_penalizes_technician_rework_history() -> None:
    """Technician with high rework rate has quality score discounted vs clean history."""
    ns_id = str(uuid4())
    wo_id = "WO-REWORK-001"

    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "work_order_id": wo_id,
        "namespace_id": UUID(ns_id),
        "kind": "service",
        "location_id": "LOC-OSLO",
        "raw": {"required_skills": ["cctv"]},
    }

    # 1. Active loads (both have 0 active load)
    # 2. History rows from v3_cognitive_ledger:
    #    Tech A: 5 completed jobs, avg rating 4.8, avg quality 0.95, 0 reworks
    #    Tech B: 5 completed jobs, avg rating 4.8, avg quality 0.95, 3 reworks (60% rework)
    conn.fetch.side_effect = [
        # load_rows
        [
            {"assignee_id": "tech_clean", "open_count": 0},
            {"assignee_id": "tech_rework", "open_count": 0},
        ],
        # history_rows
        [
            {
                "completed_by": "tech_clean",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 0,
            },
            {
                "completed_by": "tech_rework",
                "completed_count": 5,
                "avg_rating": 4.8,
                "avg_quality": 0.95,
                "rework_count": 3,
            },
        ],
    ]

    pool = _make_mock_pool(conn)

    candidates = [
        {
            "id": "tech_clean",
            "name": "Clean Tech",
            "skills": ["cctv"],
            "location_id": "LOC-OSLO",
        },
        {
            "id": "tech_rework",
            "name": "Rework Tech",
            "skills": ["cctv"],
            "location_id": "LOC-OSLO",
        },
    ]

    res = await do_dispatch(
        pool,
        {
            "namespace_id": ns_id,
            "work_order_id": wo_id,
            "candidates": candidates,
        },
    )

    ranked = res["ranked_candidates"]
    assert len(ranked) == 2
    # Clean tech must rank higher than rework tech due to rework penalty
    assert ranked[0]["id"] == "tech_clean"
    assert ranked[1]["id"] == "tech_rework"
    assert ranked[0]["score"] > ranked[1]["score"]
    assert ranked[0]["component_scores"]["history"] > ranked[1]["component_scores"]["history"]
    assert "reworks" in ranked[1]["rationale"]
