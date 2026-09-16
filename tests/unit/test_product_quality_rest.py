"""
tests/unit/test_product_quality_rest.py
=======================================
Unit tests for the GET /api/product/quality REST route and do_product_quality
core function (Module 2 / Charter Wave P-6).

Covers:
  1. do_product_quality single product by UUID.
  2. do_product_quality single product by mfr_part_no.
  3. do_product_quality single product not found returns status='not_found'.
  4. do_product_quality catalog rollup mode aggregates per manufacturer.
  5. do_product_quality channel selection (b2b_portal, quote, design).
  6. do_product_quality invalid channel raises ValueError.
  7. do_product_quality invalid product_id UUID raises ValueError.
  8. api_product_quality single product returns 200 OK.
  9. api_product_quality single product not found returns 404.
  10. api_product_quality catalog rollup returns 200 OK.
  11. api_product_quality disabled namespace returns 409.
  12. api_product_quality missing namespace_id returns 422.
  13. api_product_quality malformed namespace_id returns 422.
  14. api_product_quality malformed product_id returns 422.
  15. api_product_quality invalid channel returns 422.
  16. api_product_quality engine disconnected returns 503.
  17. ADR-0017: No cost, cost_price, margin, or bid_id columns leaked.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nce.admin_handlers.product import api_product_quality
from nce.vertical_modules.product.quality import (
    do_product_quality,
)

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000001"
_PRODUCT_ID = "11111111-1111-4111-8111-111111111111"
_FORBIDDEN_COLUMNS: frozenset[str] = frozenset({"cost", "cost_price", "margin", "bid_id"})


class _AsyncCtx:
    """Minimal async context manager that yields the given object."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_: Any) -> None:
        pass


def _make_pool(fetch_return=None, fetchrow_return=None) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="SET")
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool, conn


def _make_starlette_request(query_params: dict[str, str]) -> MagicMock:
    req = MagicMock()
    req.query_params = query_params
    req.path_params = {}
    return req


def _sample_etim_specs_perfect() -> dict[str, Any]:
    """Return an etim_specs dict that passes all completeness and grade criteria."""
    fields = (
        "short_description",
        "category",
        "manufacturer",
        "mfr_part_no",
        "lifecycle_status",
        "price",
        "compliance",
        "warranty",
    )
    return {
        f: {
            "value": f"sample_{f}",
            "confidence": 0.95,
            "source": "cisco_api",
            "provenance": {"source": "cisco_api", "reason": "manufacturer_verified"},
        }
        for f in fields
    }


def _sample_product_row(
    prod_id: uuid.UUID | None = None,
    manufacturer: str = "CISCO",
    mfr_part_no: str = "CS-KIT-K9",
    specs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": prod_id or uuid.UUID(_PRODUCT_ID),
        "manufacturer": manufacturer,
        "mfr_part_no": mfr_part_no,
        "gtin": "00882658000000",
        "lifecycle_status": "active",
        "etim_specs": specs or _sample_etim_specs_perfect(),
    }


# ---------------------------------------------------------------------------
# Core: do_product_quality tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_product_quality_single_product_found():
    """do_product_quality resolves single product by UUID and evaluates scores."""
    row = _sample_product_row()
    pool, _ = _make_pool(fetchrow_return=row)

    res = await do_product_quality(
        pool,
        {
            "namespace_id": _NAMESPACE_ID,
            "product_id": _PRODUCT_ID,
            "channel": "b2b_portal",
        },
    )

    assert res["status"] == "ok"
    assert res["product_id"] == _PRODUCT_ID
    assert res["manufacturer"] == "CISCO"
    assert res["mfr_part_no"] == "CS-KIT-K9"
    assert res["channel"] == "b2b_portal"

    comp = res["completeness"]
    assert comp["score"] == 1.0
    assert comp["channel"] == "b2b_portal"
    assert len(comp["missing"]) == 0

    grade = res["grade_result"]
    assert grade["grade"] == "A"
    assert grade["score"] >= 0.90


@pytest.mark.asyncio
async def test_do_product_quality_single_product_by_mfr_part_no():
    """do_product_quality resolves single product by mfr_part_no and manufacturer."""
    row = _sample_product_row(mfr_part_no="SX20-CODEC")
    pool, _ = _make_pool(fetchrow_return=row)

    res = await do_product_quality(
        pool,
        {
            "namespace_id": _NAMESPACE_ID,
            "mfr_part_no": "SX20-CODEC",
            "manufacturer": "CISCO",
        },
    )

    assert res["status"] == "ok"
    assert res["mfr_part_no"] == "SX20-CODEC"
    assert res["manufacturer"] == "CISCO"


@pytest.mark.asyncio
async def test_do_product_quality_single_product_not_found():
    """do_product_quality returns status='not_found' when UUID doesn't exist."""
    pool, _ = _make_pool(fetchrow_return=None)

    res = await do_product_quality(
        pool,
        {
            "namespace_id": _NAMESPACE_ID,
            "product_id": _PRODUCT_ID,
        },
    )

    assert res["status"] == "not_found"
    assert "error" in res
    assert _PRODUCT_ID in res["error"]


@pytest.mark.asyncio
async def test_do_product_quality_catalog_rollup():
    """do_product_quality aggregates catalog products into per-manufacturer rollup."""
    rows = [
        _sample_product_row(
            prod_id=uuid.uuid4(),
            manufacturer="CISCO",
            mfr_part_no="PART-1",
        ),
        _sample_product_row(
            prod_id=uuid.uuid4(),
            manufacturer="CRESTRON",
            mfr_part_no="DM-TX-401",
        ),
    ]
    pool, _ = _make_pool(fetch_return=rows)

    res = await do_product_quality(
        pool,
        {
            "namespace_id": _NAMESPACE_ID,
            "channel": "quote",
        },
    )

    assert res["status"] == "ok"
    assert res["channel"] == "quote"
    assert res["total_products"] == 2
    assert "CISCO" in res["manufacturers"]
    assert "CRESTRON" in res["manufacturers"]

    cisco_summary = res["manufacturers"]["CISCO"]
    assert cisco_summary["product_count"] == 1
    assert cisco_summary["avg_completeness"] == 1.0

    summary = res["summary"]
    assert summary["total_products"] == 2
    assert summary["total_manufacturers"] == 2
    assert summary["dominant_grade"] == "A"


@pytest.mark.asyncio
async def test_do_product_quality_invalid_channel():
    """do_product_quality raises ValueError when invalid channel is specified."""
    pool, _ = _make_pool()

    with pytest.raises(ValueError, match="Invalid channel"):
        await do_product_quality(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "channel": "non_existent_channel",
            },
        )


@pytest.mark.asyncio
async def test_do_product_quality_invalid_product_id():
    """do_product_quality raises ValueError when malformed UUID is passed as product_id."""
    pool, _ = _make_pool()

    with pytest.raises(ValueError, match="Invalid product_id"):
        await do_product_quality(
            pool,
            {
                "namespace_id": _NAMESPACE_ID,
                "product_id": "not-a-valid-uuid",
            },
        )


# ---------------------------------------------------------------------------
# REST Handler: api_product_quality tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_product_quality_single_product_ok():
    """GET /api/product/quality?product_id=... returns 200 and quality payload."""
    row = _sample_product_row()
    pool, _ = _make_pool(fetchrow_return=row)
    engine = MagicMock()
    engine.pg_pool = pool

    request = _make_starlette_request(
        {
            "namespace_id": _NAMESPACE_ID,
            "product_id": _PRODUCT_ID,
            "channel": "design",
        }
    )

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["status"] == "ok"
    assert body["product_id"] == _PRODUCT_ID
    assert body["channel"] == "design"
    assert "completeness" in body
    assert "grade_result" in body


@pytest.mark.asyncio
async def test_api_product_quality_single_product_not_found_returns_404():
    """GET /api/product/quality?product_id=... returns 404 when product is missing."""
    pool, _ = _make_pool(fetchrow_return=None)
    engine = MagicMock()
    engine.pg_pool = pool

    request = _make_starlette_request(
        {
            "namespace_id": _NAMESPACE_ID,
            "product_id": _PRODUCT_ID,
        }
    )

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 404
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.asyncio
async def test_api_product_quality_catalog_rollup_ok():
    """GET /api/product/quality?namespace_id=... returns 200 and rollup data."""
    rows = [_sample_product_row(prod_id=uuid.uuid4(), manufacturer="CISCO")]
    pool, _ = _make_pool(fetch_return=rows)
    engine = MagicMock()
    engine.pg_pool = pool

    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["status"] == "ok"
    assert "manufacturers" in body
    assert "summary" in body
    assert body["total_products"] == 1


@pytest.mark.asyncio
async def test_api_product_quality_disabled_namespace_returns_409():
    """GET /api/product/quality returns 409 when product vertical is disabled."""
    engine = MagicMock()
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    from nce.admin_handlers._shared import JSONResponse

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(
                return_value=JSONResponse(
                    {"error": "Product vertical is not enabled for this namespace"},
                    status_code=409,
                )
            ),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.asyncio
async def test_api_product_quality_missing_namespace_returns_422():
    """GET /api/product/quality returns 422 when namespace_id query param is omitted."""
    engine = MagicMock()
    request = _make_starlette_request({})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 422
    body = json.loads(response.body)
    assert "error" in body


@pytest.mark.asyncio
async def test_api_product_quality_malformed_namespace_returns_422():
    """GET /api/product/quality returns 422 when namespace_id is not a valid UUID."""
    engine = MagicMock()
    request = _make_starlette_request({"namespace_id": "malformed-not-a-uuid"})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 422
    body = json.loads(response.body)
    assert "Invalid namespace_id" in body["error"]


@pytest.mark.asyncio
async def test_api_product_quality_malformed_product_id_returns_422():
    """GET /api/product/quality returns 422 when product_id is not a valid UUID."""
    engine = MagicMock()
    request = _make_starlette_request(
        {"namespace_id": _NAMESPACE_ID, "product_id": "bad-prod-uuid"}
    )

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 422
    body = json.loads(response.body)
    assert "Invalid product_id" in body["error"]


@pytest.mark.asyncio
async def test_api_product_quality_invalid_channel_returns_422():
    """GET /api/product/quality returns 422 when an unknown channel is requested."""
    engine = MagicMock()
    request = _make_starlette_request(
        {"namespace_id": _NAMESPACE_ID, "channel": "invalid_channel_xyz"}
    )

    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        response = await api_product_quality(request)

    assert response.status_code == 422
    body = json.loads(response.body)
    assert "Invalid channel" in body["error"]


@pytest.mark.asyncio
async def test_api_product_quality_engine_disconnected_returns_503():
    """GET /api/product/quality returns 503 when admin_state.engine is None."""
    request = _make_starlette_request({"namespace_id": _NAMESPACE_ID})

    with patch("nce.admin_handlers.product.admin_state") as mock_state:
        mock_state.engine = None
        response = await api_product_quality(request)

    assert response.status_code == 503
    body = json.loads(response.body)
    assert "Engine not connected" in body["error"]


@pytest.mark.asyncio
async def test_api_product_quality_no_forbidden_columns_leak():
    """ADR-0017: Product quality responses must never leak cost, margin, or BID data."""
    row = _sample_product_row()
    # Add accidental raw price columns to row to ensure handler does not pass them through
    row["cost_price"] = 50.0
    row["margin"] = 0.25
    row["bid_id"] = "BID-999"

    pool, _ = _make_pool(fetchrow_return=row, fetch_return=[row])
    engine = MagicMock()
    engine.pg_pool = pool

    # Single product check
    req_single = _make_starlette_request({"namespace_id": _NAMESPACE_ID, "product_id": _PRODUCT_ID})
    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        res_single = await api_product_quality(req_single)

    body_single = json.loads(res_single.body)
    for forbidden in _FORBIDDEN_COLUMNS:
        assert forbidden not in body_single

    # Rollup check
    req_rollup = _make_starlette_request({"namespace_id": _NAMESPACE_ID})
    with (
        patch("nce.admin_handlers.product.admin_state") as mock_state,
        patch(
            "nce.admin_handlers.product._check_product_enabled_rest",
            AsyncMock(return_value=None),
        ),
    ):
        mock_state.engine = engine
        res_rollup = await api_product_quality(req_rollup)

    body_rollup = json.loads(res_rollup.body)
    for forbidden in _FORBIDDEN_COLUMNS:
        assert forbidden not in body_rollup
