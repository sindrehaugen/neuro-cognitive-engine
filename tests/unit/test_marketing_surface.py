"""Unit tests for Module 14 Marketing Engine MCP surface, flags, and schemas."""

from __future__ import annotations

from tests.tool_pins import FLAG_PINS

from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)

# Pin lives in tests/tool_pins.py (FLAG_PINS["marketing"]) -- edit there.
EXPECTED_MARKETING_TOOLS = FLAG_PINS["marketing"]


def test_marketing_tools_registered_in_tool_registry():
    """Verify all 9 marketing tools are present in TOOL_REGISTRY with correct flags."""
    for tool_name, flags in EXPECTED_MARKETING_TOOLS.items():
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name!r} missing from TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable is flags["cacheable"], (
            f"Tool {tool_name!r} cacheable is {spec.cacheable}, expected {flags['cacheable']}"
        )
        assert spec.admin_only is flags["admin_only"], (
            f"Tool {tool_name!r} admin_only is {spec.admin_only}, expected {flags['admin_only']}"
        )
        assert spec.mutation is flags["mutation"], (
            f"Tool {tool_name!r} mutation is {spec.mutation}, expected {flags['mutation']}"
        )


def test_marketing_tools_flag_sets():
    """Verify presence in global flag sets."""
    for tool_name, flags in EXPECTED_MARKETING_TOOLS.items():
        if flags["mutation"]:
            assert tool_name in MUTATION_TOOLS
        else:
            assert tool_name not in MUTATION_TOOLS

        if flags["admin_only"]:
            assert tool_name in ADMIN_ONLY_TOOLS
        else:
            assert tool_name not in ADMIN_ONLY_TOOLS

        if flags["cacheable"]:
            assert tool_name in CACHEABLE_TOOLS
        else:
            assert tool_name not in CACHEABLE_TOOLS


def test_marketing_tool_schemas_defined():
    """Verify all 9 marketing tools have JSON schemas in mcp_stdio_tools.py."""
    tool_map = {t.name: t for t in TOOLS}
    for tool_name in EXPECTED_MARKETING_TOOLS:
        assert tool_name in tool_map, f"Schema missing for tool {tool_name!r}"
        tool = tool_map[tool_name]
        assert tool.description
        assert "properties" in tool.inputSchema
        assert tool.inputSchema["type"] == "object"
