"""
tests/unit/test_procurement_surface.py
=======================================
Acceptance tests for Batch 047 — Module 1.Wave 4 (cores-surface).

Covers:
  1. ``procurement_calculate_tco``, ``procurement_rank_suppliers``,
     ``procurement_evaluate_match`` are in TOOL_REGISTRY with correct flags
     (cacheable=True, admin_only=False, mutation=False, migration=False).
  2. Each ``handle_*`` returns valid JSON for a good payload.
  3. Each ``handle_*`` returns ``{"error": ...}`` for a missing ``namespace_id``.
  4. Tool-count assertion reflects +3 procurement tools (total 81).
  5. REST routes are mounted in the admin app.

All tests are pure unit tests (no DB, no Redis, no real config load).
``load_procurement_config`` is patched to return fixed weights/tolerances.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"

_WEIGHTS: dict[str, Any] = {
    "TCO_WEIGHTS": {
        "freight": 0.05,
        "warranty": 0.02,
        "stock": 0.03,
        "delivery_risk": 0.01,
    },
    "SCORING_WEIGHTS": {
        "tco": 0.40,
        "delivery_reliability": 0.20,
        "bid_price": 0.20,
        "tier_bundling": 0.10,
        "kickback_proximity": 0.10,
    },
}

_TOLERANCES: dict[str, Any] = {
    "MATCH_TOLERANCE": {
        "GREEN_THRESHOLD": 85,
        "YELLOW_THRESHOLD": 60,
        "zones": {
            "GREEN": {"label": "GREEN", "action": "auto_approve"},
            "YELLOW": {"label": "YELLOW", "action": "review"},
            "RED": {"label": "RED", "action": "reject"},
        },
    },
    "DEFAULT_THRESHOLDS": {"green": 85, "yellow": 60},
}


@pytest.fixture(autouse=True)
def _patch_load_config(monkeypatch):
    """Patch load_procurement_config in all procurement surface modules."""
    for mod_path in (
        "nce.vertical_modules.procurement.mcp_handlers.load_procurement_config",
        "nce.admin_handlers.procurement.load_procurement_config",
    ):
        monkeypatch.setattr(mod_path, lambda: (_WEIGHTS, _TOLERANCES))


def _make_engine() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# 1. Tool registry — flags
# ---------------------------------------------------------------------------


def test_procurement_calculate_tco_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["procurement_calculate_tco"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


def test_procurement_rank_suppliers_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["procurement_rank_suppliers"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


def test_procurement_evaluate_match_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["procurement_evaluate_match"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


# ---------------------------------------------------------------------------
# 4. Tool-count assertion
# ---------------------------------------------------------------------------


def test_tool_count_includes_procurement_tools():
    from nce.tool_registry import TOOL_REGISTRY

    assert "procurement_calculate_tco" in TOOL_REGISTRY
    assert "procurement_rank_suppliers" in TOOL_REGISTRY
    assert "procurement_evaluate_match" in TOOL_REGISTRY
    assert len(TOOL_REGISTRY) >= 95, (
        f"Expected at least 95 tools (unified realignment registry), "
        f"got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )


# ---------------------------------------------------------------------------
# 2. handle_* returns valid JSON for good payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_calculate_tco_returns_valid_json():
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_calculate_tco,
    )

    engine = _make_engine()
    result = await handle_procurement_calculate_tco(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "supplier": {"unit_price": 100.0},
            "bom_line": {"quantity": 5},
        },
    )
    parsed = json.loads(result)
    assert "total" in parsed
    assert "price" in parsed
    assert "freight" in parsed
    assert "warranty" in parsed
    assert "stock" in parsed
    assert "delivery_risk" in parsed
    assert parsed["price"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_handle_rank_suppliers_returns_valid_json():
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_rank_suppliers,
    )

    engine = _make_engine()
    result = await handle_procurement_rank_suppliers(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "bom_line": {"quantity": 10},
            "candidates": [
                {"unit_price": 50.0, "supplier_id": "A"},
                {"unit_price": 60.0, "supplier_id": "B"},
            ],
        },
    )
    parsed = json.loads(result)
    assert "ranked" in parsed
    assert "rebate_override" in parsed
    assert "rebate_rationale" in parsed
    assert len(parsed["ranked"]) == 2


@pytest.mark.asyncio
async def test_handle_evaluate_match_returns_valid_json():
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_evaluate_match,
    )

    engine = _make_engine()
    result = await handle_procurement_evaluate_match(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "po": {"article_id": "SKU-001", "quantity": 10, "unit_price": 25.0},
            "goods_receipt": {"quantity": 10},
            "invoice": {"article_id": "SKU-001", "quantity": 10, "unit_price": 25.0},
        },
    )
    parsed = json.loads(result)
    assert "confidence" in parsed
    assert "tier" in parsed
    assert "tolerance_zone" in parsed
    assert "substitution" in parsed
    assert parsed["tier"] == "GREEN"


# ---------------------------------------------------------------------------
# 3. handle_* returns {"error": ...} for missing namespace_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_calculate_tco_missing_namespace_id():
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_calculate_tco,
    )

    engine = _make_engine()
    # @mcp_handler catches ValueError and raises McpError, but the
    # caller (dispatch) formats that; here we just confirm a McpError is raised.
    from nce.mcp_errors import McpError

    with pytest.raises(McpError) as exc_info:
        await handle_procurement_calculate_tco(
            engine,
            {
                # namespace_id intentionally omitted
                "supplier": {"unit_price": 100.0},
                "bom_line": {"quantity": 5},
            },
        )
    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS


@pytest.mark.asyncio
async def test_handle_rank_suppliers_missing_namespace_id():
    from nce.mcp_errors import McpError
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_rank_suppliers,
    )

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_procurement_rank_suppliers(
            engine,
            {
                "bom_line": {"quantity": 10},
                "candidates": [{"unit_price": 50.0}],
            },
        )
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_evaluate_match_missing_namespace_id():
    from nce.mcp_errors import McpError
    from nce.vertical_modules.procurement.mcp_handlers import (
        handle_procurement_evaluate_match,
    )

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_procurement_evaluate_match(
            engine,
            {
                "po": {"article_id": "SKU-001", "quantity": 10, "unit_price": 25.0},
                "goods_receipt": {"quantity": 10},
                "invoice": {"article_id": "SKU-001", "quantity": 10, "unit_price": 25.0},
            },
        )
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 5. REST routes are mounted in the admin app
# ---------------------------------------------------------------------------


def test_procurement_routes_mounted_in_admin_app():
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/procurement/tco" in paths
    assert "/api/procurement/rank" in paths
    assert "/api/procurement/match" in paths
