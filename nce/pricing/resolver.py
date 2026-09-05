"""
nce.pricing.resolver — Price resolution with precedence and freshness signal.

Resolves the cost for a (product, customer) pair using the precedence chain:

    customer BID  >  supplier list  >  base

The winning tier's cost, source label, and timestamp are returned.  A ``stale``
flag is set when the winning ``as_of`` timestamp is older than
``NCE_PRICING_MAX_AGE`` seconds — the stale value is **returned, never dropped**
(callers must decide how to handle it).

Per ADR-0017: cost/margin values must never cross to a customer-facing surface.
All callers are responsible for redacting ``cost`` before any external response.

Namespace isolation: when price rows are read from the database, the caller
must supply a connection obtained inside ``scoped_pg_session`` so the RLS
``nce.namespace_id`` GUC is active for all queries.
"""

from __future__ import annotations

import datetime
from typing import Any

from nce.config import cfg

# ---------------------------------------------------------------------------
# Public type alias
# ---------------------------------------------------------------------------

PriceResult = dict[str, Any]
"""
Return type of resolve_price::

    {
        "cost":   float,              # resolved cost (ADR-0017 — redact before external use)
        "source": str,                # "bid" | "supplier_list" | "base"
        "as_of":  datetime.datetime,  # timestamp of the winning price row
        "stale":  bool,               # True when (now − as_of) > NCE_PRICING_MAX_AGE
    }
"""


# ---------------------------------------------------------------------------
# Price-tier dataclass (lightweight, no extra dependency)
# ---------------------------------------------------------------------------


class PriceTier:
    """A single price candidate with cost, source label, and timestamp.

    Args:
        cost:   The cost value (internal domain value; see ADR-0017).
        source: One of "bid", "supplier_list", or "base".
        as_of:  Timezone-aware UTC timestamp when the price was last set.
    """

    __slots__ = ("cost", "source", "as_of")

    def __init__(self, cost: float, source: str, as_of: datetime.datetime) -> None:
        self.cost = cost
        self.source = source
        self.as_of = as_of


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------


def _is_stale(as_of: datetime.datetime, max_age_seconds: int) -> bool:
    """Return True when (now - as_of) exceeds max_age_seconds.

    A NAIVE ``as_of`` is read as UTC rather than rejected. This docstring's own
    contract says ``as_of (datetime, UTC)``, and a naive UTC timestamp satisfies
    that -- but ``now`` is aware, and subtracting the two raises
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``.

    That is not hypothetical. ``sales/dealroom.py`` reads its price tiers out of
    MongoDB, and BSON hands datetimes back **naive**, so every dated price
    arriving by that route crashed here. It was invisible because dealroom used
    to catch the exception and substitute a fabricated price; PR #187 removed
    the fabrication, and the crash surfaced as ``price_resolution_failed`` on a
    line whose price was perfectly well recorded.
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=datetime.timezone.utc)
    age = (now - as_of).total_seconds()
    return age > max_age_seconds


def _select_tier(
    bid: PriceTier | None,
    supplier_list: PriceTier | None,
    base: PriceTier | None,
) -> PriceTier:
    """Apply precedence: BID > supplier list > base.

    Raises ValueError when no tier is available (callers must seed at least base).
    """
    winning = bid or supplier_list or base
    if winning is None:
        raise ValueError(
            "resolve_price: no price tier available. At least a base price must be supplied."
        )
    return winning


async def resolve_price(
    conn: Any,
    *,
    namespace_id: str,
    product: dict[str, Any],
    customer: dict[str, Any],
) -> PriceResult:
    """Resolve cost for a (product, customer) pair.

    Precedence: customer BID > supplier list > base.

    The ``conn`` parameter must be a connection acquired inside
    ``scoped_pg_session`` so RLS ``nce.namespace_id`` is active for any
    database reads.  This wave derives price tiers from the ``product`` and
    ``customer`` dicts (no new DDL); future waves may issue scoped DB reads
    using ``conn``.

    Args:
        conn:         asyncpg connection inside a namespace-scoped transaction.
        namespace_id: Active namespace UUID string (passed to scoped_pg_session
                      by the caller — verified, not re-set here).
        product:      Dict with optional keys:
                        ``supplier_list_price`` (float),
                        ``supplier_list_as_of``  (datetime, UTC),
                        ``base_price``           (float),
                        ``base_as_of``           (datetime, UTC).
        customer:     Dict with optional keys:
                        ``bid_price``  (float),
                        ``bid_as_of``  (datetime, UTC).

    Returns:
        PriceResult dict with keys ``cost``, ``source``, ``as_of``, ``stale``.
        ``stale`` is True when ``as_of`` is older than ``NCE_PRICING_MAX_AGE``
        seconds.  A stale result is returned, never silently dropped — the
        caller decides how to handle it.

    Raises:
        ValueError: When no price tier is available in product or customer.
    """
    bid = _build_bid_tier(customer)
    supplier_list = _build_supplier_list_tier(product)
    base = _build_base_tier(product)

    winner = _select_tier(bid, supplier_list, base)

    stale = _is_stale(winner.as_of, cfg.NCE_PRICING_MAX_AGE)

    return {
        "cost": winner.cost,
        "source": winner.source,
        "as_of": winner.as_of,
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# Private tier builders — one concern each
# ---------------------------------------------------------------------------


def _build_bid_tier(customer: dict[str, Any]) -> PriceTier | None:
    """Extract customer BID tier from ``customer`` dict, or None."""
    bid_price = customer.get("bid_price")
    bid_as_of = customer.get("bid_as_of")
    if bid_price is None or bid_as_of is None:
        return None
    return PriceTier(cost=float(bid_price), source="bid", as_of=bid_as_of)


def _build_supplier_list_tier(product: dict[str, Any]) -> PriceTier | None:
    """Extract supplier list tier from ``product`` dict, or None."""
    list_price = product.get("supplier_list_price")
    list_as_of = product.get("supplier_list_as_of")
    if list_price is None or list_as_of is None:
        return None
    return PriceTier(cost=float(list_price), source="supplier_list", as_of=list_as_of)


def _build_base_tier(product: dict[str, Any]) -> PriceTier | None:
    """Extract base price tier from ``product`` dict, or None."""
    base_price = product.get("base_price")
    base_as_of = product.get("base_as_of")
    if base_price is None or base_as_of is None:
        return None
    return PriceTier(cost=float(base_price), source="base", as_of=base_as_of)
