"""
tests/test_project_baseline.py
================================
Tests for ``nce/vertical_modules/project/baseline.py`` (Wave 2 — baseline-read).

Test strategy
-------------
- PURE UNIT tests (no DB, no network) cover:
    - margin-trinity math (_estimated_margin_pct, _clamp_margin_pct)
    - trinity assembly from a signed row (_build_trinity_from_row)
    - unavailable-Sales degradation path (_build_unavailable_trinity)
    - build_margin_trinity with mocked A2A seam

- INTEGRATION tests (@pytest.mark.integration) cover:
    - DB/A2A paths (require a live Postgres with schema.sql + migrations)
    - These are correctly written but NOT executed by the sub-agent;
      the orchestrator runs them against the dev DB.

Invariants asserted
-------------------
1. build_margin_trinity READS ``sales_signed_baselines`` (via A2A seam)
   and NEVER writes it.
2. ``signed`` dimension is always taken verbatim from the Sales row —
   never overwritten or mutated.
3. ``actual`` dimension is always None (Economy cascade; not Project's).
4. ``estimated`` dimension is the ONLY value Project computes/writes.
5. No ``project_signed_baselines`` object is created anywhere.
6. Sales-unavailable path degrades to BASELINE_UNAVAILABLE ("unknown"),
   never blocks, never fabricates.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.project.baseline import (
    BASELINE_UNAVAILABLE,
    _build_trinity_from_row,
    _build_unavailable_trinity,
    _clamp_margin_pct,
    _estimated_margin_pct,
    build_margin_trinity,
)

# ---------------------------------------------------------------------------
# Module-level A2A seam path for patch() calls.
# ---------------------------------------------------------------------------

_SEAM = "nce.vertical_modules.project.baseline._read_signed_baseline"

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NS_ID = str(uuid.uuid4())
_QUOTE_ID = "QUOTE-TEST-001"

_SIGNED_ROW: dict[str, Any] = {
    "id": "SB-001",
    "quote_id": _QUOTE_ID,
    "signed_margin_pct": 0.32,
    "signed_total_nok": 500_000.0,
    "signed_at": "2026-06-01T12:00:00Z",
}


class _EngineStub:
    """Minimal engine stub — A2A seam is patched, pg_pool not needed here."""

    pass


# ===========================================================================
# PURE UNIT TESTS
# ===========================================================================


class TestClampMarginPct:
    """_clamp_margin_pct: robustness against bad input."""

    def test_normal_float_passthrough(self):
        assert _clamp_margin_pct(0.32) == pytest.approx(0.32)

    def test_zero(self):
        assert _clamp_margin_pct(0) == 0.0

    def test_negative(self):
        assert _clamp_margin_pct(-0.1) == pytest.approx(-0.1)

    def test_nan_returns_zero(self):
        assert _clamp_margin_pct(float("nan")) == 0.0

    def test_inf_returns_zero(self):
        assert _clamp_margin_pct(float("inf")) == 0.0
        assert _clamp_margin_pct(float("-inf")) == 0.0

    def test_none_returns_zero(self):
        assert _clamp_margin_pct(None) == 0.0

    def test_string_float(self):
        assert _clamp_margin_pct("0.25") == pytest.approx(0.25)

    def test_non_numeric_string_returns_zero(self):
        assert _clamp_margin_pct("bad") == 0.0


class TestEstimatedMarginPct:
    """_estimated_margin_pct: pure math, Project's only write dimension."""

    def test_standard_margin(self):
        # (1000 - 680) / 1000 = 0.32
        result = _estimated_margin_pct(680.0, 1_000.0)
        assert result == pytest.approx(0.32)

    def test_zero_revenue_returns_zero(self):
        # Avoids ZeroDivisionError; undefined margin → 0.0
        assert _estimated_margin_pct(500.0, 0.0) == 0.0

    def test_zero_cost_full_margin(self):
        assert _estimated_margin_pct(0.0, 1_000.0) == pytest.approx(1.0)

    def test_negative_margin(self):
        # Cost exceeds revenue — loss project
        result = _estimated_margin_pct(1_200.0, 1_000.0)
        assert result == pytest.approx(-0.2)

    def test_equal_cost_revenue_zero_margin(self):
        assert _estimated_margin_pct(1_000.0, 1_000.0) == pytest.approx(0.0)


class TestBuildUnavailableTrinity:
    """_build_unavailable_trinity: graceful-degradation shape."""

    def test_signed_is_unknown_string(self):
        trinity = _build_unavailable_trinity()
        assert trinity["signed"] == BASELINE_UNAVAILABLE

    def test_estimated_is_none(self):
        assert _build_unavailable_trinity()["estimated"] is None

    def test_actual_is_none(self):
        assert _build_unavailable_trinity()["actual"] is None

    def test_sales_available_is_false(self):
        assert _build_unavailable_trinity()["sales_available"] is False

    def test_signed_baseline_id_is_none(self):
        assert _build_unavailable_trinity()["signed_baseline_id"] is None

    def test_no_project_signed_baselines_key(self):
        """No key named 'project_signed_baselines' may appear in the snapshot."""
        trinity = _build_unavailable_trinity()
        for key in trinity:
            assert "project_signed_baselines" not in key


class TestBuildTrinityFromRow:
    """_build_trinity_from_row: assembly from Sales-frozen row."""

    def test_signed_is_taken_verbatim_from_row(self):
        """signed must NEVER be recomputed — taken straight from Sales row."""
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        assert trinity["signed"] == pytest.approx(0.32)

    def test_signed_not_overwritten_by_estimated(self):
        """Even if estimated_margin differs, signed stays unchanged."""
        trinity = _build_trinity_from_row(_SIGNED_ROW, 900_000.0, 1_000_000.0)
        # estimated = 0.10, but signed must still be 0.32
        assert trinity["signed"] == pytest.approx(0.32)
        assert trinity["estimated"] == pytest.approx(0.10)

    def test_actual_is_always_none(self):
        """actual is Economy's responsibility; Project never sets it."""
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        assert trinity["actual"] is None

    def test_estimated_computed_from_params(self):
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        # (1_000_000 - 680_000) / 1_000_000 = 0.32
        assert trinity["estimated"] == pytest.approx(0.32)

    def test_sales_available_is_true(self):
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        assert trinity["sales_available"] is True

    def test_signed_baseline_id_from_row(self):
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        assert trinity["signed_baseline_id"] == "SB-001"

    def test_no_project_signed_baselines_key(self):
        """No key named 'project_signed_baselines' may appear anywhere."""
        trinity = _build_trinity_from_row(_SIGNED_ROW, 680_000.0, 1_000_000.0)
        for key in trinity:
            assert "project_signed_baselines" not in key


class TestBuildMarginTrinityUnit:
    """build_margin_trinity with mocked A2A seam (pure unit — no DB)."""

    @pytest.mark.asyncio
    async def test_returns_trinity_when_sales_row_found(self):
        mock_read = AsyncMock(return_value=_SIGNED_ROW)
        with patch(_SEAM, mock_read):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 680_000.0,
                    "estimated_revenue_nok": 1_000_000.0,
                },
            )

        assert result["signed"] == pytest.approx(0.32)
        assert result["estimated"] == pytest.approx(0.32)
        assert result["actual"] is None
        assert result["sales_available"] is True
        assert result["signed_baseline_id"] == "SB-001"

    @pytest.mark.asyncio
    async def test_seam_called_once_with_correct_args(self):
        """A2A seam is invoked exactly once with (engine, ns_uuid, quote_id)."""
        mock_read = AsyncMock(return_value=_SIGNED_ROW)
        with patch(_SEAM, mock_read):
            await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 0.0,
                    "estimated_revenue_nok": 0.0,
                },
            )
        mock_read.assert_awaited_once()
        _, call_ns, call_qid = mock_read.await_args[0]
        assert str(call_ns) == _NS_ID
        assert call_qid == _QUOTE_ID

    @pytest.mark.asyncio
    async def test_seam_read_not_write(self):
        """A2A seam only reads; the returned row is NEVER mutated by this function."""
        original_signed = _SIGNED_ROW["signed_margin_pct"]
        mock_read = AsyncMock(return_value=dict(_SIGNED_ROW))  # fresh copy
        with patch(_SEAM, mock_read):
            await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 750_000.0,
                    "estimated_revenue_nok": 1_000_000.0,
                },
            )
        # The row returned by the seam must be unchanged
        returned_row: dict[str, Any] = mock_read.return_value
        assert returned_row["signed_margin_pct"] == original_signed

    @pytest.mark.asyncio
    async def test_sales_unavailable_not_implemented_degrades_to_unknown(self):
        """NotImplementedError (Sales not built) → unknown, never blocks."""
        with patch(_SEAM, AsyncMock(side_effect=NotImplementedError)):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 0.0,
                    "estimated_revenue_nok": 0.0,
                },
            )

        assert result["signed"] == BASELINE_UNAVAILABLE
        assert result["sales_available"] is False
        assert result["actual"] is None
        assert result["estimated"] is None

    @pytest.mark.asyncio
    async def test_sales_unavailable_exception_degrades_gracefully(self):
        """Any unexpected A2A error → unknown, never propagates."""
        with patch(_SEAM, AsyncMock(side_effect=RuntimeError("network error"))):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 0.0,
                    "estimated_revenue_nok": 0.0,
                },
            )

        assert result["signed"] == BASELINE_UNAVAILABLE
        assert result["sales_available"] is False

    @pytest.mark.asyncio
    async def test_sales_returns_none_row_degrades_to_unknown(self):
        """A None return (no row in sales_signed_baselines) → unknown."""
        with patch(_SEAM, AsyncMock(return_value=None)):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 0.0,
                    "estimated_revenue_nok": 0.0,
                },
            )

        assert result["signed"] == BASELINE_UNAVAILABLE
        assert result["sales_available"] is False

    @pytest.mark.asyncio
    async def test_only_estimated_dimension_is_computed_by_project(self):
        """Project writes ONLY estimated; signed is read-only, actual is None."""
        mock_read = AsyncMock(return_value=_SIGNED_ROW)
        with patch(_SEAM, mock_read):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 500_000.0,
                    "estimated_revenue_nok": 1_000_000.0,
                },
            )

        # estimated = (1_000_000 - 500_000) / 1_000_000 = 0.50
        assert result["estimated"] == pytest.approx(0.50)
        # signed must still be the Sales-frozen value
        assert result["signed"] == pytest.approx(0.32)
        # actual is NEVER set by Project
        assert result["actual"] is None

    @pytest.mark.asyncio
    async def test_missing_namespace_id_raises(self):
        with pytest.raises(ValueError, match="namespace_id"):
            await build_margin_trinity(
                _EngineStub(),
                {"quote_id": _QUOTE_ID, "estimated_cost_nok": 0.0, "estimated_revenue_nok": 0.0},
            )

    @pytest.mark.asyncio
    async def test_missing_quote_id_raises(self):
        with pytest.raises(ValueError, match="quote_id"):
            await build_margin_trinity(
                _EngineStub(),
                {"namespace_id": _NS_ID, "estimated_cost_nok": 0.0, "estimated_revenue_nok": 0.0},
            )

    @pytest.mark.asyncio
    async def test_no_project_signed_baselines_in_result(self):
        """Trinity result must never contain a 'project_signed_baselines' key."""
        mock_read = AsyncMock(return_value=_SIGNED_ROW)
        with patch(_SEAM, mock_read):
            result = await build_margin_trinity(
                _EngineStub(),
                {
                    "namespace_id": _NS_ID,
                    "quote_id": _QUOTE_ID,
                    "estimated_cost_nok": 680_000.0,
                    "estimated_revenue_nok": 1_000_000.0,
                },
            )
        for key in result:
            assert "project_signed_baselines" not in key


# ===========================================================================
# INTEGRATION TESTS — require live Postgres (run by the orchestrator)
# ===========================================================================


@pytest.mark.integration
class TestBuildMarginTrinityIntegration:
    """Integration tests for build_margin_trinity.

    These tests require a live Postgres with schema.sql and migrations applied.
    The Sales engine (Module 5) is not built yet — the A2A seam is patched
    with an AsyncMock in all tests here, providing deterministic Sales rows.
    The DB is used only where scoped_pg_session reads are involved in future
    waves (do_convert_signed_quote etc.).  This wave's entry point
    ``build_margin_trinity`` is pure compute + A2A seam — no direct DB writes.

    Run against a live DB via: pytest tests/test_project_baseline.py -m integration
    """

    @pytest.mark.asyncio
    async def test_trinity_reads_sales_row_not_writes(self, pg_pool: Any):
        """Verify build_margin_trinity reads Sales baseline and does not write it.

        Uses a patched A2A seam supplying a deterministic signed row.
        Asserts that no ``project_signed_baselines``-keyed data appears in
        the result, and that the signed row passed through the seam is
        returned unchanged.
        """
        ns_id = str(uuid.uuid4())
        quote_id = "QUOTE-INT-001"
        mock_row = {
            "id": "SB-INT-001",
            "quote_id": quote_id,
            "signed_margin_pct": 0.28,
            "signed_total_nok": 250_000.0,
            "signed_at": "2026-06-01T08:00:00Z",
        }

        class _IntEngineStub:
            def __init__(self) -> None:
                self.pg_pool = pg_pool

        mock_read = AsyncMock(return_value=mock_row)
        with patch(_SEAM, mock_read):
            result = await build_margin_trinity(
                _IntEngineStub(),
                {
                    "namespace_id": ns_id,
                    "quote_id": quote_id,
                    "estimated_cost_nok": 200_000.0,
                    "estimated_revenue_nok": 250_000.0,
                },
            )

        # Contract A: signed value is verbatim from Sales row, never overwritten.
        assert result["signed"] == pytest.approx(0.28)
        # Project writes only estimated.
        # (250_000 - 200_000) / 250_000 = 0.20
        assert result["estimated"] == pytest.approx(0.20)
        # actual is always None (Economy).
        assert result["actual"] is None
        assert result["sales_available"] is True
        # No project_signed_baselines anywhere.
        for key in result:
            assert "project_signed_baselines" not in key

    @pytest.mark.asyncio
    async def test_trinity_sales_unavailable_degrades_cleanly(self, pg_pool: Any):
        """With Sales engine absent (NotImplementedError), result is unknown — not a crash."""
        ns_id = str(uuid.uuid4())

        class _IntEngineStub:
            def __init__(self) -> None:
                self.pg_pool = pg_pool

        with patch(_SEAM, AsyncMock(side_effect=NotImplementedError)):
            result = await build_margin_trinity(
                _IntEngineStub(),
                {
                    "namespace_id": ns_id,
                    "quote_id": "QUOTE-INT-NOTBUILT",
                    "estimated_cost_nok": 0.0,
                    "estimated_revenue_nok": 0.0,
                },
            )

        assert result["signed"] == BASELINE_UNAVAILABLE
        assert result["sales_available"] is False
        assert result["actual"] is None
