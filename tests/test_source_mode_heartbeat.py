"""
tests/test_source_mode_heartbeat.py
======================================
Live-Postgres verification of the C5 flip-gate staleness heartbeat
(migration 098, 2026-09-20): nce.source_mode.divergence.
record_comparison_heartbeat/last_comparison_at, wired into
nce.source_mode.resolver.resolve() and surfaced on flip_status().

Follow-on from nce/source_mode/flip.py's own "STATED LIMITATION" docstring
(written during Wave D-9): flip_blocked() answers "zero divergence rows in
the window", indistinguishable from "nothing compared anything in the
window at all". This wave makes that distinction observable (not yet
gating -- see flip.py's own docstring for why enforcement is a separate,
deliberate decision not made here).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.source_mode.divergence import last_comparison_at, record_comparison_heartbeat
from nce.source_mode.flip import flip_status
from nce.source_mode.resolver import resolve

pytestmark = pytest.mark.integration


async def _seed_mode(
    pool: asyncpg.Pool, *, ns_id: uuid.UUID, engine: str, function: str, mode: str
) -> None:
    async with pool.acquire() as conn:
        await set_namespace_context(conn, ns_id)
        await conn.execute(
            """
            INSERT INTO source_mode_config (namespace_id, engine, function, mode, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (namespace_id, engine, function) DO UPDATE SET mode = EXCLUDED.mode
            """,
            ns_id,
            engine,
            function,
            mode,
        )


@pytest.mark.asyncio
async def test_last_comparison_at_is_none_before_any_heartbeat(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()
    result = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")
    assert result is None, "a namespace/engine pair with no heartbeat must read as None, not 0"


@pytest.mark.asyncio
async def test_record_comparison_heartbeat_never_raises_even_on_a_real_db_error(
    pg_pool: asyncpg.Pool, make_namespace: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """ML-orch's review of this wave: a monitoring heartbeat must not be
    able to fail the read it observes. Forces a real error (scoped_pg_session
    raising) and proves the function swallows it and logs at warning,
    rather than letting the caller's resolve()/read fail alongside it.
    """
    ns = await make_namespace()

    with (
        patch(
            "nce.source_mode.divergence.scoped_pg_session",
            side_effect=RuntimeError("simulated connection failure"),
        ),
        caplog.at_level(logging.WARNING, logger="nce.source_mode.divergence"),
    ):
        await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")

    assert any("record_comparison_heartbeat failed" in r.message for r in caplog.records), (
        "a swallowed failure must still be logged, not silently disappear"
    )
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync") is None


@pytest.mark.asyncio
async def test_record_comparison_heartbeat_first_call_inserts(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()

    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")
    first = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")
    assert first is not None

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        count_after_first = await conn.fetchval(
            "SELECT check_count FROM source_mode_heartbeat WHERE namespace_id = $1 AND engine = $2",
            ns,
            "widgets_sync",
        )
    assert count_after_first == 1


@pytest.mark.asyncio
async def test_record_comparison_heartbeat_is_throttled_within_the_window(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """ML-orch's review of this wave: this is the hottest read path in the
    estate, and PRIMARY KEY (namespace_id, engine) is a single row every
    concurrent reader of that pair would otherwise serialize on. A second
    call immediately after the first must NOT increment check_count or
    move last_checked_at -- it must be a no-op, proving the throttle
    actually suppresses the write rather than merely being documented.
    """
    ns = await make_namespace()

    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")
    first = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")

    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        count_after_second_call = await conn.fetchval(
            "SELECT check_count FROM source_mode_heartbeat WHERE namespace_id = $1 AND engine = $2",
            ns,
            "widgets_sync",
        )
    second = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")

    assert count_after_second_call == 1, "an immediate second call must be throttled, not counted"
    assert second == first, "a throttled call must not move last_checked_at either"


@pytest.mark.asyncio
async def test_record_comparison_heartbeat_increments_after_the_throttle_window_elapses(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """The other half of the throttle proof: once the existing row's
    last_checked_at is older than the throttle window, the next call must
    go through -- the WHERE clause's job is to skip a too-recent write, not
    to freeze the row forever.
    """
    ns = await make_namespace()
    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        await conn.execute(
            "UPDATE source_mode_heartbeat SET last_checked_at = now() - interval '5 minutes' "
            "WHERE namespace_id = $1 AND engine = $2",
            ns,
            "widgets_sync",
        )

    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        count_after = await conn.fetchval(
            "SELECT check_count FROM source_mode_heartbeat WHERE namespace_id = $1 AND engine = $2",
            ns,
            "widgets_sync",
        )
    assert count_after == 2, "a call after the throttle window elapsed must go through"


@pytest.mark.asyncio
async def test_resolve_both_mode_records_a_heartbeat_for_an_arbitrary_engine(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """The actual point of this wave: resolve() itself must call the
    heartbeat recorder whenever it resolves to "both" -- proven here for
    "widgets_sync", an engine with no hand-written flip.py of its own, same
    "prove it generalizes" discipline as test_source_mode_flip.py.
    """
    ns = await make_namespace()
    await _seed_mode(pg_pool, ns_id=ns, engine="widgets_sync", function="read_widgets", mode="both")

    before = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")
    assert before is None

    mode = await resolve(pg_pool, engine="widgets_sync", function="read_widgets", namespace_id=ns)
    assert mode == "both"

    after = await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync")
    assert after is not None, "resolve() returning 'both' must have recorded a heartbeat"


@pytest.mark.asyncio
async def test_resolve_d365_and_nce_mode_do_not_record_a_heartbeat(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """A heartbeat means "a comparison ran" -- only "both" mode ever runs
    parity_check (read_through's own dispatch table), so d365-only and
    nce-only resolutions must not fabricate a heartbeat that never happened.
    """
    ns = await make_namespace()

    await _seed_mode(pg_pool, ns_id=ns, engine="widgets_sync", function="read_a", mode="d365")
    mode_a = await resolve(pg_pool, engine="widgets_sync", function="read_a", namespace_id=ns)
    assert mode_a == "d365"
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync") is None

    await _seed_mode(pg_pool, ns_id=ns, engine="widgets_sync", function="read_b", mode="nce")
    mode_b = await resolve(pg_pool, engine="widgets_sync", function="read_b", namespace_id=ns)
    assert mode_b == "nce"
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync") is None


@pytest.mark.asyncio
async def test_flip_status_reports_heartbeat_stale_when_nothing_ever_compared(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()
    result = await flip_status(
        pg_pool, namespace_id=ns, engine="widgets_sync", window_seconds=86400.0
    )
    assert result["clean"] is True, "zero divergence rows -- still reads as clean"
    assert result["heartbeat_stale"] is True, (
        "but heartbeat_stale must say this 'clean' could mean 'never checked'"
    )
    assert result["last_compared_at"] is None


@pytest.mark.asyncio
async def test_flip_status_reports_heartbeat_fresh_after_a_real_comparison(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()
    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")

    result = await flip_status(
        pg_pool, namespace_id=ns, engine="widgets_sync", window_seconds=86400.0
    )
    assert result["clean"] is True
    assert result["heartbeat_stale"] is False, "a fresh heartbeat must not read as stale"
    assert result["last_compared_at"] is not None


@pytest.mark.asyncio
async def test_flip_status_reports_heartbeat_stale_when_last_comparison_is_outside_the_window(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()
    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="widgets_sync")
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        await conn.execute(
            "UPDATE source_mode_heartbeat SET last_checked_at = now() - interval '2 days' "
            "WHERE namespace_id = $1 AND engine = $2",
            ns,
            "widgets_sync",
        )

    result = await flip_status(
        pg_pool, namespace_id=ns, engine="widgets_sync", window_seconds=3600.0
    )
    assert result["heartbeat_stale"] is True, (
        "a 2-day-old heartbeat is outside a 1-hour window and must read stale"
    )


@pytest.mark.asyncio
async def test_flip_function_authorization_is_unchanged_by_a_stale_heartbeat(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Deliberate, stated in this wave's docstring: observability ships now,
    enforcement does not. A flip with zero divergences must still succeed
    even when the heartbeat is stale (or absent entirely) -- this test is
    the guardrail against someone accidentally wiring the gate to refuse on
    staleness without the explicit, separate decision flip.py's docstring
    calls for.
    """
    from nce.source_mode.flip import flip_function

    ns = await make_namespace()
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync") is None

    result = await flip_function(
        pg_pool,
        namespace_id=ns,
        engine="widgets_sync",
        function="read_widgets",
        window_seconds=7 * 86400.0,
    )
    assert result["ok"] is True, "no heartbeat at all must not block a flip -- not this wave's gate"


@pytest.mark.asyncio
async def test_record_comparison_heartbeat_is_scoped_to_its_own_engine(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    ns = await make_namespace()
    await record_comparison_heartbeat(pg_pool, namespace_id=ns, engine="sales")
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="widgets_sync") is None
    assert await last_comparison_at(pg_pool, namespace_id=ns, engine="sales") is not None
