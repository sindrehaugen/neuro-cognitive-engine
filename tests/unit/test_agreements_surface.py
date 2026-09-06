"""
tests/unit/test_agreements_surface.py
======================================
Acceptance tests for Wave AG-2 (Agreements Surface Completion).

Covers:
  1. All 10 agreements MCP tools registered in TOOL_REGISTRY with exact flags.
  2. MCP handler opt-in enforcement: non-opted-in namespace raises McpError(-32005).
  3. MCP handler missing namespace_id raises McpError.
  4. MCP handler execution on valid payloads with mocked engine/DB:
     - handle_agreements_coverage_matrix
     - handle_agreements_reconcile_kickback
     - handle_agreements_run_compliance_audit
     - handle_agreements_extract
     - handle_agreements_create
     - handle_agreements_suggest_revision
     - handle_agreements_request_signature
     - handle_agreements_record_signature
     - handle_agreements_review_extraction
  5. Admin REST routes:
     - api_agreements_reconcile
     - api_agreements_create
     - api_agreements_suggest_revision
     - api_agreements_comment
     - api_agreements_request_signature
     - api_agreements_record_signature
     - api_agreements_compliance_audit
     - api_agreements_suggest_terms
     - api_agreements_sla_coverage
     - api_agreements_upsert
     - 422 on missing/malformed namespace_id
     - 409 on disabled namespace
     - 200/ok on valid request
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers import agreements as admin_agreements
from nce.mcp_errors import McpError
from nce.tool_registry import ADMIN_ONLY_TOOLS, CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.agreements import mcp_handlers

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_AGREEMENT_ID = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_engine_disabled() -> MagicMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"agreements_enabled": False})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _make_engine_enabled() -> MagicMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"agreements_enabled": True})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


def _patch_guard_ok():
    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.require_agreements_enabled",
        new=AsyncMock(return_value=None),
    )


def _patch_scoped_session(conn: MagicMock):
    @asynccontextmanager
    async def _fake_session(pool: Any, namespace_id: Any):
        yield conn

    return patch(
        "nce.vertical_modules.agreements.mcp_handlers.scoped_pg_session",
        _fake_session,
    )


def _make_request(
    data: dict[str, Any] | None = None,
    query_params: dict[str, str] | None = None,
    method: str = "POST",
) -> MagicMock:
    req = MagicMock()
    req.method = method
    req.query_params = query_params or {}
    if data is not None:
        req.json = AsyncMock(return_value=data)
    else:
        req.json = AsyncMock(side_effect=Exception("No JSON body"))
    return req


# ---------------------------------------------------------------------------
# 1. TOOL_REGISTRY registrations and flags
# ---------------------------------------------------------------------------


def test_agreements_all_tools_registered() -> None:
    expected_tools = {
        "agreements_lookup_terms",
        "agreements_coverage_matrix",
        "agreements_reconcile_kickback",
        "agreements_run_compliance_audit",
        "agreements_extract",
        "agreements_create",
        "agreements_suggest_revision",
        "agreements_request_signature",
        "agreements_record_signature",
        "agreements_review_extraction",
    }
    for name in expected_tools:
        assert name in TOOL_REGISTRY, f"Tool {name} missing from TOOL_REGISTRY"


def test_agreements_tool_flags() -> None:
    cacheable_expected = {
        "agreements_lookup_terms",
        "agreements_coverage_matrix",
        "agreements_reconcile_kickback",
        "agreements_run_compliance_audit",
    }
    mutation_expected = {
        "agreements_extract",
        "agreements_create",
        "agreements_suggest_revision",
        "agreements_request_signature",
        "agreements_record_signature",
        "agreements_review_extraction",
    }
    admin_only_expected = mutation_expected

    for name in cacheable_expected:
        spec = TOOL_REGISTRY[name]
        assert spec.cacheable is True, f"{name} should be cacheable"
        assert spec.mutation is False, f"{name} should not be mutation"
        assert spec.admin_only is False, f"{name} should not be admin_only"
        assert name in CACHEABLE_TOOLS

    for name in mutation_expected:
        spec = TOOL_REGISTRY[name]
        assert spec.cacheable is False, f"{name} should not be cacheable"
        assert spec.mutation is True, f"{name} should be mutation"
        assert name in MUTATION_TOOLS

    for name in admin_only_expected:
        spec = TOOL_REGISTRY[name]
        assert spec.admin_only is True, f"{name} should be admin_only"
        assert name in ADMIN_ONLY_TOOLS


# ---------------------------------------------------------------------------
# 2. MCP Handlers opt-in enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_agreements_coverage_matrix",
        "handle_agreements_reconcile_kickback",
        "handle_agreements_run_compliance_audit",
        "handle_agreements_extract",
        "handle_agreements_create",
        "handle_agreements_suggest_revision",
        "handle_agreements_request_signature",
        "handle_agreements_record_signature",
        "handle_agreements_review_extraction",
    ],
)
async def test_mcp_handler_disabled_namespace(handler_name: str) -> None:
    handler = getattr(mcp_handlers, handler_name)
    engine = _make_engine_disabled()
    with pytest.raises(McpError) as exc_info:
        await handler(engine, {"namespace_id": _NAMESPACE_ID})
    assert exc_info.value.code == -32005
    assert exc_info.value.data["reason"] == "agreements_disabled"


# ---------------------------------------------------------------------------
# 3. MCP Handlers missing namespace_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_agreements_coverage_matrix",
        "handle_agreements_reconcile_kickback",
        "handle_agreements_run_compliance_audit",
        "handle_agreements_extract",
        "handle_agreements_create",
        "handle_agreements_suggest_revision",
        "handle_agreements_request_signature",
        "handle_agreements_record_signature",
        "handle_agreements_review_extraction",
    ],
)
async def test_mcp_handler_missing_namespace_id(handler_name: str) -> None:
    handler = getattr(mcp_handlers, handler_name)
    engine = _make_engine_enabled()
    with pytest.raises(McpError) as exc_info:
        await handler(engine, {})
    assert exc_info.value.code in (-32602, -32603)


# ---------------------------------------------------------------------------
# 4. MCP Handlers execution with mocked core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agreements_coverage_matrix_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_coverage_matrix",
            new=AsyncMock(return_value={"status": "ok", "flags": []}),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_coverage_matrix(
            engine, {"namespace_id": _NAMESPACE_ID}
        )
        data = json.loads(raw)
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_agreements_reconcile_kickback_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_reconcile_kickback",
            new=AsyncMock(return_value={"status": "ok", "payout_amount_nok": 1500.0}),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_reconcile_kickback(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "spend_nok": 50000.0,
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["payout_amount_nok"] == 1500.0


@pytest.mark.asyncio
async def test_handle_agreements_run_compliance_audit_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_run_compliance_audit",
            new=AsyncMock(return_value={"status": "ok", "approved": True, "reasons": []}),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_run_compliance_audit(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "po_number": "PO-9999",
                "supplier_id": "vendor-1",
                "rebate_amount": 1000.0,
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["approved"] is True


@pytest.mark.asyncio
async def test_handle_agreements_extract_success() -> None:
    engine = _make_engine_enabled()
    conn = AsyncMock()
    with (
        _patch_guard_ok(),
        _patch_scoped_session(conn),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_extract_agreement",
            new=AsyncMock(
                return_value={
                    "validFrom": {
                        "value": "2026-01-01",
                        "extractionConfidence": 95.0,
                        "reviewStatus": "auto_green",
                    }
                }
            ),
        ),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.write_agreement_to_graph_and_memories",
            new=AsyncMock(return_value=None),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_extract(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "source_doc_ref": "minio://docs/contract.pdf",
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert "agreement_id" in data


@pytest.mark.asyncio
async def test_handle_agreements_create_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_create_agreement",
            new=AsyncMock(return_value={"status": "ok", "agreement_id": _AGREEMENT_ID}),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_create(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "title": "Master Services Agreement",
                "terms": {"payment_terms_days": 30},
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["agreement_id"] == _AGREEMENT_ID


@pytest.mark.asyncio
async def test_handle_agreements_suggest_revision_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_suggest_revision",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "agreement_id": _AGREEMENT_ID,
                    "suggestion_id": "sug-1",
                    "applied": False,
                }
            ),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_suggest_revision(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "field": "paymentTermsDays",
                "proposed_value": 45,
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["suggestion_id"] == "sug-1"


@pytest.mark.asyncio
async def test_handle_agreements_request_signature_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_request_signature",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "agreement_id": _AGREEMENT_ID,
                    "envelope_id": "env-1",
                    "signing_status": "sent",
                }
            ),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_request_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "document": "contract text content",
                "signer": "bob@example.com",
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["envelope_id"] == "env-1"


@pytest.mark.asyncio
async def test_handle_agreements_record_signature_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_record_signature",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "agreement_id": _AGREEMENT_ID,
                    "signing_status": "signed",
                }
            ),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_record_signature(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "session_id": "sess-1",
                "signed_document": "signed contract text content",
                "signer": "bob@example.com",
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["signing_status"] == "signed"


@pytest.mark.asyncio
async def test_handle_agreements_review_extraction_success() -> None:
    engine = _make_engine_enabled()
    with (
        _patch_guard_ok(),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.do_review_extraction",
            new=AsyncMock(
                return_value={
                    "status": "ok",
                    "agreement_id": _AGREEMENT_ID,
                    "decision": "confirm",
                    "source_doc_ref": "doc-1",
                    "extracted": {},
                }
            ),
        ),
        patch(
            "nce.vertical_modules.agreements.mcp_handlers.write_agreement_to_graph_and_memories",
            new=AsyncMock(return_value=None),
        ),
    ):
        raw = await mcp_handlers.handle_agreements_review_extraction(
            engine,
            {
                "namespace_id": _NAMESPACE_ID,
                "agreement_id": _AGREEMENT_ID,
                "decision": "confirm",
                "reviewed_by": "auditor@example.com",
            },
        )
        data = json.loads(raw)
        assert data["status"] == "ok"
        assert data["agreement"]["decision"] == "confirm"


# ---------------------------------------------------------------------------
# 5. REST route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route_fn_name",
    [
        "api_agreements_reconcile",
        "api_agreements_create",
        "api_agreements_suggest_revision",
        "api_agreements_comment",
        "api_agreements_request_signature",
        "api_agreements_record_signature",
        "api_agreements_compliance_audit",
        "api_agreements_suggest_terms",
        "api_agreements_sla_coverage",
        "api_agreements_upsert",
    ],
)
async def test_rest_routes_missing_namespace_id(route_fn_name: str) -> None:
    fn = getattr(admin_agreements, route_fn_name)
    req = _make_request(data={})
    with patch("nce.admin_handlers.agreements.admin_state.engine", _make_engine_enabled()):
        res = await fn(req)
    assert res.status_code == 422
    body = json.loads(res.body)
    assert "error" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route_fn_name",
    [
        "api_agreements_reconcile",
        "api_agreements_create",
        "api_agreements_suggest_revision",
        "api_agreements_comment",
        "api_agreements_request_signature",
        "api_agreements_record_signature",
        "api_agreements_compliance_audit",
        "api_agreements_suggest_terms",
        "api_agreements_sla_coverage",
        "api_agreements_upsert",
    ],
)
async def test_rest_routes_malformed_namespace_id(route_fn_name: str) -> None:
    fn = getattr(admin_agreements, route_fn_name)
    req = _make_request(data={"namespace_id": "not-a-valid-uuid"})
    with patch("nce.admin_handlers.agreements.admin_state.engine", _make_engine_enabled()):
        res = await fn(req)
    assert res.status_code == 422
    body = json.loads(res.body)
    assert "Invalid namespace_id" in body.get("error", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route_fn_name",
    [
        "api_agreements_reconcile",
        "api_agreements_create",
        "api_agreements_suggest_revision",
        "api_agreements_comment",
        "api_agreements_request_signature",
        "api_agreements_record_signature",
        "api_agreements_compliance_audit",
        "api_agreements_suggest_terms",
        "api_agreements_sla_coverage",
        "api_agreements_upsert",
    ],
)
async def test_rest_routes_disabled_namespace(route_fn_name: str) -> None:
    fn = getattr(admin_agreements, route_fn_name)
    req = _make_request(data={"namespace_id": _NAMESPACE_ID})
    with patch("nce.admin_handlers.agreements.admin_state.engine", _make_engine_disabled()):
        res = await fn(req)
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_rest_routes_success_execution() -> None:
    engine = _make_engine_enabled()

    with (
        patch("nce.admin_handlers.agreements.admin_state.engine", engine),
        patch(
            "nce.admin_handlers.agreements._check_agreements_enabled_rest",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "nce.admin_handlers.agreements.do_reconcile_kickback",
            new=AsyncMock(return_value={"payout_nok": 500.0}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_create_agreement",
            new=AsyncMock(return_value={"agreement_id": _AGREEMENT_ID}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_suggest_revision",
            new=AsyncMock(return_value={"suggestion_id": "sug-1"}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_add_comment",
            new=AsyncMock(return_value={"comment_id": "comm-1"}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_request_signature",
            new=AsyncMock(return_value={"envelope_id": "env-1"}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_record_signature",
            new=AsyncMock(return_value={"signing_status": "signed"}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_run_compliance_audit",
            new=AsyncMock(return_value={"approved": True}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_suggest_terms",
            new=AsyncMock(return_value={"recommendations": []}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_set_sla_coverage",
            new=AsyncMock(return_value={"coverage_edge": "covers"}),
        ),
        patch(
            "nce.admin_handlers.agreements.do_upsert_agreement",
            new=AsyncMock(return_value={"agreement_id": _AGREEMENT_ID}),
        ),
    ):
        # 1. reconcile
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_reconcile(req)
        assert res.status_code == 200
        assert json.loads(res.body)["payout_nok"] == 500.0

        # 2. create
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "title": "Test Agreement"})
        res = await admin_agreements.api_agreements_create(req)
        assert res.status_code == 200
        assert json.loads(res.body)["agreement_id"] == _AGREEMENT_ID

        # 3. suggest-revision
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_suggest_revision(req)
        assert res.status_code == 200
        assert json.loads(res.body)["suggestion_id"] == "sug-1"

        # 4. comment
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_comment(req)
        assert res.status_code == 200
        assert json.loads(res.body)["comment_id"] == "comm-1"

        # 5. request-signature
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_request_signature(req)
        assert res.status_code == 200
        assert json.loads(res.body)["envelope_id"] == "env-1"

        # 6. record-signature
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_record_signature(req)
        assert res.status_code == 200
        assert json.loads(res.body)["signing_status"] == "signed"

        # 7. compliance-audit
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "supplier_id": "v-1"})
        res = await admin_agreements.api_agreements_compliance_audit(req)
        assert res.status_code == 200
        assert json.loads(res.body)["approved"] is True

        # 8. suggest-terms
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_suggest_terms(req)
        assert res.status_code == 200
        assert "recommendations" in json.loads(res.body)

        # 9. sla-coverage
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_sla_coverage(req)
        assert res.status_code == 200
        assert json.loads(res.body)["coverage_edge"] == "covers"

        # 10. upsert
        req = _make_request(data={"namespace_id": _NAMESPACE_ID, "agreement_id": _AGREEMENT_ID})
        res = await admin_agreements.api_agreements_upsert(req)
        assert res.status_code == 200
        assert json.loads(res.body)["agreement_id"] == _AGREEMENT_ID
