"""BATCH 2 — process_batch concurrent embed, per-row failures, notify_all."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from nce.reembedding_migration import (
    InMemoryReembeddingStore,
    MemoryEmbeddingRow,
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


def _store_with_ids(ids: list[str]) -> InMemoryReembeddingStore:
    records: dict[str, MemoryEmbeddingRow] = {}
    for i, mid in enumerate(ids):
        text = f"memory body {i}"
        records[mid] = MemoryEmbeddingRow(mid, text, embedding_v1=_v1(text))
    return InMemoryReembeddingStore.from_records(records, initial_pending=ids)


@pytest.mark.asyncio
async def test_process_batch_uses_gather_once_for_ten_rows() -> None:
    ids = [f"m{i}" for i in range(10)]
    store = _store_with_ids(ids)
    embed_calls: list[str] = []

    def tracking_embed(text: str, *, dimension: int) -> list[float]:
        embed_calls.append(text)
        return _embed_v2(text, dimension=dimension)

    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=tracking_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    real_gather = asyncio.gather
    gather_calls: list[tuple[tuple[object, ...], dict]] = []

    async def tracking_gather(*aws: object, **kwargs: object) -> object:
        gather_calls.append((aws, kwargs))
        return await real_gather(*aws, **kwargs)

    with patch(
        "nce.reembedding_migration.asyncio.gather",
        side_effect=tracking_gather,
    ):
        written = await orch.process_batch(10)

    assert written == 10
    assert len(gather_calls) == 1
    coros = gather_calls[0][0]
    assert len(coros) == 10
    assert gather_calls[0][1].get("return_exceptions") is True
    assert len(embed_calls) == 10


@pytest.mark.asyncio
async def test_process_batch_timeout_on_one_row_writes_nine() -> None:
    ids = [f"m{i}" for i in range(10)]
    slow_idx = 5
    slow_text = f"memory body {slow_idx}"

    def slow_embed(text: str, *, dimension: int) -> list[float]:
        if text == slow_text:
            time.sleep(0.2)
        return _embed_v2(text, dimension=dimension)

    store = _store_with_ids(ids)
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=slow_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    with patch(
        "nce.reembedding_migration._EMBED_TIMEOUT_SECONDS",
        0.05,
    ):
        written = await orch.process_batch(10)

    assert written == 9
    assert store._rows[ids[slow_idx]].embedding_v2 is None
    for i, mid in enumerate(ids):
        if i == slow_idx:
            continue
        assert store._rows[mid].embedding_v2 is not None


@pytest.mark.asyncio
async def test_process_batch_wrong_dimension_per_row_writes_nine() -> None:
    ids = [f"m{i}" for i in range(10)]
    bad_idx = 3
    bad_text = f"memory body {bad_idx}"

    def bad_dim_embed(text: str, *, dimension: int) -> list[float]:
        if text == bad_text:
            return [0.0] * (dimension - 1)
        return _embed_v2(text, dimension=dimension)

    store = _store_with_ids(ids)
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=bad_dim_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    written = await orch.process_batch(10)

    assert written == 9
    assert store._rows[ids[bad_idx]].embedding_v2 is None
    for i, mid in enumerate(ids):
        if i == bad_idx:
            continue
        assert store._rows[mid].embedding_v2 is not None


@pytest.mark.asyncio
async def test_process_batch_notify_all_after_partial_failures() -> None:
    ids = [f"m{i}" for i in range(10)]
    bad_text = "memory body 7"

    def bad_dim_embed(text: str, *, dimension: int) -> list[float]:
        if text == bad_text:
            return [0.0] * (dimension - 1)
        return _embed_v2(text, dimension=dimension)

    store = _store_with_ids(ids)
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=bad_dim_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    with patch.object(orch._cv, "notify_all") as notify_mock:
        written = await orch.process_batch(10)

    assert written == 9
    notify_mock.assert_called_once()
