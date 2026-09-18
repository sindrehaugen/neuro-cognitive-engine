"""
tests/unit/test_system_design_fl_tree.py
========================================
Unit tests for System Design Wave C-1:
- Graph-backed tree operations on FUNCTIONAL_LOCATION nodes over kg_nodes/kg_edges.
- Re-parenting with cycle detection (move).
- Reversible merging with child reparenting and audit trail (merge).
- Duplicate-fold rules configuration and name normalization (fold_rules.py).
- Design-intent to as-built promotion.
- Lineage paths, ancestor chains, and child navigation.
- Corresponding MCP handlers and Admin HTTP routes.
- Cross-engine facade at nce/vertical_modules/fl_tree.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nce import admin_state
from nce.admin_handlers import system_design as admin_routes
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import TOOL_REGISTRY
from nce.vertical_modules import fl_tree as fl_tree_facade
from nce.vertical_modules.system_design.fl_tree import (
    CycleDetectedError,
    FLNodeNotFoundError,
    InvalidMoveError,
    MergeConflictError,
    _clear_mem_store,
    derive_fl_kind,
    get_fl_ancestors,
    get_fl_children,
    get_fl_node,
    get_fl_path,
    merge_fl_nodes,
    move_fl_node,
    promote_fl_node,
    search_fl_nodes,
    seed_mem_node,
)
from nce.vertical_modules.system_design.fold_rules import (
    FoldRules,
    evaluate_fl_match,
    normalize_name,
)
from nce.vertical_modules.system_design.mcp_handlers import (
    handle_system_design_get_fl_ancestors,
    handle_system_design_get_fl_children,
    handle_system_design_get_fl_path,
    handle_system_design_get_functional_location,
    handle_system_design_list_functional_locations,
    handle_system_design_merge_functional_locations,
    handle_system_design_move_functional_location,
    handle_system_design_promote_functional_location,
)

# ---------------------------------------------------------------------------
# Test fixtures & stubs
# ---------------------------------------------------------------------------

_TEST_NS = str(uuid.uuid4())


class _StubRequest:
    """Minimal request stub for Starlette/FastAPI-style handlers."""

    def __init__(
        self,
        body: Any = None,
        query_params: dict[str, Any] | None = None,
        path_params: dict[str, Any] | None = None,
    ) -> None:
        self._body = body or {}
        self.query_params = query_params or {}
        self.path_params = path_params or {}

    async def json(self) -> Any:
        return self._body


@pytest.fixture(autouse=True)
def _reset_mock_store():
    """Reset the in-memory mock store before and after each test."""
    _clear_mem_store()
    yield
    _clear_mem_store()


@pytest.fixture
def sample_tree():
    """Set up a standard 4-level test hierarchy:
    Campus (FL:CAMPUS)
      -> Building Alpha (FL:CAMPUS:BLDG-A)
           -> Floor 1 (FL:CAMPUS:BLDG-A:FLR-01)
                -> Room 101 (FL:CAMPUS:BLDG-A:FLR-01:RM-101)
                -> Room 102 (FL:CAMPUS:BLDG-A:FLR-01:RM-102)
           -> Floor 2 (FL:CAMPUS:BLDG-A:FLR-02)
      -> Building Beta (FL:CAMPUS:BLDG-B)
    """
    campus = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS",
        kind="site",
        change_origin="operator",
    )
    bldg_a = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-A",
        kind="building",
        change_origin="operator",
        parent_label="FL:CAMPUS",
    )
    bldg_b = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-B",
        kind="building",
        change_origin="sync",
        parent_label="FL:CAMPUS",
    )
    flr_1 = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-A:FLR-01",
        kind="floor",
        change_origin="operator",
        parent_label="FL:CAMPUS:BLDG-A",
    )
    flr_2 = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-A:FLR-02",
        kind="floor",
        change_origin="operator",
        parent_label="FL:CAMPUS:BLDG-A",
    )
    rm_101 = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-A:FLR-01:RM-101",
        kind="room",
        change_origin="sync",
        parent_label="FL:CAMPUS:BLDG-A:FLR-01",
    )
    rm_102 = seed_mem_node(
        _TEST_NS,
        label="FL:CAMPUS:BLDG-A:FLR-01:RM-102",
        kind="room",
        change_origin="operator",
        parent_label="FL:CAMPUS:BLDG-A:FLR-01",
    )

    return {
        "campus": campus,
        "bldg_a": bldg_a,
        "bldg_b": bldg_b,
        "flr_1": flr_1,
        "flr_2": flr_2,
        "rm_101": rm_101,
        "rm_102": rm_102,
    }


# ---------------------------------------------------------------------------
# Fold Rules Unit Tests
# ---------------------------------------------------------------------------


class TestFoldRules:
    """Test suite for duplicate-fold rules in fold_rules.py."""

    def test_normalize_name_punctuation_and_casing(self):
        assert normalize_name("  Meeting Room - 101  ") == "101"
        assert normalize_name("1. etasje") == "01"
        assert normalize_name("Campus (Main Site)") == "campus main site"
        assert normalize_name("") == ""

    def test_evaluate_exact_match(self):
        is_match, reason = evaluate_fl_match("Boardroom A", "boardroom a")
        assert is_match is True
        assert "Normalized exact match" in reason

    def test_evaluate_room_code_match(self):
        is_match, reason = evaluate_fl_match("Meeting Room 302", "Room 302")
        assert is_match is True
        assert "Normalized exact match" in reason or "Room code match" in reason

    def test_evaluate_mismatched_names(self):
        is_match, reason = evaluate_fl_match("Main Auditorium", "Server Room B")
        assert is_match is False
        assert "Non-matching" in reason

    def test_evaluate_kind_mismatch(self):
        is_match, reason = evaluate_fl_match(
            "Main Hall",
            "Main Hall",
            kind_a="building",
            kind_b="room",
        )
        assert is_match is False
        assert "Kind mismatch" in reason

    def test_custom_fold_rules(self):
        rules = FoldRules(case_sensitive=True)
        assert rules.case_sensitive is True


# ---------------------------------------------------------------------------
# FL Tree Graph Operations Unit Tests
# ---------------------------------------------------------------------------


class TestFLTreeOperations:
    """Test suite for graph tree retrieval, hierarchy navigation, and mutations."""

    @pytest.mark.asyncio
    async def test_get_fl_node_by_id_and_label(self, sample_tree):
        campus = sample_tree["campus"]
        by_id = await get_fl_node(None, _TEST_NS, campus["id"])
        assert by_id["id"] == campus["id"]
        assert by_id["label"] == "FL:CAMPUS"
        assert by_id["kind"] == "site"

        by_label = await get_fl_node(None, _TEST_NS, "FL:CAMPUS:BLDG-A")
        assert by_label["id"] == sample_tree["bldg_a"]["id"]
        assert by_label["label"] == "FL:CAMPUS:BLDG-A"

    @pytest.mark.asyncio
    async def test_get_fl_node_not_found(self):
        with pytest.raises(FLNodeNotFoundError):
            await get_fl_node(None, _TEST_NS, "FL:NON-EXISTENT")

    @pytest.mark.asyncio
    async def test_search_fl_nodes(self, sample_tree):
        results = await search_fl_nodes(None, _TEST_NS, q="RM-101")
        assert len(results) == 1
        assert results[0]["label"] == "FL:CAMPUS:BLDG-A:FLR-01:RM-101"

        bldg_results = await search_fl_nodes(None, _TEST_NS, kind="building")
        assert len(bldg_results) == 2

    @pytest.mark.asyncio
    async def test_get_fl_children(self, sample_tree):
        flr_1 = sample_tree["flr_1"]
        children = await get_fl_children(None, _TEST_NS, flr_1["label"])
        assert len(children) == 2
        labels = [c["label"] for c in children]
        assert "FL:CAMPUS:BLDG-A:FLR-01:RM-101" in labels
        assert "FL:CAMPUS:BLDG-A:FLR-01:RM-102" in labels

    @pytest.mark.asyncio
    async def test_get_fl_ancestors(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        ancestors = await get_fl_ancestors(None, _TEST_NS, rm_101["label"])
        ancestor_labels = [a["label"] for a in ancestors]
        # Ancestors returns immediate parent first up to root
        assert ancestor_labels == [
            "FL:CAMPUS:BLDG-A:FLR-01",
            "FL:CAMPUS:BLDG-A",
            "FL:CAMPUS",
        ]

    @pytest.mark.asyncio
    async def test_get_fl_path(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        path_info = await get_fl_path(None, _TEST_NS, rm_101["label"])
        assert "path_string" in path_info
        assert path_info["depth"] == 4
        assert len(path_info["path_nodes"]) == 4

    @pytest.mark.asyncio
    async def test_move_fl_node_success(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        flr_2 = sample_tree["flr_2"]

        res = await move_fl_node(None, _TEST_NS, rm_101["label"], flr_2["label"], actor="tester")
        assert res["moved"] is True

        # Verify new immediate parent in ancestors
        ancestors = await get_fl_ancestors(None, _TEST_NS, rm_101["label"])
        assert ancestors[0]["label"] == "FL:CAMPUS:BLDG-A:FLR-02"

    @pytest.mark.asyncio
    async def test_move_fl_node_cycle_detection(self, sample_tree):
        campus = sample_tree["campus"]
        rm_101 = sample_tree["rm_101"]

        # Attempting to move Campus under Room 101 would cause a cycle
        with pytest.raises(CycleDetectedError):
            await move_fl_node(None, _TEST_NS, campus["label"], rm_101["label"])

    @pytest.mark.asyncio
    async def test_move_fl_node_self_parent(self, sample_tree):
        bldg_a = sample_tree["bldg_a"]
        with pytest.raises(InvalidMoveError):
            await move_fl_node(None, _TEST_NS, bldg_a["label"], bldg_a["label"])

    @pytest.mark.asyncio
    async def test_merge_fl_nodes_success(self, sample_tree):
        dup_room = seed_mem_node(
            _TEST_NS,
            label="FL:CAMPUS:BLDG-A:FLR-01:RM-101-DUP",
            kind="room",
            change_origin="sync",
            parent_label="FL:CAMPUS:BLDG-A:FLR-01",
        )
        sub_rack = seed_mem_node(
            _TEST_NS,
            label="FL:CAMPUS:BLDG-A:FLR-01:RM-101-DUP:RACK-01",
            kind="rack",
            change_origin="sync",
            parent_label=dup_room["label"],
        )

        survivor = sample_tree["rm_101"]
        merge_res = await merge_fl_nodes(
            None,
            _TEST_NS,
            survivor_id_or_label=survivor["label"],
            absorbed_id_or_label=dup_room["label"],
            reversible=True,
            actor="lead_architect",
        )
        assert len(merge_res["reparented_children"]) == 1
        assert merge_res["absorbed"]["label"] == dup_room["label"]

        # Check that child's new immediate ancestor is survivor
        child_anc = await get_fl_ancestors(None, _TEST_NS, sub_rack["label"])
        assert child_anc[0]["label"] == survivor["label"]

    @pytest.mark.asyncio
    async def test_merge_fl_nodes_self_conflict(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        with pytest.raises(MergeConflictError):
            await merge_fl_nodes(None, _TEST_NS, rm_101["label"], rm_101["label"])

    @pytest.mark.asyncio
    async def test_promote_fl_node(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        assert rm_101["as_built"] is False

        res = await promote_fl_node(None, _TEST_NS, rm_101["label"], actor="site_tech")
        assert res["as_built"] is True

        updated = await get_fl_node(None, _TEST_NS, rm_101["label"])
        assert updated["as_built"] is True

    def test_derive_fl_kind(self):
        assert derive_fl_kind("FL:SITE", depth=1) == "site"
        assert derive_fl_kind("FL:TENANT:SITE:BLDG", depth=2) == "building"
        assert derive_fl_kind("FL:TENANT:SITE:BLDG:FLR", depth=3) == "floor"
        assert derive_fl_kind("FL:TENANT:SITE:BLDG:FLR:RM", depth=4) == "room"
        assert derive_fl_kind("FL:TENANT:SITE:BLDG:FLR:RM:DESK:1", depth=5) == "desk"
        assert derive_fl_kind("FL:VESSEL:BOAT1") == "vessel"


# ---------------------------------------------------------------------------
# MCP Handler Unit Tests
# ---------------------------------------------------------------------------


class TestFLTreeMCPHandlers:
    """Test suite for the 8 MCP handler endpoints."""

    @pytest.mark.asyncio
    async def test_mcp_list_and_get(self, sample_tree):
        mock_engine = MagicMock()
        mock_engine.pg_pool = None
        res_str = await handle_system_design_list_functional_locations(
            mock_engine, {"namespace_id": _TEST_NS, "kind": "building"}
        )
        res = json.loads(res_str)
        assert isinstance(res, list)
        assert len(res) == 2

        get_str = await handle_system_design_get_functional_location(
            mock_engine, {"namespace_id": _TEST_NS, "node_id": "FL:CAMPUS"}
        )
        get_res = json.loads(get_str)
        assert get_res["label"] == "FL:CAMPUS"

    @pytest.mark.asyncio
    async def test_mcp_children_ancestors_path(self, sample_tree):
        mock_engine = MagicMock()
        mock_engine.pg_pool = None
        rm_101 = sample_tree["rm_101"]

        ch_str = await handle_system_design_get_fl_children(
            mock_engine, {"namespace_id": _TEST_NS, "node_id": "FL:CAMPUS:BLDG-A"}
        )
        ch_res = json.loads(ch_str)
        assert isinstance(ch_res, list)
        assert len(ch_res) == 2

        anc_str = await handle_system_design_get_fl_ancestors(
            mock_engine, {"namespace_id": _TEST_NS, "node_id": rm_101["label"]}
        )
        anc_res = json.loads(anc_str)
        assert isinstance(anc_res, list)
        assert len(anc_res) == 3

        path_str = await handle_system_design_get_fl_path(
            mock_engine, {"namespace_id": _TEST_NS, "node_id": rm_101["label"]}
        )
        path_res = json.loads(path_str)
        assert path_res["depth"] == 4

    @pytest.mark.asyncio
    async def test_mcp_move_and_merge(self, sample_tree):
        mock_engine = MagicMock()
        mock_engine.pg_pool = None
        rm_101 = sample_tree["rm_101"]
        rm_102 = sample_tree["rm_102"]
        flr_2 = sample_tree["flr_2"]

        move_str = await handle_system_design_move_functional_location(
            mock_engine,
            {
                "namespace_id": _TEST_NS,
                "node_id": rm_102["label"],
                "new_parent_id": flr_2["label"],
            },
        )
        move_res = json.loads(move_str)
        assert move_res["moved"] is True

        merge_str = await handle_system_design_merge_functional_locations(
            mock_engine,
            {
                "namespace_id": _TEST_NS,
                "survivor_id": rm_101["label"],
                "absorbed_id": rm_102["label"],
            },
        )
        merge_res = json.loads(merge_str)
        assert "survivor" in merge_res

        # Cycle error over MCP
        campus = sample_tree["campus"]
        with pytest.raises(McpError) as excinfo:
            await handle_system_design_move_functional_location(
                mock_engine,
                {
                    "namespace_id": _TEST_NS,
                    "node_id": campus["label"],
                    "new_parent_id": rm_101["label"],
                },
            )
        assert excinfo.value.code == -32602

    @pytest.mark.asyncio
    async def test_mcp_promote(self, sample_tree):
        mock_engine = MagicMock()
        mock_engine.pg_pool = None
        rm_101 = sample_tree["rm_101"]

        prom_str = await handle_system_design_promote_functional_location(
            mock_engine,
            {"namespace_id": _TEST_NS, "node_id": rm_101["label"], "actor": "alice"},
        )
        prom_res = json.loads(prom_str)
        assert prom_res["as_built"] is True


# ---------------------------------------------------------------------------
# Admin REST HTTP Route Unit Tests
# ---------------------------------------------------------------------------


class TestFLTreeAdminRoutes:
    """Test suite for Admin REST API endpoints under /api/system-design/functional-locations."""

    @pytest.fixture(autouse=True)
    def _mock_admin_engine(self):
        fake_engine = MagicMock()
        fake_engine.pg_pool = None
        fake_engine.mcp_cache = None
        with patch.object(admin_state, "engine", fake_engine):
            yield

    @pytest.mark.asyncio
    async def test_admin_list_functional_locations(self, sample_tree):
        req = _StubRequest(query_params={"namespace_id": _TEST_NS, "kind": "building"})
        resp = await admin_routes.api_system_design_list_functional_locations(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["status"] == "ok"
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_admin_get_functional_location(self, sample_tree):
        req = _StubRequest(
            query_params={"namespace_id": _TEST_NS},
            path_params={"id": "FL:CAMPUS"},
        )
        resp = await admin_routes.api_system_design_get_functional_location(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["node"]["label"] == "FL:CAMPUS"

    @pytest.mark.asyncio
    async def test_admin_get_fl_children_and_path(self, sample_tree):
        bldg_a = sample_tree["bldg_a"]
        req = _StubRequest(
            query_params={"namespace_id": _TEST_NS},
            path_params={"id": bldg_a["label"]},
        )
        resp = await admin_routes.api_system_design_get_fl_children(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert len(body["children"]) == 2

        rm_101 = sample_tree["rm_101"]
        req_path = _StubRequest(
            query_params={"namespace_id": _TEST_NS},
            path_params={"id": rm_101["label"]},
        )
        resp_path = await admin_routes.api_system_design_get_fl_path(req_path)
        assert resp_path.status_code == 200
        body_path = json.loads(resp_path.body.decode("utf-8"))
        assert body_path["depth"] == 4

    @pytest.mark.asyncio
    async def test_admin_move_cycle_conflict_status(self, sample_tree):
        campus = sample_tree["campus"]
        rm_101 = sample_tree["rm_101"]
        req = _StubRequest(
            body={"namespace_id": _TEST_NS, "new_parent_id": rm_101["label"]},
            path_params={"id": campus["label"]},
        )
        resp = await admin_routes.api_system_design_move_functional_location(req)
        assert resp.status_code == 409
        body = json.loads(resp.body.decode("utf-8"))
        assert "cycle" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_admin_promote_functional_location(self, sample_tree):
        rm_101 = sample_tree["rm_101"]
        req = _StubRequest(
            body={"namespace_id": _TEST_NS, "actor": "admin"},
            path_params={"id": rm_101["label"]},
        )
        resp = await admin_routes.api_system_design_promote_functional_location(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["as_built"] is True


# ---------------------------------------------------------------------------
# Cross-Engine Facade & Predicate P10 Unit Test
# ---------------------------------------------------------------------------


class TestCrossEngineFacadeAndPredicateP10:
    """Verify that nce/vertical_modules/fl_tree.py satisfies P10 and mirrors implementation."""

    def test_facade_reexports_canonical_functions(self):
        assert hasattr(fl_tree_facade, "get_fl_node")
        assert hasattr(fl_tree_facade, "search_fl_nodes")
        assert hasattr(fl_tree_facade, "get_fl_children")
        assert hasattr(fl_tree_facade, "get_fl_ancestors")
        assert hasattr(fl_tree_facade, "get_fl_path")
        assert hasattr(fl_tree_facade, "move_fl_node")
        assert hasattr(fl_tree_facade, "merge_fl_nodes")
        assert hasattr(fl_tree_facade, "promote_fl_node")
        assert hasattr(fl_tree_facade, "derive_fl_kind")
        assert hasattr(fl_tree_facade, "FoldRules")
        assert hasattr(fl_tree_facade, "evaluate_fl_match")

    def test_all_8_tools_present_in_tool_registry_and_mcp_tools(self):
        tools = [
            "system_design_list_functional_locations",
            "system_design_get_functional_location",
            "system_design_get_fl_children",
            "system_design_get_fl_ancestors",
            "system_design_get_fl_path",
            "system_design_move_functional_location",
            "system_design_merge_functional_locations",
            "system_design_promote_functional_location",
        ]
        for t in tools:
            assert t in TOOL_REGISTRY, f"Missing {t} in TOOL_REGISTRY"
            assert any(tool.name == t for tool in TOOLS), f"Missing {t} in TOOLS"
