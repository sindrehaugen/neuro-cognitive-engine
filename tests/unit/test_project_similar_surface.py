"""Unit tests for Wave PJ-3 — Project Similar Projects Recall Surface.

Covers:
1. ToolSpec registration in TOOL_REGISTRY (cacheable=True, admin_only=False, mutation=False).
2. Tool declaration in mcp_stdio_tools.TOOLS with required arguments.
3. MCP handler handle_project_recall_similar (success, missing namespace, missing project_id).
4. REST endpoint api_project_recall_similar (GET, POST, {id} path param, 422, 503).
5. Route mount in build_admin_routes() for /api/project/similar and /api/project/{id}/similar.
6. Core do_recall_similar_projects parameter validation.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from nce import admin_state
from nce.admin_app import build_admin_routes
from nce.admin_handlers import project as project_handlers
from nce.mcp_errors import McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import CACHEABLE_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.project import mcp_handlers as project_mcp_handlers
from nce.vertical_modules.project.recall import do_recall_similar_projects

# ---------------------------------------------------------------------------
# 1. MCP Tool Registration & Flags
# ---------------------------------------------------------------------------


def test_project_recall_similar_tool_registered() -> None:
    """Wave PJ-3: project_recall_similar must be registered in TOOL_REGISTRY with correct flags."""
    assert "project_recall_similar" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["project_recall_similar"]
    assert spec.cacheable is True, "project_recall_similar must be cacheable"
    assert spec.admin_only is False, "project_recall_similar must NOT be admin_only"
    assert spec.mutation is False, "project_recall_similar must NOT be mutation"
    assert "project_recall_similar" in CACHEABLE_TOOLS


def test_project_recall_similar_stdio_schema() -> None:
    """Wave PJ-3: Tool declaration in mcp_stdio_tools.TOOLS matches contract."""
    tool = next((t for t in TOOLS if t.name == "project_recall_similar"), None)
    assert tool is not None, "project_recall_similar missing from mcp_stdio_tools.TOOLS"
    assert "Recall similar past slipped projects" in tool.description
    props = tool.inputSchema.get("properties", {})
    assert "namespace_id" in props
    assert "project_id" in props
    assert "description" in props
    assert "query" in props
    assert "top_k" in props
    assert set(tool.inputSchema.get("required", [])) == {"namespace_id", "project_id"}


# ---------------------------------------------------------------------------
# 2. MCP Handler Behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_project_recall_similar_success() -> None:
    """Wave PJ-3: MCP handler returns expected JSON with results."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    fake_results = [
        {"project": "PROJECT:101", "slip_reason": "Lead time extended", "similarity": 0.92},
        {"project": "PROJECT:102", "slip_reason": "Firmware conflict", "similarity": 0.85},
    ]

    with patch(
        "nce.vertical_modules.project.mcp_handlers.do_recall_similar_projects",
        new=AsyncMock(return_value=fake_results),
    ) as mock_do:
        raw_resp = await project_mcp_handlers.handle_project_recall_similar(
            mock_engine,
            {
                "namespace_id": ns_id,
                "project_id": "PROJECT:ACTIVE",
                "top_k": 3,
            },
        )
        mock_do.assert_awaited_once_with(
            mock_engine,
            {
                "namespace_id": ns_id,
                "project_id": "PROJECT:ACTIVE",
                "top_k": 3,
            },
        )

    parsed = json.loads(raw_resp)
    assert "results" in parsed
    assert len(parsed["results"]) == 2
    assert parsed["results"][0]["project"] == "PROJECT:101"
    assert parsed["results"][0]["similarity"] == 0.92


@pytest.mark.asyncio
async def test_handle_project_recall_similar_missing_namespace_raises() -> None:
    """Wave PJ-3: missing namespace_id raises McpError(-32602)."""
    mock_engine = MagicMock()
    with pytest.raises(McpError) as exc_info:
        await project_mcp_handlers.handle_project_recall_similar(
            mock_engine,
            {"project_id": "PROJECT:123"},
        )
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_project_recall_similar_missing_project_id_raises() -> None:
    """Wave PJ-3: missing project_id raises McpError(-32602)."""
    mock_engine = MagicMock()
    with pytest.raises(McpError) as exc_info:
        await project_mcp_handlers.handle_project_recall_similar(
            mock_engine,
            {"namespace_id": str(uuid.uuid4())},
        )
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 3. REST Routes Mount & Endpoint Behavior
# ---------------------------------------------------------------------------


def test_project_recall_similar_routes_mounted() -> None:
    """Wave PJ-3: /api/project/similar and /api/project/{id}/similar routes mounted."""
    routes = build_admin_routes()
    route_map: dict[str, Any] = {}
    for r in routes:
        if hasattr(r, "path") and hasattr(r, "endpoint"):
            route_map.setdefault(r.path, []).append(r)

    assert "/api/project/similar" in route_map
    similar_routes = route_map["/api/project/similar"]
    all_methods = set()
    for sr in similar_routes:
        all_methods.update(sr.methods)
        assert sr.endpoint == project_handlers.api_project_recall_similar
    assert "GET" in all_methods
    assert "POST" in all_methods

    assert "/api/project/{id}/similar" in route_map
    id_similar_routes = route_map["/api/project/{id}/similar"]
    id_methods = set()
    for ir in id_similar_routes:
        id_methods.update(ir.methods)
        assert ir.endpoint == project_handlers.api_project_recall_similar
    assert "GET" in id_methods


def _make_mock_request(
    method: str = "GET",
    query_params: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Request:
    req = MagicMock(spec=Request)
    req.method = method
    req.query_params = query_params or {}
    req.path_params = path_params or {}

    async def _json():
        if json_body is None:
            raise ValueError("No JSON body")
        return json_body

    req.json = AsyncMock(side_effect=_json)
    return req


@pytest.mark.asyncio
async def test_api_project_recall_similar_get_success() -> None:
    """Wave PJ-3: GET /api/project/similar returns 200 with status ok and results."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    fake_results = [{"project": "PROJECT:A", "slip_reason": "scope creep", "similarity": 0.9}]

    with (
        patch.object(admin_state, "engine", mock_engine),
        patch(
            "nce.admin_handlers.project.do_recall_similar_projects",
            new=AsyncMock(return_value=fake_results),
        ) as mock_do,
    ):
        req = _make_mock_request(
            method="GET",
            query_params={"namespace_id": ns_id, "project_id": "PROJECT:BASE", "top_k": "3"},
        )
        resp = await project_handlers.api_project_recall_similar(req)

        assert resp.status_code == 200
        data = json.loads(resp.body.decode("utf-8"))
        assert data["status"] == "ok"
        assert data["results"] == fake_results
        mock_do.assert_awaited_once_with(
            mock_engine,
            {
                "namespace_id": ns_id,
                "project_id": "PROJECT:BASE",
                "top_k": 3,
            },
        )


@pytest.mark.asyncio
async def test_api_project_recall_similar_path_id_success() -> None:
    """Wave PJ-3: GET /api/project/{id}/similar uses path parameter for project_id."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    fake_results = [{"project": "PROJECT:B", "slip_reason": "resource conflict", "similarity": 0.8}]

    with (
        patch.object(admin_state, "engine", mock_engine),
        patch(
            "nce.admin_handlers.project.do_recall_similar_projects",
            new=AsyncMock(return_value=fake_results),
        ) as mock_do,
    ):
        req = _make_mock_request(
            method="GET",
            query_params={"namespace_id": ns_id},
            path_params={"id": "PROJECT:FROM_PATH"},
        )
        resp = await project_handlers.api_project_recall_similar(req)

        assert resp.status_code == 200
        data = json.loads(resp.body.decode("utf-8"))
        assert data["status"] == "ok"
        mock_do.assert_awaited_once_with(
            mock_engine,
            {
                "namespace_id": ns_id,
                "project_id": "PROJECT:FROM_PATH",
            },
        )


@pytest.mark.asyncio
async def test_api_project_recall_similar_post_success() -> None:
    """Wave PJ-3: POST /api/project/similar returns 200 with JSON body."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    fake_results = [{"project": "PROJECT:C", "slip_reason": "design change", "similarity": 0.77}]

    with (
        patch.object(admin_state, "engine", mock_engine),
        patch(
            "nce.admin_handlers.project.do_recall_similar_projects",
            new=AsyncMock(return_value=fake_results),
        ) as mock_do,
    ):
        req = _make_mock_request(
            method="POST",
            json_body={
                "namespace_id": ns_id,
                "project_id": "PROJECT:BODY",
                "description": "Custom query description",
                "query": "video matrix",
                "top_k": 2,
            },
        )
        resp = await project_handlers.api_project_recall_similar(req)

        assert resp.status_code == 200
        data = json.loads(resp.body.decode("utf-8"))
        assert data["status"] == "ok"
        assert data["results"] == fake_results
        mock_do.assert_awaited_once_with(
            mock_engine,
            {
                "namespace_id": ns_id,
                "project_id": "PROJECT:BODY",
                "description": "Custom query description",
                "query": "video matrix",
                "top_k": 2,
            },
        )


@pytest.mark.asyncio
async def test_api_project_recall_similar_missing_namespace_returns_422() -> None:
    """Wave PJ-3: Missing namespace returns 422 for both GET and POST."""
    mock_engine = MagicMock()
    with patch.object(admin_state, "engine", mock_engine):
        # GET
        req_get = _make_mock_request(method="GET", query_params={"project_id": "P1"})
        resp_get = await project_handlers.api_project_recall_similar(req_get)
        assert resp_get.status_code == 422

        # POST
        req_post = _make_mock_request(method="POST", json_body={"project_id": "P1"})
        resp_post = await project_handlers.api_project_recall_similar(req_post)
        assert resp_post.status_code == 422


@pytest.mark.asyncio
async def test_api_project_recall_similar_missing_project_id_returns_422() -> None:
    """Wave PJ-3: Missing project_id returns 422."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    with patch.object(admin_state, "engine", mock_engine):
        req = _make_mock_request(method="GET", query_params={"namespace_id": ns_id})
        resp = await project_handlers.api_project_recall_similar(req)
        assert resp.status_code == 422
        data = json.loads(resp.body.decode("utf-8"))
        assert "Missing project_id" in data["error"]


@pytest.mark.asyncio
async def test_api_project_recall_similar_invalid_json_returns_422() -> None:
    """Wave PJ-3: Invalid JSON body returns 422."""
    mock_engine = MagicMock()
    with patch.object(admin_state, "engine", mock_engine):
        req = _make_mock_request(method="POST", json_body=None)
        resp = await project_handlers.api_project_recall_similar(req)
        assert resp.status_code == 422
        data = json.loads(resp.body.decode("utf-8"))
        assert "Invalid JSON body" in data["error"]


@pytest.mark.asyncio
async def test_api_project_recall_similar_invalid_top_k_returns_422() -> None:
    """Wave PJ-3: Non-integer top_k returns 422."""
    mock_engine = MagicMock()
    ns_id = str(uuid.uuid4())
    with patch.object(admin_state, "engine", mock_engine):
        req = _make_mock_request(
            method="GET",
            query_params={"namespace_id": ns_id, "project_id": "P1", "top_k": "not_an_int"},
        )
        resp = await project_handlers.api_project_recall_similar(req)
        assert resp.status_code == 422
        data = json.loads(resp.body.decode("utf-8"))
        assert "Invalid top_k" in data["error"]


@pytest.mark.asyncio
async def test_api_project_recall_similar_engine_disconnected_returns_503() -> None:
    """Wave PJ-3: Disconnected engine returns 503."""
    with patch.object(admin_state, "engine", None):
        req = _make_mock_request(method="GET", query_params={"namespace_id": str(uuid.uuid4())})
        resp = await project_handlers.api_project_recall_similar(req)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 4. Core do_recall_similar_projects Parameter Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_recall_similar_projects_missing_params_raises() -> None:
    """Wave PJ-3: do_recall_similar_projects raises ValueError on missing mandatory params."""
    mock_engine = MagicMock()
    with pytest.raises(ValueError, match="namespace_id.*required"):
        await do_recall_similar_projects(mock_engine, {})

    with pytest.raises(ValueError, match="project_id.*required"):
        await do_recall_similar_projects(mock_engine, {"namespace_id": str(uuid.uuid4())})
