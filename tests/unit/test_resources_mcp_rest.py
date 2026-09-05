"""
Unit tests for Module 15: Staff & Resources Engine MCP tool handlers & Admin REST handlers.
Verifies:
  - All 9 MCP tool handlers in nce.vertical_modules.resources.mcp_handlers
  - All 10 REST endpoints in nce.admin_handlers.resources
  - Error translation (422 for missing/invalid namespace_id or validation error, 404, 409)
  - MCP cache generation bumping on mutating endpoints
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers.resources import (
    api_resources_capacity_pulse,
    api_resources_detect_conflicts,
    api_resources_field_schedule,
    api_resources_forecast_demand,
    api_resources_plan_allocation,
    api_resources_plan_material_flow,
    api_resources_plan_travel,
    api_resources_release,
    api_resources_reserve,
    api_resources_resolve_capacity,
)
from nce.mcp_errors import McpError
from nce.vertical_modules.resources.mcp_handlers import (
    handle_resources_detect_conflicts,
    handle_resources_field_schedule,
    handle_resources_forecast_demand,
    handle_resources_plan_allocation,
    handle_resources_plan_material_flow,
    handle_resources_plan_travel,
    handle_resources_release,
    handle_resources_reserve,
    handle_resources_resolve_capacity,
)

_VALID_NS = "11111111-2222-3333-4444-555555555555"
_MALFORMED_NS = "not-a-uuid"


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# ==============================================================================
# MCP Tool Handlers Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_mcp_resources_capacity() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_resolve_capacity",
        new=AsyncMock(return_value={"capacity": "ok"}),
    ) as mock_fn:
        res = await handle_resources_resolve_capacity(
            mock_engine, {"namespace_id": _VALID_NS, "kind": "technician"}
        )
        assert json.loads(res) == {"capacity": "ok"}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_reserve() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_reserve",
        new=AsyncMock(return_value={"reservation_id": "r-123"}),
    ) as mock_fn:
        res = await handle_resources_reserve(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"reservation_id": "r-123"}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_release() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_release",
        new=AsyncMock(return_value={"released": True}),
    ) as mock_fn:
        res = await handle_resources_release(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"released": True}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_conflicts() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_detect_conflicts",
        new=AsyncMock(return_value={"conflicts": []}),
    ) as mock_fn:
        res = await handle_resources_detect_conflicts(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"conflicts": []}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_plan_allocation() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_plan_allocation",
        new=AsyncMock(return_value={"ranked_candidates": []}),
    ) as mock_fn:
        res = await handle_resources_plan_allocation(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"ranked_candidates": []}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_material_flow() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_plan_material_flow",
        new=AsyncMock(return_value={"manifest": {}}),
    ) as mock_fn:
        res = await handle_resources_plan_material_flow(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"manifest": {}}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_travel() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_plan_travel",
        new=AsyncMock(return_value={"travel_legs": []}),
    ) as mock_fn:
        res = await handle_resources_plan_travel(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"travel_legs": []}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_forecast() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_forecast_demand",
        new=AsyncMock(return_value={"forecast": {}}),
    ) as mock_fn:
        res = await handle_resources_forecast_demand(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"forecast": {}}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_resources_field_schedule() -> None:
    mock_engine = MagicMock()
    with patch(
        "nce.vertical_modules.resources.mcp_handlers.do_field_schedule",
        new=AsyncMock(return_value={"assignments": []}),
    ) as mock_fn:
        res = await handle_resources_field_schedule(mock_engine, {"namespace_id": _VALID_NS})
        assert json.loads(res) == {"assignments": []}
        mock_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_requires_namespace_id() -> None:
    mock_engine = MagicMock()
    with pytest.raises(McpError):
        await handle_resources_resolve_capacity(mock_engine, {})


# ==============================================================================
# Admin REST Endpoints Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_rest_missing_namespace_id() -> None:
    req = _make_request(query={})
    with patch("nce.admin_handlers.resources.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        resp = await api_resources_resolve_capacity(req)
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert "error" in body


@pytest.mark.asyncio
async def test_rest_malformed_namespace_id() -> None:
    req = _make_request(query={"namespace_id": _MALFORMED_NS})
    with patch("nce.admin_handlers.resources.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        resp = await api_resources_resolve_capacity(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rest_resolve_capacity_ok() -> None:
    req = _make_request(query={"namespace_id": _VALID_NS, "kind": "technician"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_resolve_capacity",
            new=AsyncMock(return_value={"status": "ok"}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_resolve_capacity(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"status": "ok"}


@pytest.mark.asyncio
async def test_rest_plan_allocation_ok() -> None:
    req = _make_request(body={"namespace_id": _VALID_NS, "role_required": "lead_installer"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_plan_allocation",
            new=AsyncMock(return_value={"ranked_candidates": []}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_plan_allocation(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"ranked_candidates": []}


@pytest.mark.asyncio
async def test_rest_reserve_bumps_cache() -> None:
    req = _make_request(body={"namespace_id": _VALID_NS, "resource_id": "r1"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_reserve",
            new=AsyncMock(return_value={"allocation": {"id": "a1"}}),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_reserve(req)
        assert resp.status_code == 201
        mock_bump.assert_awaited_once_with(mock_state.engine, route="api_resources_reserve")


@pytest.mark.asyncio
async def test_rest_release_bumps_cache() -> None:
    req = _make_request(body={"namespace_id": _VALID_NS, "allocation_id": "a1"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_release",
            new=AsyncMock(return_value={"status": "released"}),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_release(req)
        assert resp.status_code == 200
        mock_bump.assert_awaited_once_with(mock_state.engine, route="api_resources_release")


@pytest.mark.asyncio
async def test_rest_conflicts_ok() -> None:
    req = _make_request(query={"namespace_id": _VALID_NS})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_detect_conflicts",
            new=AsyncMock(return_value={"conflicts": []}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_detect_conflicts(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"conflicts": []}


@pytest.mark.asyncio
async def test_rest_material_flow_bumps_cache() -> None:
    req = _make_request(body={"namespace_id": _VALID_NS, "action": "staging"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_plan_material_flow",
            new=AsyncMock(return_value={"material_flow": "ok"}),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_plan_material_flow(req)
        assert resp.status_code == 200
        mock_bump.assert_awaited_once_with(
            mock_state.engine, route="api_resources_plan_material_flow"
        )


@pytest.mark.asyncio
async def test_rest_travel_bumps_cache() -> None:
    req = _make_request(body={"namespace_id": _VALID_NS, "project_id": "p1", "action": "book"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_plan_travel",
            new=AsyncMock(return_value={"travel_plan": "ok"}),
        ),
        patch(
            "nce.admin_handlers.resources.bump_mcp_cache_generation", new=AsyncMock()
        ) as mock_bump,
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_plan_travel(req)
        assert resp.status_code == 200
        mock_bump.assert_awaited_once_with(mock_state.engine, route="api_resources_plan_travel")


@pytest.mark.asyncio
async def test_rest_field_schedule_ok() -> None:
    req = _make_request(query={"namespace_id": _VALID_NS, "resource_id": "r1"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_field_schedule",
            new=AsyncMock(return_value={"assignments": []}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_field_schedule(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"assignments": []}


@pytest.mark.asyncio
async def test_rest_forecast_ok() -> None:
    req = _make_request(query={"namespace_id": _VALID_NS, "horizon_days": "14"})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.do_forecast_demand",
            new=AsyncMock(return_value={"supply_hours": 100}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_forecast_demand(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"supply_hours": 100}


@pytest.mark.asyncio
async def test_rest_capacity_pulse_ok() -> None:
    req = _make_request(query={"namespace_id": _VALID_NS})
    with (
        patch("nce.admin_handlers.resources.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.resources.get_morning_brief_capacity_pulse",
            new=AsyncMock(return_value={"capacity_health": "optimal"}),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_resources_capacity_pulse(req)
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"capacity_health": "optimal"}


@pytest.mark.asyncio
async def test_rest_error_translation() -> None:
    from nce.vertical_modules.resources._guard import (
        ResourceConcurrencyError,
        ResourceNotFoundError,
        ResourceValidationError,
    )

    req = _make_request(body={"namespace_id": _VALID_NS})
    with patch("nce.admin_handlers.resources.admin_state") as mock_state:
        mock_state.engine = MagicMock()

        # 404
        with patch(
            "nce.admin_handlers.resources.do_reserve",
            new=AsyncMock(side_effect=ResourceNotFoundError("Missing")),
        ):
            resp = await api_resources_reserve(req)
            assert resp.status_code == 404

        # 409
        with patch(
            "nce.admin_handlers.resources.do_reserve",
            new=AsyncMock(side_effect=ResourceConcurrencyError("Conflict")),
        ):
            resp = await api_resources_reserve(req)
            assert resp.status_code == 409

        # 422
        with patch(
            "nce.admin_handlers.resources.do_reserve",
            new=AsyncMock(side_effect=ResourceValidationError("Invalid")),
        ):
            resp = await api_resources_reserve(req)
            assert resp.status_code == 422
