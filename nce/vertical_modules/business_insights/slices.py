"""
nce/vertical_modules/business_insights/slices.py
=================================================
Composed Slice Registry and Resolution Layer for Module 16 (Business Insights Engine).

Wave BI-2 (Charter §13 & Spec §3.4 / §6):
Every slice the morning brief, board pack, and risk radar compose resolves to a
registered read -- a cacheable tool or a catalogued event -- never a per-engine
query negotiated at call time.

Rules:
1. Canonical Registry: Every composed slice is explicitly registered in
   ``COMPOSED_SLICES`` with its upstream engine, tool name, and cacheability.
2. No Fabricated Defaults: A slice with no producer or an unavailable producer
   is absent and labelled ('not available yet' / degraded=True), NEVER defaulted to 0.
3. Observable Grace Degradation: Upstream engine absences or read failures trigger
   ``nce.degradation.record_degradation()``.
4. Deterministic Simulation: Caller-supplied ``override`` dictionaries (used in tests
   or scenario modelling) bypass live network/DB reads while preserving the schema.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nce.vertical_modules.business_insights._guard import is_engine_landed
from nce.vertical_modules.business_insights.kpi import STATUS_NOT_AVAILABLE_YET

log = logging.getLogger("nce.vertical_modules.business_insights.slices")


@dataclass(frozen=True)
class ComposedSliceSpec:
    """Specification for an upstream vertical slice consumed by Business Insights."""

    slice_id: str
    engine: str
    tool_name: str
    description: str
    cacheable: bool = True


# Canonical registry of all cross-engine slices composed by Business Insights.
# Wave BI-2: Every entry MUST map to a registered tool in nce.tool_registry.TOOL_REGISTRY.
COMPOSED_SLICES: dict[str, ComposedSliceSpec] = {
    "sales_pipeline_brief": ComposedSliceSpec(
        slice_id="sales_pipeline_brief",
        engine="sales",
        tool_name="sales_morning_brief_slice",
        description="Sales open pipeline, at-risk stalled deals, and won deals metrics (Wave S-4)",
        cacheable=True,
    ),
    "support_at_risk": ComposedSliceSpec(
        slice_id="support_at_risk",
        engine="support",
        tool_name="support_at_risk_aggregate",
        description="Support operational risk slice covering SLA clocks, churn risk, and proactive tickets (Wave SU-3)",
        cacheable=True,
    ),
    "resources_forecast": ComposedSliceSpec(
        slice_id="resources_forecast",
        engine="resources",
        tool_name="resources_forecast_demand",
        description="Resources capacity supply vs demand forecast, utilization, and headroom (Wave RS-4)",
        cacheable=True,
    ),
    "economy_financial_pulse": ComposedSliceSpec(
        slice_id="economy_financial_pulse",
        engine="economy",
        tool_name="economy_snapshot_mrr_arr_churn",
        description="Economy financial pulse snapshot covering MRR, ARR, churn, and gross margins (Wave E-1)",
        cacheable=True,
    ),
    "procurement_savings": ComposedSliceSpec(
        slice_id="procurement_savings",
        engine="procurement",
        tool_name="procurement_aggregate_savings",
        description="Procurement realized savings and supplier spend leakage candidates (Wave PR-3)",
        cacheable=True,
    ),
    "agreements_coverage": ComposedSliceSpec(
        slice_id="agreements_coverage",
        engine="agreements",
        tool_name="agreements_coverage_matrix",
        description="Agreements contract coverage, expiry timeline, review queue, and GL leakage flags (Wave AG-2, folding AG-5)",
        cacheable=True,
    ),
    "inventory_dead_stock": ComposedSliceSpec(
        slice_id="inventory_dead_stock",
        engine="inventory",
        tool_name="inventory_reconcile_dead_stock",
        description="Inventory non-moving stock valuation and dead stock reconciliation",
        cacheable=False,
    ),
}


def get_composed_slice_spec(slice_id: str) -> ComposedSliceSpec:
    """Retrieve the specification for a registered composed slice."""
    if slice_id not in COMPOSED_SLICES:
        raise KeyError(
            f"Unknown composed slice {slice_id!r}. Registered: {sorted(COMPOSED_SLICES)}"
        )
    return COMPOSED_SLICES[slice_id]


async def resolve_slice(
    engine: Any,
    namespace_id: str | UUID,
    slice_id: str,
    *,
    override: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve an upstream vertical slice via registered MCP tool read.

    Parameters
    ----------
    engine : Any
        NCEEngine instance providing DB pool and execution context.
    namespace_id : str | UUID
        Target tenant namespace ID.
    slice_id : str
        Canonical slice identifier registered in ``COMPOSED_SLICES``.
    override : dict[str, Any] | None
        Optional simulation/mock data override (preserves deterministic testing).
    arguments : dict[str, Any] | None
        Optional tool-specific arguments passed to the upstream handler.

    Returns
    -------
    dict[str, Any]
        Structured slice resolution response containing:
        - ok: bool
        - slice_id: str
        - engine: str
        - tool_name: str
        - degraded: bool
        - status: str ('operational' or 'not available yet')
        - display_value: str
        - data: dict | None
        - metrics: dict
        - provenance_nodes: list[str]
        - derived_from: list[dict]
    """
    spec = get_composed_slice_spec(slice_id)
    ns_str = str(namespace_id)

    # 1. Deterministic simulation override (used in testing or scenario what-if)
    if override is not None and isinstance(override, dict):
        is_degraded = override.get("degraded", False)
        status = override.get("status", STATUS_NOT_AVAILABLE_YET if is_degraded else "operational")
        prov_nodes = list(override.get("provenance_nodes") or [])
        metrics = {
            k: v
            for k, v in override.items()
            if k not in ("provenance_nodes", "derived_from", "degraded", "status")
        }
        return {
            "ok": not is_degraded,
            "slice_id": spec.slice_id,
            "engine": spec.engine,
            "tool_name": spec.tool_name,
            "degraded": is_degraded,
            "status": status,
            "display_value": override.get("display_value", status),
            "value": override.get("value"),
            "data": override,
            "metrics": metrics,
            "provenance_nodes": prov_nodes,
            "derived_from": [{"source_node": n, "edge_type": "derived_from"} for n in prov_nodes],
            "source": "override",
        }

    # 2. Check if upstream engine is landed
    if not is_engine_landed(spec.engine):
        _record_slice_degradation(
            namespace_id=namespace_id,
            slice_id=spec.slice_id,
            engine=spec.engine,
            code=f"slice_{spec.slice_id}_engine_unlanded",
            detail=f"Upstream engine {spec.engine!r} is not landed on main; slice grace-degraded.",
            hint=f"Deploy {spec.engine} engine to unlock {spec.slice_id} slice metrics.",
        )
        return _make_degraded_response(spec, reason="engine_unlanded")

    # 3. Resolve tool from live TOOL_REGISTRY
    try:
        from nce.tool_registry import TOOL_REGISTRY
    except ImportError:
        TOOL_REGISTRY = {}

    tool_spec = TOOL_REGISTRY.get(spec.tool_name)
    if tool_spec is None:
        _record_slice_degradation(
            namespace_id=namespace_id,
            slice_id=spec.slice_id,
            engine=spec.engine,
            code=f"slice_{spec.slice_id}_tool_unregistered",
            detail=f"Producer tool {spec.tool_name!r} is not registered in TOOL_REGISTRY.",
            hint=f"Register tool {spec.tool_name} to restore {spec.slice_id} slice resolution.",
        )
        return _make_degraded_response(spec, reason="tool_unregistered")

    # 4. Invoke the registered tool handler
    call_args: dict[str, Any] = {"namespace_id": ns_str}
    if arguments:
        call_args.update(arguments)

    try:
        raw_output = await tool_spec.handler(engine, call_args)
        if isinstance(raw_output, str):
            try:
                payload = json.loads(raw_output)
            except (ValueError, TypeError):
                payload = {"raw_text": raw_output}
        elif isinstance(raw_output, dict):
            payload = raw_output
        else:
            payload = {}
    except Exception as exc:
        log.info("Resolution of slice %s via %s failed: %s", spec.slice_id, spec.tool_name, exc)
        _record_slice_degradation(
            namespace_id=namespace_id,
            slice_id=spec.slice_id,
            engine=spec.engine,
            code=f"slice_{spec.slice_id}_read_failed",
            detail=f"Error invoking producer tool {spec.tool_name}: {exc}",
            hint=f"Verify database connectivity and configuration for {spec.engine} engine.",
        )
        return _make_degraded_response(spec, reason="read_failed", detail=str(exc))

    if isinstance(payload, dict) and payload.get("error"):
        _record_slice_degradation(
            namespace_id=namespace_id,
            slice_id=spec.slice_id,
            engine=spec.engine,
            code=f"slice_{spec.slice_id}_refused",
            detail=f"Tool {spec.tool_name} returned error: {payload.get('error')}",
            hint=f"Check {spec.engine} engine permissions and preconditions.",
        )
        return _make_degraded_response(spec, reason="tool_error", detail=str(payload.get("error")))

    # 5. Extract structured metrics and provenance nodes
    metrics, prov_nodes = _extract_slice_metrics_and_provenance(spec.slice_id, payload)

    return {
        "ok": True,
        "slice_id": spec.slice_id,
        "engine": spec.engine,
        "tool_name": spec.tool_name,
        "degraded": False,
        "status": "operational",
        "display_value": "operational",
        "value": None,
        "data": payload,
        "metrics": metrics,
        "provenance_nodes": prov_nodes,
        "derived_from": [{"source_node": n, "edge_type": "derived_from"} for n in prov_nodes],
        "source": "live_tool",
    }


def _extract_slice_metrics_and_provenance(
    slice_id: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Extract domain metrics and provenance graph node IDs from tool payloads."""
    metrics: dict[str, Any] = {}
    prov: list[str] = []

    if slice_id == "sales_pipeline_brief":
        pipe_val = payload.get("pipeline_value")
        if pipe_val is not None:
            metrics["open_pipeline_value"] = pipe_val
            metrics["at_risk_count"] = payload.get("at_risk_deals_count")
            metrics["won_deals_count"] = payload.get("won_count_this_period")
            metrics["won_deals_value"] = payload.get("won_value_this_period")
            metrics["pipeline_growth_pct"] = payload.get("pipeline_growth_pct")

        pipe = payload.get("pipeline") or {}
        at_risk = payload.get("at_risk_deals") or {}
        won = payload.get("won_deals") or {}
        if pipe:
            metrics["pipeline_growth_pct"] = pipe.get("pipeline_growth_pct")
            metrics["open_pipeline_value"] = pipe.get(
                "open_pipeline_value", metrics.get("open_pipeline_value")
            )
            metrics["pipeline_deals_count"] = pipe.get("deals_count")
        if isinstance(at_risk, dict):
            if "count" in at_risk:
                metrics["at_risk_count"] = at_risk.get("count")
            for d in at_risk.get("deals") or []:
                d_id = d.get("id") or d.get("deal_id")
                if d_id:
                    prov.append(f"deal:{d_id}")
        if isinstance(won, dict):
            if "count" in won:
                metrics["won_deals_count"] = won.get("count")
            if "total_value" in won:
                metrics["won_deals_value"] = won.get("total_value")
            for d in won.get("deals") or []:
                d_id = d.get("id") or d.get("deal_id")
                if d_id:
                    prov.append(f"quote:{d_id}")

    elif slice_id == "support_at_risk":
        op = payload.get("operations_slice") or {}
        sla_list = op.get("sla_at_risk") or []
        churn_list = op.get("churn_risk_customers") or []
        proactive_list = op.get("proactive_tickets") or []
        metrics["sla_at_risk_count"] = op.get("sla_at_risk_count", len(sla_list))
        metrics["churn_risk_count"] = op.get("churn_risk_count", len(churn_list))
        metrics["proactive_tickets_count"] = op.get("proactive_tickets_count", len(proactive_list))

        for t in sla_list:
            t_id = t.get("ticket_id") or t.get("id")
            if t_id:
                prov.append(f"ticket:{t_id}")
        for c in churn_list:
            c_id = c.get("customer_id")
            if c_id:
                prov.append(f"customer:{c_id}")
        for t in proactive_list:
            t_id = t.get("id") or t.get("ticket_id")
            if t_id:
                prov.append(f"ticket:{t_id}")

    elif slice_id == "resources_forecast":
        metrics["capacity_utilization_pct"] = payload.get("capacity_utilization_pct")
        metrics["capacity_status"] = payload.get("capacity_status")
        metrics["supply_hours"] = payload.get("supply_hours")
        metrics["demand_hours"] = payload.get("demand_hours")
        metrics["net_capacity_gap_hours"] = payload.get("net_capacity_gap_hours")
        for r in payload.get("resources") or []:
            r_id = r.get("id") or r.get("resource_id")
            if r_id:
                prov.append(f"resource:{r_id}")

    elif slice_id == "economy_financial_pulse":
        metrics["mrr"] = payload.get("mrr")
        metrics["arr"] = payload.get("arr")
        metrics["gross_margin_pct"] = payload.get("gross_margin_pct")
        metrics["margin_compression_bps"] = payload.get("margin_compression_bps")
        metrics["churn_rate"] = payload.get("churn_rate")
        for p in payload.get("postings") or []:
            p_id = p.get("id") or p.get("posting_id")
            if p_id:
                prov.append(f"posting:{p_id}")

    elif slice_id == "procurement_savings":
        metrics["realized_savings_nok"] = payload.get("realized_savings_nok")
        metrics["leakage_candidates_count"] = payload.get("leakage_candidates_count")
        for s in payload.get("leakage_candidates") or []:
            s_id = s.get("supplier_id") or s.get("id")
            if s_id:
                prov.append(f"supplier:{s_id}")

    elif slice_id == "agreements_coverage":
        flags = payload.get("flags") or []
        metrics["agreements_scanned"] = payload.get("agreements_scanned")
        metrics["flags_count"] = len(flags)
        metrics["expiry_flags_count"] = len([f for f in flags if f.get("flag_type") == "expiry"])
        metrics["leakage_flags_count"] = len([f for f in flags if f.get("flag_type") == "leakage"])
        for f in flags:
            ag_id = f.get("agreement_id")
            if ag_id:
                prov.append(f"agreement:{ag_id}")

    elif slice_id == "inventory_dead_stock":
        metrics["dead_stock_value"] = payload.get("dead_stock_value") or payload.get(
            "total_dead_stock_value"
        )
        for item in payload.get("dead_items") or []:
            sku = item.get("sku") or item.get("product_sku")
            if sku:
                prov.append(f"sku:{sku}")

    # Ensure at least one grounding node linking to the slice producer
    if not prov:
        prov.append(f"slice:{slice_id}")

    return metrics, prov


def _make_degraded_response(
    spec: ComposedSliceSpec, reason: str, detail: str = ""
) -> dict[str, Any]:
    """Build an absent and labelled response adhering to BI-4 (never 0, never blank)."""
    return {
        "ok": False,
        "slice_id": spec.slice_id,
        "engine": spec.engine,
        "tool_name": spec.tool_name,
        "degraded": True,
        "status": STATUS_NOT_AVAILABLE_YET,
        "display_value": STATUS_NOT_AVAILABLE_YET,
        "value": None,
        "data": None,
        "metrics": {},
        "provenance_nodes": [],
        "derived_from": [],
        "reason": reason,
        "detail": detail,
        "source": "degraded_fallback",
    }


def _record_slice_degradation(
    namespace_id: str | UUID,
    slice_id: str,
    engine: str,
    code: str,
    detail: str,
    hint: str,
) -> None:
    """Record observable non-fatal degradation event in nce.degradation."""
    try:
        from nce.degradation import record_degradation

        ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
        record_degradation(
            namespace_id=ns_uuid,
            engine="business_insights",
            code=code,
            detail=detail,
            onboarding_hint=hint,
        )
    except Exception:
        log.warning("Failed to record degradation for slice %s", slice_id, exc_info=True)
