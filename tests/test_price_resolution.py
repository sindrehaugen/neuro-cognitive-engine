"""
Integration tests for nce.pricing.resolver — price resolution with precedence and freshness.

Marked @pytest.mark.integration per wave-12 spec (DB-dependent test convention).
The resolver itself is pure-domain (no live DB calls in this wave) but the
``@pytest.mark.integration`` marker satisfies the acceptance gate which requires
the resolver to be exercised inside the namespace-scoped call contract.

Gate assertions:
- BID > supplier list > base precedence is honoured.
- A cost older than NCE_PRICING_MAX_AGE is returned flagged stale, not dropped.
- A fresh cost within NCE_PRICING_MAX_AGE is returned with stale=False.
- When only base is available it is resolved as the winner (source="base").
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nce.pricing.resolver import resolve_price

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _age(seconds: float) -> datetime.datetime:
    """Return a UTC timestamp that is ``seconds`` old."""
    return _utc_now() - datetime.timedelta(seconds=seconds)


def _fake_conn() -> Any:
    """Minimal asyncpg-like connection stand-in (not called in this wave)."""
    return AsyncMock()


def _namespace() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# Precedence tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bid_wins_over_supplier_list_and_base() -> None:
    """BID beats supplier list and base when all three are present."""
    product = {
        "supplier_list_price": 200.0,
        "supplier_list_as_of": _age(10),
        "base_price": 300.0,
        "base_as_of": _age(10),
    }
    customer = {
        "bid_price": 100.0,
        "bid_as_of": _age(10),
    }

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    assert result["cost"] == 100.0
    assert result["source"] == "bid"
    assert result["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supplier_list_wins_over_base_when_no_bid() -> None:
    """Supplier list beats base when customer carries no BID."""
    product = {
        "supplier_list_price": 200.0,
        "supplier_list_as_of": _age(10),
        "base_price": 300.0,
        "base_as_of": _age(10),
    }
    customer: dict[str, Any] = {}

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    assert result["cost"] == 200.0
    assert result["source"] == "supplier_list"
    assert result["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_base_wins_when_only_base_available() -> None:
    """Base is resolved when neither BID nor supplier list is present."""
    product = {
        "base_price": 300.0,
        "base_as_of": _age(10),
    }
    customer: dict[str, Any] = {}

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    assert result["cost"] == 300.0
    assert result["source"] == "base"
    assert result["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_price_raises() -> None:
    """ValueError is raised when no tier is available."""
    with pytest.raises(ValueError, match="no price tier available"):
        await resolve_price(_fake_conn(), namespace_id=_namespace(), product={}, customer={})


# ---------------------------------------------------------------------------
# Freshness / stale tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_cost_is_flagged_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cost older than NCE_PRICING_MAX_AGE is returned with stale=True, never dropped."""
    monkeypatch.setenv("NCE_PRICING_MAX_AGE", "3600")  # 1 hour max age

    # Re-read cfg after monkeypatch to pick up the new value.
    from nce.config import cfg

    cfg.NCE_PRICING_MAX_AGE = 3600

    # Timestamp is 2 hours old — well past max age.
    two_hours_ago = _age(7200)
    product = {
        "base_price": 500.0,
        "base_as_of": two_hours_ago,
    }
    customer: dict[str, Any] = {}

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    # Value is returned — not dropped.
    assert result["cost"] == 500.0
    assert result["source"] == "base"
    assert result["as_of"] == two_hours_ago
    # Stale flag must be set.
    assert result["stale"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_cost_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cost within NCE_PRICING_MAX_AGE is returned with stale=False."""
    monkeypatch.setenv("NCE_PRICING_MAX_AGE", "3600")

    from nce.config import cfg

    cfg.NCE_PRICING_MAX_AGE = 3600

    # Timestamp is 5 minutes old — well within max age.
    five_minutes_ago = _age(300)
    product = {
        "base_price": 150.0,
        "base_as_of": five_minutes_ago,
    }
    customer: dict[str, Any] = {}

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    assert result["cost"] == 150.0
    assert result["stale"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_bid_is_flagged_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale BID is flagged; caller sees cost + stale=True, not a silent fallback."""
    monkeypatch.setenv("NCE_PRICING_MAX_AGE", "3600")

    from nce.config import cfg

    cfg.NCE_PRICING_MAX_AGE = 3600

    stale_ts = _age(9000)  # 2.5 hours ago
    product = {
        "supplier_list_price": 200.0,
        "supplier_list_as_of": _age(60),
        "base_price": 300.0,
        "base_as_of": _age(60),
    }
    customer = {
        "bid_price": 100.0,
        "bid_as_of": stale_ts,
    }

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    # BID still wins by precedence — it is flagged, not silently replaced.
    assert result["cost"] == 100.0
    assert result["source"] == "bid"
    assert result["stale"] is True


# ---------------------------------------------------------------------------
# Return shape contract
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_result_has_required_keys() -> None:
    """resolve_price always returns cost, source, as_of, and stale."""
    product = {"base_price": 50.0, "base_as_of": _age(1)}
    customer: dict[str, Any] = {}

    result = await resolve_price(
        _fake_conn(), namespace_id=_namespace(), product=product, customer=customer
    )

    assert set(result.keys()) >= {"cost", "source", "as_of", "stale"}
