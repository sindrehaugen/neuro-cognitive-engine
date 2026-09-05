"""
tests/unit/test_business_insights_coverage.py
=============================================
Unit tests for BI-2 (Confidence and Coverage) and Risk Radar (Phase 3):
  - BI-2 coverage indicator: 'based on N engines, M fully reconciled, K with structured attribution'
  - Low-coverage findings are FLAGGED, not asserted as undisputed facts
  - Cross-engine collision detection across 3 canonical classes:
      1. pipeline-up x capacity-redlined (Sales x Resources/Project)
      2. margin-erosion x dead-stock (Economy x Inventory)
      3. SLA-breach-trend x renewal-due (Support x Agreements)
  - BI-4 Grace degradation for unlanded engines (never 0, never blank)
  - Role-scoped execution gating
"""

from __future__ import annotations

import pytest

from nce.vertical_modules.business_insights.coverage import compute_coverage_indicator
from nce.vertical_modules.business_insights.radar import do_risk_radar


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


def test_coverage_indicator_fully_reconciled():
    """BI-2: High coverage when all engines are reconciled and have structured attribution."""
    engines = ["economy", "project", "sales"]
    details = {
        "economy": {"reconciled": True, "structured_attribution": True, "live": True},
        "project": {"reconciled": True, "structured_attribution": True, "live": True},
        "sales": {"reconciled": True, "structured_attribution": True, "live": True},
    }
    indicator = compute_coverage_indicator(engines, details)
    assert indicator["total_engines"] == 3
    assert indicator["reconciled_engines"] == 3
    assert indicator["structured_attribution_engines"] == 3
    assert indicator["is_low_coverage"] is False
    assert indicator["flagged"] is False
    assert (
        indicator["summary"]
        == "based on 3 engines, 3 fully reconciled, 3 with structured attribution"
    )
    assert len(indicator["flags"]) == 0


def test_coverage_indicator_low_coverage_flagged():
    """BI-2: Low coverage flagged when an engine is unreconciled or lacking attribution."""
    engines = ["economy", "support"]
    details = {
        "economy": {"reconciled": False, "structured_attribution": True, "live": True},
        "support": {"reconciled": True, "structured_attribution": False, "live": True},
    }
    indicator = compute_coverage_indicator(engines, details)
    assert indicator["total_engines"] == 2
    assert indicator["reconciled_engines"] == 1
    assert indicator["structured_attribution_engines"] == 1
    assert indicator["is_low_coverage"] is True
    assert indicator["flagged"] is True
    assert (
        indicator["summary"]
        == "based on 2 engines, 1 fully reconciled, 1 with structured attribution"
    )
    assert len(indicator["flags"]) == 2
    assert any("economy: books not reconciled" in f for f in indicator["flags"])
    assert any("support: lacking structured outcome attribution" in f for f in indicator["flags"])


@pytest.mark.asyncio
async def test_risk_radar_evaluates_canonical_collisions():
    """Risk radar detects all 3 canonical cross-engine collision classes."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "actor": "exec@example.test",
        "data_override": {
            "sales": {
                "pipeline_growth_pct": 35.0,
                "pipeline_value": "$1,500,000",
                "provenance_nodes": ["quote:QUOTE-001", "quote:QUOTE-002"],
            },
            "resources": {
                "capacity_utilization_pct": 94.0,
                "capacity_status": "redlined",
                "live": True,
                "provenance_nodes": ["resource:RES-101"],
            },
            "economy": {
                "margin_compression_bps": 250,
                "reconciled": True,
                "provenance_nodes": ["invoice:INV-501"],
            },
            "inventory": {
                "dead_stock_value": 80000.0,
                "provenance_nodes": ["sku:SKU-909"],
            },
            "support": {
                "sla_breach_rate_pct": 20.0,
                "structured_attribution": True,
                "provenance_nodes": ["ticket:TICK-101"],
            },
            "agreements": {
                "renewal_window_days": 45,
                "provenance_nodes": ["agreement:AGR-202"],
            },
        },
    }
    result = await do_risk_radar(engine, params)
    assert result["status"] == "ok"
    findings = result["findings"]
    assert len(findings) >= 3

    rule_ids = {f["rule_id"] for f in findings}
    assert "pipeline_up_capacity_redlined" in rule_ids
    assert "margin_erosion_dead_stock" in rule_ids
    assert "sla_breach_trend_renewal_due" in rule_ids

    # Check finding contract
    for f in findings:
        assert "title" in f and f["title"]
        assert "severity" in f and f["severity"] in ("critical", "high", "medium", "low")
        assert "rationale" in f and f["rationale"]
        assert "engines" in f and len(f["engines"]) >= 2
        assert "provenance_node_ids" in f and len(f["provenance_node_ids"]) > 0
        assert "coverage" in f
        assert "assertion_status" in f
        assert "based on" in f["coverage"]["summary"]

    # Graph nodes and edges
    graph_nodes = result["graph_nodes"]
    assert any(n["entity_type"] == "BUSINESS_INSIGHTS_FINDING" for n in graph_nodes)
    graph_edges = result["graph_edges"]
    assert any(e["edge_type"] == "derived_from" for e in graph_edges)


@pytest.mark.asyncio
async def test_risk_radar_flags_low_coverage_findings_not_asserted():
    """
    BI-2 Control that can fail:
    Findings built on unreconciled or attribution-poor slices MUST be flagged
    with assertion_status='flagged_low_coverage' and flagged=True, NEVER asserted as undisputed fact.
    """
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "data_override": {
            "economy": {
                "margin_compression_bps": 300,
                "reconciled": False,  # Unreconciled books!
                "provenance_nodes": ["invoice:INV-999"],
            },
            "inventory": {
                "dead_stock_value": 95000.0,
                "provenance_nodes": ["sku:SKU-777"],
            },
        },
    }
    result = await do_risk_radar(engine, params)
    findings = result["findings"]
    margin_finding = next(f for f in findings if f["rule_id"] == "margin_erosion_dead_stock")

    # Must be FLAGGED, NOT asserted
    assert margin_finding["flagged"] is True
    assert margin_finding["coverage"]["is_low_coverage"] is True
    assert margin_finding["assertion_status"] == "flagged_low_coverage"
    assert margin_finding["assertion_status"] != "asserted"
    assert any(
        "economy: books not reconciled" in flag for flag in margin_finding["coverage"]["flags"]
    )


@pytest.mark.asyncio
async def test_risk_radar_grace_degradation_unlanded_engine():
    """
    BI-4: When Resources engine is unlanded, pipeline_up_capacity_redlined
    shows capacity as 'not available yet' (never 0, never blank) and flags low coverage.
    """
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "executive",
        "data_override": {
            "sales": {
                "pipeline_growth_pct": 40.0,
                "pipeline_value": "$2,000,000",
                "provenance_nodes": ["quote:QUOTE-700"],
            },
            "resources": {
                "live": False,  # Unlanded engine!
                "capacity_utilization_pct": None,
                "capacity_status": "not available yet",
                "provenance_nodes": [],
            },
        },
    }
    result = await do_risk_radar(engine, params)
    findings = result["findings"]
    pipeline_finding = next(f for f in findings if f["rule_id"] == "pipeline_up_capacity_redlined")

    assert pipeline_finding["flagged"] is True
    assert pipeline_finding["coverage"]["is_low_coverage"] is True
    assert "not available yet" in pipeline_finding["rationale"]
    # Must NOT report '0%' or empty
    assert "at 0%" not in pipeline_finding["rationale"]
    assert "at %" not in pipeline_finding["rationale"]


@pytest.mark.asyncio
async def test_risk_radar_role_gating():
    """do_risk_radar must reject unauthorized roles."""
    engine = DummyEngine()
    params = {
        "namespace_id": "00000000-0000-4000-8000-000000000001",
        "principal_role": "intern",
    }
    with pytest.raises(PermissionError) as exc:
        await do_risk_radar(engine, params)
    assert "Access denied" in str(exc.value)
