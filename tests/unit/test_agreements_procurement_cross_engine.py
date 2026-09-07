"""
tests/unit/test_agreements_procurement_cross_engine.py
======================================================
Unit tests for Wave AG-3 / PR-5:
  Cross-engine wiring of Agreements compliance audit into Procurement's
  rebate_override gate in do_submit_po via engine.modules.

Covers:
  1. rebate_override=True with engine.modules["agreements"] approving -> proceeds to transport.
  2. rebate_override=True with engine.modules["agreements"] rejecting -> fails closed (pending_approval),
     transport NOT called, idempotency key NOT burned.
  3. rebate_override=True with Agreements engine disabled for namespace -> EngineDisabledError caught,
     records degradation (agreements_module_disabled), fails closed (pending_approval).
  4. rebate_override=True with Agreements engine not found -> fails closed (pending_approval).
  5. rebate_override=True with neither engine.modules nor a2a_client -> fails closed (pending_approval).
  6. Backward compatibility: existing a2a_client paths remain operational when engine is absent.
  7. handle_procurement_submit_po MCP handler threads engine down to do_submit_po.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.engine_registry import EngineRegistry
from nce.vertical_modules.procurement import ManualPoTransport
from nce.vertical_modules.procurement.mcp_handlers import handle_procurement_submit_po
from nce.vertical_modules.procurement.po import do_submit_po
from nce.vertical_modules.procurement.po_line import POLineStatus

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_mock_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.is_in_transaction = MagicMock(return_value=True)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetch = AsyncMock(
        return_value=[
            {
                "line_ref": "1",
                "project_id": UUID("11111111-1111-4111-8111-111111111111"),
                "bom_line_label": "BOM_LINE:bom-001",
            }
        ]
    )
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    return conn


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


@pytest.mark.asyncio
async def test_rebate_override_with_engine_modules_approves_proceeds_to_transport():
    """When engine.modules['agreements'] approves the rebate override, order executes."""
    conn = _make_mock_conn()
    transport = ManualPoTransport()

    mock_agreements = SimpleNamespace()
    mock_agreements.do_run_compliance_audit = AsyncMock(
        return_value={"approved": True, "reason": "signed contract allows kickback"}
    )

    registry = EngineRegistry()
    registry.register("agreements", mock_agreements)

    engine = SimpleNamespace(modules=registry)

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
        patch(
            "nce.vertical_modules.procurement.po.update_po_line_status",
            new=AsyncMock(
                return_value={
                    "po_line_id": "line-uuid-1",
                    "status": POLineStatus.ORDERED.value,
                    "project_id": "11111111-1111-4111-8111-111111111111",
                    "bom_line_label": "BOM_LINE:bom-001",
                }
            ),
        ),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-APP-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-app-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=5000.0,
            transport=transport,
            engine=engine,
        )

    assert res["status"] == "executed"
    mock_agreements.do_run_compliance_audit.assert_awaited_once_with(
        engine,
        {
            "namespace_id": _NAMESPACE_ID,
            "po_number": "PO-REBATE-APP-001",
            "supplier_id": "sup-crestron",
            "rebate_amount": 5000.0,
        },
    )


@pytest.mark.asyncio
async def test_rebate_override_with_engine_modules_rejects_fails_closed():
    """When engine.modules['agreements'] rejects the rebate override, fail-closed to pending_approval."""
    conn = _make_mock_conn()
    transport = MagicMock()
    transport.place_order = AsyncMock()

    mock_agreements = SimpleNamespace()
    mock_agreements.do_run_compliance_audit = AsyncMock(
        return_value={"approved": False, "reason": "kickback exceeds max authorized margin"}
    )

    registry = EngineRegistry()
    registry.register("agreements", mock_agreements)

    engine = SimpleNamespace(modules=registry)

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-REJ-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-rej-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=50000.0,
            transport=transport,
            engine=engine,
        )

    assert res["status"] == "pending_approval"
    assert "rebate_override compliance check failed" in res["reason"]
    assert "kickback exceeds max authorized margin" in res["reason"]
    transport.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_rebate_override_engine_disabled_records_degradation_and_fails_closed(monkeypatch):
    """When Agreements is disabled for namespace, fail closed and record degradation."""
    conn = _make_mock_conn()
    transport = MagicMock()
    transport.place_order = AsyncMock()

    mock_agreements = SimpleNamespace()
    registry = EngineRegistry()
    registry.register("agreements", mock_agreements)
    registry.disable_for_namespace(_NAMESPACE_ID, "agreements")

    engine = SimpleNamespace(modules=registry)

    degradations = []

    def _mock_record_degradation(**kwargs):
        degradations.append(kwargs)

    monkeypatch.setattr("nce.degradation.record_degradation", _mock_record_degradation)

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-DIS-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-dis-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=1000.0,
            transport=transport,
            engine=engine,
        )

    assert res["status"] == "pending_approval"
    assert "disabled" in res["reason"].lower()
    transport.place_order.assert_not_called()

    assert len(degradations) >= 1
    deg = degradations[0]
    assert deg["engine"] == "procurement"
    assert deg["code"] == "agreements_module_disabled"
    assert "disabled" in deg["detail"].lower()


@pytest.mark.asyncio
async def test_rebate_override_engine_not_found_fails_closed():
    """When Agreements is not in engine registry, fail closed."""
    conn = _make_mock_conn()
    transport = MagicMock()
    transport.place_order = AsyncMock()

    registry = EngineRegistry()
    engine = SimpleNamespace(modules=registry)

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-NOTFOUND-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-nf-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=1000.0,
            transport=transport,
            engine=engine,
        )

    assert res["status"] == "pending_approval"
    assert "not found" in res["reason"].lower()
    transport.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_rebate_override_no_client_and_no_engine_fails_closed():
    """When neither engine.modules nor a2a_client is provided, fail closed."""
    conn = _make_mock_conn()
    transport = MagicMock()
    transport.place_order = AsyncMock()

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-NONE-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-none-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=1000.0,
            transport=transport,
            a2a_client=None,
            engine=None,
        )

    assert res["status"] == "pending_approval"
    assert "not provided" in res["reason"].lower()
    transport.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_rebate_override_a2a_fallback_preserved():
    """When a2a_client is provided without engine.modules, A2A path functions normally."""
    conn = _make_mock_conn()
    transport = ManualPoTransport()

    mock_a2a = MagicMock()
    mock_a2a.call_tool = AsyncMock(return_value={"approved": True, "note": "ok"})

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
        patch(
            "nce.vertical_modules.procurement.po.update_po_line_status",
            new=AsyncMock(
                return_value={
                    "po_line_id": "line-uuid-1",
                    "status": POLineStatus.ORDERED.value,
                    "project_id": "11111111-1111-4111-8111-111111111111",
                    "bom_line_label": "BOM_LINE:bom-001",
                }
            ),
        ),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-REBATE-A2A-001",
            supplier_id="sup-crestron",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-a2a-001",
            confirm=True,
            rebate_override=True,
            rebate_amount=1000.0,
            transport=transport,
            a2a_client=mock_a2a,
            engine=None,
        )

    assert res["status"] == "executed"
    mock_a2a.call_tool.assert_awaited_once_with(
        "agreements.compliance_audit",
        {
            "po_number": "PO-REBATE-A2A-001",
            "supplier_id": "sup-crestron",
            "rebate_amount": 1000.0,
            "namespace_id": _NAMESPACE_ID,
        },
    )


@pytest.mark.asyncio
async def test_handle_procurement_submit_po_threads_engine():
    """MCP handler handle_procurement_submit_po threads engine to do_submit_po."""
    conn = _make_mock_conn()
    pool = _make_mock_pool(conn)

    mock_agreements = SimpleNamespace()
    mock_agreements.do_run_compliance_audit = AsyncMock(
        return_value={"approved": True, "reason": "approved by contract"}
    )

    registry = EngineRegistry()
    registry.register("agreements", mock_agreements)

    engine = SimpleNamespace(
        pg_pool=pool,
        redis_pool=None,
        modules=registry,
    )

    args = {
        "namespace_id": _NAMESPACE_ID,
        "po_number": "PO-MCP-REBATE-001",
        "supplier_id": "sup-crestron",
        "line_items": [{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
        "po_value": 0.0,
        "idempotency_key": "idemp-mcp-001",
        "confirm": True,
        "rebate_override": True,
        "rebate_amount": 2500.0,
        "transport_method": "manual",
    }

    with (
        patch("nce.vertical_modules.procurement.po._audit_rebate_decision", new=AsyncMock()),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
        patch(
            "nce.vertical_modules.procurement.po.update_po_line_status",
            new=AsyncMock(
                return_value={
                    "po_line_id": "line-uuid-1",
                    "status": POLineStatus.ORDERED.value,
                    "project_id": "11111111-1111-4111-8111-111111111111",
                    "bom_line_label": "BOM_LINE:bom-001",
                }
            ),
        ),
    ):
        raw = await handle_procurement_submit_po(engine, args)
        res = json.loads(raw)

    assert res["status"] == "executed"
    mock_agreements.do_run_compliance_audit.assert_awaited_once()
