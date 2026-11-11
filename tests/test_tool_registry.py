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

_EXPECTED_STATIC_TOTAL = 71
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
        (
            "compile_working_memory",
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


@pytest.mark.asyncio
async def test_handle_compile_working_memory_success():
    """Verify handle_compile_working_memory routes through tool registry and serializes result."""
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.memory_mcp_handlers import handle_compile_working_memory

    mock_engine = MagicMock()
    mock_engine.default_namespace_id = "00000000-0000-4000-8000-000000000001"
    mock_engine.pg_pool = MagicMock()
    mock_engine.mongo_client = MagicMock()
    mock_engine.embedding_fn = AsyncMock(return_value=[0.1] * 768)

    with patch(
        "nce.memory_mcp_handlers.compile_working_memory", new_callable=AsyncMock
    ) as mock_cwm:
        mock_cwm.return_value = {
            "goal": "Verify system telemetry",
            "token_count": 120,
            "top_memories": [],
            "procedural_guardrails": [],
            "knowledge_subgraph": [],
            "contradiction_alerts": [],
            "provenance_citations": [],
        }
        args = {
            "goal": "Verify system telemetry",
            "namespace_id": "00000000-0000-4000-8000-000000000001",
            "max_tokens": 2000,
        }
        res_json = await handle_compile_working_memory(mock_engine, args)
        res = json.loads(res_json)
        assert res["goal"] == "Verify system telemetry"
        assert res["token_count"] == 120
        mock_cwm.assert_called_once()


@pytest.mark.asyncio
async def test_compile_working_memory_parallel_branches_and_token_budget():
    """Verify compile_working_memory executes parallel branches and bounds token budget."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.working_memory import compile_working_memory

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _scoped(_pool, _ns):
        yield mock_conn

    mock_engine = MagicMock()
    mock_engine.pg_pool = mock_pool
    mock_engine.default_namespace_id = "00000000-0000-4000-8000-000000000001"
    mock_engine.semantic_search = AsyncMock(
        return_value=[
            {
                "memory_id": "00000000-0000-4000-8000-000000000010",
                "payload_ref": "ref1",
                "score": 0.95,
                "raw_data": "Important configuration state details " * 20,
                "confidence_score": 0.9,
                "epistemic_warnings": [],
            }
        ]
    )

    with patch("nce.working_memory.scoped_pg_session", _scoped):
        result = await compile_working_memory(
            mock_engine,
            goal="Configure telemetry pipeline",
            namespace_id="00000000-0000-4000-8000-000000000001",
            max_tokens=500,
        )

        assert result["goal"] == "Configure telemetry pipeline"
        assert result["token_count"] <= 500
        assert len(result["provenance_citations"]) >= 1
        assert (
            result["provenance_citations"][0]["memory_id"] == "00000000-0000-4000-8000-000000000010"
        )


@pytest.mark.asyncio
async def test_procedural_memory_distillation_and_retrieval():
    """Verify distill_procedural_heuristics synthesizes rules and fetch retrieves them."""
    import json
    import uuid
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from nce.procedural_memory import (
        distill_procedural_heuristics,
    )

    ns = uuid.UUID("00000000-0000-4000-8000-000000000001")
    mock_conn = AsyncMock()

    aaq_rows = [
        {
            "id": uuid.uuid4(),
            "action_type": "upgrade_firmware",
            "target_system": "edge_switch_01",
            "status": "approved",
            "resolved_by": "sec_admin",
        }
    ]
    feedback_rows = [
        {
            "id": uuid.uuid4(),
            "params": json.dumps(
                {"decision": "allow", "engine": "procurement", "context_id": "ctx-1"}
            ),
        }
    ]
    contra_rows = [
        {
            "id": uuid.uuid4(),
            "resolution": "memory A superseded by manual review",
            "memory_a_id": uuid.uuid4(),
            "memory_b_id": uuid.uuid4(),
        }
    ]
    mock_conn.fetch.side_effect = [
        aaq_rows,
        feedback_rows,
        contra_rows,
        [],  # existing procedural_heuristics
    ]
    mock_conn.fetchrow.return_value = {
        "id": uuid.uuid4(),
        "namespace_id": ns,
        "memory_id": None,
        "rule_text": "ALLOW: Verified execution of upgrade_firmware on edge_switch_01.",
        "trigger_condition": "action_type == 'upgrade_firmware' and target_system == 'edge_switch_01'",
        "action_guidance": "Proceed with upgrade_firmware on edge_switch_01.",
        "source_type": "governed_decision",
        "source_refs": [],
        "confidence_weight": 0.85,
        "support_count": 1,
        "valid_from": datetime.now(timezone.utc),
        "valid_to": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    distilled = await distill_procedural_heuristics(mock_conn, ns)
    assert len(distilled) >= 1
    assert "upgrade_firmware" in distilled[0]["rule_text"]


def test_exact_identifier_extraction_and_epistemic_warnings():
    """Verify regex extraction of SKU, Norwegian orgnr, IPv4/IPv6."""
    from nce.semantic_search import extract_exact_identifiers

    query = "Find order for SKU-9942-X from org 987654321 at host 10.42.0.15 and 2001:db8::1"
    identifiers = extract_exact_identifiers(query)
    assert "SKU-9942-X" in identifiers
    assert "987654321" in identifiers
    assert "10.42.0.15" in identifiers
    assert "2001:db8::1" in identifiers


@pytest.mark.asyncio
async def test_semantic_search_exact_identifier_boost_and_epistemic_confidence():
    """Verify exact SKU boost to #1 and calibrated epistemic confidence scoring."""
    import uuid
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, MagicMock, patch

    from bson import ObjectId

    from nce.semantic_search import semantic_search

    ns = "00000000-0000-4000-8000-000000000001"
    mid_sku = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
    mid_other = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
    oid_sku = str(ObjectId())
    oid_other = str(ObjectId())

    # Candidate 'mid_other' had higher initial DB score (0.90) than 'mid_sku' (0.40)
    pg_rows = [
        {
            "payload_ref": oid_other,
            "memory_id": mid_other,
            "final_score": 0.90,
            "raw_salience": 1.0,
        },
        {"payload_ref": oid_sku, "memory_id": mid_sku, "final_score": 0.40, "raw_salience": 1.0},
    ]

    mem_meta_rows = [
        {
            "id": mid_sku,
            "payload_ref": oid_sku,
            "wrapped_dek": None,
            "valid_to": None,
            "assertion_type": "fact",
            "derivation_depth": 0,
            "metadata": {},
        },
        {
            "id": mid_other,
            "payload_ref": oid_other,
            "wrapped_dek": None,
            "valid_to": datetime.now(timezone.utc) - timedelta(days=1),  # ATMS invalidated
            "assertion_type": "opinion",  # Low provenance tier
            "derivation_depth": 2,  # High derivation depth
            "metadata": {"provenance_tier": "unverified"},
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    # 1st fetch: ranking query; 2nd fetch: memories table meta; 3rd fetch: contradictions
    mock_conn.fetch.side_effect = [
        pg_rows,
        mem_meta_rows,
        [{"memory_a_id": mid_other, "memory_b_id": uuid.uuid4()}],  # mid_other in contradiction
    ]

    @asynccontextmanager
    async def _scoped(_pool, _namespace_id):
        yield mock_conn

    # Mongo docs: mid_sku has the matching SKU
    async def _find(query, projection=None):
        ids = query.get("_id", {}).get("$in", [])
        for oid in ids:
            if str(oid) == oid_sku:
                yield {
                    "_id": ObjectId(oid_sku),
                    "raw_data": "Warehouse item record: SKU-9942-X active.",
                }
            elif str(oid) == oid_other:
                yield {
                    "_id": ObjectId(oid_other),
                    "raw_data": "General overview without identifier.",
                }

    episodes = MagicMock()
    episodes.find = MagicMock(side_effect=_find)
    mongo = MagicMock()
    mongo.memory_archive.episodes = episodes

    async def _embed(_q: str):
        return [0.0] * 768

    with patch("nce.semantic_search.scoped_pg_session", _scoped):
        with patch(
            "nce.semantic_search.asyncio.create_task",
            side_effect=lambda coro: (coro.close(), MagicMock())[1],
        ):
            results = await semantic_search(
                pg_pool=MagicMock(),
                mongo_client=mongo,
                embedding_fn=_embed,
                query="Find part SKU-9942-X",
                namespace_id=ns,
                agent_id="test-agent",
            )

    assert len(results) == 2
    # Exact SKU candidate should be boosted to rank #1 despite lower vector score
    assert results[0]["memory_id"] == mid_sku
    assert results[0]["confidence_score"] > 0.8
    assert results[0]["epistemic_warnings"] == []

    # Other candidate should be rank #2 and carry epistemic warnings + decayed confidence
    assert results[1]["memory_id"] == mid_other
    assert results[1]["confidence_score"] < 0.25
    assert "atms_invalidated" in results[1]["epistemic_warnings"]
    assert "unresolved_contradiction" in results[1]["epistemic_warnings"]
    assert "derived_inference_depth_2" in results[1]["epistemic_warnings"]
    assert "low_provenance_tier" in results[1]["epistemic_warnings"]
