"""Phase 2.1 — re-embedding migration: deterministic vector mocks + orchestrator drains."""

from __future__ import annotations

import asyncio
import math

import pytest

from nce.reembedding_migration import (
    InMemoryReembeddingStore,
    MemoryEmbeddingRow,
    MigrationPhase,
    ReembeddingMigrationOrchestrator,
    cosine_similarity,
    deterministic_unit_embedding,
    neighbor_overlap_fraction,
)

DIM = 24


def _embed_v2(text: str, *, dimension: int) -> list[float]:
    return deterministic_unit_embedding(
        text, model_version="targets/v2-stable", dimension=dimension
    )


def _gate_all_pass(
    samples: list[tuple[list[str], list[str]]],
    *,
    threshold: float = 0.7,
) -> bool:
    return all(neighbor_overlap_fraction(o, n) >= threshold for o, n in samples)


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ([1.0, 0.0, 0.0], [1.0, 0.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([3.0, 4.0], [6.0, 8.0], 1.0),
    ],
)
def test_cosine_similarity_known_pairs(a: list[float], b: list[float], want: float) -> None:
    assert math.isclose(cosine_similarity(a, b), want, abs_tol=1e-9)


@pytest.mark.parametrize(
    ("old", "new", "want"),
    [
        ([], [], 1.0),
        (["x"], [], 0.0),
        (["a", "b"], ["b", "c"], 1.0 / 3.0),
        (["same"], ["same"], 1.0),
    ],
)
def test_neighbor_overlap_fraction(
    old: list[str],
    new: list[str],
    want: float,
) -> None:
    assert math.isclose(neighbor_overlap_fraction(old, new), want, abs_tol=1e-9)


def test_deterministic_reembed_stable_within_model(
    TEST_2_1_01_tolerance: float = 0.01,
) -> None:
    """Same canonical text produces identical deterministic mock vectors."""
    text = "Phase 2.1 — quality gate cohort"
    e1 = deterministic_unit_embedding(text, model_version="frozen", dimension=DIM)
    e2 = deterministic_unit_embedding(text, model_version="frozen", dimension=DIM)
    sim = cosine_similarity(e1, e2)
    assert sim >= 1.0 - TEST_2_1_01_tolerance


def test_model_bump_rotates_but_stays_unit() -> None:
    text = "shared payload"
    v1 = deterministic_unit_embedding(text, model_version="sources/v1", dimension=DIM)
    v2 = deterministic_unit_embedding(text, model_version="targets/v2", dimension=DIM)
    n1 = math.sqrt(sum(x * x for x in v1))
    n2 = math.sqrt(sum(y * y for y in v2))
    assert math.isclose(n1, 1.0, abs_tol=1e-9)
    assert math.isclose(n2, 1.0, abs_tol=1e-9)
    assert abs(cosine_similarity(v1, v2)) < 1.0 - 1e-9


async def _drain_backlog(
    orch: ReembeddingMigrationOrchestrator,
    store: InMemoryReembeddingStore,
    *,
    batch_size: int = 8,
) -> None:
    while True:
        n = await orch.process_batch(batch_size)
        if n == 0 and store.pending_qsize() == 0:
            break


@pytest.mark.asyncio
async def test_worker_drains_queue_shadow_v2_reads_stay_on_v1() -> None:
    ids = [f"m{i}" for i in range(10)]
    records: dict[str, MemoryEmbeddingRow] = {}
    for i, mid in enumerate(ids):
        text = f"memory body {i % 4}"
        v1 = deterministic_unit_embedding(text, model_version="legacy/v1", dimension=DIM)
        records[mid] = MemoryEmbeddingRow(mid, text, embedding_v1=v1)

    store = InMemoryReembeddingStore.from_records(records, initial_pending=ids)
    read_snap_before = {mid: list(store.active_embedding(mid) or []) for mid in ids}
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    await _drain_backlog(orch, store)

    assert store.pending_qsize() == 0
    for mid in ids:
        row = store._rows[mid]
        assert row.embedding_v2 is not None
        expected_v2 = _embed_v2(row.canonical_text, dimension=DIM)
        assert cosine_similarity(row.embedding_v2, expected_v2) >= 1.0 - 1e-9
        assert list(store.active_embedding(mid) or []) == read_snap_before[mid]
        assert list(store.active_embedding(mid) or []) == list(row.embedding_v1)


@pytest.mark.asyncio
async def test_commit_promotes_shadow_to_authoritative_reads() -> None:
    mid = "only"
    text = "commit swap"
    v1_seed = deterministic_unit_embedding(text, model_version="v1-pre", dimension=DIM)
    store = InMemoryReembeddingStore.from_records(
        {mid: MemoryEmbeddingRow(mid, text, embedding_v1=v1_seed)},
        initial_pending=[mid],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="prod-v2",
        dimension=DIM,
    )
    await _drain_backlog(orch, store)

    promoted = list(_embed_v2(text, dimension=DIM))
    assert promoted != list(v1_seed)
    store.commit_primary_to_v2()
    orch.mark_committed()

    assert store.phase == MigrationPhase.COMMITTED
    row = store._rows[mid]
    assert row.embedding_v2 is None
    assert list(store.active_embedding(mid) or []) == promoted
    assert cosine_similarity(store.active_embedding(mid) or [], promoted) >= 1.0 - 1e-9


@pytest.mark.asyncio
async def test_concurrent_reads_unchanged_during_backfill() -> None:
    ids = [f"c{i}" for i in range(16)]
    records: dict[str, MemoryEmbeddingRow] = {}
    for i, mid in enumerate(ids):
        text = f"concurrent {i % 3}"
        vec = deterministic_unit_embedding(text, model_version="v1-live", dimension=DIM)
        records[mid] = MemoryEmbeddingRow(mid, text, embedding_v1=vec)
    store = InMemoryReembeddingStore.from_records(records, initial_pending=ids)

    baseline = {mid: tuple(store.active_embedding(mid) or []) for mid in ids}
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=lambda t, dimension: deterministic_unit_embedding(
            t,
            model_version="v2-shadow",
            dimension=dimension,
        ),
        target_model_id="parallel-v2",
        dimension=DIM,
    )

    async def hammer_reads() -> None:
        for _ in range(400):
            for mid in ids:
                cur = tuple(store.active_embedding(mid) or [])
                assert cur == baseline[mid]
            await asyncio.sleep(0)

    async def slow_drain() -> None:
        while store.pending_qsize() > 0 or True:
            n = await orch.process_batch(3)
            if n == 0 and store.pending_qsize() == 0:
                break
            await asyncio.sleep(0)

    await asyncio.wait_for(asyncio.gather(slow_drain(), hammer_reads()), timeout=5.0)


def test_quality_gate_neighbor_overlap_aggregate() -> None:
    # Pair 2: overlap 8/10 = 0.8 (> 0.7); pair 3: exact singleton match.
    old_tail = [f"i{i}" for i in range(8)]
    new_tail = [f"i{i}" for i in range(10)]
    assert neighbor_overlap_fraction(old_tail, new_tail) >= 0.7
    passing = [
        (["n1", "n2"], ["n1", "n2"]),
        (old_tail, new_tail),
        (["a"], ["a"]),
    ]
    failing = passing + [(["only-left"], [])]
    assert _gate_all_pass(passing, threshold=0.7) is True
    assert _gate_all_pass(failing, threshold=0.7) is False


@pytest.mark.asyncio
async def test_abort_clears_queue_and_shadow_without_touching_authoritative_v1() -> None:
    ids = ["a", "b", "c"]
    records = {
        mid: MemoryEmbeddingRow(
            mid,
            mid * 4,
            embedding_v1=deterministic_unit_embedding(mid * 4, model_version="v1", dimension=DIM),
        )
        for mid in ids
    }
    store = InMemoryReembeddingStore.from_records(records, initial_pending=ids[:])
    before = {mid: tuple(store.active_embedding(mid) or []) for mid in ids}

    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    await orch.process_batch(batch_size=1)
    assert store._rows["a"].embedding_v2 is not None
    assert store.pending_qsize() == 2

    store.abort_and_clear_pending_v2()
    await orch.mark_aborted()

    assert store.phase == MigrationPhase.ABORTED
    assert store.pending_qsize() == 0
    for mid in ids:
        assert tuple(store.active_embedding(mid) or []) == before[mid]
        assert store._rows[mid].embedding_v2 is None


@pytest.mark.asyncio
async def test_orchestrator_returns_zero_after_abort() -> None:
    store = InMemoryReembeddingStore.from_records(
        {
            "x": MemoryEmbeddingRow(
                "x",
                "tx",
                embedding_v1=deterministic_unit_embedding("tx", model_version="v1", dimension=DIM),
            )
        },
        initial_pending=["x"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="v2",
        dimension=DIM,
    )
    await orch.mark_aborted()
    processed = await orch.process_batch(batch_size=5)
    assert processed == 0
