"""
tests/unit/test_product_surface.py
====================================
Acceptance tests for Batch 033 — Module 2.Wave 3 (search-get-graph).

Covers:
  1. ``do_search_products`` returns correct shape with no cost/margin/BID leak.
  2. ``do_get_product`` returns correct shape with no cost/margin/BID leak.
  3. ``product_search`` / ``product_get`` are registered in TOOL_REGISTRY with
     correct flags (cacheable=True, mutation=False, admin_only=False).
  4. Tool-count assertion is bumped to 74 (two new registry entries).
  5. ``upsert_product_node`` writes a PRODUCT_SKU node (mock the conn).
  6. ``upsert_bom_references_edge`` writes a references edge with confidence
     on the edge (mock the conn).
  7. No ``confidence`` column on the node upsert SQL (invariant check).

All tests are pure unit tests (no DB, no Redis).  DB-dependent tests are
``@pytest.mark.integration``; none of those are added here.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1-2: do_search_products / do_get_product — shape + no secret leak
# ---------------------------------------------------------------------------

_FORBIDDEN: frozenset[str] = frozenset({"cost_price", "bid_id", "margin", "cost", "unit_cost"})

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine(fetch_return=None, fetchrow_return=None) -> MagicMock:
    """Build a minimal NCEEngine mock with a pg_pool that honours scoped_pg_session."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="SET")
    # fetchval is used by scoped_pg_session to set the namespace GUC
    conn.fetchval = AsyncMock(return_value=None)

    pool = AsyncMock()
    # scoped_pg_session uses pool.acquire() as an async context manager
    pool.acquire = MagicMock(return_value=_async_ctx(conn))
    engine.pg_pool = pool
    return engine, conn


class _async_ctx:
    """Minimal async context manager that yields the given object."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


# Patch the product opt-in guard so handle_* tests pass without a real namespaces table.
@pytest.fixture(autouse=True)
def _patch_product_guard(monkeypatch):
    """Bypass the metadata.product.enabled guard for all unit tests in this file."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "nce.vertical_modules.product.mcp_handlers._check_product_enabled",
        AsyncMock(return_value=None),
    )


# scoped_pg_session does SET LOCAL on the connection; we patch it to a no-op.
@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch):
    """Replace scoped_pg_session with a trivial pass-through for unit tests."""

    class _FakeScoped:
        def __init__(self, pool, ns):
            self._pool = pool
            self._ns = ns

        async def __aenter__(self):
            # Return the conn that pool.acquire() would return
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.product.mcp_handlers.scoped_pg_session",
        _FakeScoped,
    )


@pytest.mark.asyncio
async def test_do_search_products_returns_expected_shape():
    from nce.vertical_modules.product.mcp_handlers import do_search_products

    fake_rows = [
        {
            "id": "aaa",
            "manufacturer": "CISCO",
            "mfr_part_no": "SFP-10G-SR",
            "gtin": None,
            "lifecycle_status": "active",
            "etim_specs": {},
            "updated_at": None,
        }
    ]
    engine, conn = _make_engine(fetch_return=fake_rows)

    result = await do_search_products(engine, {"namespace_id": _NAMESPACE_ID, "query": "SFP"})

    assert "results" in result
    assert "total" in result
    assert result["total"] == 1
    assert result["results"][0]["manufacturer"] == "CISCO"
    # No forbidden fields
    for row in result["results"]:
        for bad_col in _FORBIDDEN:
            assert bad_col not in row, f"Forbidden column '{bad_col}' leaked in search result"


@pytest.mark.asyncio
async def test_do_search_products_no_query_raises():
    from nce.vertical_modules.product.mcp_handlers import do_search_products

    engine, _ = _make_engine()
    with pytest.raises(ValueError, match="query"):
        await do_search_products(engine, {"namespace_id": _NAMESPACE_ID, "query": ""})


@pytest.mark.asyncio
async def test_do_get_product_returns_expected_shape():
    from nce.vertical_modules.product.mcp_handlers import do_get_product

    master_row = {
        "id": "bbb",
        "manufacturer": "CISCO",
        "mfr_part_no": "SFP-10G-SR",
        "gtin": None,
        "lifecycle_status": "active",
        "etim_specs": {},
        "created_at": None,
        "updated_at": None,
    }
    price_rows = [{"supplier": "nettailer", "list_price": 99.0, "updated_at": None}]
    edge_rows = [
        {
            "predicate": "references",
            "object_label": "PRODUCT:CISCO:SFP-10G-SR",
            "confidence": 0.9,
            "updated_at": None,
        }
    ]

    engine, conn = _make_engine(fetch_return=price_rows, fetchrow_return=master_row)
    # Second conn.fetch call (edges) should return edge_rows; first call returns price_rows.
    conn.fetch = AsyncMock(side_effect=[price_rows, edge_rows])

    result = await do_get_product(
        engine,
        {"namespace_id": _NAMESPACE_ID, "mfr_part_no": "SFP-10G-SR", "manufacturer": "CISCO"},
    )

    assert "product" in result
    assert "prices" in result
    assert "edges" in result

    # No forbidden fields anywhere
    def _check_no_forbidden(rows, label):
        for row in rows:
            for bad_col in _FORBIDDEN:
                assert bad_col not in row, f"Forbidden column '{bad_col}' in {label}"

    _check_no_forbidden([result["product"]] if result["product"] else [], "product")
    _check_no_forbidden(result["prices"], "prices")
    _check_no_forbidden(result["edges"], "edges")

    assert result["product"]["manufacturer"] == "CISCO"
    assert result["prices"][0]["list_price"] == 99.0
    assert result["edges"][0]["confidence"] == 0.9


@pytest.mark.asyncio
async def test_do_get_product_not_found_returns_none():
    from nce.vertical_modules.product.mcp_handlers import do_get_product

    engine, conn = _make_engine(fetchrow_return=None)

    result = await do_get_product(
        engine, {"namespace_id": _NAMESPACE_ID, "mfr_part_no": "UNKNOWN-PART"}
    )

    assert result["product"] is None
    assert result["prices"] == []
    assert result["edges"] == []


@pytest.mark.asyncio
async def test_do_get_product_no_mfr_part_no_raises():
    from nce.vertical_modules.product.mcp_handlers import do_get_product

    engine, _ = _make_engine()
    with pytest.raises(ValueError, match="mfr_part_no"):
        await do_get_product(engine, {"namespace_id": _NAMESPACE_ID})


# ---------------------------------------------------------------------------
# MCP handle wrappers return JSON strings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_product_search_returns_json_string():
    from nce.vertical_modules.product.mcp_handlers import handle_product_search

    engine, conn = _make_engine(fetch_return=[])

    result = await handle_product_search(engine, {"namespace_id": _NAMESPACE_ID, "query": "SFP"})
    parsed = json.loads(result)
    assert "results" in parsed
    assert "total" in parsed


@pytest.mark.asyncio
async def test_handle_product_get_returns_json_string():
    from nce.vertical_modules.product.mcp_handlers import handle_product_get

    engine, conn = _make_engine(fetchrow_return=None)

    result = await handle_product_get(
        engine, {"namespace_id": _NAMESPACE_ID, "mfr_part_no": "NONE"}
    )
    parsed = json.loads(result)
    assert "product" in parsed


# ---------------------------------------------------------------------------
# 3-4: Tool registry — flags + total count
# ---------------------------------------------------------------------------


def test_product_search_registered_with_correct_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["product_search"]
    assert spec.cacheable is True
    assert spec.mutation is False
    assert spec.admin_only is False
    assert spec.migration is False


def test_product_get_registered_with_correct_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["product_get"]
    assert spec.cacheable is True
    assert spec.mutation is False
    assert spec.admin_only is False
    assert spec.migration is False


def test_tool_count_includes_product_tools():
    from nce.tool_registry import TOOL_REGISTRY

    assert "product_search" in TOOL_REGISTRY
    assert "product_get" in TOOL_REGISTRY
    assert len(TOOL_REGISTRY) >= 95, (
        f"Expected at least 95 tools (unified realignment registry), got {len(TOOL_REGISTRY)}: "
        f"{sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 5-7: Graph upsert — node + edge, confidence on edge only (mock conn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_product_node_calls_correct_sql():
    """PRODUCT_SKU node upsert: asserts ownership, inserts with correct entity_type."""
    from nce.vertical_modules.product.graph import upsert_product_node

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with (
        patch(
            "nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock
        ) as mock_owner,
        patch(
            "nce.vertical_modules.product.graph.emit_graph_write", new_callable=AsyncMock
        ) as mock_emit,
    ):
        await upsert_product_node(
            conn,
            _NAMESPACE_ID,
            manufacturer="CISCO",
            mfr_part_no="SFP-10G-SR",
        )

    # Ownership must be asserted before any DB write
    mock_owner.assert_called_once()
    call_kwargs = mock_owner.call_args
    assert call_kwargs[0][2] == "PRODUCT_SKU"  # node_type positional arg
    assert call_kwargs[0][3] == "product"  # writer_engine positional arg

    # INSERT must have been called
    conn.execute.assert_called_once()
    sql: str = conn.execute.call_args[0][0]

    # Ensure confidence is NOT in the node INSERT
    assert "confidence" not in sql.lower(), "confidence must not appear in kg_nodes INSERT"

    # entity_type must be PRODUCT_SKU
    # The actual value is passed as $2 — check the positional args
    insert_args = conn.execute.call_args[0]
    assert "PRODUCT_SKU" in insert_args, f"entity_type PRODUCT_SKU not found in args: {insert_args}"

    # Label format check
    label_arg = insert_args[1]  # $1 = label
    assert label_arg == "PRODUCT:CISCO:SFP-10G-SR"

    # emit_graph_write called inside same connection
    mock_emit.assert_called_once()
    emit_kwargs = mock_emit.call_args[1]
    assert emit_kwargs["node_type"] == "PRODUCT_SKU"
    assert emit_kwargs["op"] == "upserted"


@pytest.mark.asyncio
async def test_upsert_bom_references_edge_carries_confidence():
    """BOM_LINE->PRODUCT edge: confidence is passed to the INSERT, not to a node."""
    from nce.vertical_modules.product.graph import upsert_bom_references_edge

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with patch(
        "nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock
    ) as mock_owner:
        await upsert_bom_references_edge(
            conn,
            _NAMESPACE_ID,
            bom_line_label="BOM_LINE:PRJ-001:ITEM-42",
            manufacturer="CISCO",
            mfr_part_no="SFP-10G-SR",
            confidence=0.85,
        )

    mock_owner.assert_called_once()

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]

    # Verify positional args: subject_label, predicate, object_label, confidence, namespace_id
    assert args[1] == "BOM_LINE:PRJ-001:ITEM-42"  # subject_label
    assert args[2] == "references"  # predicate
    assert args[3] == "PRODUCT:CISCO:SFP-10G-SR"  # object_label
    assert abs(args[4] - 0.85) < 1e-9  # confidence (on the edge, not node)


@pytest.mark.asyncio
async def test_upsert_product_node_sql_has_no_confidence_column():
    """Regression guard: confidence must never appear in the kg_nodes INSERT."""
    from nce.vertical_modules.product.graph import upsert_product_node

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    with (
        patch("nce.vertical_modules.product.graph.assert_owner", new_callable=AsyncMock),
        patch("nce.vertical_modules.product.graph.emit_graph_write", new_callable=AsyncMock),
    ):
        await upsert_product_node(
            conn,
            _NAMESPACE_ID,
            manufacturer="TEST",
            mfr_part_no="PART-1",
        )

    sql: str = conn.execute.call_args[0][0]
    assert "confidence" not in sql.lower(), (
        "Rule 7 violation: 'confidence' must NOT appear in the kg_nodes INSERT SQL. "
        "Confidence belongs on kg_edges only."
    )
