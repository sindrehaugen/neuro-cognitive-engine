"""Tests for the NCE-FE-2 tool-registration hook (``register_tool``).

A host registers custom MCP tools without editing ``tool_registry.py`` or the
dispatch loop, and those tools inherit the same gating — see
``docs/FRONTEND_READINESS.md`` (NCE-FE-2).
"""

from __future__ import annotations

import pathlib

import pytest

from nce import tool_registry
from nce.tool_registry import ToolSpec, register_tool


async def _noop_handler(engine, arguments):  # pragma: no cover - not invoked in unit
    return "ok"


@pytest.fixture
def restore_registry():
    """Snapshot + restore TOOL_REGISTRY so host tools don't leak between tests."""
    before = dict(tool_registry.TOOL_REGISTRY)
    yield
    tool_registry.TOOL_REGISTRY.clear()
    tool_registry.TOOL_REGISTRY.update(before)
    tool_registry._refresh_derived_sets()


def test_register_tool_is_dispatchable_and_scope_enforced(restore_registry):
    register_tool("host_admin_tool", ToolSpec(_noop_handler, admin_only=True))
    # Dispatchable: present in the single source of truth the dispatch loop reads.
    spec = tool_registry.TOOL_REGISTRY.get("host_admin_tool")
    assert spec is not None
    assert spec.handler is _noop_handler
    # Scope enforced: admin flag is live (dispatch reads spec.admin_only) and the
    # derived set the rest of the codebase queries is refreshed.
    assert spec.admin_only is True
    assert "host_admin_tool" in tool_registry.ADMIN_ONLY_TOOLS
    # A non-admin host tool is NOT gated as admin.
    register_tool("host_open_tool", ToolSpec(_noop_handler))
    assert "host_open_tool" not in tool_registry.ADMIN_ONLY_TOOLS


def test_duplicate_name_requires_replace(restore_registry):
    register_tool("host_dup", ToolSpec(_noop_handler))
    with pytest.raises(ValueError):
        register_tool("host_dup", ToolSpec(_noop_handler))
    # replace=True overrides cleanly and the derived sets follow.
    register_tool("host_dup", ToolSpec(_noop_handler, mutation=True), replace=True)
    assert tool_registry.TOOL_REGISTRY["host_dup"].mutation is True
    assert "host_dup" in tool_registry.MUTATION_TOOLS


def test_empty_name_rejected(restore_registry):
    with pytest.raises(ValueError):
        register_tool("", ToolSpec(_noop_handler))


@pytest.mark.asyncio
async def test_disabled_toggle_applies_to_host_tool(restore_registry):
    from nce.mcp_stdio_dispatch import execute_call_tool
    from nce.tool_governance import GOVERNANCE

    register_tool("host_toggle_tool", ToolSpec(_noop_handler))

    # Batch 100: governance reads the ``nce:tools:disabled`` hash via ``hkeys``
    # (last-known-good cache), not the old per-call ``hexists``.
    class _FakeRedis:
        async def hkeys(self, key):
            return [b"host_toggle_tool"] if key == "nce:tools:disabled" else []

    class _FakeEngine:
        redis_client = _FakeRedis()

    # Force a live refresh so the host-tool disabled snapshot is read (the
    # conftest pre-seeds an INITIALIZED-EMPTY allow-snapshot otherwise).
    GOVERNANCE._snapshot = None
    GOVERNANCE._fetched_at = None

    resp = await execute_call_tool(_FakeEngine(), "host_toggle_tool", {})
    text = resp[0].text
    assert "disabled by the administrator" in text  # name-based toggle fired


def test_registry_source_has_no_host_imports():
    """NCE core (tool_registry + fleet) must carry no host-specific imports."""
    src = pathlib.Path(tool_registry.__file__).read_text(encoding="utf-8")
    fleet = pathlib.Path(tool_registry.__file__).with_name("admin_handlers") / "fleet.py"
    fleet_src = fleet.read_text(encoding="utf-8") if fleet.exists() else ""
    for marker in ("steps_bff", "steps_mcp_tools", "nettailer", "portal_hr"):
        assert marker not in src, f"{marker!r} leaked into tool_registry.py"
        assert marker not in fleet_src, f"{marker!r} leaked into fleet.py"
