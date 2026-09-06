"""
tests/unit/test_assets_orphaned_cores.py
========================================
Acceptance tests for ML9b Phase 1 — Wiring orphaned cores into MCP and REST:
  - do_seed_asset_from_bom / handle_assets_seed_from_bom / api_assets_seed_from_bom
  - do_pull_telemetry / handle_assets_pull_telemetry / api_assets_pull_telemetry
  - do_attach_sla / handle_assets_attach_sla / api_assets_attach_sla
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.mcp_errors import McpError

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_ASSET_ID = str(uuid4())
_AGREEMENT_ID = str(uuid4())
_BOM_LINE_ID = "BOM-LINE-TEST-001"
_ROOM_ID = "ROOM-101"


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# 1. MCP Handlers — tests for seed_from_bom, pull_telemetry, attach_sla
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_assets_seed_from_bom_success() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    fake_result = {
        "ok": True,
        "created": True,
        "asset_id": _ASSET_ID,
        "bom_line_id": _BOM_LINE_ID,
        "serial": "SN-TEST-123",
        "functional_location_id": _ROOM_ID,
        "lifecycle_state": "PROPOSED",
    }
    with patch.object(mcp_handlers, "do_seed_asset_from_bom", AsyncMock(return_value=fake_result)):
        raw = await mcp_handlers.handle_assets_seed_from_bom(
            MagicMock(),
            {
                "namespace_id": _NAMESPACE_ID,
                "bom_line_id": _BOM_LINE_ID,
                "serial": "SN-TEST-123",
                "functional_location_id": _ROOM_ID,
            },
        )
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    assert parsed["created"] is True
    assert parsed["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_handle_assets_seed_from_bom_missing_namespace_raises() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    with pytest.raises(McpError) as exc_info:
        await mcp_handlers.handle_assets_seed_from_bom(MagicMock(), {"bom_line_id": _BOM_LINE_ID})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_assets_pull_telemetry_success() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    fake_result = {
        "ok": True,
        "asset_id": _ASSET_ID,
        "platform": "mock",
        "adapter_platform": "mock",
        "pulled": 3,
        "written": 3,
        "duplicates": 0,
    }
    with patch.object(mcp_handlers, "do_pull_telemetry", AsyncMock(return_value=fake_result)):
        raw = await mcp_handlers.handle_assets_pull_telemetry(
            MagicMock(),
            {"namespace_id": _NAMESPACE_ID, "asset_id": _ASSET_ID, "platform": "mock"},
        )
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    assert parsed["pulled"] == 3
    assert parsed["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_handle_assets_pull_telemetry_missing_namespace_raises() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    with pytest.raises(McpError) as exc_info:
        await mcp_handlers.handle_assets_pull_telemetry(MagicMock(), {"asset_id": _ASSET_ID})
    assert exc_info.value.code == -32602


@pytest.mark.asyncio
async def test_handle_assets_attach_sla_success() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    fake_result = {
        "status": "ok",
        "agreement_id": _AGREEMENT_ID,
        "functional_location_id": _ROOM_ID,
        "covered_by_edge": {
            "subject": f"FL:test:{_ROOM_ID}",
            "predicate": "covered_by",
            "object": f"Agreement:{_AGREEMENT_ID}",
        },
        "sla_terms_read": ["AgreementTerm:t1:sla_resolution_hours"],
    }
    with patch.object(mcp_handlers, "do_attach_sla", AsyncMock(return_value=fake_result)):
        raw = await mcp_handlers.handle_assets_attach_sla(
            MagicMock(),
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "functional_location_id": _ROOM_ID,
            },
        )
    parsed = json.loads(raw)
    assert parsed["status"] == "ok"
    assert parsed["agreement_id"] == _AGREEMENT_ID


@pytest.mark.asyncio
async def test_handle_assets_attach_sla_missing_namespace_raises() -> None:
    from nce.vertical_modules.assets import mcp_handlers

    with pytest.raises(McpError) as exc_info:
        await mcp_handlers.handle_assets_attach_sla(
            MagicMock(), {"agreement_id": _AGREEMENT_ID, "functional_location_id": _ROOM_ID}
        )
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 2. REST Handlers — tests for seed, telemetry, sla
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_assets_seed_from_bom_success() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    fake_result = {
        "ok": True,
        "created": True,
        "asset_id": _ASSET_ID,
        "bom_line_id": _BOM_LINE_ID,
        "serial": "SN-123",
        "functional_location_id": _ROOM_ID,
        "lifecycle_state": "PROPOSED",
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(
            assets_mod, "do_seed_asset_from_bom", AsyncMock(return_value=fake_result)
        ):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "bom_line_id": _BOM_LINE_ID,
                    "serial": "SN-123",
                    "functional_location_id": _ROOM_ID,
                }
            )
            resp = await assets_mod.api_assets_seed_from_bom(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))

    assert resp.status_code == 201
    assert body["ok"] is True
    assert body["asset_id"] == _ASSET_ID


@pytest.mark.asyncio
async def test_api_assets_seed_from_bom_validation_error() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    with patch.object(admin_state, "engine", MagicMock()):
        # Missing namespace_id
        req = _make_request(body={"bom_line_id": _BOM_LINE_ID})
        resp = await assets_mod.api_assets_seed_from_bom(req)
        assert resp.status_code == 422

        # Missing bom_line_id (raises ValueError in core)
        with patch.object(
            assets_mod,
            "do_seed_asset_from_bom",
            AsyncMock(side_effect=ValueError("bom_line_id is required")),
        ):
            req = _make_request(body={"namespace_id": _NAMESPACE_ID})
            resp = await assets_mod.api_assets_seed_from_bom(req)
            assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assets_pull_telemetry_success() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    fake_result = {
        "ok": True,
        "asset_id": _ASSET_ID,
        "platform": "mock",
        "adapter_platform": "mock",
        "pulled": 2,
        "written": 2,
        "duplicates": 0,
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(assets_mod, "do_pull_telemetry", AsyncMock(return_value=fake_result)):
            req = _make_request(
                path_params={"id": _ASSET_ID},
                body={"namespace_id": _NAMESPACE_ID, "platform": "mock"},
            )
            resp = await assets_mod.api_assets_pull_telemetry(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["pulled"] == 2


@pytest.mark.asyncio
async def test_api_assets_pull_telemetry_validation_error() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    with patch.object(admin_state, "engine", MagicMock()):
        # Missing path param id
        req = _make_request(path_params={}, body={"namespace_id": _NAMESPACE_ID})
        resp = await assets_mod.api_assets_pull_telemetry(req)
        assert resp.status_code == 422

        # Missing namespace_id
        req = _make_request(path_params={"id": _ASSET_ID}, body={})
        resp = await assets_mod.api_assets_pull_telemetry(req)
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assets_attach_sla_success() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    fake_result = {
        "status": "ok",
        "agreement_id": _AGREEMENT_ID,
        "functional_location_id": _ROOM_ID,
        "covered_by_edge": {
            "subject": f"FL:test:{_ROOM_ID}",
            "predicate": "covered_by",
            "object": f"Agreement:{_AGREEMENT_ID}",
        },
        "sla_terms_read": ["AgreementTerm:t1:sla_resolution_hours"],
    }
    with patch.object(admin_state, "engine", MagicMock()):
        with patch.object(assets_mod, "do_attach_sla", AsyncMock(return_value=fake_result)):
            req = _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "agreement_id": _AGREEMENT_ID,
                    "functional_location_id": _ROOM_ID,
                }
            )
            resp = await assets_mod.api_assets_attach_sla(req)
            body = json.loads(bytes(resp.body).decode("utf-8"))

    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["agreement_id"] == _AGREEMENT_ID


@pytest.mark.asyncio
async def test_api_assets_attach_sla_validation_error() -> None:
    from nce import admin_state
    from nce.admin_handlers import assets as assets_mod

    with patch.object(admin_state, "engine", MagicMock()):
        # Missing namespace_id
        req = _make_request(
            body={"agreement_id": _AGREEMENT_ID, "functional_location_id": _ROOM_ID}
        )
        resp = await assets_mod.api_assets_attach_sla(req)
        assert resp.status_code == 422

        # Missing agreement_id (raises ValueError in core)
        with patch.object(
            assets_mod,
            "do_attach_sla",
            AsyncMock(side_effect=ValueError("agreement_id is required")),
        ):
            req = _make_request(
                body={"namespace_id": _NAMESPACE_ID, "functional_location_id": _ROOM_ID}
            )
            resp = await assets_mod.api_assets_attach_sla(req)
            assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assets_new_routes_no_engine_returns_503() -> None:
    from nce import admin_state
    from nce.admin_handlers.assets import (
        api_assets_attach_sla,
        api_assets_pull_telemetry,
        api_assets_seed_from_bom,
    )

    with patch.object(admin_state, "engine", None):
        resp = await api_assets_seed_from_bom(
            _make_request(body={"namespace_id": _NAMESPACE_ID, "bom_line_id": _BOM_LINE_ID})
        )
        assert resp.status_code == 503

        resp = await api_assets_pull_telemetry(
            _make_request(path_params={"id": _ASSET_ID}, body={"namespace_id": _NAMESPACE_ID})
        )
        assert resp.status_code == 503

        resp = await api_assets_attach_sla(
            _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "agreement_id": _AGREEMENT_ID,
                    "functional_location_id": _ROOM_ID,
                }
            )
        )
        assert resp.status_code == 503
