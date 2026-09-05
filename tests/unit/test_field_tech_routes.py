"""
tests/unit/test_field_tech_routes.py
====================================
Unit tests for Module 12 (Field Tech Engine) admin HTTP REST surface:
  - Route mounting in admin_app.py for all 12 routes
  - Engine disconnected handling (503)
  - Opt-in guard enforcement (409 on FieldTechDisabledError)
  - Missing parameter and invalid payload validation (422)
  - Happy paths for all 12 route handlers
  - Mutating route MCP cache generation bumping
  - Domain refusal mappings (404 on not found, 409 on invalid transition / checklist incomplete)
  - Partner view redaction assertion

Pure unit tests — no live database or Redis required.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce import admin_state
from nce.admin_app import build_admin_routes
from nce.admin_handlers import field_tech as ft_mod
from nce.vertical_modules.field_tech._guard import FieldTechDisabledError
from nce.vertical_modules.field_tech.checklist import (
    ChecklistIncompleteError,
    ChecklistNotFoundError,
)
from nce.vertical_modules.field_tech.work_orders import (
    WorkOrderInvalidTransitionError,
    WorkOrderNotFoundError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_WO_ID = str(uuid4())
_CHECKLIST_ID = str(uuid4())


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette Request mock."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body if body is not None else {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


@pytest.fixture(autouse=True)
def _setup_engine():
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    engine.redis_client = MagicMock()
    with patch.object(admin_state, "engine", engine):
        yield engine


# ---------------------------------------------------------------------------
# 1. Route Mounting
# ---------------------------------------------------------------------------


def test_field_tech_routes_mounted_in_admin_app() -> None:
    routes = build_admin_routes()
    route_table = {
        (r.path, tuple(sorted(m for m in (r.methods or []) if m != "HEAD")))
        for r in routes
        if hasattr(r, "methods")
    }

    assert ("/api/field-tech/dispatch", ("POST",)) in route_table
    assert ("/api/field-tech/work-orders", ("POST",)) in route_table
    assert ("/api/field-tech/work-orders", ("GET",)) in route_table
    assert ("/api/field-tech/work-orders/{id}", ("GET",)) in route_table
    assert ("/api/field-tech/work-orders/{id}/assign", ("POST",)) in route_table
    assert ("/api/field-tech/checklists", ("POST",)) in route_table
    assert ("/api/field-tech/scans", ("POST",)) in route_table
    assert ("/api/field-tech/time-entries", ("POST",)) in route_table
    assert ("/api/field-tech/photos", ("POST",)) in route_table
    assert ("/api/field-tech/sync", ("POST",)) in route_table
    assert ("/api/field-tech/outcomes", ("POST",)) in route_table
    assert ("/api/field-tech/partner-view", ("GET",)) in route_table


# ---------------------------------------------------------------------------
# 2. Engine Disconnected (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_disconnected_returns_503() -> None:
    with patch.object(admin_state, "engine", None):
        req = _make_request()
        resp = await ft_mod.api_field_tech_dispatch(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_create_work_order(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_query_work_orders(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_work_order(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_assign(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_complete_checklist(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_scan_serial(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_log_time(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_attach_photo(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_sync(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_record_outcome(req)
        assert resp.status_code == 503

        resp = await ft_mod.api_field_tech_partner_view(req)
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 3. Opt-in Gate (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_tech_disabled_returns_409() -> None:
    with patch(
        "nce.admin_handlers.field_tech.require_field_tech_enabled",
        new=AsyncMock(side_effect=FieldTechDisabledError("field_tech not enabled")),
    ):
        req = _make_request(
            query={"namespace_id": _NAMESPACE_ID},
            body={"namespace_id": _NAMESPACE_ID, "kind": "service_call"},
        )
        resp = await ft_mod.api_field_tech_create_work_order(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "not enabled" in body["error"]


# ---------------------------------------------------------------------------
# 4. Parameter Validation (422)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_namespace_id_returns_422() -> None:
    with patch(
        "nce.admin_handlers.field_tech.require_field_tech_enabled",
        new=AsyncMock(),
    ):
        req = _make_request(query={}, body={})
        resp = await ft_mod.api_field_tech_query_work_orders(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_path_param_id_returns_422() -> None:
    with patch(
        "nce.admin_handlers.field_tech.require_field_tech_enabled",
        new=AsyncMock(),
    ):
        req = _make_request(path_params={"id": ""}, query={"namespace_id": _NAMESPACE_ID})
        resp = await ft_mod.api_field_tech_work_order(req)
        assert resp.status_code == 422

        resp2 = await ft_mod.api_field_tech_assign(req)
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_missing_partner_scope_id_returns_422() -> None:
    with patch(
        "nce.admin_handlers.field_tech.require_field_tech_enabled",
        new=AsyncMock(),
    ):
        req = _make_request(query={"namespace_id": _NAMESPACE_ID})
        resp = await ft_mod.api_field_tech_partner_view(req)
        assert resp.status_code == 422
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "partner_scope_id" in body["error"]


@pytest.mark.asyncio
async def test_invalid_json_body_returns_422() -> None:
    req = MagicMock()
    req.json = AsyncMock(side_effect=ValueError("malformed JSON"))
    req.query_params = {}
    req.path_params = {}
    resp = await ft_mod.api_field_tech_dispatch(req)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. Happy Paths & Cache Invalidation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_field_tech_dispatch_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_dispatch",
            new=AsyncMock(
                return_value={"recommendations": [{"assignee_id": "tech-1", "score": 92.5}]}
            ),
        ) as mock_core,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "location_id": "LOC-1",
                "candidate_assignee_ids": ["tech-1"],
            }
        )
        resp = await ft_mod.api_field_tech_dispatch(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert len(body["recommendations"]) == 1
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_field_tech_create_work_order_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_create_work_order",
            new=AsyncMock(return_value={"id": _WO_ID, "kind": "service_call", "status": "draft"}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "kind": "service_call", "title": "Inspect rack"}
        )
        resp = await ft_mod.api_field_tech_create_work_order(req)
        assert resp.status_code == 201
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["work_order"]["id"] == _WO_ID
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_query_work_orders_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_query_work_order",
            new=AsyncMock(return_value={"work_orders": [{"id": _WO_ID}], "total": 1}),
        ) as mock_core,
    ):
        req = _make_request(query={"namespace_id": _NAMESPACE_ID, "status": "draft"})
        resp = await ft_mod.api_field_tech_query_work_orders(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert len(body["work_orders"]) == 1
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_field_tech_work_order_get_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_get_work_order",
            new=AsyncMock(return_value={"id": _WO_ID, "kind": "service_call"}),
        ) as mock_core,
    ):
        req = _make_request(
            path_params={"id": _WO_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await ft_mod.api_field_tech_work_order(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["work_order"]["id"] == _WO_ID
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_field_tech_assign_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_assign",
            new=AsyncMock(
                return_value={"id": _WO_ID, "assignee_id": "tech-007", "status": "assigned"}
            ),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            path_params={"id": _WO_ID},
            body={"namespace_id": _NAMESPACE_ID, "assignee_id": "tech-007"},
        )
        resp = await ft_mod.api_field_tech_assign(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["assignment"]["assignee_id"] == "tech-007"
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_complete_checklist_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_complete_checklist",
            new=AsyncMock(return_value={"checklist_id": _CHECKLIST_ID, "status": "completed"}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "template_key": "commissioning_standard",
            }
        )
        resp = await ft_mod.api_field_tech_complete_checklist(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["checklist"]["status"] == "completed"
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_scan_serial_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_scan_serial",
            new=AsyncMock(return_value={"scan_id": str(uuid4()), "scanned_serial": "SN-98765"}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "scanned_serial": "SN-98765",
            }
        )
        resp = await ft_mod.api_field_tech_scan_serial(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["scan"]["scanned_serial"] == "SN-98765"
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_log_time_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_log_time",
            new=AsyncMock(return_value={"entry_id": str(uuid4()), "hours": 2.5}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "technician_id": "tech-001",
                "hours": 2.5,
            }
        )
        resp = await ft_mod.api_field_tech_log_time(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["time_entry"]["hours"] == 2.5
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_attach_photo_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_attach_photo",
            new=AsyncMock(return_value={"photo_id": str(uuid4()), "caption": "Rack front"}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "photo_blob_ref": "blob://rack.jpg",
            }
        )
        resp = await ft_mod.api_field_tech_attach_photo(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_sync_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_sync",
            new=AsyncMock(return_value={"accepted": 2, "conflicts": 0}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={"namespace_id": _NAMESPACE_ID, "device_id": "phone-1", "mutations": []}
        )
        resp = await ft_mod.api_field_tech_sync(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["sync"]["accepted"] == 2
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_record_outcome_success() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_record_outcome",
            new=AsyncMock(return_value={"outcome_id": str(uuid4()), "status": "completed"}),
        ) as mock_core,
        patch(
            "nce.admin_handlers.field_tech.bump_mcp_cache_generation", new=MagicMock()
        ) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "completion_status": "completed",
            }
        )
        resp = await ft_mod.api_field_tech_record_outcome(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        mock_core.assert_awaited_once()
        mock_bump.assert_called_once()


@pytest.mark.asyncio
async def test_api_field_tech_partner_view_success_and_redaction() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_partner_view",
            new=AsyncMock(
                return_value={
                    "partner_scope_id": "PARTNER-1",
                    "work_orders": [
                        {
                            "id": _WO_ID,
                            "title": "Rack Maintenance",
                            "status": "in_progress",
                            # Internal rates / salaries must NOT be present
                        }
                    ],
                }
            ),
        ) as mock_core,
    ):
        req = _make_request(query={"namespace_id": _NAMESPACE_ID, "partner_scope_id": "PARTNER-1"})
        resp = await ft_mod.api_field_tech_partner_view(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["partner_scope_id"] == "PARTNER-1"
        wo = body["work_orders"][0]
        assert "billing_rate" not in wo
        assert "internal_cost" not in wo
        assert "hourly_wage" not in wo
        mock_core.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Domain Refusals & Error Statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_order_not_found_returns_404() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_get_work_order",
            new=AsyncMock(side_effect=WorkOrderNotFoundError(work_order_id=_WO_ID)),
        ),
    ):
        req = _make_request(
            path_params={"id": _WO_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await ft_mod.api_field_tech_work_order(req)
        assert resp.status_code == 404
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "no work_orders row" in body["error"]


@pytest.mark.asyncio
async def test_work_order_invalid_transition_returns_409() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_assign",
            new=AsyncMock(
                side_effect=WorkOrderInvalidTransitionError(
                    "Invalid work order status transition: completed -> assigned"
                )
            ),
        ),
    ):
        req = _make_request(
            path_params={"id": _WO_ID},
            body={"namespace_id": _NAMESPACE_ID, "assignee_id": "tech-001"},
        )
        resp = await ft_mod.api_field_tech_assign(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "Invalid work order status transition" in body["error"]


@pytest.mark.asyncio
async def test_checklist_incomplete_returns_409() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_complete_checklist",
            new=AsyncMock(
                side_effect=ChecklistIncompleteError(
                    "Checklist cannot be marked completed: missing items"
                )
            ),
        ),
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "work_order_id": _WO_ID,
                "template_key": "commissioning_standard",
            }
        )
        resp = await ft_mod.api_field_tech_complete_checklist(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "Checklist cannot be marked completed" in body["error"]


@pytest.mark.asyncio
async def test_checklist_not_found_returns_404() -> None:
    with (
        patch("nce.admin_handlers.field_tech.require_field_tech_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.field_tech.do_complete_checklist",
            new=AsyncMock(side_effect=ChecklistNotFoundError("Checklist not found")),
        ),
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID, "checklist_id": _CHECKLIST_ID})
        resp = await ft_mod.api_field_tech_complete_checklist(req)
        assert resp.status_code == 404
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert "Checklist not found" in body["error"]
