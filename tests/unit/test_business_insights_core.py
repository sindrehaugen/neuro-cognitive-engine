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

from nce.vertical_modules.business_insights.brief import (
    MorningBriefUngroundedError,
    do_morning_brief,
)


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
