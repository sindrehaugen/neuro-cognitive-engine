"""
tests/unit/test_product_pricing.py
====================================
Acceptance tests for Batch 034 — Module 2.Wave 4 (price-product).

Asserts:
  1. ``do_price_product`` delegates to ``nce.pricing.resolve_price``.
  2. ``do_price_product`` delegates to ``nce.pricing.dg_price``.
  3. No local DG arithmetic is performed (no ``*0.7``, no ``cost/(1-dg)``).
  4. Public shape contains ``sales_price``, ``source``, ``as_of``, ``stale``.
  5. Public shape never contains ``cost``, ``margin``, or BID (ADR-0017).
  6. ``stale=True`` is surfaced when the resolver flags a stale cost.
  7. ``product_price`` is registered in TOOL_REGISTRY with correct flags.

All tests are plain unit tests (no DB, no Redis).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NAMESPACE_ID = "00000000-0000-4000-8000-000000000002"
_FORBIDDEN_KEYS: frozenset[str] = frozenset({"cost", "margin", "bid_id", "cost_price", "unit_cost"})

_NOW = datetime.datetime(2024, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)

_PRODUCT_PARAMS: dict[str, Any] = {
    "namespace_id": _NAMESPACE_ID,
    "product": {
        "base_price": 100.0,
        "base_as_of": _NOW,
    },
    "customer": {},
}


def _make_engine() -> MagicMock:
    """Minimal NCEEngine mock with a pg_pool usable via scoped_pg_session."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="SET")
    conn.fetchval = AsyncMock(return_value=None)

    pool = AsyncMock()

    class _AsyncCtx:
        def __init__(self, obj: Any) -> None:
            self._obj = obj

        async def __aenter__(self) -> Any:
            return self._obj

        async def __aexit__(self, *_: Any) -> None:
            pass

    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    engine.pg_pool = pool
    return engine


@pytest.fixture(autouse=True)
def _patch_scoped_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace scoped_pg_session with a trivial pass-through for unit tests."""

    class _FakeScoped:
        def __init__(self, pool: Any, ns: str) -> None:
            self._pool = pool

        async def __aenter__(self) -> Any:
            return await self._pool.acquire().__aenter__()

        async def __aexit__(self, *_: Any) -> None:
            pass

    monkeypatch.setattr(
        "nce.vertical_modules.product.pricing.scoped_pg_session",
        _FakeScoped,
    )


# ---------------------------------------------------------------------------
# 1-2: do_price_product delegates to resolve_price + dg_price (not local maths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_calls_resolve_price() -> None:
    """resolve_price is called with conn, namespace_id, product, customer."""
    from nce.vertical_modules.product.pricing import do_price_product

    fake_result = {
        "cost": 100.0,
        "source": "base",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price", new_callable=AsyncMock
        ) as mock_resolve,
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=142.857),
    ):
        mock_resolve.return_value = fake_result
        engine = _make_engine()

        await do_price_product(engine, _PRODUCT_PARAMS)

    mock_resolve.assert_called_once()
    call_kwargs = mock_resolve.call_args
    assert call_kwargs.kwargs["namespace_id"] == _NAMESPACE_ID
    assert call_kwargs.kwargs["product"] == _PRODUCT_PARAMS["product"]
    assert call_kwargs.kwargs["customer"] == {}


@pytest.mark.asyncio
async def test_do_price_product_calls_dg_price() -> None:
    """dg_price is called with the cost returned by resolve_price."""
    from nce.vertical_modules.product.pricing import do_price_product

    fake_result = {
        "cost": 200.0,
        "source": "supplier_list",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.25),
        patch(
            "nce.vertical_modules.product.pricing.dg_price", return_value=266.667
        ) as mock_dg_price,
    ):
        engine = _make_engine()
        await do_price_product(engine, _PRODUCT_PARAMS)

    mock_dg_price.assert_called_once_with(200.0, 0.25)


# ---------------------------------------------------------------------------
# 3: No local arithmetic — dg_price return value is used as-is for sales_price
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_uses_dg_price_result_as_sales_price() -> None:
    """sales_price equals exactly what dg_price returns — no local transformation."""
    from nce.vertical_modules.product.pricing import do_price_product

    sentinel_sales_price = 999.123

    fake_result = {
        "cost": 100.0,
        "source": "base",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=sentinel_sales_price),
    ):
        engine = _make_engine()
        result = await do_price_product(engine, _PRODUCT_PARAMS)

    assert result["sales_price"] == sentinel_sales_price, (
        "sales_price must be the exact value returned by dg_price — no local transformation"
    )


# ---------------------------------------------------------------------------
# 4: Public shape — required fields present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_public_shape() -> None:
    """Result contains sales_price, source, as_of, stale — and nothing hidden."""
    from nce.vertical_modules.product.pricing import do_price_product

    fake_result = {
        "cost": 100.0,
        "source": "base",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=142.857),
    ):
        engine = _make_engine()
        result = await do_price_product(engine, _PRODUCT_PARAMS)

    assert "sales_price" in result
    assert "source" in result
    assert "as_of" in result
    assert "stale" in result


# ---------------------------------------------------------------------------
# 5: ADR-0017 — cost/margin/BID never on the public shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_no_forbidden_keys() -> None:
    """cost, margin, BID must not appear on the returned dict (ADR-0017)."""
    from nce.vertical_modules.product.pricing import do_price_product

    fake_result = {
        "cost": 100.0,
        "source": "bid",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=fake_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=142.857),
    ):
        engine = _make_engine()
        result = await do_price_product(engine, _PRODUCT_PARAMS)

    for forbidden in _FORBIDDEN_KEYS:
        assert forbidden not in result, (
            f"ADR-0017 violation: forbidden key '{forbidden}' found on public shape"
        )


# ---------------------------------------------------------------------------
# 6: stale=True is surfaced when resolver flags stale cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_surfaces_stale_flag() -> None:
    """When resolve_price returns stale=True, do_price_product propagates it."""
    from nce.vertical_modules.product.pricing import do_price_product

    stale_result = {
        "cost": 100.0,
        "source": "supplier_list",
        "as_of": _NOW,
        "stale": True,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=stale_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=142.857),
    ):
        engine = _make_engine()
        result = await do_price_product(engine, _PRODUCT_PARAMS)

    assert result["stale"] is True


@pytest.mark.asyncio
async def test_do_price_product_surfaces_not_stale_flag() -> None:
    """When resolve_price returns stale=False, do_price_product propagates it."""
    from nce.vertical_modules.product.pricing import do_price_product

    fresh_result = {
        "cost": 100.0,
        "source": "base",
        "as_of": _NOW,
        "stale": False,
    }

    with (
        patch(
            "nce.vertical_modules.product.pricing.resolve_price",
            new_callable=AsyncMock,
            return_value=fresh_result,
        ),
        patch("nce.vertical_modules.product.pricing.load_dg", return_value=0.3),
        patch("nce.vertical_modules.product.pricing.dg_price", return_value=142.857),
    ):
        engine = _make_engine()
        result = await do_price_product(engine, _PRODUCT_PARAMS)

    assert result["stale"] is False


# ---------------------------------------------------------------------------
# Validation: missing required params raise ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_price_product_missing_namespace_raises() -> None:
    from nce.vertical_modules.product.pricing import do_price_product

    engine = _make_engine()
    with pytest.raises((ValueError, KeyError)):
        await do_price_product(engine, {"product": {"base_price": 100.0, "base_as_of": _NOW}})


@pytest.mark.asyncio
async def test_do_price_product_missing_product_raises() -> None:
    from nce.vertical_modules.product.pricing import do_price_product

    engine = _make_engine()
    with pytest.raises(ValueError, match="product"):
        await do_price_product(engine, {"namespace_id": _NAMESPACE_ID})


# ---------------------------------------------------------------------------
# 7: Tool registry — product_price registered with correct flags
# ---------------------------------------------------------------------------


def test_product_price_registered_with_correct_flags() -> None:
    from nce.tool_registry import TOOL_REGISTRY

    assert "product_price" in TOOL_REGISTRY, "product_price not found in TOOL_REGISTRY"
    spec = TOOL_REGISTRY["product_price"]
    assert spec.cacheable is True
    assert spec.mutation is False
    assert spec.admin_only is False
    assert spec.migration is False
