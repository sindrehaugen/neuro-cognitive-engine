"""
tests/unit/test_support_routes.py
=================================
Unit tests for Module 10 (Support Engine) admin HTTP REST surface:
  - Route mounting in admin_app.py
  - Opt-in guard enforcement (409 on disabled)
  - Missing parameter and invalid payload validation (422)
  - Engine disconnected handling (503)
  - Happy paths for all 7 route handlers
  - Mutating route MCP cache generation bumping
  - Domain refusal mappings (404 on not found, 409 on status conflict)

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
from nce.admin_handlers import support as support_mod
from nce.vertical_modules.support._guard import SupportDisabledError
from nce.vertical_modules.support.tickets import (
    InvalidTicketStatusError,
    TicketAlreadyResolvedError,
    TicketNotFoundError,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_TICKET_ID = str(uuid4())
_CUSTOMER_ID = "CUST-999"


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette Request mock."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
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


def test_support_routes_mounted_in_admin_app() -> None:
    routes = build_admin_routes()
    route_table = {
        (r.path, tuple(sorted(m for m in (r.methods or []) if m != "HEAD")))
        for r in routes
        if hasattr(r, "methods")
    }

    assert ("/api/support/tickets", ("GET",)) in route_table

    assert ("/api/support/tickets", ("POST",)) in route_table
    assert ("/api/support/tickets/{id}", ("GET",)) in route_table
    assert ("/api/support/tickets/{id}/sla-clock", ("GET",)) in route_table
    assert ("/api/support/customers/{id}/health", ("GET",)) in route_table
    assert ("/api/support/troubleshoot", ("POST",)) in route_table
    assert ("/api/support/tickets/{id}/resolve", ("POST",)) in route_table


# ---------------------------------------------------------------------------
# 2. Engine Disconnected (503)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_disconnected_returns_503():
    with patch.object(admin_state, "engine", None):
        req = _make_request()
        resp = await support_mod.api_support_tickets_list(req)
        assert resp.status_code == 503

        resp2 = await support_mod.api_support_tickets_open(req)
        assert resp2.status_code == 503


# ---------------------------------------------------------------------------
# 3. Opt-in Gate (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_support_disabled_returns_409():
    with patch(
        "nce.admin_handlers.support.require_support_enabled",
        new=AsyncMock(side_effect=SupportDisabledError("support not enabled")),
    ):
        req = _make_request(query={"namespace_id": _NAMESPACE_ID})
        resp = await support_mod.api_support_tickets_list(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["reason"] == "support_disabled"


# ---------------------------------------------------------------------------
# 4. Parameter Validation (422)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_namespace_id_returns_422():
    with patch(
        "nce.admin_handlers.support.require_support_enabled",
        new=AsyncMock(),
    ):
        req = _make_request(query={})
        resp = await support_mod.api_support_tickets_list(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_path_param_id_returns_422():
    with patch(
        "nce.admin_handlers.support.require_support_enabled",
        new=AsyncMock(),
    ):
        req = _make_request(path_params={"id": "   "}, query={"namespace_id": _NAMESPACE_ID})
        resp = await support_mod.api_support_tickets_get(req)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. Happy Paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_support_tickets_list_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_query_ticket",
            new=AsyncMock(return_value={"items": [], "total": 0}),
        ) as mock_core,
    ):
        req = _make_request(
            query={
                "namespace_id": _NAMESPACE_ID,
                "status": "open",
                "limit": "10",
                "offset": "0",
            }
        )
        resp = await support_mod.api_support_tickets_list(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["items"] == []
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_tickets_open_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_open_ticket",
            new=AsyncMock(return_value={"ticket": {"id": _TICKET_ID}}),
        ) as mock_core,
        patch("nce.admin_handlers.support.bump_mcp_cache_generation", new=AsyncMock()) as mock_bump,
    ):
        req = _make_request(
            body={
                "namespace_id": _NAMESPACE_ID,
                "summary": "Projector overheated",
            }
        )
        resp = await support_mod.api_support_tickets_open(req)
        assert resp.status_code == 201
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["ticket"]["id"] == _TICKET_ID
        mock_core.assert_awaited_once()
        mock_bump.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_tickets_get_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_query_ticket",
            new=AsyncMock(return_value={"ticket": {"id": _TICKET_ID}, "sla_clock": None}),
        ) as mock_core,
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await support_mod.api_support_tickets_get(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["ticket"]["id"] == _TICKET_ID
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_ticket_sla_clock_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_sla_clock",
            new=AsyncMock(return_value={"ticket_id": _TICKET_ID, "breached": False}),
        ) as mock_core,
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await support_mod.api_support_ticket_sla_clock(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["ticket_id"] == _TICKET_ID
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_customer_health_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_health_score",
            new=AsyncMock(return_value={"customer_id": _CUSTOMER_ID, "score": 85.0}),
        ) as mock_core,
    ):
        req = _make_request(
            path_params={"id": _CUSTOMER_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await support_mod.api_support_customer_health(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["customer_id"] == _CUSTOMER_ID
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_troubleshoot_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_troubleshoot",
            new=AsyncMock(return_value={"cited_ticket_ids": ["T-1"], "zero_history": False}),
        ) as mock_core,
    ):
        req = _make_request(body={"namespace_id": _NAMESPACE_ID, "symptom_text": "HDMI no sync"})
        resp = await support_mod.api_support_troubleshoot(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["cited_ticket_ids"] == ["T-1"]
        mock_core.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_support_tickets_resolve_success():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_resolve_ticket",
            new=AsyncMock(return_value={"ticket": {"id": _TICKET_ID, "status": "resolved"}}),
        ) as mock_core,
        patch("nce.admin_handlers.support.bump_mcp_cache_generation", new=AsyncMock()) as mock_bump,
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            body={
                "namespace_id": _NAMESPACE_ID,
                "resolution_text": "Firmware reflashed",
            },
        )
        resp = await support_mod.api_support_tickets_resolve(req)
        assert resp.status_code == 200
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["ok"] is True
        assert body["ticket"]["status"] == "resolved"
        mock_core.assert_awaited_once()
        mock_bump.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Domain Refusals & Error Statuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_not_found_returns_404():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_query_ticket",
            new=AsyncMock(side_effect=TicketNotFoundError(ticket_id=_TICKET_ID)),
        ),
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            query={"namespace_id": _NAMESPACE_ID},
        )
        resp = await support_mod.api_support_tickets_get(req)
        assert resp.status_code == 404
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["not_found"] is True


@pytest.mark.asyncio
async def test_ticket_already_resolved_returns_409():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_resolve_ticket",
            new=AsyncMock(
                side_effect=TicketAlreadyResolvedError(ticket_id=_TICKET_ID, status="resolved")
            ),
        ),
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            body={"namespace_id": _NAMESPACE_ID, "resolution_text": "Duplicate resolve"},
        )
        resp = await support_mod.api_support_tickets_resolve(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["reason"] == "ticket_already_resolved"


@pytest.mark.asyncio
async def test_invalid_ticket_status_returns_409():
    with (
        patch("nce.admin_handlers.support.require_support_enabled", new=AsyncMock()),
        patch(
            "nce.admin_handlers.support.do_resolve_ticket",
            new=AsyncMock(
                side_effect=InvalidTicketStatusError(ticket_id=_TICKET_ID, status="cancelled")
            ),
        ),
    ):
        req = _make_request(
            path_params={"id": _TICKET_ID},
            body={"namespace_id": _NAMESPACE_ID, "resolution_text": "Resolve cancelled"},
        )
        resp = await support_mod.api_support_tickets_resolve(req)
        assert resp.status_code == 409
        body = json.loads(bytes(resp.body).decode("utf-8"))
        assert body["reason"] == "invalid_ticket_status"
