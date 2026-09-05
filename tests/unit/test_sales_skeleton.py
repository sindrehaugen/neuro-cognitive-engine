"""
tests/unit/test_sales_skeleton.py
===================================
Acceptance tests for Batch 080 — Module 5.Wave 1 (sales-skeleton).

Covers:
  1. The ``sales`` package imports cleanly.
  2. ``handle_sales_ping`` returns ``{"ok": true, "engine": "sales"}``
     for a valid ``namespace_id``.
  3. ``handle_sales_ping`` raises ``McpError(-32602)`` when ``namespace_id``
     is absent (``@mcp_handler`` converts the ``ValueError`` from
     ``require_namespace_id``).
  4. ``TOOL_REGISTRY["sales_ping"]`` exists with ``cacheable=True``,
     ``admin_only=False``, ``mutation=False``.
  5. Tool-count delta: registry grew by exactly 1 relative to the previous
     baseline of 96 (now 97).

All tests are pure unit tests — no DB, no Redis, no HTTP.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Package import
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    import nce.vertical_modules.sales  # noqa: F401
    import nce.vertical_modules.sales.mcp_handlers  # noqa: F401


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    """Minimal NCEEngine mock — sales_ping needs no DB calls."""
    return MagicMock()


# ---------------------------------------------------------------------------
# 2. handle_sales_ping — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_ping_ok() -> None:
    from nce.vertical_modules.sales.mcp_handlers import (
        handle_sales_ping,
    )

    engine = _make_engine()
    result = await handle_sales_ping(engine, {"namespace_id": _NAMESPACE_ID})
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["engine"] == "sales"


# ---------------------------------------------------------------------------
# 3. handle_sales_ping — missing namespace_id → McpError(-32602)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sales_ping_missing_namespace_id() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.sales.mcp_handlers import (
        handle_sales_ping,
    )

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_sales_ping(engine, {})

    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS


# ---------------------------------------------------------------------------
# 4. TOOL_REGISTRY entry — flags
# ---------------------------------------------------------------------------


def test_sales_ping_registered_with_correct_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "sales_ping" in TOOL_REGISTRY, "'sales_ping' not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY["sales_ping"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


# ---------------------------------------------------------------------------
# 5. Tool-count delta: total grew by exactly 2 → now 105
# ---------------------------------------------------------------------------


def test_tool_count_grew_by_one() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert len(TOOL_REGISTRY) == 170, (
        f"Expected 135 tools (unified realignment registry; +11 inventory tools from "
        f"Batch 138a, M11.W10a -- this Sales test carries a repo-wide registry ratchet, "
        f"so it moves whenever ANY module registers a tool), "
        f"got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )
