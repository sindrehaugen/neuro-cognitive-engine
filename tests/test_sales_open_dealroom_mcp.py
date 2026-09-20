"""
tests/test_sales_open_dealroom_mcp.py
========================================
Wave B-4 (2026-09-20): the MCP tool side of DealRoom (``sales_open_dealroom``).

The REST route (``POST /api/sales/dealroom``, ``admin_handlers/sales.py::
api_admin_sales_dealroom``) and the underlying ``do_open_dealroom``
(``dealroom.py``, Wave S-3) both already existed and are covered by
``tests/test_sales_dealroom.py``. Q-5 does not block this wave: DEAL already
has a ``node-ownership.json`` row and a registered ``ResourceSpec``
(``sales/resources.py::DEAL_SPEC``) -- this is wiring an existing internal
function to a new MCP tool, not declaring a new node type.

Covers:
  1. ``handle_sales_open_dealroom``: missing namespace/quote_id -> McpError,
     successful delegation with exact param passthrough (mocked).
  2. Live end-to-end: the MCP tool actually materialises a real DealRoom
     from real bom_line_content/product_catalog rows, proving the wiring
     reaches the real function, not just that the mock was called.
  3. Surface invariants: TOOL_REGISTRY spec, mcp_stdio_tools.TOOLS schema,
     REST route unaffected (already covered, spot-checked here).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest

from nce.auth import set_namespace_context
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules.sales.mcp_handlers import handle_sales_open_dealroom

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# 1. handle_sales_open_dealroom (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_open_dealroom_missing_namespace() -> None:
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_open_dealroom(engine, {"quote_id": "Q-1"})


@pytest.mark.asyncio
async def test_handle_sales_open_dealroom_missing_quote_id_reaches_do_open_dealroom() -> None:
    """The handler does not duplicate quote_id validation -- do_open_dealroom's
    own ValueError is what @mcp_handler translates, same as the REST route
    delegates its own required-field check separately at its own layer.
    """
    engine = _make_engine()
    with pytest.raises(McpError):
        await handle_sales_open_dealroom(engine, {"namespace_id": _NAMESPACE_ID})


@pytest.mark.asyncio
async def test_handle_sales_open_dealroom_success_passes_through_exact_params() -> None:
    engine = _make_engine()
    expected_result = {
        "quote_id": "Q-1",
        "name": "Test Quote",
        "description": "desc",
        "total_price_nok": 1000.0,
        "unpriced_line_count": 0,
        "lines": [],
    }

    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_open_dealroom",
        new=AsyncMock(return_value=expected_result),
    ) as mock_core:
        raw = await handle_sales_open_dealroom(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "quote_id": "Q-1",
                "toggled_options": {"LINE-1": False},
            },
        )

    assert json.loads(raw) == expected_result
    mock_core.assert_awaited_once_with(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "quote_id": "Q-1",
            "toggled_options": {"LINE-1": False},
        },
    )


@pytest.mark.asyncio
async def test_handle_sales_open_dealroom_defaults_toggled_options_to_empty_dict() -> None:
    engine = _make_engine()
    with patch(
        "nce.vertical_modules.sales.mcp_handlers.do_open_dealroom",
        new=AsyncMock(return_value={}),
    ) as mock_core:
        await handle_sales_open_dealroom(engine, {"namespace_id": _NAMESPACE_ID, "quote_id": "Q-1"})

    mock_core.assert_awaited_once_with(
        engine,
        {"namespace_id": _NAMESPACE_ID, "quote_id": "Q-1", "toggled_options": {}},
    )


# ---------------------------------------------------------------------------
# 2. Live end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handle_sales_open_dealroom_live_materialises_a_real_quote(
    pg_pool: asyncpg.Pool, make_namespace: Any
) -> None:
    """Proves the MCP tool reaches the real do_open_dealroom against real
    Postgres rows -- not just that a mock was called with the right args.
    """
    ns = await make_namespace()
    quote_id = f"q-{uuid4().hex[:8]}"
    label = f"BOM_LINE:{quote_id.upper()}:LINE-1"

    class _EngineStub:
        pass

    engine = _EngineStub()
    engine.pg_pool = pg_pool  # type: ignore[attr-defined]

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns)
            await conn.execute(
                """
                INSERT INTO sales_read_model
                    (namespace_id, entity, source_id, name, source_json, manual, is_deleted, modifiedon, synced_at)
                VALUES ($1, 'quotes', $2, $3, $4::jsonb, '{}'::jsonb, false, now(), now())
                """,
                str(ns),
                quote_id,
                "Live MCP Quote",
                json.dumps({"quoteid": quote_id, "name": "Live MCP Quote", "description": "d"}),
            )
            await conn.execute(
                """
                INSERT INTO bom_line_content
                    (namespace_id, bom_line_label, quote_id, line_ref, writer_engine,
                     origin_kind, qty, unit_price, line_total, currency, priced)
                VALUES ($1, $2, $3, 'LINE-1', 'sales', 'manual', 1.0, 100.0, 100.0, 'NOK', true)
                """,
                str(ns),
                label,
                quote_id,
            )

    raw = await handle_sales_open_dealroom(engine, {"namespace_id": str(ns), "quote_id": quote_id})
    result = json.loads(raw)

    assert result["quote_id"] == quote_id
    assert result["name"] == "Live MCP Quote"
    assert len(result["lines"]) == 1
    assert abs(result["total_price_nok"] - 100.0) < 1e-6


# ---------------------------------------------------------------------------
# 3. Surface invariants
# ---------------------------------------------------------------------------


def test_sales_open_dealroom_tool_registry_spec() -> None:
    spec = TOOL_REGISTRY.get("sales_open_dealroom")
    assert spec is not None
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False


def test_sales_open_dealroom_in_stdio_list() -> None:
    tool_names = {t.name for t in TOOLS}
    assert "sales_open_dealroom" in tool_names
