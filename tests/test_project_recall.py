"""
tests/test_project_recall.py
============================
Integration tests for project/recall.py — Wave 11 (cognitive-recall).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.embeddings import _deterministic_hash_embedding
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.project.recall import (
    do_recall_similar_projects,
    do_record_project_outcome,
    do_suggest_pl,
)


def _make_engine_stub(pg_pool: asyncpg.Pool) -> Any:  # type: ignore[type-arg]
    class _EngineStub:
        pass

    stub = _EngineStub()
    stub.pg_pool = pg_pool  # type: ignore[attr-defined]
    return stub


async def mock_embed(text: str) -> list[float]:
    """Offline deterministic mock for embed using SHA-256 hash."""
    return _deterministic_hash_embedding(text)


class MockA2AClient:
    """Mock A2A client for testing do_suggest_pl."""

    def __init__(self, suggest_result: Any = None, raise_exc: Exception | None = None) -> None:
        self.suggest_result = suggest_result
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> Any:
        self.calls.append((tool_name, params))
        if self.raise_exc:
            raise self.raise_exc
        return self.suggest_result


@pytest.mark.integration
@pytest.mark.asyncio
class TestProjectRecall:
    """Integration suite for cognitive project recall."""

    @pytest.fixture(autouse=True)
    def patch_embed(self):
        """Ensure all embedding requests in recall use the fast offline mock."""
        with patch("nce.vertical_modules.project.recall.embed", side_effect=mock_embed):
            yield

    async def test_do_record_project_outcome_and_recall(
        self, pg_pool: asyncpg.Pool, make_namespace: Any
    ) -> None:
        """Recording project outcomes writes to database and allows recall ranking."""
        ns_uuid = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # Seed node ownership for the project engine
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_uuid)
                await seed_node_ownership_registry(conn, ns_uuid)

        # Record a past slipped project outcome
        project_id = "PROJECT:PAST_SLIPPED_01"
        description = (
            "Large scale AV deployment with multi-room matrix switches and network routing issues."
        )
        slip_reason = (
            "Network switch hardware delayed by manufacturer, resulting in project timeline slip."
        )

        record_res = await do_record_project_outcome(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": project_id,
                "description": description,
                "slip_reason": slip_reason,
                "margin_drift": -0.15,
                "gate_dwell_time": 45,
            },
        )
        assert record_res["ok"] is True
        assert "memory_id" in record_res

        # Record a second outcome to test ranking
        project_id_2 = "PROJECT:PAST_SLIPPED_02"
        description_2 = "Small huddle room layout installation with minimal audio components."
        slip_reason_2 = "Minor cable routing issues, resolved quickly."

        record_res_2 = await do_record_project_outcome(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": project_id_2,
                "description": description_2,
                "slip_reason": slip_reason_2,
                "margin_drift": -0.02,
                "gate_dwell_time": 5,
            },
        )
        assert record_res_2["ok"] is True

        # Now test recall using a description similar to the first project
        recall_res = await do_recall_similar_projects(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": "PROJECT:ACTIVE_01",
                "description": "AV deployment using network switches and multi-room audio matrices.",
                "top_k": 5,
            },
        )

        assert len(recall_res) >= 2
        # The first project should be ranked higher due to similarity in switches/matrices
        assert recall_res[0]["project"] == project_id
        assert recall_res[0]["slip_reason"] == slip_reason
        assert recall_res[0]["similarity"] > recall_res[1]["similarity"]

        # Verify idempotency by overwriting project_id_2
        new_slip_reason_2 = "Overwritten new slip reason."
        record_res_2_overwrite = await do_record_project_outcome(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": project_id_2,
                "description": description_2,
                "slip_reason": new_slip_reason_2,
            },
        )
        assert record_res_2_overwrite["ok"] is True

        # Recall again and check that project_id_2's reason is updated and no duplicates exist
        recall_res_after = await do_recall_similar_projects(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": "PROJECT:ACTIVE_01",
                "description": "huddle room audio routing",
            },
        )
        # Should rank project 2 first now
        assert recall_res_after[0]["project"] == project_id_2
        assert recall_res_after[0]["slip_reason"] == new_slip_reason_2

        # Verify no duplicate entries for project_id_2 in memories
        async with pg_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE namespace_id = $1::uuid AND name = $2 AND node_type = 'PROJECT'",
                str(ns_uuid),
                project_id_2,
            )
            assert count == 1

    async def test_do_suggest_pl_graceful_degradation(
        self, pg_pool: asyncpg.Pool, make_namespace: Any
    ) -> None:
        """do_suggest_pl degrades gracefully when HR is unavailable."""
        ns_uuid = await make_namespace()
        engine = _make_engine_stub(pg_pool)

        # 1. No A2A client
        res = await do_suggest_pl(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": "PROJECT:ACTIVE_01",
            },
        )
        assert res["ok"] is False
        assert "HR unavailable" in res["error"]

        # 2. A2A client call raises exception (e.g. tool not registered)
        failed_client = MockA2AClient(raise_exc=RuntimeError("Tool hr.suggest_pl not registered"))
        res_fail = await do_suggest_pl(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": "PROJECT:ACTIVE_01",
                "a2a_client": failed_client,
            },
        )
        assert res_fail["ok"] is False
        assert "HR unavailable" in res_fail["error"]

        # 3. Successful tool call with mock client
        mock_suggestion = {"employee_id": "EMP:123", "fit_score": 0.95}
        success_client = MockA2AClient(suggest_result=mock_suggestion)
        res_success = await do_suggest_pl(
            engine,
            {
                "namespace_id": ns_uuid,
                "project_id": "PROJECT:ACTIVE_01",
                "a2a_client": success_client,
            },
        )
        assert res_success["ok"] is True
        assert res_success["suggestion"] == mock_suggestion
        assert len(success_client.calls) == 1
        assert success_client.calls[0] == (
            "hr.suggest_pl",
            {"namespace_id": str(ns_uuid), "project_id": "PROJECT:ACTIVE_01"},
        )
