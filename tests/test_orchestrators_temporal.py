"""Unit tests for nce.orchestrators.temporal (compare_states hardening)."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.models import (
    AssertionType,
    CompareStatesRequest,
    MemoryType,
)
from nce.orchestrators import temporal as temporal_mod
from nce.orchestrators.temporal import (
    TemporalOrchestrator,
    _cap_diff_list,
    _metadata_as_dict,
    _normalize_compare_query,
    _validate_compare_window,
)

NS = UUID("550e8400-e29b-41d4-a716-446655440000")
T_A = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T_B = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)


def _compare_payload(**overrides: object) -> CompareStatesRequest:
    base = {
        "namespace_id": NS,
        "as_of_a": T_A,
        "as_of_b": T_B,
        "query": "network outage",
        "top_k": 10,
    }
    base.update(overrides)
    return CompareStatesRequest(**base)


def _memory_row(memory_id: UUID, payload_ref: str) -> dict:
    return {
        "memory_id": memory_id,
        "namespace_id": NS,
        "agent_id": "default",
        "payload_ref": payload_ref,
        "assertion_type": AssertionType.fact,
        "memory_type": MemoryType.episodic,
        "valid_from": T_A,
        "pii_redacted": False,
        "derived_from": None,
        "metadata": {},
        "salience": 0.5,
    }


class _FakeAcquire:
    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def temporal_orchestrator() -> TemporalOrchestrator:
    pool = MagicMock()
    pool.acquire.return_value = _FakeAcquire(MagicMock())
    mongo = MagicMock()
    mongo.memory_archive = MagicMock()
    orch = TemporalOrchestrator(
        pg_pool=pool,
        mongo_client=mongo,
        semantic_search_fn=AsyncMock(return_value=[]),
    )
    return orch


class TestCompareValidation:
    def test_rejects_equal_timestamps(self) -> None:
        with pytest.raises(ValueError, match="as_of_a must be strictly before"):
            _validate_compare_window(T_B, T_B)

    def test_rejects_whitespace_query(self) -> None:
        with pytest.raises(ValueError, match="whitespace-only"):
            _normalize_compare_query("   ")

    def test_rejects_oversized_query(self) -> None:
        with pytest.raises(ValueError, match="maximum length"):
            _normalize_compare_query("x" * 2049)


class TestMetadataParsing:
    def test_invalid_metadata_json_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=temporal_mod.log.name)
        assert _metadata_as_dict("{not-json") == {}
        assert any("Invalid memories.metadata JSON" in r.message for r in caplog.records)


class TestCapDiffList:
    def test_truncates_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger=temporal_mod.log.name)
        items = list(range(temporal_mod.MAX_DIFF_ITEMS + 5))
        out = _cap_diff_list("added", items)
        assert len(out) == temporal_mod.MAX_DIFF_ITEMS
        assert any("truncated" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestCompareStatesSemanticPath:
    async def test_deterministic_added_removed_order(
        self, temporal_orchestrator: TemporalOrchestrator
    ) -> None:
        id_lo = UUID("00000000-0000-4000-8000-000000000002")
        id_hi = UUID("00000000-0000-4000-8000-000000000001")
        ref_lo = f"{24 * 'a'}"
        ref_hi = f"{24 * 'b'}"

        def _semantic_side_effect(
            _query: str,
            _ns: str,
            _agent: str,
            *,
            limit: int,
            offset: int,
            as_of: datetime,
        ) -> list[dict]:
            if as_of == T_A:
                return [{"memory_id": str(id_hi), "score": 0.9, "raw_data": "b"}]
            return [
                {"memory_id": str(id_hi), "score": 0.9, "raw_data": "b"},
                {"memory_id": str(id_lo), "score": 0.8, "raw_data": "a"},
            ]

        temporal_orchestrator._semantic_search_fn = AsyncMock(side_effect=_semantic_side_effect)

        conn = MagicMock()

        async def fetch_valid_at(
            _conn: object,
            _ns: UUID,
            memory_ids: list[UUID],
            _as_of: datetime,
        ) -> dict[str, dict]:
            out: dict[str, dict] = {}
            for mid in memory_ids:
                ref = ref_hi if mid == id_hi else ref_lo
                out[str(mid)] = _memory_row(mid, ref)
            return out

        temporal_orchestrator._fetch_memories_valid_at = AsyncMock(  # type: ignore[method-assign]
            side_effect=fetch_valid_at
        )

        @asynccontextmanager
        async def fake_scoped(_ns: object):
            yield conn

        temporal_orchestrator.scoped_session = fake_scoped  # type: ignore[method-assign]

        with patch.object(
            temporal_orchestrator,
            "_hydrate_semantic_results",
            new=AsyncMock(),
        ):
            result = await temporal_orchestrator.compare_states(_compare_payload())
            result2 = await temporal_orchestrator.compare_states(_compare_payload())

        assert [str(r.memory_id) for r in result.added] == [str(r.memory_id) for r in result2.added]
        assert [str(r.memory_id) for r in result.added] == [str(id_lo)]

    async def test_hydrate_uses_batch_preview_fetch(
        self, temporal_orchestrator: TemporalOrchestrator
    ) -> None:
        oid = f"{42:024x}"
        row = SimpleNamespace(payload_ref=oid, content_preview=None)
        previews = {oid: "summary text"}

        with patch(
            "nce.orchestrators.temporal.fetch_episode_previews_by_ref",
            new=AsyncMock(return_value=previews),
        ) as mock_fetch:
            await temporal_orchestrator._hydrate_semantic_results([row])

        mock_fetch.assert_awaited_once()
        assert row.content_preview == "summary text"

    async def test_rejects_invalid_timestamp_window(
        self, temporal_orchestrator: TemporalOrchestrator
    ) -> None:
        with pytest.raises(ValueError, match="as_of_a must be strictly before"):
            await temporal_orchestrator.compare_states(_compare_payload(as_of_a=T_B, as_of_b=T_A))


@pytest.mark.asyncio
class TestTriggerConsolidation:
    async def test_uses_create_tracked_task(self) -> None:
        pool = MagicMock()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"metadata": json.dumps({"consolidation": {}})})
        pool.acquire.return_value = _FakeAcquire(conn)
        mongo = MagicMock()

        worker = MagicMock()
        worker.run_consolidation = AsyncMock()

        with (
            patch("nce.orchestrators.temporal.create_tracked_task") as mock_track,
            patch(
                "nce.consolidation.ConsolidationWorker",
                return_value=worker,
            ),
            patch(
                "nce.providers.get_provider",
                return_value=MagicMock(),
            ),
        ):
            orch = TemporalOrchestrator(pool, mongo, semantic_search_fn=AsyncMock())
            res = await orch.trigger_consolidation(str(NS))

        mock_track.assert_called_once()
        assert res["status"] == "triggered"
