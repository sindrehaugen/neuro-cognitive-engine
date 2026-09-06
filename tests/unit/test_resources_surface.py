"""
tests/unit/test_resources_surface.py
====================================
Acceptance test suite for Staff & Resources Engine surface completion (Wave RS-1).

Covers:
  1. Tool registrations in TOOL_REGISTRY (exact flags for mutations, admin_only, cacheable).
  2. MCP handlers for resources_create, resources_update, resources_get_resource, resources_list_resources.
  3. Input validation (require_namespace_id) and opt-in gate enforcement.
  4. REST route registration in admin app and status code handling (201, 200, 404, 409, 422, 503).
  5. bump_mcp_cache_generation calls for mutating REST endpoints.
  6. Contract-A node ownership enforcement (RESOURCE owned by resources engine).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_app import build_admin_routes
from nce.mcp_errors import MCP_SCOPE_FORBIDDEN, McpError
from nce.mcp_stdio_tools import TOOLS
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.resources.mcp_handlers import (
    handle_resources_create,
    handle_resources_get_resource,
    handle_resources_list_resources,
    handle_resources_update,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_RESOURCE_ID = "11111111-2222-3333-4444-555555555555"

_ALL_RESOURCES_TOOLS = (
    "resources_resolve_capacity",
    "resources_plan_allocation",
    "resources_detect_conflicts",
    "resources_forecast_demand",
    "resources_field_schedule",
    "resources_reserve",
    "resources_release",
    "resources_plan_material_flow",
    "resources_plan_travel",
    # Wave RS-1 additions
    "resources_create",
    "resources_update",
    "resources_get_resource",
    "resources_list_resources",
)


def _make_engine() -> MagicMock:
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.mongo_client = MagicMock()
    return engine


def _make_request(
    qp: dict[str, str] | None = None,
    path_params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.query_params = qp or {}
    req.path_params = path_params or {}
    if body is not None:
        req.json = AsyncMock(return_value=body)
    else:
        req.json = AsyncMock(side_effect=Exception("No JSON body"))
    return req


# =========================================================================
# 1. Registry & Flag Verification
# =========================================================================


def test_resources_tools_registered_in_tool_registry():
    """All 13 resources tools must be registered in TOOL_REGISTRY."""
    for tool_name in _ALL_RESOURCES_TOOLS:
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name!r} missing from TOOL_REGISTRY"


def test_resources_rs1_tool_flags():
    """Wave RS-1 tools must have exact flag configurations."""
    # resources_create: mutation=True, admin_only=True, cacheable=False
    assert "resources_create" in MUTATION_TOOLS
    assert "resources_create" in ADMIN_ONLY_TOOLS
    assert "resources_create" not in CACHEABLE_TOOLS

    # resources_update: mutation=True, admin_only=True, cacheable=False
    assert "resources_update" in MUTATION_TOOLS
    assert "resources_update" in ADMIN_ONLY_TOOLS
    assert "resources_update" not in CACHEABLE_TOOLS

    # resources_get_resource: mutation=False, admin_only=False, cacheable=True
    assert "resources_get_resource" not in MUTATION_TOOLS
    assert "resources_get_resource" not in ADMIN_ONLY_TOOLS
    assert "resources_get_resource" in CACHEABLE_TOOLS

    # resources_list_resources: mutation=False, admin_only=False, cacheable=True
    assert "resources_list_resources" not in MUTATION_TOOLS
    assert "resources_list_resources" not in ADMIN_ONLY_TOOLS
    assert "resources_list_resources" in CACHEABLE_TOOLS


def test_resources_rs1_tools_in_stdio_tools():
    """Wave RS-1 tools must be defined in mcp_stdio_tools.TOOLS."""
    names = {t.name for t in TOOLS}
    assert "resources_create" in names
    assert "resources_update" in names
    assert "resources_get_resource" in names
    assert "resources_list_resources" in names


# =========================================================================
# 2. MCP Handlers Parameter Validation & Execution
# =========================================================================


@pytest.mark.asyncio
async def test_mcp_handlers_missing_namespace_id():
    """All 4 RS-1 handlers raise McpError when namespace_id is absent."""
    engine = _make_engine()
    for handler in (
        handle_resources_create,
        handle_resources_update,
        handle_resources_get_resource,
        handle_resources_list_resources,
    ):
        with pytest.raises(McpError) as exc_info:
            await handler(engine, {})
        assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_mcp_handlers_opt_in_gate():
    """Handlers raise MCP_SCOPE_FORBIDDEN if resources module is disabled."""
    engine = _make_engine()
    params = {
        "namespace_id": _NAMESPACE_ID,
        "namespace_metadata": {"resources": {"enabled": False}},
    }
    for handler in (
        handle_resources_create,
        handle_resources_update,
        handle_resources_get_resource,
        handle_resources_list_resources,
    ):
        with pytest.raises(McpError) as exc_info:
            await handler(engine, params)
        assert exc_info.value.code == MCP_SCOPE_FORBIDDEN


@pytest.mark.asyncio
async def test_handle_resources_create_calls_core():
    engine = _make_engine()
    expected = {"resource_id": _RESOURCE_ID, "display_name": "Tech Van 1", "kind": "vehicle"}
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_create_resource",
        new=AsyncMock(return_value=expected),
    ) as mock_core:
        params = {
            "namespace_id": _NAMESPACE_ID,
            "display_name": "Tech Van 1",
            "kind": "vehicle",
        }
        res_json = await handle_resources_create(engine, params)
        assert json.loads(res_json) == expected
        mock_core.assert_awaited_once_with(engine, params)


@pytest.mark.asyncio
async def test_handle_resources_update_calls_core():
    engine = _make_engine()
    expected = {"resource_id": _RESOURCE_ID, "display_name": "Tech Van 1 Updated"}
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_update_resource",
        new=AsyncMock(return_value=expected),
    ) as mock_core:
        params = {
            "namespace_id": _NAMESPACE_ID,
            "resource_id": _RESOURCE_ID,
            "display_name": "Tech Van 1 Updated",
        }
        res_json = await handle_resources_update(engine, params)
        assert json.loads(res_json) == expected
        mock_core.assert_awaited_once_with(engine, params)


@pytest.mark.asyncio
async def test_handle_resources_get_resource_calls_core():
    engine = _make_engine()
    expected = {"resource_id": _RESOURCE_ID, "display_name": "Tech Van 1"}
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_get_resource",
        new=AsyncMock(return_value=expected),
    ) as mock_core:
        params = {
            "namespace_id": _NAMESPACE_ID,
            "resource_id": _RESOURCE_ID,
        }
        res_json = await handle_resources_get_resource(engine, params)
        assert json.loads(res_json) == expected
        mock_core.assert_awaited_once_with(engine, params)


@pytest.mark.asyncio
async def test_handle_resources_list_resources_calls_core():
    engine = _make_engine()
    expected = {"resources": [{"resource_id": _RESOURCE_ID}], "total": 1}
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_list_resources",
        new=AsyncMock(return_value=expected),
    ) as mock_core:
        params = {
            "namespace_id": _NAMESPACE_ID,
            "kind": "vehicle",
        }
        res_json = await handle_resources_list_resources(engine, params)
        assert json.loads(res_json) == expected
        mock_core.assert_awaited_once_with(engine, params)


# =========================================================================
# 3. REST Route Mounting & Execution
# =========================================================================


def test_rest_routes_mounted_in_admin_app():
    """Verify all 4 RS-1 REST routes are properly mounted."""
    from nce.admin_handlers.resources import (
        api_resources_create,
        api_resources_get,
        api_resources_list,
        api_resources_update,
    )

    routes = build_admin_routes()
    endpoint_map = {
        (r.path, r.endpoint): set(r.methods or []) for r in routes if hasattr(r, "endpoint")
    }

    assert ("/api/resources", api_resources_list) in endpoint_map
    assert "GET" in endpoint_map[("/api/resources", api_resources_list)]

    assert ("/api/resources", api_resources_create) in endpoint_map
    assert "POST" in endpoint_map[("/api/resources", api_resources_create)]

    assert ("/api/resources/{id}", api_resources_get) in endpoint_map
    assert "GET" in endpoint_map[("/api/resources/{id}", api_resources_get)]

    assert ("/api/resources/{id}", api_resources_update) in endpoint_map
    assert {"POST", "PATCH"}.issubset(endpoint_map[("/api/resources/{id}", api_resources_update)])


@pytest.mark.asyncio
async def test_rest_api_resources_create_status_201():
    from nce import admin_state
    from nce.admin_handlers.resources import api_resources_create

    engine = _make_engine()
    admin_state.engine = engine

    payload = {"namespace_id": _NAMESPACE_ID, "display_name": "Ladder 1", "kind": "equipment"}
    req = _make_request(body=payload)

    expected = {"resource_id": _RESOURCE_ID, **payload}
    with (
        patch(
            "nce.admin_handlers.resources.do_create_resource",
            new=AsyncMock(return_value=expected),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        resp = await api_resources_create(req)
        assert resp.status_code == 201
        assert json.loads(resp.body.decode()) == expected
        mock_bump.assert_awaited_once_with(engine, route="api_resources_create")


@pytest.mark.asyncio
async def test_rest_api_resources_update_status_200():
    from nce import admin_state
    from nce.admin_handlers.resources import api_resources_update

    engine = _make_engine()
    admin_state.engine = engine

    payload = {"namespace_id": _NAMESPACE_ID, "display_name": "Ladder 1 Deluxe"}
    req = _make_request(path_params={"id": _RESOURCE_ID}, body=payload)

    expected = {"resource_id": _RESOURCE_ID, **payload}
    with (
        patch(
            "nce.admin_handlers.resources.do_update_resource",
            new=AsyncMock(return_value=expected),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        resp = await api_resources_update(req)
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == expected
        mock_bump.assert_awaited_once_with(engine, route="api_resources_update")


@pytest.mark.asyncio
async def test_rest_api_resources_get_status_200():
    from nce import admin_state
    from nce.admin_handlers.resources import api_resources_get

    engine = _make_engine()
    admin_state.engine = engine

    req = _make_request(
        qp={"namespace_id": _NAMESPACE_ID},
        path_params={"id": _RESOURCE_ID},
    )

    expected = {"resource_id": _RESOURCE_ID, "display_name": "Ladder 1"}
    with patch(
        "nce.admin_handlers.resources.do_get_resource",
        new=AsyncMock(return_value=expected),
    ):
        resp = await api_resources_get(req)
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == expected


@pytest.mark.asyncio
async def test_rest_api_resources_list_status_200():
    from nce import admin_state
    from nce.admin_handlers.resources import api_resources_list

    engine = _make_engine()
    admin_state.engine = engine

    req = _make_request(
        qp={"namespace_id": _NAMESPACE_ID, "kind": "equipment"},
    )

    expected = {"resources": [{"resource_id": _RESOURCE_ID}], "total": 1}
    with patch(
        "nce.admin_handlers.resources.do_list_resources",
        new=AsyncMock(return_value=expected),
    ):
        resp = await api_resources_list(req)
        assert resp.status_code == 200
        assert json.loads(resp.body.decode()) == expected


@pytest.mark.asyncio
async def test_rest_error_responses():
    from nce import admin_state
    from nce.admin_handlers.resources import (
        api_resources_create,
        api_resources_get,
    )
    from nce.vertical_modules.resources._guard import (
        ResourceNotFoundError,
        ResourcesDisabledError,
        ResourceValidationError,
    )

    # 503 if engine is None
    admin_state.engine = None
    req = _make_request(body={"namespace_id": _NAMESPACE_ID})
    resp = await api_resources_create(req)
    assert resp.status_code == 503

    engine = _make_engine()
    admin_state.engine = engine

    # 404 on ResourceNotFoundError
    with patch(
        "nce.admin_handlers.resources.do_get_resource",
        side_effect=ResourceNotFoundError("Resource not found"),
    ):
        req = _make_request(
            qp={"namespace_id": _NAMESPACE_ID},
            path_params={"id": _RESOURCE_ID},
        )
        resp = await api_resources_get(req)
        assert resp.status_code == 404

    # 422 on ResourceValidationError
    with patch(
        "nce.admin_handlers.resources.do_create_resource",
        side_effect=ResourceValidationError("Invalid resource"),
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_resources_create(req)
        assert resp.status_code == 422

    # 409 on ResourcesDisabledError
    with patch(
        "nce.admin_handlers.resources.do_create_resource",
        side_effect=ResourcesDisabledError("Resources engine disabled"),
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID})
        resp = await api_resources_create(req)
        assert resp.status_code == 409


# =========================================================================
# 4. Contract-A Node Ownership Enforcement
# =========================================================================


@pytest.mark.asyncio
async def test_do_create_resource_enforces_assert_owner():
    """Verify do_create_resource enforces Contract-A assert_owner."""
    from nce.entity_resolution.ownership import OwnershipError
    from nce.vertical_modules.resources.registry import do_create_resource

    engine = _make_engine()
    conn = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            pass

    with (
        patch(
            "nce.vertical_modules.resources.registry.scoped_pg_session",
            return_value=_Ctx(),
        ),
        patch(
            "nce.vertical_modules.resources.registry.assert_owner",
            side_effect=OwnershipError(
                node_type="RESOURCE",
                writer_engine="wrong_engine",
                owner_engine="resources",
                transition=None,
            ),
        ),
    ):
        with pytest.raises(OwnershipError):
            await do_create_resource(
                engine,
                {"namespace_id": _NAMESPACE_ID, "display_name": "Tech Van 1", "kind": "vehicle"},
            )
