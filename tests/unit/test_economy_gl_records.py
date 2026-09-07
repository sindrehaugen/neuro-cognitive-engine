"""
tests/unit/test_economy_gl_records.py
=====================================
Unit test suite for Wave B-AG1 / B-E2:
  - economy_get_gl_records MCP tool (cacheable, Advisor read-only)
  - Contract C8 allow-list redaction for gl-records surface
  - In-process seam rewiring in nce/vertical_modules/agreements/coverage.py::_read_economy_gl_rows
  - Non-degraded coverage and kickback evaluation over real GL records
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.mcp_errors import McpError
from nce.redaction import project
from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.vertical_modules.agreements.coverage import _read_economy_gl_rows, do_coverage_matrix
from nce.vertical_modules.agreements.kickback import do_reconcile_kickback
from nce.vertical_modules.economy._guard import EconomyDisabledError
from nce.vertical_modules.economy.gl import do_get_gl_records
from nce.vertical_modules.economy.mcp_handlers import handle_economy_get_gl_records

# ---------------------------------------------------------------------------
# 1. ToolSpec Contract & Registration
# ---------------------------------------------------------------------------


def test_economy_get_gl_records_tool_spec() -> None:
    """economy_get_gl_records must be an Advisor cacheable read tool."""
    assert "economy_get_gl_records" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["economy_get_gl_records"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False
    assert "economy_get_gl_records" in CACHEABLE_TOOLS
    assert "economy_get_gl_records" not in MUTATION_TOOLS
    assert "economy_get_gl_records" not in ADMIN_ONLY_TOOLS


def test_economy_get_gl_records_in_stdio_tools() -> None:
    """Tool schema must be declared in mcp_stdio_tools.py with valid parameters."""
    from nce.mcp_stdio_tools import TOOLS

    tool = next((t for t in TOOLS if t.name == "economy_get_gl_records"), None)
    assert tool is not None, "economy_get_gl_records not defined in mcp_stdio_tools.TOOLS"
    schema = tool.inputSchema
    assert schema["type"] == "object"
    assert "namespace_id" in schema["required"]
    props = schema["properties"]
    assert "since_iso" in props
    assert "until_iso" in props
    assert "account" in props
    assert "account_prefix" in props
    assert "limit" in props


# ---------------------------------------------------------------------------
# 2. Opt-in Guard & Error Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_economy_get_gl_records_refuses_when_disabled() -> None:
    """Raises McpError(-32005) when Economy is not enabled for the namespace."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    ns_id = str(uuid.uuid4())

    with patch(
        "nce.vertical_modules.economy.mcp_handlers.require_economy_enabled",
        side_effect=EconomyDisabledError("Disabled for test"),
    ):
        with pytest.raises(McpError) as exc_info:
            await handle_economy_get_gl_records(engine, {"namespace_id": ns_id})
        assert exc_info.value.code == -32005


@pytest.mark.asyncio
async def test_economy_get_gl_records_requires_namespace_id() -> None:
    """Missing namespace_id returns an error JSON payload."""
    engine = MagicMock()
    raw = await handle_economy_get_gl_records(engine, {})
    data = json.loads(raw)
    assert "error" in data
    assert "namespace_id is required" in data["error"]


# ---------------------------------------------------------------------------
# 3. Contract C8 Allow-List Redaction (gl-records surface)
# ---------------------------------------------------------------------------


def test_c8_gl_records_redaction_drops_sensitive_fields() -> None:
    """C8 projection for 'gl-records' strips margin, cost, and internal-status."""
    raw_node = {
        "id": "rec-100",
        "namespace_id": "ns-200",
        "account": "4300",
        "amount_nok": 15000.50,
        "gl_date": "2026-06-15",
        "supplier_id": "912345678",
        "supplier_name": "Acme Electronics AS",
        "period_id": "2026-06",
        "economy_source_id": "Vendor:912345678",
        "event_id": "evt-hash-1",
        "event_type": "economy.invoice.approved",
        "line_no": 0,
        "change_origin": "agent",
        "created_at": "2026-06-15T10:00:00+00:00",
        # Sensitive fields that must NEVER leak:
        "margin": 0.28,
        "cost": 12000.00,
        "internal-status": "unreviewed",
        "secret_markup": 500.0,
    }

    projected = project(raw_node, "gl-records")

    # Allowed fields pass through
    assert projected["id"] == "rec-100"
    assert projected["account"] == "4300"
    assert projected["amount_nok"] == 15000.50
    assert projected["gl_date"] == "2026-06-15"
    assert projected["supplier_id"] == "912345678"
    assert projected["supplier_name"] == "Acme Electronics AS"

    # Sensitive fields are omitted by default-deny
    assert "margin" not in projected
    assert "cost" not in projected
    assert "internal-status" not in projected
    assert "secret_markup" not in projected


# ---------------------------------------------------------------------------
# 4. do_get_gl_records Execution & Resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_get_gl_records_query_and_formatting() -> None:
    """do_get_gl_records queries economy_postings and derives supplier identities."""
    ns = uuid.uuid4()
    rec_id = uuid.uuid4()
    created_at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

    mock_row = {
        "id": rec_id,
        "namespace_id": ns,
        "event_id": "hash-abc",
        "event_type": "economy.invoice.approved",
        "line_no": 1,
        "account": "4300",
        "amount": Decimal("45000.00"),
        "period_id": "2026-07",
        "economy_source_id": "Vendor:912345678",
        "change_origin": "agent",
        "created_at": created_at,
        "vendor_label": "Vendor:912345678",
    }

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[mock_row])

    pool_ctx = AsyncMock()
    pool_ctx.__aenter__.return_value = mock_conn
    pool_ctx.__aexit__.return_value = None

    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()

    with patch("nce.vertical_modules.economy.gl.scoped_pg_session", return_value=pool_ctx):
        res = await do_get_gl_records(
            mock_engine,
            {
                "namespace_id": str(ns),
                "since_iso": "2026-01-01",
                "account": "4300",
            },
        )

    assert res["ok"] is True
    assert res["count"] == 1
    record = res["records"][0]
    assert record["id"] == str(rec_id)
    assert record["account"] == "4300"
    assert record["amount_nok"] == 45000.0
    assert record["gl_date"] == "2026-07-01"
    assert record["supplier_id"] == "912345678"
    assert record["supplier_name"] == "Vendor:912345678"


# ---------------------------------------------------------------------------
# 5. In-Process Seam Resolution in agreements/coverage.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_economy_gl_rows_calls_registry_tool() -> None:
    """_read_economy_gl_rows dispatches to TOOL_REGISTRY['economy_get_gl_records']."""
    ns = uuid.uuid4()
    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()

    mock_records = [
        {
            "supplier_name": "Vendor A",
            "supplier_id": "912345678",
            "amount_nok": 5000.0,
            "gl_date": "2026-08-10",
        }
    ]

    mock_tool_spec = MagicMock()
    mock_tool_spec.handler = AsyncMock(
        return_value=json.dumps({"ok": True, "records": mock_records})
    )

    with patch.dict(TOOL_REGISTRY, {"economy_get_gl_records": mock_tool_spec}):
        rows = await _read_economy_gl_rows(mock_engine, ns, since_iso="2026-01-01")

    assert rows == mock_records
    mock_tool_spec.handler.assert_awaited_once_with(
        mock_engine,
        {"namespace_id": str(ns), "since_iso": "2026-01-01"},
    )


@pytest.mark.asyncio
async def test_read_economy_gl_rows_raises_not_implemented_on_failure() -> None:
    """_read_economy_gl_rows raises NotImplementedError when the tool handler fails or returns error."""
    ns = uuid.uuid4()
    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()

    mock_tool_spec = MagicMock()
    mock_tool_spec.handler = AsyncMock(
        return_value=json.dumps({"error": "Economy vertical disabled"})
    )

    with patch.dict(TOOL_REGISTRY, {"economy_get_gl_records": mock_tool_spec}):
        with pytest.raises(NotImplementedError, match="economy_get_gl_records returned error"):
            await _read_economy_gl_rows(mock_engine, ns)


# ---------------------------------------------------------------------------
# 6. Integration: Coverage & Kickback over Real GL Rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_matrix_with_real_gl_records_returns_ok() -> None:
    """do_coverage_matrix computes coverage leakage with status 'ok' when GL rows are provided."""
    ns = uuid.uuid4()
    agreement_id = uuid.uuid4()
    vendor_node_id = uuid.uuid4()

    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()

    gl_rows = [
        {
            "supplier_name": "Vendor:912345678",
            "supplier_id": "912345678",
            "amount_nok": 150000.0,
            "gl_date": "2026-08-15",
        }
    ]

    mock_tool_spec = MagicMock()
    mock_tool_spec.handler = AsyncMock(return_value=json.dumps({"ok": True, "records": gl_rows}))

    mock_conn = AsyncMock()
    # Mock _fetch_agreements
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "agreement_id": agreement_id,
                "review_status": "auto_green",
                "extracted": json.dumps(
                    {
                        "supplierId": {"value": "912345678"},
                        "validTo": {"value": "2099-01-01"},
                        "volumeCommitment": {"value": 100000.0},
                    }
                ),
            }
        ]
    )

    # Mock C1 resolve and node fetch for _resolve_vendor_node_id
    mock_match = MagicMock()
    mock_match.node_id = vendor_node_id
    mock_match.score = 0.95

    mock_conn.fetchrow = AsyncMock(return_value={"label": "Vendor:912345678"})

    pool_ctx = AsyncMock()
    pool_ctx.__aenter__.return_value = mock_conn
    pool_ctx.__aexit__.return_value = None

    with (
        patch.dict(TOOL_REGISTRY, {"economy_get_gl_records": mock_tool_spec}),
        patch("nce.vertical_modules.agreements.coverage.scoped_pg_session", return_value=pool_ctx),
        patch("nce.vertical_modules.agreements.coverage.resolve", return_value=[mock_match]),
    ):
        result = await do_coverage_matrix(mock_engine, {"namespace_id": str(ns)})

    assert result["status"] == "ok"
    assert result["agreements_scanned"] == 1
    assert result["gl_rows_processed"] == 1
    # Leakage detected because spend 150,000 exceeds volume commitment 100,000
    leakage_flags = [f for f in result["flags"] if f["flag_type"] == "leakage"]
    assert len(leakage_flags) == 1
    assert "exceeds agreement volumeCommitment cap" in leakage_flags[0]["detail"]


@pytest.mark.asyncio
async def test_kickback_reconciliation_with_real_gl_records() -> None:
    """do_reconcile_kickback calculates tier progression over real GL spend basis."""
    ns = uuid.uuid4()
    agreement_id = uuid.uuid4()
    vendor_node_id = uuid.uuid4()

    mock_engine = MagicMock()
    mock_engine.pg_pool = MagicMock()

    gl_rows = [
        {
            "supplier_name": "Vendor:912345678",
            "supplier_id": "912345678",
            "amount_nok": 250000.0,
            "gl_date": "2026-06-20",
        }
    ]

    mock_tool_spec = MagicMock()
    mock_tool_spec.handler = AsyncMock(return_value=json.dumps({"ok": True, "records": gl_rows}))

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=datetime(2026, 6, 21, tzinfo=timezone.utc))
    # Mock _read_agreement
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            # 1. _read_agreement
            {
                "agreement_id": agreement_id,
                "review_status": "auto_green",
                "extracted": json.dumps(
                    {
                        "supplierId": {"value": "912345678"},
                        "kickbackTiers": [
                            {"threshold": 100000, "pct": 2.0},
                            {"threshold": 200000, "pct": 4.0},
                        ],
                    }
                ),
            },
            # 2. _read_latest_term_snapshot
            None,
            # 3. _snapshot_term_state (SELECT now())
            {"now": datetime(2026, 6, 21, tzinfo=timezone.utc)},
        ]
    )

    pool_ctx = AsyncMock()
    pool_ctx.__aenter__.return_value = mock_conn
    pool_ctx.__aexit__.return_value = None

    with (
        patch.dict(TOOL_REGISTRY, {"economy_get_gl_records": mock_tool_spec}),
        patch("nce.vertical_modules.agreements.kickback.scoped_pg_session", return_value=pool_ctx),
        patch(
            "nce.vertical_modules.agreements.kickback._resolve_vendor_node_id",
            AsyncMock(return_value=vendor_node_id),
        ),
    ):
        result = await do_reconcile_kickback(
            mock_engine,
            {
                "namespace_id": str(ns),
                "agreement_id": str(agreement_id),
            },
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 250000.0
    # Spend 250,000 qualifies for Tier 2 (threshold 200,000 @ 4.0% -> 10,000 NOK earned)
    assert result["active_tier"]["pct"] == 4.0
    assert result["earned_to_date_nok"] == 10000.0
