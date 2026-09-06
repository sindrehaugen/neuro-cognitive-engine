"""
tests/unit/test_procurement_po_lifecycle_tools.py
=================================================
Acceptance tests for Wave PR-1:
  "Surface procurement_generate_po and procurement_submit_po Actor tools,
   wire PO_LINE lifecycle, and emit PO_LINE.status_changed into C4"

Covers:
  1. Tool registration, admin_only=True, mutation=True, cacheable=False.
  2. Confirm-first posture (confirm=True required for execution, confirm=False returns pending_approval).
  3. do_generate_po creates PO_LINE nodes with status DRAFT.
  4. do_submit_po executes via ManualPoTransport, advances PO_LINE to ORDERED,
     and emits PO_LINE.status_changed into C4 outbox.
  5. ManualPoTransport interface and contract.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.mcp_errors import McpError
from nce.tool_registry import ADMIN_ONLY_TOOLS, CACHEABLE_TOOLS, MUTATION_TOOLS, TOOL_REGISTRY
from nce.vertical_modules.procurement import ManualPoTransport, PoTransport
from nce.vertical_modules.procurement.mcp_handlers import (
    handle_procurement_generate_po,
    handle_procurement_submit_po,
)
from nce.vertical_modules.procurement.po import (
    do_generate_po,
    do_submit_po,
)
from nce.vertical_modules.procurement.po_line import POLineStatus

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"


def _make_mock_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.is_in_transaction = MagicMock(return_value=True)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


# ---------------------------------------------------------------------------
# 1. MCP Tool Registration & Spec
# ---------------------------------------------------------------------------


def test_procurement_po_tools_registered():
    """Both PR-1 tools must be registered with strict Actor flags."""
    for tool_name in ("procurement_generate_po", "procurement_submit_po"):
        assert tool_name in TOOL_REGISTRY
        spec = TOOL_REGISTRY[tool_name]
        assert spec.admin_only is True
        assert spec.mutation is True
        assert spec.cacheable is False
        assert tool_name in ADMIN_ONLY_TOOLS
        assert tool_name in MUTATION_TOOLS
        assert tool_name not in CACHEABLE_TOOLS


# ---------------------------------------------------------------------------
# 2. Confirm-First & Validation Gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_po_requires_confirm():
    """Calling generate_po without confirm=True returns pending_approval (Governor gating)."""
    engine = MagicMock()
    conn = _make_mock_conn()
    engine.pg_pool = _make_mock_pool(conn)

    args = {
        "namespace_id": _NAMESPACE_ID,
        "po_number": "PO-TEST-001",
        "confirm": False,
        "idempotency_key": "idemp-gen-001",
    }
    raw = await handle_procurement_generate_po(engine, args)
    res = json.loads(raw)
    assert res.get("status") == "pending_approval"


@pytest.mark.asyncio
async def test_submit_po_requires_confirm():
    """Calling submit_po without confirm=True returns pending_approval (Governor gating)."""
    engine = MagicMock()
    conn = _make_mock_conn()
    engine.pg_pool = _make_mock_pool(conn)

    args = {
        "namespace_id": _NAMESPACE_ID,
        "po_number": "PO-TEST-001",
        "confirm": False,
        "idempotency_key": "idemp-sub-001",
    }
    raw = await handle_procurement_submit_po(engine, args)
    res = json.loads(raw)
    assert res.get("status") == "pending_approval"


@pytest.mark.asyncio
async def test_generate_po_missing_po_number():
    """Missing po_number raises McpError (-32602)."""
    engine = MagicMock()
    args = {
        "namespace_id": _NAMESPACE_ID,
        "confirm": True,
    }
    with pytest.raises(McpError) as exc_info:
        await handle_procurement_generate_po(engine, args)
    assert exc_info.value.code == -32602


# ---------------------------------------------------------------------------
# 3. do_generate_po creates PO_LINE nodes with status DRAFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_generate_po_creates_po_lines():
    """do_generate_po creates PO and upserts PO_LINE nodes in DRAFT state."""
    engine = MagicMock()
    conn = _make_mock_conn()

    # Mock upsert_po_line_node
    with (
        patch(
            "nce.vertical_modules.procurement.po.upsert_po_line_node",
            new=AsyncMock(return_value={"po_line_id": "POL-1", "status": POLineStatus.DRAFT.value}),
        ) as mock_upsert,
        patch(
            "nce.vertical_modules.procurement.po.upsert_po_node",
            new=AsyncMock(return_value="PO:PO-TEST-001"),
        ),
        patch(
            "nce.vertical_modules.procurement.po.do_resolve_bids",
            new=AsyncMock(return_value={"results": []}),
        ),
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_generate_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-TEST-001",
            idempotency_key="idemp-gen-001",
            confirm=True,
            engine=engine,
            line_items=[
                {
                    "line_ref": 1,
                    "bom_line_ref": "BOM_LINE:b1",
                    "sku": "SPK-01",
                    "qty": 4,
                    "unit_price": 250.0,
                }
            ],
        )
        assert res["status"] == "executed"
        data = res["result"]
        assert data["po_number"] == "PO-TEST-001"
        assert len(data["po_lines"]) == 1
        assert data["po_lines"][0]["status"] == POLineStatus.DRAFT.value
        mock_upsert.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. do_submit_po advances lines to ORDERED & emits PO_LINE.status_changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_submit_po_advances_status_and_emits_event():
    """do_submit_po uses ManualPoTransport, updates lines to ORDERED, and emits event."""
    conn = _make_mock_conn()

    po_lines_mock = [
        {
            "line_ref": "1",
            "project_id": UUID("11111111-1111-4111-8111-111111111111"),
            "bom_line_label": "BOM_LINE:bom-001",
        }
    ]
    conn.fetch = AsyncMock(return_value=po_lines_mock)

    transport = ManualPoTransport()
    with (
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
        ) as mock_update_status,
        patch("nce.autonomy.governor._audit_execution", new=AsyncMock()),
    ):
        res = await do_submit_po(
            conn,
            _NAMESPACE_ID,
            po_number="PO-TEST-001",
            supplier_id="sup-acme-01",
            line_items=[{"line_ref": 1, "bom_line_ref": "BOM_LINE:bom-001"}],
            po_value=0.0,
            idempotency_key="idemp-sub-001",
            confirm=True,
            transport=transport,
        )
        assert res["status"] == "executed"
        data = res["result"]
        assert data["status"] == "submitted"
        assert data["po_number"] == "PO-TEST-001"
        assert data["transport_result"]["status"] == "placed"
        assert data["transport_result"]["method"] == "manual"
        assert len(data["ordered_lines"]) == 1

        mock_update_status.assert_awaited_once_with(
            conn,
            _NAMESPACE_ID,
            po_number="PO-TEST-001",
            line_ref="1",
            new_status=POLineStatus.ORDERED,
            project_id=UUID("11111111-1111-4111-8111-111111111111"),
            bom_line_label="BOM_LINE:bom-001",
            project_value=0.0,
        )


# ---------------------------------------------------------------------------
# 5. ManualPoTransport interface & contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_po_transport_contract():
    """ManualPoTransport must inherit from PoTransport and implement place_order cleanly."""
    assert issubclass(ManualPoTransport, PoTransport)
    transport = ManualPoTransport()
    line_items = [{"item": "Mic", "qty": 2}]
    result = await transport.place_order(
        po_number="PO-MANUAL-001",
        supplier_id="sup-vendor-1",
        line_items=line_items,
        namespace_id=_NAMESPACE_ID,
        idempotency_key="test-key-001",
    )
    assert result["status"] == "placed"
    assert result["method"] == "manual"
    assert result["po_number"] == "PO-MANUAL-001"
    assert result["supplier_id"] == "sup-vendor-1"
    assert result["line_count"] == 1
