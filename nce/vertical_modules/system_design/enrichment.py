"""
nce/vertical_modules/system_design/enrichment.py
=================================================
Scoped A2A enrichment for System Design DESIGN_LINEs (Phase 1a step 6).

When a design's DESIGN_LINEs reference PRODUCTs with missing specs/pricing,
this module asks **Product** to enrich **each referenced product** (via the
Module 2.W8 ``enqueue_product_enrichment`` fire-and-backfill tool) and asks
**Procurement** for a live TCO estimate for each line.

Design invariants (§5 / ADR scoped-enrichment):
  - SCOPED: every A2A call is tied to one of the design's referenced products.
    Never enumerate the catalog; never trigger enrichment for products not on
    this design (rule: "scope every call to the design's referenced products
    only").
  - FIRE-AND-BACKFILL: Product enrichment is dispatched without blocking the
    caller.  A failed or slow A2A call MUST NOT raise into the caller or block
    a proposal/quote/SoW.  Results backfill the lines/edges asynchronously.
  - Procurement TCO is pure/synchronous (``do_calculate_tco`` is a zero-I/O
    function).  It runs inline but does NOT depend on the enrichment completing.

Graph data model (Wave 2 — do NOT query a relational ``design_lines`` table):
  - DESIGN_LINE nodes live in ``kg_nodes`` with entity_type='DESIGN_LINE'.
  - Labels: ``DESIGN_LINE:<DESIGN_ID>:<LINE_REF>`` (all upper-cased).
  - ``DESIGN:<DESIGN_ID> -[contains]-> DESIGN_LINE:<…>`` edges in kg_edges.
  - ``DESIGN_LINE:<…> -[references]-> PRODUCT:<MFR>:<PART>`` edges in kg_edges.
  - Product price/qty are NOT stored in the graph; they come from product_catalog
    via ``resolve_price``.

Dependency rule (uncle-bob inward):
  - Imports from ``nce.db_utils``, ``nce.vertical_modules.product.a2a``,
    ``nce.vertical_modules.procurement.tco``, ``nce.pricing.resolver``,
    and stdlib only.
  - No web / admin / HTTP modules.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.procurement.tco import do_calculate_tco, load_procurement_config
from nce.vertical_modules.product.a2a import enqueue_product_enrichment

log = logging.getLogger("nce.vertical_modules.system_design.enrichment")

# Stable low-cardinality trigger kind for enrichment requests from this engine.
_TRIGGER_KIND = "design"

# Edge predicates that define the design-line → product relationship (Wave 2).
_PRED_CONTAINS = "contains"
_PRED_REFERENCES = "references"


# ---------------------------------------------------------------------------
# Private: build canonical labels (mirrors graph.py helpers — no extra import)
# ---------------------------------------------------------------------------


def _design_label_for(design_id: str) -> str:
    """Canonical DESIGN label: ``DESIGN:<DESIGN_ID>`` (upper-cased)."""
    return f"DESIGN:{design_id.upper()}"


# ---------------------------------------------------------------------------
# Private: fetch DESIGN_LINE → PRODUCT pairs from the knowledge graph
# ---------------------------------------------------------------------------


async def _fetch_design_lines(
    conn: Any,
    ns_uuid: UUID,
    design_id: str,
) -> list[dict[str, Any]]:
    """Return DESIGN_LINE entries that reference a PRODUCT, via the kg graph.

    Reads two kg_edges hops:
      1. ``DESIGN:<design_id> -[contains]-> DESIGN_LINE:*``
      2. ``DESIGN_LINE:* -[references]-> PRODUCT:*``

    Only design lines that have a ``references`` edge to a PRODUCT label are
    returned — lines without a product reference have nothing to enrich.

    Returns a list of dicts::

        {
            "design_line_label": str,   # e.g. "DESIGN_LINE:DESIGN-X:L1"
            "product_label":     str,   # e.g. "PRODUCT:BIAMP:TESIRAFORTE-CI"
        }

    The query is bounded to one design: never a full-catalog scan.
    """
    design_lbl = _design_label_for(design_id)

    rows = await conn.fetch(
        """
        SELECT dl_edge.object_label  AS design_line_label,
               ref_edge.object_label AS product_label
        FROM   kg_edges dl_edge
        JOIN   kg_edges ref_edge
               ON  ref_edge.subject_label  = dl_edge.object_label
               AND ref_edge.predicate      = $3
               AND ref_edge.namespace_id   = dl_edge.namespace_id
               AND ref_edge.object_label   LIKE 'PRODUCT:%'
        WHERE  dl_edge.subject_label = $1
          AND  dl_edge.predicate     = $2
          AND  dl_edge.namespace_id  = $4::uuid
          AND  dl_edge.object_label  LIKE 'DESIGN_LINE:%'
        """,
        design_lbl,
        _PRED_CONTAINS,
        _PRED_REFERENCES,
        str(ns_uuid),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Private: resolve a PRODUCT label to a product_catalog UUID
# ---------------------------------------------------------------------------


async def _resolve_product_uuid(
    conn: Any,
    ns_uuid: UUID,
    product_label: str,
) -> str | None:
    """Parse ``PRODUCT:<MFR>:<PART>`` and look up the product_catalog UUID.

    Returns the product UUID string, or None when no matching catalog row
    exists (product unknown to this tenant — cannot enrich).
    """
    # Label format: "PRODUCT:<MANUFACTURER>:<MFR_PART_NO>"
    parts = product_label.split(":", 2)
    if len(parts) != 3:  # noqa: PLR2004
        log.warning(
            "[enrichment] unexpected product_label format=%r — skipping",
            product_label,
        )
        return None

    _, mfr_upper, part_upper = parts

    row = await conn.fetchrow(
        """
        SELECT id
        FROM   product_catalog
        WHERE  upper(manufacturer) = $1
          AND  upper(mfr_part_no)  = $2
          AND  is_deleted          = false
        LIMIT  1
        """,
        mfr_upper,
        part_upper,
    )
    if row is None:
        log.info(
            "[enrichment] product_catalog miss: mfr=%s part=%s ns=%s — skipping",
            mfr_upper,
            part_upper,
            str(ns_uuid)[:8],
        )
        return None
    return str(row["id"])


# ---------------------------------------------------------------------------
# Private: fire product enrichment for one product (never raises into caller)
# ---------------------------------------------------------------------------


def _fire_product_enrichment(
    pg_pool: Any,
    namespace_id: str,
    product_id: str,
    design_id: str,
    missing_fields: list[str],
) -> None:
    """Enqueue enrichment for one product — fire-and-backfill, never raises.

    Uses ``enqueue_product_enrichment`` (synchronous; creates a tracked
    background task internally).  Exceptions from the enqueue step itself
    are caught and logged so that a broken enrichment path never propagates
    into proposal/quote/SoW callers.
    """
    trigger_context: dict[str, Any] = {
        "kind": _TRIGGER_KIND,
        "ref_id": design_id,
        "missing_fields": missing_fields,
        "source_watermark": design_id,
    }
    try:
        enqueue_product_enrichment(
            pg_pool=pg_pool,
            namespace_id=namespace_id,
            product_id=product_id,
            trigger_context=trigger_context,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "[enrichment] failed to enqueue product enrichment: "
            "design=%s product=%s — continuing (fire-and-backfill)",
            design_id,
            product_id[:8] if len(product_id) >= 8 else product_id,
        )


# ---------------------------------------------------------------------------
# Private: resolve price for one product (never raises into caller)
# ---------------------------------------------------------------------------


async def _resolve_unit_price(
    pg_pool: Any,
    ns_uuid: UUID,
    product_id: str,  # noqa: ARG001 — reserved for future DB price lookup
    namespace_id_str: str,
) -> float | None:
    """Attempt to resolve a unit price for the product via the scoped DB.

    Reads price rows from ``product_catalog`` (base_price / supplier_list_price
    columns do not exist — prices are in ``product_prices``).  The shared
    ``resolve_price`` function derives price tiers from the product/customer dicts;
    since kg_nodes stores no price data, we attempt a minimal lookup from
    ``product_prices`` first.  When no price row is available, returns None so
    the caller can skip TCO gracefully.

    Exceptions are caught and logged — a missing price must never block
    a proposal/quote/SoW.
    """
    try:
        async with scoped_pg_session(pg_pool, ns_uuid) as conn:
            price_row = await conn.fetchrow(
                """
                SELECT list_price, cost_price
                FROM   product_prices pp
                JOIN   product_catalog pc
                       ON  upper(pp.mfr_part_no) = upper(pc.mfr_part_no)
                WHERE  pc.id           = $1::uuid
                  AND  pp.namespace_id = $2::uuid
                ORDER BY pp.updated_at DESC
                LIMIT  1
                """,
                product_id,
                namespace_id_str,
            )
        if price_row is None:
            return None
        # Prefer list_price; fall back to cost_price.
        raw = price_row["list_price"] or price_row["cost_price"]
        return float(raw) if raw is not None else None
    except Exception:  # noqa: BLE001
        log.exception(
            "[enrichment] price lookup failed for product=%s — skipping TCO",
            product_id[:8] if len(product_id) >= 8 else product_id,
        )
        return None


# ---------------------------------------------------------------------------
# Private: compute TCO for one product+price (pure, never raises into caller)
# ---------------------------------------------------------------------------


def _compute_tco(
    weights: dict[str, Any],
    tolerances: dict[str, Any],
    unit_price: float,
    quantity: int,
    product_id: str,
) -> dict[str, Any] | None:
    """Return a TCO breakdown for one product line, or None on failure.

    ``do_calculate_tco`` is a pure function — no I/O.  Errors (e.g. bad config)
    are caught and logged; the caller continues with the remaining lines.
    """
    supplier: dict[str, Any] = {"unit_price": unit_price}
    bom_line: dict[str, Any] = {"quantity": quantity, "unit_price": unit_price}
    try:
        return do_calculate_tco(weights, tolerances, supplier, bom_line)
    except Exception:  # noqa: BLE001
        log.exception(
            "[enrichment] TCO calculation failed for product=%s — skipping",
            product_id[:8] if len(product_id) >= 8 else product_id,
        )
        return None


# ---------------------------------------------------------------------------
# Public: do_enrich_design_lines
# ---------------------------------------------------------------------------


async def do_enrich_design_lines(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Fire scoped Product enrichment and Procurement TCO for a design's lines.

    Reads the knowledge graph (kg_edges) to discover DESIGN_LINEs and their
    referenced PRODUCTs.  For each unique referenced product:
      1. Resolves the PRODUCT label to a ``product_catalog`` UUID (scoped lookup).
      2. Calls ``enqueue_product_enrichment`` once per unique product UUID.
      3. Attempts to resolve a unit price and compute TCO.

    Enrichment is FIRE-AND-BACKFILL:
      - ``enqueue_product_enrichment`` returns synchronously and schedules the
        enrichment as a background task.  This function does NOT await its
        completion.
      - Any failure in the enqueue, price-lookup, or TCO step is caught and
        logged; it never raises into the caller (proposal/quote/SoW paths
        remain unaffected).

    Parameters
    ----------
    engine:
        NCEEngine instance — must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str,   # required
            "design_id":    str,   # required — the DESIGN to enrich
            "missing_fields": list[str],  # optional, default ["etim_specs"]
        }``

    Returns
    -------
    dict
        ``{
            "design_id":          str,
            "lines_found":        int,   # total DESIGN_LINEs with a product ref
            "products_enqueued":  int,   # unique products fired for enrichment
            "products_skipped":   int,   # product labels not in catalog
            "tco_computed":       int,   # lines for which TCO was computed
            "tco_skipped":        int,   # lines skipped (no price data yet)
            "enrichment":         "queued",
        }``
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_enrich_design_lines: 'namespace_id' is required in params")

    design_id = str(params.get("design_id") or "").strip()
    if not design_id:
        raise ValueError("do_enrich_design_lines: 'design_id' is required in params")

    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw
    namespace_id_str = str(ns_uuid)

    missing_fields: list[str] = list(params.get("missing_fields") or ["etim_specs"])

    # Load TCO config once — pure I/O, outside the DB transaction.
    weights, tolerances = load_procurement_config()

    # -----------------------------------------------------------------------
    # 1. Fetch graph edges: DESIGN -[contains]-> DESIGN_LINE -[references]-> PRODUCT
    #    This is the only source of design-line → product relationships.
    #    Never queries a relational design_lines table.
    # -----------------------------------------------------------------------
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        graph_rows = await _fetch_design_lines(conn, ns_uuid, design_id)

    log.info(
        "[enrichment] design=%s ns=%s found %d graph edge(s) with product refs",
        design_id,
        namespace_id_str[:8],
        len(graph_rows),
    )

    # -----------------------------------------------------------------------
    # 2. Resolve PRODUCT labels → product_catalog UUIDs (scoped).
    #    Dedup by resolved UUID so two design-lines sharing the same catalog
    #    product fire exactly one enrichment job.
    # -----------------------------------------------------------------------
    # Map: product_label -> product_catalog UUID (None = not in catalog)
    label_to_uuid: dict[str, str | None] = {}

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        for row in graph_rows:
            lbl: str = row["product_label"]
            if lbl not in label_to_uuid:
                label_to_uuid[lbl] = await _resolve_product_uuid(conn, ns_uuid, lbl)

    products_skipped = sum(1 for v in label_to_uuid.values() if v is None)

    # Dedup by UUID: only unique resolvable products fire enrichment.
    seen_uuids: set[str] = set()
    for lbl, uuid_str in label_to_uuid.items():
        if uuid_str is None or uuid_str in seen_uuids:
            continue
        seen_uuids.add(uuid_str)
        _fire_product_enrichment(
            pg_pool=engine.pg_pool,
            namespace_id=namespace_id_str,
            product_id=uuid_str,
            design_id=design_id,
            missing_fields=missing_fields,
        )

    # -----------------------------------------------------------------------
    # 3. For each resolvable product, attempt price lookup and TCO (scoped).
    #    qty defaults to 1 — DESIGN_LINE nodes carry no qty in the graph.
    # -----------------------------------------------------------------------
    tco_computed = 0
    tco_skipped = 0

    processed_uuids: set[str] = set()
    for row in graph_rows:
        product_uuid = label_to_uuid.get(row["product_label"])
        if product_uuid is None:
            tco_skipped += 1
            continue
        if product_uuid in processed_uuids:
            # Already did TCO for this product UUID in a previous line.
            continue
        processed_uuids.add(product_uuid)

        unit_price = await _resolve_unit_price(
            engine.pg_pool, ns_uuid, product_uuid, namespace_id_str
        )
        if unit_price is None:
            log.debug(
                "[enrichment] product=%s has no price data — skipping TCO",
                product_uuid[:8],
            )
            tco_skipped += 1
            continue

        tco = _compute_tco(weights, tolerances, unit_price, 1, product_uuid)
        if tco is not None:
            tco_computed += 1
        else:
            tco_skipped += 1

    log.info(
        "[enrichment] design=%s enrichment_queued=%d tco_computed=%d "
        "tco_skipped=%d products_skipped=%d",
        design_id,
        len(seen_uuids),
        tco_computed,
        tco_skipped,
        products_skipped,
    )

    return {
        "design_id": design_id,
        "lines_found": len(graph_rows),
        "products_enqueued": len(seen_uuids),
        "products_skipped": products_skipped,
        "tco_computed": tco_computed,
        "tco_skipped": tco_skipped,
        "enrichment": "queued",
    }
