"""
tests/unit/test_inventory_hardening.py
======================================
Batch 140 -- Module 11.Wave 12 (``hardening``).  The final wave of Module 11.

This file **certifies** work that other waves built; it changes nothing.

  1. **Exact tool-count certification.**  Every number below was *measured*
     from ``nce.tool_registry`` on this branch, never copied out of a brief or
     a ledger -- a count asserted from prose ratifies itself.  The registry is
     also re-derived here from the ``ToolSpec`` flags so that the module-level
     frozensets cannot drift away from the table they summarise.
  2. **The opt-in gate refuses a non-opted-in namespace** (B140a owns the
     gate; this wave owns the assertion).
  3. **Dual-representation atomicity.**  ``inventory_items`` is the
     authoritative row; ``kg_nodes``/``kg_edges`` are an eventually-consistent
     projection.  Stock-truth reads must hit the row, and writers must land
     the row before the mirror, in one scoped session.

     The read test is *discriminating*, not decorative: the fake connection
     answers a row query and a projection query with **different** quantities
     (``42`` vs a stale ``7``), so a reader switched to the projection returns
     the stale number and the assertion fails.  ``do_stock_levels`` is reached
     through the module attribute precisely so that substitution is possible.

Pure unit tests -- mocked pool and connection, no database, no Redis, no
``pytest.mark.integration``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce import tool_registry as tr
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.vertical_modules.inventory import mcp_handlers as inv_mcp
from nce.vertical_modules.inventory import stock as inv_stock
from nce.vertical_modules.inventory._guard import (
    InventoryDisabledError,
    require_inventory_enabled,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_LOCATION_A = "11111111-1111-4111-8111-111111111111"
_LOCATION_B = "22222222-2222-4222-8222-222222222222"

# --- MEASURED on branch vm-b140-m11-w12-hardening, base 039fdd1 -------------
# python -c "import nce.tool_registry as tr; print(len(tr.TOOL_REGISTRY))"
_TOTAL_TOOLS = 174  # +8 hr tools (ML13, M13 HR engine) + 8 marketing tools (ML14-B3, M14.W3)
_MUTATION_TOOLS = 74  # +3 mutating hr tools + 5 mutating marketing tools
_CACHEABLE_TOOLS = 62  # +5 cacheable hr tools + 3 cacheable marketing tools
_ADMIN_ONLY_TOOLS = 39  # +2 admin_only hr tools + 5 admin_only marketing tools
_MIGRATION_TOOLS = 5


# The Inventory vertical's tools, read from ``TOOL_REGISTRY`` itself.  14, not
# the 12 the plan expected and not the 11 B138a added: three predate B138a.
_INVENTORY_TOOLS = frozenset(
    {
        "inventory_dispose_rma_weee",
        "inventory_forecast_demand",
        "inventory_recommend_restock",
        "inventory_reconcile_dead_stock",
        "inventory_record_consumption",
        "inventory_record_goods_receipt",
        "inventory_record_goods_receipt_and_match",
        "inventory_record_rma",
        "inventory_release_stock",
        "inventory_reserve_stock",
        "inventory_restock_from_rma",
        "inventory_stock_levels",
        "inventory_transfer_stock",
        "inventory_valuation",
    }
)

_INVENTORY_MUTATION = frozenset(
    {
        "inventory_dispose_rma_weee",
        "inventory_record_consumption",
        "inventory_record_goods_receipt",
        "inventory_record_goods_receipt_and_match",
        "inventory_record_rma",
        "inventory_release_stock",
        "inventory_reserve_stock",
        "inventory_restock_from_rma",
        "inventory_transfer_stock",
    }
)
_INVENTORY_CACHEABLE = frozenset(
    {
        "inventory_forecast_demand",
        "inventory_recommend_restock",
        "inventory_stock_levels",
    }
)
_INVENTORY_ADMIN_ONLY = _INVENTORY_MUTATION | {
    "inventory_reconcile_dead_stock",
    "inventory_valuation",
}


def _registered_inventory_tools() -> frozenset[str]:
    return frozenset(n for n in tr.TOOL_REGISTRY if n.startswith("inventory_"))


# ---------------------------------------------------------------------------
# 1. Exact tool-count certification
# ---------------------------------------------------------------------------


def test_tool_registry_holds_the_measured_total() -> None:
    assert len(tr.TOOL_REGISTRY) == _TOTAL_TOOLS


def test_inventory_tool_names_are_exactly_the_measured_set() -> None:
    found = _registered_inventory_tools()
    assert found == _INVENTORY_TOOLS, found ^ _INVENTORY_TOOLS


def test_inventory_tool_count_is_fourteen() -> None:
    assert len(_registered_inventory_tools()) == 14


def test_derived_counters_match_the_registry_they_summarise() -> None:
    """The four frozensets are derived, so they must equal a fresh derivation."""
    assert tr.MUTATION_TOOLS == {n for n, s in tr.TOOL_REGISTRY.items() if s.mutation}
    assert tr.CACHEABLE_TOOLS == {n for n, s in tr.TOOL_REGISTRY.items() if s.cacheable}
    assert tr.ADMIN_ONLY_TOOLS == {n for n, s in tr.TOOL_REGISTRY.items() if s.admin_only}
    assert tr.MIGRATION_TOOLS == {n for n, s in tr.TOOL_REGISTRY.items() if s.migration}


def test_derived_counter_sizes_are_the_measured_ones() -> None:
    assert len(tr.MUTATION_TOOLS) == _MUTATION_TOOLS
    assert len(tr.CACHEABLE_TOOLS) == _CACHEABLE_TOOLS
    assert len(tr.ADMIN_ONLY_TOOLS) == _ADMIN_ONLY_TOOLS
    assert len(tr.MIGRATION_TOOLS) == _MIGRATION_TOOLS


def test_inventory_flag_partition_is_exact() -> None:
    inventory = _registered_inventory_tools()
    assert inventory & tr.MUTATION_TOOLS == _INVENTORY_MUTATION
    assert inventory & tr.CACHEABLE_TOOLS == _INVENTORY_CACHEABLE
    assert inventory & tr.ADMIN_ONLY_TOOLS == _INVENTORY_ADMIN_ONLY
    assert inventory & tr.MIGRATION_TOOLS == frozenset()


def test_no_inventory_tool_is_both_cacheable_and_a_mutation() -> None:
    assert _INVENTORY_CACHEABLE & _INVENTORY_MUTATION == frozenset()


def test_every_inventory_tool_has_a_callable_handler() -> None:
    for name in sorted(_registered_inventory_tools()):
        assert callable(tr.TOOL_REGISTRY[name].handler), name


# ---------------------------------------------------------------------------
# 2. B140a's opt-in gate -- we assert it, we do not build it
# ---------------------------------------------------------------------------


def _pool_with_enabled(enabled: bool | None) -> MagicMock:
    """A pool whose ``namespaces`` read reports the guard's own predicate.

    ``None`` models the namespace row being absent altogether.
    """
    conn = MagicMock()
    if enabled is None:
        conn.fetchrow = AsyncMock(return_value=None)
    else:
        conn.fetchrow = AsyncMock(return_value={"inventory_enabled": enabled})
    pool = MagicMock()
    ctx = pool.acquire.return_value
    ctx.__aenter__.return_value = conn
    ctx.__aexit__.return_value = False
    return pool


@pytest.mark.asyncio
async def test_gate_refuses_a_namespace_that_has_not_opted_in() -> None:
    with pytest.raises(InventoryDisabledError) as exc:
        await require_inventory_enabled(_pool_with_enabled(False), _NAMESPACE_ID)
    assert _NAMESPACE_ID in str(exc.value)


@pytest.mark.asyncio
async def test_gate_refuses_an_unknown_namespace() -> None:
    with pytest.raises(InventoryDisabledError):
        await require_inventory_enabled(_pool_with_enabled(None), _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_gate_admits_an_opted_in_namespace() -> None:
    await require_inventory_enabled(_pool_with_enabled(True), _NAMESPACE_ID)


@pytest.mark.asyncio
async def test_mcp_stock_levels_refuses_a_non_opted_in_namespace() -> None:
    """The gate is wired, not merely present: the core never runs."""
    engine = MagicMock()
    engine.pg_pool = _pool_with_enabled(False)
    with patch.object(inv_mcp, "do_stock_levels", new=AsyncMock()) as core:
        with pytest.raises(McpError) as exc:
            await inv_mcp.handle_inventory_stock_levels(engine, {"namespace_id": _NAMESPACE_ID})
    assert exc.value.code == MCP_SCOPE_FORBIDDEN
    core.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_stock_levels_runs_the_core_once_opted_in() -> None:
    """Guard-the-guard: the refusal above is the gate, not a broken handler."""
    engine = MagicMock()
    engine.pg_pool = _pool_with_enabled(True)
    payload = {"ok": True, "items": []}
    with patch.object(inv_mcp, "do_stock_levels", new=AsyncMock(return_value=payload)):
        out = await inv_mcp.handle_inventory_stock_levels(engine, {"namespace_id": _NAMESPACE_ID})
    assert json.loads(out) == payload


# ---------------------------------------------------------------------------
# 3. Dual-representation atomicity: the ROW is the truth, the graph is a mirror
# ---------------------------------------------------------------------------

_ROW_ON_HAND = Decimal("42.000")
_STALE_PROJECTION_ON_HAND = Decimal("7.000")


class _DivergentConn:
    """A connection whose row and projection disagree, on purpose.

    ``inventory_items`` answers 42; ``kg_nodes``/``kg_edges`` answer a stale 7.
    A reader that has been switched to the projection therefore returns 7 and
    fails the assertion below -- which is the whole point of the divergence.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append(query)
        if "kg_nodes" in query or "kg_edges" in query:
            qty = _STALE_PROJECTION_ON_HAND
        elif "inventory_items" in query:
            qty = _ROW_ON_HAND
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected read: {query}")
        return [
            {
                "sku": "SKU-1",
                "location_id": _LOCATION_A,
                "qty_on_hand": qty,
                "qty_reserved": Decimal("2.000"),
                "qty_blocked": Decimal("1.000"),
                "available": qty - Decimal("3.000"),
            }
        ]


@asynccontextmanager
async def _session_yielding(conn: Any) -> AsyncIterator[Any]:
    yield conn


@pytest.mark.asyncio
async def test_stock_truth_comes_from_the_row_not_the_projection() -> None:
    conn = _DivergentConn()
    engine = MagicMock()
    with patch.object(
        inv_stock,
        "scoped_pg_session",
        new=lambda pool, ns: _session_yielding(conn),
    ):
        result = await inv_stock.do_stock_levels(engine, {"namespace_id": _NAMESPACE_ID})

    on_hand = result["items"][0]["on_hand"]
    assert on_hand == _ROW_ON_HAND
    assert on_hand != _STALE_PROJECTION_ON_HAND
    assert result["items"][0]["available"] == _ROW_ON_HAND - Decimal("3.000")


@pytest.mark.asyncio
async def test_stock_levels_never_queries_the_graph_projection() -> None:
    conn = _DivergentConn()
    engine = MagicMock()
    with patch.object(
        inv_stock,
        "scoped_pg_session",
        new=lambda pool, ns: _session_yielding(conn),
    ):
        await inv_stock.do_stock_levels(engine, {"namespace_id": _NAMESPACE_ID})

    assert len(conn.statements) == 1
    query = conn.statements[0]
    assert "FROM inventory_items" in query
    assert "kg_nodes" not in query
    assert "kg_edges" not in query


class _RecordingConn:
    """Records every statement in order; answers row writes with a quantity."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.statements.append(query)
        return {"qty_on_hand": _ROW_ON_HAND}

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append(query)
        return "INSERT 0 1"

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        self.statements.append(query)
        return _ROW_ON_HAND


def _first_index(statements: list[str], needle: str) -> int:
    for i, sql in enumerate(statements):
        if needle in sql:
            return i
    raise AssertionError(f"no statement touched {needle}")


@pytest.mark.asyncio
async def test_transfer_writes_the_authoritative_row_before_the_mirror() -> None:
    """One scoped session; every ``inventory_items`` write precedes every
    ``kg_nodes``/``kg_edges`` write, so a failed mirror can never leave a
    projection that is ahead of the truth it mirrors.

    ``append_transaction``, ``assert_owner`` and ``emit_graph_write`` are
    stubbed -- this test asserts the ordering of the row vs mirror writes
    only, not the ledger append or the ownership check (both are covered by
    their own wave's tests).
    """
    conn = _RecordingConn()
    engine = MagicMock()
    sessions: list[Any] = []

    def _session(pool: Any, ns: Any) -> Any:
        sessions.append(ns)
        return _session_yielding(conn)

    with (
        patch.object(inv_stock, "scoped_pg_session", new=_session),
        patch.object(inv_stock, "append_transaction", new=AsyncMock()),
        patch.object(inv_stock, "assert_owner", new=AsyncMock()),
        patch.object(inv_stock, "emit_graph_write", new=AsyncMock()),
    ):
        await inv_stock.do_transfer_stock(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "sku": "SKU-1",
                "qty": Decimal("5"),
                "from_location": _LOCATION_A,
                "to_location": _LOCATION_B,
            },
        )

    assert len(sessions) == 1, "row and mirror must share one scoped session"
    row_writes = [i for i, s in enumerate(conn.statements) if "inventory_items" in s]
    mirror_writes = [i for i, s in enumerate(conn.statements) if "kg_nodes" in s or "kg_edges" in s]
    assert row_writes, "no inventory_items write was issued"
    assert mirror_writes, "no graph-mirror write was issued"
    assert max(row_writes) < min(mirror_writes)
    assert _first_index(conn.statements, "kg_edges") > _first_index(conn.statements, "kg_nodes")
