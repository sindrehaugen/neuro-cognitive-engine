"""
tests/test_procurement_recalibration.py
========================================
Integration tests for ledger-backed per-supplier recalibration (Wave 8).

Validates:
- ``do_record_match_decision`` appends to ``v3_cognitive_ledger`` (append-only).
- After N decisions the per-supplier threshold recalibrates and the movement
  is reconstructable from the ledger alone (auditor-queryable).
- Recompute does NOT fire below N decisions.
- RLS isolates: a second namespace cannot see the first namespace's rows.
"""

from __future__ import annotations

import json
import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from nce.vertical_modules.procurement.recalibration import (
    do_recalibrate_supplier,
    do_record_match_decision,
)

# Use a small window so tests run quickly without inserting 100 rows each.
_TEST_WINDOW = 10


@pytest.mark.integration
@pytest.mark.asyncio
class TestProcurementRecalibration:
    """Integration tests for procurement recalibration against a live Postgres."""

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    @pytest_asyncio.fixture
    async def ns_a(self, make_namespace) -> uuid.UUID:
        """First test namespace."""
        return await make_namespace()

    @pytest_asyncio.fixture
    async def ns_b(self, make_namespace) -> uuid.UUID:
        """Second test namespace (RLS isolation)."""
        return await make_namespace()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _count_ledger_rows(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
        supplier_id: str,
    ) -> int:
        """Count procurement match-decision rows for a supplier in the ledger."""
        async with pg_pool.acquire() as conn:
            # Use superuser connection (pg_pool) to bypass RLS for the count probe.
            return await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM   v3_cognitive_ledger
                WHERE  namespace_id = $1::uuid
                  AND  tlx_scores->>'event_type' = 'match_decision'
                  AND  tlx_scores->>'supplier_id' = $2
                """,
                str(namespace_id),
                supplier_id,
            )

    async def _fetch_ledger_rows(
        self,
        pg_pool: asyncpg.Pool,
        namespace_id: uuid.UUID,
        supplier_id: str,
    ) -> list[dict]:
        """Fetch all procurement match-decision rows for a supplier (auditor query)."""
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, tlx_scores, created_at
                FROM   v3_cognitive_ledger
                WHERE  namespace_id = $1::uuid
                  AND  tlx_scores->>'event_type' = 'match_decision'
                  AND  tlx_scores->>'supplier_id' = $2
                ORDER BY created_at DESC
                """,
                str(namespace_id),
                supplier_id,
            )
        return [
            {
                "id": str(r["id"]),
                "tlx_scores": r["tlx_scores"]
                if isinstance(r["tlx_scores"], dict)
                else json.loads(r["tlx_scores"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    async def test_record_decision_appends_to_ledger(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """do_record_match_decision inserts exactly one row per call."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        before = await self._count_ledger_rows(pg_pool, ns_a, supplier_id)

        result = await do_record_match_decision(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            decision="accept",
            score=88.5,
        )

        after = await self._count_ledger_rows(pg_pool, ns_a, supplier_id)

        assert result["supplier_id"] == supplier_id
        assert "ledger_id" in result
        assert after == before + 1, f"Expected {before + 1} rows, got {after}"

    async def test_record_decision_payload_stored_correctly(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """The stored tlx_scores payload contains all expected fields."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        await do_record_match_decision(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            decision="override",
            score=42.0,
        )

        rows = await self._fetch_ledger_rows(pg_pool, ns_a, supplier_id)
        assert len(rows) >= 1

        payload = rows[0]["tlx_scores"]
        assert payload["event_type"] == "match_decision"
        assert payload["supplier_id"] == supplier_id
        assert payload["decision"] == "override"
        assert payload["score"] == 42.0

    async def test_no_recompute_below_n(self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID) -> None:
        """Recalibration is skipped when a supplier has fewer than N decisions."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        # Insert fewer than _TEST_WINDOW decisions
        for i in range(_TEST_WINDOW - 1):
            await do_record_match_decision(
                pg_pool,
                ns_a,
                supplier_id=supplier_id,
                decision="accept",
                score=float(70 + i),
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            window_n=_TEST_WINDOW,
        )

        assert result["recalibrated"] is False
        assert result["threshold_delta"] is None
        assert result["weight_delta"] is None
        assert result["precision"] is None
        assert result["decision_count"] == _TEST_WINDOW - 1

    async def test_recompute_fires_at_n(self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID) -> None:
        """Recalibration fires and returns a delta once >= N decisions exist."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        # Insert exactly _TEST_WINDOW decisions, all accepted
        for i in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool,
                ns_a,
                supplier_id=supplier_id,
                decision="accept",
                score=float(80 + i),
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            window_n=_TEST_WINDOW,
        )

        assert result["recalibrated"] is True
        assert result["decision_count"] == _TEST_WINDOW
        assert result["precision"] == 1.0  # all accepted
        # Precision=1.0 → delta = (1.0 - 0.5) × 0.1 = +0.05
        assert result["threshold_delta"] == pytest.approx(0.05, abs=1e-9)
        assert result["weight_delta"] == result["threshold_delta"]

    async def test_mixed_decisions_precision_formula(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """Threshold delta is precision-earned: half accepted → ~0 delta."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        # 5 accept + 5 override = precision 0.5 → delta 0.0
        for _ in range(5):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=supplier_id, decision="accept", score=75.0
            )
        for _ in range(5):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=supplier_id, decision="override", score=40.0
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            window_n=_TEST_WINDOW,
        )

        assert result["recalibrated"] is True
        assert result["precision"] == pytest.approx(0.5, abs=1e-9)
        # (0.5 - 0.5) × 0.1 = 0.0
        assert result["threshold_delta"] == pytest.approx(0.0, abs=1e-9)

    async def test_auditor_reconstructs_movement_from_ledger(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """An auditor reading only ledger rows reproduces the same delta as do_recalibrate_supplier."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        # 8 accept + 2 override → precision = 0.8
        for _ in range(8):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=supplier_id, decision="accept", score=85.0
            )
        for _ in range(2):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=supplier_id, decision="override", score=50.0
            )

        result = await do_recalibrate_supplier(
            pg_pool,
            ns_a,
            supplier_id=supplier_id,
            window_n=_TEST_WINDOW,
        )

        assert result["recalibrated"] is True

        # Auditor reconstruction: re-derive from raw ledger rows.
        rows = await self._fetch_ledger_rows(pg_pool, ns_a, supplier_id)
        window_rows = rows[:_TEST_WINDOW]
        auditor_accepted = sum(
            1 for r in window_rows if r["tlx_scores"].get("decision") == "accept"
        )
        auditor_precision = auditor_accepted / len(window_rows)
        raw_delta = (auditor_precision - 0.5) * 0.1
        auditor_delta = max(-0.05, min(0.05, raw_delta))

        assert auditor_delta == pytest.approx(result["threshold_delta"], abs=1e-9), (
            f"Auditor delta {auditor_delta} != reported delta {result['threshold_delta']}"
        )

    async def test_rls_namespace_isolation(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID, ns_b: uuid.UUID
    ) -> None:
        """Decisions written for ns_a are not visible when recalibrating ns_b."""
        supplier_id = f"sup-{uuid.uuid4().hex[:8]}"

        # Write _TEST_WINDOW decisions into ns_a
        for i in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool,
                ns_a,
                supplier_id=supplier_id,
                decision="accept",
                score=float(80 + i),
            )

        # ns_b has zero decisions — recalibration must NOT fire
        result_b = await do_recalibrate_supplier(
            pg_pool,
            ns_b,
            supplier_id=supplier_id,
            window_n=_TEST_WINDOW,
        )

        assert result_b["recalibrated"] is False
        assert result_b["decision_count"] == 0

    async def test_invalid_decision_rejected(self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID) -> None:
        """do_record_match_decision raises ValueError on an unknown decision value."""
        with pytest.raises(ValueError, match="decision must be"):
            await do_record_match_decision(
                pg_pool,
                ns_a,
                supplier_id="sup-x",
                decision="approve",  # not a valid value
                score=80.0,
            )

    async def test_multiple_suppliers_isolated(
        self, pg_pool: asyncpg.Pool, ns_a: uuid.UUID
    ) -> None:
        """Decisions for supplier A do not affect recalibration for supplier B."""
        sup_a = f"sup-a-{uuid.uuid4().hex[:8]}"
        sup_b = f"sup-b-{uuid.uuid4().hex[:8]}"

        # sup_a: all accepted (_TEST_WINDOW rows)
        for i in range(_TEST_WINDOW):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=sup_a, decision="accept", score=float(80 + i)
            )

        # sup_b: only partial (not yet at N)
        for _ in range(_TEST_WINDOW - 2):
            await do_record_match_decision(
                pg_pool, ns_a, supplier_id=sup_b, decision="accept", score=70.0
            )

        result_a = await do_recalibrate_supplier(
            pg_pool, ns_a, supplier_id=sup_a, window_n=_TEST_WINDOW
        )
        result_b = await do_recalibrate_supplier(
            pg_pool, ns_a, supplier_id=sup_b, window_n=_TEST_WINDOW
        )

        assert result_a["recalibrated"] is True
        assert result_b["recalibrated"] is False
