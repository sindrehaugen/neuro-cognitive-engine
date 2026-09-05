"""
nce/vertical_modules/sales/dealroom.py
======================================
DealRoom operations for Sales Engine (Batch 089).
Implements do_open_dealroom which assembles the room from QUOTE/BOM nodes,
updating and recomputing prices through C6 shared pricing service.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from bson import ObjectId

from nce.db_utils import scoped_pg_session
from nce.pricing import dg_price, resolve_price

log = logging.getLogger("nce.vertical_modules.sales.dealroom")


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

    A price is never invented.  A line whose price cannot be established is
    returned with ``priced: False``, ``unit_price``/``total_price``/``base_cost``
    set to ``None`` and an ``unpriced_reason`` of either ``"no_price_on_record"``
    (nothing was ever recorded for it) or ``"price_resolution_failed"`` (a price
    existed but resolving it raised).  Because such a line carries money of an
    unknown amount, ``total_price_nok`` is ``None`` whenever any *toggled* line
    is unpriced; ``unpriced_line_count`` reports how many toggled lines that is.
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
        # 1. Fetch quote details from read model (or fallback to graph node)
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
            # Merged source_json and manual
            source_json = quote["source_json"] or {}
            if isinstance(source_json, str):
                source_json = json.loads(source_json)
            manual = quote["manual"] or {}
            if isinstance(manual, str):
                manual = json.loads(manual)
            merged_quote = {**(source_json or {}), **(manual or {})}
            quote_name = quote["name"] or merged_quote.get("name", quote_name)
            quote_desc = merged_quote.get("description", "")

        # 2. Fetch BOM lines from graph nodes. Literal prefix test via
        # starts_with() -- NOT SQL LIKE. `quote_id` is caller-supplied and
        # this surface renders the result to the customer (DealRoom); `_`
        # and `%` are ordinary LIKE metacharacters (`_` matches any single
        # character, `%` matches any sequence), so a quote id containing
        # either would, against a raw LIKE pattern, silently widen the match
        # to a DIFFERENT quote's BOM lines -- showing one customer another
        # quote's line items (confirmed live:
        # 'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%' is true). starts_with()
        # is a plain literal-prefix test with no pattern semantics at all, so
        # no quote_id can ever be crafted to widen the match. Mirrors
        # economy/cascade.py's _read_actual_cost_total (Batch 120).
        bom_label_prefix = f"BOM_LINE:{quote_id.upper()}:"
        rows = await conn.fetch(
            """
            SELECT label, payload_ref
            FROM kg_nodes
            WHERE entity_type = 'BOM_LINE'
              AND namespace_id = $1::uuid
              AND starts_with(label, $2)
            ORDER BY label
            """,
            str(ns_uuid),
            bom_label_prefix,
        )

        lines_list = []
        total_price_nok = 0.0
        unpriced_toggled_count = 0

        for r in rows:
            label = r["label"]
            payload_ref = r["payload_ref"]

            product: dict[str, Any] = {}
            customer: dict[str, Any] = {}
            quantity = 1
            dg_pct = 0.3
            is_optional = True
            toggled = True
            manufacturer = "Unknown"
            model = "Unknown"

            # Fetch MongoDB payload details if available
            if payload_ref and engine.mongo_client:
                try:
                    doc = await engine.mongo_client.memory_archive.episodes.find_one(
                        {"_id": ObjectId(payload_ref)}
                    )
                    if doc:
                        product = doc.get("product") or {}
                        customer = doc.get("customer") or {}
                        quantity = doc.get("quantity", quantity)
                        dg_pct = doc.get("dg_pct", dg_pct)
                        is_optional = doc.get("is_optional", is_optional)
                        toggled = doc.get("toggled", toggled)
                        manufacturer = doc.get("manufacturer", manufacturer)
                        model = doc.get("model", model)

                        # If product/customer sub-dicts don't exist, build from root keys
                        if not product:
                            product = {
                                "supplier_list_price": doc.get("supplier_list_price"),
                                "supplier_list_as_of": doc.get("supplier_list_as_of"),
                                "base_price": doc.get("base_price"),
                                "base_as_of": doc.get("base_as_of"),
                            }
                        if not customer:
                            customer = {
                                "bid_price": doc.get("bid_price"),
                                "bid_as_of": doc.get("bid_as_of"),
                            }
                except Exception as e:
                    log.warning("Failed to fetch mongo payload for BOM line %s: %s", label, e)

            # A price must be ON RECORD.  Never substitute a plausible number
            # for a missing one -- carry the absence instead.
            #
            # "On record" means what ``resolve_price`` means by it, and that is a
            # PAIR: every tier builder in ``nce/pricing/resolver.py``
            # (``_build_bid_tier``, ``_build_supplier_list_tier``, ``_build_base_tier``)
            # returns None unless BOTH the amount and its ``*_as_of`` are present --
            # an undated price cannot be judged stale, so it is not a usable tier.
            #
            # This check used to test the amounts alone. A record carrying
            # ``base_price`` and no ``base_as_of`` therefore passed the guard, went
            # into ``resolve_price``, and came back "no price tier available" --
            # reported as ``price_resolution_failed``, i.e. as a SERVER fault, when
            # the truth is that the record is incomplete. Two answers to one question
            # in one codebase; the guard now asks the resolver's question instead of
            # its own.
            has_price_on_record = bool(
                (product.get("base_price") is not None and product.get("base_as_of") is not None)
                or (
                    product.get("supplier_list_price") is not None
                    and product.get("supplier_list_as_of") is not None
                )
                or (customer.get("bid_price") is not None and customer.get("bid_as_of") is not None)
            )

            # Determine toggle override from params
            line_ref = label.split(":")[-1]
            toggled_val = None
            if label in toggled_options:
                toggled_val = toggled_options[label]
            elif line_ref in toggled_options:
                toggled_val = toggled_options[line_ref]

            if toggled_val is not None:
                toggled = bool(toggled_val)
                # Persist updated toggle state back to Mongo
                if payload_ref and engine.mongo_client:
                    try:
                        await engine.mongo_client.memory_archive.episodes.update_one(
                            {"_id": ObjectId(payload_ref)}, {"$set": {"toggled": toggled}}
                        )
                    except Exception as e:
                        log.warning("Failed to update toggled state in mongo for %s: %s", label, e)

            # Price line through C6 Pricing Service.  Two different operator
            # problems are kept distinguishable: nothing was ever recorded, vs
            # a recorded price whose resolution failed.
            cost: float | None = None
            unpriced_reason: str | None = None
            if not has_price_on_record:
                unpriced_reason = "no_price_on_record"
                log.warning("No price on record for line %s; returning it unpriced", label)
            else:
                try:
                    price_result = await resolve_price(
                        conn,
                        namespace_id=str(ns_uuid),
                        product=product,
                        customer=customer,
                    )
                    cost = float(price_result["cost"])
                except Exception as e:
                    log.warning("Price resolution failed for line %s: %s", label, e)
                    unpriced_reason = "price_resolution_failed"

            unit_price = dg_price(cost, dg_pct) if cost is not None else None
            total_price = unit_price * quantity if unit_price is not None else None

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
