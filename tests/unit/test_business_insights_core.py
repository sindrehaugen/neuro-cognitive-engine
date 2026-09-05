"""
tests/unit/test_business_insights_core.py
=========================================
Unit tests for Module 16 (Business Insights Engine) core domain logic:
  - do_morning_brief (1 risk, 1 opportunity, financial pulse, capacity headline)
  - Provenance traceability on every claim (derived_from edges)
  - Exec/board role authorization gate
  - Ledger audit emission to v3_cognitive_ledger
  - Grace degradation for unlanded capacity engine
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.business_insights.board_pack import do_generate_board_pack
from nce.vertical_modules.business_insights.brief import (
    MorningBriefUngroundedError,
    do_morning_brief,
)
from nce.vertical_modules.business_insights.scenario import do_run_scenario


class DummyConnection:
    def __init__(self):
        self.queries = []

    async def execute(self, query: str, *args):
        self.queries.append((query, args))
        return "INSERT 0 1"

    async def fetch(self, query: str, *args):
        return []

    async def fetchrow(self, query: str, *args):
        return None


class DummyPool:
    def __init__(self):
        self.conn = DummyConnection()

    def acquire(self):
        class _Ctx:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                return self.conn

            async def __aexit__(self, *args):
                pass

        return _Ctx(self.conn)


class DummyEngine:
    def __init__(self):
        self.pg_pool = DummyPool()
        self.pool = self.pg_pool


@pytest.mark.asyncio
async def test_morning_brief_role_gate():
    """do_morning_brief must reject unauthorized roles."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "guest",
    }
    with pytest.raises(PermissionError) as exc:
        await do_morning_brief(engine, params)
    assert "Access denied" in str(exc.value)


@pytest.mark.asyncio
async def test_morning_brief_structure_and_provenance():
    """do_morning_brief must return top risk, opportunity, financial pulse, capacity, all with provenance."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "actor": "exec@example.test",
    }
    result = await do_morning_brief(engine, params)
    assert result["status"] == "ok"
    brief = result["briefing"]

    # Must have 4 canonical pillars
    assert "top_risk" in brief
    assert "top_opportunity" in brief
    assert "financial_pulse" in brief
    assert "capacity_headline" in brief

    # Every section must have title, rationale, and non-empty provenance links (except unlanded grace-degraded)
    for section_key in ("top_risk", "top_opportunity", "financial_pulse"):
        section = brief[section_key]
        assert "title" in section and section["title"]
        assert "rationale" in section and section["rationale"]
        assert "provenance_nodes" in section and len(section["provenance_nodes"]) > 0
        assert "derived_from" in section and len(section["derived_from"]) > 0

    # Capacity headline must grace-degrade since Resources(15) is not landed
    cap = brief["capacity_headline"]
    assert cap["degraded"] is True
    assert cap["status"] == "not available yet"
    assert cap["display_value"] == "not available yet"
    assert cap["display_value"] != "0"
    assert cap["display_value"] != ""

    # Graph nodes and edges created
    nodes = result["graph_nodes"]
    assert any(n["entity_type"] == "BUSINESS_INSIGHTS_BRIEFING" for n in nodes)
    assert any(n["entity_type"] == "BUSINESS_INSIGHTS_FINDING" for n in nodes)

    edges = result["graph_edges"]
    assert any(e["edge_type"] == "surfaces" for e in edges)
    assert any(e["edge_type"] == "derived_from" for e in edges)


@pytest.mark.asyncio
async def test_morning_brief_fails_if_claim_has_no_provenance():
    """Gate: Every finding must resolve to a derived_from edge; a finding with no source fails."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "simulate_unprovenanced_claim": True,
    }
    with pytest.raises(MorningBriefUngroundedError) as exc:
        await do_morning_brief(engine, params)
    assert "unprovenanced claim" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_scenario_modeling_and_monte_carlo():
    """do_run_scenario computes pipeline, cashflow Monte-Carlo, BI-4 capacity degradation, and graph nodes."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "actor": "exec@example.test",
        "name": "Q3 Growth Push",
        "assumptions": {
            "deals": [
                {
                    "id": "deal-1",
                    "name": "Deal Alpha",
                    "value": 500000.0,
                    "staff_needed_fte": 3.0,
                    "win_probability": 0.8,
                },
                {
                    "id": "deal-2",
                    "name": "Deal Beta",
                    "value": 300000.0,
                    "staff_needed_fte": 2.0,
                    "win_probability": 0.5,
                },
            ],
            "months": 6,
            "baseline_cash": 1500000.0,
            "monthly_burn": 100000.0,
            "monte_carlo": True,
            "monte_carlo_iterations": 200,
        },
    }
    result = await do_run_scenario(engine, params)
    assert result["status"] == "ok"
    proj = result["projections"]

    # 1. Pipeline
    pipe = proj["pipeline"]
    assert pipe["deals_count"] == 2
    assert pipe["total_pipeline_value"] == 800000.0
    assert pipe["expected_bookings"] == 550000.0  # 500k*0.8 + 300k*0.5
    assert pipe["total_fte_demanded"] == 5.0

    # 2. Capacity: Grace degraded because Resources engine is not landed
    cap = proj["capacity"]
    assert cap["degraded"] is True
    assert cap["status"] == "not available yet"
    assert cap["display_value"] == "not available yet"
    assert cap["display_value"] != "0"
    assert cap["display_value"] != ""

    # 3. Cashflow & Monte-Carlo
    cash = proj["cashflow"]
    assert cash["deterministic_ending_cash"] == 1450000.0  # 1.5M - 600k + 550k
    mc = cash["monte_carlo"]
    assert mc["iterations"] == 200
    assert mc["ending_cash_p10"] <= mc["ending_cash_p50"] <= mc["ending_cash_p90"]
    assert 0.0 <= mc["probability_cash_positive"] <= 1.0

    # 4. Graph nodes and edges
    nodes = result["graph_nodes"]
    assert len(nodes) == 1
    assert nodes[0]["entity_type"] == "BUSINESS_INSIGHTS_SCENARIO"
    edges = result["graph_edges"]
    assert len(edges) == 3
    assert all(e["edge_type"] == "projects" for e in edges)


@pytest.mark.asyncio
async def test_board_pack_generation_staged_as_draft():
    """do_generate_board_pack generates structured narrative staged as draft for review."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "board",
        "actor": "board@example.test",
        "period": "2026-Q3",
        "data_override": {
            "executive_summary": {"headline": "Q3 Board Review: Expansion Phase"},
            "economy": {"revenue": "$5,100,000", "gross_margin_pct": 40.2},
            "sales": {"pipeline_total_value": "$14,000,000"},
        },
    }
    result = await do_generate_board_pack(engine, params)
    assert result["status"] == "ok"
    assert result["period"] == "2026-Q3"
    bp = result["board_pack"]

    # Must be staged as draft
    assert bp["staged_as_draft"] is True
    assert bp["status"] == "draft_staged_for_review"
    assert "draft" in bp["role_advisory_statement"].lower()

    # Sections contract
    sections = bp["sections"]
    assert "executive_summary" in sections
    assert sections["executive_summary"]["headline"] == "Q3 Board Review: Expansion Phase"
    assert "financial_pulse" in sections
    assert sections["financial_pulse"]["revenue"] == "$5,100,000"
    assert "sales_pipeline" in sections
    assert "operational_capacity" in sections
    # Capacity grace-degraded
    assert sections["operational_capacity"]["status"] == "not available yet"
    assert sections["operational_capacity"]["display_value"] == "not available yet"
    assert "risk_radar_summary" in sections


@pytest.mark.asyncio
async def test_scenario_and_board_pack_role_gates():
    """Both do_run_scenario and do_generate_board_pack reject unauthorized roles."""
    engine = DummyEngine()
    unauth = {"namespace_id": "00000000-0000-4000-8000-000000000001", "principal_role": "intern"}

    with pytest.raises(PermissionError):
        await do_run_scenario(engine, unauth)

    with pytest.raises(PermissionError):
        await do_generate_board_pack(engine, unauth)
