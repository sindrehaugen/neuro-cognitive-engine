"""BATCH 4 — commit_primary_to_v2 guards, quality gate, mark_aborted notify."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nce.reembedding_migration import (
    InMemoryReembeddingStore,
    MemoryEmbeddingRow,
    MigrationPhase,
    ReembeddingMigrationOrchestrator,
    deterministic_unit_embedding,
)

DIM = 24
_TARGET = "prod-v2"


def _embed_v2(text: str, *, dimension: int) -> list[float]:
    return deterministic_unit_embedding(
        text, model_version="targets/v2-stable", dimension=dimension
    )


def _v1(text: str) -> list[float]:
    return deterministic_unit_embedding(text, model_version="legacy/v1", dimension=DIM)


def _commit_ready_store(*, memory_id: str = "m-ready") -> InMemoryReembeddingStore:
    text = "commit ready"
    v1 = _v1(text)
    v2 = _embed_v2(text, dimension=DIM)
    return InMemoryReembeddingStore.from_records(
        {
            memory_id: MemoryEmbeddingRow(
                memory_id,
                text,
                embedding_v1=v1,
                embedding_v2=v2,
                embedding_v2_target_model_id=_TARGET,
            )
        },
        initial_pending=[],
    )


def test_commit_primary_to_v2_raises_when_pending_qsize_positive() -> None:
    store = InMemoryReembeddingStore.from_records(
        {
            "m-pending": MemoryEmbeddingRow(
                "m-pending",
                "still queued",
                embedding_v1=_v1("still queued"),
            )
        },
        initial_pending=["m-pending"],
    )
    assert store.pending_qsize() > 0

    with pytest.raises(RuntimeError, match=r"Cannot commit migration: 1 items are still pending"):
        store.commit_primary_to_v2()


def test_commit_raises_when_quality_gate_below_threshold() -> None:
    store = _commit_ready_store()
    gate = MagicMock(return_value=0.5)

    with pytest.raises(
        RuntimeError,
        match=r"Quality gate failed: neighbor overlap 0\.500 < threshold 0\.700",
    ):
        store.commit_primary_to_v2(quality_gate_fn=gate, quality_threshold=0.7)

    gate.assert_called_once()
    row = store._rows["m-ready"]
    assert row.embedding_v2 is not None
    assert store.phase != MigrationPhase.COMMITTED


def test_commit_succeeds_when_quality_gate_meets_threshold() -> None:
    store = _commit_ready_store()
    promoted = list(store._rows["m-ready"].embedding_v2 or [])

    store.commit_primary_to_v2(quality_gate_fn=lambda: 0.8, quality_threshold=0.7)

    assert store.phase == MigrationPhase.COMMITTED
    row = store._rows["m-ready"]
    assert row.embedding_v2 is None
    assert list(row.embedding_v1) == promoted


def test_commit_without_quality_gate_skips_gate_fn() -> None:
    store = _commit_ready_store()
    gate = MagicMock(return_value=0.0)

    store.commit_primary_to_v2(quality_gate_fn=None)

    gate.assert_not_called()
    assert store.phase == MigrationPhase.COMMITTED


@pytest.mark.asyncio
async def test_mark_aborted_notifies_condition() -> None:
    store = _commit_ready_store()
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    with patch.object(orch._cv, "notify_all") as notify_mock:
        await orch.mark_aborted()

    assert orch.phase == MigrationPhase.ABORTED
    notify_mock.assert_called_once()


@pytest.mark.asyncio
async def test_mark_aborted_unblocks_cv_waiters() -> None:
    store = _commit_ready_store()
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    unblocked = asyncio.Event()

    async def waiter() -> None:
        async with orch._cv:
            await orch._cv.wait()
        unblocked.set()

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not unblocked.is_set()

    await orch.mark_aborted()

    await asyncio.wait_for(unblocked.wait(), timeout=1.0)
    assert orch.phase == MigrationPhase.ABORTED
    await waiter_task
