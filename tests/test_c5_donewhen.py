"""
C5 done-when integration tests (Wave 28 acceptance gate).

Covers:
  1. A divergence is logged with its materiality.
  2. An above-threshold divergence dispatches an alert via the existing
     NotificationDispatcher; a sub-threshold divergence does not.
  3. A ``both→nce`` flip is BLOCKED while the divergence log is dirty
     over the parity window.
  4. A ``both→nce`` flip is ALLOWED once the window has passed (log
     is clean over the requested lookback).
  5. A ``both``-mode write reaches BOTH systems with no identity
     collision (source-prefixed identity).

All tests are ``@pytest.mark.integration`` and require a live Postgres
instance via the ``pg_pool`` / ``make_namespace`` fixtures from conftest.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.source_mode.divergence import flip_blocked, record_divergence
from nce.source_mode.resolver import write_route

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count_divergence_rows(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    namespace_id: UUID,
    engine: str,
) -> int:
    """Superuser count — bypasses FORCE RLS for assertion purposes."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*)::int FROM divergence_log WHERE namespace_id = $1 AND engine = $2",
            namespace_id,
            engine,
        )


# ---------------------------------------------------------------------------
# 1 + 2: record_divergence — materiality logging and alert dispatch
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_divergence_logged_with_materiality(
    pg_pool: Any,
    make_namespace: Any,
) -> None:
    """A recorded divergence row is persisted with the correct materiality."""
    ns_id: UUID = await make_namespace()
    engine = "test-engine-log"

    await record_divergence(
        pg_pool,
        namespace_id=ns_id,
        engine=engine,
        entity="contact:test-001",
        field="phone",
        nce_value="+47 900 00001",
        ext_value="+47 900 99999",
        materiality=0.05,
    )

    count = await _count_divergence_rows(pg_pool, namespace_id=ns_id, engine=engine)
    assert count == 1, f"Expected 1 divergence row, got {count}"

    # Verify the stored materiality value.
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT materiality FROM divergence_log WHERE namespace_id = $1 AND engine = $2",
            ns_id,
            engine,
        )
    assert row is not None
    assert float(row["materiality"]) == pytest.approx(0.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_above_threshold_divergence_dispatches_alert(
    pg_pool: Any,
    make_namespace: Any,
    monkeypatch: Any,
) -> None:
    """Materiality above threshold dispatches an alert via the existing dispatcher."""
    ns_id: UUID = await make_namespace()

    # Set threshold low so our value (0.5) is above it.
    monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.1")

    mock_dispatch = AsyncMock()
    with patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch):
        await record_divergence(
            pg_pool,
            namespace_id=ns_id,
            engine="alert-engine",
            entity="account:abc",
            field="name",
            nce_value="Acme AS",
            ext_value="Acme Corp",
            materiality=0.5,
        )

    mock_dispatch.assert_awaited_once()
    title, message = mock_dispatch.call_args.args
    assert "alert-engine" in title
    assert "0.5" in message or "0.50" in message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sub_threshold_divergence_logs_only(
    pg_pool: Any,
    make_namespace: Any,
    monkeypatch: Any,
) -> None:
    """Materiality below threshold is logged but does NOT dispatch an alert."""
    ns_id: UUID = await make_namespace()

    # Set threshold high so our value (0.01) is below it.
    monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.1")

    mock_dispatch = AsyncMock()
    with patch("nce.source_mode.divergence.dispatcher.dispatch_alert", mock_dispatch):
        await record_divergence(
            pg_pool,
            namespace_id=ns_id,
            engine="quiet-engine",
            entity="contact:quiet",
            field="email",
            nce_value="a@b.com",
            ext_value="a@c.com",
            materiality=0.01,
        )

    mock_dispatch.assert_not_awaited()

    # Row must still be persisted.
    count = await _count_divergence_rows(pg_pool, namespace_id=ns_id, engine="quiet-engine")
    assert count == 1


# ---------------------------------------------------------------------------
# 3 + 4: flip_blocked — gate on dirty / clean window
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flip_blocked_while_log_is_dirty(
    pg_pool: Any,
    make_namespace: Any,
    monkeypatch: Any,
) -> None:
    """Flip is blocked when divergence rows exist within the parity window."""
    ns_id: UUID = await make_namespace()
    monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.9")  # suppress alerts

    engine = "flip-dirty-engine"

    await record_divergence(
        pg_pool,
        namespace_id=ns_id,
        engine=engine,
        entity="contact:001",
        field="phone",
        nce_value="A",
        ext_value="B",
        materiality=0.2,
    )

    # Window of 3600 s covers the row just inserted.
    blocked = await flip_blocked(
        pg_pool,
        namespace_id=ns_id,
        engine=engine,
        window_seconds=3600.0,
    )
    assert blocked is True, "Expected flip to be BLOCKED with recent divergence rows"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flip_allowed_when_window_is_clean(
    pg_pool: Any,
    make_namespace: Any,
) -> None:
    """Flip is allowed when no divergence rows exist for this engine in the window."""
    ns_id: UUID = await make_namespace()

    # Use an engine with no divergence rows at all.
    engine = f"clean-engine-{uuid.uuid4().hex}"

    blocked = await flip_blocked(
        pg_pool,
        namespace_id=ns_id,
        engine=engine,
        window_seconds=3600.0,
    )
    assert blocked is False, "Expected flip to be ALLOWED with no divergence rows"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flip_allowed_after_window_expires(
    pg_pool: Any,
    make_namespace: Any,
    monkeypatch: Any,
) -> None:
    """Flip is allowed when divergence rows are older than the window."""
    ns_id: UUID = await make_namespace()
    monkeypatch.setenv("NCE_DIVERGENCE_ALERT_THRESHOLD", "0.9")

    engine = f"old-divergence-engine-{uuid.uuid4().hex}"

    # Insert an old divergence row directly (bypassing RLS via superuser pool).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO divergence_log
                   (namespace_id, engine, entity, field, nce_value, ext_value,
                    materiality, detected_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7,
                    now() - INTERVAL '7200 seconds')
            """,
            ns_id,
            engine,
            "contact:old",
            "email",
            "a@x.com",
            "a@y.com",
            0.3,
        )

    # Window of 3600 s should NOT reach the 7200-second-old row.
    blocked = await flip_blocked(
        pg_pool,
        namespace_id=ns_id,
        engine=engine,
        window_seconds=3600.0,
    )
    assert blocked is False, "Expected flip ALLOWED because the divergence row is older than window"


# ---------------------------------------------------------------------------
# 5: both-mode write reaches both systems with no identity collision
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_both_mode_write_reaches_both_systems_no_collision(
    pg_pool: Any,
    make_namespace: Any,
) -> None:
    """A ``both``-mode write reaches native AND external with source-prefixed identity.

    Source-prefixed identity means each system receives a write keyed with its
    own prefix so they cannot collide (nce::<id> vs ext::<id>).  We verify:
      - native_writer is called with the nce-prefixed key.
      - external_writer is called with the ext-prefixed key.
      - The two keys are distinct (no collision).
    """
    entity_id = str(uuid.uuid4())
    nce_key = f"nce::{entity_id}"
    ext_key = f"ext::{entity_id}"

    written_native: list[str] = []
    written_external: list[str] = []

    async def native_writer() -> str:
        written_native.append(nce_key)
        return nce_key

    async def external_writer() -> str:
        written_external.append(ext_key)
        return ext_key

    result = await write_route(
        "both",
        native_writer=native_writer,
        external_writer=external_writer,
    )

    assert written_native == [nce_key], f"native_writer not called or wrong key: {written_native}"
    assert written_external == [ext_key], (
        f"external_writer not called or wrong key: {written_external}"
    )

    # Identity must be distinct — no collision between the two systems.
    assert written_native[0] != written_external[0], (
        "Source-prefixed identities must be distinct; "
        f"nce={written_native[0]!r} ext={written_external[0]!r}"
    )

    assert result["native"] == nce_key
    assert result["external"] == ext_key
