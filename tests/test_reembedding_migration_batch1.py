"""BATCH 1 — process_batch guards: clamp, idempotency, text limits, dimension check."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from nce.reembedding_migration import (
    _MAX_BATCH_SIZE,
    _MAX_CANONICAL_TEXT_BYTES,
    InMemoryReembeddingStore,
    MemoryEmbeddingRow,
    ReembeddingMigrationOrchestrator,
    deterministic_unit_embedding,
)

DIM = 24
_LOG = "nce-reembedding-migration"


def _embed_v2(text: str, *, dimension: int) -> list[float]:
    return deterministic_unit_embedding(
        text, model_version="targets/v2-stable", dimension=dimension
    )


def _v1(text: str) -> list[float]:
    return deterministic_unit_embedding(text, model_version="legacy/v1", dimension=DIM)


@pytest.mark.asyncio
async def test_process_batch_clamps_batch_size_to_max() -> None:
    store = MagicMock()
    store.pop_pending_ids = AsyncMock(return_value=[])
    store.load_row = AsyncMock()

    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    processed = await orch.process_batch(10_000)

    assert processed == 0
    store.pop_pending_ids.assert_awaited_once_with(_MAX_BATCH_SIZE)


@pytest.mark.asyncio
async def test_process_batch_skips_row_with_embedding_v2_already_set() -> None:
    embed_calls: list[str] = []

    def tracking_embed(text: str, *, dimension: int) -> list[float]:
        embed_calls.append(text)
        return _embed_v2(text, dimension=dimension)

    text = "already migrated"
    v1 = _v1(text)
    v2 = _embed_v2(text, dimension=DIM)
    store = InMemoryReembeddingStore.from_records(
        {
            "m-done": MemoryEmbeddingRow(
                "m-done",
                text,
                embedding_v1=v1,
                embedding_v2=v2,
                embedding_v2_target_model_id="prod-v2",
            )
        },
        initial_pending=["m-done"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=tracking_embed,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    n = await orch.process_batch(10)

    assert n == 0
    assert embed_calls == []
    assert store._rows["m-done"].embedding_v2 == v2


@pytest.mark.asyncio
async def test_process_batch_skips_empty_canonical_text_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOG)
    embed_calls: list[str] = []

    def tracking_embed(text: str, *, dimension: int) -> list[float]:
        embed_calls.append(text)
        return _embed_v2(text, dimension=dimension)

    store = InMemoryReembeddingStore.from_records(
        {
            "m-empty": MemoryEmbeddingRow(
                "m-empty",
                "",
                embedding_v1=_v1("placeholder"),
            )
        },
        initial_pending=["m-empty"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=tracking_embed,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    await orch.process_batch(10)

    assert embed_calls == []
    assert store._rows["m-empty"].embedding_v2 is None
    assert any(
        r.levelno == logging.WARNING and "m-empty" in r.message and "empty" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_process_batch_skips_oversized_canonical_text_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOG)
    embed_calls: list[str] = []

    def tracking_embed(text: str, *, dimension: int) -> list[float]:
        embed_calls.append(text)
        return _embed_v2(text, dimension=dimension)

    oversized = "a" * (_MAX_CANONICAL_TEXT_BYTES + 1)
    assert len(oversized.encode("utf-8")) > _MAX_CANONICAL_TEXT_BYTES

    store = InMemoryReembeddingStore.from_records(
        {
            "m-big": MemoryEmbeddingRow(
                "m-big",
                oversized,
                embedding_v1=_v1("seed"),
            )
        },
        initial_pending=["m-big"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=tracking_embed,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    await orch.process_batch(10)

    assert embed_calls == []
    assert store._rows["m-big"].embedding_v2 is None
    assert any(
        r.levelno == logging.WARNING
        and "m-big" in r.message
        and str(_MAX_CANONICAL_TEXT_BYTES) in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_process_batch_raises_on_embedding_v1_dimension_mismatch() -> None:
    store = InMemoryReembeddingStore.from_records(
        {
            "m-bad-dim": MemoryEmbeddingRow(
                "m-bad-dim",
                "valid text",
                embedding_v1=[0.0] * (DIM - 1),
            )
        },
        initial_pending=["m-bad-dim"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id="prod-v2",
        dimension=DIM,
    )

    with pytest.raises(ValueError, match=r"memory_id=m-bad-dim: embedding_v1 has dim"):
        await orch.process_batch(10)

    assert store._rows["m-bad-dim"].embedding_v2 is None
