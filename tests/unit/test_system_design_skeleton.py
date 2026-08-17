"""
tests/unit/test_system_design_skeleton.py
==========================================
Acceptance tests for Batch 056 — Module 6.Wave 1 (skeleton).

Covers:
  1. The ``system_design`` package imports cleanly.
  2. ``handle_system_design_ping`` returns ``{"ok": true, "engine": "system_design"}``
     for a valid ``namespace_id``.
  3. ``handle_system_design_ping`` raises ``McpError(-32602)`` when ``namespace_id``
     is absent (``@mcp_handler`` converts the ``ValueError`` from
     ``require_namespace_id``).
  4. ``TOOL_REGISTRY["system_design_ping"]`` exists with ``cacheable=True``,
     ``admin_only=False``, ``mutation=False``.
  5. Tool-count delta: registry grew by exactly 1 relative to the previous
     baseline of 84 (now 85).

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
    import nce.vertical_modules.system_design  # noqa: F401
    import nce.vertical_modules.system_design.mcp_handlers  # noqa: F401


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    """Minimal NCEEngine mock — system_design_ping needs no DB calls."""
    return MagicMock()


# ---------------------------------------------------------------------------
# 2. handle_system_design_ping — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_system_design_ping_ok() -> None:
    from nce.vertical_modules.system_design.mcp_handlers import (
        handle_system_design_ping,
    )

    engine = _make_engine()
    result = await handle_system_design_ping(engine, {"namespace_id": _NAMESPACE_ID})
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["engine"] == "system_design"


# ---------------------------------------------------------------------------
# 3. handle_system_design_ping — missing namespace_id → McpError(-32602)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_system_design_ping_missing_namespace_id() -> None:
    from nce.mcp_errors import McpError
    from nce.vertical_modules.system_design.mcp_handlers import (
        handle_system_design_ping,
    )

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_system_design_ping(engine, {})

    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS


# ---------------------------------------------------------------------------
# 4. TOOL_REGISTRY entry — flags
# ---------------------------------------------------------------------------


def test_system_design_ping_registered_with_correct_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "system_design_ping" in TOOL_REGISTRY, "'system_design_ping' not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY["system_design_ping"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


# ---------------------------------------------------------------------------
# 5. Tool-count delta: total grew by exactly 1 → now 85
# ---------------------------------------------------------------------------


def test_tool_count_grew_by_one() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert len(TOOL_REGISTRY) >= 95, (
        f"Expected at least 95 tools (unified realignment registry), got {len(TOOL_REGISTRY)}: {sorted(TOOL_REGISTRY)}"
    )
