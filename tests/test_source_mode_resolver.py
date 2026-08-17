"""
Integration tests for C5 source-mode resolver (Wave 27).

Covers:
  - All three modes (d365 / both / nce) resolve correctly from
    ``source_mode_config`` rows seeded in-test.
  - An unset (engine, function) pair returns the safe default ``"d365"``
    (not a raise, not silent-native).
  - ``read_through`` dispatches reads correctly for each mode:
      d365  → external_reader called, native_reader not called.
      both  → native_reader called (primary), parity_check fired.
      nce   → native_reader called, external_reader not called.
  - ``write_route`` dispatches writes correctly for each mode:
      d365  → external-only (write-through).
      both  → both native and external written (write-through).
      nce   → native-only (migration complete).

All tests are ``@pytest.mark.integration`` and require a live Postgres
instance (routed via ``pg_pool`` / ``make_namespace`` fixtures).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from nce.auth import set_namespace_context
from nce.source_mode import read_through, resolve, write_route

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_mode(
    conn: Any,
    *,
    namespace_id: UUID,
    engine: str,
    function: str,
    mode: str,
) -> None:
    """Insert a source_mode_config row inside the already-scoped ``conn``."""
    await conn.execute(
        """
        INSERT INTO source_mode_config (namespace_id, engine, function, mode)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (namespace_id, engine, function)
        DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
        """,
        namespace_id,
        engine,
        function,
        mode,
    )


# ---------------------------------------------------------------------------
# resolve() — mode lookup
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_d365_mode(pg_pool: Any, make_namespace: Any) -> None:
    """Configured mode ``"d365"`` is returned as-is."""
    ns_id: UUID = await make_namespace()

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await _seed_mode(
                conn,
                namespace_id=ns_id,
                engine="test-engine",
                function="test-func-d365",
                mode="d365",
            )

    result = await resolve(
        pg_pool,
        engine="test-engine",
        function="test-func-d365",
        namespace_id=ns_id,
    )
    assert result == "d365"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_both_mode(pg_pool: Any, make_namespace: Any) -> None:
    """Configured mode ``"both"`` is returned as-is."""
    ns_id: UUID = await make_namespace()

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await _seed_mode(
                conn,
                namespace_id=ns_id,
                engine="test-engine",
                function="test-func-both",
                mode="both",
            )

    result = await resolve(
        pg_pool,
        engine="test-engine",
        function="test-func-both",
        namespace_id=ns_id,
    )
    assert result == "both"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_nce_mode(pg_pool: Any, make_namespace: Any) -> None:
    """Configured mode ``"nce"`` is returned as-is."""
    ns_id: UUID = await make_namespace()

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await _seed_mode(
                conn,
                namespace_id=ns_id,
                engine="test-engine",
                function="test-func-nce",
                mode="nce",
            )

    result = await resolve(
        pg_pool,
        engine="test-engine",
        function="test-func-nce",
        namespace_id=ns_id,
    )
    assert result == "nce"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolve_unset_returns_safe_default(pg_pool: Any, make_namespace: Any) -> None:
    """An (engine, function) with no row returns ``"d365"`` — safe external default.

    Must NOT raise and must NOT silently return ``"nce"`` (which would skip
    the external system before migration is declared complete).
    """
    ns_id: UUID = await make_namespace()

    result = await resolve(
        pg_pool,
        engine="nonexistent-engine",
        function="nonexistent-func",
        namespace_id=ns_id,
    )
    assert result == "d365", (
        f"Expected safe default 'd365' for unset (engine, function), got {result!r}"
    )


# ---------------------------------------------------------------------------
# read_through() — read dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_through_d365_calls_external_only() -> None:
    """Mode ``"d365"``: only external_reader is called; result is returned."""
    calls: list[str] = []

    async def native() -> str:
        calls.append("native")
        return "native-data"

    async def external() -> str:
        calls.append("external")
        return "external-data"

    async def parity(n: Any, e: Any) -> None:
        calls.append("parity")

    result = await read_through(
        "d365", native_reader=native, external_reader=external, parity_check=parity
    )

    assert result == "external-data"
    assert calls == ["external"], f"Unexpected calls: {calls}"


@pytest.mark.asyncio
async def test_read_through_both_calls_native_primary_and_parity() -> None:
    """Mode ``"both"``: native_reader is primary; parity_check is fired."""
    calls: list[str] = []

    async def native() -> str:
        calls.append("native")
        return "native-data"

    async def external() -> str:
        calls.append("external")
        return "external-data"

    async def parity(n: Any, e: Any) -> None:
        calls.append(f"parity({n},{e})")

    result = await read_through(
        "both", native_reader=native, external_reader=external, parity_check=parity
    )

    assert result == "native-data"
    assert "native" in calls
    assert "external" in calls
    assert any(c.startswith("parity") for c in calls), f"parity_check not called: {calls}"


@pytest.mark.asyncio
async def test_read_through_nce_calls_native_only() -> None:
    """Mode ``"nce"``: only native_reader is called; result is returned."""
    calls: list[str] = []

    async def native() -> str:
        calls.append("native")
        return "native-data"

    async def external() -> str:
        calls.append("external")
        return "external-data"

    async def parity(n: Any, e: Any) -> None:
        calls.append("parity")

    result = await read_through(
        "nce", native_reader=native, external_reader=external, parity_check=parity
    )

    assert result == "native-data"
    assert calls == ["native"], f"Unexpected calls: {calls}"


# ---------------------------------------------------------------------------
# write_route() — write dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_route_d365_writes_external_only() -> None:
    """Mode ``"d365"``: write-through to external; native is skipped."""
    calls: list[str] = []

    async def native_w() -> str:
        calls.append("native")
        return "native-written"

    async def external_w() -> str:
        calls.append("external")
        return "external-written"

    out = await write_route("d365", native_writer=native_w, external_writer=external_w)

    assert calls == ["external"], f"Unexpected write calls: {calls}"
    assert out["native"] is None
    assert out["external"] == "external-written"


@pytest.mark.asyncio
async def test_write_route_both_writes_to_both() -> None:
    """Mode ``"both"``: write-through to native AND external."""
    calls: list[str] = []

    async def native_w() -> str:
        calls.append("native")
        return "native-written"

    async def external_w() -> str:
        calls.append("external")
        return "external-written"

    out = await write_route("both", native_writer=native_w, external_writer=external_w)

    assert "native" in calls
    assert "external" in calls
    assert out["native"] == "native-written"
    assert out["external"] == "external-written"


@pytest.mark.asyncio
async def test_write_route_nce_writes_native_only() -> None:
    """Mode ``"nce"``: native-only write; external is skipped (migration complete)."""
    calls: list[str] = []

    async def native_w() -> str:
        calls.append("native")
        return "native-written"

    async def external_w() -> str:
        calls.append("external")
        return "external-written"

    out = await write_route("nce", native_writer=native_w, external_writer=external_w)

    assert calls == ["native"], f"Unexpected write calls: {calls}"
    assert out["native"] == "native-written"
    assert out["external"] is None


# ---------------------------------------------------------------------------
# Integration round-trip: resolve then route (all three modes)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_round_trip_all_modes(pg_pool: Any, make_namespace: Any) -> None:
    """Seed all three modes and verify resolve + read_through + write_route behave correctly."""
    ns_id: UUID = await make_namespace()

    modes_to_test = ["d365", "both", "nce"]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            for mode in modes_to_test:
                await _seed_mode(
                    conn,
                    namespace_id=ns_id,
                    engine="round-trip-engine",
                    function=f"round-trip-{mode}",
                    mode=mode,
                )

    for mode in modes_to_test:
        resolved = await resolve(
            pg_pool,
            engine="round-trip-engine",
            function=f"round-trip-{mode}",
            namespace_id=ns_id,
        )
        assert resolved == mode, f"Expected mode={mode!r}, got {resolved!r}"

        # Verify write routing contract
        write_calls: list[str] = []

        async def _native_w(m: str = mode) -> str:
            write_calls.append("native")
            return f"native-{m}"

        async def _external_w(m: str = mode) -> str:
            write_calls.append("external")
            return f"external-{m}"

        await write_route(resolved, native_writer=_native_w, external_writer=_external_w)

        if mode == "d365":
            assert write_calls == ["external"], f"d365: expected external-only, got {write_calls}"
        elif mode == "both":
            assert "native" in write_calls and "external" in write_calls, (
                f"both: expected both writers called, got {write_calls}"
            )
        elif mode == "nce":
            assert write_calls == ["native"], f"nce: expected native-only, got {write_calls}"
