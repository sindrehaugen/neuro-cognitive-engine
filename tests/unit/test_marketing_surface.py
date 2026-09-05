"""Unit tests for Module 14 Marketing Engine MCP surface, flags, and schemas."""

from __future__ import annotations

from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)

EXPECTED_MARKETING_TOOLS = {
    "marketing_find_case_study_candidates": {
        "cacheable": True,
        "admin_only": False,
        "mutation": False,
    },
    "marketing_draft_case_study": {"cacheable": False, "admin_only": True, "mutation": True},
    "marketing_request_testimonial": {"cacheable": False, "admin_only": True, "mutation": True},
    "marketing_capture_testimonial": {"cacheable": False, "admin_only": True, "mutation": True},
    "marketing_suggest_content": {"cacheable": True, "admin_only": False, "mutation": False},
    "marketing_audit_seo": {"cacheable": True, "admin_only": False, "mutation": False},
    "marketing_approve_content": {"cacheable": False, "admin_only": True, "mutation": True},
    "marketing_publish_content": {"cacheable": False, "admin_only": True, "mutation": True},
}


def test_marketing_tools_registered_in_tool_registry():
    """Verify all 8 marketing tools are present in TOOL_REGISTRY with correct flags."""
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
    """Verify all 8 marketing tools have JSON schemas in mcp_stdio_tools.py."""
    tool_map = {t.name: t for t in TOOLS}
    for tool_name in EXPECTED_MARKETING_TOOLS:
        assert tool_name in tool_map, f"Schema missing for tool {tool_name!r}"
        tool = tool_map[tool_name]
        assert tool.description
        assert "properties" in tool.inputSchema
        assert tool.inputSchema["type"] == "object"
