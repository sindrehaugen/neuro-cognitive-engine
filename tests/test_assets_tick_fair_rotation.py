"""Tests for the asset telemetry tick's keyset-pagination fair-rotation fix
(``nce/cron.py``, ``_fetch_asset_tick_batch``, 2026-09-20).

Found by lane F, unowned: the tick's original query,

    SELECT DISTINCT namespace_id, id AS asset_id FROM assets
     WHERE lifecycle_state <> $1 LIMIT 100

has no ``ORDER BY``, no cursor, no offset. Above 100 non-terminal assets it
services an arbitrary ~100 forever and never reaches the rest -- no error,
no failing test, and the tick's own docstring ("scans every non-terminal
asset in every namespace") goes false at that instant.

This file proves two things, not one:

1. ``test_the_original_query_shape_starves_above_the_batch_size`` runs the
   ORIGINAL (unfixed) query inline against a real Postgres connection and
   shows it reproduces the exact defect -- the same batch, byte-for-byte,
   on a second call. Without this, "ship a test that fails above 100
   assets" would be an assertion nobody ever watched go red; this test
   *is* that watch, permanently, self-verifying on every run rather than
   a one-time manual check that bit-rots.
2. ``test_fetch_asset_tick_batch_rotates_through_more_than_one_batch``
   proves the FIXED function (keyset cursor, ``ORDER BY id``) visits every
   seeded asset across enough batches with no repeats, and that the
   cursor wraps back to ``None`` once the end of the set is reached.

Both tests track only their OWN seeded asset ids when asserting rotation
properties -- the query itself scans every namespace, so this file cannot
assume it is the only source of non-terminal assets in a shared
integration database, and does not need to: repeats/coverage are checked
against the known set this file created, not against the raw batch
contents.
"""

from __future__ import annotations

import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.cron import _fetch_asset_tick_batch

# "ACTIVE" is a real, non-terminal entry in nce/config_data/asset-lifecycle.json's
# STATES list (read, not guessed) -- any state other than the last (terminal) one
# would do; ACTIVE is simply the most legible choice for a test.
_NON_TERMINAL_STATE = "ACTIVE"


async def _seed_assets(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
    count: int,
    lifecycle_state: str = _NON_TERMINAL_STATE,
) -> set[uuid.UUID]:
    """Insert `count` bare asset rows directly -- the rotation fix cares only
    about (namespace_id, id, lifecycle_state); do_seed_asset_from_bom's full
    BOM-line/product-catalog machinery is irrelevant here and far slower for
    150+ rows."""
    ids: set[uuid.UUID] = set()
    async with pg_pool.acquire() as conn:
        for i in range(count):
            asset_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO assets (id, namespace_id, bom_line_id, lifecycle_state)
                VALUES ($1, $2, $3, $4)
                """,
                asset_id,
                namespace_id,
                f"BOM-LINE-ROTATION-{i}-{asset_id}",
                lifecycle_state,
            )
            ids.add(asset_id)
    return ids


def _terminal_state() -> str:
    from nce.vertical_modules.assets.lifecycle import load_lifecycle_config

    return load_lifecycle_config()["STATES"][-1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_original_query_shape_starves_above_the_batch_size(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """Reproduces the exact pre-fix defect: no ORDER BY, no cursor, LIMIT
    only. Two calls of the ORIGINAL query, with 150 non-terminal assets
    present, must be able to return the identical batch -- proving this
    class of test is capable of catching the bug the fix addresses, not
    just asserting the fix's own behaviour back at itself."""
    terminal = _terminal_state()
    seeded = await _seed_assets(pg_pool, namespace_id, 150)
    assert len(seeded) == 150

    async def _original_unfixed_query() -> list[uuid.UUID]:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT namespace_id, id AS asset_id
                FROM assets
                WHERE lifecycle_state <> $1
                LIMIT 100
                """,
                terminal,
            )
        return [r["asset_id"] for r in rows]

    first = set(await _original_unfixed_query())
    second = set(await _original_unfixed_query())

    my_first = first & seeded
    my_second = second & seeded
    assert my_first, "seeded assets must appear in the batch or this test proves nothing"
    assert my_first == my_second, (
        "the original query (no ORDER BY, no cursor) is expected to return the SAME "
        "batch on every call when the batch size never changes -- this is the defect "
        "under test, not a flaky assertion. If this fails, Postgres started returning "
        "an unordered LIMIT non-deterministically across calls in this environment, "
        "which would mean this reproduction no longer demonstrates the bug."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fetch_asset_tick_batch_rotates_through_more_than_one_batch(
    pg_pool: asyncpg.Pool,  # type: ignore[type-arg]
    namespace_id: uuid.UUID,
) -> None:
    """The fix: keyset pagination over 150 seeded assets (batch size 100)
    must reach every one of them within 2 batches, with zero repeats, and
    the cursor must wrap back to None once exhausted -- proving the tick
    resumes from the start on its next pass rather than returning nothing
    forever."""
    terminal = _terminal_state()
    seeded = await _seed_assets(pg_pool, namespace_id, 150)
    assert len(seeded) == 150

    seen: set[uuid.UUID] = set()
    cursor: str | None = None
    batches: list[set[uuid.UUID]] = []

    # 150 seeded / batch size 100 = 2 batches to cover everything, +1 slack
    # for any unrelated non-terminal assets a shared integration DB may hold.
    for _ in range(3):
        async with pg_pool.acquire() as conn:
            rows, cursor = await _fetch_asset_tick_batch(conn, terminal, cursor, batch_size=100)
        batch_ids = {r["asset_id"] for r in rows}
        my_ids_this_batch = batch_ids & seeded
        batches.append(my_ids_this_batch)

        overlap = my_ids_this_batch & seen
        assert not overlap, (
            f"batch repeated {len(overlap)} already-seen seeded asset(s) before the "
            f"full set was covered -- keyset pagination is not advancing correctly"
        )
        seen |= my_ids_this_batch

        if seen == seeded:
            break

    assert seen == seeded, (
        f"only {len(seen)} of {len(seeded)} seeded assets were visited across 3 "
        f"batches of 100 -- rotation did not reach the full set"
    )
    # At least two distinct batches must have contributed -- a single batch
    # covering all 150 would mean batch_size silently stopped being enforced.
    non_empty_batches = [b for b in batches if b]
    assert len(non_empty_batches) >= 2, (
        "expected the 150 seeded assets to span at least two batches at "
        "batch_size=100; got them all in one, which is not what this test seeds for"
    )

    # Cursor must reach None (wrap) once the non-terminal set is exhausted --
    # otherwise the NEXT tick would resume past the end and see nothing,
    # which is the same "stops making progress" failure in a different shape.
    assert cursor is None, (
        "cursor did not wrap back to None after exhausting the non-terminal set -- "
        "the next tick would start from a stale cursor and could see zero rows "
        "forever if no new non-terminal assets are ever added past it"
    )
