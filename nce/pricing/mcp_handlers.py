"""
MCP handler for pricing resolution (Wave 13).

Wraps nce.pricing.resolver.resolve_price and returns the stale flag
verbatim in the response — never silently dropping it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id as _require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.orchestrator import NCEEngine
from nce.pricing.resolver import resolve_price


@mcp_handler
async def handle_pricing_resolve(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """Resolve pricing for a (product, customer) pair within a namespace.

    Arguments:
        namespace_id (str):           Required. UUID of the target namespace.
        product (dict, optional):     Product data with optional keys:
                                       - supplier_list_price (float)
                                       - supplier_list_as_of (ISO 8601 datetime)
                                       - base_price (float)
                                       - base_as_of (ISO 8601 datetime)
        customer (dict, optional):    Customer data with optional keys:
                                       - bid_price (float)
                                       - bid_as_of (ISO 8601 datetime)

    Returns (JSON):
        {
            "status": "ok",
            "cost": float,               # (ADR-0017: redact before external use)
            "source": "bid" | "supplier_list" | "base",
            "as_of": ISO 8601 datetime,
            "stale": bool,               # True if age > NCE_PRICING_MAX_AGE
        }

    The stale flag is returned as-is — callers must inspect it; never silently dropped.
    """
    namespace_id = uuid.UUID(_require_namespace_id(arguments))
    product = dict(arguments.get("product") or {})
    customer = dict(arguments.get("customer") or {})

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        result = await resolve_price(
            conn,
            namespace_id=str(namespace_id),
            product=product,
            customer=customer,
        )

    return json.dumps(
        {
            "status": "ok",
            "cost": result["cost"],
            "source": result["source"],
            "as_of": result["as_of"].isoformat(),
            "stale": result["stale"],
        }
    )
