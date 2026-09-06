"""
tests/unit/test_decision_feedback.py
====================================
Contract and gate tests for C10 Decision-Feedback Service:
  1. record_decision_feedback core validation, SQL binding, and RLS discipline.
  2. handle_record_decision_feedback MCP handler contract.
  3. Gate verification: each of the five vertical engine call sites
     (product, procurement, economy, resources, field_tech) actually writes
     a row to decision_feedback.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from nce.decision_feedback import (
    handle_record_decision_feedback,
    record_decision_feedback,
)
from nce.vertical_modules.economy.recalibration import (
    do_record_match_decision as economy_record_match_decision,
)
from nce.vertical_modules.field_tech.outcome import do_record_outcome
from nce.vertical_modules.procurement.recalibration import (
    do_record_match_decision as procurement_record_match_decision,
)
from nce.vertical_modules.product.matching import (
    do_record_match_decision as product_record_match_decision,
)
from nce.vertical_modules.resources.planner import do_record_allocation_outcome

_NS_ID = "11111111-2222-3333-4444-555555555555"
_NS_UUID = UUID(_NS_ID)


class _AsyncCtx:
    def __init__(self, obj: Any = None) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock | None = None) -> MagicMock:
    """Create a mock asyncpg.Pool returning a configured mock connection."""
    if conn is None:
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=uuid4())
        conn.fetchrow = AsyncMock(
            return_value={"id": uuid4(), "created_at": "2026-09-06T12:00:00Z"}
        )
        conn.execute = AsyncMock(return_value="INSERT 1")

    conn.transaction = MagicMock(return_value=_AsyncCtx())
    pool = MagicMock()
    pool.pg_pool = pool
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


# ============================================================================
# 1. Core Service Unit Tests
# ============================================================================


@pytest.mark.asyncio
async def test_record_decision_feedback_valid() -> None:
    """Positive control: valid decision feedback writes row and returns metadata."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    res = await record_decision_feedback(
        pool,
        _NS_ID,
        engine="product",
        proposal={"sku": "SKU-1", "score": 0.95},
        decision="accept",
        delta={"sku": "SKU-1"},
        actor="engineer-1",
        context_id="BOM-LINE-1",
    )

    assert res["status"] == "recorded"
    assert res["id"] == str(row_id)
    assert res["engine"] == "product"
    assert res["decision"] == "accept"
    assert res["context_id"] == "BOM-LINE-1"

    # Assert SQL parameter bindings
    conn.fetchrow.assert_called_once()
    call_args = conn.fetchrow.call_args[0]
    query = call_args[0]
    assert "INSERT INTO decision_feedback" in query
    assert call_args[1] == _NS_UUID
    assert call_args[2] == "product"
    assert call_args[3] == "BOM-LINE-1"
    assert json.loads(call_args[4]) == {"sku": "SKU-1", "score": 0.95}
    assert call_args[5] == "accept"
    assert json.loads(call_args[6]) == {"sku": "SKU-1"}
    assert call_args[7] == "engineer-1"


@pytest.mark.asyncio
async def test_record_decision_feedback_validation_errors() -> None:
    """Negative controls: invalid inputs raise ValueError."""
    pool = _make_mock_pool()

    with pytest.raises(ValueError, match="engine must be a non-empty string"):
        await record_decision_feedback(pool, _NS_ID, engine="", decision="accept")

    with pytest.raises(ValueError, match="decision must be a non-empty string"):
        await record_decision_feedback(pool, _NS_ID, engine="product", decision="")


# ============================================================================
# 2. MCP Handler Tests
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_handler_record_decision_feedback() -> None:
    """MCP tool handler correctly parses arguments, invokes core, and returns JSON."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    engine = MagicMock()
    engine.pg_pool = pool

    args = {
        "namespace_id": _NS_ID,
        "engine": "procurement",
        "decision": "override",
        "context_id": "SUPPLIER-42",
        "proposal": {"preferred": "SUPPLIER-10"},
        "delta": {"chosen": "SUPPLIER-42"},
        "actor": "procurement-mgr",
    }
    raw_res = await handle_record_decision_feedback(engine, args)
    res = json.loads(raw_res)

    assert res["status"] == "recorded"
    assert res["id"] == str(row_id)
    assert res["engine"] == "procurement"
    assert res["decision"] == "override"


# ============================================================================
# 3. Gate Verification Tests: 5 Engine Call Sites
# ============================================================================


@pytest.mark.asyncio
async def test_gate_product_writes_decision_feedback() -> None:
    """Gate 1: product.do_record_match_decision writes to decision_feedback."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.fetchval.return_value = uuid4()  # product_match_feedback RETURNING id
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    engine = MagicMock()
    engine.pg_pool = pool

    res = await product_record_match_decision(
        engine,
        {
            "chosen_sku": "SKU-99",
            "rejected_sku": "SKU-10",
            "matched_score": 0.88,
            "actor": "tech_bob",
        },
        _NS_ID,
        "CAT6A 24-port Patch Panel",
        "override",
    )

    assert res["decision"] == "override"
    assert res["decision_feedback_id"] == str(row_id)

    # Verify decision_feedback insert call
    df_call = next(
        (c for c in conn.fetchrow.call_args_list if "INSERT INTO decision_feedback" in c[0][0]),
        None,
    )
    assert df_call is not None, (
        "product.do_record_match_decision did not write to decision_feedback"
    )
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "product"
    assert args[3] == "CAT6A 24-port Patch Panel"
    assert args[5] == "override"


@pytest.mark.asyncio
async def test_gate_procurement_writes_decision_feedback() -> None:
    """Gate 2: procurement.do_record_match_decision writes to decision_feedback."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.execute.return_value = "INSERT 1"
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    res = await procurement_record_match_decision(
        pool,
        _NS_UUID,
        supplier_id="SUP-888",
        decision="accept",
        score=92.5,
    )

    assert res["decision_feedback_id"] == str(row_id)

    df_call = next(
        (c for c in conn.fetchrow.call_args_list if "INSERT INTO decision_feedback" in c[0][0]),
        None,
    )
    assert df_call is not None, (
        "procurement.do_record_match_decision did not write to decision_feedback"
    )
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "procurement"
    assert args[3] == "SUP-888"
    assert args[5] == "accept"


@pytest.mark.asyncio
async def test_gate_economy_writes_decision_feedback() -> None:
    """Gate 3: economy.do_record_match_decision writes to decision_feedback."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.execute.return_value = "INSERT 1"
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    res = await economy_record_match_decision(
        pool,
        _NS_UUID,
        supplier_orgnr="987654321",
        decision="accept",
        score=140,
        tier="GREEN",
    )

    assert res["decision_feedback_id"] == str(row_id)

    df_call = next(
        (c for c in conn.fetchrow.call_args_list if "INSERT INTO decision_feedback" in c[0][0]),
        None,
    )
    assert df_call is not None, (
        "economy.do_record_match_decision did not write to decision_feedback"
    )
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "economy"
    assert args[3] == "987654321"
    assert args[5] == "accept"


@pytest.mark.asyncio
async def test_gate_resources_writes_decision_feedback() -> None:
    """Gate 4: resources.do_record_allocation_outcome writes to decision_feedback."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.execute.return_value = "INSERT 1"
    conn.fetchrow.return_value = {"id": row_id, "created_at": "2026-09-06T12:00:00Z"}
    pool = _make_mock_pool(conn)

    engine = MagicMock()
    engine.pg_pool = pool

    res = await do_record_allocation_outcome(
        engine,
        {
            "namespace_id": _NS_ID,
            "resource_id": str(uuid4()),
            "allocation_id": str(uuid4()),
            "rating": 4.5,
            "quality_score": 0.9,
            "on_time": True,
            "namespace_metadata": {"resources": {"enabled": True}},
        },
    )

    assert res["decision_feedback_id"] == str(row_id)

    df_call = next(
        (c for c in conn.fetchrow.call_args_list if "INSERT INTO decision_feedback" in c[0][0]),
        None,
    )
    assert df_call is not None, (
        "resources.do_record_allocation_outcome did not write to decision_feedback"
    )
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "resources"
    assert args[5] == "held"


@pytest.mark.asyncio
async def test_gate_field_tech_writes_decision_feedback() -> None:
    """Gate 5: field_tech.do_record_outcome writes to decision_feedback."""
    conn = AsyncMock()
    row_id = uuid4()
    conn.fetchrow.side_effect = [
        # 1. work order select
        {
            "work_order_id": "WO-100",
            "namespace_id": _NS_UUID,
            "partner_scope_id": None,
            "kind": "service",
            "assignee_id": "tech_bob",
            "assignee_kind": "employee",
            "status": "in_progress",
            "raw": {},
        },
        # 2. decision_feedback insert RETURNING id, created_at
        {"id": row_id, "created_at": "2026-09-06T12:00:00Z"},
    ]
    conn.execute.return_value = "UPDATE 1"
    pool = _make_mock_pool(conn)

    res = await do_record_outcome(
        pool,
        {
            "namespace_id": _NS_ID,
            "work_order_id": "WO-100",
            "rating": 5.0,
            "quality_score": 1.0,
            "was_rework": False,
        },
    )

    assert res["decision_feedback_id"] == str(row_id)

    df_call = next(
        (c for c in conn.fetchrow.call_args_list if "INSERT INTO decision_feedback" in c[0][0]),
        None,
    )
    assert df_call is not None, "field_tech.do_record_outcome did not write to decision_feedback"
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "field_tech"
    assert args[3] == "WO-100"
    assert args[5] == "succeeded"
