"""
tests/unit/test_system_design_toolcount.py
==========================================
Plain unit test for Module 6 Phase-1a — tool-count regression guard.

Pins the live ``system_design_*`` entries in ``TOOL_REGISTRY`` to the exact
Phase-1a set (W1 ping + W11 lucid-export).  Fails loudly when:
  - a tool is silently added or dropped,
  - a tool's ``cacheable`` / ``admin_only`` / ``mutation`` flag changes.

No DB, no Redis, no HTTP — pure import-time assertion.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Expected Phase-1a tool set (W1 ping + W11 lucid-export).
# The flow cores (propose/from_quote/sow/enrichment/to_quote/validate) are
# NOT registered as MCP tools — they are invoked through the flow.
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS: dict[str, dict[str, bool]] = {
    "system_design_ping": {
        "cacheable": True,
        "admin_only": False,
        "mutation": False,
    },
    "system_design_publish_design_docs": {
        "cacheable": False,
        "admin_only": False,
        "mutation": True,
    },
}


def _get_system_design_tools() -> dict[str, object]:
    """Return only the system_design_* entries from the live TOOL_REGISTRY."""
    from nce.tool_registry import TOOL_REGISTRY

    return {k: v for k, v in TOOL_REGISTRY.items() if k.startswith("system_design_")}


# ---------------------------------------------------------------------------
# 1. Exact count
# ---------------------------------------------------------------------------


def test_system_design_tool_count_is_exact() -> None:
    """Exactly 2 system_design_* tools must be registered (Phase-1a set)."""
    live = _get_system_design_tools()
    assert len(live) == len(_EXPECTED_TOOLS), (
        f"Expected {len(_EXPECTED_TOOLS)} system_design_* tools, got {len(live)}.\n"
        f"Live set: {sorted(live)}\n"
        f"Expected: {sorted(_EXPECTED_TOOLS)}"
    )


# ---------------------------------------------------------------------------
# 2. Exact name set
# ---------------------------------------------------------------------------


def test_system_design_tool_names_are_exact() -> None:
    """The system_design_* tool names must match the Phase-1a set exactly."""
    live = _get_system_design_tools()
    assert set(live) == set(_EXPECTED_TOOLS), (
        f"Tool name mismatch.\n"
        f"Extra tools:   {sorted(set(live) - set(_EXPECTED_TOOLS))}\n"
        f"Missing tools: {sorted(set(_EXPECTED_TOOLS) - set(live))}"
    )


# ---------------------------------------------------------------------------
# 3. Per-tool flag assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name,expected_flags", list(_EXPECTED_TOOLS.items()))
def test_system_design_tool_flags(tool_name: str, expected_flags: dict[str, bool]) -> None:
    """Each system_design_* tool must carry the exact cacheable/admin_only/mutation flags."""
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY, (
        f"Tool '{tool_name}' not found in TOOL_REGISTRY.\nLive keys: {sorted(TOOL_REGISTRY)}"
    )
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected_value in expected_flags.items():
        actual_value = getattr(spec, flag)
        assert actual_value == expected_value, (
            f"Tool '{tool_name}': expected {flag}={expected_value!r}, got {actual_value!r}"
        )
