"""
tests/unit/test_project_convert_degraded.py
============================================
Unit tests for the degraded signal on ``do_convert_signed_quote``.

Why this exists
---------------
``do_convert_signed_quote`` is the only reachable production surface that
depends on ``BOM_LINE`` kg_nodes, and nothing in NCE creates those nodes.
``_fetch_bom_line_labels`` therefore returns ``[]``, zero contains-edges are
written, and the call returns ``bom_lines_linked: 0`` with no error.  The
payload carries no ``ok`` key, and neither caller adds one — the MCP handler
``json.dumps``es the dict and the REST handler wraps it in a bare
``JSONResponse``.  So before this signal existed, a caller could only infer
success from HTTP 200: a project was created with no bill of materials and
nothing said so.

These tests pin the signal that closes that gap:

  1. Zero BOM lines → ``degraded: True`` + ``no_bom_lines_in_graph``.
  2. BOM lines present + baseline present → ``degraded: False``, empty reasons.
  3. The reason distinguishes "no line data in NCE" from "empty quote"
     (``degraded_detail`` is populated and says so).
  4. An unavailable Sales baseline is reported as its own reason code.
  5. Both degradations at once are both reported.
  6. ``_degradation_reasons`` is pure and total over the 2x2 input space.
  7. The non-degraded payload still carries the keys, so callers can read
     ``result["degraded"]`` unconditionally without a KeyError.

All tests are pure unit tests — no DB, no Redis, no live Postgres.
``scoped_pg_session``, ``assert_owner``, ``emit_graph_write`` and
``_read_signed_baseline`` are patched, matching the idiom already used in
``tests/unit/test_system_design_sow.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.vertical_modules.project.convert import (
    _DEGRADED_NO_BOM_LINES,
    _DEGRADED_NO_SALES_BASELINE,
    _bom_line_label,
    _degradation_detail,
    _degradation_reasons,
    do_convert_signed_quote,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NS = "00000000-0000-4000-8000-000000000042"
_QUOTE_ID = "Q-DEGRADED-001"
_SIGNED_BY = "alice"
_SIGNATURE_REF = "SIG-DEGRADED-001"

_MODULE = "nce.vertical_modules.project.convert"
_BASELINE_ROW: dict[str, Any] = {"id": "baseline-uuid-0001", "quote_id": _QUOTE_ID}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine() -> Any:
    """Minimal engine stub — ``pg_pool`` is never really used (session patched)."""
    engine = MagicMock()
    engine.pg_pool = MagicMock()
    return engine


def _make_conn(bom_labels: list[str]) -> AsyncMock:
    """Fake asyncpg connection whose BOM_LINE fetch returns *bom_labels*."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"label": label} for label in bom_labels])
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    # ``conn.transaction()`` must be a *sync* call returning an async CM.
    # A bare AsyncMock attribute would return a coroutine and break
    # ``async with conn.transaction():``.
    @asynccontextmanager
    async def _tx() -> Any:
        yield None

    conn.transaction = MagicMock(side_effect=_tx)
    return conn


async def _convert(
    *,
    bom_labels: list[str],
    baseline_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run ``do_convert_signed_quote`` fully mocked out of the database.

    *bom_labels* is what ``_fetch_bom_line_labels`` will see; *baseline_row*
    is what the Sales A2A seam returns (``None`` = no signed baseline).
    """
    conn = _make_conn(bom_labels)

    @asynccontextmanager
    async def _fake_session(pool: Any, ns_id: Any) -> Any:
        yield conn

    async def _fake_baseline(engine: Any, ns_id: Any, quote_id: str) -> Any:
        return baseline_row

    params = {
        "namespace_id": _NS,
        "quote_id": _QUOTE_ID,
        "signed_by": _SIGNED_BY,
        "signature_ref": _SIGNATURE_REF,
    }

    with (
        patch(f"{_MODULE}.scoped_pg_session", _fake_session),
        patch(f"{_MODULE}.assert_owner", new=AsyncMock()),
        patch(f"{_MODULE}.emit_graph_write", new=AsyncMock()),
        patch(f"{_MODULE}._read_signed_baseline", side_effect=_fake_baseline),
    ):
        return await do_convert_signed_quote(_make_engine(), params)


# ---------------------------------------------------------------------------
# 1. Zero BOM lines is reported as degraded — the whole point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_bom_lines_is_reported_as_degraded() -> None:
    """The live production case: no BOM_LINE nodes exist → explicit signal.

    This is the assertion that fails if the flag is removed: without it the
    payload is indistinguishable from a fully-populated conversion.
    """
    result = await _convert(bom_labels=[], baseline_row=_BASELINE_ROW)

    assert result["bom_lines_linked"] == 0
    assert result["degraded"] is True, (
        "A conversion that linked ZERO BOM lines reported no degradation — "
        "callers cannot distinguish it from a fully-populated project."
    )
    assert _DEGRADED_NO_BOM_LINES in result["degraded_reasons"]


# ---------------------------------------------------------------------------
# 2. The discriminator: a populated conversion must NOT be flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populated_conversion_is_not_degraded() -> None:
    """BOM lines present + baseline present → clean, no degradation.

    Pairs with the test above: a flag hardcoded to True would pass that one
    and fail this one, so the two together pin real discrimination rather
    than a constant.
    """
    bom_labels = [_bom_line_label(_QUOTE_ID, ref) for ref in ("LINE-A", "LINE-B")]

    result = await _convert(bom_labels=bom_labels, baseline_row=_BASELINE_ROW)

    assert result["bom_lines_linked"] == 2
    assert result["degraded"] is False
    assert result["degraded_reasons"] == []
    assert result["degraded_detail"] is None


# ---------------------------------------------------------------------------
# 3. The reason distinguishes "no data in NCE" from "empty quote"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_detail_explains_missing_data_not_empty_quote() -> None:
    """The detail must not let a reader conclude the quote had no lines."""
    result = await _convert(bom_labels=[], baseline_row=_BASELINE_ROW)

    detail = result["degraded_detail"]
    assert isinstance(detail, str) and detail, "degraded_detail missing for a degraded result"
    lowered = detail.lower()
    assert "bom_line" in lowered
    assert "does not" in lowered or "not confirm" in lowered, (
        "degraded_detail must state that zero lines does NOT mean the quote "
        f"had no lines; got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# 4. Unavailable Sales baseline is its own reason code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_sales_baseline_is_its_own_reason() -> None:
    """A missing baseline degrades independently of the BOM-line count."""
    bom_labels = [_bom_line_label(_QUOTE_ID, "LINE-A")]

    result = await _convert(bom_labels=bom_labels, baseline_row=None)

    assert result["degraded"] is True
    assert result["degraded_reasons"] == [_DEGRADED_NO_SALES_BASELINE]
    assert result["baseline"]["sales_available"] is False


# ---------------------------------------------------------------------------
# 5. Both degradations at once are both reported
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_degradations_reported_together() -> None:
    """No degradation masks another — reasons accumulate."""
    result = await _convert(bom_labels=[], baseline_row=None)

    assert result["degraded"] is True
    assert set(result["degraded_reasons"]) == {
        _DEGRADED_NO_BOM_LINES,
        _DEGRADED_NO_SALES_BASELINE,
    }


# ---------------------------------------------------------------------------
# 6. The pure helper is total over the input space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bom_line_count", "sales_available", "expected"),
    [
        (2, True, []),
        (0, True, [_DEGRADED_NO_BOM_LINES]),
        (2, False, [_DEGRADED_NO_SALES_BASELINE]),
        (0, False, [_DEGRADED_NO_BOM_LINES, _DEGRADED_NO_SALES_BASELINE]),
    ],
)
def test_degradation_reasons_is_pure_and_total(
    bom_line_count: int,
    sales_available: bool,
    expected: list[str],
) -> None:
    """Every combination maps to a deterministic, ordered reason list."""
    assert (
        _degradation_reasons(
            bom_line_count=bom_line_count,
            sales_available=sales_available,
        )
        == expected
    )


def test_degradation_detail_is_none_only_when_clean() -> None:
    assert _degradation_detail([]) is None
    assert isinstance(_degradation_detail([_DEGRADED_NO_BOM_LINES]), str)
    assert isinstance(_degradation_detail([_DEGRADED_NO_SALES_BASELINE]), str)


# ---------------------------------------------------------------------------
# 7. Keys are always present — callers can read them unconditionally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_keys_always_present_in_payload() -> None:
    """Both the clean and degraded payloads carry the full key set."""
    clean = await _convert(
        bom_labels=[_bom_line_label(_QUOTE_ID, "LINE-A")],
        baseline_row=_BASELINE_ROW,
    )
    degraded = await _convert(bom_labels=[], baseline_row=None)

    for payload in (clean, degraded):
        assert "degraded" in payload
        assert "degraded_reasons" in payload
        assert "degraded_detail" in payload
        assert isinstance(payload["degraded"], bool)
        assert isinstance(payload["degraded_reasons"], list)
