"""
tests/unit/test_system_design_sd2_ratchet.py
============================================
Ratchet test suite for Wave C-SD2:
Real Data Behind the AI — System Design Outcome Weighting ON in Recommender.

Invariants verified:
  1. NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED defaults to True in nce.config.cfg.
  2. do_propose_design queries kg_edges with predicate 'has_outcome' for all candidate label variants.
  3. Candidates with attributed outcomes are boosted / discounted by margin drift and confidence.
  4. Coverage indicator reflects attributed outcomes:
     - "outcome-weighted: M/N attributed outcomes" when M > 0.
     - "similarity-only: 0 attributed outcomes" when M == 0.
  5. When outcome weighting is ON but 0 outcomes exist, degradation register records
     code="similarity_only_zero_outcomes" with onboarding hint "record 5 to unlock".
  6. top_k parameter passed by sales/commission.py:192 is respected and clamped to [1, 50].
  7. Per-namespace metadata override can disable outcome weighting if explicitly configured.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.config import cfg
from nce.vertical_modules.system_design.propose import (
    _apply_outcome_weights,
    do_propose_design,
)


def test_sd2_config_outcome_weighting_default_true() -> None:
    """Wave C-SD2: NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED must default to True."""
    assert cfg.NCE_SYSTEM_DESIGN_OUTCOME_WEIGHTING_ENABLED is True


def test_sd2_apply_outcome_weights_boost_and_discount() -> None:
    """Wave C-SD2: Successful projects boosted, troubled projects discounted."""
    candidates = [
        {"name": "proj-1", "similarity": 0.80, "metadata": {"project_id": "P-101"}},
        {"name": "proj-2", "similarity": 0.82, "metadata": {"project_id": "P-102"}},
    ]
    outcomes = {
        "P-101": {
            "confidence": 0.95,
            "margin_drift": 0.50,
        },  # weight factor: 1.0 + 0.5 = 1.5 -> 0.80 * 1.5 = 1.20
        "P-102": {
            "confidence": 0.80,
            "margin_drift": -0.50,
        },  # weight factor: 1.0 - 0.5 = 0.5 -> 0.82 * 0.5 = 0.41
    }

    ranked, coverage = _apply_outcome_weights(candidates, enabled=True, outcomes=outcomes)

    assert coverage["attributed_outcomes"] == 2
    assert coverage["total_candidates"] == 2
    assert coverage["status"] == "outcome-weighted: 2/2 attributed outcomes"

    # P-101 boosted above P-102
    assert ranked[0]["name"] == "proj-1"
    assert ranked[0]["weighted_score"] == pytest.approx(1.20)
    assert ranked[0]["attributed_outcome"] is True
    assert ranked[1]["name"] == "proj-2"
    assert ranked[1]["weighted_score"] == pytest.approx(0.41)
    assert ranked[1]["attributed_outcome"] is True


def test_sd2_apply_outcome_weights_zero_attributed_similarity_only() -> None:
    """Wave C-SD2: When no outcomes match, coverage indicates similarity-only."""
    candidates = [
        {"name": "proj-1", "similarity": 0.85, "metadata": {"project_id": "P-101"}},
        {"name": "proj-2", "similarity": 0.90, "metadata": {"project_id": "P-102"}},
    ]
    ranked, coverage = _apply_outcome_weights(candidates, enabled=True, outcomes={})

    assert coverage["attributed_outcomes"] == 0
    assert coverage["total_candidates"] == 2
    assert coverage["status"] == "similarity-only: 0 attributed outcomes"
    for cand in ranked:
        assert cand["attributed_outcome"] is False
        assert cand["outcome_confidence"] is None


@pytest.mark.asyncio
async def test_sd2_do_propose_design_queries_kg_edges_and_applies_weighting() -> None:
    """Wave C-SD2: do_propose_design queries kg_edges for has_outcome and weights candidates."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    ns_id = uuid4()
    proj_id = "proj-meeting-room-7"

    # 1. memories query returns 1 candidate
    cand_row = {
        "id": uuid4(),
        "name": f"PROJECT:{proj_id}",
        "payload_ref": "obj123",
        "node_type": "PROJECT",
        "metadata": json.dumps({"project_id": proj_id, "product_ref": "Shure:MXA920", "qty": 2}),
        "similarity": 0.85,
        "distance": 0.15,
    }
    # 2. namespace metadata query (no override)
    ns_row = {"metadata": json.dumps({})}
    # 3. kg_edges query returns outcome edge
    outcome_edge_row = {
        "subject_label": proj_id,
        "confidence": 0.98,
        "object_label": f"OUTCOME:{proj_id}",
    }

    mock_conn.fetch.side_effect = [
        [cand_row],  # memories
        [outcome_edge_row],  # kg_edges has_outcome
    ]
    mock_conn.fetchrow.return_value = ns_row

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.05] * 768),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "room_brief": "Executive room with ceiling microphone",
            },
        )

        assert res["outcome_weighting_applied"] is True
        assert res["coverage"]["attributed_outcomes"] == 1
        assert res["coverage"]["status"] == "outcome-weighted: 1/1 attributed outcomes"

        evidence = res["recall_evidence"]
        assert len(evidence) == 1
        assert evidence[0]["attributed_outcome"] is True
        assert evidence[0]["outcome_confidence"] == pytest.approx(0.98)

        # Verify SQL query checked kg_edges with predicate 'has_outcome'
        kg_call = next(
            (c for c in mock_conn.fetch.call_args_list if "has_outcome" in c[0][0]),
            None,
        )
        assert kg_call is not None, "do_propose_design must query kg_edges for has_outcome"
        queried_labels = kg_call[0][2]
        assert proj_id in queried_labels or f"PROJECT:{proj_id}" in queried_labels


@pytest.mark.asyncio
async def test_sd2_do_propose_design_zero_outcomes_records_degradation() -> None:
    """Wave C-SD2: When 0 outcomes exist, degradation register records onboarding hint."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    ns_id = uuid4()

    cand_row = {
        "id": uuid4(),
        "name": "Design Alpha",
        "payload_ref": "obj456",
        "node_type": "DESIGN",
        "metadata": json.dumps({"product_ref": "Barco:ClickShare-CX20", "qty": 1}),
        "similarity": 0.88,
        "distance": 0.12,
    }

    mock_conn.fetch.side_effect = [
        [cand_row],  # memories
        [],  # kg_edges has_outcome (0 rows)
    ]
    mock_conn.fetchrow.return_value = {"metadata": json.dumps({})}

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.05] * 768),
        ),
        patch("nce.degradation.record_degradation") as mock_deg,
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "room_brief": "Huddle space presentation",
            },
        )

        assert res["outcome_weighting_applied"] is False
        assert res["coverage"]["attributed_outcomes"] == 0
        assert res["coverage"]["status"] == "similarity-only: 0 attributed outcomes"

        # Verify degradation recorded
        mock_deg.assert_called_once()
        kwargs = mock_deg.call_args[1]
        assert kwargs["engine"] == "system_design"
        assert kwargs["code"] == "similarity_only_zero_outcomes"
        assert "record 5 to unlock" in kwargs["onboarding_hint"]


@pytest.mark.asyncio
async def test_sd2_top_k_parameter_from_sales_commission() -> None:
    """Wave C-SD2: top_k: 1 passed from sales/commission.py:192 is respected and clamped."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    ns_id = uuid4()

    mock_conn.fetch.side_effect = [
        [],  # memories
    ]
    mock_conn.fetchrow.return_value = {"metadata": json.dumps({})}

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.05] * 768),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        # Test top_k: 1
        await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "room_brief": "Simple room",
                "top_k": 1,
            },
        )

        call_args = mock_conn.fetch.call_args[0]
        # $4 is top_k LIMIT
        assert call_args[4] == 1


@pytest.mark.asyncio
async def test_sd2_per_namespace_override_can_disable_outcome_weighting() -> None:
    """Wave C-SD2: Tenant namespace metadata can explicitly override outcome weighting to False."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    ns_id = uuid4()

    cand_row = {
        "id": uuid4(),
        "name": "Design Beta",
        "payload_ref": "obj789",
        "node_type": "DESIGN",
        "metadata": json.dumps({"product_ref": "QSC:Core-110f", "qty": 1}),
        "similarity": 0.90,
        "distance": 0.10,
    }

    mock_conn.fetch.side_effect = [
        [cand_row],  # memories
        # outcome edges won't be queried when disabled
    ]
    mock_conn.fetchrow.return_value = {
        "metadata": json.dumps({"system_design": {"outcome_weighting_enabled": False}})
    }

    with (
        patch("nce.vertical_modules.system_design.propose.scoped_pg_session") as mock_scoped,
        patch(
            "nce.vertical_modules.system_design.propose.embed",
            new=AsyncMock(return_value=[0.05] * 768),
        ),
    ):
        mock_scoped.return_value.__aenter__.return_value = mock_conn

        res = await do_propose_design(
            mock_engine,
            {
                "namespace_id": str(ns_id),
                "room_brief": "Auditorium DSP setup",
            },
        )

        assert res["outcome_weighting_applied"] is False
        assert res["coverage"]["attributed_outcomes"] == 0
        assert res["coverage"]["status"] == "similarity-only: 0 attributed outcomes"


def test_sd2_ordering_high_conf_moderate_conf_no_data_low_conf() -> None:
    """Wave C-SD2: Attributed outcome ordering pins high-conf > mod-conf > no-data > low-conf.

    Resolves ML-orch §13 inquiry:
      - High-confidence on-budget outcome (conf=1.0, drift=0.0): factor 1.25 -> ranks #1
      - Moderate-confidence on-budget outcome (conf=0.6, drift=0.0): factor 1.05 -> ranks #2 (beats unmeasured)
      - No outcome data at all (unmeasured): factor 1.00 -> ranks #3 (neutral baseline)
      - Low-confidence on-budget outcome (conf=0.2, drift=0.0): factor 0.85 -> ranks #4 (discounted)
    """
    candidates = [
        {"name": "c_low", "similarity": 0.80, "metadata": {"project_id": "P-LOW"}},
        {"name": "c_nodata", "similarity": 0.80, "metadata": {"project_id": "P-NONE"}},
        {"name": "c_mod", "similarity": 0.80, "metadata": {"project_id": "P-MOD"}},
        {"name": "c_high", "similarity": 0.80, "metadata": {"project_id": "P-HIGH"}},
    ]
    outcomes = {
        "P-HIGH": {"confidence": 1.0, "margin_drift": 0.0},
        "P-MOD": {"confidence": 0.6, "margin_drift": 0.0},
        "P-LOW": {"confidence": 0.2, "margin_drift": 0.0},
    }

    ranked, coverage = _apply_outcome_weights(candidates, enabled=True, outcomes=outcomes)

    assert coverage["attributed_outcomes"] == 3
    assert coverage["total_candidates"] == 4
    assert coverage["status"] == "outcome-weighted: 3/4 attributed outcomes"

    # Assert exact ordering: high-conf > moderate-conf > no-data > low-conf
    assert [c["name"] for c in ranked] == ["c_high", "c_mod", "c_nodata", "c_low"]
    assert ranked[0]["weighted_score"] == pytest.approx(1.00)
    assert ranked[1]["weighted_score"] == pytest.approx(0.84)
    assert ranked[2]["weighted_score"] == pytest.approx(0.80)
    assert ranked[3]["weighted_score"] == pytest.approx(0.68)
    assert (
        ranked[0]["weighted_score"]
        > ranked[1]["weighted_score"]
        > ranked[2]["weighted_score"]
        > ranked[3]["weighted_score"]
    )
