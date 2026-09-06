"""
tests/unit/test_trust_dial_t1_ratchet.py
========================================
Ratchet test suite for Wave 6: T-1 The Trust Dial (Roadmap Capstone).

Verifies the three canonical constraints:
1. Proposes, never raises itself:
   - Promotion proposed (escalation_required=True, effective_tier held at current).
   - Only human (set_tenant_autonomy_tier) can raise the tier.
   - Demotion on degraded precision is automatic.
2. Show coverage, not just a score:
   - Minimum sample size (>=5) required to propose tier change.
   - Coverage string includes decision count and precision percentage.
   - Insufficient signal and degradation logged to degradation register.
3. EU-AI-Act transparency (Copper ADR-0008):
   - Full inspectable audit trail, decision breakdown, and oversight mode.
4. Tool surface:
   - trust_dial_get_status (cacheable) and trust_dial_set_tier (mutation, admin_only).
5. Integration:
   - System design design proposal includes trust dial badge beside recommendation.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.tool_registry import (
    ADMIN_ONLY_TOOLS,
    CACHEABLE_TOOLS,
    MUTATION_TOOLS,
    TOOL_REGISTRY,
)
from nce.trust_dial import (
    MIN_SAMPLE_SIZE,
    evaluate_tier_proposal,
    get_trust_dial_status,
    set_tenant_autonomy_tier,
)

# ===========================================================================
# 1. Trust Dial Tool Registration & Surface
# ===========================================================================


def test_trust_dial_tools_registered():
    """Verify trust_dial_get_status and trust_dial_set_tier are in TOOL_REGISTRY with correct flags."""
    assert "trust_dial_get_status" in TOOL_REGISTRY
    assert "trust_dial_set_tier" in TOOL_REGISTRY

    get_spec = TOOL_REGISTRY["trust_dial_get_status"]
    assert get_spec.cacheable is True
    assert get_spec.admin_only is False
    assert get_spec.mutation is False
    assert "trust_dial_get_status" in CACHEABLE_TOOLS

    set_spec = TOOL_REGISTRY["trust_dial_set_tier"]
    assert set_spec.cacheable is False
    assert set_spec.admin_only is True
    assert set_spec.mutation is True
    assert "trust_dial_set_tier" in ADMIN_ONLY_TOOLS
    assert "trust_dial_set_tier" in MUTATION_TOOLS


# ===========================================================================
# 2. Rule 1: Proposes, Never Raises Itself & Automatic Demotion
# ===========================================================================


def test_rule1_promotion_proposes_held_at_current_tier():
    """Constraint 1: When precision qualifies for promotion, tier is PROPOSED, not raised."""
    # Current tier is 3 (Advisor + PL Review), precision is 96% over 15 samples (qualifies for Tier 1)
    proposed, effective, status, escalation_req, reasoning = evaluate_tier_proposal(
        current_tier=3,
        sample_size=15,
        precision_rate=0.96,
    )
    assert proposed == 1
    assert effective == 3  # CANNOT raise itself!
    assert status == "promotion_proposed"
    assert escalation_req is True
    assert "tenant administrator must confirm" in reasoning


def test_rule1_automatic_demotion_on_degraded_precision():
    """Constraint 1: When precision degrades, autonomy is AUTOMATICALLY lowered."""
    # Current tier is 1 (Autonomous), but precision dropped to 70% over 10 samples (falls to Tier 3)
    proposed, effective, status, escalation_req, reasoning = evaluate_tier_proposal(
        current_tier=1,
        sample_size=10,
        precision_rate=0.70,
    )
    assert proposed == 3
    assert effective == 3  # Automatically lowered!
    assert status == "demoted_due_to_degradation"
    assert escalation_req is False
    assert "automatically demoted" in reasoning


@pytest.mark.asyncio
async def test_rule1_human_explicitly_raises_tier():
    """Only human administrator can explicitly raise autonomy tier."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"metadata": json.dumps({"trust_dial": {}})}

    ns_id = uuid4()

    with patch("nce.trust_dial.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await set_tenant_autonomy_tier(
            mock_engine,
            ns_id,
            tier=1,
            engine="system_design",
            actor="compliance_officer",
        )

        assert res["status"] == "tier_updated"
        assert res["tier"] == 1
        assert res["tier_label"] == "Tier 1 (Autonomous)"
        assert res["engine"] == "system_design"
        assert res["updated_by"] == "compliance_officer"
        assert mock_conn.execute.called

        # Verify SQL update parameters
        update_args = mock_conn.execute.call_args[0]
        assert "UPDATE namespaces" in update_args[0]
        meta_saved = json.loads(update_args[1])
        assert meta_saved["trust_dial"]["system_design"]["tier"] == 1


# ===========================================================================
# 3. Rule 2: Coverage Reporting & Degradation Signals
# ===========================================================================


def test_rule2_insufficient_signal_requires_minimum_samples():
    """Constraint 2: Requires >= 5 decisions before proposing tier changes."""
    # Only 3 decisions recorded
    proposed, effective, status, escalation_req, reasoning = evaluate_tier_proposal(
        current_tier=3,
        sample_size=3,
        precision_rate=1.0,
    )
    assert proposed == 3
    assert effective == 3
    assert status == "insufficient_signal"
    assert escalation_req is False
    assert f"minimum {MIN_SAMPLE_SIZE} required" in reasoning


@pytest.mark.asyncio
async def test_rule2_insufficient_signal_emits_degradation():
    """Insufficient signal records degradation onboarding hint."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"metadata": "{}"}
    mock_conn.fetch.return_value = [
        {"decision": "accept"},
        {"decision": "accept"},
    ]  # 2 decisions (< 5)

    ns_id = uuid4()

    with (
        patch("nce.trust_dial.scoped_pg_session") as mock_scoped,
        patch("nce.trust_dial.record_degradation") as mock_deg,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        status_result = await get_trust_dial_status(mock_engine, ns_id, engine="product")

        assert status_result["status"] == "insufficient_signal"
        assert (
            "insufficient_signal: 2 decisions recorded; 5 required"
            in status_result["coverage_summary"]
        )
        mock_deg.assert_called_once()
        deg_kwargs = mock_deg.call_args[1]
        assert deg_kwargs["code"] == "insufficient_decision_feedback"
        assert "Record 3 more human decisions" in deg_kwargs["onboarding_hint"]


@pytest.mark.asyncio
async def test_rule2_coverage_summary_with_adequate_samples():
    """Adequate signal formats clear coverage summary with count and precision percentage."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "metadata": json.dumps({"trust_dial": {"global": {"tier": 3}}})
    }
    mock_conn.fetch.return_value = [{"decision": "accept"} for _ in range(13)] + [
        {"decision": "override"} for _ in range(1)
    ]  # 14 decisions, 13 accepted -> 92.9% precision

    ns_id = uuid4()

    with patch("nce.trust_dial.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        status_result = await get_trust_dial_status(mock_engine, ns_id)

        assert status_result["metrics"]["sample_size"] == 14
        assert status_result["metrics"]["precision_rate"] == pytest.approx(0.9286, abs=1e-3)
        assert status_result["proposed_tier"] == 2
        assert status_result["effective_tier"] == 3  # cannot self-raise!
        assert (
            "Tier 2 proposed — from 14 recorded decisions (92.9% precision)"
            in status_result["coverage_summary"]
        )


# ===========================================================================
# 4. Rule 3: EU-AI-Act Explainability & Oversight Mode
# ===========================================================================


@pytest.mark.asyncio
async def test_rule3_eu_ai_act_compliance_structure():
    """Constraint 3: Inspectable reasoning, article citation, and human oversight mode."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"metadata": "{}"}
    mock_conn.fetch.return_value = [{"decision": "held"} for _ in range(6)]

    ns_id = uuid4()

    with patch("nce.trust_dial.scoped_pg_session") as mock_scoped:
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await get_trust_dial_status(mock_engine, ns_id, engine="resources")

        compliance = res["eu_ai_act_compliance"]
        assert "Article 14" in compliance["article"]
        assert compliance["transparency_level"] == "high"
        assert compliance["human_oversight_mode"] in ("human_in_the_loop", "human_on_the_loop")
        assert compliance["override_history_verified"] is True
        assert "Empirical precision" in compliance["inspectable_reasoning"]


# ===========================================================================
# 5. Integration: System Design Proposal includes Trust Dial Badge
# ===========================================================================


@pytest.mark.asyncio
async def test_system_design_propose_embeds_trust_dial_badge():
    """Wave T-1: do_propose_design includes trust_dial badge in returned proposal payload."""
    from nce.vertical_modules.system_design.propose import do_propose_design

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []
    mock_conn.fetchrow.return_value = {"metadata": "{}"}

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.0] * 1536),
        ),
        patch(
            "nce.trust_dial.get_trust_dial_status",
            new=AsyncMock(return_value={"effective_tier": 2, "status": "tier_confirmed"}),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(uuid4()),
                "room_brief": "Executive boardroom with CS-800",
            },
        )

        assert "trust_dial" in res
        assert res["trust_dial"] is not None
        assert res["trust_dial"]["effective_tier"] == 2
