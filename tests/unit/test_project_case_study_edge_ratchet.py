"""
tests/unit/test_project_case_study_edge_ratchet.py
===================================================
Ratchet test suite for Wave C-PJ2: Project Case-Study Edge & Marketing
Candidate Discovery.

Validates:
1. MCP tool registration (project_generate_case_study_edge) in TOOL_REGISTRY,
   MUTATION_TOOLS, and ADMIN_ONLY_TOOLS.
2. Tool schema in mcp_stdio_tools.TOOLS.
3. Removal of do_generate_case_study_edge from internal-cores.json (shrink-only).
4. REST route POST /api/project/{id}/case-study mounted and behaving per contract.
5. do_advance_phase automatically invokes do_generate_case_study_edge on G6 advance.
6. do_generate_case_study_edge creates CASE_STUDY node and generates edge when G6,
   refuses when in-flight (e.g. G5).
7. Marketing do_find_case_study_candidates discovers delivered projects with
   case-study graph edges and grounds evidence links.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.project.advance import do_advance_phase
from nce.vertical_modules.project.case_study import do_generate_case_study_edge

# ---------------------------------------------------------------------------
# 1. MCP Tool Registration & Schema Ratchet
# ---------------------------------------------------------------------------


def test_project_generate_case_study_edge_registered() -> None:
    """Wave C-PJ2: project_generate_case_study_edge registered with correct flags."""
    assert "project_generate_case_study_edge" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["project_generate_case_study_edge"]
    assert spec.mutation is True
    assert spec.admin_only is True
    assert spec.cacheable is False
    assert "project_generate_case_study_edge" in MUTATION_TOOLS
    assert "project_generate_case_study_edge" in ADMIN_ONLY_TOOLS
    assert "project_generate_case_study_edge" not in CACHEABLE_TOOLS


def test_project_generate_case_study_edge_schema_in_stdio_tools() -> None:
    """Wave C-PJ2: project_generate_case_study_edge schema is declared."""
    by_name = {t.name: t for t in TOOLS}
    assert "project_generate_case_study_edge" in by_name
    tool = by_name["project_generate_case_study_edge"]
    schema = tool.inputSchema
    assert schema["type"] == "object"
    assert "namespace_id" in schema["required"]
    assert "project_id" in schema["required"]
    assert "confidence" in schema["properties"]


def test_do_generate_case_study_edge_removed_from_internal_cores() -> None:
    """Wave C-PJ2: do_generate_case_study_edge must be pruned from internal-cores.json."""
    repo_root = Path(__file__).resolve().parents[2]
    cores_path = repo_root / "nce" / "config_data" / "internal-cores.json"
    with open(cores_path, encoding="utf-8") as f:
        data = json.load(f)
    allowlist = set(data.keys()) if isinstance(data, dict) else set(data)
    assert (
        "nce/vertical_modules/project/case_study.py::do_generate_case_study_edge" not in allowlist
    )


# ---------------------------------------------------------------------------
# 2. REST Route Verification
# ---------------------------------------------------------------------------


def test_case_study_route_mounted_in_admin_app() -> None:
    """Wave C-PJ2: /api/project/{id}/case-study mounted in admin router."""
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/project/{id}/case-study" in paths


@pytest.mark.asyncio
async def test_case_study_route_contract() -> None:
    """Wave C-PJ2: REST route maps in_flight to 409 and success to 200."""
    from nce.admin_handlers.project import api_project_generate_case_study

    ns_id = str(uuid.uuid4())
    proj_id = "PROJECT:Q100"

    def _make_req(body: dict[str, Any], path_id: str = proj_id) -> MagicMock:
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        req.path_params = {"id": path_id}
        req.query_params = {}
        return req

    # 503 when engine not connected
    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = None
        req = _make_req({"namespace_id": ns_id})
        resp = await api_project_generate_case_study(req)
        assert resp.status_code == 503

    # 422 when namespace_id missing
    with patch("nce.admin_handlers.project.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        req = _make_req({})
        resp = await api_project_generate_case_study(req)
        assert resp.status_code == 422

    # 409 when in_flight
    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_generate_case_study_edge",
            new=AsyncMock(return_value={"ok": False, "reason": "in_flight", "current_phase": "G4"}),
        ),
        patch("nce.admin_handlers.project.bump_mcp_cache_generation", new=AsyncMock()),
    ):
        mock_state.engine = MagicMock()
        req = _make_req({"namespace_id": ns_id})
        resp = await api_project_generate_case_study(req)
        assert resp.status_code == 409
        body = json.loads(resp.body)
        assert body["reason"] == "in_flight"

    # 200 when ok
    with (
        patch("nce.admin_handlers.project.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.project.do_generate_case_study_edge",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "case_study_label": "CASE_STUDY:Q100",
                    "edge": "PROJECT:Q100 -[generates]-> CASE_STUDY:Q100",
                }
            ),
        ),
        patch("nce.admin_handlers.project.bump_mcp_cache_generation", new=AsyncMock()),
    ):
        mock_state.engine = MagicMock()
        req = _make_req({"namespace_id": ns_id, "confidence": 0.95})
        resp = await api_project_generate_case_study(req)
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["case_study_label"] == "CASE_STUDY:Q100"


# ---------------------------------------------------------------------------
# 3. Domain Core: Phase Transition Seam (G6 Advance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_advance_phase_invokes_generate_case_study_edge_on_g6() -> None:
    """Wave C-PJ2: Transitioning to G6 calls do_generate_case_study_edge."""
    ns_id = str(uuid.uuid4())
    proj_label = "PROJECT:Q-G6-001"

    mock_conn = AsyncMock()

    async def _fetchrow_router(query: str, *args: Any) -> Any:
        if "kg_edges" in query:
            return {"object_label": "GATE:Q-G6-001:G5"}
        return None

    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_router)
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.transaction = MagicMock()
    mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.project.advance.scoped_pg_session",
            side_effect=_mock_scoped_session,
        ),
        patch("nce.vertical_modules.project.advance.assert_owner", new=AsyncMock()),
        patch("nce.vertical_modules.project.advance.emit_graph_write", new=AsyncMock()),
        patch("nce.vertical_modules.project.advance.append_event", new=AsyncMock()),
        patch(
            "nce.vertical_modules.project.case_study.do_generate_case_study_edge",
            new=AsyncMock(return_value={"ok": True, "case_study_label": "CASE_STUDY:Q-G6-001"}),
        ) as mock_gen_cs,
    ):
        res = await do_advance_phase(
            engine,
            {
                "namespace_id": ns_id,
                "project_id": proj_label,
                "target_phase": "G6",
                "actor": "lead@example.com",
                "criteria_met": [
                    "all_tests_passed",
                    "customer_sign_off",
                    "as_built_documented",
                    "handover_to_support_done",
                ],
            },
        )

    assert res["ok"] is True
    assert res["phase"] == "G6"
    assert mock_gen_cs.call_count == 1
    call_args = mock_gen_cs.call_args[0]
    assert str(call_args[1]["namespace_id"]) == ns_id
    assert call_args[1]["project_id"] == proj_label


# ---------------------------------------------------------------------------
# 4. do_generate_case_study_edge Core Logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_generate_case_study_edge_requires_g6() -> None:
    """Wave C-PJ2: in-flight projects return reason='in_flight'."""
    ns_id = str(uuid.uuid4())
    proj_label = "PROJECT:Q-G4-TEST"

    mock_conn = AsyncMock()

    async def _fetchval_router(query: str, *args: Any) -> Any:
        if "FROM kg_nodes" in query:
            return 1
        return None

    async def _fetchrow_router(query: str, *args: Any) -> Any:
        if "kg_edges" in query:
            return {"object_label": "GATE:Q-G4-TEST:G4"}
        return None

    mock_conn.fetchval = AsyncMock(side_effect=_fetchval_router)
    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_router)

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with patch(
        "nce.vertical_modules.project.case_study.scoped_pg_session",
        side_effect=_mock_scoped_session,
    ):
        res = await do_generate_case_study_edge(
            engine,
            {
                "namespace_id": ns_id,
                "project_id": proj_label,
            },
        )

    assert res["ok"] is False
    assert res["reason"] == "in_flight"
    assert res["current_phase"] == "G4"


@pytest.mark.asyncio
async def test_do_generate_case_study_edge_terminal_g6_creates_edge() -> None:
    """Wave C-PJ2: G6 project creates CASE_STUDY node and generates edge."""
    ns_id = str(uuid.uuid4())
    proj_label = "PROJECT:Q-TERMINAL-99"

    mock_conn = AsyncMock()

    async def _fetchval_router(query: str, *args: Any) -> Any:
        if "FROM kg_nodes" in query:
            return 1
        return None

    async def _fetchrow_router(query: str, *args: Any) -> Any:
        if "kg_edges" in query:
            return {"object_label": "GATE:Q-TERMINAL-99:G6"}
        return None

    mock_conn.fetchval = AsyncMock(side_effect=_fetchval_router)
    mock_conn.fetchrow = AsyncMock(side_effect=_fetchrow_router)
    mock_conn.execute = AsyncMock(return_value="INSERT 1")

    @asynccontextmanager
    async def _mock_scoped_session(pool: Any, ns_uuid: Any) -> Any:
        yield mock_conn

    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with (
        patch(
            "nce.vertical_modules.project.case_study.scoped_pg_session",
            side_effect=_mock_scoped_session,
        ),
        patch("nce.vertical_modules.project.case_study.assert_owner", new=AsyncMock()),
        patch("nce.vertical_modules.project.case_study.emit_graph_write", new=AsyncMock()),
    ):
        res = await do_generate_case_study_edge(
            engine,
            {
                "namespace_id": ns_id,
                "project_id": proj_label,
                "confidence": 0.95,
            },
        )

    assert res["ok"] is True
    assert res["case_study_label"] == "CASE_STUDY:Q-TERMINAL-99"
    assert "generates" in res["edge"]


# ---------------------------------------------------------------------------
# 5. Marketing Candidate Discovery with Graph Edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marketing_candidates_queries_case_study_edge() -> None:
    """Wave C-PJ2: Marketing candidate discovery reads real generates edge."""
    ns_id = str(uuid.uuid4())
    proj_uuid = str(uuid.uuid4())

    mock_conn = AsyncMock()

    # Return a delivered project with a generates -> CASE_STUDY edge
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "id": proj_uuid,
                "label": "PROJECT:Q-DELIVERED-001",
                "entity_type": "PROJECT_PROJECT",
                "created_at": "2026-09-01T10:00:00Z",
                "case_study_label": "CASE_STUDY:Q-DELIVERED-001",
                "edge_confidence": 0.95,
            }
        ]
    )

    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    engine = MagicMock()
    engine.pg_pool = mock_pool

    res = await do_find_case_study_candidates(
        engine,
        {
            "namespace_id": ns_id,
            "min_outcome_score": 7.5,
        },
    )

    assert res["total_count"] == 1
    cand = res["candidates"][0]
    assert cand["project_id"] == proj_uuid
    assert cand["case_study_label"] == "CASE_STUDY:Q-DELIVERED-001"
    assert cand["outcome_score"] == 9.5
    assert "CASE_STUDY:Q-DELIVERED-001" in cand["evidence_node_ids"]
    assert proj_uuid in cand["evidence_node_ids"]
