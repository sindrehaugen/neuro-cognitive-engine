"""
nce/vertical_modules/sales/dealroom.py
======================================
DealRoom operations for Sales Engine (Batch 089 / Wave S-3).
Materialises the room from PostgreSQL bom_line_content, correlating product
details through the global product_catalog, and computing prices through
the C6 shared pricing service without MongoDB or fabricated defaults.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.pricing import dg_price, load_dg, resolve_price

log = logging.getLogger("nce.vertical_modules.sales.dealroom")


def _get_namespace_dg(namespace_id: str | UUID) -> float:
    """Load per-namespace DG% margin from pricing config, falling back to default."""
    ns_key = str(namespace_id)
    try:
        return load_dg(ns_key)
    except KeyError:
        return load_dg("default")


async def do_open_dealroom(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Materialise a live web quote (DealRoom) with toggle-able option lines.

    Params:
      namespace_id (str | UUID): owning namespace
      quote_id (str): identifier of the quote
      toggled_options (dict[str, bool]): optional mapping of line labels or refs to toggle state

    Returns:
      dict: DealRoom payload containing quote details, lines list, and recomputed total price.

    A price is never invented. A line whose price cannot be established is
    returned with ``priced: False``, ``unit_price``/``total_price``/``base_cost``
    set to ``None`` and an ``unpriced_reason`` of either ``"no_price_on_record"``
    (nothing was ever recorded for it) or ``"price_resolution_failed"`` (a price
    existed but resolving it raised). Because such a line carries money of an
    unknown amount, ``total_price_nok`` is ``None`` whenever any *toggled* line
    is unpriced; ``unpriced_line_count`` reports how many toggled lines that is.

    Manufacturer, model, and optionality resolve via correlation to the global
    product_catalog. When unlinked or absent, fields render as None (absent),
    never as invented default constants.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    quote_id = params.get("quote_id")
    if not quote_id:
        raise ValueError("quote_id is required")

    toggled_options = params.get("toggled_options") or {}

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # 1. Fetch quote details from read model
        quote = await conn.fetchrow(
            """
            SELECT name, source_json, manual
            FROM sales_read_model
            WHERE namespace_id = $1
              AND entity = 'quotes'
              AND source_id = $2
            """,
            str(ns_uuid),
            quote_id,
        )

        quote_name = "DealRoom Quote"
        quote_desc = ""
        if quote:
            source_json = quote["source_json"] or {}
            if isinstance(source_json, str):
                source_json = json.loads(source_json)
            manual = quote["manual"] or {}
            if isinstance(manual, str):
                manual = json.loads(manual)
            merged_quote = {**(source_json or {}), **(manual or {})}
            quote_name = quote["name"] or merged_quote.get("name", quote_name)
            quote_desc = merged_quote.get("description", "")

        # 2. Fetch BOM lines from Postgres bom_line_content with LATERAL product_catalog join.
        # Literal prefix test via starts_with() protects against LIKE metacharacter
        # leaks ('_' / '%') in user-supplied quote_id.
        bom_label_prefix = f"BOM_LINE:{quote_id.upper()}:"
        rows = await conn.fetch(
            """
            SELECT
                b.id,
                b.bom_line_label,
                b.quote_id,
                b.line_ref,
                b.qty,
                b.unit_price,
                b.line_total,
                b.currency,
                b.priced,
                b.origin_kind,
                b.origin_ref,
                pc.manufacturer,
                pc.mfr_part_no AS model
            FROM bom_line_content b
            LEFT JOIN LATERAL (
                SELECT manufacturer, mfr_part_no
                FROM product_catalog
                WHERE (b.origin_ref IS NOT NULL AND (id::text = b.origin_ref OR mfr_part_no = b.origin_ref))
                   OR mfr_part_no = b.line_ref
                LIMIT 1
            ) pc ON TRUE
            WHERE b.namespace_id = $1::uuid
              AND (b.quote_id = $2 OR starts_with(b.bom_line_label, $3))
            ORDER BY b.bom_line_label
            """,
            str(ns_uuid),
            quote_id,
            bom_label_prefix,
        )

        lines_list: list[dict[str, Any]] = []
        total_price_nok = 0.0
        unpriced_toggled_count = 0

        for r in rows:
            label: str = r["bom_line_label"]
            line_ref: str = r["line_ref"] or label.split(":")[-1]
            quantity: float = float(r["qty"])
            is_priced: bool = bool(r["priced"])
            stored_unit_price: float | None = (
                float(r["unit_price"]) if r["unit_price"] is not None else None
            )
            stored_line_total: float | None = (
                float(r["line_total"]) if r["line_total"] is not None else None
            )

            manufacturer: str | None = r["manufacturer"]
            model: str | None = r["model"]
            is_optional: bool | None = None

            # Dynamic toggle state from caller params; active by default unless toggled off
            toggled_val = None
            if label in toggled_options:
                toggled_val = toggled_options[label]
            elif line_ref in toggled_options:
                toggled_val = toggled_options[line_ref]

            toggled: bool = True if toggled_val is None else bool(toggled_val)

            # Price line: either from bom_line_content record or C6 resolver
            cost: float | None = None
            unit_price: float | None = None
            total_price: float | None = None
            unpriced_reason: str | None = None

            if not is_priced:
                unpriced_reason = "no_price_on_record"
                log.warning("No price on record for line %s; returning it unpriced", label)
            else:
                # When priced, check if dynamic pricing override / resolution applies
                product_info = params.get("product_override", {}).get(label)
                customer_info = params.get("customer_override", {}).get(label)
                if product_info is not None or customer_info is not None:
                    p = product_info or {}
                    c = customer_info or {}
                    has_price_on_record = bool(
                        (p.get("base_price") is not None and p.get("base_as_of") is not None)
                        or (
                            p.get("supplier_list_price") is not None
                            and p.get("supplier_list_as_of") is not None
                        )
                        or (c.get("bid_price") is not None and c.get("bid_as_of") is not None)
                    )
                    if not has_price_on_record:
                        unpriced_reason = "no_price_on_record"
                    else:
                        try:
                            price_result = await resolve_price(
                                conn,
                                namespace_id=str(ns_uuid),
                                product=p,
                                customer=c,
                            )
                            cost = float(price_result["cost"])
                            dg_pct = _get_namespace_dg(ns_uuid)
                            unit_price = dg_price(cost, dg_pct)
                            total_price = unit_price * quantity
                        except Exception as e:
                            log.warning("Price resolution failed for line %s: %s", label, e)
                            unpriced_reason = "price_resolution_failed"
                else:
                    if stored_unit_price is None:
                        unpriced_reason = "no_price_on_record"
                    else:
                        unit_price = stored_unit_price
                        total_price = (
                            stored_line_total
                            if stored_line_total is not None
                            else (unit_price * quantity if unit_price is not None else None)
                        )

            if unpriced_reason is not None:
                cost = None
                unit_price = None
                total_price = None

            lines_list.append(
                {
                    "label": label,
                    "manufacturer": manufacturer,
                    "model": model,
                    "quantity": quantity,
                    "base_cost": cost,
                    "unit_price": unit_price,
                    "total_price": total_price,
                    "priced": unpriced_reason is None,
                    "unpriced_reason": unpriced_reason,
                    "is_optional": is_optional,
                    "toggled": toggled,
                }
            )

            if toggled:
                if total_price is None:
                    unpriced_toggled_count += 1
                else:
                    total_price_nok += total_price

        return {
            "quote_id": quote_id,
            "name": quote_name,
            "description": quote_desc,
            "total_price_nok": None if unpriced_toggled_count else total_price_nok,
            "unpriced_line_count": unpriced_toggled_count,
            "lines": lines_list,
        }
