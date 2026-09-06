"""
nce/vertical_modules/business_insights/scenario.py
==================================================
What-if and forward scenario modeling for Module 16 (Business Insights Engine).

Composes:
  - Sales pipeline (deals, win probabilities, booking values)
  - Resources capacity (staffing demand vs available headcount)
  - Economy cashflow (revenue collection, burn rate, Monte-Carlo distribution)

Enforces:
  - BI-4: Day-one grace degradation for unlanded Resources engine (never 0, never blank).
  - Graph contract: SCENARIO node with advisory 'projects' edges.
  - Event log emission: business_insights_scenario_executed.
  - Cognitive ledger audit to v3_cognitive_ledger.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from nce.vertical_modules.business_insights._guard import (
    is_engine_landed,
    require_insights_role,
)
from nce.vertical_modules.business_insights.events import (
    EVENT_BUSINESS_INSIGHTS_SCENARIO_EXECUTED,
    emit_business_insights_event,
)
from nce.vertical_modules.business_insights.provenance import (
    make_edge,
    make_scenario_node,
    record_ledger_audit,
)

log = logging.getLogger("nce.vertical_modules.business_insights.scenario")

DEFAULT_MONTE_CARLO_ITERATIONS = 500


def _percentile(data: list[float], pct: float) -> float:
    """Compute percentile from a sorted float array."""
    if not data:
        return 0.0
    k = (len(data) - 1) * (pct / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    d0 = data[f] * (c - k)
    d1 = data[c] * (k - f)
    return d0 + d1


async def do_run_scenario(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """
    Execute forward what-if scenario modeling and Monte-Carlo cashflow simulation.

    Params:
      - namespace_id: str | UUID (required)
      - principal_role: str (required for authorization check)
      - actor: str (optional, for ledger audit)
      - name: str (optional scenario label)
      - assumptions: dict (deals, months, baseline_cash, monthly_burn, monte_carlo, etc.)
    """
    namespace_id = params.get("namespace_id") or params.get("namespace")
    if not namespace_id:
        raise ValueError("namespace_id is required for do_run_scenario")

    principal_role = params.get("principal_role", "executive")
    await require_insights_role(principal_role, allow_board=True)

    actor = params.get("actor", "system")
    name = params.get("name", "Forward Commercial & Capital Scenario")
    assumptions = params.get("assumptions") or {}

    deals = assumptions.get(
        "deals",
        [
            {
                "id": "deal-alpha",
                "name": "Enterprise Deal Alpha",
                "value": 600000.0,
                "staff_needed_fte": 3.0,
                "win_probability": 0.8,
            },
            {
                "id": "deal-beta",
                "name": "Expansion Beta",
                "value": 400000.0,
                "staff_needed_fte": 2.0,
                "win_probability": 0.6,
            },
        ],
    )
    months = int(assumptions.get("months", 6))
    baseline_cash = float(assumptions.get("baseline_cash", 2000000.0))
    monthly_burn = float(assumptions.get("monthly_burn", 150000.0))
    run_mc = assumptions.get("monte_carlo", True)
    iterations = int(assumptions.get("monte_carlo_iterations", DEFAULT_MONTE_CARLO_ITERATIONS))

    # 1. Pipeline Projection
    total_pipeline_value = sum(float(d.get("value", 0.0)) for d in deals)
    expected_bookings = sum(
        float(d.get("value", 0.0)) * float(d.get("win_probability", 0.5)) for d in deals
    )
    total_fte_demanded = sum(float(d.get("staff_needed_fte", 0.0)) for d in deals)

    pipeline_projection = {
        "deals_count": len(deals),
        "total_pipeline_value": total_pipeline_value,
        "expected_bookings": round(expected_bookings, 2),
        "total_fte_demanded": total_fte_demanded,
    }

    # 2. Capacity Projection (BI-4 Grace Degradation)
    resources_live = is_engine_landed("resources")
    if not resources_live:
        capacity_projection = {
            "degraded": True,
            "status": "not available yet",
            "display_value": "not available yet",
            "staff_needed_fte": total_fte_demanded,
            "available_capacity_fte": None,
            "can_staff": "not available yet",
            "rationale": "Staffing capacity cannot be verified because the Resources engine is not available yet.",
        }
    else:
        avail_fte = float(assumptions.get("available_capacity_fte", 10.0))
        can_staff = avail_fte >= total_fte_demanded
        capacity_projection = {
            "degraded": False,
            "status": "feasible" if can_staff else "constrained",
            "display_value": f"{total_fte_demanded} FTE needed ({'Feasible' if can_staff else 'Constrained'})",
            "staff_needed_fte": total_fte_demanded,
            "available_capacity_fte": avail_fte,
            "headroom_fte": round(avail_fte - total_fte_demanded, 2),
            "can_staff": can_staff,
            "rationale": f"Team has {avail_fte} FTE capacity; scenario demands {total_fte_demanded} FTE.",
        }

    # 3. Cashflow Projection & Monte-Carlo Simulation
    total_burn = monthly_burn * months
    deterministic_ending_cash = baseline_cash - total_burn + expected_bookings

    mc_results: dict[str, Any] | None = None
    if run_mc and iterations > 0:
        rnd = random.Random(42)  # Deterministic seed for reproducible testing
        ending_cash_samples: list[float] = []

        for _ in range(iterations):
            simulated_revenue = 0.0
            for d in deals:
                win_prob = float(d.get("win_probability", 0.5))
                val = float(d.get("value", 0.0))
                if rnd.random() < win_prob:
                    # Apply collection delay / haircut variance +/- 10%
                    factor = rnd.uniform(0.9, 1.1)
                    simulated_revenue += val * factor
            ending_cash = baseline_cash - total_burn + simulated_revenue
            ending_cash_samples.append(ending_cash)

        ending_cash_samples.sort()
        p10 = _percentile(ending_cash_samples, 10.0)
        p50 = _percentile(ending_cash_samples, 50.0)
        p90 = _percentile(ending_cash_samples, 90.0)
        positive_count = sum(1 for c in ending_cash_samples if c > 0)

        mc_results = {
            "iterations": iterations,
            "ending_cash_p10": round(p10, 2),
            "ending_cash_p50": round(p50, 2),
            "ending_cash_p90": round(p90, 2),
            "probability_cash_positive": round(positive_count / iterations, 4),
            "min_ending_cash": round(ending_cash_samples[0], 2),
            "max_ending_cash": round(ending_cash_samples[-1], 2),
        }

    cashflow_projection = {
        "baseline_cash": baseline_cash,
        "monthly_burn": monthly_burn,
        "months": months,
        "total_burn": total_burn,
        "deterministic_ending_cash": round(deterministic_ending_cash, 2),
        "monte_carlo": mc_results,
    }

    results = {
        "pipeline": pipeline_projection,
        "capacity": capacity_projection,
        "cashflow": cashflow_projection,
    }

    # 4. Construct Graph Nodes & Edges
    scenario_node = make_scenario_node(
        namespace_id=namespace_id,
        name=name,
        assumptions=assumptions,
        results=results,
    )

    graph_nodes = [scenario_node]
    graph_edges = [
        make_edge(
            namespace_id, scenario_node["id"], "slice:pipeline", "projects", {"metric": "bookings"}
        ),
        make_edge(
            namespace_id,
            scenario_node["id"],
            "slice:capacity",
            "projects",
            {"metric": "utilization"},
        ),
        make_edge(
            namespace_id,
            scenario_node["id"],
            "slice:cashflow",
            "projects",
            {"metric": "ending_cash"},
        ),
    ]

    # 5. Emit Lifecycle Event
    await emit_business_insights_event(
        engine=engine,
        namespace_id=namespace_id,
        event_type=EVENT_BUSINESS_INSIGHTS_SCENARIO_EXECUTED,
        params={
            "scenario_id": scenario_node["id"],
            "name": name,
            "expected_bookings": expected_bookings,
            "monte_carlo_p50": mc_results["ending_cash_p50"] if mc_results else None,
        },
    )

    # 6. Audit to Cognitive Ledger
    try:
        pool = getattr(engine, "pg_pool", None) or getattr(engine, "pool", None)
        if pool is not None:
            async with pool.acquire() as conn:
                await record_ledger_audit(
                    conn=conn,
                    namespace_id=namespace_id,
                    actor=actor,
                    action="SCENARIO_EXECUTED",
                    referenced_nodes=[scenario_node["id"]],
                    details={
                        "name": name,
                        "assumptions": assumptions,
                        "deterministic_ending_cash": deterministic_ending_cash,
                        "monte_carlo": mc_results,
                    },
                )
    except Exception as exc:
        log.warning("Failed to record scenario audit to v3_cognitive_ledger: %s", exc)

    return {
        "status": "ok",
        "namespace_id": str(namespace_id),
        "scenario_id": scenario_node["id"],
        "name": name,
        "assumptions": assumptions,
        "projections": results,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
