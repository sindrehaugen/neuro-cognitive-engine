"""Unit tests for consolidated memory embedding during sleep consolidation (Wave R-4)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.consolidation import ConsolidatedAbstraction, ConsolidationWorker
from nce.providers.base import LLMProvider


class _StubProvider(LLMProvider):
    """Stub LLMProvider with configurable ``embed`` behavior."""

    def __init__(
        self,
        response: ConsolidatedAbstraction,
        embed_fn: Any = None,
    ) -> None:
        self._response = response
        if embed_fn is not None:
            self.embed = embed_fn  # type: ignore[attr-defined]

    async def complete(self, messages: list, response_model: type):  # noqa: ANN401
        assert response_model is ConsolidatedAbstraction
        _ = messages
        return self._response

    def model_identifier(self) -> str:
        return "stub/test-consolidation"


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self, **_kwargs: Any) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


class _FakeConn:
    def __init__(self, *, namespace_id: UUID, memory_rows: list[dict[str, Any]]) -> None:
        self.namespace_id = namespace_id
        self.memory_rows = memory_rows
        self.run_id = uuid4()
        self.new_memory_ids: list[UUID] = []
        self.memory_inserts: list[tuple[str, tuple[Any, ...]]] = []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    def is_in_transaction(self) -> bool:
        return True

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = query.lower()
        if "insert into consolidation_runs" in q:
            return self.run_id
        if "insert into memories" in q and "returning id" in q:
            self.memory_inserts.append((query, args))
            nid = uuid4()
            self.new_memory_ids.append(nid)
            return nid
        if "select id from event_log" in q:
            return None
        raise AssertionError(f"unexpected fetchval: {query!r}")

    async def fetch(self, query: str, *args: Any) -> list:
        q = query.lower()
        if "from memories" in q and "episodic" in q:
            return self.memory_rows
        if "from memory_salience" in q:
            return []
        raise AssertionError(f"unexpected fetch: {query!r}")

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        return "UPDATE 1"


def _sample_abstraction(ids: list[UUID]) -> ConsolidatedAbstraction:
    return ConsolidatedAbstraction(
        abstraction="Synthesized semantic fact.",
        key_entities=["NodeA"],
        key_relations=[{"subject": "NodeA", "predicate": "links", "object": "NodeB"}],
        supporting_memory_ids=[str(i) for i in ids],
        contradicting_memory_ids=[],
        confidence=0.9,
    )


def _patch_clustering_and_events(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]):
    async def _fake_cluster(self, memories: list) -> tuple[list, dict]:
        return memories, {0: memories}

    monkeypatch.setattr(ConsolidationWorker, "_cluster_memories_async", _fake_cluster)
    monkeypatch.setattr("nce.consolidation.append_event", AsyncMock(return_value=uuid4()))


def test_consolidation_passes_embedding_vector_when_provider_embeds(
    monkeypatch: pytest.MonkeyPatch,
):
    ns = uuid4()
    mid_a, mid_b = uuid4(), uuid4()
    rows = [
        {"id": mid_a, "payload_ref": "ref-a", "embedding": json.dumps([0.1, 0.2, 0.3])},
        {"id": mid_b, "payload_ref": "ref-b", "embedding": json.dumps([0.1, 0.2, 0.3])},
    ]
    _patch_clustering_and_events(monkeypatch, rows)

    conn = _FakeConn(namespace_id=ns, memory_rows=rows)
    embed_mock = AsyncMock(return_value=[0.1, 0.2, 0.3])
    provider = _StubProvider(_sample_abstraction([mid_a, mid_b]), embed_fn=embed_mock)

    async def _run() -> None:
        worker = ConsolidationWorker(_FakePool(conn), provider)
        await worker.run_consolidation(ns)

    asyncio.run(_run())

    embed_mock.assert_awaited_once_with("Synthesized semantic fact.")
    assert len(conn.memory_inserts) == 1
    query, args = conn.memory_inserts[0]
    assert "$5::vector" in query
    assert len(args) == 5
    assert args[0] == ns
    assert args[4] == "[0.1,0.2,0.3]"


@pytest.mark.parametrize(
    "embed_fn",
    [
        AsyncMock(side_effect=RuntimeError("embedding service unavailable")),
        AsyncMock(return_value=None),
        None,
    ],
)
def test_consolidation_falls_back_to_4_param_insert_when_embed_fails_or_missing(
    monkeypatch: pytest.MonkeyPatch,
    embed_fn: Any,
):
    ns = uuid4()
    mid_a, mid_b = uuid4(), uuid4()
    rows = [
        {"id": mid_a, "payload_ref": "ref-a", "embedding": json.dumps([0.1, 0.2, 0.3])},
        {"id": mid_b, "payload_ref": "ref-b", "embedding": json.dumps([0.1, 0.2, 0.3])},
    ]
    _patch_clustering_and_events(monkeypatch, rows)

    conn = _FakeConn(namespace_id=ns, memory_rows=rows)
    provider = _StubProvider(_sample_abstraction([mid_a, mid_b]), embed_fn=embed_fn)

    async def _run() -> None:
        worker = ConsolidationWorker(_FakePool(conn), provider)
        await worker.run_consolidation(ns)

    asyncio.run(_run())

    assert len(conn.memory_inserts) == 1
    query, args = conn.memory_inserts[0]
    assert "$5::vector" not in query
    assert len(args) == 4
    assert args[0] == ns


def test_embed_abstraction_validation_guards():
    async def _run() -> None:
        # Unconfigured MagicMock / AsyncMock -> None
        w_magic = ConsolidationWorker(None, MagicMock())  # type: ignore[arg-type]
        assert await w_magic._embed_abstraction("text") is None

        w_async = ConsolidationWorker(None, AsyncMock())  # type: ignore[arg-type]
        assert await w_async._embed_abstraction("text") is None

        # Non-finite, bool, or empty -> None
        for bad_vec in [[], [float("nan")], [float("inf")], [True, 0.5]]:
            p = _StubProvider(
                _sample_abstraction([]),
                embed_fn=AsyncMock(return_value=bad_vec),
            )
            w = ConsolidationWorker(None, p)  # type: ignore[arg-type]
            assert await w._embed_abstraction("text") is None

        # Valid ints/floats -> formatted pgvector string
        p_ok = _StubProvider(
            _sample_abstraction([]),
            embed_fn=AsyncMock(return_value=(1, 0.25, -0.5)),
        )
        w_ok = ConsolidationWorker(None, p_ok)  # type: ignore[arg-type]
        assert await w_ok._embed_abstraction("text") == "[1.0,0.25,-0.5]"

    asyncio.run(_run())


def test_consolidation_embed_provider_fallback(monkeypatch: pytest.MonkeyPatch):
    """When LLM provider lacks embed(), ConsolidationWorker falls back to _embed_provider."""
    ns = uuid4()
    mid_a, mid_b = uuid4(), uuid4()
    rows = [
        {"id": mid_a, "payload_ref": "ref-a", "embedding": json.dumps([0.1, 0.2, 0.3])},
        {"id": mid_b, "payload_ref": "ref-b", "embedding": json.dumps([0.1, 0.2, 0.3])},
    ]
    _patch_clustering_and_events(monkeypatch, rows)

    conn = _FakeConn(namespace_id=ns, memory_rows=rows)
    # LLM provider has NO embed method
    provider = _StubProvider(_sample_abstraction([mid_a, mid_b]), embed_fn=None)
    mock_embed_provider = MagicMock()
    mock_embed_provider.embed = AsyncMock(return_value=[0.4, 0.5, 0.6])

    async def _run() -> None:
        worker = ConsolidationWorker(
            _FakePool(conn),
            provider,
            embed_provider=mock_embed_provider,
        )
        await worker.run_consolidation(ns)

    asyncio.run(_run())

    mock_embed_provider.embed.assert_awaited_once_with("Synthesized semantic fact.")
    assert len(conn.memory_inserts) == 1
    query, args = conn.memory_inserts[0]
    assert "$5::vector" in query
    assert len(args) == 5
    assert args[4] == "[0.4,0.5,0.6]"


def test_consolidation_queries_with_order_by_created_at_desc(monkeypatch: pytest.MonkeyPatch):
    """Consolidation selects episodic memories ordered by created_at DESC to prioritize newest."""
    ns = uuid4()
    rows: list[dict[str, Any]] = []

    fetched_queries: list[str] = []

    class _CaptureConn(_FakeConn):
        async def fetch(self, query: str, *args: Any) -> list:
            fetched_queries.append(query)
            return await super().fetch(query, *args)

    conn = _CaptureConn(namespace_id=ns, memory_rows=rows)
    provider = _StubProvider(_sample_abstraction([]), embed_fn=None)

    async def _run() -> None:
        worker = ConsolidationWorker(_FakePool(conn), provider)
        await worker.run_consolidation(ns)

    asyncio.run(_run())

    assert any("ORDER BY created_at DESC" in q for q in fetched_queries)


def test_consolidation_records_pending_contradictions():
    """Contradicting memory IDs are preserved on worker._pending_contradictions instead of being dropped."""
    ns = uuid4()
    mid_a = uuid4()
    mid_b = uuid4()
    contradicting_abs = ConsolidatedAbstraction(
        abstraction="Contradicting statement.",
        key_entities=["A"],
        key_relations=[],
        supporting_memory_ids=[str(mid_a)],
        contradicting_memory_ids=[str(mid_b)],
        confidence=0.8,
    )

    provider = _StubProvider(contradicting_abs)
    worker = ConsolidationWorker(MagicMock(), provider)

    cluster_mems = [
        {"id": mid_a, "payload_ref": "ref-a"},
        {"id": mid_b, "payload_ref": "ref-b"},
    ]

    async def _run() -> None:
        res = await worker._call_consolidation_llm(
            cluster_mems=cluster_mems,
            mem_ids=[str(mid_a), str(mid_b)],
            label=0,
            namespace_id=ns,
        )
        assert res is None  # routed away from direct consolidation
        assert len(worker._pending_contradictions) == 1
        entry = worker._pending_contradictions[0]
        assert entry["contradicting_memory_ids"] == [str(mid_b)]
        assert entry["cluster_label"] == 0

    asyncio.run(_run())
