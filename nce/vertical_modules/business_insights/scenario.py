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

    deals = assumptions.get("deals")
    if deals is None:
        deals = params.get("deals")

    if not deals or not isinstance(deals, list):
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=str(namespace_id),
                engine="business_insights",
                code="scenario_deals_missing",
                detail="Scenario modeling refused: deals parameter is required and cannot be empty or synthesized.",
                onboarding_hint="Supply a non-empty list of deals under assumptions.deals or params.deals.",
            )
        except Exception:
            log.warning("Failed to record scenario deals missing degradation event", exc_info=True)
        raise ValueError(
            "deals is required for do_run_scenario; pipeline cannot be empty or synthesized"
        )

    provenance_sources: list[str] = []
    for d in deals:
        if isinstance(d, dict):
            deal_id = d.get("id") or d.get("name") or "unspecified"
            provenance_sources.append(f"deal:{deal_id}")

    # 1. Pipeline Projection
    # Explicit modelling conventions vs invented facts:
    # - value defaults to 0.0 (conservative neutral: unpriced leads contribute $0 to pipeline)
    # - win_probability defaults to 0.5 (Bayesian maximum-entropy: binary uncertainty between win and loss)
    # - staff_needed_fte defaults to 0.0 (conservative baseline: unstated delivery staffing demands require 0 FTE)
    DEFAULT_UNPRICED_DEAL_VALUE = 0.0
    DEFAULT_UNCERTAIN_WIN_PROBABILITY = 0.5
    DEFAULT_UNSTATED_STAFF_FTE = 0.0

    total_pipeline_value = sum(float(d.get("value") or DEFAULT_UNPRICED_DEAL_VALUE) for d in deals)
    expected_bookings = sum(
        float(d.get("value") or DEFAULT_UNPRICED_DEAL_VALUE)
        * float(
            d.get("win_probability")
            if d.get("win_probability") is not None
            else DEFAULT_UNCERTAIN_WIN_PROBABILITY
        )
        for d in deals
    )
    total_fte_demanded = sum(
        float(d.get("staff_needed_fte") or DEFAULT_UNSTATED_STAFF_FTE) for d in deals
    )

    pipeline_projection = {
        "deals_count": len(deals),
        "total_pipeline_value": total_pipeline_value,
        "expected_bookings": round(expected_bookings, 2),
        "total_fte_demanded": total_fte_demanded,
    }

    # 2. Capacity Projection (BI-4 Grace Degradation)
    resources_live = is_engine_landed("resources")
    avail_fte_raw = assumptions.get("available_capacity_fte")
    if avail_fte_raw is None:
        avail_fte_raw = params.get("available_capacity_fte")

    if not resources_live or avail_fte_raw is None:
        capacity_projection = {
            "degraded": True,
            "status": "not available yet",
            "display_value": "not available yet",
            "staff_needed_fte": total_fte_demanded,
            "available_capacity_fte": None,
            "can_staff": "not available yet",
            "rationale": (
                "Staffing capacity cannot be verified because available_capacity_fte was not supplied "
                "and the Resources engine is not available yet."
                if not resources_live
                else "Staffing capacity cannot be verified because available_capacity_fte was not supplied."
            ),
        }
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=str(namespace_id),
                engine="business_insights",
                code="scenario_resources_capacity_unavailable",
                detail="Resources capacity (available_capacity_fte) unavailable for scenario simulation; capacity grace-degraded.",
                onboarding_hint="Supply available_capacity_fte assumption or integrate Resources engine.",
            )
        except Exception:
            log.warning("Failed to record resources capacity degradation event", exc_info=True)
    else:
        avail_fte = float(avail_fte_raw)
        provenance_sources.append("assumption:available_capacity_fte")
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
    econ_live = is_engine_landed("economy")
    baseline_cash_raw = assumptions.get("baseline_cash")
    if baseline_cash_raw is None:
        baseline_cash_raw = params.get("baseline_cash")

    monthly_burn_raw = assumptions.get("monthly_burn")
    if monthly_burn_raw is None:
        monthly_burn_raw = params.get("monthly_burn")

    months_raw = assumptions.get("months")
    if months_raw is None:
        months_raw = params.get("months")
    months = int(months_raw) if months_raw is not None else 6

    run_mc = assumptions.get("monte_carlo", True)
    iterations_raw = assumptions.get("monte_carlo_iterations")
    iterations = (
        int(iterations_raw) if iterations_raw is not None else DEFAULT_MONTE_CARLO_ITERATIONS
    )

    if not econ_live or baseline_cash_raw is None or monthly_burn_raw is None:
        cashflow_projection = {
            "degraded": True,
            "status": "not available yet",
            "baseline_cash": None,
            "monthly_burn": None,
            "months": months,
            "total_burn": None,
            "deterministic_ending_cash": None,
            "monte_carlo": None,
            "notes": "Cashflow projection and Monte-Carlo simulation unavailable: baseline_cash and monthly_burn must be provided.",
        }
        try:
            from nce.degradation import record_degradation

            record_degradation(
                namespace_id=str(namespace_id),
                engine="business_insights",
                code="scenario_economy_cashflow_unavailable",
                detail="Economy data (baseline_cash/monthly_burn) unavailable for scenario simulation; cashflow grace-degraded.",
                onboarding_hint="Supply baseline_cash and monthly_burn assumptions or integrate Economy engine.",
            )
        except Exception:
            log.warning("Failed to record economy cashflow degradation event", exc_info=True)
    else:
        baseline_cash = float(baseline_cash_raw)
        monthly_burn = float(monthly_burn_raw)
        provenance_sources.append("assumption:baseline_cash")
        provenance_sources.append("assumption:monthly_burn")

        total_burn = monthly_burn * months
        deterministic_ending_cash = baseline_cash - total_burn + expected_bookings

        mc_results: dict[str, Any] | None = None
        if run_mc and iterations > 0:
            rnd = random.Random(42)  # Deterministic seed for reproducible testing
            ending_cash_samples: list[float] = []

            for _ in range(iterations):
                simulated_revenue = 0.0
                for d in deals:
                    win_prob = (
                        float(d.get("win_probability"))
                        if d.get("win_probability") is not None
                        else DEFAULT_UNCERTAIN_WIN_PROBABILITY
                    )
                    val = float(d.get("value") or DEFAULT_UNPRICED_DEAL_VALUE)
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
            "degraded": False,
            "status": "operational",
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
            "monte_carlo_p50": cashflow_projection.get("monte_carlo", {}).get("ending_cash_p50")
            if isinstance(cashflow_projection.get("monte_carlo"), dict)
            else None,
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
                        "deterministic_ending_cash": cashflow_projection.get(
                            "deterministic_ending_cash"
                        ),
                        "monte_carlo": cashflow_projection.get("monte_carlo"),
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
        "provenance": provenance_sources,
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
    }
