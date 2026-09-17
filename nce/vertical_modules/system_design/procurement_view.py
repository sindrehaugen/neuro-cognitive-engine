"""
nce/vertical_modules/system_design/procurement_view.py
======================================================
Domain-core procurement view for the System Design vertical module (Wave SD-6).

Entry-point: ``do_get_procurement_view(engine, params) -> dict``

Goal
----
Given a system design (identified by ``design_id``), inspect its components
(devices, design lines, capabilities, and frozen BOM lines), resolve and rank
supplier candidates per item via Procurement's 5-step ranking engine
(``nce.vertical_modules.procurement.ranking.do_rank_suppliers``), and group the
design's line items by winning ranked supplier.

Each grouped supplier block includes a pre-formatted payload ready for
Procurement PR-1 (``do_generate_po`` / ``do_submit_po``).

Invariants
----------
- Read-Only / Propose-Only (§9.3): Zero mutations to kg_nodes, kg_edges, or
  outbox events.
- ADR-0017 Confidentiality: Cost, margin, and raw BID keys are forbidden from
  all public and advisor dictionary representations.
- Contract-A Ownership (§9.1): System Design never mutates QUOTE or PO nodes.
- Multi-Tenant Isolation (Tenant isolation rule 7): Every database query carries an explicit
  ``namespace_id = $n::uuid`` parameter predicate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.tco import load_procurement_config
from nce.vertical_modules.system_design.geometry import fetch_design_version
from nce.vertical_modules.system_design.graph import _design_label
from nce.vertical_modules.system_design.sow import _derive_version_number

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.system_design.procurement_view")

# ADR-0017 forbidden keys — must never appear in any response dictionary.
_ADR0017_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"cost", "cost_price", "bid_price", "bid_id", "margin", "unit_cost"}
)


def _sanitize_adr0017(val: Any) -> Any:
    """Recursively scrub any ADR-0017 forbidden keys from returned data."""
    if isinstance(val, dict):
        cleaned: dict[str, Any] = {}
        for k, v in val.items():
            if k in _ADR0017_FORBIDDEN_KEYS:
                continue
            cleaned[k] = _sanitize_adr0017(v)
        return cleaned
    if isinstance(val, list):
        return [_sanitize_adr0017(item) for item in val]
    return val


async def _fetch_design_meta_and_quote(
    conn: Any,
    ns_uuid: UUID,
    design_lbl: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read DESIGN node metadata and check for a 'becomes' edge to QUOTE."""
    meta_row = await conn.fetchrow(
        """
        SELECT label, entity_type, updated_at
        FROM kg_nodes
        WHERE label        = $1
          AND entity_type  = 'DESIGN'
          AND namespace_id = $2::uuid
        """,
        design_lbl,
        str(ns_uuid),
    )
    if not meta_row:
        return None, None

    quote_row = await conn.fetchrow(
        """
        SELECT object_label
        FROM kg_edges
        WHERE subject_label = $1
          AND predicate     = 'becomes'
          AND namespace_id  = $2::uuid
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        design_lbl,
        str(ns_uuid),
    )
    quote_lbl = quote_row["object_label"] if quote_row else None
    return dict(meta_row), quote_lbl


async def _fetch_design_line_items(
    conn: Any,
    ns_uuid: UUID,
    design_lbl: str,
    quote_id: str | None,
) -> list[dict[str, Any]]:
    """Gather all line items for a design across BOM lines, DESIGN_LINEs, and DEVICEs."""
    items: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    # 1. If quote exists, read bom_line_content rows
    if quote_id:
        bom_rows = await conn.fetch(
            """
            SELECT bom_line_label, quote_id, line_ref, qty, unit_price, line_total, origin_ref, priced
            FROM bom_line_content
            WHERE namespace_id = $1::uuid
              AND quote_id      = $2
            ORDER BY line_ref ASC
            """,
            str(ns_uuid),
            quote_id,
        )
        for r in bom_rows:
            lref = r["line_ref"]
            seen_refs.add(lref)
            origin = r["origin_ref"] or ""
            items.append(
                {
                    "item_ref": lref,
                    "item_type": "BOM_LINE",
                    "origin_ref": origin,
                    "quantity": int(r["qty"]),
                    "unit_price": float(r["unit_price"] or 0.0),
                    "manufacturer": "",
                    "model_number": lref,
                    "article_number": lref,
                }
            )

    # 2. Read DESIGN_LINE nodes contained in this design
    dl_rows = await conn.fetch(
        """
        SELECT n.label AS design_line_label,
               ref.object_label AS product_label
        FROM kg_nodes n
        JOIN kg_edges c
             ON c.object_label = n.label
            AND c.predicate    = 'contains'
            AND c.namespace_id = $2::uuid
        LEFT JOIN kg_edges ref
             ON ref.subject_label = n.label
            AND ref.predicate     = 'references'
            AND ref.namespace_id  = $2::uuid
        WHERE c.subject_label = $1
          AND n.entity_type   = 'DESIGN_LINE'
          AND n.namespace_id  = $2::uuid
        ORDER BY n.label ASC
        """,
        design_lbl,
        str(ns_uuid),
    )
    for r in dl_rows:
        lbl = r["design_line_label"]
        parts = lbl.split(":")
        lref = parts[2] if len(parts) >= 3 else lbl
        prod_lbl = r["product_label"] or ""
        mfr = ""
        part_no = lref
        if prod_lbl.startswith("PRODUCT:"):
            prod_parts = prod_lbl.split(":", 2)
            if len(prod_parts) >= 3:
                mfr = prod_parts[1]
                part_no = prod_parts[2]

        # If already present from bom_line_content, enrich manufacturer/part_no
        matched = False
        for it in items:
            if it["item_ref"] == lref or it.get("origin_ref") == lbl:
                if mfr and not it["manufacturer"]:
                    it["manufacturer"] = mfr
                if part_no:
                    it["model_number"] = part_no
                    it["article_number"] = part_no
                matched = True
                break

        if not matched and lref not in seen_refs:
            seen_refs.add(lref)
            items.append(
                {
                    "item_ref": lref,
                    "item_type": "DESIGN_LINE",
                    "origin_ref": lbl,
                    "quantity": 1,
                    "unit_price": 0.0,
                    "manufacturer": mfr,
                    "model_number": part_no,
                    "article_number": part_no,
                }
            )

    # 3. Read DEVICE nodes contained in this design
    dev_rows = await conn.fetch(
        """
        SELECT n.label AS device_label,
               sddc.manufacturer,
               sddc.model_number,
               sddc.device_category
        FROM kg_nodes n
        JOIN kg_edges c
             ON c.object_label = n.label
            AND c.predicate    = 'contains'
            AND c.namespace_id = $2::uuid
        LEFT JOIN system_design_device_capabilities sddc
             ON sddc.node_label = n.label
            AND sddc.namespace_id = $2::uuid
        WHERE c.subject_label = $1
          AND n.entity_type   = 'DEVICE'
          AND n.namespace_id  = $2::uuid
        ORDER BY n.label ASC
        """,
        design_lbl,
        str(ns_uuid),
    )
    for r in dev_rows:
        dev_lbl = r["device_label"]
        if dev_lbl in seen_refs:
            continue
        seen_refs.add(dev_lbl)
        mfr = r["manufacturer"] or ""
        model = r["model_number"] or dev_lbl
        items.append(
            {
                "item_ref": dev_lbl,
                "item_type": "DEVICE",
                "origin_ref": dev_lbl,
                "quantity": 1,
                "unit_price": 0.0,
                "manufacturer": mfr,
                "model_number": model,
                "article_number": model,
            }
        )

    return items


async def _resolve_item_candidates(
    conn: Any,
    ns_uuid: UUID,
    item: dict[str, Any],
    caller_candidates: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Determine supplier candidates for one item."""
    item_ref = item["item_ref"]
    artnr = item.get("article_number") or item.get("model_number") or item_ref

    # 1. Caller explicit candidate override
    if caller_candidates:
        if isinstance(caller_candidates, dict):
            if item_ref in caller_candidates:
                return [dict(c) for c in caller_candidates[item_ref]]
            if artnr in caller_candidates:
                return [dict(c) for c in caller_candidates[artnr]]
        elif isinstance(caller_candidates, list):
            return [dict(c) for c in caller_candidates]

    candidates: list[dict[str, Any]] = []

    # 2. Check procurement_bid_prices cache
    bid_rows = await conn.fetch(
        """
        SELECT leverandor, pris
        FROM procurement_bid_prices
        WHERE namespace_id = $1::uuid
          AND (UPPER(artnr) = UPPER($2) OR UPPER(artnr) = UPPER($3))
          AND pris IS NOT NULL
        """,
        str(ns_uuid),
        artnr,
        item_ref,
    )
    for r in bid_rows:
        lev = r["leverandor"]
        pris = float(r["pris"])
        candidates.append(
            {
                "supplier_id": lev,
                "supplier_name": lev,
                "unit_price": pris,
                "own_stock": False,
                "delivery_reliability": 0.85,
                "supplier_tier": 2,
            }
        )

    # 3. Check product_prices catalog
    price_rows = await conn.fetch(
        """
        SELECT supplier, list_price, cost_price
        FROM product_prices
        WHERE namespace_id = $1::uuid
          AND (UPPER(mfr_part_no) = UPPER($2) OR UPPER(mfr_part_no) = UPPER($3))
        """,
        str(ns_uuid),
        artnr,
        item_ref,
    )
    for r in price_rows:
        sup = r["supplier"]
        # Use list_price or cost_price as unit_price, never exposing cost_price key
        uprice = float(r["list_price"] or r["cost_price"] or 0.0)
        # Avoid duplicate supplier entry
        if not any(c.get("supplier_id") == sup for c in candidates):
            candidates.append(
                {
                    "supplier_id": sup,
                    "supplier_name": sup,
                    "unit_price": uprice,
                    "own_stock": True,
                    "delivery_reliability": 0.90,
                    "supplier_tier": 1,
                }
            )

    # 4. Fallback default candidate if none found
    if not candidates:
        sup_id = item.get("manufacturer") or "DEFAULT_SUPPLIER"
        uprice = float(item.get("unit_price") or 0.0)
        candidates.append(
            {
                "supplier_id": sup_id,
                "supplier_name": sup_id,
                "unit_price": uprice,
                "own_stock": True,
                "delivery_reliability": 1.0,
                "supplier_tier": 1,
            }
        )

    return candidates


async def do_get_procurement_view(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Inspect a frozen system design and group items by top-ranked supplier for PR-1.

    Parameters
    ----------
    engine:
        NCEEngine instance with a live pg_pool.
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "design_id": str,             # required
            "weights": dict | None,       # optional procurement weights
            "candidates": list | dict,    # optional candidate override
            "require_frozen": bool,       # optional (default False)
            "design_version": int,        # optional frozen version override
            "required_by_day": int,       # optional delivery deadline
        }``

    Returns
    -------
    dict
        Structured procurement view with items grouped by ranked supplier,
        estimated spend totals, and PR-1 payloads.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_get_procurement_view: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw = params.get("design_id", "")
    if not design_id_raw or not str(design_id_raw).strip():
        raise ValueError("do_get_procurement_view: 'design_id' is required in params")
    design_id = str(design_id_raw).strip()
    design_lbl = _design_label(design_id)

    caller_weights = params.get("weights")
    if caller_weights:
        weights = dict(caller_weights)
    else:
        loaded_weights, _ = load_procurement_config()
        weights = loaded_weights

    caller_candidates = params.get("candidates")
    require_frozen = bool(params.get("require_frozen", False))
    caller_version = params.get("design_version")
    required_by_day = params.get("required_by_day")

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        meta, quote_lbl = await _fetch_design_meta_and_quote(conn, ns_uuid, design_lbl)
        if not meta:
            raise ValueError(
                f"do_get_procurement_view: DESIGN node not found for design_id={design_id!r} "
                f"in namespace={ns_uuid}"
            )

        quote_id = (
            quote_lbl[len("QUOTE:") :]
            if (quote_lbl and quote_lbl.startswith("QUOTE:"))
            else quote_lbl
        )
        has_becomes_edge = quote_lbl is not None

        geom_version = await fetch_design_version(conn, ns_uuid, design_lbl)
        derived_version = _derive_version_number(design_lbl, meta)
        version = (
            int(caller_version) if caller_version is not None else (geom_version or derived_version)
        )

        is_frozen = bool(caller_version is not None or has_becomes_edge)
        if require_frozen and not is_frozen:
            raise ValueError(
                f"do_get_procurement_view: design {design_id!r} is not frozen "
                f"(no becomes edge to QUOTE or caller version supplied)"
            )

        # Gather design items
        raw_items = await _fetch_design_line_items(conn, ns_uuid, design_lbl, quote_id)

        # Resolve candidates and rank suppliers for each item
        ranked_items: list[dict[str, Any]] = []
        for it in raw_items:
            candidates = await _resolve_item_candidates(conn, ns_uuid, it, caller_candidates)
            bom_line_input: dict[str, Any] = {
                "quantity": it["quantity"],
                "unit_price": it["unit_price"],
            }
            if required_by_day is not None:
                bom_line_input["required_by_day"] = int(required_by_day)

            ranking_res = do_rank_suppliers(weights, bom_line_input, candidates)
            winner = ranking_res["ranked"][0]
            winner_sup = str(winner.get("supplier_id") or "DEFAULT")
            raw_unit_price = winner.get("unit_price")
            if raw_unit_price is None:
                raise ValueError(
                    f"do_get_procurement_view: winner {winner_sup} missing unit_price for {it['item_ref']}"
                )
            winner_unit_price = float(raw_unit_price)
            line_total = round(winner_unit_price * it["quantity"], 2)

            # Scrub score_breakdown of forbidden ADR-0017 tokens
            breakdown = dict(winner.get("score_breakdown") or {})
            if "bid_price" in breakdown:
                breakdown["price_score"] = breakdown.pop("bid_price")

            ranked_items.append(
                {
                    "item_ref": it["item_ref"],
                    "item_type": it["item_type"],
                    "description": f"{it.get('manufacturer', '')} {it.get('model_number', '')}".strip()
                    or it["item_ref"],
                    "manufacturer": it.get("manufacturer", ""),
                    "model_number": it.get("model_number", ""),
                    "quantity": it["quantity"],
                    "estimated_unit_price": winner_unit_price,
                    "estimated_line_total": line_total,
                    "winning_supplier_id": winner_sup,
                    "winning_supplier_name": winner.get("supplier_name", winner_sup),
                    "ranking_summary": {
                        "composite_score": round(float(winner.get("composite_score", 0.0)), 4),
                        "score_breakdown": breakdown,
                        "rebate_override": ranking_res.get("rebate_override", False),
                        "rebate_rationale": ranking_res.get("rebate_rationale", ""),
                    },
                    "winner_candidate": {
                        "supplier_id": winner_sup,
                        "unit_price": winner_unit_price,
                        "delivery_reliability": float(winner.get("delivery_reliability", 0.8)),
                        "supplier_tier": int(winner.get("supplier_tier", 1)),
                        "own_stock": bool(winner.get("own_stock", True)),
                    },
                }
            )

    # Group by winning supplier
    by_supplier: dict[str, dict[str, Any]] = {}
    for it in ranked_items:
        sup_id = it["winning_supplier_id"]
        if sup_id not in by_supplier:
            by_supplier[sup_id] = {
                "supplier_id": sup_id,
                "supplier_name": it["winning_supplier_name"],
                "line_items": [],
                "item_count": 0,
                "total_estimated_spend": 0.0,
                "rebate_override": False,
            }
        grp = by_supplier[sup_id]
        grp["line_items"].append(
            {
                "item_ref": it["item_ref"],
                "item_type": it["item_type"],
                "description": it["description"],
                "manufacturer": it["manufacturer"],
                "model_number": it["model_number"],
                "quantity": it["quantity"],
                "estimated_unit_price": it["estimated_unit_price"],
                "estimated_line_total": it["estimated_line_total"],
                "ranking": it["ranking_summary"],
            }
        )
        grp["item_count"] += it["quantity"]
        grp["total_estimated_spend"] = round(
            grp["total_estimated_spend"] + it["estimated_line_total"], 2
        )
        if it["ranking_summary"]["rebate_override"]:
            grp["rebate_override"] = True

    supplier_list: list[dict[str, Any]] = []
    total_spend = 0.0
    total_qty = 0

    for sup_id, grp in sorted(by_supplier.items(), key=lambda kv: kv[0]):
        suggested_po = f"PO-{design_id.upper()}-{sup_id.upper()}"
        grp["suggested_po_number"] = suggested_po

        # Assemble PR-1 ready payload
        grp["pr1_payload"] = {
            "namespace_id": str(ns_uuid),
            "po_number": suggested_po,
            "source_id": f"system_design:{design_id}",
            "bom_line": {
                "quantity": grp["item_count"],
                "unit_price": round(grp["total_estimated_spend"] / max(1, grp["item_count"]), 2),
            },
            "line_items": [
                {
                    "line_ref": li["item_ref"],
                    "artnr": li["model_number"] or li["item_ref"],
                    "quantity": li["quantity"],
                    "unit_price": li["estimated_unit_price"],
                    "line_total": li["estimated_line_total"],
                }
                for li in grp["line_items"]
            ],
            "artnrs": [li["model_number"] or li["item_ref"] for li in grp["line_items"]],
            "candidates": [
                {
                    "supplier_id": sup_id,
                    "unit_price": round(
                        grp["total_estimated_spend"] / max(1, grp["item_count"]), 2
                    ),
                    "delivery_reliability": 0.9,
                    "supplier_tier": 1,
                    "own_stock": True,
                }
            ],
        }
        total_spend = round(total_spend + grp["total_estimated_spend"], 2)
        total_qty += grp["item_count"]
        supplier_list.append(grp)

    result = {
        "namespace_id": str(ns_uuid),
        "design_id": design_id,
        "design_label": design_lbl,
        "is_frozen": is_frozen,
        "design_version": version,
        "quote_id": quote_id,
        "quote_label": quote_lbl,
        "supplier_count": len(supplier_list),
        "total_line_items": len(ranked_items),
        "total_quantity": total_qty,
        "total_estimated_spend": total_spend,
        "suppliers": supplier_list,
        "by_supplier": {s["supplier_id"]: s for s in supplier_list},
    }

    # Strict ADR-0017 sanitization
    sanitized = _sanitize_adr0017(result)
    log.info(
        "do_get_procurement_view: ns=%s design=%s frozen=%s suppliers=%d total_spend=%.2f",
        ns_uuid,
        design_id,
        is_frozen,
        len(supplier_list),
        total_spend,
    )
    return sanitized
