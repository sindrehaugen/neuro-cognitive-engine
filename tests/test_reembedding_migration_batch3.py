"""BATCH 3 — _embed_one retry loop: backoff, max retries, gather isolation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nce.reembedding_migration import (
    _EMBED_MAX_RETRIES,
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


def _single_row_store(memory_id: str = "m-retry") -> InMemoryReembeddingStore:
    text = "retry me"
    return InMemoryReembeddingStore.from_records(
        {
            memory_id: MemoryEmbeddingRow(
                memory_id,
                text,
                embedding_v1=_v1(text),
            )
        },
        initial_pending=[memory_id],
    )


@pytest.mark.asyncio
async def test_embed_fn_fails_twice_then_succeeds_on_third_attempt() -> None:
    """embed_fn_v2 failing twice then succeeding on attempt 3 writes the row."""
    attempt_counts: dict[str, int] = {}

    def flaky_embed(text: str, *, dimension: int) -> list[float]:
        attempt_counts[text] = attempt_counts.get(text, 0) + 1
        if attempt_counts[text] < 3:
            raise RuntimeError(f"transient failure #{attempt_counts[text]}")
        return _embed_v2(text, dimension=dimension)

    store = _single_row_store()
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=flaky_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    written = await orch.process_batch(10)

    assert written == 1
    assert attempt_counts["retry me"] == 3
    row = store._rows["m-retry"]
    assert row.embedding_v2 is not None
    assert row.embedding_v2_target_model_id == _TARGET
    assert row.embedding_v2 == _embed_v2("retry me", dimension=DIM)


@pytest.mark.asyncio
async def test_embed_fn_fails_all_retries_other_rows_unaffected() -> None:
    """All retries exhausted for one row; gather captures error; peers still written."""
    attempt_counts: dict[str, int] = {}

    def selective_embed(text: str, *, dimension: int) -> list[float]:
        attempt_counts[text] = attempt_counts.get(text, 0) + 1
        if text == "always fails":
            raise RuntimeError("permanent embed failure")
        return _embed_v2(text, dimension=dimension)

    records = {
        "m-bad": MemoryEmbeddingRow(
            "m-bad",
            "always fails",
            embedding_v1=_v1("always fails"),
        ),
        "m-ok": MemoryEmbeddingRow(
            "m-ok",
            "succeeds",
            embedding_v1=_v1("succeeds"),
        ),
    }
    store = InMemoryReembeddingStore.from_records(
        records,
        initial_pending=["m-bad", "m-ok"],
    )
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=selective_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    written = await orch.process_batch(10)

    assert written == 1
    assert attempt_counts["always fails"] == _EMBED_MAX_RETRIES
    assert store._rows["m-bad"].embedding_v2 is None
    assert store._rows["m-ok"].embedding_v2 is not None
    assert store._rows["m-ok"].embedding_v2_target_model_id == _TARGET


@pytest.mark.asyncio
async def test_embed_retry_sleeps_with_increasing_delay() -> None:
    """Between retries, asyncio.sleep uses 0.5 * (attempt + 1) → 0.5 then 1.0."""
    attempt_counts: dict[str, int] = {}

    def flaky_embed(text: str, *, dimension: int) -> list[float]:
        attempt_counts[text] = attempt_counts.get(text, 0) + 1
        if attempt_counts[text] < 3:
            raise RuntimeError("retry me")
        return _embed_v2(text, dimension=dimension)

    store = _single_row_store()
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=flaky_embed,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    sleep_mock = AsyncMock()
    with patch("nce.reembedding_migration.asyncio.sleep", sleep_mock):
        written = await orch.process_batch(10)

    assert written == 1
    assert sleep_mock.await_count == 2
    sleep_mock.assert_any_await(0.5)
    sleep_mock.assert_any_await(1.0)
    delays = [call.args[0] for call in sleep_mock.await_args_list]
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_timeout_error_on_all_attempts_raises_after_max_retries() -> None:
    """TimeoutError on every attempt is retried _EMBED_MAX_RETRIES times then surfaces."""
    store = _single_row_store()
    orch = ReembeddingMigrationOrchestrator(
        store=store,
        embed_fn_v2=_embed_v2,
        target_model_id=_TARGET,
        dimension=DIM,
    )

    wait_for_calls = 0

    async def raising_wait_for(coro: object, *args: object, **kwargs: object) -> object:
        nonlocal wait_for_calls
        wait_for_calls += 1
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError("embed timed out")

    with patch("nce.reembedding_migration.asyncio.wait_for", raising_wait_for):
        written = await orch.process_batch(10)

    assert written == 0
    assert wait_for_calls == _EMBED_MAX_RETRIES
    assert store._rows["m-retry"].embedding_v2 is None
