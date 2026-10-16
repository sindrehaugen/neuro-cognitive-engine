"""Unit tests for Business Insights natural language query and semantic memory recall."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.business_insights.ask import do_ask_business


class _AsyncConnCtx:
    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_do_ask_business_semantic_memory_ranking() -> None:
    """Verify that when query embedding is provided, memories are queried by vector distance."""
    ns_id = uuid4()
    mem1_id = uuid4()
    mem2_id = uuid4()

    conn = AsyncMock()

    queries_run = []

    async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        queries_run.append((query, args))
        q = query.lower()
        if "from business_insights_kpi_snapshots" in q:
            return []
        if "from memories" in q:
            # Return memory rows ranked by semantic distance
            return [
                {"id": mem1_id, "memory_type": "quarterly_q3", "created_at": "2026-09-01"},
                {"id": mem2_id, "memory_type": "quarterly_q2", "created_at": "2026-06-01"},
            ]
        return []

    conn.fetch = AsyncMock(side_effect=fake_fetch)
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncConnCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool

    dummy_vec = [0.1] * 768

    result = await do_ask_business(
        engine,
        {
            "namespace_id": str(ns_id),
            "principal_role": "executive",
            "question": "How did previous quarters compare on margin?",
            "embedding": dummy_vec,
        },
    )

    assert result["status"] == "ok"
    # Find memories query
    mem_queries = [q for q, _ in queries_run if "from memories" in q.lower()]
    assert len(mem_queries) >= 1
    assert "distance" in mem_queries[0].lower() or "embedding <=>" in mem_queries[0].lower()

    recall = result["cognitive_recall"]
    assert str(mem1_id) in recall["prior_similar_periods"]
    assert str(mem2_id) in recall["prior_similar_periods"]
