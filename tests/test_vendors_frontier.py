"""
tests/test_vendors_frontier.py
==============================
Integration tests for Batch 104 — Module 4.Wave 11 (reliability-frontier).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nce.auth import set_namespace_context
from nce.vertical_modules.vendors.frontier import do_calibrate_weights, do_reliability_radar


class MockEngine:
    def __init__(self, pg_pool: Any, mongo_client: Any = None) -> None:
        self.pg_pool = pg_pool
        self.mongo_client = mongo_client or MagicMock()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reliability_radar_supplier_risk(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify reliability radar flags high-risk suppliers correctly based on scorecards and ledger trends."""
    engine = MockEngine(pg_pool)
    vendor_low_comp = "VENDOR:ACME_LOW_COMPOSITE"
    vendor_high_defect = "VENDOR:ACME_HIGH_DEFECT"

    # 1. Seed vendor nodes in kg_nodes and scorecards in vendor_scorecards
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)

        # Insert VENDOR nodes
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'VENDOR', $2, 'agent'), ($3, 'VENDOR', $2, 'agent')
            """,
            vendor_low_comp,
            namespace_id,
            vendor_high_defect,
        )

        # Insert scorecard 1 (composite score < 70, low defect rate)
        await conn.execute(
            """
            INSERT INTO vendor_scorecards (
                vendor_id, namespace_id, on_time_pct, defect_rma_rate,
                substitution_rate, reliability, current_tier, ytd_progress, sample_n, raw
            ) VALUES (
                $1, $2::uuid, 80.0, 2.0, 5.0, 90.0, 'Bronze', 0.2, 10,
                '{"composite_score": 65.0}'::jsonb
            )
            """,
            vendor_low_comp,
            namespace_id,
        )

        # Insert scorecard 2 (composite score > 70, high defect rate)
        await conn.execute(
            """
            INSERT INTO vendor_scorecards (
                vendor_id, namespace_id, on_time_pct, defect_rma_rate,
                substitution_rate, reliability, current_tier, ytd_progress, sample_n, raw
            ) VALUES (
                $1, $2::uuid, 80.0, 15.0, 5.0, 90.0, 'Bronze', 0.2, 10,
                '{"composite_score": 75.0}'::jsonb
            )
            """,
            vendor_high_defect,
            namespace_id,
        )

    # 2. Run do_reliability_radar
    res = await do_reliability_radar(engine, {"namespace_id": namespace_id})
    assert res["ok"] is True

    suppliers = res["supplier_risk"]

    # Low composite score vendor
    flagged_low = next((s for s in suppliers if s["vendor_id"] == vendor_low_comp), None)
    assert flagged_low is not None
    assert flagged_low["risk_level"] == "high"
    assert any("Low composite score" in r for r in flagged_low["reasons"])

    # High defect rate vendor
    flagged_high = next((s for s in suppliers if s["vendor_id"] == vendor_high_defect), None)
    assert flagged_high is not None
    assert flagged_high["risk_level"] == "high"
    assert any("High defect RMA rate" in r for r in flagged_high["reasons"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reliability_radar_supplier_trend_degradation(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify reliability radar flags suppliers with degrading performance trends."""
    engine = MockEngine(pg_pool)
    vendor_id = "VENDOR:ACME_DEGRADING"

    # 1. Seed VENDOR node and healthy scorecard
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)

        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'VENDOR', $2, 'agent')
            """,
            vendor_id,
            namespace_id,
        )

        await conn.execute(
            """
            INSERT INTO vendor_scorecards (
                vendor_id, namespace_id, on_time_pct, defect_rma_rate,
                substitution_rate, reliability, current_tier, ytd_progress, sample_n, raw
            ) VALUES (
                $1, $2::uuid, 95.0, 2.0, 1.0, 98.0, 'Bronze', 0.2, 10,
                '{"composite_score": 90.0}'::jsonb
            )
            """,
            vendor_id,
            namespace_id,
        )

        # Seed 4 outcomes in ledger (ordered by created_at DESC: recent first, prior last)
        # We'll seed 2 recent bad ones (late and defect), and 2 prior good ones (on-time and clean)
        _ZERO_TENSOR = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        # Recent 1: bad
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
            VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now())
            """,
            namespace_id,
            _ZERO_TENSOR,
            json.dumps(
                {
                    "event_type": "match_decision",
                    "vendor_id": vendor_id,
                    "on_time": False,
                    "defect_rma": True,
                }
            ),
        )

        # Recent 2: bad
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
            VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now() - interval '1 minute')
            """,
            namespace_id,
            _ZERO_TENSOR,
            json.dumps(
                {
                    "event_type": "match_decision",
                    "vendor_id": vendor_id,
                    "on_time": False,
                    "defect_rma": True,
                }
            ),
        )

        # Prior 1: good
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
            VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now() - interval '2 hour')
            """,
            namespace_id,
            _ZERO_TENSOR,
            json.dumps(
                {
                    "event_type": "match_decision",
                    "vendor_id": vendor_id,
                    "on_time": True,
                    "defect_rma": False,
                }
            ),
        )

        # Prior 2: good
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
            VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now() - interval '3 hour')
            """,
            namespace_id,
            _ZERO_TENSOR,
            json.dumps(
                {
                    "event_type": "match_decision",
                    "vendor_id": vendor_id,
                    "on_time": True,
                    "defect_rma": False,
                }
            ),
        )

    # 2. Run radar
    res = await do_reliability_radar(engine, {"namespace_id": namespace_id})
    assert res["ok"] is True

    suppliers = res["supplier_risk"]
    flagged = next((s for s in suppliers if s["vendor_id"] == vendor_id), None)
    assert flagged is not None
    assert flagged["risk_level"] == "medium"
    assert any("On-time rate degrading" in r for r in flagged["reasons"])
    assert any("Defect rate increasing" in r for r in flagged["reasons"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reliability_radar_contractor_burnout_and_trend(
    pg_pool: Any,
    namespace_id: uuid.UUID,
) -> None:
    """Verify reliability radar flags contractors at risk of burnout or rating degradation."""
    engine = MockEngine(pg_pool)
    contractor_id = "CONTRACTOR:BURNED_OUT"

    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)

        # 1. Seed contractor profile
        await conn.execute(
            """
            INSERT INTO contractor_profiles (
                contractor_id, namespace_id, partner_scope_id, performance_score
            ) VALUES ($1, $2::uuid, $3::uuid, 4.5)
            """,
            contractor_id,
            namespace_id,
            uuid.uuid4(),
        )

        # 2. Seed active load (4 edges assigned_to)
        for i in range(4):
            await conn.execute(
                """
                INSERT INTO kg_edges (subject_label, predicate, object_label, namespace_id, change_origin)
                VALUES ($1, 'assigned_to', $2, $3, 'agent')
                """,
                f"JOB:{i}",
                contractor_id,
                namespace_id,
            )

        # 3. Seed rating outcomes (recent ratings 2.0 vs prior ratings 5.0)
        _ZERO_TENSOR = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        # Recent bad ratings
        for i in range(2):
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
                VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now() - interval '1 minute' * $4)
                """,
                namespace_id,
                _ZERO_TENSOR,
                json.dumps(
                    {
                        "event_type": "work_order_rating",
                        "contractor_id": contractor_id,
                        "rating": 2.0,
                    }
                ),
                i,
            )

        # Prior good ratings
        for i in range(2):
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version, created_at)
                VALUES ($1, $2::float[], $3::jsonb, 'test-1.0', now() - interval '1 hour' * ($4 + 1))
                """,
                namespace_id,
                _ZERO_TENSOR,
                json.dumps(
                    {
                        "event_type": "work_order_rating",
                        "contractor_id": contractor_id,
                        "rating": 5.0,
                    }
                ),
                i,
            )

    # 4. Run radar
    res = await do_reliability_radar(engine, {"namespace_id": namespace_id})
    assert res["ok"] is True

    contractors = res["contractor_burnout"]
    flagged = next((c for c in contractors if c["contractor_id"] == contractor_id), None)
    assert flagged is not None
    assert flagged["risk_level"] == "high"
    assert any("Excessive active load" in r for r in flagged["reasons"])
    assert any("Performance rating degrading" in r for r in flagged["reasons"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_calibrate_weights(
    pg_pool: Any,
    namespace_id: uuid.UUID,
    tmp_path: Any,
) -> None:
    """Verify dynamic weight calibration calculations and config writing."""
    engine = MockEngine(pg_pool)

    # 1. Setup temporary config directory with original weights
    initial_weights = {
        "_comment": "Test vendor scorecard weights configuration.",
        "on_time_weight": 0.40,
        "defect_rma_weight": 0.30,
        "substitution_weight": 0.10,
        "reliability_weight": 0.20,
    }

    config_dir = tmp_path / "nce" / "config_data"
    config_dir.mkdir(parents=True)
    weights_file = config_dir / "vendor-scorecard-weights.json"
    weights_file.write_text(json.dumps(initial_weights, indent=2), encoding="utf-8")

    # 2. Seed ledger outcomes with late deliveries and defects
    # Seed 3 late deliveries (on_time=False, defect_rma=False) and 1 defect (on_time=True, defect_rma=True)
    async with pg_pool.acquire() as conn:
        await set_namespace_context(conn, namespace_id)
        _ZERO_TENSOR = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        # 3 late deliveries
        for _ in range(3):
            await conn.execute(
                """
                INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version)
                VALUES ($1, $2::float[], $3::jsonb, 'test-1.0')
                """,
                namespace_id,
                _ZERO_TENSOR,
                json.dumps({"event_type": "match_decision", "on_time": False, "defect_rma": False}),
            )

        # 1 defect delivery
        await conn.execute(
            """
            INSERT INTO v3_cognitive_ledger (namespace_id, empathic_tensor, tlx_scores, model_version)
            VALUES ($1, $2::float[], $3::jsonb, 'test-1.0')
            """,
            namespace_id,
            _ZERO_TENSOR,
            json.dumps({"event_type": "match_decision", "on_time": True, "defect_rma": True}),
        )

    # 3. Patch config dir references to use our tmp_path config dir
    with (
        patch("nce.vertical_modules.vendors.frontier._CONFIG_DATA_DIR", config_dir),
        patch("nce.vertical_modules.vendors.scorecard._CONFIG_DATA_DIR", config_dir),
    ):
        # 4. Perform calibration
        res = await do_calibrate_weights(engine, {"namespace_id": namespace_id})
        assert res["ok"] is True
        assert res["total_issues"] == 4
        assert res["late_count"] == 3
        assert res["defect_count"] == 1

        calibrated = res["calibrated_weights"]

        # Calibrated weights must sum to exactly 1.0
        total_sum = sum(calibrated.values())
        assert abs(total_sum - 1.0) < 1e-9

        # Ensure the config file was overwritten
        assert weights_file.exists()
        with open(weights_file, encoding="utf-8") as f:
            updated_json = json.load(f)

        # Verify comment is preserved
        assert updated_json["_comment"] == "Test vendor scorecard weights configuration."

        # Verify the weights in file match calibration results
        assert abs(updated_json["on_time_weight"] - calibrated["on_time_weight"]) < 1e-9
        assert abs(updated_json["defect_rma_weight"] - calibrated["defect_rma_weight"]) < 1e-9
