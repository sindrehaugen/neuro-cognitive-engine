"""
tests/unit/test_product_match_decision_c10.py
=============================================
Unit test suite for Wave P-4: Product match-decision feedback to C10 Decision-Feedback Service.

Validates:
  1. Dual invocation signature support in do_record_match_decision:
     - Uniform adapter convention: do_record_match_decision(engine, params)
     - Legacy positional convention: do_record_match_decision(engine, params, ns, bom_line, decision)
  2. Routing from do_match_bom_line when 'decision' is present in params.
  3. C10 proposal and delta payloads, context_id, engine, and actor binding.
  4. Decision validation ('accept', 'override' permitted; others raise ValueError).
  5. Missing bom_line or namespace_id validation errors.
  6. Score coercion (valid floats parsed; non-numeric values coerced to None).
  7. Fault-tolerant non-fatal degradation recording when record_decision_feedback fails.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nce.vertical_modules.product.matching import (
    do_match_bom_line,
    do_record_match_decision,
)

_NS_ID = "11111111-2222-3333-4444-555555555555"
_NS_UUID = UUID(_NS_ID)


class _AsyncCtx:
    def __init__(self, obj: Any = None) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_mock_pool(conn: AsyncMock) -> MagicMock:
    """Create a mock asyncpg.Pool returning a configured connection."""
    conn.transaction = MagicMock(return_value=_AsyncCtx())
    pool = MagicMock()
    pool.pg_pool = pool
    pool.acquire.return_value = _AsyncCtx(conn)
    return pool


def _make_engine(conn: AsyncMock) -> MagicMock:
    pool = _make_mock_pool(conn)
    engine = MagicMock()
    engine.pg_pool = pool
    return engine


@pytest.mark.asyncio
async def test_do_record_match_decision_adapter_signature() -> None:
    """do_record_match_decision(engine, params) works via the uniform adapter convention."""
    conn = AsyncMock()
    feedback_id = uuid4()
    df_id = uuid4()

    conn.fetchval = AsyncMock(return_value=feedback_id)
    conn.fetchrow = AsyncMock(return_value={"id": df_id, "created_at": "2026-09-17T20:00:00Z"})
    engine = _make_engine(conn)

    params = {
        "namespace_id": _NS_ID,
        "bom_line": "Shure SM58 Dynamic Vocal Microphone",
        "chosen_sku": "SHURE-SM58-LC",
        "rejected_sku": "SHURE-SM58-CN",
        "matched_score": "0.95",
        "decision": "accept",
        "actor": "av_designer_1",
    }

    result = await do_record_match_decision(engine, params)

    assert result["feedback_id"] == str(feedback_id)
    assert result["decision"] == "accept"
    assert result["decision_feedback_id"] == str(df_id)

    # Verify decision_feedback insert
    df_call = next(
        (
            c
            for c in conn.fetchrow.call_args_list
            if "INSERT INTO decision_feedback" in str(c[0][0])
        ),
        None,
    )
    assert df_call is not None
    args = df_call[0]
    assert args[1] == _NS_UUID
    assert args[2] == "product"
    assert args[3] == "Shure SM58 Dynamic Vocal Microphone"
    proposal = json.loads(args[4])
    assert proposal["bom_line"] == "Shure SM58 Dynamic Vocal Microphone"
    assert proposal["chosen_sku"] == "SHURE-SM58-LC"
    assert proposal["rejected_sku"] == "SHURE-SM58-CN"
    assert proposal["matched_score"] == 0.95
    assert args[5] == "accept"
    delta = json.loads(args[6])
    assert delta["chosen_sku"] == "SHURE-SM58-LC"
    assert delta["rejected_sku"] == "SHURE-SM58-CN"
    assert delta["matched_score"] == 0.95
    assert args[7] == "av_designer_1"


@pytest.mark.asyncio
async def test_do_record_match_decision_positional_signature() -> None:
    """do_record_match_decision works with legacy 5-argument positional style."""
    conn = AsyncMock()
    feedback_id = uuid4()
    df_id = uuid4()

    conn.fetchval = AsyncMock(return_value=feedback_id)
    conn.fetchrow = AsyncMock(return_value={"id": df_id, "created_at": "2026-09-17T20:00:00Z"})
    engine = _make_engine(conn)

    params = {
        "chosen_sku": "SKU-OVERRIDE",
        "rejected_sku": "SKU-AUTO",
        "matched_score": 0.40,
    }

    result = await do_record_match_decision(
        engine,
        params,
        _NS_ID,
        "Ceiling Speaker 8-inch",
        "override",
    )

    assert result["feedback_id"] == str(feedback_id)
    assert result["decision"] == "override"
    assert result["decision_feedback_id"] == str(df_id)

    # Verify default actor is human
    df_call = next(
        (
            c
            for c in conn.fetchrow.call_args_list
            if "INSERT INTO decision_feedback" in str(c[0][0])
        ),
        None,
    )
    assert df_call is not None
    assert df_call[0][7] == "human"


@pytest.mark.asyncio
async def test_do_match_bom_line_routes_to_decision_feedback() -> None:
    """do_match_bom_line delegates to feedback branch when 'decision' is present."""
    conn = AsyncMock()
    feedback_id = uuid4()
    df_id = uuid4()

    conn.fetchval = AsyncMock(return_value=feedback_id)
    conn.fetchrow = AsyncMock(return_value={"id": df_id, "created_at": "2026-09-17T20:00:00Z"})
    engine = _make_engine(conn)

    params = {
        "namespace_id": _NS_ID,
        "bom_line": "QSC Q-SYS Core 110f",
        "decision": "accept",
        "chosen_sku": "QSC-CORE-110F",
        "matched_score": 0.99,
    }

    result = await do_match_bom_line(engine, params)

    assert result["feedback_id"] == str(feedback_id)
    assert result["decision"] == "accept"
    assert result["decision_feedback_id"] == str(df_id)


@pytest.mark.asyncio
async def test_do_record_match_decision_invalid_inputs() -> None:
    """Validation errors on invalid decisions or empty fields."""
    conn = AsyncMock()
    engine = _make_engine(conn)

    # Invalid decision string
    with pytest.raises(ValueError, match="'decision' must be one of"):
        await do_record_match_decision(
            engine,
            {
                "namespace_id": _NS_ID,
                "bom_line": "Some Speaker",
                "decision": "maybe",
            },
        )

    # Empty bom_line
    with pytest.raises(ValueError, match="'bom_line' is required"):
        await do_record_match_decision(
            engine,
            {
                "namespace_id": _NS_ID,
                "bom_line": "   ",
                "decision": "accept",
            },
        )

    # Missing namespace_id
    with pytest.raises(ValueError, match="namespace_id is required"):
        await do_record_match_decision(
            engine,
            {
                "bom_line": "Some Speaker",
                "decision": "accept",
            },
        )


@pytest.mark.asyncio
async def test_do_record_match_decision_score_parsing() -> None:
    """Malformed matched_score coerces to None without raising."""
    conn = AsyncMock()
    feedback_id = uuid4()
    df_id = uuid4()

    conn.fetchval = AsyncMock(return_value=feedback_id)
    conn.fetchrow = AsyncMock(return_value={"id": df_id, "created_at": "2026-09-17T20:00:00Z"})
    engine = _make_engine(conn)

    result = await do_record_match_decision(
        engine,
        {
            "namespace_id": _NS_ID,
            "bom_line": "Speaker wire 2x2.5mm",
            "decision": "accept",
            "chosen_sku": "WIRE-2X2.5",
            "matched_score": "not-a-number",
        },
    )

    assert result["feedback_id"] == str(feedback_id)
    assert result["decision"] == "accept"

    # Verify matched_score in product_match_feedback INSERT is None
    pmf_call = conn.fetchval.call_args[0]
    assert pmf_call[6] is None


@pytest.mark.asyncio
async def test_do_record_match_decision_non_fatal_degradation() -> None:
    """When record_decision_feedback fails, product_match_feedback persists and degradation is recorded."""
    conn = AsyncMock()
    feedback_id = uuid4()

    conn.fetchval = AsyncMock(return_value=feedback_id)
    engine = _make_engine(conn)

    with (
        patch(
            "nce.vertical_modules.product.matching.record_decision_feedback",
            side_effect=RuntimeError("DB connection dropped for decision_feedback"),
        ),
        patch("nce.degradation.record_degradation") as mock_record_degradation,
    ):
        result = await do_record_match_decision(
            engine,
            {
                "namespace_id": _NS_ID,
                "bom_line": "HDMI 2.1 Cable 3m",
                "decision": "accept",
                "chosen_sku": "HDMI-21-3M",
                "matched_score": 0.88,
            },
        )

    # Primary outcome succeeds
    assert result["feedback_id"] == str(feedback_id)
    assert result["decision"] == "accept"
    # decision_feedback_id is gracefully None
    assert result["decision_feedback_id"] is None

    # Degradation was registered
    mock_record_degradation.assert_called_once()
    deg_kwargs = mock_record_degradation.call_args.kwargs
    assert deg_kwargs["namespace_id"] == _NS_UUID
    assert deg_kwargs["engine"] == "product"
    assert deg_kwargs["code"] == "decision_feedback_write_failed"
    assert "DB connection dropped" in deg_kwargs["detail"]
