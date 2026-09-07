"""
tests/unit/test_business_insights_ask_ratchet.py
================================================
Ratchet test suite for Wave C-BI2: Real Data Behind the AI (Business Insights).

Verifies:
  1. Real Data Grounding: do_ask_business dynamically derives answers and provenance
     from real database snapshot rows in business_insights_kpi_snapshots.
  2. Zero Fabricated Provenance: When 0 rows are touched, provenance is strictly empty []
     and the answer explicitly states 0 rows were touched.
  3. Grounding Requirement: When require_grounding=True (or raise_if_unavailable=True),
     missing data raises BusinessInsightsDataUnavailableError naming the missing target.
  4. Real Cognitive Recall: Memory recall derives from real episodic memories in memories table.
  5. Static AST Ratchet: Scans ask.py to assert ZERO literal money amounts, percentages,
     or month counts exist in the file.
  6. Standing Positive Control (U18): Asserts the scanner catches synthetic code containing
     the historical fabricated strings and goes RED.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.vertical_modules.business_insights._guard import (
    BusinessInsightsDataUnavailableError,
)
from nce.vertical_modules.business_insights.ask import do_ask_business

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ASK_PY_PATH = _REPO_ROOT / "nce" / "vertical_modules" / "business_insights" / "ask.py"


class MockSnapshotDb:
    """Simulated database connection returning tenant KPI snapshots and memories."""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []

    def make_pool(self) -> MagicMock:
        pool = MagicMock()
        conn = AsyncMock()

        async def fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM business_insights_kpi_snapshots" in query:
                ns_id = args[0]
                return [s for s in self.snapshots if str(s["namespace_id"]) == str(ns_id)]
            if "FROM memories" in query:
                ns_id = args[0]
                return [m for m in self.memories if str(m["namespace_id"]) == str(ns_id)]
            return []

        async def fake_execute(query: str, *args: Any) -> str:
            if "v3_cognitive_ledger" in query or "event_log" in query:
                self.audits.append({"query": query, "args": args})
            return "INSERT 0 1"

        conn.fetch = AsyncMock(side_effect=fake_fetch)
        conn.execute = AsyncMock(side_effect=fake_execute)

        class _ConnCtx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                pass

        pool.acquire = MagicMock(return_value=_ConnCtx())
        return pool


@pytest.mark.asyncio
async def test_do_ask_business_grounds_in_real_kpi_snapshots():
    """Real snapshot rows must be dynamically formatted into the answer with exact provenance."""
    db = MockSnapshotDb()
    ns_id = uuid4()
    now = datetime.now(timezone.utc)

    # Seed real measured snapshot rows (values not found anywhere in ask.py source)
    db.snapshots.append(
        {
            "id": uuid4(),
            "namespace_id": ns_id,
            "kpi_key": "economy_gross_margin_pct",
            "value": 41.75,
            "period": "2026-Q4",
            "captured_at": now,
            "source_engine": "economy",
            "business_insights_source_id": "kpi:economy_gross_margin_pct:20261231",
            "raw": "{}",
        }
    )
    db.snapshots.append(
        {
            "id": uuid4(),
            "namespace_id": ns_id,
            "kpi_key": "sales_weighted_pipeline",
            "value": 8950000.0,
            "period": "2026-Q4",
            "captured_at": now,
            "source_engine": "sales",
            "business_insights_source_id": "kpi:sales_weighted_pipeline:20261231",
            "raw": "{}",
        }
    )

    engine = MagicMock()
    engine.pg_pool = db.make_pool()

    params = {
        "namespace_id": str(ns_id),
        "principal_role": "executive",
        "question": "What is our gross margin and pipeline for Q4?",
    }

    result = await do_ask_business(engine, params)
    assert result["status"] == "ok"

    # Provenance is derived strictly from the rows read
    assert result["provenance"] == [
        "kpi:economy_gross_margin_pct:20261231",
        "kpi:sales_weighted_pipeline:20261231",
    ]

    # Answer text reflects real data from the DB row
    assert "41.75" in result["answer"]
    assert "8950000.0" in result["answer"]
    assert "economy_gross_margin_pct" in result["answer"]
    assert "sales_weighted_pipeline" in result["answer"]
    assert "2026-Q4" in result["answer"]


@pytest.mark.asyncio
async def test_do_ask_business_zero_rows_empty_provenance():
    """When 0 rows are touched and grounding is not required, provenance is [] and answer reports 0 rows."""
    db = MockSnapshotDb()
    ns_id = uuid4()

    engine = MagicMock()
    engine.pg_pool = db.make_pool()

    params = {
        "namespace_id": str(ns_id),
        "principal_role": "board",
        "question": "Show telemetry on customer retention",
    }

    result = await do_ask_business(engine, params)
    assert result["status"] == "ok"
    assert result["provenance"] == []
    assert (
        "touched 0 rows" in result["answer"].lower()
        or "no measured data" in result["answer"].lower()
    )


@pytest.mark.asyncio
async def test_do_ask_business_raises_when_grounding_required_and_data_missing():
    """When require_grounding=True, empty snapshot data raises BusinessInsightsDataUnavailableError."""
    db = MockSnapshotDb()
    ns_id = uuid4()

    engine = MagicMock()
    engine.pg_pool = db.make_pool()

    params = {
        "namespace_id": str(ns_id),
        "principal_role": "executive",
        "question": "What is our ARR and runway?",
        "require_grounding": True,
        "target_engine": "economy",
    }

    with pytest.raises(BusinessInsightsDataUnavailableError) as exc_info:
        await do_ask_business(engine, params)

    assert "unavailable" in str(exc_info.value).lower()
    assert "economy" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_do_ask_business_cognitive_recall_from_memories():
    """Episodic context must be recalled from memories table, not hardcoded strings."""
    db = MockSnapshotDb()
    ns_id = uuid4()
    now = datetime.now(timezone.utc)

    mem_id1 = uuid4()
    mem_id2 = uuid4()
    db.memories.append(
        {
            "id": mem_id1,
            "namespace_id": ns_id,
            "memory_type": "quarterly_review",
            "occurred_at": now,
        }
    )
    db.memories.append(
        {"id": mem_id2, "namespace_id": ns_id, "memory_type": "board_briefing", "occurred_at": now}
    )

    engine = MagicMock()
    engine.pg_pool = db.make_pool()

    params = {
        "namespace_id": str(ns_id),
        "principal_role": "executive",
        "question": "Have we seen periods like this?",
    }

    result = await do_ask_business(engine, params)
    recall = result["cognitive_recall"]
    assert str(mem_id1) in recall["prior_similar_periods"]
    assert str(mem_id2) in recall["prior_similar_periods"]
    assert "Recalled 2 historical episodic memories" in recall["context_note"]
    assert "2025-Q3" not in str(recall)


# ============================================================================
# 5. STATIC AST RATCHET: ZERO FORBIDDEN LITERALS IN ask.py
# ============================================================================

MONEY_PATTERN = re.compile(r"\$[0-9]+[0-9,\.]*(?:[kKMmBb])?")
PERCENTAGE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?%")
MONTH_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*months?", re.IGNORECASE)


def _scan_for_forbidden_literals(source_code: str) -> list[dict[str, Any]]:
    tree = ast.parse(source_code)
    violations: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            # Exclude SQL DDL/DML templates where $1, $2 are query placeholders
            if "SELECT " in val or "INSERT " in val or "UPDATE " in val:
                continue

            m = MONEY_PATTERN.findall(val)
            p = PERCENTAGE_PATTERN.findall(val)
            mo = MONTH_PATTERN.findall(val)
            if m or p or mo:
                violations.append(
                    {
                        "lineno": getattr(node, "lineno", 0),
                        "money": m,
                        "percentages": p,
                        "months": mo,
                        "raw": val,
                    }
                )
    return violations


def test_ask_py_has_zero_forbidden_literals():
    """Ratchet: ask.py must not contain any literal currency amounts, percentages, or month counts."""
    assert _ASK_PY_PATH.exists(), f"ask.py not found at {_ASK_PY_PATH}"
    source = _ASK_PY_PATH.read_text(encoding="utf-8")
    violations = _scan_for_forbidden_literals(source)
    assert not violations, (
        f"Found forbidden hardcoded literals in ask.py (violates Wave C-BI2): {violations}. "
        "All values must be dynamically queried from database rows."
    )


def test_positive_control_ratchet_catches_fabricated_literals():
    """Standing Positive Control (U18): Prove the scanner catches historical fabricated literals and goes RED."""
    historical_bad_code = """
def fake_ask():
    answer1 = "Current ARR stands at $5,040,000 with an estimated cash runway of 18.0 months."
    answer2 = "Operating gross margin stabilized at 38.5% in 2026-Q3."
    answer3 = "Commercial pipeline totals $12.4M across active enterprise deals, showing 28% growth."
    return [answer1, answer2, answer3]
"""
    violations = _scan_for_forbidden_literals(historical_bad_code)
    assert len(violations) == 3, f"Expected 3 distinct literal violations, found {len(violations)}"

    # Confirm each specific category was detected
    detected_money = [v["money"] for v in violations if v["money"]]
    detected_pct = [v["percentages"] for v in violations if v["percentages"]]
    detected_months = [v["months"] for v in violations if v["months"]]

    assert any("$5,040,000" in m for m in detected_money)
    assert any("$12.4M" in m for m in detected_money)
    assert any("38.5%" in p for p in detected_pct)
    assert any("28%" in p for p in detected_pct)
    assert any("18.0 months" in mo for mo in detected_months)
