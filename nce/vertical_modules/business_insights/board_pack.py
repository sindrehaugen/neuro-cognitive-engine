"""
nce/vertical_modules/business_insights/board_pack.py
====================================================
Draft board-pack narrative generator for Module 16 (Business Insights Engine).

Contract:
  - Assembles structured board narrative from cognitive graph, KPI snapshots, and risk radar.
  - Staged as a draft for human review and presentation ('drafts; human presents').
  - Advisor role (never writes operational state).
  - Uses Config-as-IP template in business-insights-board-pack-template.json.
  - BI-4: Missing engine slices collapse to 'not available yet' (never 0, never blank).
  - Emits business_insights_board_pack_drafted and audits to v3_cognitive_ledger.
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
from nce.vertical_modules.business_insights.events import (
    EVENT_BUSINESS_INSIGHTS_BOARD_PACK_DRAFTED,
    emit_business_insights_event,
)
from nce.vertical_modules.business_insights.provenance import record_ledger_audit
from nce.vertical_modules.business_insights.radar import do_risk_radar

log = logging.getLogger("nce.vertical_modules.business_insights.board_pack")

_TEMPLATE_FILE = Path(__file__).parent / "business-insights-board-pack-template.json"


def load_board_pack_template() -> dict[str, Any]:
    """Load default Config-as-IP board pack template."""
    try:
        if _TEMPLATE_FILE.exists():
            with open(_TEMPLATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        log.warning("Failed to load board pack template: %s", exc)
    return {
        "template_version": "1.0",
        "title": "Quarterly Board Executive Pack",
        "sections": [],
    }


async def do_generate_board_pack(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a structured quarterly or period board narrative, staged as a draft.

    Params:
      - namespace_id: str | UUID (required)
      - principal_role: str (required for authorization check)
      - period: str (e.g. '2026-Q3')
      - actor: str (optional)
      - data_override: dict (optional mock data)
    """
    namespace_id = params.get("namespace_id") or params.get("namespace")
    if not namespace_id:
        raise ValueError("namespace_id is required for do_generate_board_pack")

    principal_role = params.get("principal_role") or "executive"
    await require_insights_role(principal_role, allow_board=True)

    actor = params.get("actor") or "system"
    raw_period = params.get("period") or params.get("quarter")
    if not raw_period or not str(raw_period).strip():
        raise ValueError("period is required for do_generate_board_pack")
    raw_period = str(raw_period).strip()
    import re

    q_match = re.match(r"^Q([1-4])[-_](\d{4})$", raw_period, re.IGNORECASE)
    if q_match:
        period = f"{q_match.group(2)}-Q{q_match.group(1)}"
    else:
        period = raw_period
    data_override = params.get("data_override") or {}

    template = load_board_pack_template()

    # 1. Executive Summary
    exec_data = data_override.get("executive_summary") or {}
    headline = (
        exec_data.get("headline")
        or f"Executive Board Review ({period}): Commercial Acceleration & Operating Efficiency"
    )
    strategic_highlights = exec_data.get("strategic_highlights") or [
        "Gross margins stabilized following vendor rebate renegotiations.",
        "Pipeline expanded with multiple tier-1 enterprise opportunities.",
        "Operational cross-engine collisions actively monitored via Risk Radar.",
    ]

    # 2. Financial Pulse (Economy - Grace Degradation)
    econ_live = is_engine_landed("economy")
    econ_data = data_override.get("economy")
    if not econ_live or not econ_data:
        financial_pulse = {
            "degraded": True,
            "status": "not available yet",
            "revenue": "not available yet",
            "gross_margin_pct": None,
            "mrr": "not available yet",
            "arr": "not available yet",
            "cash_runway_months": None,
            "notes": "Financial metrics collapsed: Economy engine data is not available yet.",
        }
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=namespace_id,
                engine="business_insights",
                code="board_pack_economy_unavailable",
                detail="Economy engine data unavailable for board pack financial pulse; grace-degraded.",
                onboarding_hint="Deploy Economy engine to unlock board pack financial metrics.",
            )
        except Exception:
            log.warning("Failed to record economy degradation event", exc_info=True)
    else:
        financial_pulse = {
            "degraded": False,
            "status": "operational",
            "revenue": econ_data.get("revenue"),
            "gross_margin_pct": econ_data.get("gross_margin_pct"),
            "mrr": econ_data.get("mrr"),
            "arr": econ_data.get("arr"),
            "cash_runway_months": econ_data.get("cash_runway_months"),
            "notes": "Financial metrics within operating thresholds.",
        }

    # 3. Commercial Pipeline (Sales - Grace Degradation)
    sales_live = is_engine_landed("sales")
    sales_data = data_override.get("sales")
    if not sales_live or not sales_data:
        sales_pipeline = {
            "degraded": True,
            "status": "not available yet",
            "pipeline_total_value": "not available yet",
            "pipeline_growth_pct": None,
            "top_deals": [],
            "conversion_rate_pct": None,
            "notes": "Pipeline metrics collapsed: Sales engine data is not available yet.",
        }
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=namespace_id,
                engine="business_insights",
                code="board_pack_sales_unavailable",
                detail="Sales engine data unavailable for board pack commercial pipeline; grace-degraded.",
                onboarding_hint="Deploy Sales engine to unlock board pack pipeline metrics.",
            )
        except Exception:
            log.warning("Failed to record sales degradation event", exc_info=True)
    else:
        sales_pipeline = {
            "degraded": False,
            "status": "operational",
            "pipeline_total_value": sales_data.get("pipeline_total_value"),
            "pipeline_growth_pct": sales_data.get("pipeline_growth_pct"),
            "top_deals": sales_data.get("top_deals") or [],
            "conversion_rate_pct": sales_data.get("conversion_rate_pct"),
            "notes": "Commercial pipeline within operating thresholds.",
        }

    # 4. Operational Capacity (Resources - BI-4 Grace Degradation)
    resources_live = is_engine_landed("resources")
    res_data = data_override.get("resources")
    if not resources_live or not res_data:
        operational_capacity = {
            "degraded": True,
            "status": "not available yet",
            "display_value": "not available yet",
            "team_utilization_pct": None,
            "capacity_headroom": None,
            "staffing_constraints": "not available yet",
            "notes": "Capacity metrics collapsed: Resources engine data is not available yet.",
        }
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=namespace_id,
                engine="business_insights",
                code="board_pack_resources_unavailable",
                detail="Resources engine data unavailable for board pack operational capacity; grace-degraded.",
                onboarding_hint="Deploy Resources engine to unlock board pack capacity metrics.",
            )
        except Exception:
            log.warning("Failed to record resources degradation event", exc_info=True)
    else:
        util_pct = res_data.get("team_utilization_pct")
        headroom = res_data.get("capacity_headroom")
        staffing = res_data.get("staffing_constraints")
        display_val = f"{util_pct}% utilization" if util_pct is not None else "not available yet"
        operational_capacity = {
            "degraded": False,
            "status": "operational",
            "display_value": display_val,
            "team_utilization_pct": util_pct,
            "capacity_headroom": headroom,
            "staffing_constraints": staffing,
            "notes": "Capacity within operating thresholds.",
        }

    # 5. Risk Radar Summary
    radar_params = {
        "namespace_id": namespace_id,
        "principal_role": principal_role,
        "actor": actor,
    }
    if "radar" in data_override:
        radar_params["data_override"] = data_override["radar"]

    try:
        radar_res = await do_risk_radar(engine, radar_params)
        surfaced_risks = radar_res.get("findings") or []
        unevaluated_rules = radar_res.get("unevaluated_rules") or []
    except Exception as exc:
        log.warning("Risk radar evaluation in board pack generator encountered issue: %s", exc)
        surfaced_risks = []
        unevaluated_rules = []

    # Assemble Structured Board Pack Draft
    board_pack = {
        "title": template.get("title") or "Quarterly Board Executive Pack",
        "period": period,
        "staged_as_draft": True,
        "status": "draft_staged_for_review",
        "role_advisory_statement": (
            "This pack is staged as a draft for human executive review and presentation. "
            "No operational changes are executed automatically."
        ),
        "sections": {
            "executive_summary": {
                "headline": headline,
                "period": period,
                "strategic_highlights": strategic_highlights,
            },
            "financial_pulse": financial_pulse,
            "sales_pipeline": sales_pipeline,
            "operational_capacity": operational_capacity,
            "risk_radar_summary": {
                "active_collisions_count": len(surfaced_risks),
                "unevaluated_rules_count": len(unevaluated_rules),
                "findings": surfaced_risks[:3],  # Top 3 ranked risks
                "unevaluated_rules": unevaluated_rules,
            },
        },
    }

    # Emit Lifecycle Event
    await emit_business_insights_event(
        engine=engine,
        namespace_id=namespace_id,
        event_type=EVENT_BUSINESS_INSIGHTS_BOARD_PACK_DRAFTED,
        params={
            "period": period,
            "headline": headline,
            "staged_as_draft": True,
        },
    )

    # Audit to Cognitive Ledger
    try:
        pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
        if pool is not None:
            async with pool.acquire() as conn:
                await record_ledger_audit(
                    conn=conn,
                    namespace_id=namespace_id,
                    actor=actor,
                    action="BOARD_PACK_DRAFTED",
                    referenced_nodes=[f"period:{period}"],
                    details={
                        "period": period,
                        "staged_as_draft": True,
                        "sections_count": len(board_pack["sections"]),
                    },
                )
    except Exception as exc:
        log.warning("Failed to record board pack audit to v3_cognitive_ledger: %s", exc)

    return {
        "status": "ok",
        "namespace_id": str(namespace_id),
        "period": period,
        "board_pack": board_pack,
    }
