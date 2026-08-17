"""
tests/test_agreements_compliance.py
====================================
Integration tests for M3.W8 — do_run_compliance_audit + do_suggest_terms.

This is the security-critical kickback-governance gate.  The load-bearing tests
are the WRONGFUL-APPROVE HUNT: every way a rebate could be wrongly authorized
must return ``approved is False`` and must NOT leave an ``approved=true`` decision
row in the ledger.  A single ``approved=True`` path exists (a human-signed
agreement that governs the supplier, carries no restricted clause, encodes a
rebate provision, and yields a ceiling the rebate does not exceed); everything
else fails closed.

Seeding convention (mirrors tests/test_agreements_kickback.py)
--------------------------------------------------------------
- Seed node ownership before inserting VENDOR nodes.
- VENDOR labels follow ``Vendor:{orgnr}`` with short orgnr-style identifiers so
  pg_trgm similarity stays above the C1 candidate gate before the exact
  suffix-match confirmation.
- Agreement rows use the nested per-field ``extracted`` shape produced by
  extract.py (``{value, extractionConfidence, reviewStatus}``); only
  ``review_status == 'auto_green'`` counts as human-signed.

Run with::

    set -a && source .env && set +a
    .venv/Scripts/python.exe -m pytest tests/test_agreements_compliance.py -q -rs
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from typing import Any

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.vertical_modules.agreements.compliance import (
    _MODEL_VERSION,
    _evaluate_discount_limit,
    _evaluate_restricted_clause,
    do_run_compliance_audit,
    do_suggest_terms,
)
from nce.vertical_modules.agreements.coverage import _resolve_vendor_node_id

# 3-tier table used across tests.  pct is a PERCENT (extract.py:36).  Max pct 5.0.
_TIERS_3: list[dict[str, float]] = [
    {"threshold": 100_000.0, "pct": 2.0},
    {"threshold": 500_000.0, "pct": 3.5},
    {"threshold": 1_000_000.0, "pct": 5.0},
]

# volumeCommitment 1_000_000 × max rate 5.0% / 100 = 50_000 signed ceiling.
_VOLUME_COMMITMENT = 1_000_000.0
_CEILING = 50_000.0


class _EngineStub:
    """Minimal engine stub — holds pg_pool (this wave makes no A2A calls)."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Seeding helpers (mirror test_agreements_kickback.py)
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
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
    review_status: str = "auto_green",
    kickback_tiers: Any = None,
    frame_discount_pct: Any = None,
    volume_commitment: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert (or upsert) an agreement review-queue row with the given terms."""
    extracted: dict[str, Any] = {}
    if supplier_id is not None:
        extracted["supplierId"] = {
            "value": supplier_id,
            "extractionConfidence": 95.0,
            "reviewStatus": "auto_green",
        }
    if kickback_tiers is not None:
        extracted["kickbackTiers"] = {
            "value": kickback_tiers,
            "extractionConfidence": 90.0,
            "reviewStatus": "needs_review_yellow",
        }
    if frame_discount_pct is not None:
        extracted["frameDiscountPct"] = {
            "value": frame_discount_pct,
            "extractionConfidence": 90.0,
            "reviewStatus": "needs_review_yellow",
        }
    if volume_commitment is not None:
        extracted["volumeCommitment"] = {
            "value": volume_commitment,
            "extractionConfidence": 90.0,
            "reviewStatus": "needs_review_yellow",
        }
    if extra:
        extracted.update(extra)

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


async def _decision_rows(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    po_number: str,
) -> list[dict[str, Any]]:
    """Return decoded compliance_audit decision payloads for one PO."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        rows = await conn.fetch(
            """
            SELECT tlx_scores
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  model_version = $2
              AND  tlx_scores->>'kind' = 'compliance_audit'
              AND  tlx_scores->>'po_number' = $3
            """,
            str(namespace_id),
            _MODEL_VERSION,
            po_number,
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row["tlx_scores"]
        out.append(json.loads(payload) if isinstance(payload, str) else payload)
    return out


async def _assert_no_approved_true_row(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    po_number: str,
) -> None:
    rows = await _decision_rows(pg_pool, namespace_id, po_number)
    assert all(r["approved"] is False for r in rows), (
        f"a denied audit must never leave an approved=true ledger row: {rows}"
    )


async def _suggestion_count(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
) -> int:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        count = await conn.fetchval(
            """
            SELECT count(*)
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  model_version = $2
              AND  tlx_scores->>'kind' = 'terms_suggestion'
              AND  tlx_scores->>'agreement_id' = $3
            """,
            str(namespace_id),
            _MODEL_VERSION,
            str(agreement_id),
        )
    return int(count)


async def _kg_node_count(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> int:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid",
            str(namespace_id),
        )
    return int(count)


def _params(
    namespace_id: uuid.UUID,
    *,
    po_number: str,
    supplier_id: str,
    rebate_amount: Any,
    agreement_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "namespace_id": str(namespace_id),
        "po_number": po_number,
        "supplier_id": supplier_id,
        "rebate_amount": rebate_amount,
    }
    if agreement_id is not None:
        p["agreement_id"] = str(agreement_id)
    return p


# ===========================================================================
# 1. WRONGFUL-APPROVE HUNT (the load-bearing tests) — every path must deny
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_agreement_for_supplier_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Supplier resolves to a real vendor node but has NO signed agreement."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "912345678"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-1", supplier_id=orgnr, rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any("no signed agreement governs supplier" in r for r in result["reasons"])
    assert result["checks"]["signed_agreement"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-1")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unconfirmed_agreement_never_authorizes(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A needs_review_yellow agreement (unconfirmed OCR terms) must NOT authorize.

    §9.3: money terms only reach auto_green via a human 'confirm'.  Passing the
    unconfirmed agreement's id explicitly must still deny.
    """
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "913111222"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="needs_review_yellow",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(
            namespace_id,
            po_number="PO-2",
            supplier_id=orgnr,
            rebate_amount=1_000.0,
            agreement_id=agreement_id,
        ),
    )

    assert result["approved"] is False
    assert any("not signed" in r for r in result["reasons"])
    assert result["checks"]["signed_agreement"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-2")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unconfirmed_agreement_supplier_match_path_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Without an explicit agreement_id, a non-auto_green row is invisible to the
    supplier-match query → no signed agreement governs supplier."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "913111223"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=uuid.uuid4(),
        supplier_id=orgnr,
        review_status="manual_red",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-2b", supplier_id=orgnr, rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any("no signed agreement governs supplier" in r for r in result["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-2b")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restricted_clause_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A signed agreement carrying a restricted clause must deny, naming it."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "914333444"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
        extra={"clauseText": "This master agreement includes a rebate_prohibited term."},
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-3", supplier_id=orgnr, rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any(
        "restricted clause present" in r and "rebate_prohibited" in r for r in result["reasons"]
    )
    assert result["checks"]["signed_agreement"] is True
    assert result["checks"]["restricted_clause"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-3")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebate_exceeds_signed_ceiling_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """rebate 60_000 > ceiling 50_000 (1_000_000 × 5% / 100) must deny."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "915555666"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-4", supplier_id=orgnr, rebate_amount=60_000.0),
    )

    assert result["approved"] is False
    assert any("exceeds signed ceiling" in r for r in result["reasons"])
    assert result["checks"]["discount_limit"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-4")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_rebate_provision_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A signed agreement with NO kickbackTiers and NO frameDiscountPct gives no
    basis for any rebate → deny (even a tiny rebate)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "916777888"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        volume_commitment=_VOLUME_COMMITMENT,  # basis present, but no rebate provision
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-5", supplier_id=orgnr, rebate_amount=1.0),
    )

    assert result["approved"] is False
    assert any("no signed rebate or kickback provision" in r for r in result["reasons"])
    assert result["checks"]["discount_limit"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-5")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provision_without_volume_basis_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A rebate provision but no volumeCommitment basis → no derivable ceiling →
    the amount cannot be confirmed within a signed limit → deny."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "916777889"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,  # provision present, NO volumeCommitment
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-5b", supplier_id=orgnr, rebate_amount=1.0),
    )

    assert result["approved"] is False
    assert any("no derivable numeric rebate ceiling" in r for r in result["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-5b")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_rebate", [-1.0, float("nan"), float("inf"), "1000", True, None])
async def test_invalid_rebate_amount_denies_without_ledger(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    bad_rebate: Any,
) -> None:
    """Negative / NaN / inf / non-numeric / bool / missing rebate → deny, no crash,
    and no ledger row (param validation precedes any DB scope)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "917999000"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-6", supplier_id=orgnr, rebate_amount=bad_rebate),
    )

    assert result["approved"] is False
    assert any("rebate_amount" in r for r in result["reasons"])
    # Param-validation denials write no ledger row (nothing to audit yet).
    assert await _decision_rows(pg_pool, namespace_id, "PO-6") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_tiers_deny(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A signed agreement whose tier table cannot be normalized (localized decimal
    comma) fails closed — never a guessed ceiling."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "918111222"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=[{"threshold": 100_000.0, "pct": "3,5"}],
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-7", supplier_id=orgnr, rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any("malformed signed terms" in r for r in result["reasons"])
    assert result["checks"]["discount_limit"] is False
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-7")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolvable_supplier_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A supplier_id that resolves to NO vendor node cannot be governed → deny."""
    await _seed_ownership(pg_pool, namespace_id)
    # No vendor node inserted for this orgnr.
    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-8", supplier_id="919000000", rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any("could not be resolved" in r for r in result["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-8")


# ===========================================================================
# 2. Happy path — the ONLY approve path
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_signed_within_limit_approves(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Signed agreement + valid kickback provision + within-limit rebate + no
    restricted clause → approved; a decision row (approved=true) is appended."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "920111222"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-OK", supplier_id=orgnr, rebate_amount=40_000.0),
    )

    assert result["approved"] is True
    assert result["reasons"] == []
    assert result["agreement_id"] == str(agreement_id)
    assert result["checks"] == {
        "signed_agreement": True,
        "restricted_clause": True,
        "discount_limit": True,
    }

    rows = await _decision_rows(pg_pool, namespace_id, "PO-OK")
    assert len(rows) == 1
    assert rows[0]["approved"] is True
    assert rows[0]["agreement_id"] == str(agreement_id)
    assert rows[0]["rebate_amount"] == 40_000.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exactly_at_ceiling_approves(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """rebate exactly == ceiling (50_000) is within the limit (boundary)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "920111223"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-OK2", supplier_id=orgnr, rebate_amount=_CEILING),
    )

    assert result["approved"] is True
    assert result["checks"]["discount_limit"] is True


# ===========================================================================
# 3. Supplier identity — C1 node discrimination (non-vacuous)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_suppliers_signed_agreement_does_not_authorize(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Supplier B has a signed agreement; an audit for supplier A must NOT be
    authorized by it (C1 node-identity discrimination).

    Non-vacuous: both orgnrs are proven to resolve to distinct, non-None nodes.
    """
    await _seed_ownership(pg_pool, namespace_id)
    orgnr_a = "821111111"
    orgnr_b = "822222222"
    node_a = await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_a}")
    node_b = await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_b}")
    assert node_a != node_b

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        resolved_a = await _resolve_vendor_node_id(conn, namespace_id, raw_id=orgnr_a)
        resolved_b = await _resolve_vendor_node_id(conn, namespace_id, raw_id=orgnr_b)
    assert resolved_a == node_a, "vendor A did not resolve — test would be vacuous"
    assert resolved_b == node_b, "vendor B did not resolve — test would be vacuous"

    # Only supplier B has a signed agreement.
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=uuid.uuid4(),
        supplier_id=orgnr_b,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    # Audit a rebate for supplier A — must deny (B's agreement cannot govern A).
    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-C1", supplier_id=orgnr_a, rebate_amount=1_000.0),
    )

    assert result["approved"] is False
    assert any("no signed agreement governs supplier" in r for r in result["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-C1")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_agreement_id_for_other_supplier_denies(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Passing supplier B's signed agreement_id while auditing supplier A must
    deny on vendor-identity mismatch — the explicit-id path is not a bypass."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr_a = "823333333"
    orgnr_b = "824444444"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_a}")
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr_b}")
    b_agreement = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=b_agreement,
        supplier_id=orgnr_b,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(
            namespace_id,
            po_number="PO-C1b",
            supplier_id=orgnr_a,
            rebate_amount=1_000.0,
            agreement_id=b_agreement,
        ),
    )

    assert result["approved"] is False
    assert any("does not govern supplier" in r for r in result["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-C1b")


# ===========================================================================
# 4. do_suggest_terms — propose-only advisor, never mutates the agreement
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_suggest_terms_is_propose_only(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """do_suggest_terms appends ONE ledger row, recommends vs a benchmark, and
    leaves the agreement terms and the graph completely unchanged (applied=False)."""
    await _seed_ownership(pg_pool, namespace_id)
    orgnr = "830111222"
    agreement_id = uuid.uuid4()
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id=orgnr,
        review_status="auto_green",
        frame_discount_pct=2.0,
        volume_commitment=_VOLUME_COMMITMENT,
        extra={
            "paymentTermsDays": {
                "value": 30,
                "extractionConfidence": 90.0,
                "reviewStatus": "needs_review_yellow",
            }
        },
    )

    # Snapshot the agreement row and node count BEFORE the suggestion.
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        before = await conn.fetchval(
            "SELECT extracted FROM agreement_review_queue "
            "WHERE agreement_id = $1 AND namespace_id = $2::uuid",
            agreement_id,
            str(namespace_id),
        )
    nodes_before = await _kg_node_count(pg_pool, namespace_id)

    result = await do_suggest_terms(
        _EngineStub(pg_pool),
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "benchmark": {"paymentTermsDays": 60, "frameDiscountPct": 3.0},
        },
    )

    assert result["status"] == "ok"
    assert result["applied"] is False
    assert result["agreement_id"] == str(agreement_id)
    assert result["suggestion_id"]
    fields = {rec["field"] for rec in result["recommendations"]}
    assert fields == {"paymentTermsDays", "frameDiscountPct"}

    # Exactly one propose-only ledger row was appended.
    assert await _suggestion_count(pg_pool, namespace_id, agreement_id) == 1

    # The agreement terms are UNCHANGED and NO graph node was written.
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        after = await conn.fetchval(
            "SELECT extracted FROM agreement_review_queue "
            "WHERE agreement_id = $1 AND namespace_id = $2::uuid",
            agreement_id,
            str(namespace_id),
        )
    before_d = before if isinstance(before, dict) else json.loads(before)
    after_d = after if isinstance(after, dict) else json.loads(after)
    assert before_d == after_d, "do_suggest_terms must not mutate the agreement terms"
    assert await _kg_node_count(pg_pool, namespace_id) == nodes_before, (
        "do_suggest_terms must not write any graph node"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_suggest_terms_no_benchmark_empty_recommendations(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """With no benchmark supplied, recommendations are empty (no invented numbers)
    but the propose-only ledger row is still appended."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    await _insert_agreement(
        pg_pool,
        namespace_id,
        agreement_id=agreement_id,
        supplier_id="831111222",
        review_status="auto_green",
        frame_discount_pct=2.0,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    result = await do_suggest_terms(
        _EngineStub(pg_pool),
        {"namespace_id": str(namespace_id), "agreement_id": str(agreement_id)},
    )

    assert result["status"] == "ok"
    assert result["applied"] is False
    assert result["recommendations"] == []
    assert await _suggestion_count(pg_pool, namespace_id, agreement_id) == 1


# ===========================================================================
# 5. Namespace scoping — an agreement in ns B never authorizes an audit in ns A
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_namespace_scoping_agreement_in_b_does_not_authorize_in_a(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace,
) -> None:
    """The signed agreement lives in ns B (where the same rebate WOULD approve);
    the identical audit run in ns A must deny — agreements are namespace-scoped.

    The vendor node exists in BOTH namespaces so the supplier resolves in ns A —
    isolating the assertion to the AGREEMENT's namespace scope, not resolution.
    """
    ns_b = await make_namespace()
    await _seed_ownership(pg_pool, namespace_id)  # ns A
    await _seed_ownership(pg_pool, ns_b)  # ns B

    orgnr = "840111222"
    await _insert_vendor_node(pg_pool, namespace_id, label=f"Vendor:{orgnr}")  # ns A
    await _insert_vendor_node(pg_pool, ns_b, label=f"Vendor:{orgnr}")  # ns B

    # The signed agreement exists ONLY in ns B.
    await _insert_agreement(
        pg_pool,
        ns_b,
        agreement_id=uuid.uuid4(),
        supplier_id=orgnr,
        review_status="auto_green",
        kickback_tiers=_TIERS_3,
        volume_commitment=_VOLUME_COMMITMENT,
    )

    # Non-vacuous: the same audit in ns B approves.
    approved_in_b = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(ns_b, po_number="PO-NS", supplier_id=orgnr, rebate_amount=1_000.0),
    )
    assert approved_in_b["approved"] is True

    # In ns A the agreement is invisible → deny.
    denied_in_a = await do_run_compliance_audit(
        _EngineStub(pg_pool),
        _params(namespace_id, po_number="PO-NS", supplier_id=orgnr, rebate_amount=1_000.0),
    )
    assert denied_in_a["approved"] is False
    assert any("no signed agreement governs supplier" in r for r in denied_in_a["reasons"])
    await _assert_no_approved_true_row(pg_pool, namespace_id, "PO-NS")


# ===========================================================================
# 6. Source-level ledger immutability discipline
# ===========================================================================


@pytest.mark.integration
def test_module_source_never_updates_or_deletes_ledger_rows() -> None:
    """compliance.py must contain no UPDATE/DELETE against v3_cognitive_ledger."""
    from nce.vertical_modules.agreements import compliance as compliance_module

    source = inspect.getsource(compliance_module)
    assert not re.search(r"(?i)\bUPDATE\s+v3_cognitive_ledger\b", source), (
        "compliance.py must never UPDATE ledger rows (append-only audit trail)"
    )
    assert not re.search(r"(?i)\bDELETE\s+FROM\s+v3_cognitive_ledger\b", source), (
        "compliance.py must never DELETE ledger rows (append-only audit trail)"
    )


# ===========================================================================
# 7. Pure-helper discrimination (no DB) — ceiling math + clause scan
# ===========================================================================


@pytest.mark.integration
def test_evaluate_discount_limit_boundaries() -> None:
    """The pure ceiling helper: within/at/over limit, no-provision, no-basis,
    malformed — each fails closed correctly."""
    from decimal import Decimal

    terms = {"kickbackTiers": _TIERS_3, "frameDiscountPct": None, "volumeCommitment": 1_000_000.0}
    # ceiling = 1_000_000 × 5% / 100 = 50_000
    assert _evaluate_discount_limit(terms, Decimal("40000"))[0] is True
    assert _evaluate_discount_limit(terms, Decimal("50000"))[0] is True
    over_ok, over_reason = _evaluate_discount_limit(terms, Decimal("50000.01"))
    assert over_ok is False
    assert over_reason is not None and "exceeds signed ceiling" in over_reason

    # No provision (no tiers, no frame) → deny.
    no_prov_ok, no_prov_reason = _evaluate_discount_limit(
        {"kickbackTiers": None, "frameDiscountPct": None, "volumeCommitment": 1_000_000.0},
        Decimal("1"),
    )
    assert no_prov_ok is False
    assert no_prov_reason is not None and "no signed rebate or kickback provision" in no_prov_reason

    # Provision but no volume basis → no derivable ceiling → deny.
    no_basis_ok, no_basis_reason = _evaluate_discount_limit(
        {"kickbackTiers": _TIERS_3, "frameDiscountPct": None, "volumeCommitment": None},
        Decimal("1"),
    )
    assert no_basis_ok is False
    assert no_basis_reason is not None and "no derivable numeric rebate ceiling" in no_basis_reason

    # Malformed tiers → deny.
    bad_ok, bad_reason = _evaluate_discount_limit(
        {"kickbackTiers": [{"threshold": 1.0, "pct": "3,5"}], "volumeCommitment": 1_000.0},
        Decimal("1"),
    )
    assert bad_ok is False
    assert bad_reason is not None and "malformed signed terms" in bad_reason

    # frameDiscountPct-only provision, within limit: 1_000_000 × 4% / 100 = 40_000.
    frame_terms = {"kickbackTiers": None, "frameDiscountPct": 4.0, "volumeCommitment": 1_000_000.0}
    assert _evaluate_discount_limit(frame_terms, Decimal("40000"))[0] is True
    assert _evaluate_discount_limit(frame_terms, Decimal("40001"))[0] is False


@pytest.mark.integration
def test_evaluate_restricted_clause_scan() -> None:
    """The pure clause scanner catches markers in keys, values, and free text
    (underscore and space forms) and passes clean terms."""
    clean, hits = _evaluate_restricted_clause(
        {"kickbackTiers": [{"threshold": 1.0, "pct": 2.0}], "volumeCommitment": 100.0}
    )
    assert clean is True
    assert hits == []

    # Space form in free text.
    dirty1, hits1 = _evaluate_restricted_clause({"clauseText": "supplier demands exclusivity"})
    assert dirty1 is False
    assert "exclusivity" in hits1

    # Underscore form as a structured flag key.
    dirty2, hits2 = _evaluate_restricted_clause({"no_rebate": True})
    assert dirty2 is False
    assert "no_rebate" in hits2
