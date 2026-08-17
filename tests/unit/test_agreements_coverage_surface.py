"""
tests/unit/test_agreements_coverage_surface.py
===============================================
Acceptance tests for Batch 109 — Module 3.Wave 5 (coverage-surface).

Covers:
  1. ``agreements_lookup_terms`` MCP tool — unwrapped terms shape (per-field
     value/confidence/review_status), explicit namespace_id SQL predicate,
     agreement_id / supplier filter paths, disabled-namespace error surface.
  2. ``GET /api/agreements/coverage`` REST route — 200 with flags + KPI counts,
     422 on missing namespace_id, 409 on disabled namespace, mounted BEFORE
     ``/api/agreements/{id}``.
  3. ``_agreements_coverage_watcher_tick`` — one throttled alert per
     (namespace, flag_type) group for expiry/leakage, NEVER for review;
     namespace scan filtered on ``metadata->'agreements'->>'enabled'``;
     lock-held short-circuit.
  4. Tool registry — ``agreements_lookup_terms`` present, cacheable,
     non-mutation.

All tests are pure unit tests (no DB, no Redis) — mocking conventions follow
``tests/unit/test_procurement_surface.py`` / ``tests/unit/test_project_routes.py``.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_errors import McpError
from nce.vertical_modules.agreements._guard import AgreementsDisabledError

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_AGREEMENT_ID = "00000000-0000-4000-8000-0000000000aa"

_EXTRACTED = {
    "supplierId": {
        "value": "912345678",
        "extractionConfidence": 95.0,
        "reviewStatus": "auto_green",
    },
    "paymentTermsDays": {
        "value": 30,
        "extractionConfidence": 72.0,
        "reviewStatus": "needs_review_yellow",
    },
    "validTo": "2027-01-01",  # flat shape — must be tolerated like _unwrap_field
}


def _make_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agreement_id": uuid.UUID(_AGREEMENT_ID),
        "source_doc_ref": "doc-001",
        "review_status": "needs_review_yellow",
        "extraction_confidence": 82.5,
        "extracted": json.dumps(_EXTRACTED),
        "flagged_at": "2026-07-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _make_conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _patch_scoped_session(conn: MagicMock):
    """Patch scoped_pg_session in the mcp_handlers module to yield *conn*."""

    @asynccontextmanager
    async def _fake_session(pool: Any, namespace_id: Any):
        yield conn

    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.scoped_pg_session",
        _fake_session,
    )


def _patch_guard_ok():
    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.require_agreements_enabled",
        new=AsyncMock(return_value=None),
    )


def _make_request(qp: dict[str, str] | None = None) -> MagicMock:
    """Minimal Starlette-like request mock."""
    req = MagicMock()
    req.query_params = qp or {}
    req.path_params = {}
    return req


# ---------------------------------------------------------------------------
# 1. agreements_lookup_terms — MCP tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_terms_unwraps_fields_with_confidence_and_review_status():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([_make_row()])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        result = await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["count"] == 1
    ag = parsed["agreements"][0]
    assert ag["agreement_id"] == _AGREEMENT_ID
    assert ag["source_doc_ref"] == "doc-001"
    assert ag["review_status"] == "needs_review_yellow"
    assert ag["extraction_confidence"] == 82.5
    # Recency is trust context for a §9.3-oriented tool — surfaced, not dropped.
    assert ag["flagged_at"] == "2026-07-01T00:00:00+00:00"

    terms = ag["terms"]
    assert terms["supplierId"] == {
        "value": "912345678",
        "confidence": 95.0,
        "review_status": "auto_green",
    }
    # Unconfirmed field is included WITH its review state — never filtered (§9.3).
    assert terms["paymentTermsDays"] == {
        "value": 30,
        "confidence": 72.0,
        "review_status": "needs_review_yellow",
    }
    # Flat value tolerated the same way coverage._unwrap_field does.
    assert terms["validTo"] == {"value": "2027-01-01", "confidence": None, "review_status": None}


@pytest.mark.asyncio
async def test_lookup_terms_sql_has_explicit_namespace_filter():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    sql = conn.fetch.call_args[0][0]
    assert "namespace_id = $1::uuid" in sql, f"No explicit namespace predicate in SQL:\n{sql}"
    assert conn.fetch.call_args[0][1] == uuid.UUID(_NAMESPACE_ID)
    assert "ORDER BY flagged_at DESC" in sql
    assert "LIMIT 50" in sql


@pytest.mark.asyncio
async def test_lookup_terms_agreement_id_filter_path():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([_make_row()])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        await handle_agreements_lookup_terms(
            MagicMock(),
            {"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID},
        )

    sql = conn.fetch.call_args[0][0]
    assert "agreement_id = $2" in sql
    assert conn.fetch.call_args[0][2] == uuid.UUID(_AGREEMENT_ID)


@pytest.mark.asyncio
async def test_lookup_terms_supplier_filter_path():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    conn = _make_conn([_make_row()])
    with _patch_guard_ok(), _patch_scoped_session(conn):
        await handle_agreements_lookup_terms(
            MagicMock(),
            {"namespace_id": _NAMESPACE_ID, "supplier": "912345678"},
        )

    sql = conn.fetch.call_args[0][0]
    # Filter targets the supplierId term from the extraction schema
    # (extract.py's ExtractedAgreementModel has no supplierName field).
    # COALESCE fallback also matches rows overwritten with FLAT corrected_terms
    # via the review path (review.py stores reviewer input shape-unvalidated).
    assert "COALESCE(extracted->'supplierId'->>'value'" in sql
    assert "extracted->>'supplierId') ILIKE '%'||$2||'%'" in sql
    assert "extracted->>'supplierId') = $2" in sql
    assert "namespace_id = $1::uuid" in sql
    assert conn.fetch.call_args[0][2] == "912345678"


@pytest.mark.asyncio
async def test_lookup_terms_disabled_namespace_surfaces_scope_error():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    with patch(
        "nce.vertical_modules.agreements.mcp_handlers.require_agreements_enabled",
        new=AsyncMock(side_effect=AgreementsDisabledError("not enabled")),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_agreements_lookup_terms(MagicMock(), {"namespace_id": _NAMESPACE_ID})

    assert exc_info.value.code == -32005  # MCP_SCOPE_FORBIDDEN
    assert exc_info.value.data["reason"] == "agreements_disabled"


@pytest.mark.asyncio
async def test_lookup_terms_missing_namespace_id_is_invalid_params():
    from nce.vertical_modules.agreements.mcp_handlers import handle_agreements_lookup_terms

    with pytest.raises(McpError) as exc_info:
        await handle_agreements_lookup_terms(MagicMock(), {})

    assert exc_info.value.code == -32602  # MCP_INVALID_PARAMS


# ---------------------------------------------------------------------------
# 2. GET /api/agreements/coverage — REST route
# ---------------------------------------------------------------------------

_COVERAGE_RESULT: dict[str, Any] = {
    "status": "ok",
    "agreements_scanned": 3,
    "gl_rows_processed": 2,
    "flags": [
        {"agreement_id": "a1", "flag_type": "expiry", "detail": "d1"},
        {"agreement_id": "a2", "flag_type": "expiry", "detail": "d2"},
        {"agreement_id": "a3", "flag_type": "leakage", "detail": "d3"},
        {"agreement_id": "a4", "flag_type": "review", "detail": "d4"},
    ],
}


@pytest.mark.asyncio
async def test_coverage_route_200_with_kpis_counted_by_flag_type():
    from nce.admin_handlers.agreements import api_agreements_coverage

    with (
        patch("nce.admin_handlers.agreements.admin_state") as mock_state,
        # Batch: admin-surface sweep, Fix 1 — the REST handler now reassigns
        # namespace_id = validate_agent_id(namespace_id) (mirrors vendors.py)
        # before its own explicit UUID check, so the mock must pass the
        # value through unchanged (as the real sanitizer does for an
        # already-valid, already-stripped UUID string) rather than default
        # to an un-configured MagicMock.
        patch("nce.admin_handlers.agreements.validate_agent_id", side_effect=lambda x: x),
        patch(
            "nce.admin_handlers.agreements.require_agreements_enabled",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "nce.admin_handlers.agreements.do_coverage_matrix",
            new=AsyncMock(return_value=_COVERAGE_RESULT),
        ) as mock_core,
    ):
        mock_state.engine = MagicMock()
        req = _make_request(qp={"namespace_id": _NAMESPACE_ID, "since_iso": "2026-01-01"})
        resp = await api_agreements_coverage(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    assert data["coverage"] == _COVERAGE_RESULT
    assert data["kpis"] == {"leakage": 1, "expiry": 2, "review": 1}
    mock_core.assert_awaited_once()
    params = mock_core.await_args[0][1]
    assert params == {"namespace_id": _NAMESPACE_ID, "since_iso": "2026-01-01"}


@pytest.mark.asyncio
async def test_coverage_route_missing_namespace_id_returns_422():
    from nce.admin_handlers.agreements import api_agreements_coverage

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = MagicMock()
        resp = await api_agreements_coverage(_make_request(qp={}))

    assert resp.status_code == 422
    assert "error" in json.loads(resp.body)


@pytest.mark.asyncio
async def test_coverage_route_disabled_namespace_returns_409():
    from nce.admin_handlers.agreements import api_agreements_coverage

    with (
        patch("nce.admin_handlers.agreements.admin_state") as mock_state,
        # Batch: admin-surface sweep, Fix 1 — the REST handler now reassigns
        # namespace_id = validate_agent_id(namespace_id) (mirrors vendors.py)
        # before its own explicit UUID check, so the mock must pass the
        # value through unchanged (as the real sanitizer does for an
        # already-valid, already-stripped UUID string) rather than default
        # to an un-configured MagicMock.
        patch("nce.admin_handlers.agreements.validate_agent_id", side_effect=lambda x: x),
        patch(
            "nce.admin_handlers.agreements.require_agreements_enabled",
            new=AsyncMock(side_effect=AgreementsDisabledError("not enabled")),
        ),
    ):
        mock_state.engine = MagicMock()
        resp = await api_agreements_coverage(_make_request(qp={"namespace_id": _NAMESPACE_ID}))

    assert resp.status_code == 409
    assert "error" in json.loads(resp.body)


@pytest.mark.asyncio
async def test_coverage_route_503_when_engine_not_connected():
    from nce.admin_handlers.agreements import api_agreements_coverage

    with patch("nce.admin_handlers.agreements.admin_state") as mock_state:
        mock_state.engine = None
        resp = await api_agreements_coverage(_make_request(qp={"namespace_id": _NAMESPACE_ID}))

    assert resp.status_code == 503


def test_coverage_route_mounted_before_id_route():
    from nce.admin_app import build_admin_routes

    routes = build_admin_routes()
    paths = [r.path for r in routes]
    assert "/api/agreements/coverage" in paths
    # Starlette matches in order — /coverage must precede /{id} or "coverage"
    # would be captured as the id path parameter.
    assert paths.index("/api/agreements/coverage") < paths.index("/api/agreements/{id}")


# ---------------------------------------------------------------------------
# 3. _agreements_coverage_watcher_tick — cron watcher
# ---------------------------------------------------------------------------


def _patch_unmanaged(conn: MagicMock):
    @asynccontextmanager
    async def _fake_unmanaged(pool: Any, *, site: str):
        yield conn

    return patch("nce.cron.unmanaged_pg_connection", _fake_unmanaged)


@pytest.mark.asyncio
async def test_watcher_dispatches_grouped_alerts_for_expiry_and_leakage_only():
    from nce.cron import _agreements_coverage_watcher_tick

    ns_id = uuid.UUID(_NAMESPACE_ID)
    scan_conn = _make_conn([{"id": ns_id}])

    with (
        patch("nce.cron.acquire_cron_lock", new=AsyncMock(return_value=MagicMock())),
        patch("nce.cron.release_cron_lock", new=AsyncMock()) as mock_release,
        _patch_unmanaged(scan_conn),
        patch(
            "nce.vertical_modules.agreements.coverage.do_coverage_matrix",
            new=AsyncMock(return_value=_COVERAGE_RESULT),
        ) as mock_core,
        patch("nce.cron._dispatch_throttled_alert", new=AsyncMock()) as mock_alert,
    ):
        await _agreements_coverage_watcher_tick(MagicMock())

    mock_core.assert_awaited_once()
    assert mock_core.await_args[0][1] == {"namespace_id": ns_id}

    # Exactly one alert per (namespace, flag_type) group — storm control.
    keys = [c.args[0] for c in mock_alert.await_args_list]
    assert keys.count(f"agreements_coverage.{ns_id}.expiry") == 1
    assert keys.count(f"agreements_coverage.{ns_id}.leakage") == 1
    assert len(keys) == 2, f"Unexpected extra alerts: {keys}"
    # review flags NEVER alert — they are dashboard/queue-visible only.
    assert not any("review" in k for k in keys)

    expiry_call = next(c for c in mock_alert.await_args_list if c.args[0].endswith(".expiry"))
    assert expiry_call.args[1] == f"Agreements expiry alert: Namespace {ns_id}"
    assert "2 expiry flag(s)" in expiry_call.args[2]

    mock_release.assert_awaited_once()


@pytest.mark.asyncio
async def test_watcher_namespace_scan_filters_on_agreements_enabled():
    from nce.cron import _agreements_coverage_watcher_tick

    scan_conn = _make_conn([])

    with (
        patch("nce.cron.acquire_cron_lock", new=AsyncMock(return_value=MagicMock())),
        patch("nce.cron.release_cron_lock", new=AsyncMock()),
        _patch_unmanaged(scan_conn),
        patch(
            "nce.vertical_modules.agreements.coverage.do_coverage_matrix",
            new=AsyncMock(),
        ) as mock_core,
        patch("nce.cron._dispatch_throttled_alert", new=AsyncMock()) as mock_alert,
    ):
        await _agreements_coverage_watcher_tick(MagicMock())

    scan_sql = scan_conn.fetch.call_args[0][0]
    assert "metadata->'agreements'->>'enabled'" in scan_sql, (
        f"Namespace scan must filter on the agreements opt-in flag:\n{scan_sql}"
    )
    mock_core.assert_not_awaited()
    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_lock_held_returns_without_scanning():
    from nce.cron import _agreements_coverage_watcher_tick

    unmanaged = MagicMock()

    with (
        patch("nce.cron.acquire_cron_lock", new=AsyncMock(return_value=None)),
        patch("nce.cron.unmanaged_pg_connection", unmanaged),
        patch(
            "nce.vertical_modules.agreements.coverage.do_coverage_matrix",
            new=AsyncMock(),
        ) as mock_core,
        patch("nce.cron._dispatch_throttled_alert", new=AsyncMock()) as mock_alert,
    ):
        await _agreements_coverage_watcher_tick(MagicMock())

    unmanaged.assert_not_called()
    mock_core.assert_not_awaited()
    mock_alert.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Tool registry — flags
# ---------------------------------------------------------------------------


def test_agreements_lookup_terms_registered_with_correct_flags():
    from nce.tool_registry import CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY

    assert "agreements_lookup_terms" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["agreements_lookup_terms"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert spec.migration is False
    assert "agreements_lookup_terms" in CACHEABLE_TOOLS
    assert "agreements_lookup_terms" not in MUTATION_TOOLS
