"""
nce/vertical_modules/procurement/mcp_handlers.py
=================================================
MCP tool handlers for the Procurement vertical module (W4: dual-surface).

Public entry-points:
  ``handle_procurement_calculate_tco``  — TCO breakdown for one (supplier, bom_line) pair.
  ``handle_procurement_rank_suppliers``  — Rank supplier candidates for a BOM line.
  ``handle_procurement_evaluate_match``  — Three-way match evaluation (PO × GR × invoice).

W12 Advisor (frontier) entry-points:
  ``handle_procurement_forecast_rebate``   — Forecast year-end rebate band.
  ``handle_procurement_recommend_move_spend`` — Recommend highest-ROI supplier.
  ``handle_procurement_whatif_spend``      — Simulate a hypothetical spend shift.

All are read-only Advisor tools (cacheable=True, admin_only=False, mutation=False).
No new logic — thin wrappers over W1–W3 and W12 pure cores.

Registered in ``nce/tool_registry.py`` via ``_h(procurement_mcp_handlers, "handle_*")``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nce.mcp_args import require_namespace_id
from nce.mcp_errors import mcp_handler
from nce.vertical_modules.procurement import frontier
from nce.vertical_modules.procurement.ranking import do_rank_suppliers
from nce.vertical_modules.procurement.tco import (
    do_calculate_tco,
    load_procurement_config,
)
from nce.vertical_modules.procurement.three_way_match import do_evaluate_three_way_match

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.procurement.mcp_handlers")


@mcp_handler
async def handle_procurement_calculate_tco(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: procurement_calculate_tco — TCO breakdown for one (supplier, bom_line) pair.

    Required arguments:
        namespace_id (str, UUID)
        supplier     (dict) — must contain ``unit_price`` (float).
        bom_line     (dict) — must contain ``quantity`` (int).
    """
    require_namespace_id(arguments)
    weights, tolerances = load_procurement_config()
    supplier: dict[str, Any] = dict(arguments.get("supplier") or {})
    bom_line: dict[str, Any] = dict(arguments.get("bom_line") or {})
    result = do_calculate_tco(weights, tolerances, supplier, bom_line)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_procurement_rank_suppliers(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: procurement_rank_suppliers — rank supplier candidates for one BOM line.

    Required arguments:
        namespace_id (str, UUID)
        bom_line     (dict) — must contain ``quantity`` (int).
        candidates   (list[dict]) — each must contain ``unit_price`` (float).
    """
    require_namespace_id(arguments)
    namespace_id = arguments["namespace_id"]
    weights, _tolerances = load_procurement_config()
    bom_line: dict[str, Any] = dict(arguments.get("bom_line") or {})
    candidates: list[dict[str, Any]] = list(arguments.get("candidates") or [])

    a2a_client = arguments.get("a2a_client")

    enriched_candidates: list[dict[str, Any]] = []
    for c in candidates:
        candidate = dict(c)
        supplier_id = candidate.get("supplier_id") or candidate.get("vendor_id")
        if supplier_id:
            supplier_id_str = str(supplier_id).strip()
            # 1. Fetch kickback tier status
            tier_status = None
            if a2a_client:
                try:
                    res = await a2a_client.call_tool(
                        "vendors_get_tier_status",
                        {"namespace_id": str(namespace_id), "vendor_id": supplier_id_str},
                    )
                    if isinstance(res, str):
                        res = json.loads(res)
                    tier_status = res
                except Exception as exc:
                    log.warning(
                        "[procurement-feed] a2a call to vendors_get_tier_status failed for %s: %s",
                        supplier_id_str,
                        exc,
                    )

            # Direct fallback if A2A failed or was absent
            if not tier_status:
                try:
                    from nce.vertical_modules.vendors.tiers import do_get_tier_status

                    tier_status = await do_get_tier_status(
                        engine,
                        {"namespace_id": namespace_id, "vendor_id": supplier_id_str},
                    )
                except Exception as exc:
                    log.warning(
                        "[procurement-feed] fallback do_get_tier_status failed for %s: %s",
                        supplier_id_str,
                        exc,
                    )

            if tier_status and isinstance(tier_status, dict):
                current_tier = tier_status.get("current_tier")
                if current_tier:
                    current_tier_str = str(current_tier).strip().lower()
                    if current_tier_str == "platinum":
                        candidate["supplier_tier"] = 1
                    elif current_tier_str == "gold":
                        candidate["supplier_tier"] = 2
                    elif current_tier_str == "silver":
                        candidate["supplier_tier"] = 3
                    else:
                        candidate["supplier_tier"] = 4
                else:
                    candidate["supplier_tier"] = 4
            else:
                candidate["supplier_tier"] = 4

            # 2. Fetch scorecard details
            vendor_info = None
            if a2a_client:
                try:
                    res = await a2a_client.call_tool(
                        "vendors_get_vendor",
                        {"namespace_id": str(namespace_id), "vendor_id": supplier_id_str},
                    )
                    if isinstance(res, str):
                        res = json.loads(res)
                    vendor_info = res
                except Exception as exc:
                    log.warning(
                        "[procurement-feed] a2a call to vendors_get_vendor failed for %s: %s",
                        supplier_id_str,
                        exc,
                    )

            # Direct fallback if A2A failed or was absent
            if not vendor_info:
                try:
                    from nce.vertical_modules.vendors.registry import do_get_vendor

                    vendor_info = await do_get_vendor(
                        engine,
                        {"namespace_id": namespace_id, "vendor_id": supplier_id_str},
                    )
                except Exception as exc:
                    log.warning(
                        "[procurement-feed] fallback do_get_vendor failed for %s: %s",
                        supplier_id_str,
                        exc,
                    )

            if vendor_info and isinstance(vendor_info, dict):
                scorecard = vendor_info.get("scorecard")
                if scorecard and isinstance(scorecard, dict):
                    reliability = scorecard.get("reliability")
                    if reliability is not None:
                        # scorecard has reliability in range 0-100; normalize to 0-1 for delivery_reliability
                        candidate["delivery_reliability"] = float(reliability) / 100.0
                    else:
                        # insufficient data or missing reliability; use neutral default if not already set
                        if "delivery_reliability" not in candidate:
                            candidate["delivery_reliability"] = 0.7
                else:
                    # no scorecard; use neutral default if not already set
                    if "delivery_reliability" not in candidate:
                        candidate["delivery_reliability"] = 0.7
            else:
                # no vendor details; use neutral default if not already set
                if "delivery_reliability" not in candidate:
                    candidate["delivery_reliability"] = 0.7

        enriched_candidates.append(candidate)

    result = do_rank_suppliers(weights, bom_line, enriched_candidates)
    return json.dumps(result, default=str)


@mcp_handler
async def handle_procurement_evaluate_match(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: procurement_evaluate_match — three-way match evaluation (PO × GR × invoice).

    Required arguments:
        namespace_id  (str, UUID)
        po            (dict) — purchase order with ``article_id``, ``quantity``, ``unit_price``.
        goods_receipt (dict) — goods receipt with ``quantity``.
        invoice       (dict) — invoice with ``article_id``, ``quantity``, ``unit_price``.
    """
    require_namespace_id(arguments)
    _weights, tolerances = load_procurement_config()
    po: dict[str, Any] = dict(arguments.get("po") or {})
    goods_receipt: dict[str, Any] = dict(arguments.get("goods_receipt") or {})
    invoice: dict[str, Any] = dict(arguments.get("invoice") or {})
    result = do_evaluate_three_way_match(tolerances, po, goods_receipt, invoice)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# W12: Frontier Advisor handlers — read-only, cacheable, no PO write
# ---------------------------------------------------------------------------


@mcp_handler
async def handle_procurement_forecast_rebate(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: procurement_forecast_rebate — forecast year-end rebate band.

    Required arguments:
        namespace_id (str, UUID)
    Optional arguments:
        supplier_id  (str) — filter BOM rows + tiers to one supplier.
    """
    require_namespace_id(arguments)
    try:
        result = await frontier.do_forecast_rebate(engine, arguments)
        return json.dumps(result, default=str)
    except Exception as exc:
        log.exception("[procurement-frontier] handle_procurement_forecast_rebate error")
        return json.dumps({"error": str(exc)}, default=str)


@mcp_handler
async def handle_procurement_recommend_move_spend(
    engine: NCEEngine, arguments: dict[str, Any]
) -> str:
    """MCP tool: procurement_recommend_move_spend — recommend highest-ROI supplier.

    Required arguments:
        namespace_id (str, UUID)
    """
    require_namespace_id(arguments)
    try:
        result = await frontier.do_recommend_move_spend(engine, arguments)
        return json.dumps(result, default=str)
    except Exception as exc:
        log.exception("[procurement-frontier] handle_procurement_recommend_move_spend error")
        return json.dumps({"error": str(exc)}, default=str)


@mcp_handler
async def handle_procurement_whatif_spend(engine: NCEEngine, arguments: dict[str, Any]) -> str:
    """MCP tool: procurement_whatif_spend — simulate a hypothetical spend shift.

    Required arguments:
        namespace_id   (str, UUID)
        from_supplier  (str) — supplier_id to shift spend away from.
        to_supplier    (str) — supplier_id to shift spend toward.
        shift_fraction (float) — fraction of current spend to shift (0–1).
    """
    require_namespace_id(arguments)
    try:
        result = await frontier.do_whatif_spend(engine, arguments)
        return json.dumps(result, default=str)
    except Exception as exc:
        log.exception("[procurement-frontier] handle_procurement_whatif_spend error")
        return json.dumps({"error": str(exc)}, default=str)
