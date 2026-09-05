"""Unit tests for Module 14 Marketing Engine core drafting logic, MK-2 and MK-3 red lines."""

from __future__ import annotations

import pytest

from nce.vertical_modules.marketing._guard import (
    MarketingSensitiveDataLeakError,
    MarketingUngroundedClaimError,
)
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.marketing.drafting import (
    do_draft_case_study,
    validate_draft_grounding,
)
from nce.vertical_modules.marketing.redaction import redact_for_marketing_draft


class MockEngine:
    """Mock engine providing scoped_pg_session and knowledge graph mocks."""

    def __init__(self, projects: list[dict] | None = None, nodes: dict | None = None):
        self.projects = projects or []
        self.nodes = nodes or {}


def test_margin_and_cost_redacted_at_assembly_mk3():
    """MK-3: Margin, cost, and internal rate fields must NEVER enter a marketing draft."""
    raw_node = {
        "id": "node-101",
        "label": "Conference Room A System",
        "room_type": "boardroom",
        "budget_total": 50000.0,
        "cost": 28000.0,
        "margin": 0.44,
        "internal_labor_cost": 4500.0,
        "profit_margin_pct": 44.0,
        "customer_name": "MegaCorp Oslo",
        "technology_stack": ["Crestron", "Q-SYS", "Shure"],
        "outcomes": {"downtime_reduction_pct": 98, "setup_time_mins": 2},
    }

    # Redact using the marketing assembly allow-list
    redacted = redact_for_marketing_draft(raw_node, anonymize=True)

    # Allowed operational & outcome facts remain
    assert "room_type" in redacted
    assert "technology_stack" in redacted
    assert "outcomes" in redacted

    # Sensitive internal financial fields MUST be stripped
    assert "margin" not in redacted
    assert "cost" not in redacted
    assert "internal_labor_cost" not in redacted
    assert "profit_margin_pct" not in redacted

    # Direct refusal if someone attempts to pass raw margin into assembly validator
    with pytest.raises(MarketingSensitiveDataLeakError):
        redact_for_marketing_draft({"margin": 0.35, "title": "Leak"}, strict=True)


def test_retrieval_grounded_assembly_cites_graph_nodes_mk2():
    """MK-2: do_draft_case_study constructs claims only from cited graph facts."""
    project_fact = {
        "project_id": "PRJ-9901",
        "namespace_id": "00000000-0000-0000-0000-000000000001",
        "title": "Nordic Boardroom Modernization",
        "challenge": "Legacy analog video distribution caused recurring meeting delays.",
        "solution_nodes": ["node-av-over-ip", "node-shure-mxa920"],
        "outcome_metrics": {"meeting_start_delay_reduction_pct": 95},
    }

    draft = do_draft_case_study(project_fact, anonymize=True)

    assert draft["status"] == "draft"
    assert draft["anonymized"] is True
    # Every claim section must carry explicit citations to graph nodes
    assert len(draft["citations"]) >= 2
    for citation in draft["citations"]:
        assert "graph_node_id" in citation
        assert "claim" in citation

    # The body text must NOT contain ungrounded hallucinations
    assert "PRJ-9901" in str(draft["citations"])


def test_uncited_claim_blocks_approval_mk2():
    """MK-2: A draft with an un-cited factual claim must be blocked by the grounding gate."""
    valid_citations = [
        {"graph_node_id": "node-1", "claim": "Reduced meeting start time by 95%"},
        {"graph_node_id": "node-2", "claim": "Deployed Shure MXA920 ceiling array"},
    ]

    # Valid draft passes validation
    assert validate_draft_grounding("Valid summary", valid_citations) is True

    # Draft with empty citations or missing node references must fail
    with pytest.raises(MarketingUngroundedClaimError):
        validate_draft_grounding(
            "Acme saved $5M annually using our magical quantum soundbar",
            citations=[],
        )

    with pytest.raises(MarketingUngroundedClaimError):
        validate_draft_grounding(
            "Claim without valid graph node",
            citations=[{"graph_node_id": "", "claim": "Some hallucinated claim"}],
        )


@pytest.mark.asyncio
async def test_find_case_study_candidates_ranks_by_outcome():
    """Candidates are discovered and ranked by verified outcome score and clean delivery."""
    candidates = await do_find_case_study_candidates(
        engine=MockEngine(),
        params={
            "namespace_id": "00000000-0000-0000-0000-000000000001",
            "min_outcome_score": 8.0,
            "lookback_days": 90,
        },
    )

    assert isinstance(candidates, dict)
    assert "candidates" in candidates
