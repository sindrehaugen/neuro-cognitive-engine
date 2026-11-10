"""Structural contract tests for nce.tool_registry (Pure Core).

These tests pin the exact shape of the pure Core TOOL_REGISTRY and its derived
sets, plus verify the nce.plugins entry-point extension seam.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from nce.resource_surface import build_all_resource_tool_specs
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MIGRATION_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
    ToolSpec,
)

_C12_TOOL_SPECS = build_all_resource_tool_specs()
_C12_TOOL_NAMES = frozenset(_C12_TOOL_SPECS)
_C12_MUTATION_TOOLS = frozenset(n for n, s in _C12_TOOL_SPECS.items() if s.mutation)
_C12_CACHEABLE_TOOLS = frozenset(n for n, s in _C12_TOOL_SPECS.items() if s.cacheable)
_C12_ADMIN_ONLY_TOOLS = frozenset(n for n, s in _C12_TOOL_SPECS.items() if s.admin_only)

_EXPECTED_STATIC_TOTAL = 70
_EXPECTED_TOTAL = _EXPECTED_STATIC_TOTAL + len(_C12_TOOL_NAMES)


def test_registry_has_expected_entries():
    hand_written = TOOL_REGISTRY.keys() - _C12_TOOL_NAMES
    assert len(TOOL_REGISTRY) >= _EXPECTED_STATIC_TOTAL, (
        f"Sanity floor: expected at least {_EXPECTED_STATIC_TOTAL} tools, got {len(TOOL_REGISTRY)}."
    )
    assert len(hand_written) == _EXPECTED_STATIC_TOTAL, (
        f"Hand-written Core tool count changed: expected "
        f"{_EXPECTED_STATIC_TOTAL}, got {len(hand_written)}. Tools: {sorted(hand_written)}"
    )


def test_all_handlers_are_async_callables():
    """Every registered handler must be an awaitable (async def) callable."""
    bad = [
        name
        for name, spec in TOOL_REGISTRY.items()
        if not (callable(spec.handler) and inspect.iscoroutinefunction(spec.handler))
    ]
    assert not bad, f"Non-async handlers found: {bad}"


_EXPECTED_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "a2a_create_grant",
        "a2a_revoke_grant",
        "a2a_update_grant_scopes",
        "abort_migration",
        "boost_memory",
        "commit_migration",
        "complete_bridge_auth",
        "connect_bridge",
        "create_snapshot",
        "decision_feedback_record",
        "delete_snapshot",
        "disconnect_bridge",
        "explain_past_decision",
        "force_resync_bridge",
        "forget_memory",
        "import_snapshot",
        "index_code_file",
        "manage_namespace",
        "manage_quotas",
        "merge_queue_confirm",
        "merge_queue_reject",
        "purge_dlq",
        "replay_dlq",
        "replay_reconstruct",
        "resolve_contradiction",
        "rotate_signing_key",
        "shred_memory",
        "start_migration",
        "store_artifact",
        "store_media",
        "store_memory",
        "trigger_consolidation",
        "trust_dial_set_tier",
        "unredact_memory",
    }
)


def test_mutation_tools_exact_match():
    expected = _EXPECTED_MUTATION_TOOLS | _C12_MUTATION_TOOLS
    assert MUTATION_TOOLS == expected, (
        f"Extra: {MUTATION_TOOLS - expected}  Missing: {expected - MUTATION_TOOLS}"
    )


def test_mutation_tools_count():
    hand_written_mutation_tools = MUTATION_TOOLS - _C12_MUTATION_TOOLS
    assert len(hand_written_mutation_tools) == 34


_EXPECTED_CACHEABLE: frozenset[str] = frozenset(
    {
        "graph_search",
        "merge_queue_list",
        "neuromorphic_search",
        "pricing_get_fx_rates",
        "pricing_resolve",
        "resolve",
        "search_codebase",
        "semantic_search",
        "trust_dial_get_status",
    }
)


def test_cacheable_tools_exact_match():
    expected = _EXPECTED_CACHEABLE | _C12_CACHEABLE_TOOLS
    assert CACHEABLE_TOOLS == expected, (
        f"Extra: {CACHEABLE_TOOLS - expected}  Missing: {expected - CACHEABLE_TOOLS}"
    )


def test_cacheable_tools_count():
    hand_written_cacheable_tools = CACHEABLE_TOOLS - _C12_CACHEABLE_TOOLS
    assert len(hand_written_cacheable_tools) == 9


_EXPECTED_ADMIN_ONLY: frozenset[str] = frozenset(
    {
        "decision_feedback_record",
        "detect_causal_cycles",
        "explain_config_change",
        "explain_past_decision",
        "merge_queue_confirm",
        "merge_queue_reject",
        "replay_fork",
        "replay_observe",
        "replay_reconstruct",
        "replay_status",
        "shred_memory",
        "trust_dial_set_tier",
        "unredact_memory",
    }
)


def test_admin_only_tools_exact_match():
    expected = _EXPECTED_ADMIN_ONLY | _C12_ADMIN_ONLY_TOOLS
    assert ADMIN_ONLY_TOOLS == expected, (
        f"Extra: {ADMIN_ONLY_TOOLS - expected}  Missing: {expected - ADMIN_ONLY_TOOLS}"
    )


def test_admin_only_tools_count():
    assert len(ADMIN_ONLY_TOOLS - _C12_ADMIN_ONLY_TOOLS) == 13


_EXPECTED_MIGRATION: frozenset[str] = frozenset(
    {
        "start_migration",
        "migration_status",
        "validate_migration",
        "commit_migration",
        "abort_migration",
    }
)


def test_migration_tools_exact_match():
    assert MIGRATION_TOOLS == _EXPECTED_MIGRATION, (
        f"Extra: {MIGRATION_TOOLS - _EXPECTED_MIGRATION}  "
        f"Missing: {_EXPECTED_MIGRATION - MIGRATION_TOOLS}"
    )


def test_migration_tools_count():
    assert len(MIGRATION_TOOLS) == 5


def test_mutation_tools_subset_of_registry():
    assert MUTATION_TOOLS <= TOOL_REGISTRY.keys()


def test_cacheable_tools_subset_of_registry():
    assert CACHEABLE_TOOLS <= TOOL_REGISTRY.keys()


def test_admin_only_tools_subset_of_registry():
    assert ADMIN_ONLY_TOOLS <= TOOL_REGISTRY.keys()


def test_migration_tools_subset_of_registry():
    assert MIGRATION_TOOLS <= TOOL_REGISTRY.keys()


def test_migration_mutations_are_in_mutation_tools():
    migration_mutations = {
        name for name, spec in TOOL_REGISTRY.items() if spec.migration and spec.mutation
    }
    assert migration_mutations <= MUTATION_TOOLS


def test_no_tool_is_cacheable_and_mutation():
    overlap = CACHEABLE_TOOLS & MUTATION_TOOLS
    assert not overlap, f"Tools are both cacheable and mutation: {overlap}"


def test_toolspec_is_frozen():
    spec = TOOL_REGISTRY["get_health"]
    with pytest.raises((AttributeError, TypeError)):
        spec.admin_only = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "tool_name,expected_flags",
    [
        (
            "store_memory",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "semantic_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "unredact_memory",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "shred_memory",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "index_code_file",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "search_codebase",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "graph_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "neuromorphic_search",
            {"mutation": False, "cacheable": True, "admin_only": False, "migration": False},
        ),
        (
            "connect_bridge",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "list_bridges",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "start_migration",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": True},
        ),
        (
            "migration_status",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": True},
        ),
        (
            "commit_migration",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": True},
        ),
        (
            "replay_observe",
            {"mutation": False, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "replay_reconstruct",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "get_event_provenance",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "explain_memory",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "explain_past_decision",
            {"mutation": True, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "explain_config_change",
            {"mutation": False, "cacheable": False, "admin_only": True, "migration": False},
        ),
        (
            "a2a_create_grant",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "a2a_list_grants",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "manage_namespace",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "get_health",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "create_snapshot",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "compare_states",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "import_snapshot",
            {"mutation": True, "cacheable": False, "admin_only": False, "migration": False},
        ),
        (
            "suggest_queries",
            {"mutation": False, "cacheable": False, "admin_only": False, "migration": False},
        ),
    ],
)
def test_tool_flags(tool_name: str, expected_flags: dict):
    spec = TOOL_REGISTRY[tool_name]
    for flag, expected in expected_flags.items():
        actual = getattr(spec, flag)
        assert actual == expected, f"{tool_name}.{flag}: expected {expected!r}, got {actual!r}"


def test_plugin_entry_point_loading():
    """Verify nce.plugins entry-point loader discovers and mounts plugin tools."""
    from nce.plugins import load_plugins, register_all_plugin_tools

    async def dummy_handler(engine, args):
        return "{}"

    class DummyPlugin:
        plugin_id = "dummy_vertical"

        def register_tools(self, registry):
            registry["dummy_vertical_ping"] = ToolSpec(
                handler=dummy_handler,
                mutation=False,
                cacheable=True,
            )

    fake_ep = MagicMock()
    fake_ep.name = "dummy_vertical"
    fake_ep.load.return_value = DummyPlugin

    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        plugins = load_plugins(force_reload=True)
        assert len(plugins) == 1
        assert plugins["dummy_vertical"].plugin_id == "dummy_vertical"
        target_reg: dict[str, ToolSpec] = {}
        register_all_plugin_tools(target_reg)
        assert "dummy_vertical_ping" in target_reg
    load_plugins(force_reload=True)


@pytest.mark.asyncio
async def test_handle_neuromorphic_search_success():
    import json
    from unittest.mock import AsyncMock, MagicMock

    from nce.graph_mcp_handlers import handle_neuromorphic_search
    from nce.graph_query import Subgraph

    mock_engine = MagicMock()
    mock_traverser = AsyncMock()
    mock_engine._graph_traverser = mock_traverser

    dummy_subgraph = Subgraph(anchor="mock_anchor")
    mock_traverser.neuromorphic_search.return_value = dummy_subgraph

    args = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "query": "test query",
        "telemetry_severity": 0.8,
        "theta": 0.6,
        "decay": 0.9,
        "alpha": 1.1,
        "ticks": 3,
        "max_depth": 3,
        "anchor_top_k": 2,
    }

    resp = await handle_neuromorphic_search(mock_engine, args)
    resp_dict = json.loads(resp)

    assert resp_dict["anchor"] == "mock_anchor"
    mock_traverser.neuromorphic_search.assert_called_once_with(
        query="test query",
        namespace_id="00000000-0000-4000-8000-000000000001",
        max_depth=3,
        anchor_top_k=2,
        user_id=None,
        private=False,
        as_of=None,
        max_edges_per_node=512,
        edge_limit=None,
        edge_offset=0,
        telemetry_severity=0.8,
        theta=0.6,
        decay=0.9,
        alpha=1.1,
        ticks=3,
    )


@pytest.mark.asyncio
async def test_plugin_descriptors_and_admin_endpoint():
    """Verify get_plugin_descriptors and /api/admin/plugins support type & category filters."""
    import json

    from admin_server import api_admin_plugins
    from starlette.requests import Request

    from nce.plugins import get_plugin_descriptors, load_plugins

    class DummyMultiEnginePlugin:
        plugin_id = "dummy_multi"

        def get_plugin_descriptors(self):
            return [
                {
                    "id": "engine_alpha",
                    "name": "Alpha Engine",
                    "types": ["type-a", "type-shared"],
                    "categories": ["cat-one"],
                    "installed": True,
                    "enabled": True,
                },
                {
                    "id": "engine_beta",
                    "name": "Beta Engine",
                    "types": ["type-b", "type-shared"],
                    "categories": ["cat-two"],
                    "installed": True,
                    "enabled": False,
                },
            ]

    fake_ep = MagicMock()
    fake_ep.name = "dummy_multi"
    fake_ep.load.return_value = DummyMultiEnginePlugin

    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        load_plugins(force_reload=True)
        all_desc = get_plugin_descriptors()
        assert len(all_desc) == 2
        by_type = get_plugin_descriptors(type_filter="type-a")
        assert [d["id"] for d in by_type] == ["engine_alpha"]
        by_cat = get_plugin_descriptors(category_filter="cat-two")
        assert [d["id"] for d in by_cat] == ["engine_beta"]
        by_both = get_plugin_descriptors(type_filter="type-shared", category_filter="cat-one")
        assert [d["id"] for d in by_both] == ["engine_alpha"]

        req = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/admin/plugins",
                "query_string": b"type=type-shared&category=cat-two",
            }
        )
        resp = await api_admin_plugins(req)
        assert resp.status_code == 200
        payload = json.loads(resp.body.decode())
        assert payload["total"] == 1
        assert payload["plugins"][0]["id"] == "engine_beta"
        assert payload["facets"]["types"] == ["type-a", "type-b", "type-shared"]
        assert payload["facets"]["categories"] == ["cat-one", "cat-two"]
    load_plugins(force_reload=True)
