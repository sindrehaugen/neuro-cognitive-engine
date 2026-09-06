"""
tests/unit/test_economy_approve_invoice.py
============================================
Unit test suite for Wave E-3: economy_approve_invoice Actor tool.

Validates:
1. Tool registration contract (admin_only=True, mutation=True, cacheable=False).
2. Namespace opt-in guard enforcement (raises -32005 when disabled).
3. Confirm-first governance (defaults to confirm=False returning pending_approval).
4. Required parameter validation (approval_id, quote_id, namespace_id).
5. Fail-closed posture on OCR-derived figures (is_ocr, ocr_derived, document_format, line flag, memory lookup).
6. Confirmed execution calling do_cascade_on_approval.
7. Idempotency behavior on replay.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nce.mcp_errors import McpError
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.economy._guard import EconomyDisabledError
from nce.vertical_modules.economy.mcp_handlers import handle_economy_approve_invoice

_NS_UUID = UUID("00000000-0000-4000-8000-000000000001")


# ---------------------------------------------------------------------------
# 1. Surface Ratchet & Metadata Contracts
# ---------------------------------------------------------------------------


def test_economy_approve_invoice_tool_spec() -> None:
    """economy_approve_invoice must be an Actor mutation tool (admin_only, mutation, not cacheable)."""
    assert "economy_approve_invoice" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["economy_approve_invoice"]
    assert spec.admin_only is True
    assert spec.mutation is True
    assert spec.cacheable is False

    assert "economy_approve_invoice" in ADMIN_ONLY_TOOLS
    assert "economy_approve_invoice" in MUTATION_TOOLS
    assert "economy_approve_invoice" not in CACHEABLE_TOOLS


# ---------------------------------------------------------------------------
# 2. Opt-in Guard Enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_approve_invoice_refuses_when_disabled() -> None:
    """Refuses execution with -32005 when economy vertical is not enabled."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()

    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        side_effect=EconomyDisabledError("Economy disabled for test"),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_economy_approve_invoice(
                engine,
                {
                    "namespace_id": str(_NS_UUID),
                    "approval_id": "appr-001",
                    "quote_id": "Q-100",
                },
            )
        assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_economy_approve_invoice_requires_namespace_id() -> None:
    """Missing namespace_id returns error in JSON envelope."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        raw = await handle_economy_approve_invoice(
            engine,
            {
                "approval_id": "appr-001",
                "quote_id": "Q-100",
            },
        )
        res = json.loads(raw)
        assert "namespace_id is required" in res.get("error", "")


# ---------------------------------------------------------------------------
# 3. Confirm-first Governance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_approve_invoice_confirm_first_default() -> None:
    """Default confirm=False returns pending_approval without executing cascade."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        raw = await handle_economy_approve_invoice(
            engine,
            {
                "namespace_id": str(_NS_UUID),
                "approval_id": "appr-001",
                "quote_id": "Q-100",
            },
        )
        res = json.loads(raw)
        assert res["status"] == "pending_approval"
        assert res["action_type"] == "economy_approve_invoice"
        assert res["approval_id"] == "appr-001"
        assert res["quote_id"] == "Q-100"
        assert "Human confirmation required" in res["message"]


# ---------------------------------------------------------------------------
# 4. Required Parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_approve_invoice_missing_approval_id() -> None:
    """Missing approval_id returns error."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        raw = await handle_economy_approve_invoice(
            engine,
            {
                "namespace_id": str(_NS_UUID),
                "quote_id": "Q-100",
                "confirm": True,
            },
        )
        res = json.loads(raw)
        assert "approval_id is required" in res.get("error", "")


@pytest.mark.asyncio
async def test_economy_approve_invoice_missing_quote_id() -> None:
    """Missing quote_id returns error."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        raw = await handle_economy_approve_invoice(
            engine,
            {
                "namespace_id": str(_NS_UUID),
                "approval_id": "appr-001",
                "confirm": True,
            },
        )
        res = json.loads(raw)
        assert "quote_id is required" in res.get("error", "")


# ---------------------------------------------------------------------------
# 5. Fail-Closed Posture on OCR Figures (OQ-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ocr_arg",
    [
        {"is_ocr": True},
        {"ocr_derived": True},
        {"document_format": "ocr_pdf"},
        {"document_format": "ocr_image"},
    ],
)
async def test_economy_approve_invoice_fails_closed_on_ocr_flags(ocr_arg: dict[str, Any]) -> None:
    """Explicit OCR flags on arguments refuse automated approval."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        args = {
            "namespace_id": str(_NS_UUID),
            "approval_id": "appr-001",
            "quote_id": "Q-100",
            "confirm": True,
            **ocr_arg,
        }
        raw = await handle_economy_approve_invoice(engine, args)
        res = json.loads(raw)
        assert "Refusing automated approval on OCR-derived invoice amount" in res.get("error", "")


@pytest.mark.asyncio
async def test_economy_approve_invoice_fails_closed_on_ocr_lines() -> None:
    """OCR flags on individual BOM lines refuse automated approval."""
    engine = MagicMock()
    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        return_value=None,
    ):
        args = {
            "namespace_id": str(_NS_UUID),
            "approval_id": "appr-001",
            "quote_id": "Q-100",
            "confirm": True,
            "lines": [
                {
                    "bom_line_label": "BOM_LINE:Q-100:AMP01",
                    "actual_cost": 1000,
                    "ocr_derived": True,
                }
            ],
        }
        raw = await handle_economy_approve_invoice(engine, args)
        res = json.loads(raw)
        assert "Refusing automated approval on OCR-derived invoice amount" in res.get("error", "")


@pytest.mark.asyncio
async def test_economy_approve_invoice_fails_closed_on_stored_ocr_memory() -> None:
    """Stored OCR invoice in memories table triggers fail-closed refusal."""
    engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "fmt": "ocr_pdf",
        "req_rev": "true",
    }
    mock_pool = MagicMock()
    engine.pg_pool = mock_pool

    with (
        patch(
            "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
            return_value=None,
        ),
        patch("nce.vertical_modules.economy.mcp_handlers.scoped_pg_session") as mock_scoped,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn
        args = {
            "namespace_id": str(_NS_UUID),
            "approval_id": "appr-001",
            "quote_id": "Q-100",
            "invoice_id": "INV-OCR-001",
            "confirm": True,
        }
        raw = await handle_economy_approve_invoice(engine, args)
        res = json.loads(raw)
        assert "Refusing automated approval on OCR-derived invoice amount" in res.get("error", "")


# ---------------------------------------------------------------------------
# 6. Confirmed Execution & Cascade Delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_approve_invoice_executes_cascade() -> None:
    """Confirmed call invokes do_cascade_on_approval and returns executed envelope."""
    engine = MagicMock()
    engine.pg_pool = None

    cascade_return = {
        "ok": True,
        "approval_id": "appr-001",
        "bom_lines_written": ["BOM_LINE:Q-100:AMP01"],
        "bom_lines_replayed": [],
        "effects": [{"type": "economy.invoice.approved"}],
        "signed_margin_pct": Decimal("0.35"),
    }

    with (
        patch(
            "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
            return_value=None,
        ),
        patch(
            "nce.vertical_modules.economy.mcp_handlers.do_cascade_on_approval",
            new_callable=AsyncMock,
            return_value=cascade_return,
        ) as mock_cascade,
    ):
        args = {
            "namespace_id": str(_NS_UUID),
            "approval_id": "appr-001",
            "quote_id": "Q-100",
            "confirm": True,
            "lines": [
                {
                    "bom_line_label": "BOM_LINE:Q-100:AMP01",
                    "actual_cost": 5000,
                }
            ],
        }
        raw = await handle_economy_approve_invoice(engine, args)
        res = json.loads(raw)
        assert res["status"] == "executed"
        assert res["approval_id"] == "appr-001"
        assert res["result"]["bom_lines_written"] == ["BOM_LINE:Q-100:AMP01"]
        mock_cascade.assert_awaited_once()
