"""
tests/test_agreements_kickback.py
===================================
Integration tests for M3.W6 — do_reconcile_kickback + get_term_change_history.

Key invariants asserted
-----------------------
1. Happy path: exact spend/earned/active/next/to_next numbers for a 3-tier
   table (mid-tier, below-first-tier earned 0, top-tier to_next None).
   Assertions are exact expected numbers — no vacuous negative controls
   (B108 lesson).
2. §9.3 gate: ``needs_review_yellow`` and ``manual_red`` rows return
   ``status="unconfirmed_terms"`` with ZERO GL seam calls and ZERO ledger
   rows — unreviewed money terms must never reconcile against real GL.
3. Supplier identity: GL rows for a DIFFERENT vendor (distinct orgnr that
   resolves to a distinct, non-None VENDOR node — proven in-test) are
   EXCLUDED from spend; asserted via exact totals (C1 discrimination).
4. gl_unavailable degrade: seam raising NotImplementedError → terms echoed,
   no earned math keys, no ledger write.
5. Term-change history: first reconcile → 1 snapshot / term_drift False;
   mutated terms → 2 snapshots / term_drift True; unchanged third run →
   still 2 snapshots; ``get_term_change_history`` returns newest-first;
   a second namespace sees none.
6. Ledger immutability discipline: the module source contains no
   UPDATE/DELETE statement against v3_cognitive_ledger.
7. Fail-closed money terms: a confirmed tier table that cannot be normalized
   without guessing (localized numerics, bools, duplicate thresholds) returns
   ``status="malformed_terms"`` with ZERO GL calls and ZERO ledger rows.
8. Fail-closed period bounds: a supplied-but-unparseable since_iso/until_iso
   raises ValueError instead of silently widening to all-time spend; GL rows
   with missing/unparseable amount_nok are skipped AND counted
   (``gl_rows_skipped``), never guessed to 0.

Seeding convention (mirrors tests/test_agreements_coverage.py)
---------------------------------------------------------------
- Seed node ownership before inserting VENDOR nodes.
- Use direct INSERTs inside scoped_pg_session for deterministic state.
- Patch the A2A GL seam via unittest.mock.AsyncMock at the module path
  WHERE KICKBACK.PY LOOKS IT UP (the symbol is imported into kickback's
  namespace): ``nce.vertical_modules.agreements.kickback._read_economy_gl_rows``
- VENDOR labels follow the agreements-module convention ``Vendor:{orgnr}``
  with short orgnr-style identifiers so pg_trgm similarity stays above the
  candidate gate before the exact suffix-match confirmation.

Test structure
--------------
All tests are ``@pytest.mark.integration`` + ``@pytest.mark.asyncio`` and
require a live Postgres via the ``pg_pool`` / ``namespace_id`` fixtures in
conftest.py.  Run with::

    set -a && source .env && set +a
    export NCE_INTEGRATION_PG_DSN="$PG_DSN"
    .venv/Scripts/python.exe -m pytest tests/test_agreements_kickback.py -q -rs

(Never set NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL — it rotates
active signing keys and is for disposable databases only; this suite passes
without it.)
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.agreements.coverage import _resolve_vendor_node_id
from nce.vertical_modules.agreements.kickback import (
    _MODEL_VERSION,
    do_reconcile_kickback,
    get_term_change_history,
)

# ---------------------------------------------------------------------------
# A2A seam patch target — where KICKBACK.PY looks the symbol up.
# ---------------------------------------------------------------------------

_SEAM = "nce.vertical_modules.agreements.kickback._read_economy_gl_rows"

# 3-tier table used across the happy-path tests.  pct is a PERCENT
# (extract.py:36 — "The rebate percentage for this tier"): 2.0 == 2 %.
_TIERS_3: list[dict[str, float]] = [
    {"threshold": 100_000.0, "pct": 2.0},
    {"threshold": 500_000.0, "pct": 3.5},
    {"threshold": 1_000_000.0, "pct": 5.0},
]


# ---------------------------------------------------------------------------
# Engine stub
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub — holds pg_pool, A2A seam is patched."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Seeding helpers (mirrors test_agreements_coverage.py)
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Seed node ownership registry for the test namespace."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _insert_vendor_node(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    label: str,
) -> uuid.UUID:
    """Insert a VENDOR node with the given label; return its UUID from kg_nodes."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'VENDOR', $2::uuid, 'agent')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            str(namespace_id),
        )
        row = await conn.fetchrow(
            "SELECT id FROM kg_nodes WHERE label = $1 AND namespace_id = $2::uuid",
            label,
            str(namespace_id),
        )
        assert row is not None, f"Vendor node not found after insert: {label}"
        return uuid.UUID(str(row["id"]))


async def _insert_agreement(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    agreement_id: uuid.UUID,
    supplier_id: str | None,
    tiers: list[dict[str, float]] | None,
    volume_commitment: float | None = None,
    review_status: str = "auto_green",
) -> None:
    """Insert (or upsert) an agreement review-queue row with kickback terms.

    Uses the nested per-field extracted shape produced by extract.py
    (``{value, extractionConfidence, reviewStatus}``).  ON CONFLICT updates
    ``extracted`` + ``review_status`` so tests can mutate terms in place.
    """
    extracted: dict[str, Any] = {}
    if supplier_id is not None:
        extracted["supplierId"] = {
            "value": supplier_id,
            "extractionConfidence": 95.0,
            "reviewStatus": "auto_green",
        }
    if tiers is not None:
        extracted["kickbackTiers"] = {
            "value": tiers,
            "extractionConfidence": 90.0,
            "reviewStatus": "needs_review_yellow",
        }
    if volume_commitment is not None:
        extracted["volumeCommitment"] = {
            "value": volume_commitment,
            "extractionConfidence": 90.0,
            "reviewStatus": "needs_review_yellow",
        }

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref,
                extraction_confidence, review_status, extracted
            ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
            ON CONFLICT (agreement_id, namespace_id) DO UPDATE
                SET extracted = EXCLUDED.extracted,
                    review_status = EXCLUDED.review_status
            """,
            agreement_id,
            str(namespace_id),
            f"sharepoint://test/{agreement_id}.pdf",
            95.0,
            review_status,
            json.dumps(extracted),
        )


async def _ledger_snapshot_count(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
) -> int:
    """Count term-snapshot ledger rows for one agreement in one namespace."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        count = await conn.fetchval(
            """
            SELECT count(*)
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  model_version = $2
              AND  tlx_scores->>'kind' = 'term_snapshot'
              AND  tlx_scores->>'agreement_id' = $3
            """,
            str(namespace_id),
            _MODEL_VERSION,
            str(agreement_id),
        )
    return int(count)


def _gl_row(supplier_id: str | None, amount_nok: float, gl_date: str = "2026-03-15") -> dict:
    return {
        "supplier_name": "Test Supplier AS",
        "supplier_id": supplier_id,
        "amount_nok": amount_nok,
        "gl_date": gl_date,
    }


# ---------------------------------------------------------------------------
# 1. Happy path — exact tier-progression numbers
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mid_tier_progression_exact_numbers(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Spend 350k on a 100k/500k/1M tier table → tier 1 active, exact math.

    spend   = 150_000 + 200_000       = 350_000.00
    earned  = 350_000 × 2.0 / 100     =   7_000.00  (pct is a PERCENT)
    to_next = 500_000 − 350_000       = 150_000.00
    drift   = 7_000 − 5_000 projection =  2_000.00
    """
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    orgnr = "912345678"
    vendor_node_id = await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    gl_rows = [
        _gl_row(orgnr, 150_000.0, "2026-02-01"),
        _gl_row(orgnr, 200_000.0, "2026-04-15"),
    ]

    engine = _EngineStub(pg_pool)
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result = await do_reconcile_kickback(
            engine,
            {
                "namespace_id": str(namespace_id),
                "agreement_id": str(agreement_id),
                "projected_kickback_nok": 5_000.0,
            },
        )

    assert result["status"] == "ok"
    assert result["agreement_id"] == str(agreement_id)
    assert result["supplier_node_id"] == str(vendor_node_id)
    assert result["spend_to_date_nok"] == 350_000.0
    assert result["earned_to_date_nok"] == 7_000.0
    assert result["active_tier"] == {"threshold": 100_000.0, "pct": 2.0}
    assert result["next_tier"] == {"threshold": 500_000.0, "pct": 3.5}
    assert result["to_next_tier_nok"] == 150_000.0
    assert result["projection_drift_nok"] == 2_000.0
    assert result["gl_rows_matched"] == 2
    assert result["term_drift"] is False

    # First successful reconcile records exactly one term snapshot.
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_first_tier_earns_zero(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Spend below the first threshold → earned 0, active None, exact to_next."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    orgnr = "913111222"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    engine = _EngineStub(pg_pool)
    with patch(_SEAM, AsyncMock(return_value=[_gl_row(orgnr, 50_000.0)])):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 50_000.0
    assert result["earned_to_date_nok"] == 0.0
    assert result["active_tier"] is None
    assert result["next_tier"] == {"threshold": 100_000.0, "pct": 2.0}
    assert result["to_next_tier_nok"] == 50_000.0
    assert result["projection_drift_nok"] is None  # no projection supplied
    assert result["gl_rows_matched"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_top_tier_has_no_next_tier(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Spend past the top threshold → top tier active, next/to_next None."""
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    orgnr = "914333444"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    engine = _EngineStub(pg_pool)
    with patch(_SEAM, AsyncMock(return_value=[_gl_row(orgnr, 1_200_000.0)])):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 1_200_000.0
    # 1_200_000 × 5.0 / 100 = 60_000 — retroactive-on-total at the top tier.
    assert result["earned_to_date_nok"] == 60_000.0
    assert result["active_tier"] == {"threshold": 1_000_000.0, "pct": 5.0}
    assert result["next_tier"] is None
    assert result["to_next_tier_nok"] is None
    assert result["gl_rows_matched"] == 1


# ---------------------------------------------------------------------------
# 2. §9.3 sign-off gate — unreviewed money terms never reconcile
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("review_status", ["needs_review_yellow", "manual_red"])
async def test_unconfirmed_terms_gate_blocks_before_gl_and_ledger(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    review_status: str,
) -> None:
    """Non-auto_green row → unconfirmed_terms, ZERO GL calls, ZERO ledger rows."""
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="915555666",
        tiers=_TIERS_3,
        review_status=review_status,
    )

    engine = _EngineStub(pg_pool)
    seam_mock = AsyncMock(return_value=[_gl_row("915555666", 999_999.0)])
    with patch(_SEAM, seam_mock):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
        )

    assert result["status"] == "unconfirmed_terms"
    assert result["agreement_id"] == str(agreement_id)
    assert result["review_status"] == review_status
    # No earned math leaked into the gate path.
    assert "earned_to_date_nok" not in result
    # The GL seam must NOT have been touched (gate runs FIRST).
    seam_mock.assert_not_called()
    # No ledger write in this path.
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_not_found_agreement(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Missing review-queue row → not_found; GL seam untouched."""
    engine = _EngineStub(pg_pool)
    seam_mock = AsyncMock(return_value=[])
    with patch(_SEAM, seam_mock):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(uuid.uuid4())},
        )

    assert result["status"] == "not_found"
    seam_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Supplier identity — C1 node discrimination, exact totals
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gl_rows_for_different_vendor_excluded_from_spend(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """GL rows for a DIFFERENT vendor node are EXCLUDED from spend.

    Non-vacuous control: vendor B's orgnr is proven to resolve to a distinct,
    non-None node BEFORE asserting exclusion — so the exclusion comes from
    node-identity mismatch, not from failed resolution.

    Exact totals: only vendor A's 100_000 counts.  If vendor B's 900_000
    leaked in, spend would be 1_000_000 → tier 3 → earned 50_000 (≠ 2_000).
    """
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    orgnr_a = "811111111"
    orgnr_b = "822222222"
    vendor_a_id = await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_a}")
    vendor_b_id = await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_b}")
    assert vendor_a_id != vendor_b_id

    # Non-vacuous: prove BOTH orgnrs resolve via C1 to their distinct nodes.
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        resolved_a = await _resolve_vendor_node_id(conn, namespace_id, raw_id=orgnr_a)
        resolved_b = await _resolve_vendor_node_id(conn, namespace_id, raw_id=orgnr_b)
    assert resolved_a == vendor_a_id, "vendor A did not resolve — test would be vacuous"
    assert resolved_b == vendor_b_id, "vendor B did not resolve — test would be vacuous"

    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr_a,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    gl_rows = [
        _gl_row(orgnr_a, 100_000.0),  # vendor A — counts
        _gl_row(orgnr_b, 900_000.0),  # vendor B — must be EXCLUDED
        _gl_row(None, 50_000.0),  # unresolvable — must be EXCLUDED
    ]

    engine = _EngineStub(pg_pool)
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
        )

    assert result["status"] == "ok"
    assert result["supplier_node_id"] == str(vendor_a_id)
    assert result["gl_rows_matched"] == 1
    assert result["spend_to_date_nok"] == 100_000.0
    # 100_000 hits tier 1 exactly (threshold <= spend): earned = 2_000.
    assert result["active_tier"] == {"threshold": 100_000.0, "pct": 2.0}
    assert result["earned_to_date_nok"] == 2_000.0


# ---------------------------------------------------------------------------
# 4. gl_unavailable graceful degrade
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gl_unavailable_degrades_without_ledger_write(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Seam NotImplementedError → gl_unavailable, terms echoed, no ledger row."""
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="916777888",
        tiers=_TIERS_3,
        volume_commitment=250_000.0,
        review_status="auto_green",
    )

    engine = _EngineStub(pg_pool)
    with patch(_SEAM, AsyncMock(side_effect=NotImplementedError)):
        result = await do_reconcile_kickback(
            engine,
            {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
        )

    assert result["status"] == "gl_unavailable"
    assert result["agreement_id"] == str(agreement_id)
    # Terms echoed (unwrapped), but NO earned math.
    assert result["terms"]["kickbackTiers"] == _TIERS_3
    assert result["terms"]["volumeCommitment"] == 250_000.0
    assert "earned_to_date_nok" not in result
    assert "spend_to_date_nok" not in result
    # No ledger write in this path.
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


# ---------------------------------------------------------------------------
# 5. Term-change history — append-only ledger snapshots
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_term_change_history_drift_and_namespace_scoping(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace,
) -> None:
    """Snapshot lifecycle: 1st reconcile records, change drifts, no-op repeats.

    Run 1 (original terms)  → 1 snapshot, term_drift False.
    Run 2 (mutated terms)   → 2 snapshots, term_drift True.
    Run 3 (unchanged terms) → still 2 snapshots, term_drift False.
    History is newest-first; a second namespace sees none.
    """
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    orgnr = "917999000"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    engine = _EngineStub(pg_pool)
    params = {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)}
    gl_rows = [_gl_row(orgnr, 150_000.0)]

    # Run 1: first reconcile — snapshot recorded, no drift (nothing prior).
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result_1 = await do_reconcile_kickback(engine, params)
    assert result_1["status"] == "ok"
    assert result_1["term_drift"] is False
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 1

    # Mutate the money terms in the DB: tier-1 pct 2.0 → 2.5.
    mutated_tiers = [
        {"threshold": 100_000.0, "pct": 2.5},
        {"threshold": 500_000.0, "pct": 3.5},
        {"threshold": 1_000_000.0, "pct": 5.0},
    ]
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=mutated_tiers,
        review_status="auto_green",
    )

    # Run 2: terms changed → new snapshot + drift flagged.
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result_2 = await do_reconcile_kickback(engine, params)
    assert result_2["status"] == "ok"
    assert result_2["term_drift"] is True
    # Mutated pct also changes the earned math: 150_000 × 2.5 / 100 = 3_750.
    assert result_2["earned_to_date_nok"] == 3_750.0
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 2

    # Run 3: unchanged terms → NO new snapshot, no drift.
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result_3 = await do_reconcile_kickback(engine, params)
    assert result_3["status"] == "ok"
    assert result_3["term_drift"] is False
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 2

    # History: newest-first, both snapshots, payloads intact.
    history = await get_term_change_history(pg_pool, namespace_id, agreement_id)
    assert len(history) == 2
    assert history[0]["agreement_id"] == str(agreement_id)
    assert history[0]["terms"]["kickbackTiers"] == mutated_tiers  # newest
    assert history[1]["terms"]["kickbackTiers"] == _TIERS_3  # original
    assert history[0]["recorded_at_iso"] is not None
    assert history[1]["recorded_at_iso"] is not None
    assert history[0]["created_at_iso"] >= history[1]["created_at_iso"]

    # Namespace scoping: a second namespace sees NO history for this agreement.
    other_namespace = await make_namespace()
    other_history = await get_term_change_history(pg_pool, other_namespace, agreement_id)
    assert other_history == []


# ---------------------------------------------------------------------------
# 6. Ledger immutability discipline — source-level assertion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_module_source_never_updates_or_deletes_ledger_rows() -> None:
    """kickback.py must contain no UPDATE/DELETE against v3_cognitive_ledger."""
    from nce.vertical_modules.agreements import kickback as kickback_module

    source = inspect.getsource(kickback_module)
    assert not re.search(r"(?i)\bUPDATE\s+v3_cognitive_ledger\b", source), (
        "kickback.py must never UPDATE ledger rows (append-only audit trail)"
    )
    assert not re.search(r"(?i)\bDELETE\s+FROM\s+v3_cognitive_ledger\b", source), (
        "kickback.py must never DELETE ledger rows (append-only audit trail)"
    )


# ---------------------------------------------------------------------------
# 7. Fail-closed money terms + period bounds + GL amounts (TAG fix-forwards)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_tiers_localized_decimal_fails_closed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A confirmed tier with pct "3,5" (Norwegian decimal comma) must return
    malformed_terms — never a silently truncated tier table with status ok
    (a dropped mid-tier understates earned kickback)."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="913333333",
        tiers=[
            {"threshold": 100_000.0, "pct": 2.0},
            {"threshold": 500_000.0, "pct": "3,5"},  # type: ignore[list-item]
        ],
        review_status="auto_green",
    )

    seam = AsyncMock(return_value=[_gl_row("913333333", 600_000.0)])
    with patch(_SEAM, seam):
        result = await do_reconcile_kickback(
            _EngineStub(pg_pool),
            {"namespace_id": namespace_id, "agreement_id": agreement_id},
        )

    assert result["status"] == "malformed_terms"
    assert "tier[1]" in result["detail"]
    seam.assert_not_called()  # fails BEFORE any GL access
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_tiers_bool_and_duplicate_fail_closed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Boolean thresholds (float(True)==1.0 would overstate) and duplicate
    thresholds (ambiguous pct) both fail closed as malformed_terms."""
    await _seed_ownership(pg_pool, namespace_id)

    for bad_tiers, expected_fragment in [
        ([{"threshold": True, "pct": 2.0}], "boolean"),
        (
            [
                {"threshold": 100_000.0, "pct": 2.0},
                {"threshold": 100_000.0, "pct": 4.0},
            ],
            "duplicate",
        ),
    ]:
        agreement_id = uuid.uuid4()
        await _insert_agreement(
            pg_pool,
            namespace_id,
            agreement_id=agreement_id,
            supplier_id="913333333",
            tiers=bad_tiers,  # type: ignore[arg-type]
            review_status="auto_green",
        )
        seam = AsyncMock(return_value=[])
        with patch(_SEAM, seam):
            result = await do_reconcile_kickback(
                _EngineStub(pg_pool),
                {"namespace_id": namespace_id, "agreement_id": agreement_id},
            )
        assert result["status"] == "malformed_terms"
        assert expected_fragment in result["detail"]
        seam.assert_not_called()
        assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unparseable_period_bound_raises_never_widens(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A supplied-but-unparseable since_iso/until_iso must raise ValueError —
    silently dropping the bound would return ALL-TIME spend labeled as the
    requested period."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="913333333",
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    seam = AsyncMock(return_value=[])
    for bad_params in (
        {"since_iso": "01.02.2026"},
        {"until_iso": "31-12-2026"},
    ):
        with patch(_SEAM, seam):
            with pytest.raises(ValueError, match="parseable ISO date"):
                await do_reconcile_kickback(
                    _EngineStub(pg_pool),
                    {
                        "namespace_id": namespace_id,
                        "agreement_id": agreement_id,
                        **bad_params,
                    },
                )
    seam.assert_not_called()  # validation precedes any GL access


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bad_gl_amounts_skipped_and_counted_never_guessed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """GL rows with missing or unparseable amount_nok are excluded from the
    spend basis AND surfaced via gl_rows_skipped — not coerced to 0 into
    gl_rows_matched, and never a mid-reconcile crash.

    good row 150_000 → spend 150_000.00, earned 150_000 × 2.0 % = 3_000.00
    None amount + localized-string amount → gl_rows_skipped == 2
    """
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    orgnr = "914444444"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    gl_rows = [
        _gl_row(orgnr, 150_000.0, "2026-02-01"),
        {**_gl_row(orgnr, 0.0, "2026-02-02"), "amount_nok": None},
        {**_gl_row(orgnr, 0.0, "2026-02-03"), "amount_nok": "150 000,50"},
    ]
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result = await do_reconcile_kickback(
            _EngineStub(pg_pool),
            {"namespace_id": namespace_id, "agreement_id": agreement_id},
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 150_000.0
    assert result["earned_to_date_nok"] == 3_000.0
    assert result["gl_rows_matched"] == 1
    assert result["gl_rows_skipped"] == 2


# ---------------------------------------------------------------------------
# 8. Non-finite / negative money inputs + period filtering (re-audit round 2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nonfinite_and_negative_tiers_fail_closed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """float() accepts "nan"/"inf"/"-Infinity" and negatives — all must fail
    closed as malformed_terms, never poison earned math under status ok."""
    await _seed_ownership(pg_pool, namespace_id)

    for bad_tiers, expected_fragment in [
        ([{"threshold": 100_000.0, "pct": "nan"}], "non-finite"),
        ([{"threshold": "inf", "pct": 2.0}], "non-finite"),
        ([{"threshold": "-Infinity", "pct": 3.0}], "non-finite"),
        ([{"threshold": -100.0, "pct": 2.0}], "negative"),
        ([{"threshold": 100_000.0, "pct": -5.0}], "negative"),
    ]:
        agreement_id = uuid.uuid4()
        await _insert_agreement(
            pg_pool,
            namespace_id,
            agreement_id=agreement_id,
            supplier_id="915555555",
            tiers=bad_tiers,  # type: ignore[arg-type]
            review_status="auto_green",
        )
        seam = AsyncMock(return_value=[])
        with patch(_SEAM, seam):
            result = await do_reconcile_kickback(
                _EngineStub(pg_pool),
                {"namespace_id": namespace_id, "agreement_id": agreement_id},
            )
        assert result["status"] == "malformed_terms", f"tiers={bad_tiers}"
        assert expected_fragment in result["detail"], f"tiers={bad_tiers}"
        seam.assert_not_called()
        assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nonfinite_gl_amounts_skipped_and_counted(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """float('nan')/float('inf') amounts parse into Decimal without error but
    would poison the spend basis — skipped AND counted, never summed.

    good 200_000 → spend 200_000.00, tier 1 active, earned 4_000.00
    """
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    orgnr = "916666666"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    gl_rows = [
        _gl_row(orgnr, 200_000.0, "2026-02-01"),
        {**_gl_row(orgnr, 0.0, "2026-02-02"), "amount_nok": float("nan")},
        {**_gl_row(orgnr, 0.0, "2026-02-03"), "amount_nok": float("inf")},
    ]
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result = await do_reconcile_kickback(
            _EngineStub(pg_pool),
            {"namespace_id": namespace_id, "agreement_id": agreement_id},
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 200_000.0
    assert result["earned_to_date_nok"] == 4_000.0
    assert result["gl_rows_matched"] == 1
    assert result["gl_rows_skipped"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_projected_kickback_raises_before_any_work(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A non-numeric/bool/non-finite projected_kickback_nok fails loud at
    param validation — before the gate read and before any ledger write."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="915555555",
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    seam = AsyncMock(return_value=[])
    for bad_projected in ("abc", True, float("nan")):
        with patch(_SEAM, seam):
            with pytest.raises(ValueError, match="projected_kickback_nok"):
                await do_reconcile_kickback(
                    _EngineStub(pg_pool),
                    {
                        "namespace_id": namespace_id,
                        "agreement_id": agreement_id,
                        "projected_kickback_nok": bad_projected,
                    },
                )
    seam.assert_not_called()
    assert await _ledger_snapshot_count(pg_pool, namespace_id, agreement_id) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valid_period_bounds_filter_gl_rows_exactly(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A valid since/until pair scopes the spend basis (inclusive bounds);
    an unparseable gl_date under bounds is skipped AND counted.

    in-period rows 150_000 (2026-02-01, ON since bound) + 100_000 (2026-03-31,
    ON until bound) → spend 250_000.00, tier 1 active, earned 5_000.00;
    out-of-period 300_000 (2026-04-01) excluded (not counted as skipped);
    bad-date row counted in gl_rows_skipped.
    """
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    orgnr = "917777777"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        tiers=_TIERS_3,
        review_status="auto_green",
    )

    gl_rows = [
        _gl_row(orgnr, 150_000.0, "2026-02-01"),
        _gl_row(orgnr, 100_000.0, "2026-03-31"),
        _gl_row(orgnr, 300_000.0, "2026-04-01"),
        _gl_row(orgnr, 50_000.0, "not-a-date"),
    ]
    with patch(_SEAM, AsyncMock(return_value=gl_rows)):
        result = await do_reconcile_kickback(
            _EngineStub(pg_pool),
            {
                "namespace_id": namespace_id,
                "agreement_id": agreement_id,
                "since_iso": "2026-02-01",
                "until_iso": "2026-03-31",
            },
        )

    assert result["status"] == "ok"
    assert result["spend_to_date_nok"] == 250_000.0
    assert result["earned_to_date_nok"] == 5_000.0
    assert result["gl_rows_matched"] == 2
    assert result["gl_rows_skipped"] == 1
    assert result["period"] == {"since_iso": "2026-02-01", "until_iso": "2026-03-31"}
