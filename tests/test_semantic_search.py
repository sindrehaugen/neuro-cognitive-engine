"""Unit tests for nce.semantic_search (batch 1–3 hardening)."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from nce.embeddings import VECTOR_DIM
from nce.semantic_search import (
    _MAX_RAW_DATA_CHARS,
    semantic_search,
)

NS = "00000000-0000-4000-8000-000000000001"
AGENT = "test-agent"


def _fake_scoped(mock_conn):
    @asynccontextmanager
    async def _scoped(_pool, _namespace_id):
        yield mock_conn

    return _scoped


def _pg_row(*, payload_ref: str, memory_id, score: float):
    return {
        "payload_ref": payload_ref,
        "memory_id": memory_id,
        "final_score": score,
    }


def _base_pg_conn(rows=None):
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=rows or [])
    return mock_conn


def _mongo_client(*, episode_docs: dict[str, dict] | None = None):
    """episode_docs maps payload_ref (str) -> episode document."""
    docs = episode_docs or {}
    find_calls: list[dict] = []

    async def _find(query, projection=None):
        find_calls.append({"query": query, "projection": projection})
        ids = query.get("_id", {}).get("$in", [])
        for oid in ids:
            doc = docs.get(str(oid))
            if doc is not None:
                yield doc

    episodes = MagicMock()
    episodes.find = MagicMock(side_effect=_find)
    episodes._find_calls = find_calls

    client = MagicMock()
    client.memory_archive = MagicMock()
    client.memory_archive.episodes = episodes
    return client


async def _run_search(
    *,
    embedding_fn,
    pg_rows=None,
    mongo_docs=None,
    limit: int = 5,
    schedule_reinforcement: bool = False,
):
    mock_conn = _base_pg_conn(pg_rows)
    mongo = _mongo_client(episode_docs=mongo_docs)
    pool = MagicMock()

    def _discard_background_task(coro):
        coro.close()
        return MagicMock()

    create_task_patch = (
        patch("nce.semantic_search.asyncio.create_task")
        if schedule_reinforcement
        else patch(
            "nce.semantic_search.asyncio.create_task",
            side_effect=_discard_background_task,
        )
    )

    with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
        with create_task_patch:
            return await semantic_search(
                pg_pool=pool,
                mongo_client=mongo,
                embedding_fn=embedding_fn,
                query="network topology",
                namespace_id=NS,
                agent_id=AGENT,
                limit=limit,
            )


# ---------------------------------------------------------------------------
# Batch 1 — embedding protection, sort stability, raw_data cap
# ---------------------------------------------------------------------------


class TestBatch1EmbeddingProtection:
    @pytest.mark.asyncio
    async def test_embedding_fn_timeout_raises(self) -> None:
        async def slow_embed(_query: str):
            await asyncio.sleep(999)

        with patch("nce.semantic_search._EMBED_TIMEOUT_SECONDS", 0.05):
            with pytest.raises(asyncio.TimeoutError):
                await _run_search(embedding_fn=slow_embed)

    @pytest.mark.asyncio
    async def test_embedding_fn_wrong_dimension_raises(self) -> None:
        async def bad_embed(_query: str):
            return [0.1] * (VECTOR_DIM - 1)

        with pytest.raises(ValueError, match=f"expected {VECTOR_DIM}"):
            await _run_search(embedding_fn=bad_embed)


class TestBatch1SortStability:
    @pytest.mark.asyncio
    async def test_equal_scores_use_memory_id_tiebreak_in_sql(self) -> None:
        mock_conn = _base_pg_conn([])
        pool = MagicMock()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                await semantic_search(
                    pg_pool=pool,
                    mongo_client=_mongo_client(),
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                )

        sql = mock_conn.fetch.call_args[0][0]
        assert "COALESCE(v.memory_id, f.memory_id)" in sql

    @pytest.mark.asyncio
    async def test_equal_scores_query_is_deterministic_across_calls(self) -> None:
        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        sql_queries: list[str] = []
        for _ in range(3):
            mock_conn = _base_pg_conn([])
            pool = MagicMock()
            with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
                with patch(
                    "nce.semantic_search.asyncio.create_task",
                    side_effect=lambda coro: (coro.close(), MagicMock())[1],
                ):
                    await semantic_search(
                        pg_pool=pool,
                        mongo_client=_mongo_client(),
                        embedding_fn=embed,
                        query="q",
                        namespace_id=NS,
                        agent_id=AGENT,
                    )
            sql_queries.append(mock_conn.fetch.call_args[0][0])

        assert sql_queries[0] == sql_queries[1] == sql_queries[2]
        assert "ORDER BY" in sql_queries[0]
        assert "COALESCE(v.memory_id, f.memory_id)" in sql_queries[0]


class TestBatch1RawDataCap:
    @pytest.mark.asyncio
    async def test_raw_data_truncated_to_max_chars(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()
        long_raw = "x" * (_MAX_RAW_DATA_CHARS + 500)

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        out = await _run_search(
            embedding_fn=embed,
            pg_rows=[_pg_row(payload_ref=oid, memory_id=mid, score=1.0)],
            mongo_docs={oid: {"_id": ObjectId(oid), "raw_data": long_raw}},
        )

        assert len(out) == 1
        assert len(out[0]["raw_data"]) == _MAX_RAW_DATA_CHARS
        assert out[0]["raw_data"] == long_raw[:_MAX_RAW_DATA_CHARS]


# ---------------------------------------------------------------------------
# Batch 2 — join key fix, batched Mongo hydration, safe ObjectId
# ---------------------------------------------------------------------------


class TestBatch2JoinAndHydration:
    @pytest.mark.asyncio
    async def test_full_outer_join_uses_memory_id_not_payload_ref(self) -> None:
        mock_conn = _base_pg_conn([])
        pool = MagicMock()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                await semantic_search(
                    pg_pool=pool,
                    mongo_client=_mongo_client(),
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                )

        sql = mock_conn.fetch.call_args[0][0]
        assert '"v"."memory_id"="f"."memory_id"' in sql.replace(" ", "")

    @pytest.mark.asyncio
    async def test_shared_payload_ref_returns_both_memories(self) -> None:
        shared_ref = str(ObjectId())
        mid_a = uuid.UUID("00000000-0000-4000-8000-000000000011")
        mid_b = uuid.UUID("00000000-0000-4000-8000-000000000022")
        rows = [
            _pg_row(payload_ref=shared_ref, memory_id=mid_a, score=0.9),
            _pg_row(payload_ref=shared_ref, memory_id=mid_b, score=0.8),
        ]

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        out = await _run_search(
            embedding_fn=embed,
            pg_rows=rows,
            mongo_docs={
                shared_ref: {"_id": ObjectId(shared_ref), "raw_data": "shared"},
            },
            limit=2,
        )

        assert len(out) == 2
        memory_ids = {str(r["memory_id"]) for r in out}
        assert memory_ids == {str(mid_a), str(mid_b)}
        assert all(r["payload_ref"] == shared_ref for r in out)

    @pytest.mark.asyncio
    async def test_invalid_payload_ref_skipped_without_error(self) -> None:
        mid = uuid.uuid4()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        out = await _run_search(
            embedding_fn=embed,
            pg_rows=[_pg_row(payload_ref="not-a-valid-oid", memory_id=mid, score=1.0)],
        )

        assert len(out) == 1
        assert out[0]["raw_data"] is None

    @pytest.mark.asyncio
    async def test_batched_mongo_uses_single_find_query(self) -> None:
        oid_a = str(ObjectId())
        oid_b = str(ObjectId())
        rows = [
            _pg_row(payload_ref=oid_a, memory_id=uuid.uuid4(), score=0.9),
            _pg_row(payload_ref=oid_b, memory_id=uuid.uuid4(), score=0.8),
            _pg_row(payload_ref=oid_a, memory_id=uuid.uuid4(), score=0.7),
        ]
        mongo = _mongo_client(
            episode_docs={
                oid_a: {"_id": ObjectId(oid_a), "raw_data": "a"},
                oid_b: {"_id": ObjectId(oid_b), "raw_data": "b"},
            }
        )

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()
        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                await semantic_search(
                    pg_pool=pool,
                    mongo_client=mongo,
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                    limit=3,
                )

        assert mongo.memory_archive.episodes.find.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_mongo_doc_yields_raw_data_none(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        out = await _run_search(
            embedding_fn=embed,
            pg_rows=[_pg_row(payload_ref=oid, memory_id=mid, score=1.0)],
            mongo_docs={},
        )

        assert len(out) == 1
        assert out[0]["raw_data"] is None

    @pytest.mark.asyncio
    async def test_valid_mongo_doc_hydrates_raw_data(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        out = await _run_search(
            embedding_fn=embed,
            pg_rows=[_pg_row(payload_ref=oid, memory_id=mid, score=1.0)],
            mongo_docs={oid: {"_id": ObjectId(oid), "raw_data": "episode-body"}},
        )

        assert len(out) == 1
        assert out[0]["raw_data"] == "episode-body"


# ---------------------------------------------------------------------------
# Batch 3 — fire-and-forget reinforcement
# ---------------------------------------------------------------------------


class TestBatch3BackgroundReinforcement:
    @pytest.mark.asyncio
    async def test_search_returns_without_waiting_for_reinforcement(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()
        rows = [_pg_row(payload_ref=oid, memory_id=mid, score=1.0)]

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        async def slow_reinforce(*_args, **_kwargs):
            await asyncio.sleep(5)

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()
        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch("nce.salience.reinforce", side_effect=slow_reinforce):
                started = time.monotonic()
                out = await semantic_search(
                    pg_pool=pool,
                    mongo_client=_mongo_client(),
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                )
                elapsed = time.monotonic() - started

        assert len(out) == 1
        assert elapsed < 1.0

    @pytest.mark.asyncio
    async def test_reinforcement_failure_does_not_propagate(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        async def failing_reinforce(*_args, **_kwargs):
            raise RuntimeError("reinforcement exploded")

        mock_conn = _base_pg_conn([_pg_row(payload_ref=oid, memory_id=mid, score=1.0)])
        pool = MagicMock()
        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch("nce.salience.reinforce", side_effect=failing_reinforce):
                out = await semantic_search(
                    pg_pool=pool,
                    mongo_client=_mongo_client(),
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                )
                await asyncio.sleep(0.05)

        assert len(out) == 1

    @pytest.mark.asyncio
    async def test_reinforcement_called_for_each_top_result(self) -> None:
        oid_a = str(ObjectId())
        oid_b = str(ObjectId())
        mid_a = uuid.UUID("00000000-0000-4000-8000-000000000031")
        mid_b = uuid.UUID("00000000-0000-4000-8000-000000000032")
        rows = [
            _pg_row(payload_ref=oid_a, memory_id=mid_a, score=0.9),
            _pg_row(payload_ref=oid_b, memory_id=mid_b, score=0.8),
        ]

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        reinforced: list[str] = []
        done = asyncio.Event()

        async def track_reinforce(_conn, memory_id, _agent_id, _namespace_id, *, delta):
            reinforced.append(str(memory_id))
            if len(reinforced) == 2:
                done.set()

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()
        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch("nce.salience.reinforce", side_effect=track_reinforce):
                await semantic_search(
                    pg_pool=pool,
                    mongo_client=_mongo_client(),
                    embedding_fn=embed,
                    query="q",
                    namespace_id=NS,
                    agent_id=AGENT,
                    limit=2,
                )
                await asyncio.wait_for(done.wait(), timeout=1.0)

        assert set(reinforced) == {str(mid_a), str(mid_b)}


class TestBatch37DecayConfidence:
    @pytest.mark.asyncio
    async def test_three_month_unreinforced_memory_returns_low_confidence_and_stale(self) -> None:
        from datetime import datetime, timedelta, timezone

        oid = str(ObjectId())
        mid = uuid.uuid4()
        three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)

        mock_row = {
            "payload_ref": oid,
            "memory_id": mid,
            "final_score": 0.8,
            "raw_salience": 1.0,
            "last_updated": three_months_ago,
        }

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn([mock_row])
        pool = MagicMock()
        mongo = _mongo_client(episode_docs={oid: {"_id": ObjectId(oid), "raw_data": "some memory"}})

        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                results = await semantic_search(
                    pg_pool=pool,
                    mongo_client=mongo,
                    embedding_fn=embed,
                    query="test",
                    namespace_id=NS,
                    agent_id=AGENT,
                )

        assert len(results) == 1
        res = results[0]
        assert res["stale"] is True
        assert res["confidence"] < 0.15
        assert res["confidence"] > 0.04
        assert res["salience_score"] == 1.0
        assert res["last_reinforced_at"] == three_months_ago.isoformat()


class TestBatch63CrossEncoderReranking:
    @pytest.mark.asyncio
    async def test_rerank_default_false(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()
        rows = [_pg_row(payload_ref=oid, memory_id=mid, score=0.9)]
        mongo = _mongo_client(
            episode_docs={oid: {"_id": ObjectId(oid), "raw_data": "some memory content"}}
        )

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()

        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                results = await semantic_search(
                    pg_pool=pool,
                    mongo_client=mongo,
                    embedding_fn=embed,
                    query="test query",
                    namespace_id=NS,
                    agent_id=AGENT,
                    rerank=False,
                )

        assert len(results) == 1
        assert results[0]["reranker_score"] is None

    @pytest.mark.asyncio
    async def test_rerank_local_model_reorders_and_surfaces_score(self) -> None:
        oid_a = str(ObjectId())
        oid_b = str(ObjectId())
        mid_a = uuid.UUID("00000000-0000-4000-8000-00000000000a")
        mid_b = uuid.UUID("00000000-0000-4000-8000-00000000000b")

        # A has lower database score (0.5) than B (0.9)
        rows = [
            _pg_row(payload_ref=oid_b, memory_id=mid_b, score=0.9),
            _pg_row(payload_ref=oid_a, memory_id=mid_a, score=0.5),
        ]
        mongo = _mongo_client(
            episode_docs={
                oid_a: {"_id": ObjectId(oid_a), "raw_data": "matching content A"},
                oid_b: {"_id": ObjectId(oid_b), "raw_data": "unrelated content B"},
            }
        )

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()

        # Mock the local NLI model to return higher entailment score (probs[0]) for A than B
        # probs structure: [entailment, neutral, contradiction]
        import numpy as np

        mock_model = MagicMock()
        mock_model.predict = MagicMock(
            side_effect=lambda pairs: np.array(
                [[2.0, 0.0, -1.0] if pairs[0][0] == "matching content A" else [-2.0, 0.0, 1.0]],
                dtype=np.float32,
            )
        )

        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                with patch("nce.contradictions._load_nli_model", return_value=mock_model):
                    results = await semantic_search(
                        pg_pool=pool,
                        mongo_client=mongo,
                        embedding_fn=embed,
                        query="matching query",
                        namespace_id=NS,
                        agent_id=AGENT,
                        rerank=True,
                    )

        # After reranking, A (higher NLI entailment score) should be first, even though its database score was lower
        assert len(results) == 2

        # A should have high entailment probability (> 0.5)
        # B should have low entailment probability (< 0.5)
        assert results[0]["memory_id"] == mid_a
        assert results[1]["memory_id"] == mid_b
        assert results[0]["reranker_score"] > 0.8
        assert results[1]["reranker_score"] < 0.2

        # Surfaced as a confidence signal
        assert results[0]["confidence"] == results[0]["reranker_score"]
        assert results[1]["confidence"] == results[1]["reranker_score"]

    @pytest.mark.asyncio
    async def test_rerank_remote_cognitive_sidecar(self) -> None:
        oid_a = str(ObjectId())
        oid_b = str(ObjectId())
        mid_a = uuid.UUID("00000000-0000-4000-8000-00000000000a")
        mid_b = uuid.UUID("00000000-0000-4000-8000-00000000000b")

        rows = [
            _pg_row(payload_ref=oid_b, memory_id=mid_b, score=0.9),
            _pg_row(payload_ref=oid_a, memory_id=mid_a, score=0.5),
        ]
        mongo = _mongo_client(
            episode_docs={
                oid_a: {"_id": ObjectId(oid_a), "raw_data": "matching content A"},
                oid_b: {"_id": ObjectId(oid_b), "raw_data": "unrelated content B"},
            }
        )

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()

        # Mock cognitive base URL to return a fake URL
        with patch(
            "nce.embeddings.validated_cognitive_base_url", return_value="http://localhost:11435"
        ):
            # Mock httpx POST response
            mock_resp_a = MagicMock()
            mock_resp_a.json = MagicMock(
                return_value={"score": 0.1}
            )  # low contradiction = high relevance

            mock_resp_b = MagicMock()
            mock_resp_b.json = MagicMock(
                return_value={"score": 0.9}
            )  # high contradiction = low relevance

            async def mock_post(*args, **kwargs):
                body = kwargs.get("json", {})
                if body.get("premise") == "matching content A":
                    return mock_resp_a
                return mock_resp_b

            with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
                with patch(
                    "nce.semantic_search.asyncio.create_task",
                    side_effect=lambda coro: (coro.close(), MagicMock())[1],
                ):
                    with patch("nce.http_resilience.request_with_retry", side_effect=mock_post):
                        results = await semantic_search(
                            pg_pool=pool,
                            mongo_client=mongo,
                            embedding_fn=embed,
                            query="matching query",
                            namespace_id=NS,
                            agent_id=AGENT,
                            rerank=True,
                        )

        # After reranking, A should be first (relevance = 1.0 - 0.1 = 0.9)
        # B should be second (relevance = 1.0 - 0.9 = 0.1)
        assert len(results) == 2
        assert results[0]["memory_id"] == mid_a
        assert results[1]["memory_id"] == mid_b
        assert pytest.approx(results[0]["reranker_score"], 0.01) == 0.9
        assert pytest.approx(results[1]["reranker_score"], 0.01) == 0.1
        assert results[0]["confidence"] == results[0]["reranker_score"]

    @pytest.mark.asyncio
    async def test_rerank_graceful_degradation_on_nli_failure(self) -> None:
        oid = str(ObjectId())
        mid = uuid.uuid4()
        rows = [_pg_row(payload_ref=oid, memory_id=mid, score=0.9)]
        mongo = _mongo_client(episode_docs={oid: {"_id": ObjectId(oid), "raw_data": "some memory"}})

        async def embed(_query: str):
            return [0.0] * VECTOR_DIM

        mock_conn = _base_pg_conn(rows)
        pool = MagicMock()

        # Mock NLI model loading to return None (model not installed/loaded)
        with patch("nce.semantic_search.scoped_pg_session", _fake_scoped(mock_conn)):
            with patch(
                "nce.semantic_search.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), MagicMock())[1],
            ):
                with patch("nce.contradictions._load_nli_model", return_value=None):
                    results = await semantic_search(
                        pg_pool=pool,
                        mongo_client=mongo,
                        embedding_fn=embed,
                        query="test",
                        namespace_id=NS,
                        agent_id=AGENT,
                        rerank=True,
                    )

        # Should fall back to database sorting and return results cleanly
        assert len(results) == 1
        assert results[0]["reranker_score"] is None
