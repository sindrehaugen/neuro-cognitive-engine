"""
tests/unit/test_project_can_enter_phase.py
==========================================
Acceptance tests for Batch 070 — Module 7.Wave 3 (can-enter-phase surface).

Covers:
  1. ``handle_project_can_enter_phase`` returns valid JSON ``{"ok": True, ...}``
     for a legal transition with all criteria met.
  2. ``handle_project_can_enter_phase`` returns ``{"ok": False, "missing_criteria": [...]}``
     for a legal transition with unmet criteria.
  3. ``handle_project_can_enter_phase`` raises ``McpError(-32602)`` when
     ``namespace_id`` is missing (via ``@mcp_handler`` + ``require_namespace_id``).
  4. ``project_can_enter_phase`` is in ``TOOL_REGISTRY`` with correct flags
     (``cacheable=True``, ``mutation=False``, ``admin_only=False``).

All tests are pure unit tests — no DB, no Redis, no HTTP.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_engine() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# 4. Tool registry — flags
# ---------------------------------------------------------------------------


def test_project_can_enter_phase_flags():
    from nce.tool_registry import TOOL_REGISTRY

    spec = TOOL_REGISTRY["project_can_enter_phase"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False


def test_project_can_enter_phase_in_registry():
    from nce.tool_registry import TOOL_REGISTRY

    assert "project_can_enter_phase" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# 1. ok=True — legal transition, all criteria met
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_can_enter_phase_ok():
    """G0 → G1 with all G1 criteria satisfied returns ok=True."""
    from nce.vertical_modules.project.mcp_handlers import (
        handle_project_can_enter_phase,
    )

    engine = _make_engine()
    result = await handle_project_can_enter_phase(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "project": {
                "current_phase": "G0",
                "criteria_met": ["signed_quote_attached", "project_manager_assigned"],
            },
            "target_phase": "G1",
        },
    )
    parsed = json.loads(result)
    assert parsed["ok"] is True
    assert parsed["missing_criteria"] == []


# ---------------------------------------------------------------------------
# 2. ok=False with missing_criteria — legal transition, criteria unmet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_can_enter_phase_missing_criteria():
    """G0 → G1 with no criteria satisfied returns ok=False with missing list."""
    from nce.vertical_modules.project.mcp_handlers import (
        handle_project_can_enter_phase,
    )

    engine = _make_engine()
    result = await handle_project_can_enter_phase(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "project": {
                "current_phase": "G0",
                "criteria_met": [],
            },
            "target_phase": "G1",
        },
    )
    parsed = json.loads(result)
    assert parsed["ok"] is False
    assert "signed_quote_attached" in parsed["missing_criteria"]
    assert "project_manager_assigned" in parsed["missing_criteria"]
    assert len(parsed["missing_criteria"]) == 2


# ---------------------------------------------------------------------------
# 3. namespace_id missing — McpError raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_can_enter_phase_missing_namespace_id():
    """Missing namespace_id raises McpError(-32602) via @mcp_handler."""
    from nce.mcp_errors import McpError
    from nce.vertical_modules.project.mcp_handlers import (
        handle_project_can_enter_phase,
    )

    engine = _make_engine()
    with pytest.raises(McpError) as exc_info:
        await handle_project_can_enter_phase(
            engine,
            {
                # namespace_id intentionally omitted
                "project": {"current_phase": "G0", "criteria_met": []},
                "target_phase": "G1",
            },
        )
    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS
