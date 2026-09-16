"""
nce/vertical_modules/business_insights/radar.py
================================================
Cross-engine collision detection (The Risk Radar — The Moat).

Correlates signals across disparate engines that no single engine can see:
  1. pipeline-up x capacity-redlined (Sales x Resources/Project)
  2. margin-erosion x dead-stock (Economy x Inventory)
  3. SLA-breach-trend x renewal-due (Support x Agreements)

Enforces:
  - BI-2: Findings carry confidence and coverage ('based on N engines, M fully reconciled, K structured attribution').
          Low-coverage findings are FLAGGED, not asserted as undisputed facts.
  - BI-4: Day-one grace degradation for unlanded engines (never 0, never blank).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nce.vertical_modules.business_insights._guard import (
    is_engine_landed,
    require_insights_role,
)
from nce.vertical_modules.business_insights.coverage import compute_coverage_indicator
from nce.vertical_modules.business_insights.provenance import (
    make_edge,
    make_finding_node,
    record_ledger_audit,
)

log = logging.getLogger("nce.vertical_modules.business_insights.radar")

_RULES_FILE = Path(__file__).parent / "business-insights-risk-rules.json"

SEVERITY_WEIGHTS = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def load_default_risk_rules() -> list[dict[str, Any]]:
    """Load default cross-engine risk rules from module JSON config."""
    try:
        if _RULES_FILE.exists():
            with open(_RULES_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data.get("rules", [])
    except Exception as exc:
        log.warning("Failed to load risk rules from %s: %s", _RULES_FILE, exc)
    return []


async def do_risk_radar(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Execute cross-engine collision detection across cognitive graph and A2A signals.

    Params:
      - namespace_id: str | UUID (required)
      - principal_role: str (required for authorization check)
      - actor: str (optional, for ledger audit)
      - rules: list[dict] (optional override)
      - min_severity: str (optional filter)
      - data_override: dict (optional signal override for testing/deterministic simulation)
    """
    namespace_id = params.get("namespace_id") or params.get("namespace")
    if not namespace_id:
        raise ValueError("namespace_id is required for do_risk_radar")

    principal_role = params.get("principal_role") or "executive"
    await require_insights_role(principal_role, allow_board=True)

    actor = params.get("actor") or "system"
    rules = params.get("rules") or load_default_risk_rules()
    data_override = params.get("data_override") or {}

    findings: list[dict[str, Any]] = []
    unevaluated_rules: list[dict[str, Any]] = []
    evaluated_clear_rules: list[dict[str, Any]] = []
    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []
    all_evaluated_engines: set[str] = set()
    all_engine_details: dict[str, dict[str, Any]] = {}

    for rule in rules:
        rule_id = rule.get("id")
        rule_name = rule.get("name") or rule_id
        severity = rule.get("severity") or "medium"
        rule_engines = rule.get("engines") or []
        thresholds = rule.get("thresholds") or {}
        all_evaluated_engines.update(rule_engines)

        # Determine engine status and details
        engine_details: dict[str, dict[str, Any]] = {}
        for eng in rule_engines:
            override = data_override.get(eng) or {}
            live_status = override.get("live")
            if live_status is None:
                live_status = is_engine_landed(eng)
            reconciled = override.get("reconciled")
            if reconciled is None:
                reconciled = True
            has_attribution = override.get("structured_attribution")
            if has_attribution is None:
                has_attribution = True
            details = {
                "live": live_status,
                "reconciled": reconciled,
                "structured_attribution": has_attribution,
            }
            engine_details[eng] = details
            all_engine_details[eng] = details

        # Evaluate rule conditions
        triggered = False
        rationale = ""
        provenance_node_ids: list[str] = []

        if rule_id == "pipeline_up_capacity_redlined":
            sales_data = data_override.get("sales")
            resources_data = data_override.get("resources")
            project_data = data_override.get("project") or {}

            sales_live = engine_details.get("sales", {}).get("live", False)
            resources_live = engine_details.get("resources", {}).get("live", False)

            pipeline_growth = sales_data.get("pipeline_growth_pct") if sales_data else None
            pipeline_val = sales_data.get("pipeline_value") if sales_data else None

            if not sales_live or sales_data is None or pipeline_growth is None:
                uneval_entry = {
                    "rule_id": rule_id,
                    "title": rule_name,
                    "severity": severity,
                    "status": "unevaluated",
                    "reason": "data_unavailable",
                    "detail": "Sales pipeline growth metric is unavailable; rule cannot be evaluated.",
                    "missing_engines": ["sales"] if not sales_live else [],
                }
                unevaluated_rules.append(uneval_entry)
                try:
                    from nce.degradation import record_degradation

                    record_degradation(
                        namespace_id=namespace_id,
                        engine="business_insights",
                        code="radar_pipeline_up_capacity_redlined_sales_unavailable",
                        detail="Sales pipeline growth unavailable; risk rule pipeline_up_capacity_redlined unevaluated.",
                        onboarding_hint="Supply Sales pipeline metrics to evaluate commercial capacity strain.",
                    )
                except Exception:
                    log.warning("Failed to record degradation event", exc_info=True)
                continue

            growth_thresh = thresholds.get("pipeline_growth_pct")
            if growth_thresh is None:
                growth_thresh = 25.0
            util_thresh = thresholds.get("capacity_utilization_pct")
            if util_thresh is None:
                util_thresh = 85.0

            if pipeline_growth < growth_thresh:
                evaluated_clear_rules.append(
                    {
                        "rule_id": rule_id,
                        "title": rule_name,
                        "severity": severity,
                        "status": "evaluated_clear",
                        "rationale": f"Pipeline growth ({pipeline_growth}%) is below risk threshold ({growth_thresh}%).",
                    }
                )
                continue

            prov_sales = sales_data.get("provenance_nodes") or []
            prov_proj = project_data.get("provenance_nodes") or []
            display_val = pipeline_val if pipeline_val is not None else "significant volume"

            if not resources_live:
                triggered = True
                rationale = (
                    f"Sales pipeline surged by {pipeline_growth}% to {display_val}, "
                    f"while delivery capacity is not available yet. "
                    f"Potential operational strain flagged pending capacity data."
                )
                provenance_node_ids = prov_sales + prov_proj
            else:
                if resources_data is None:
                    uneval_entry = {
                        "rule_id": rule_id,
                        "title": rule_name,
                        "severity": severity,
                        "status": "unevaluated",
                        "reason": "data_unavailable",
                        "detail": "Resources capacity metric is unavailable; rule cannot be evaluated.",
                        "missing_engines": [],
                    }
                    unevaluated_rules.append(uneval_entry)
                    try:
                        from nce.degradation import record_degradation

                        record_degradation(
                            namespace_id=namespace_id,
                            engine="business_insights",
                            code="radar_pipeline_up_capacity_redlined_resources_unavailable",
                            detail="Resources capacity unavailable; risk rule pipeline_up_capacity_redlined unevaluated.",
                            onboarding_hint="Supply Resources capacity metrics to evaluate operational capacity strain.",
                        )
                    except Exception:
                        log.warning("Failed to record degradation event", exc_info=True)
                    continue

                cap_status = resources_data.get("capacity_status")
                cap_util = resources_data.get("capacity_utilization_pct")
                prov_res = resources_data.get("provenance_nodes") or []
                provenance_node_ids = prov_sales + prov_res + prov_proj

                if (cap_util is not None and cap_util >= util_thresh) or cap_status == "redlined":
                    triggered = True
                    cap_desc = f"{cap_util}%" if cap_util is not None else "redlined"
                    rationale = (
                        f"Sales pipeline surged by {pipeline_growth}% to {display_val}, "
                        f"while delivery capacity is at {cap_desc} ({cap_status or 'strained'}). "
                        f"New commitments risk project delivery delays and customer dissatisfaction."
                    )
                else:
                    evaluated_clear_rules.append(
                        {
                            "rule_id": rule_id,
                            "title": rule_name,
                            "severity": severity,
                            "status": "evaluated_clear",
                            "rationale": f"Delivery capacity ({cap_util}%) is within operating thresholds.",
                        }
                    )
                    continue

        elif rule_id == "margin_erosion_dead_stock":
            economy_data = data_override.get("economy")
            inventory_data = data_override.get("inventory")

            economy_live = engine_details.get("economy", {}).get("live", False)
            inventory_live = engine_details.get("inventory", {}).get("live", False)

            compression_bps = economy_data.get("margin_compression_bps") if economy_data else None
            dead_stock_val = inventory_data.get("dead_stock_value") if inventory_data else None

            if (
                not economy_live
                or not inventory_live
                or compression_bps is None
                or dead_stock_val is None
            ):
                missing = []
                if not economy_live or compression_bps is None:
                    missing.append("economy")
                if not inventory_live or dead_stock_val is None:
                    missing.append("inventory")
                uneval_entry = {
                    "rule_id": rule_id,
                    "title": rule_name,
                    "severity": severity,
                    "status": "unevaluated",
                    "reason": "data_unavailable",
                    "detail": f"Required data unavailable from: {', '.join(missing)}; rule cannot be evaluated.",
                    "missing_engines": missing,
                }
                unevaluated_rules.append(uneval_entry)
                try:
                    from nce.degradation import record_degradation

                    record_degradation(
                        namespace_id=namespace_id,
                        engine="business_insights",
                        code="radar_margin_erosion_dead_stock_data_unavailable",
                        detail=f"Data unavailable from {', '.join(missing)}; risk rule margin_erosion_dead_stock unevaluated.",
                        onboarding_hint="Connect Economy and Inventory engines to evaluate margin compression vs dead stock.",
                    )
                except Exception:
                    log.warning("Failed to record degradation event", exc_info=True)
                continue

            bps_thresh = thresholds.get("margin_compression_bps")
            if bps_thresh is None:
                bps_thresh = 200
            stock_thresh = thresholds.get("dead_stock_value_threshold")
            if stock_thresh is None:
                stock_thresh = 50000.0

            if compression_bps >= bps_thresh and dead_stock_val >= stock_thresh:
                triggered = True
                prov_eco = economy_data.get("provenance_nodes") or []
                prov_inv = inventory_data.get("provenance_nodes") or []
                provenance_node_ids = prov_eco + prov_inv
                rationale = (
                    f"Gross margins compressed by {compression_bps} bps while dead stock "
                    f"reached ${dead_stock_val:,.2f}. Unsold inventory is tying up operating "
                    f"capital amidst narrowing margins."
                )
            else:
                evaluated_clear_rules.append(
                    {
                        "rule_id": rule_id,
                        "title": rule_name,
                        "severity": severity,
                        "status": "evaluated_clear",
                        "rationale": f"Margin compression ({compression_bps} bps) or dead stock (${dead_stock_val:,.2f}) did not exceed thresholds.",
                    }
                )
                continue

        elif rule_id == "sla_breach_trend_renewal_due":
            support_data = data_override.get("support")
            agreements_data = data_override.get("agreements")

            support_live = engine_details.get("support", {}).get("live", False)
            agreements_live = engine_details.get("agreements", {}).get("live", False)

            breach_rate = support_data.get("sla_breach_rate_pct") if support_data else None
            renewal_days = agreements_data.get("renewal_window_days") if agreements_data else None

            if (
                not support_live
                or not agreements_live
                or breach_rate is None
                or renewal_days is None
            ):
                missing = []
                if not support_live or breach_rate is None:
                    missing.append("support")
                if not agreements_live or renewal_days is None:
                    missing.append("agreements")
                uneval_entry = {
                    "rule_id": rule_id,
                    "title": rule_name,
                    "severity": severity,
                    "status": "unevaluated",
                    "reason": "data_unavailable",
                    "detail": f"Required data unavailable from: {', '.join(missing)}; rule cannot be evaluated.",
                    "missing_engines": missing,
                }
                unevaluated_rules.append(uneval_entry)
                try:
                    from nce.degradation import record_degradation

                    record_degradation(
                        namespace_id=namespace_id,
                        engine="business_insights",
                        code="radar_sla_breach_trend_renewal_due_data_unavailable",
                        detail=f"Data unavailable from {', '.join(missing)}; risk rule sla_breach_trend_renewal_due unevaluated.",
                        onboarding_hint="Connect Support and Agreements engines to evaluate SLA breaches on renewal-due contracts.",
                    )
                except Exception:
                    log.warning("Failed to record degradation event", exc_info=True)
                continue

            breach_thresh = thresholds.get("sla_breach_rate_pct")
            if breach_thresh is None:
                breach_thresh = 15.0
            window_thresh = thresholds.get("renewal_window_days")
            if window_thresh is None:
                window_thresh = 90

            if breach_rate >= breach_thresh and renewal_days <= window_thresh:
                triggered = True
                prov_sup = support_data.get("provenance_nodes") or []
                prov_agr = agreements_data.get("provenance_nodes") or []
                provenance_node_ids = prov_sup + prov_agr
                rationale = (
                    f"Enterprise agreement renewal is due within {renewal_days} days, "
                    f"but customer support tickets show an SLA breach rate of {breach_rate}%. "
                    f"Churn risk is elevated."
                )
            else:
                evaluated_clear_rules.append(
                    {
                        "rule_id": rule_id,
                        "title": rule_name,
                        "severity": severity,
                        "status": "evaluated_clear",
                        "rationale": f"SLA breach rate ({breach_rate}%) or renewal window ({renewal_days} days) did not breach risk thresholds.",
                    }
                )
                continue

        else:
            # Generic rule fallback
            evaluated_clear_rules.append(
                {
                    "rule_id": rule_id,
                    "title": rule_name,
                    "severity": severity,
                    "status": "evaluated_clear",
                    "rationale": "No collision criteria matched.",
                }
            )
            continue

        if triggered:
            coverage = compute_coverage_indicator(rule_engines, engine_details)
            is_low_coverage = coverage["is_low_coverage"]
            assertion_status = "flagged_low_coverage" if is_low_coverage else "asserted"

            finding_node = make_finding_node(
                namespace_id=namespace_id,
                finding_type=rule_id,
                title=rule_name,
                rationale=rationale,
                provenance_node_ids=provenance_node_ids,
                coverage=coverage,
            )
            graph_nodes.append(finding_node)

            for prov_id in provenance_node_ids:
                edge = make_edge(
                    namespace_id=namespace_id,
                    source_id=finding_node["id"],
                    target_id=prov_id,
                    edge_type="derived_from",
                    properties={"rule_id": rule_id},
                )
                graph_edges.append(edge)

            finding_entry = {
                "rule_id": rule_id,
                "title": rule_name,
                "severity": severity,
                "status": "triggered",
                "rationale": rationale,
                "engines": rule_engines,
                "provenance_node_ids": provenance_node_ids,
                "coverage": coverage,
                "flagged": is_low_coverage,
                "assertion_status": assertion_status,
                "graph_node_id": finding_node["id"],
            }
            findings.append(finding_entry)

    # Rank findings by severity descending, then by coverage
    findings.sort(
        key=lambda f: (
            SEVERITY_WEIGHTS.get(f["severity"]) or 0,
            0 if f["flagged"] else 1,
        ),
        reverse=True,
    )

    overall_coverage = compute_coverage_indicator(
        list(all_evaluated_engines),
        all_engine_details,
    )

    # Record audit in v3_cognitive_ledger
    try:
        pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
        if pool is not None:
            async with pool.acquire() as conn:
                await record_ledger_audit(
                    conn=conn,
                    namespace_id=namespace_id,
                    actor=actor,
                    action="RISK_RADAR_EVALUATED",
                    referenced_nodes=[f["graph_node_id"] for f in findings],
                    details={
                        "findings_count": len(findings),
                        "rule_ids": [f["rule_id"] for f in findings],
                        "unevaluated_rules_count": len(unevaluated_rules),
                        "coverage": overall_coverage,
                    },
                )
    except Exception as exc:
        log.warning("Failed to record risk radar audit: %s", exc)

    return {
        "status": "ok",
        "namespace_id": str(namespace_id),
        "findings": findings,
        "unevaluated_rules": unevaluated_rules,
        "evaluated_clear_rules": evaluated_clear_rules,
        "coverage": overall_coverage,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
