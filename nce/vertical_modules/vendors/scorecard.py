"""
nce/vertical_modules/vendors/scorecard.py
=========================================
Vendor scorecard core (Module 4 Wave 2).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from nce.config import cfg
from nce.db_utils import scoped_pg_session

log = logging.getLogger("nce.vertical_modules.vendors.scorecard")

_CONFIG_DATA_DIR = Path(__file__).parents[3] / "nce" / "config_data"


def load_scorecard_weights() -> dict[str, float]:
    """Load default vendor scorecard weights from vendor-scorecard-weights.json."""
    weights_path = _CONFIG_DATA_DIR / "vendor-scorecard-weights.json"
    if not weights_path.exists():
        return {
            "on_time_weight": 0.4,
            "defect_rma_weight": 0.3,
            "substitution_weight": 0.1,
            "reliability_weight": 0.2,
        }
    with open(weights_path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in data.items() if not k.startswith("_")}


async def do_compute_scorecard(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compute vendor scorecard based on PO-match and GR outcome events.

    Pure math reducer logic inside, database persistence after math logic.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    vendor_id = params.get("vendor_id")
    if not vendor_id:
        raise ValueError("vendor_id is required")

    events = params.get("events", [])
    sample_n = len(events)

    weights = load_scorecard_weights()
    min_sample = getattr(cfg, "NCE_VENDORS_SCORECARD_MIN_SAMPLE", 5)

    if sample_n < min_sample:
        result = {
            "vendor_id": vendor_id,
            "on_time_pct": None,
            "defect_rma_rate": None,
            "substitution_rate": None,
            "reliability": None,
            "composite_score": None,
            "sample_n": sample_n,
            "insufficient_data": True,
            "current_tier": params.get("current_tier"),
            "ytd_progress": params.get("ytd_progress"),
        }
    else:
        on_time_count = sum(1 for e in events if e.get("on_time") is True or e.get("on_time") == 1)
        defect_count = sum(
            1 for e in events if e.get("defect_rma") is True or e.get("defect_rma") == 1
        )
        sub_count = sum(
            1 for e in events if e.get("substituted") is True or e.get("substituted") == 1
        )

        on_time_pct = (on_time_count / sample_n) * 100.0
        defect_rma_rate = (defect_count / sample_n) * 100.0
        substitution_rate = (sub_count / sample_n) * 100.0

        rel_sum = sum(float(e.get("reliability", 100.0)) for e in events)
        reliability = rel_sum / sample_n

        on_time_score = on_time_pct
        defect_rma_score = 100.0 - defect_rma_rate
        substitution_score = 100.0 - substitution_rate
        reliability_score = reliability

        composite_score = (
            on_time_score * weights.get("on_time_weight", 0.4)
            + defect_rma_score * weights.get("defect_rma_weight", 0.3)
            + substitution_score * weights.get("substitution_weight", 0.1)
            + reliability_score * weights.get("reliability_weight", 0.2)
        )

        result = {
            "vendor_id": vendor_id,
            "on_time_pct": on_time_pct,
            "defect_rma_rate": defect_rma_rate,
            "substitution_rate": substitution_rate,
            "reliability": reliability,
            "composite_score": composite_score,
            "sample_n": sample_n,
            "insufficient_data": False,
            "current_tier": params.get("current_tier"),
            "ytd_progress": params.get("ytd_progress"),
        }

    if hasattr(engine, "pg_pool") and engine.pg_pool:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            await conn.execute(
                """
                INSERT INTO vendor_scorecards (
                    vendor_id, namespace_id, on_time_pct, defect_rma_rate,
                    substitution_rate, reliability, current_tier, ytd_progress,
                    sample_n, raw, computed_at
                ) VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
                ON CONFLICT (vendor_id, namespace_id) DO UPDATE SET
                    on_time_pct = EXCLUDED.on_time_pct,
                    defect_rma_rate = EXCLUDED.defect_rma_rate,
                    substitution_rate = EXCLUDED.substitution_rate,
                    reliability = EXCLUDED.reliability,
                    current_tier = COALESCE(EXCLUDED.current_tier, vendor_scorecards.current_tier),
                    ytd_progress = COALESCE(EXCLUDED.ytd_progress, vendor_scorecards.ytd_progress),
                    sample_n = EXCLUDED.sample_n,
                    raw = EXCLUDED.raw,
                    computed_at = NOW()
                """,
                vendor_id,
                ns_uuid,
                result["on_time_pct"],
                result["defect_rma_rate"],
                result["substitution_rate"],
                result["reliability"],
                result["current_tier"],
                result["ytd_progress"],
                result["sample_n"],
                json.dumps(result),
            )

    return result
