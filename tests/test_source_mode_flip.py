"""
tests/test_source_mode_flip.py
================================
Live-Postgres verification of the generic C5 flip-gate (Wave D-9, 2026-09-20):
nce.source_mode.flip.flip_status / flip_function.

Generalizes nce/vertical_modules/sales/flip.py's do_read_sales_divergence /
do_flip_function, which independently re-implemented the exact same
divergence_log query nce.source_mode.divergence.flip_blocked() already
provided generically, with 'sales' hardcoded instead of taken as a
parameter. tests/test_sales_flip.py's own test_gated_flip_function keeps
covering the "sales" wrapper unchanged; this file proves the underlying
mechanism actually generalizes to an arbitrary engine, not just to the one
engine it was extracted from.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import patch

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.source_mode import divergence as divergence_module
from nce.source_mode.flip import flip_function, flip_status

pytestmark = pytest.mark.integration


async def _seed_divergence(
    conn: asyncpg.Connection,
    *,
    ns_id: uuid.UUID,
    engine: str,
    entity: str,
    field: str,
    nce_val: str,
    ext_val: str,
    materiality: float,
    detected_at: datetime.datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO divergence_log (namespace_id, engine, entity, field, nce_value, ext_value, materiality, detected_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::numeric, $8::timestamptz)
        """,
        ns_id,
        engine,
        entity,
        field,
        nce_val,
        ext_val,
        materiality,
        detected_at,
    )


@pytest.mark.asyncio
async def test_flip_status_is_scoped_to_its_own_engine(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Seed divergence rows for TWO different engines in the same namespace.

    flip_status(engine="widgets_sync") must see only widgets_sync's row,
    never sales's -- proving the generalization is a real parameter, not a
    second hardcoded literal wearing a different name.
    """
    ns = await make_namespace()
    now = datetime.datetime.now(datetime.timezone.utc)

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_divergence(
                conn,
                ns_id=ns,
                engine="sales",
                entity="accounts",
                field="name",
                nce_val="NCE Corp",
                ext_val="D365 Corp",
                materiality=1.0,
                detected_at=now,
            )
            await _seed_divergence(
                conn,
                ns_id=ns,
                engine="widgets_sync",
                entity="widgets",
                field="sku",
                nce_val="W-1",
                ext_val="W-1-OLD",
                materiality=0.5,
                detected_at=now,
            )

    result = await flip_status(
        pg_pool, namespace_id=ns, engine="widgets_sync", window_seconds=86400.0
    )
    assert result["ok"] is True
    assert result["engine"] == "widgets_sync"
    assert result["clean"] is False
    assert result["flip_blocked"] is True
    assert result["divergences_count"] == 1
    assert result["items"][0]["entity"] == "widgets"


@pytest.mark.asyncio
async def test_flip_function_gated_for_an_arbitrary_engine(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Same lifecycle as tests/test_sales_flip.py's test_gated_flip_function,
    but for an engine that has never had its own hand-written flip.py --
    proving the mechanism itself is generic, not just that sales's copy of
    it still works.
    """
    ns = await make_namespace()

    # 1. Clean window -> flip succeeds.
    res_clean = await flip_function(
        pg_pool,
        namespace_id=ns,
        engine="widgets_sync",
        function="read_widgets",
        window_seconds=7 * 86400.0,
    )
    assert res_clean["ok"] is True
    assert res_clean["mode"] == "nce"

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, ns)
        mode = await conn.fetchval(
            """
            SELECT mode FROM source_mode_config
            WHERE namespace_id = $1::uuid AND engine = 'widgets_sync' AND function = 'read_widgets'
            """,
            ns,
        )
        assert mode == "nce"

    # 2. Reset to 'both', seed a divergence -> flip refused.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE source_mode_config SET mode = 'both'
            WHERE namespace_id = $1::uuid AND engine = 'widgets_sync' AND function = 'read_widgets'
            """,
            ns,
        )
    now = datetime.datetime.now(datetime.timezone.utc)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await _seed_divergence(
                conn,
                ns_id=ns,
                engine="widgets_sync",
                entity="widgets",
                field="sku",
                nce_val="W-1",
                ext_val="W-1-OLD",
                materiality=1.0,
                detected_at=now - datetime.timedelta(days=1),
            )

    res_dirty = await flip_function(
        pg_pool,
        namespace_id=ns,
        engine="widgets_sync",
        function="read_widgets",
        window_seconds=7 * 86400.0,
    )
    assert res_dirty["ok"] is False
    assert "Refused" in res_dirty["reason"]
    assert res_dirty["divergences_count"] == 1

    # 3. Narrower window than the divergence's age -> flip succeeds again.
    res_narrow = await flip_function(
        pg_pool,
        namespace_id=ns,
        engine="widgets_sync",
        function="read_widgets",
        window_seconds=1.0,
    )
    assert res_narrow["ok"] is True
    assert res_narrow["mode"] == "nce"


@pytest.mark.asyncio
async def test_flip_function_uses_the_shared_flip_blocked_gate(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """The actual generalization this wave made: flip_function's block
    decision must come from the one canonical
    nce.source_mode.divergence.flip_blocked(), not a second, independent
    reimplementation of its query -- which is exactly what
    sales/flip.py's do_flip_function used to do with 'sales' hardcoded.
    Spies on the real function (still calls through) to prove it is
    actually invoked, not bypassed.
    """
    ns = await make_namespace()
    calls: list[dict[str, Any]] = []
    real_flip_blocked = divergence_module.flip_blocked

    async def _spy(*args: Any, **kwargs: Any) -> bool:
        calls.append(kwargs)
        return await real_flip_blocked(*args, **kwargs)

    with patch("nce.source_mode.flip.flip_blocked", side_effect=_spy):
        result = await flip_function(
            pg_pool,
            namespace_id=ns,
            engine="widgets_sync",
            function="read_widgets",
            window_seconds=86400.0,
        )

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["engine"] == "widgets_sync"
    assert calls[0]["window_seconds"] == 86400.0
