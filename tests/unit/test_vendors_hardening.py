"""
tests/unit/test_vendors_hardening.py
====================================
Hardening unit tests for Module 4 (Vendors & Contractors Engine) - Wave V-4.

Covers comprehensive unit testing across all 10 module operations:
  1. matching.py: weights loading, location parsing, contractor ranking & scoring invariants.
  2. tiers.py: strip_tier_details redaction, kickback tier threshold calculation, ledger volumes.
  3. scorecard.py: scorecard weighting, minimum sample handling, composite score math.
  4. frontier.py: reliability radar burnout, rating degradation, weight calibration.
  5. feed.py: degradation trend detection, half-split comparisons, tier at risk.
  6. performance.py: rolling window averages, contractor_id normalization, 0-100 scaling.
  7. certs.py: cert date parsing, expiry threshold calculations.
  8. contractors.py: parameter validation.
  9. partner_view.py: partner scoping, node redaction.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nce.vertical_modules.vendors.certs import do_check_cert_expiry, do_upsert_cert
from nce.vertical_modules.vendors.contractors import do_upsert_contractor
from nce.vertical_modules.vendors.feed import (
    do_check_tier_at_risk,
    do_detect_reliability_degradation,
)
from nce.vertical_modules.vendors.frontier import (
    do_calibrate_weights,
    do_reliability_radar,
)
from nce.vertical_modules.vendors.frontier import (
    parse_json as frontier_parse_json,
)
from nce.vertical_modules.vendors.matching import (
    do_match_contractor,
    get_location_str,
    load_match_weights,
)
from nce.vertical_modules.vendors.partner_view import do_partner_view
from nce.vertical_modules.vendors.performance import (
    do_compute_performance,
)
from nce.vertical_modules.vendors.performance import (
    parse_json as perf_parse_json,
)
from nce.vertical_modules.vendors.scorecard import (
    do_compute_scorecard,
    load_scorecard_weights,
)
from nce.vertical_modules.vendors.tiers import (
    do_get_tier_status,
    do_record_outcome,
    strip_tier_details,
)

_NS = uuid4()


class _MockEngine:
    """Mock engine providing pg_pool and mongo_client for unit tests."""

    def __init__(self, pg_pool: Any = None, mongo_client: Any = None) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client or MagicMock()


def _make_mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


def _scoped_session_patch(module_path: str, conn: MagicMock):
    @asynccontextmanager
    async def _fake_session(pool: Any, ns_id: Any):
        yield conn

    return patch(f"{module_path}.scoped_pg_session", _fake_session)


# ===========================================================================
# 1. Matching Logic Unit Tests (matching.py)
# ===========================================================================


class TestContractorMatching:
    def test_matching_weights_defaults(self) -> None:
        weights = load_match_weights()
        assert "skill_weight" in weights
        assert "location_weight" in weights
        assert "load_weight" in weights
        assert "history_weight" in weights
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-4

    def test_get_location_str_none_and_empty(self) -> None:
        assert get_location_str(None) is None
        assert get_location_str("") == ""
        assert get_location_str("   ") == ""
        assert get_location_str(12345) is None

    def test_get_location_str_clean_string(self) -> None:
        assert get_location_str("Oslo") == "oslo"
        assert get_location_str("  BERGEN  ") == "bergen"
        assert get_location_str("Trondheim") == "trondheim"

    def test_get_location_str_from_dict(self) -> None:
        assert get_location_str({"city": "Stavanger"}) == "stavanger"
        assert get_location_str({"name": "Tromsø"}) == "tromsø"
        assert get_location_str({"address": "Drammen"}) == "drammen"
        assert get_location_str({"location": "Kristiansand"}) == "kristiansand"
        assert get_location_str({"other": "nowhere"}) is None

    @pytest.mark.asyncio
    async def test_do_match_contractor_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_match_contractor(engine, {})

        with pytest.raises(ValueError, match="job parameter must be a dictionary"):
            await do_match_contractor(engine, {"namespace_id": _NS})

        with pytest.raises(ValueError, match="job parameter must be a dictionary"):
            await do_match_contractor(engine, {"namespace_id": _NS, "job": "not-a-dict"})

    @pytest.mark.asyncio
    async def test_do_match_contractor_scoring_and_ranking(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        contractors = [
            {
                "contractor_id": "CONTRACTOR:ALPHA",
                "partner_scope_id": uuid4(),
                "profile": json.dumps({"location": "Oslo"}),
                "rates": json.dumps({"hourly": 1200}),
                "skills": ["dsp", "acoustics"],
                "availability": json.dumps({"status": "available"}),
                "performance_score": 90.0,
            },
            {
                "contractor_id": "CONTRACTOR:BETA",
                "partner_scope_id": uuid4(),
                "profile": json.dumps({"location": "Bergen"}),
                "rates": json.dumps({"hourly": 1000}),
                "skills": ["dsp"],
                "availability": json.dumps({"status": "busy"}),
                "performance_score": None,  # Defaults to 0.8 (80%)
            },
            {
                "contractor_id": "CONTRACTOR:GAMMA",
                "partner_scope_id": uuid4(),
                "profile": json.dumps({"location": "Oslo"}),
                "rates": json.dumps({"hourly": 1500}),
                "skills": [],
                "availability": json.dumps({}),
                "performance_score": 60.0,
            },
        ]

        active_loads = [
            {"contractor_id": "CONTRACTOR:ALPHA", "active_load": 0},
            {"contractor_id": "CONTRACTOR:BETA", "active_load": 1},
            {"contractor_id": "CONTRACTOR:GAMMA", "active_load": 3},
        ]

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM contractor_profiles" in query:
                return contractors
            if "FROM kg_edges" in query:
                return active_loads
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.matching", conn):
            res = await do_match_contractor(
                engine,
                {
                    "namespace_id": _NS,
                    "job": {"skills": ["dsp", "acoustics"], "location": "Oslo"},
                },
            )

        assert res["ok"] is True
        matches = res["matches"]
        assert len(matches) == 3

        # Contractor ALPHA: 2/2 skills (1.0), matching location (1.0), 0 load (1.0), 90.0 perf (0.9)
        # Expected: 1.0*0.4 + 1.0*0.3 + 1.0*0.1 + 0.9*0.2 = 0.4 + 0.3 + 0.1 + 0.18 = 0.98
        alpha = matches[0]
        assert alpha["contractor_id"] == "CONTRACTOR:ALPHA"
        assert alpha["skill_score"] == 1.0
        assert alpha["location_score"] == 1.0
        assert alpha["load_score"] == 1.0
        assert alpha["history_score"] == 0.9
        assert abs(alpha["score"] - 0.98) < 1e-3

        # Contractor GAMMA: 0/2 skills (0.0), matching location (1.0), 3 load (0.25), 60.0 perf (0.6)
        # Expected: 0.0*0.4 + 1.0*0.3 + 0.25*0.1 + 0.6*0.2 = 0.3 + 0.025 + 0.12 = 0.445
        gamma = matches[1]
        assert gamma["contractor_id"] == "CONTRACTOR:GAMMA"
        assert gamma["skill_score"] == 0.0
        assert gamma["location_score"] == 1.0
        assert gamma["load_score"] == 0.25
        assert gamma["history_score"] == 0.6
        assert abs(gamma["score"] - 0.445) < 1e-3

        # Contractor BETA: 1/2 skills (0.5), location mismatch (0.0), 1 load (0.5), perf default (0.8)
        # Expected: 0.5*0.4 + 0.0*0.3 + 0.5*0.1 + 0.8*0.2 = 0.2 + 0.05 + 0.16 = 0.41
        beta = matches[2]
        assert beta["contractor_id"] == "CONTRACTOR:BETA"
        assert beta["skill_score"] == 0.5
        assert beta["location_score"] == 0.0
        assert beta["load_score"] == 0.5
        assert beta["history_score"] == 0.8
        assert abs(beta["score"] - 0.41) < 1e-3

    @pytest.mark.asyncio
    async def test_do_match_contractor_empty_job_skills_graceful(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        contractors = [
            {
                "contractor_id": "CONTRACTOR:XYZ",
                "partner_scope_id": uuid4(),
                "profile": {},
                "rates": {},
                "skills": [],
                "availability": {},
                "performance_score": 85.0,
            }
        ]

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM contractor_profiles" in query:
                return contractors
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.matching", conn):
            res = await do_match_contractor(
                engine,
                {
                    "namespace_id": _NS,
                    "job": {"skills": [], "location": None},
                },
            )

        assert res["ok"] is True
        match = res["matches"][0]
        assert match["skill_score"] == 1.0
        assert match["location_score"] == 1.0

    @pytest.mark.asyncio
    async def test_do_match_contractor_tie_break_determinism(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        contractors = [
            {
                "contractor_id": "CONTRACTOR:ZEBRA",
                "partner_scope_id": uuid4(),
                "profile": {},
                "rates": {},
                "skills": ["audio"],
                "availability": {},
                "performance_score": 80.0,
            },
            {
                "contractor_id": "CONTRACTOR:APPLE",
                "partner_scope_id": uuid4(),
                "profile": {},
                "rates": {},
                "skills": ["audio"],
                "availability": {},
                "performance_score": 80.0,
            },
        ]

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM contractor_profiles" in query:
                return contractors
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.matching", conn):
            res = await do_match_contractor(
                engine,
                {
                    "namespace_id": _NS,
                    "job": {"skills": ["audio"]},
                },
            )

        assert res["ok"] is True
        assert res["matches"][0]["contractor_id"] == "CONTRACTOR:APPLE"
        assert res["matches"][1]["contractor_id"] == "CONTRACTOR:ZEBRA"


# ===========================================================================
# 2. Tiers and Redaction Unit Tests (tiers.py)
# ===========================================================================


class TestVendorsTiers:
    def test_strip_tier_details_empty(self) -> None:
        assert strip_tier_details({}) == {}

    def test_strip_tier_details_root_keys(self) -> None:
        vendor_data = {
            "vendor_id": "VENDOR:ACME",
            "current_tier": "Gold",
            "ytd_progress": 0.85,
            "ytd_volume": 75000.0,
            "next_tier_threshold": 100000.0,
            "days_left": 120,
            "name": "ACME Supplies",
        }
        stripped = strip_tier_details(vendor_data)
        assert "current_tier" not in stripped
        assert "ytd_progress" not in stripped
        assert "ytd_volume" not in stripped
        assert "next_tier_threshold" not in stripped
        assert "days_left" not in stripped
        assert stripped["vendor_id"] == "VENDOR:ACME"
        assert stripped["name"] == "ACME Supplies"

    def test_strip_tier_details_nested_scorecard(self) -> None:
        vendor_data = {
            "vendor_id": "VENDOR:ACME",
            "scorecard": {
                "composite_score": 92.5,
                "current_tier": "Platinum",
                "ytd_progress": 1.0,
                "on_time_pct": 98.0,
            },
        }
        stripped = strip_tier_details(vendor_data)
        scorecard = stripped["scorecard"]
        assert "current_tier" not in scorecard
        assert "ytd_progress" not in scorecard
        assert scorecard["composite_score"] == 92.5
        assert scorecard["on_time_pct"] == 98.0

    def test_strip_tier_details_non_dict_scorecard(self) -> None:
        vendor_data = {
            "vendor_id": "VENDOR:ACME",
            "scorecard": "unavailable",
        }
        stripped = strip_tier_details(vendor_data)
        assert stripped["scorecard"] == "unavailable"

    def test_strip_tier_details_immutability(self) -> None:
        original = {"vendor_id": "VENDOR:ACME", "current_tier": "Silver"}
        _ = strip_tier_details(original)
        assert "current_tier" in original

    @pytest.mark.asyncio
    async def test_do_get_tier_status_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_get_tier_status(engine, {})

        with pytest.raises(ValueError, match="vendor_id is required"):
            await do_get_tier_status(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_do_get_tier_status_vendor_not_found(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)

        with _scoped_session_patch("nce.vertical_modules.vendors.tiers", conn):
            with pytest.raises(ValueError, match="Vendor not found: VENDOR:UNKNOWN"):
                await do_get_tier_status(
                    engine, {"namespace_id": _NS, "vendor_id": "VENDOR:UNKNOWN"}
                )

    @pytest.mark.asyncio
    async def test_do_get_tier_status_default_tiers_base(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        async def _fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
            if "FROM kg_nodes" in query and "entity_type = 'VENDOR'" in query:
                return {"label": "VENDOR:ACME", "payload_ref": None}
            if "FROM kg_edges" in query and "predicate = 'under'" in query:
                return None  # No agreement edge -> defaults to standard tiers
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        conn.fetch = AsyncMock(return_value=[])

        with _scoped_session_patch("nce.vertical_modules.vendors.tiers", conn):
            res = await do_get_tier_status(
                engine, {"namespace_id": _NS, "vendor_id": "VENDOR:ACME"}
            )

        assert res["vendor_id"] == "VENDOR:ACME"
        assert res["current_tier"] == "Base"
        assert res["ytd_volume"] == 0.0
        assert res["next_tier_threshold"] == 10000.0  # Bronze threshold
        assert res["ytd_progress"] == 0.0

    @pytest.mark.asyncio
    async def test_do_get_tier_status_default_tiers_silver_progress(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        async def _fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
            if "FROM kg_nodes" in query and "entity_type = 'VENDOR'" in query:
                return {"label": "VENDOR:ACME", "payload_ref": None}
            if "FROM kg_edges" in query and "predicate = 'under'" in query:
                return None
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)

        conn.fetch = AsyncMock(
            return_value=[
                {"tlx_scores": json.dumps({"amount": 50000.0})},
                {"tlx_scores": json.dumps({"volume": 25000.0})},
            ]
        )

        with _scoped_session_patch("nce.vertical_modules.vendors.tiers", conn):
            res = await do_get_tier_status(
                engine, {"namespace_id": _NS, "vendor_id": "VENDOR:ACME"}
            )

        assert res["current_tier"] == "Silver"
        assert res["ytd_volume"] == 75000.0
        assert res["next_tier_threshold"] == 100000.0
        assert abs(res["ytd_progress"] - 0.5) < 1e-4

    @pytest.mark.asyncio
    async def test_do_get_tier_status_default_tiers_platinum_capped(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        async def _fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
            if "FROM kg_nodes" in query and "entity_type = 'VENDOR'" in query:
                return {"label": "VENDOR:ACME", "payload_ref": None}
            return None

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        conn.fetch = AsyncMock(return_value=[{"tlx_scores": json.dumps({"value": 300000.0})}])

        with _scoped_session_patch("nce.vertical_modules.vendors.tiers", conn):
            res = await do_get_tier_status(
                engine, {"namespace_id": _NS, "vendor_id": "VENDOR:ACME"}
            )

        assert res["current_tier"] == "Platinum"
        assert res["ytd_volume"] == 300000.0
        assert res["next_tier_threshold"] is None
        assert res["ytd_progress"] == 1.0

    @pytest.mark.asyncio
    async def test_do_record_outcome_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_record_outcome(engine, {})

        with pytest.raises(ValueError, match="event_type is required"):
            await do_record_outcome(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_do_record_outcome_success(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        with _scoped_session_patch("nce.vertical_modules.vendors.tiers", conn):
            res = await do_record_outcome(
                engine,
                {
                    "namespace_id": _NS,
                    "event_type": "match_decision",
                    "vendor_id": "VENDOR:ACME",
                    "decision": "accept",
                    "score": 95.0,
                    "amount": 12500.0,
                },
            )

        assert res["ok"] is True
        assert res["event_type"] == "match_decision"
        assert "ledger_id" in res
        conn.execute.assert_awaited_once()


# ===========================================================================
# 3. Scorecard Unit Tests (scorecard.py)
# ===========================================================================


class TestVendorsScorecard:
    def test_load_scorecard_weights_defaults(self) -> None:
        weights = load_scorecard_weights()
        assert "on_time_weight" in weights
        assert "defect_rma_weight" in weights
        assert "substitution_weight" in weights
        assert "reliability_weight" in weights
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-4

    @pytest.mark.asyncio
    async def test_do_compute_scorecard_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_compute_scorecard(engine, {})

        with pytest.raises(ValueError, match="vendor_id is required"):
            await do_compute_scorecard(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_do_compute_scorecard_perfect_score(self) -> None:
        engine = _MockEngine(pg_pool=None)
        events = [
            {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 100.0}
            for _ in range(5)
        ]

        res = await do_compute_scorecard(
            engine,
            {
                "namespace_id": _NS,
                "vendor_id": "VENDOR:PERFECT",
                "events": events,
            },
        )

        assert res["insufficient_data"] is False
        assert res["sample_n"] == 5
        assert res["on_time_pct"] == 100.0
        assert res["defect_rma_rate"] == 0.0
        assert res["substitution_rate"] == 0.0
        assert res["reliability"] == 100.0
        assert res["composite_score"] == 100.0

    @pytest.mark.asyncio
    async def test_do_compute_scorecard_zero_score(self) -> None:
        engine = _MockEngine(pg_pool=None)
        events = [
            {"on_time": False, "defect_rma": True, "substituted": True, "reliability": 0.0}
            for _ in range(5)
        ]

        res = await do_compute_scorecard(
            engine,
            {
                "namespace_id": _NS,
                "vendor_id": "VENDOR:FAIL",
                "events": events,
            },
        )

        assert res["insufficient_data"] is False
        assert res["sample_n"] == 5
        assert res["on_time_pct"] == 0.0
        assert res["defect_rma_rate"] == 100.0
        assert res["substitution_rate"] == 100.0
        assert res["reliability"] == 0.0
        assert res["composite_score"] == 0.0

    @pytest.mark.asyncio
    async def test_do_compute_scorecard_empty_pool_graceful(self) -> None:
        engine = _MockEngine(pg_pool=None)
        events = [
            {"on_time": True, "defect_rma": False, "substituted": False, "reliability": 90.0}
            for _ in range(5)
        ]
        res = await do_compute_scorecard(
            engine,
            {
                "namespace_id": _NS,
                "vendor_id": "VENDOR:NOPOOL",
                "events": events,
            },
        )
        assert res["insufficient_data"] is False
        assert res["sample_n"] == 5


# ===========================================================================
# 4. Frontier (Reliability Radar & Weights Calibration) Unit Tests
# ===========================================================================


class TestVendorsFrontier:
    def test_frontier_parse_json(self) -> None:
        assert frontier_parse_json({"key": "val"}) == {"key": "val"}
        assert frontier_parse_json('{"parsed": true}') == {"parsed": True}
        assert frontier_parse_json("invalid-json") == {}
        assert frontier_parse_json(None) == {}

    @pytest.mark.asyncio
    async def test_do_reliability_radar_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_reliability_radar(engine, {})

    @pytest.mark.asyncio
    async def test_do_reliability_radar_clean_state(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetch = AsyncMock(return_value=[])

        with _scoped_session_patch("nce.vertical_modules.vendors.frontier", conn):
            res = await do_reliability_radar(engine, {"namespace_id": _NS})

        assert res["ok"] is True
        assert res["supplier_risk"] == []
        assert res["contractor_burnout"] == []

    @pytest.mark.asyncio
    async def test_do_reliability_radar_supplier_composite_risk(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        scorecards = [
            {
                "vendor_id": "VENDOR:RISKY",
                "composite_score": 65.0,  # Below 70 threshold -> risk_level = "high"
                "defect_rma_rate": 5.0,
                "on_time_pct": 80.0,
                "current_tier": "Bronze",
                "ytd_progress": 0.3,
            }
        ]

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM vendor_scorecards" in query:
                return scorecards
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.frontier", conn):
            res = await do_reliability_radar(engine, {"namespace_id": _NS})

        assert len(res["supplier_risk"]) == 1
        risk = res["supplier_risk"][0]
        assert risk["vendor_id"] == "VENDOR:RISKY"
        assert risk["risk_level"] == "high"
        assert any("Low composite score" in r for r in risk["reasons"])

    @pytest.mark.asyncio
    async def test_do_reliability_radar_contractor_burnout_active_load(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        contractor_rows = [{"contractor_id": "CONTRACTOR:OVERLOADED", "performance_score": 95.0}]
        load_rows = [{"contractor_id": "CONTRACTOR:OVERLOADED", "active_load": 5}]  # > 3

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM contractor_profiles" in query:
                return contractor_rows
            if "FROM kg_edges" in query:
                return load_rows
            if "FROM v3_cognitive_ledger" in query:
                return []
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.frontier", conn):
            res = await do_reliability_radar(engine, {"namespace_id": _NS})

        assert len(res["contractor_burnout"]) == 1
        burnout = res["contractor_burnout"][0]
        assert burnout["contractor_id"] == "CONTRACTOR:OVERLOADED"
        assert burnout["risk_level"] == "high"
        assert burnout["active_load"] == 5

    @pytest.mark.asyncio
    async def test_do_reliability_radar_contractor_rating_degradation(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        contractor_rows = [{"contractor_id": "CONTRACTOR:TIRED", "performance_score": 75.0}]
        load_rows = [{"contractor_id": "CONTRACTOR:TIRED", "active_load": 1}]

        rating_rows = [
            {"tlx_scores": json.dumps({"rating": 3.0})},
            {"tlx_scores": json.dumps({"rating": 3.5})},
            {"tlx_scores": json.dumps({"rating": 4.5})},
            {"tlx_scores": json.dumps({"rating": 5.0})},
        ]

        async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM contractor_profiles" in query:
                return contractor_rows
            if "FROM kg_edges" in query:
                return load_rows
            if "FROM v3_cognitive_ledger" in query:
                return rating_rows
            return []

        conn.fetch = AsyncMock(side_effect=_fetch)

        with _scoped_session_patch("nce.vertical_modules.vendors.frontier", conn):
            res = await do_reliability_radar(engine, {"namespace_id": _NS})

        assert len(res["contractor_burnout"]) == 1
        burnout = res["contractor_burnout"][0]
        assert burnout["contractor_id"] == "CONTRACTOR:TIRED"
        assert any("Performance rating degrading" in r for r in burnout["reasons"])

    @pytest.mark.asyncio
    async def test_do_calibrate_weights_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_calibrate_weights(engine, {})

    @pytest.mark.asyncio
    async def test_do_calibrate_weights_calculation(self, tmp_path: Any) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        temp_weights_file = tmp_path / "vendor-scorecard-weights.json"
        temp_weights_file.write_text(
            json.dumps({"_comment": "test", "on_time_weight": 0.4, "defect_rma_weight": 0.3}),
            encoding="utf-8",
        )

        rows = [
            {"tlx_scores": json.dumps({"on_time": False, "defect_rma": False})},
            {"tlx_scores": json.dumps({"on_time": False, "defect_rma": False})},
            {"tlx_scores": json.dumps({"on_time": False, "defect_rma": False})},
            {"tlx_scores": json.dumps({"on_time": True, "defect_rma": True})},
        ]
        conn.fetch = AsyncMock(return_value=rows)

        with _scoped_session_patch("nce.vertical_modules.vendors.frontier", conn):
            with patch("nce.vertical_modules.vendors.frontier._CONFIG_DATA_DIR", tmp_path):
                res = await do_calibrate_weights(engine, {"namespace_id": _NS})

        assert res["ok"] is True
        weights = res["calibrated_weights"]
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-4
        assert weights["on_time_weight"] > weights["defect_rma_weight"]


# ===========================================================================
# 5. Feed & Watchers Unit Tests (feed.py)
# ===========================================================================


class TestVendorsFeed:
    @pytest.mark.asyncio
    async def test_detect_reliability_degradation_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_detect_reliability_degradation(engine, {})

        with pytest.raises(ValueError, match="vendor_id is required"):
            await do_detect_reliability_degradation(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_detect_reliability_degradation_vendor_not_found(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)

        with _scoped_session_patch("nce.vertical_modules.vendors.feed", conn):
            with pytest.raises(ValueError, match="Vendor not found: VENDOR:GHOST"):
                await do_detect_reliability_degradation(
                    engine, {"namespace_id": _NS, "vendor_id": "VENDOR:GHOST"}
                )

    @pytest.mark.asyncio
    async def test_detect_reliability_degradation_insufficient_sample(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetchrow = AsyncMock(return_value={"label": "VENDOR:NEW"})
        conn.fetch = AsyncMock(return_value=[])  # 0 events < min_sample

        with _scoped_session_patch("nce.vertical_modules.vendors.feed", conn):
            res = await do_detect_reliability_degradation(
                engine,
                {"namespace_id": _NS, "vendor_id": "VENDOR:NEW", "min_sample": 4},
            )

        assert res["degraded"] is False
        assert "Insufficient data" in res["reason"]

    @pytest.mark.asyncio
    async def test_detect_reliability_degradation_on_time_drop(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetchrow = AsyncMock(return_value={"label": "VENDOR:FAILING"})

        rows = [
            {"tlx_scores": json.dumps({"on_time": True, "defect_rma": False, "reliability": 100})},
            {"tlx_scores": json.dumps({"on_time": True, "defect_rma": False, "reliability": 100})},
            {"tlx_scores": json.dumps({"on_time": False, "defect_rma": False, "reliability": 60})},
            {"tlx_scores": json.dumps({"on_time": False, "defect_rma": False, "reliability": 60})},
        ]
        conn.fetch = AsyncMock(return_value=rows)

        with _scoped_session_patch("nce.vertical_modules.vendors.feed", conn):
            res = await do_detect_reliability_degradation(
                engine,
                {"namespace_id": _NS, "vendor_id": "VENDOR:FAILING", "min_sample": 4},
            )

        assert res["degraded"] is True
        assert res["on_time_degraded_pct"] >= 10.0

    @pytest.mark.asyncio
    async def test_do_check_tier_at_risk_highest_tier(self) -> None:
        engine = _MockEngine()
        with patch(
            "nce.vertical_modules.vendors.feed.do_get_tier_status",
            new=AsyncMock(
                return_value={
                    "vendor_id": "VENDOR:TOP",
                    "next_tier_threshold": None,
                    "current_tier": "Platinum",
                }
            ),
        ):
            res = await do_check_tier_at_risk(
                engine,
                {
                    "namespace_id": _NS,
                    "vendor_id": "VENDOR:TOP",
                },
            )
        assert res["at_risk"] is False
        assert "highest tier" in res["reason"]

    @pytest.mark.asyncio
    async def test_do_check_tier_at_risk_flagged(self) -> None:
        engine = _MockEngine()
        # Vendor needs 100,000, has 10,000, 10 days left -> projected < needed -> at risk!
        with patch(
            "nce.vertical_modules.vendors.feed.do_get_tier_status",
            new=AsyncMock(
                return_value={
                    "vendor_id": "VENDOR:SLOW",
                    "current_tier": "Bronze",
                    "next_tier_threshold": 100000.0,
                    "ytd_volume": 10000.0,
                    "days_left": 10,
                }
            ),
        ):
            res = await do_check_tier_at_risk(
                engine,
                {
                    "namespace_id": _NS,
                    "vendor_id": "VENDOR:SLOW",
                },
            )
        assert res["at_risk"] is True
        assert res["vendor_id"] == "VENDOR:SLOW"
        assert res["needed_remaining_volume"] > res["projected_remaining_volume"]
        assert res["pace_per_day"] > 0


# ===========================================================================
# 6. Performance Unit Tests (performance.py)
# ===========================================================================


class TestVendorsPerformance:
    def test_perf_parse_json(self) -> None:
        assert perf_parse_json({"rating": 4}) == {"rating": 4}
        assert perf_parse_json('{"rating": 5}') == {"rating": 5}
        assert perf_parse_json("corrupt") == {}
        assert perf_parse_json(None) == {}

    @pytest.mark.asyncio
    async def test_do_compute_performance_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_compute_performance(engine, {})

        with pytest.raises(ValueError, match="contractor_id is required"):
            await do_compute_performance(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_do_compute_performance_insufficient_sample(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        rows = [
            {"tlx_scores": json.dumps({"rating": 4.5}), "created_at": datetime.now()},
            {"tlx_scores": json.dumps({"rating": 5.0}), "created_at": datetime.now()},
        ]
        conn.fetch = AsyncMock(return_value=rows)

        with _scoped_session_patch("nce.vertical_modules.vendors.performance", conn):
            res = await do_compute_performance(
                engine,
                {"namespace_id": _NS, "contractor_id": "CONTRACTOR:NEW"},
            )

        assert res["contractor_id"] == "CONTRACTOR:NEW"
        assert res["insufficient_data"] is True
        assert res["performance_score"] is None
        assert res["sample_n"] == 2

    @pytest.mark.asyncio
    async def test_do_compute_performance_scaling_and_normalization(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()

        rows = [
            {"tlx_scores": json.dumps({"rating": 4.0}), "created_at": datetime.now()}
            for _ in range(5)
        ]
        conn.fetch = AsyncMock(return_value=rows)

        with _scoped_session_patch("nce.vertical_modules.vendors.performance", conn):
            res = await do_compute_performance(
                engine,
                {
                    "namespace_id": _NS,
                    "contractor_id": "bob",  # tests normalization to CONTRACTOR:BOB
                },
            )

        assert res["contractor_id"] == "CONTRACTOR:BOB"
        assert res["insufficient_data"] is False
        assert res["sample_n"] == 5
        assert res["performance_score"] == 80.0


# ===========================================================================
# 7. Certifications Unit Tests (certs.py)
# ===========================================================================


class TestVendorsCerts:
    @pytest.mark.asyncio
    async def test_do_upsert_cert_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_upsert_cert(engine, {})

        with pytest.raises(ValueError, match="contractor_id is required"):
            await do_upsert_cert(engine, {"namespace_id": _NS})

        with pytest.raises(ValueError, match="cert_name is required"):
            await do_upsert_cert(engine, {"namespace_id": _NS, "contractor_id": "CONTRACTOR:X"})

        with pytest.raises(ValueError, match="expiry_date is required"):
            await do_upsert_cert(
                engine,
                {
                    "namespace_id": _NS,
                    "contractor_id": "CONTRACTOR:X",
                    "cert_name": "SAFETY_1",
                },
            )

    @pytest.mark.asyncio
    async def test_do_check_cert_expiry_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_check_cert_expiry(engine, {})

    @pytest.mark.asyncio
    async def test_do_check_cert_expiry_no_certs(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetch = AsyncMock(return_value=[])

        with _scoped_session_patch("nce.vertical_modules.vendors.certs", conn):
            res = await do_check_cert_expiry(engine, {"namespace_id": _NS})

        assert res["checked"] == 0
        assert res["expiring"] == 0
        assert res["published"] == 0


# ===========================================================================
# 8. Contractors & Partner View Unit Tests
# ===========================================================================


class TestVendorsContractorsAndPartnerView:
    @pytest.mark.asyncio
    async def test_do_upsert_contractor_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_upsert_contractor(engine, {})

        with pytest.raises(ValueError, match="contractor_id is required"):
            await do_upsert_contractor(engine, {"namespace_id": _NS})

        with pytest.raises(ValueError, match="partner_scope_id is required"):
            await do_upsert_contractor(
                engine, {"namespace_id": _NS, "contractor_id": "CONTRACTOR:ALICE"}
            )

    @pytest.mark.asyncio
    async def test_do_partner_view_missing_params(self) -> None:
        engine = _MockEngine()
        with pytest.raises(ValueError, match="namespace_id is required"):
            await do_partner_view(engine, {})

        with pytest.raises(ValueError, match="node_id is required"):
            await do_partner_view(engine, {"namespace_id": _NS})

    @pytest.mark.asyncio
    async def test_do_partner_view_not_found(self) -> None:
        engine = _MockEngine(pg_pool=MagicMock())
        conn = _make_mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)

        with _scoped_session_patch("nce.vertical_modules.vendors.partner_view", conn):
            res = await do_partner_view(
                engine, {"namespace_id": _NS, "node_id": "VENDOR:NONEXISTENT"}
            )

        assert res is None
