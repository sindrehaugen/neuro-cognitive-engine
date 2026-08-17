"""Unit tests for nce.orchestrators.graph.GraphOrchestrator hardening."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.orchestrators.graph import GraphOrchestrator

NS = "00000000-0000-4000-8000-000000000001"


def _fake_scoped(mock_conn):
    @asynccontextmanager
    async def _scoped(_pool, _namespace_id):
        yield mock_conn

    return _scoped


def _make_orchestrator(*, embed_fn=None, traverser=None):
    embed = embed_fn or AsyncMock(return_value=[0.1] * 8)
    trav = traverser if traverser is not None else MagicMock()
    trav.search = AsyncMock(return_value=MagicMock(to_dict=lambda: {"nodes": []}))
    return GraphOrchestrator(
        pg_pool=MagicMock(),
        mongo_client=MagicMock(),
        graph_traverser=trav,
        embed_fn=embed,
    )


def _fused_row(memory_id: uuid.UUID, score: float):
    return {"id": memory_id, "score": score}


def _memory_row(
    memory_id: uuid.UUID,
    *,
    payload_ref: str = "code/ref.py",
    metadata=None,
):
    return {
        "id": memory_id,
        "payload_ref": payload_ref,
        "language": "python",
        "filepath": "ref.py",
        "assertion_type": None,
        "metadata": metadata,
        "content_fts": "def main",
        "name": None,
        "node_type": None,
        "start_line": None,
        "end_line": None,
    }


@pytest.fixture
def graph_orch():
    return _make_orchestrator()


# ---------------------------------------------------------------------------
# CORRECTNESS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_codebase_returns_stable_ordering(graph_orch: GraphOrchestrator) -> None:
    """Identical RRF scores tie-break on id ASC (SQL); order is stable across calls."""
    ids = [uuid.uuid4() for _ in range(5)]
    same_score = 0.01639344262295082
    fused = sorted([_fused_row(i, same_score) for i in ids], key=lambda r: str(r["id"]))

    memory_rows = [_memory_row(i) for i in ids]

    async def _fetch(sql, *params):
        if "WITH vector_candidates" in sql:
            return fused
        return memory_rows

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=_fetch)

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                r1 = await graph_orch.search_codebase("find handler", namespace_id=NS, top_k=5)
                r2 = await graph_orch.search_codebase("find handler", namespace_id=NS, top_k=5)

    assert [x["memory_id"] for x in r1] == [x["memory_id"] for x in r2]
    assert len(r1) == 5


@pytest.mark.asyncio
async def test_no_duplicate_results(graph_orch: GraphOrchestrator) -> None:
    """Fused SQL emits one row per memory_id; enrichment preserves uniqueness."""
    mid = uuid.uuid4()
    fused = [_fused_row(mid, 0.5)]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        side_effect=[
            fused,
            [_memory_row(mid)],
        ]
    )

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                results = await graph_orch.search_codebase("query", namespace_id=NS, top_k=5)

    memory_ids = [r["memory_id"] for r in results]
    assert len(memory_ids) == len(set(memory_ids))


# ---------------------------------------------------------------------------
# SAFETY / VALIDATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_returns_empty_list(graph_orch: GraphOrchestrator) -> None:
    graph_orch._embed = AsyncMock()
    results = await graph_orch.search_codebase("   ", namespace_id=NS)
    assert results == []
    graph_orch._embed.assert_not_called()


@pytest.mark.asyncio
async def test_query_too_long_raises_value_error(graph_orch: GraphOrchestrator) -> None:
    with pytest.raises(ValueError, match="too long"):
        await graph_orch.search_codebase("x" * 1001, namespace_id=NS)


@pytest.mark.asyncio
async def test_graph_search_empty_query_raises_value_error() -> None:
    payload = MagicMock()
    payload.query = ""
    payload.namespace_id = NS
    payload.max_depth = 2
    payload.agent_id = None
    payload.as_of = None
    payload.max_edges_per_node = 10
    payload.edge_limit = 100
    payload.edge_offset = 0

    orch = _make_orchestrator()
    with pytest.raises(ValueError, match="non-empty query"):
        await orch.graph_search(payload)


@pytest.mark.asyncio
async def test_invalid_language_filter_raises_value_error(graph_orch: GraphOrchestrator) -> None:
    with pytest.raises(ValueError, match="Invalid language_filter"):
        await graph_orch.search_codebase("fn main", namespace_id=NS, language_filter="cobol")


# ---------------------------------------------------------------------------
# EMBEDDING FAILURES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_timeout_raises_runtime_error(graph_orch: GraphOrchestrator) -> None:
    async def slow_embed(_query: str):
        await asyncio.sleep(999)

    graph_orch._embed = slow_embed

    real_wait_for = asyncio.wait_for

    async def short_wait_for(coro, *, timeout=None):
        return await real_wait_for(coro, timeout=0.001)

    with patch("nce.orchestrators.graph.asyncio.wait_for", side_effect=short_wait_for):
        with pytest.raises(RuntimeError, match="timed out"):
            await graph_orch.search_codebase("slow embed", namespace_id=NS)


@pytest.mark.asyncio
async def test_embedding_invalid_output_raises_value_error(graph_orch: GraphOrchestrator) -> None:
    graph_orch._embed = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="invalid output"):
        await graph_orch.search_codebase("q", namespace_id=NS)


@pytest.mark.asyncio
async def test_embedding_empty_list_raises_value_error(graph_orch: GraphOrchestrator) -> None:
    graph_orch._embed = AsyncMock(return_value=[])
    with pytest.raises(ValueError, match="invalid output"):
        await graph_orch.search_codebase("q", namespace_id=NS)


# ---------------------------------------------------------------------------
# RESILIENCE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_decode_error_logged_not_raised(
    graph_orch: GraphOrchestrator, caplog: pytest.LogCaptureFixture
) -> None:
    mid = uuid.uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        side_effect=[
            [_fused_row(mid, 0.9)],
            [_memory_row(mid, metadata="{broken json")],
        ]
    )

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                with caplog.at_level("WARNING", logger="nce-orchestrator.graph"):
                    results = await graph_orch.search_codebase("q", namespace_id=NS, top_k=5)

    assert len(results) == 1
    assert any("Invalid metadata JSON" in rec.message for rec in caplog.records)
    assert str(mid) in caplog.text


@pytest.mark.asyncio
async def test_mongo_failure_returns_empty_excerpt(graph_orch: GraphOrchestrator) -> None:
    mid = uuid.uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        side_effect=[
            [_fused_row(mid, 0.8)],
            [_memory_row(mid)],
        ]
    )

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            side_effect=RuntimeError("mongo down"),
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                results = await graph_orch.search_codebase("q", namespace_id=NS, top_k=5)

    assert len(results) == 1
    assert results[0]["excerpt"] == ""


# ---------------------------------------------------------------------------
# PERFORMANCE CONTRACTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_k_is_bounded(graph_orch: GraphOrchestrator) -> None:
    mid = uuid.uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        side_effect=[
            [_fused_row(mid, 0.7)],
            [_memory_row(mid)],
        ]
    )
    captured: list = []

    async def _capture_fetch(sql, *params):
        captured.append(params)
        if len(captured) == 1:
            return [_fused_row(mid, 0.7)]
        return [_memory_row(mid)]

    mock_conn.fetch = AsyncMock(side_effect=_capture_fetch)

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                await graph_orch.search_codebase("bounded", namespace_id=NS, top_k=100)

    assert captured, "expected fused SQL fetch"
    candidate_k = captured[0][1]
    assert candidate_k <= 500


@pytest.mark.asyncio
async def test_sql_includes_namespace_filter(graph_orch: GraphOrchestrator) -> None:
    """Defense-in-depth namespace_id filter is present in hybrid search SQL."""
    mid = uuid.uuid4()
    sql_calls: list[str] = []

    async def _capture_fetch(sql, *params):
        sql_calls.append(sql)
        if len(sql_calls) == 1:
            return [_fused_row(mid, 0.5)]
        return [_memory_row(mid)]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=_capture_fetch)

    with patch("nce.orchestrators.graph.scoped_pg_session", _fake_scoped(mock_conn)):
        with patch(
            "nce.orchestrators.graph.fetch_code_files_raw_by_ref",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch("nce.orchestrators.graph.normalize_payload_ref", side_effect=lambda x: x):
                await graph_orch.search_codebase("ns filter", namespace_id=NS, top_k=3)

    assert sql_calls
    assert "namespace_id = current_setting('nce.namespace_id')::uuid" in sql_calls[0]
    assert "ORDER BY score DESC, id ASC" in sql_calls[0]
