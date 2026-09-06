"""
tests/unit/test_vendors_surface.py
===================================
Acceptance tests for Vendors & Contractors Engine surface completion (Wave V-1).

Covers:
  1. The ``vendors`` package imports cleanly and exports all cores.
  2. MCP handlers raise ``McpError(-32602)`` when required params are absent.
  3. All 14 vendor MCP tools are registered in TOOL_REGISTRY with correct flags.
  4. All 6 REST routes are registered in the admin app and respond properly.
  5. Handlers call underlying cores and return expected responses.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_app import build_admin_routes
from nce.mcp_errors import McpError
from nce.tool_registry import TOOL_REGISTRY

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PARTNER_SCOPE_ID = "11111111-1111-4000-8000-000000000002"


def test_package_imports() -> None:
    import nce.admin_handlers.vendors  # noqa: F401
    import nce.vertical_modules.vendors  # noqa: F401
    import nce.vertical_modules.vendors.mcp_handlers  # noqa: F401


def test_package_exports_all_cores() -> None:
    import nce.vertical_modules.vendors as v

    expected_cores = [
        "do_upsert_vendor",
        "do_get_vendor",
        "do_compute_scorecard",
        "do_partner_view",
        "do_upsert_cert",
        "do_check_cert_expiry",
        "do_match_contractor",
        "do_compute_performance",
        "do_recall_similar_jobs",
        "do_reliability_radar",
        "do_calibrate_weights",
        "do_get_contractor",
        "do_upsert_contractor",
        "do_check_tier_at_risk",
        "do_detect_reliability_degradation",
        "do_get_tier_status",
        "do_record_outcome",
    ]
    for core in expected_cores:
        assert hasattr(v, core), f"vendors package missing export: {core}"


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


# ---------------------------------------------------------------------------
# MCP Handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_handlers_missing_namespace_id() -> None:
    from nce.vertical_modules.vendors.mcp_handlers import (
        handle_vendors_compute_scorecard,
        handle_vendors_get_contractor,
        handle_vendors_get_vendor,
        handle_vendors_upsert_cert,
        handle_vendors_upsert_contractor,
        handle_vendors_upsert_vendor,
    )

    engine = _make_engine()

    for handler in [
        handle_vendors_get_vendor,
        handle_vendors_compute_scorecard,
        handle_vendors_upsert_vendor,
        handle_vendors_upsert_contractor,
        handle_vendors_get_contractor,
        handle_vendors_upsert_cert,
    ]:
        with pytest.raises(McpError) as exc:
            await handler(engine, {})
        assert exc.value.code == -32602


@pytest.mark.asyncio
async def test_mcp_handlers_success() -> None:
    from nce.vertical_modules.vendors.mcp_handlers import (
        handle_vendors_get_contractor,
        handle_vendors_upsert_cert,
        handle_vendors_upsert_contractor,
        handle_vendors_upsert_vendor,
    )

    engine = _make_engine()

    with patch(
        "nce.vertical_modules.vendors.mcp_handlers.do_upsert_vendor",
        new_callable=AsyncMock,
        return_value={"ok": True, "vendor_id": "V1"},
    ):
        res = await handle_vendors_upsert_vendor(
            engine,
            {"namespace_id": _NAMESPACE_ID, "orgnr": "123456789", "name": "ACME"},
        )
        data = json.loads(res)
        assert data["ok"] is True
        assert data["vendor_id"] == "V1"

    with patch(
        "nce.vertical_modules.vendors.mcp_handlers.do_upsert_contractor",
        new_callable=AsyncMock,
        return_value={"ok": True, "contractor_id": "CONTRACTOR:C1"},
    ):
        res = await handle_vendors_upsert_contractor(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "contractor_id": "C1",
                "partner_scope_id": _PARTNER_SCOPE_ID,
            },
        )
        data = json.loads(res)
        assert data["ok"] is True
        assert data["contractor_id"] == "CONTRACTOR:C1"

    with patch(
        "nce.vertical_modules.vendors.mcp_handlers.do_get_contractor",
        new_callable=AsyncMock,
        return_value={"contractor_id": "CONTRACTOR:C1", "skills": ["AV"]},
    ):
        res = await handle_vendors_get_contractor(
            engine,
            {"namespace_id": _NAMESPACE_ID, "contractor_id": "C1"},
        )
        data = json.loads(res)
        assert data["contractor_id"] == "CONTRACTOR:C1"
        assert data["skills"] == ["AV"]

    with patch(
        "nce.vertical_modules.vendors.mcp_handlers.do_upsert_cert",
        new_callable=AsyncMock,
        return_value={"ok": True, "cert_label": "CERT:C1:SAFETY"},
    ):
        res = await handle_vendors_upsert_cert(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "contractor_id": "C1",
                "cert_name": "SAFETY",
                "expiry_date": "2027-01-01",
            },
        )
        data = json.loads(res)
        assert data["ok"] is True
        assert data["cert_label"] == "CERT:C1:SAFETY"


def test_mcp_tools_registered_with_correct_flags() -> None:
    expected_tools = {
        "vendors_get_vendor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_compute_scorecard": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_get_tier_status": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_detect_reliability_degradation": {
            "cacheable": True,
            "admin_only": False,
            "mutation": False,
        },
        "vendors_check_tier_at_risk": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_match_contractor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_compute_performance": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_recall_similar_jobs": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_reliability_radar": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_calibrate_weights": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_upsert_vendor": {"cacheable": False, "admin_only": True, "mutation": True},
        "vendors_upsert_contractor": {"cacheable": False, "admin_only": True, "mutation": True},
        "vendors_get_contractor": {"cacheable": True, "admin_only": False, "mutation": False},
        "vendors_upsert_cert": {"cacheable": False, "admin_only": True, "mutation": True},
    }

    for tool_name, flags in expected_tools.items():
        assert tool_name in TOOL_REGISTRY, f"'{tool_name}' not registered in TOOL_REGISTRY"
        spec = TOOL_REGISTRY[tool_name]
        assert spec.cacheable is flags["cacheable"], f"'{tool_name}' cacheable mismatch"
        assert spec.admin_only is flags["admin_only"], f"'{tool_name}' admin_only mismatch"
        assert spec.mutation is flags["mutation"], f"'{tool_name}' mutation mismatch"


def test_vendors_routes_mounted_in_admin_app() -> None:
    routes = build_admin_routes()
    paths = {r.path for r in routes}
    assert "/api/vendors/scorecard" in paths
    assert "/api/vendors/upsert" in paths
    assert "/api/vendors/contractors/upsert" in paths
    assert "/api/vendors/contractors/{id}" in paths
    assert "/api/vendors/certs/upsert" in paths
    assert "/api/vendors/{id}" in paths


# ---------------------------------------------------------------------------
# HTTP REST Route Testing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_routes_no_engine() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import (
        api_vendors_get_contractor,
        api_vendors_get_vendor,
        api_vendors_scorecard,
        api_vendors_upsert,
        api_vendors_upsert_cert,
        api_vendors_upsert_contractor,
    )

    with patch.object(admin_state, "engine", None):
        # GET /api/vendors/scorecard
        resp = await api_vendors_scorecard(_make_request(qp={"namespace_id": _NAMESPACE_ID}))
        assert resp.status_code == 503

        # GET /api/vendors/{id}
        resp = await api_vendors_get_vendor(
            _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "some-id"})
        )
        assert resp.status_code == 503

        # POST /api/vendors/upsert
        resp = await api_vendors_upsert(
            _make_request(body={"namespace_id": _NAMESPACE_ID, "orgnr": "123", "name": "ACME"})
        )
        assert resp.status_code == 503

        # POST /api/vendors/contractors/upsert
        resp = await api_vendors_upsert_contractor(
            _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "contractor_id": "C1",
                    "partner_scope_id": _PARTNER_SCOPE_ID,
                }
            )
        )
        assert resp.status_code == 503

        # GET /api/vendors/contractors/{id}
        resp = await api_vendors_get_contractor(
            _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "C1"})
        )
        assert resp.status_code == 503

        # POST /api/vendors/certs/upsert
        resp = await api_vendors_upsert_cert(
            _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "contractor_id": "C1",
                    "cert_name": "SAFETY",
                    "expiry_date": "2027-01-01",
                }
            )
        )
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_rest_routes_missing_params() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import (
        api_vendors_get_contractor,
        api_vendors_get_vendor,
        api_vendors_scorecard,
        api_vendors_upsert,
        api_vendors_upsert_cert,
        api_vendors_upsert_contractor,
    )

    engine = _make_engine()
    with patch.object(admin_state, "engine", engine):
        # Missing namespace_id
        assert (await api_vendors_scorecard(_make_request(qp={}))).status_code == 422
        assert (
            await api_vendors_get_vendor(_make_request(qp={}, path_params={"id": "some-id"}))
        ).status_code == 422
        assert (await api_vendors_upsert(_make_request(body={}))).status_code == 422
        assert (await api_vendors_upsert_contractor(_make_request(body={}))).status_code == 422
        assert (
            await api_vendors_get_contractor(_make_request(qp={}, path_params={"id": "C1"}))
        ).status_code == 422
        assert (await api_vendors_upsert_cert(_make_request(body={}))).status_code == 422

        # Missing required body/path fields
        assert (
            await api_vendors_get_vendor(_make_request(qp={"namespace_id": _NAMESPACE_ID}))
        ).status_code == 422
        assert (
            await api_vendors_upsert(
                _make_request(body={"namespace_id": _NAMESPACE_ID, "orgnr": "123"})
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert(
                _make_request(body={"namespace_id": _NAMESPACE_ID, "name": "ACME"})
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert_contractor(
                _make_request(body={"namespace_id": _NAMESPACE_ID, "contractor_id": "C1"})
            )
        ).status_code == 422
        assert (
            await api_vendors_get_contractor(_make_request(qp={"namespace_id": _NAMESPACE_ID}))
        ).status_code == 422
        assert (
            await api_vendors_upsert_cert(
                _make_request(
                    body={
                        "namespace_id": _NAMESPACE_ID,
                        "contractor_id": "C1",
                        "cert_name": "SAFETY",
                    }
                )
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_rest_routes_invalid_uuid() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import (
        api_vendors_get_contractor,
        api_vendors_get_vendor,
        api_vendors_scorecard,
        api_vendors_upsert,
        api_vendors_upsert_cert,
        api_vendors_upsert_contractor,
    )

    engine = _make_engine()
    with patch.object(admin_state, "engine", engine):
        assert (
            await api_vendors_scorecard(_make_request(qp={"namespace_id": "not-a-uuid"}))
        ).status_code == 422
        assert (
            await api_vendors_get_vendor(
                _make_request(qp={"namespace_id": "not-a-uuid"}, path_params={"id": "some-id"})
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert(
                _make_request(body={"namespace_id": "not-a-uuid", "orgnr": "123", "name": "ACME"})
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert_contractor(
                _make_request(
                    body={
                        "namespace_id": "not-a-uuid",
                        "contractor_id": "C1",
                        "partner_scope_id": _PARTNER_SCOPE_ID,
                    }
                )
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert_contractor(
                _make_request(
                    body={
                        "namespace_id": _NAMESPACE_ID,
                        "contractor_id": "C1",
                        "partner_scope_id": "not-a-uuid",
                    }
                )
            )
        ).status_code == 422
        assert (
            await api_vendors_get_contractor(
                _make_request(qp={"namespace_id": "not-a-uuid"}, path_params={"id": "C1"})
            )
        ).status_code == 422
        assert (
            await api_vendors_upsert_cert(
                _make_request(
                    body={
                        "namespace_id": "not-a-uuid",
                        "contractor_id": "C1",
                        "cert_name": "SAFETY",
                        "expiry_date": "2027-01-01",
                    }
                )
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_rest_routes_success_flow() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import (
        api_vendors_get_contractor,
        api_vendors_get_vendor,
        api_vendors_upsert,
        api_vendors_upsert_cert,
        api_vendors_upsert_contractor,
    )

    engine = _make_engine()
    with (
        patch.object(admin_state, "engine", engine),
        patch("nce.admin_handlers.vendors.bump_mcp_cache_generation", new_callable=AsyncMock),
        patch(
            "nce.admin_handlers.vendors.do_get_vendor",
            new_callable=AsyncMock,
            return_value={"id": "uuid1", "name": "ACME"},
        ),
        patch(
            "nce.admin_handlers.vendors.do_upsert_vendor",
            new_callable=AsyncMock,
            return_value={"status": "ok", "vendor_id": "uuid1"},
        ),
        patch(
            "nce.admin_handlers.vendors.do_upsert_contractor",
            new_callable=AsyncMock,
            return_value={"ok": True, "contractor_id": "CONTRACTOR:C1"},
        ),
        patch(
            "nce.admin_handlers.vendors.do_get_contractor",
            new_callable=AsyncMock,
            return_value={"contractor_id": "CONTRACTOR:C1", "skills": ["AV"]},
        ),
        patch(
            "nce.admin_handlers.vendors.do_upsert_cert",
            new_callable=AsyncMock,
            return_value={"ok": True, "cert_label": "CERT:C1:SAFETY"},
        ),
    ):
        # 1. GET vendor ok
        resp = await api_vendors_get_vendor(
            _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "ACME"})
        )
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body).decode("utf-8"))["vendor"]["name"] == "ACME"

        # 2. POST upsert vendor ok
        resp = await api_vendors_upsert(
            _make_request(
                body={"namespace_id": _NAMESPACE_ID, "orgnr": "123456789", "name": "ACME"}
            )
        )
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body).decode("utf-8"))["result"]["status"] == "ok"

        # 3. POST upsert contractor ok
        resp = await api_vendors_upsert_contractor(
            _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "contractor_id": "C1",
                    "partner_scope_id": _PARTNER_SCOPE_ID,
                }
            )
        )
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body).decode("utf-8"))["result"]["ok"] is True

        # 4. GET contractor ok
        resp = await api_vendors_get_contractor(
            _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "C1"})
        )
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body).decode("utf-8"))["contractor"]["skills"] == ["AV"]

        # 5. POST upsert cert ok
        resp = await api_vendors_upsert_cert(
            _make_request(
                body={
                    "namespace_id": _NAMESPACE_ID,
                    "contractor_id": "C1",
                    "cert_name": "SAFETY",
                    "expiry_date": "2027-01-01",
                }
            )
        )
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body).decode("utf-8"))["result"]["ok"] is True


@pytest.mark.asyncio
async def test_rest_api_vendors_get_contractor_not_found() -> None:
    from nce import admin_state
    from nce.admin_handlers.vendors import api_vendors_get_contractor

    engine = _make_engine()
    with (
        patch.object(admin_state, "engine", engine),
        patch(
            "nce.admin_handlers.vendors.do_get_contractor",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = await api_vendors_get_contractor(
            _make_request(qp={"namespace_id": _NAMESPACE_ID}, path_params={"id": "NON_EXISTENT"})
        )
        assert resp.status_code == 404
        assert json.loads(bytes(resp.body).decode("utf-8"))["contractor"] is None
