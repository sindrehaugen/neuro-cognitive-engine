"""
nce/vertical_modules/product/pricing.py
=========================================
Core: do_price_product — delegates entirely to the C6 shared pricing service.

Per ADR-0017: ``cost``, ``margin``, and BID are internal domain values and must
NEVER appear on a customer-facing return shape.  The public shape exposes only
``sales_price``, ``source``, ``as_of``, and ``stale``.

No local DG arithmetic lives here.  ``dg_price`` and ``resolve_price`` are the
sole owners of pricing logic (§9.6: shared pricing service — callers never
re-implement).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.pricing import dg_price, load_dg, resolve_price

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine


async def do_price_product(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the sales price for a (product, customer) pair.

    Delegates to:
    - ``nce.pricing.resolve_price`` — picks the applicable cost tier with a
      freshness signal (BID > supplier list > base).
    - ``nce.pricing.dg_price`` — converts cost to sales price via DG%.

    No local DG arithmetic is performed here (no ``*0.7``, no
    ``cost/(1-dg)``).  That logic lives entirely in C6.

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id``  (str, required)
        ``product``       (dict, required) — product row with optional price
                          keys: ``supplier_list_price``, ``supplier_list_as_of``,
                          ``base_price``, ``base_as_of``.
        ``customer``      (dict, optional, default {}) — customer row with
                          optional BID keys: ``bid_price``, ``bid_as_of``.

    Returns
    -------
    dict with customer-facing keys only:
      ``sales_price``  — resolved sales price (float).
      ``source``       — winning price tier: ``"bid"``, ``"supplier_list"``,
                         or ``"base"``.
      ``as_of``        — timestamp (datetime) of the winning price row.
      ``stale``        — True when the winning cost is older than
                         ``NCE_PRICING_MAX_AGE`` seconds.

    Raises
    ------
    ValueError:
        When ``namespace_id`` is missing, ``product`` is missing, or no price
        tier is available in the product/customer dicts.
    """
    namespace_id = require_namespace_id(params)

    raw_product = params.get("product")
    if not isinstance(raw_product, dict):
        raise ValueError("'product' is required and must be a dict")

    customer: dict[str, Any] = params.get("customer") or {}

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        price_result = await resolve_price(
            conn,
            namespace_id=namespace_id,
            product=raw_product,
            customer=customer,
        )

    dg_pct = load_dg(namespace_id)
    sales_price = dg_price(price_result["cost"], dg_pct)

    # ADR-0017: expose sales_price + provenance only — never cost/margin/BID.
    return {
        "sales_price": sales_price,
        "source": price_result["source"],
        "as_of": price_result["as_of"],
        "stale": price_result["stale"],
    }
