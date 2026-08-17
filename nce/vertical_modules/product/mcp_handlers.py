"""
nce/vertical_modules/product/mcp_handlers.py
============================================
MCP tool handlers for the Product vertical module (W3: search-get-graph;
W4: price-product; W5: related-products; W6: match-bom-line; W7: enrich-product).

Public entry-points:
  ``handle_product_search``       — keyword search over product_catalog (lexical floor;
  semantic stays off this wave).
  ``handle_product_get``          — single product: master row + live prices + graph edges.
  ``handle_product_price``        — resolve sales price via C6 shared pricing service.
  ``handle_product_related``      — derive and persist accessory/warranty/mount/replacement
  edges for a product (W5).
  ``handle_product_match_bom_line`` — resolve a free-text BOM line to the best SKU (W6).
  ``handle_product_enrich``       — on-demand enrichment of one product's missing fields
  through the C2 ``@governed`` gate (W7, mutation).

W3-W6 are read-only advisor tools (cacheable=True/False, mutation=False).
W7 is mutation=True (governed confirm-only write).
None return cost, margin, or BID data (ADR-0017).

Registered in ``nce/tool_registry.py`` via ``_h(product_mcp_handlers, "handle_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.mcp_errors import McpError, mcp_handler
from nce.vertical_modules.product._guard import (
    ProductDisabledError,
    require_product_enabled,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.product.mcp_handlers")


# ---------------------------------------------------------------------------
# Shared opt-in guard — applied at handler boundary (not inside do_* cores)
# ---------------------------------------------------------------------------

_MCP_PRODUCT_DISABLED_CODE: int = -32005  # MCP_SCOPE_FORBIDDEN


async def _check_product_enabled(engine: NCEEngine, arguments: dict[str, Any]) -> None:
    """Check namespace opt-in; raise McpError(-32005) if not enabled."""
    namespace_id = require_namespace_id(arguments)
    try:
        await require_product_enabled(engine.pg_pool, namespace_id)
    except ProductDisabledError as exc:
        raise McpError(
            _MCP_PRODUCT_DISABLED_CODE,
            "Product vertical is not enabled for this namespace",
            data={"reason": "product_disabled", "detail": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# Forbidden columns — never leak to callers
# ---------------------------------------------------------------------------
_HIDDEN_COLUMNS: frozenset[str] = frozenset({"cost_price", "bid_id", "margin"})

# Maximum rows returned from search (guard against large result dumps).
_SEARCH_MAX_ROWS: int = 50


# ---------------------------------------------------------------------------
# Core: do_search_products
# ---------------------------------------------------------------------------


async def do_search_products(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Hybrid lexical search over product_catalog, namespace-scoped.

    Uses PostgreSQL full-text search (``to_tsvector`` + ``plainto_tsquery``) for
    the query term; falls back to a ``ILIKE`` floor when the query is too short
    for the FTS parser to emit useful tokens.

    Semantic re-ranking stays off this wave (design note: vector column not yet
    populated for this engine; add in a later wave when embeddings land).

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id`` (str, required)
        ``query``        (str, required) — search term.
        ``limit``        (int, optional, default 20, max 50)

    Returns
    -------
    dict with keys ``results`` (list of product rows, safe projection) and
    ``total`` (int, number of rows returned).  No cost/margin/BID.
    """
    namespace_id = require_namespace_id(params)
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required and must be a non-empty string")

    raw_limit = params.get("limit", 20)
    try:
        limit = max(1, min(_SEARCH_MAX_ROWS, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 20

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # Full-text search with an ILIKE fallback for very-short terms (<3 chars)
        # that the FTS parser would reject or produce no tokens for.
        if len(query) >= 3:
            rows = await conn.fetch(
                """
                SELECT id, manufacturer, mfr_part_no, gtin, lifecycle_status,
                       etim_specs, updated_at
                FROM   product_catalog
                WHERE  is_deleted = false
                  AND  (
                           to_tsvector('simple', manufacturer || ' ' || mfr_part_no)
                           @@ plainto_tsquery('simple', $1)
                       )
                ORDER BY ts_rank(
                    to_tsvector('simple', manufacturer || ' ' || mfr_part_no),
                    plainto_tsquery('simple', $1)
                ) DESC
                LIMIT  $2
                """,
                query,
                limit,
            )
        else:
            pattern = f"%{query}%"
            rows = await conn.fetch(
                """
                SELECT id, manufacturer, mfr_part_no, gtin, lifecycle_status,
                       etim_specs, updated_at
                FROM   product_catalog
                WHERE  is_deleted = false
                  AND  (
                           manufacturer ILIKE $1
                        OR mfr_part_no  ILIKE $1
                       )
                ORDER BY manufacturer, mfr_part_no
                LIMIT  $2
                """,
                pattern,
                limit,
            )

    results = [_safe_row(dict(r)) for r in rows]
    return {"results": results, "total": len(results)}


# ---------------------------------------------------------------------------
# Core: do_get_product
# ---------------------------------------------------------------------------


async def do_get_product(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch a single product: master row + live prices + outbound graph edges.

    Parameters
    ----------
    engine:
        Live NCEEngine instance.
    params:
        ``namespace_id``  (str, required)
        ``mfr_part_no``   (str, required) — unique key within the namespace.
        ``manufacturer``  (str, optional) — disambiguates when two manufacturers
                          share a part number.

    Returns
    -------
    dict with keys:
      ``product``  — safe master row (no cost/margin/BID).
      ``prices``   — list of safe price rows (list_price only; cost_price/bid_id excluded).
      ``edges``    — list of outbound kg_edges originating from the PRODUCT node.
    """
    namespace_id = require_namespace_id(params)
    mfr_part_no = str(params.get("mfr_part_no") or "").strip()
    if not mfr_part_no:
        raise ValueError("'mfr_part_no' is required")

    manufacturer = str(params.get("manufacturer") or "").strip() or None

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        if manufacturer:
            master = await conn.fetchrow(
                """
                SELECT id, manufacturer, mfr_part_no, gtin, lifecycle_status,
                       etim_specs, created_at, updated_at
                FROM   product_catalog
                WHERE  is_deleted = false
                  AND  mfr_part_no  = $1
                  AND  manufacturer = $2
                LIMIT  1
                """,
                mfr_part_no,
                manufacturer,
            )
        else:
            master = await conn.fetchrow(
                """
                SELECT id, manufacturer, mfr_part_no, gtin, lifecycle_status,
                       etim_specs, created_at, updated_at
                FROM   product_catalog
                WHERE  is_deleted = false
                  AND  mfr_part_no = $1
                LIMIT  1
                """,
                mfr_part_no,
            )

        if master is None:
            return {"product": None, "prices": [], "edges": []}

        # Live prices — strip cost_price and bid_id (ADR-0017)
        price_rows = await conn.fetch(
            """
            SELECT supplier, list_price, updated_at
            FROM   product_prices
            WHERE  mfr_part_no = $1
            ORDER  BY supplier
            """,
            mfr_part_no,
        )

        # Outbound graph edges for this PRODUCT node
        kg_label = _product_label(master["manufacturer"], mfr_part_no)
        edge_rows = await conn.fetch(
            """
            SELECT predicate, object_label, confidence, updated_at
            FROM   kg_edges
            WHERE  subject_label = $1
            ORDER  BY predicate, object_label
            LIMIT  200
            """,
            kg_label,
        )

    product = _safe_row(dict(master))
    prices = [_safe_price_row(dict(r)) for r in price_rows]
    edges = [_safe_edge_row(dict(r)) for r in edge_rows]
    return {"product": product, "prices": prices, "edges": edges}


# ---------------------------------------------------------------------------
# Thin MCP handles — just serialise the core output
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_product_search(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_search — keyword search over the product catalog."""
    await _check_product_enabled(engine, arguments)
    result = await do_search_products(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_product_get(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_get — fetch one product with live prices and graph edges."""
    await _check_product_enabled(engine, arguments)
    result = await do_get_product(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_product_price(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_price — resolve sales price via C6 shared pricing service."""
    await _check_product_enabled(engine, arguments)
    from nce.vertical_modules.product.pricing import do_price_product

    result = await do_price_product(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_product_related(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_related — derive and persist related-product graph edges."""
    await _check_product_enabled(engine, arguments)
    from nce.vertical_modules.product.related import do_related_products

    result = await do_related_products(engine, arguments)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_product_enrich(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_enrich — on-demand enrichment of one product's missing fields.

    Goes through the C2 ``@governed`` gate (confirm-only default).  Without
    ``confirm=True`` the tool returns ``{"status": "pending_approval", ...}``.
    With ``confirm=True`` it runs once (idempotent on replay) and writes
    enrichment proposals to ``product_enrichment_log`` (FORCE RLS).

    Required arguments:
      ``namespace_id``  (str, UUID)
      ``product_id``    (str, UUID) — exactly ONE product, never a list.
      ``trigger_context`` (dict) — ``{kind, ref_id, missing_fields, source_watermark}``

    Optional arguments:
      ``confirm``          (bool, default False)
      ``idempotency_key``  (str) — caller-supplied override; when absent a stable
                           hash of ``(product_id, missing_fields, source_watermark)``
                           is derived automatically.
    """
    await _check_product_enabled(engine, arguments)

    import uuid as _uuid

    from nce.db_utils import scoped_pg_session
    from nce.mcp_args import require_namespace_id
    from nce.vertical_modules.product.enrich import (
        _derive_idempotency_key,
        do_enrich_product,
    )

    namespace_id = require_namespace_id(arguments)
    product_id = str(arguments.get("product_id") or "").strip()
    if not product_id:
        raise ValueError("'product_id' is required")

    trigger_context: dict[str, Any] = dict(arguments.get("trigger_context") or {})
    confirm: bool = bool(arguments.get("confirm", False))

    # Derive stable idempotency key unless the caller supplied one.
    caller_key = str(arguments.get("idempotency_key") or "").strip()
    if caller_key:
        idem_key = caller_key
    else:
        missing_fields = list(trigger_context.get("missing_fields") or [])
        source_watermark = str(trigger_context.get("source_watermark") or "")
        idem_key = _derive_idempotency_key(product_id, missing_fields, source_watermark)

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        result = await do_enrich_product(
            conn,
            _uuid.UUID(namespace_id),
            idempotency_key=idem_key,
            confirm=confirm,
            product_id=product_id,
            trigger_context=trigger_context,
        )

    return json.dumps(result, default=str)


@mcp_handler
async def handle_product_match_bom_line(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: product_match_bom_line — resolve a free-text BOM line to the best SKU.

    Delegates ranking to the C1 ``resolve()`` primitive.  When a ``decision``
    param ('accept' or 'override') is present the call records a learning event
    in ``product_match_feedback`` instead of returning ranked matches.
    """
    await _check_product_enabled(engine, arguments)
    from nce.vertical_modules.product.matching import do_match_bom_line

    result = await do_match_bom_line(engine, arguments)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _product_label(manufacturer: str, mfr_part_no: str) -> str:
    """Canonical kg_nodes label for a PRODUCT_SKU node (matches graph.py)."""
    return f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"


def _safe_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip any hidden (cost/margin/BID) columns from a catalog row."""
    return {k: v for k, v in row.items() if k not in _HIDDEN_COLUMNS}


def _safe_price_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return only the public price columns (list_price + supplier + timestamp)."""
    return {k: v for k, v in row.items() if k not in _HIDDEN_COLUMNS}


def _safe_edge_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return edge columns safe for external projection."""
    return {k: v for k, v in row.items() if k not in _HIDDEN_COLUMNS}
