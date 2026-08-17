"""
tests/test_agreements_signing.py
==================================
Integration tests for M3.W9 — do_request_signature + do_record_signature +
get_signature_history.

Key invariants asserted
-----------------------
1. Request opens a session: returns ``pending`` + a SHA-256 fingerprint; an
   ``AGREEMENT_SIGNATURE`` kg_node and a ``has_signature`` edge exist; a
   ``signature_request`` ledger row is written carrying the fingerprint.
2. Record with the SAME document → ``signed``; a ``signature_recorded`` ledger
   row is written; ``get_signature_history`` returns both events newest-first.
3. **Record with a TAMPERED document (different bytes) → ``fingerprint_mismatch``;
   NO ``signature_recorded`` row is written** (count unchanged).  This is the
   load-bearing security test: a tampered document must never record as signed.
4. Missing required params → ValueError.
5. Namespace scoping: a second namespace sees no signatures for the agreement.
6. Ledger immutability discipline: the module source contains no UPDATE/DELETE
   against v3_cognitive_ledger.

Seeding convention (mirrors tests/test_agreements_kickback.py)
---------------------------------------------------------------
- Seed node ownership before writing AGREEMENT_SIGNATURE nodes.
- Use direct INSERTs / scoped_pg_session reads for deterministic state.
- All DB-touching tests are ``@pytest.mark.integration`` + ``@pytest.mark.asyncio``
  and require a live Postgres via the ``pg_pool`` / ``namespace_id`` fixtures.

    set -a && source .env && set +a
    .venv/Scripts/python.exe -m pytest tests/test_agreements_signing.py -q -rs

(Never set NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL.)
"""

from __future__ import annotations

import inspect
import json
import re
import uuid

import asyncpg
import pytest

from nce.auth import set_namespace_context
from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
from nce.signing_service import sha256_fingerprint
from nce.vertical_modules.agreements.signing import (
    _KIND_RECORDED,
    _KIND_REQUEST,
    _MODEL_VERSION,
    do_record_signature,
    do_request_signature,
    get_signature_history,
)

# ---------------------------------------------------------------------------
# Engine stub
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub — holds pg_pool; uses the module transport singleton."""

    def __init__(self, pg_pool: asyncpg.Pool) -> None:
        self.pg_pool = pg_pool


# ---------------------------------------------------------------------------
# Seeding / query helpers (mirror test_agreements_kickback.py)
# ---------------------------------------------------------------------------


async def _seed_ownership(pg_pool: asyncpg.Pool, namespace_id: uuid.UUID) -> None:
    """Seed node ownership registry for the test namespace."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, namespace_id)
            await seed_node_ownership_registry(conn, namespace_id)


async def _count_ledger_by_kind(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
    kind: str,
) -> int:
    """Count signature ledger rows of one kind for one agreement in one namespace."""
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        count = await conn.fetchval(
            """
            SELECT count(*)
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  model_version = $2
              AND  tlx_scores->>'kind' = $3
              AND  tlx_scores->>'agreement_id' = $4
            """,
            str(namespace_id),
            _MODEL_VERSION,
            kind,
            str(agreement_id),
        )
    return int(count)


async def _signature_node_exists(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    label: str,
) -> bool:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM kg_nodes
            WHERE label = $1 AND entity_type = 'AGREEMENT_SIGNATURE'
              AND namespace_id = $2::uuid
            """,
            label,
            str(namespace_id),
        )
    return row is not None


async def _has_signature_edge_exists(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    *,
    agreement_id: uuid.UUID,
    object_label: str,
) -> bool:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM kg_edges
            WHERE subject_label = $1 AND predicate = 'has_signature'
              AND object_label = $2 AND namespace_id = $3::uuid
            """,
            f"Agreement:{agreement_id}",
            object_label,
            str(namespace_id),
        )
    return row is not None


# ---------------------------------------------------------------------------
# 1. Request opens a session: node + edge + pending ledger row
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_request_signature_opens_session_writes_node_edge_and_ledger(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    document = "Frame agreement between Example Integrator AS and Vendor AS — terms v1."
    engine = _EngineStub(pg_pool)

    result = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "signer@vendor.example",
        },
    )

    assert result["status"] == "ok"
    assert result["agreement_id"] == str(agreement_id)
    assert result["signature_status"] == "pending"
    # Fingerprint is the deterministic SHA-256 of the document bytes.
    assert result["fingerprint"] == sha256_fingerprint(document.encode("utf-8"))
    session_id = result["session_id"]
    assert session_id

    # Graph: AGREEMENT_SIGNATURE node + has_signature edge exist.
    label = f"AgreementSignature:{agreement_id}:{session_id}"
    assert await _signature_node_exists(pg_pool, namespace_id, label)
    assert await _has_signature_edge_exists(
        pg_pool, namespace_id, agreement_id=agreement_id, object_label=label
    )

    # Ledger: exactly one signature_request row, no recorded row yet.
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_REQUEST) == 1
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_RECORDED) == 0


# ---------------------------------------------------------------------------
# 2. Record with the same document → signed; history newest-first
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_signature_same_document_signs_and_history_newest_first(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    document = "SLA agreement — customer Veidekke — v2026.1"
    signer = "cfo@customer.example"
    engine = _EngineStub(pg_pool)

    requested = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": signer,
        },
    )
    session_id = requested["session_id"]

    recorded = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": session_id,
            "signed_document": document,  # identical bytes → fingerprint matches
            "signer": signer,
        },
    )

    assert recorded["status"] == "ok"
    assert recorded["signature_status"] == "signed"
    assert recorded["session_id"] == session_id
    assert recorded["fingerprint"] == requested["fingerprint"]

    # Ledger: one request row + one recorded row.
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_REQUEST) == 1
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_RECORDED) == 1

    # History: newest-first — recorded event first, request event second.
    history = await get_signature_history(pg_pool, namespace_id, agreement_id)
    assert len(history) == 2
    assert history[0]["kind"] == _KIND_RECORDED
    assert history[0]["status"] == "signed"
    assert history[1]["kind"] == _KIND_REQUEST
    assert history[1]["status"] == "pending"
    assert history[0]["session_id"] == session_id == history[1]["session_id"]
    assert history[0]["fingerprint"] == requested["fingerprint"]
    assert history[0]["created_at_iso"] >= history[1]["created_at_iso"]


# ---------------------------------------------------------------------------
# 3. TAMPERED document → fingerprint_mismatch, NO signed row (load-bearing)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_signature_tampered_document_never_records_as_signed(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    original = "Payment terms: 30 days net. Kickback tier 1: 2%."
    tampered = "Payment terms: 30 days net. Kickback tier 1: 20%."  # substituted
    signer = "attacker@vendor.example"
    engine = _EngineStub(pg_pool)

    requested = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": original,
            "signer": signer,
        },
    )
    session_id = requested["session_id"]

    result = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": session_id,
            "signed_document": tampered,  # different bytes → mismatch
            "signer": signer,
        },
    )

    assert result["status"] == "fingerprint_mismatch"
    assert result["expected_fingerprint"] == requested["fingerprint"]
    assert result["actual_fingerprint"] == sha256_fingerprint(tampered.encode("utf-8"))
    assert result["expected_fingerprint"] != result["actual_fingerprint"]

    # NOTHING was recorded as signed — the request row remains, no recorded row.
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_REQUEST) == 1
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_RECORDED) == 0

    # History carries only the pending request — no signed event leaked in.
    history = await get_signature_history(pg_pool, namespace_id, agreement_id)
    assert len(history) == 1
    assert history[0]["kind"] == _KIND_REQUEST


# ---------------------------------------------------------------------------
# 4. Missing required params → ValueError
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_required_params_raise_value_error(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    engine = _EngineStub(pg_pool)
    agreement_id = str(uuid.uuid4())

    request_base = {
        "namespace_id": str(namespace_id),
        "agreement_id": agreement_id,
        "document": "doc",
        "signer": "s@e.example",
    }
    for missing in ("namespace_id", "agreement_id", "document", "signer"):
        params = {k: v for k, v in request_base.items() if k != missing}
        with pytest.raises(ValueError):
            await do_request_signature(engine, params)

    record_base = {
        "namespace_id": str(namespace_id),
        "agreement_id": agreement_id,
        "session_id": str(uuid.uuid4()),
        "signed_document": "doc",
        "signer": "s@e.example",
    }
    for missing in ("namespace_id", "agreement_id", "session_id", "signed_document", "signer"):
        params = {k: v for k, v in record_base.items() if k != missing}
        with pytest.raises(ValueError):
            await do_record_signature(engine, params)


# ---------------------------------------------------------------------------
# 5. Namespace scoping — a second namespace sees no signatures
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_signature_history_is_namespace_scoped(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    make_namespace,
) -> None:
    await _seed_ownership(pg_pool, namespace_id)

    agreement_id = uuid.uuid4()
    engine = _EngineStub(pg_pool)
    await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": "Namespace A contract.",
            "signer": "a@a.example",
        },
    )

    # Owning namespace sees the request.
    own = await get_signature_history(pg_pool, namespace_id, agreement_id)
    assert len(own) == 1

    # A second namespace sees nothing for the same agreement id.
    other_namespace = await make_namespace()
    other = await get_signature_history(pg_pool, other_namespace, agreement_id)
    assert other == []


# ---------------------------------------------------------------------------
# 6. Ledger immutability discipline — source-level assertion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_module_source_never_updates_or_deletes_ledger_rows() -> None:
    """signing.py must contain no UPDATE/DELETE against v3_cognitive_ledger."""
    from nce.vertical_modules.agreements import signing as signing_module

    source = inspect.getsource(signing_module)
    assert not re.search(r"(?i)\bUPDATE\s+v3_cognitive_ledger\b", source), (
        "signing.py must never UPDATE ledger rows (append-only audit trail)"
    )
    assert not re.search(r"(?i)\bDELETE\s+FROM\s+v3_cognitive_ledger\b", source), (
        "signing.py must never DELETE ledger rows (append-only audit trail)"
    )


# ---------------------------------------------------------------------------
# 7. Recording a signature promotes the review-queue row to auto_green (§9.3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_signature_promotes_review_queue_to_auto_green(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A recorded signature is the §9.3 human sign-off: it flips the agreement's
    review-queue row from an unsigned DRAFT (needs_review_yellow) to auto_green,
    which is what makes an authored agreement reconcilable by kickback."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()

    # Seed an unsigned DRAFT review-queue row (as B111 do_create_agreement writes).
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref,
                extraction_confidence, review_status, extracted
            ) VALUES ($1, $2::uuid, $3, $4, 'needs_review_yellow', '{}'::jsonb)
            """,
            agreement_id,
            str(namespace_id),
            f"authored://{agreement_id}",
            100.0,
        )

    document = "Frame agreement — Example Integrator AS × Vendor AS — authored draft."
    engine = _EngineStub(pg_pool)
    requested = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "cfo@example.test",
        },
    )
    await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": requested["session_id"],
            "signed_document": document,
            "signer": "cfo@example.test",
        },
    )

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT review_status, reviewed_by
            FROM   agreement_review_queue
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            agreement_id,
            str(namespace_id),
        )
    assert row is not None
    assert row["review_status"] == "auto_green", "signing did not promote the row"
    assert row["reviewed_by"] == "cfo@example.test"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_signature_never_overrides_manual_red_reject(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """A human ``manual_red`` reject is a deliberate veto — a countersignature
    must NOT silently promote it to auto_green (that would make rejected terms
    reconcilable against money without an explicit re-review)."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref,
                extraction_confidence, review_status, extracted
            ) VALUES ($1, $2::uuid, $3, $4, 'manual_red', '{}'::jsonb)
            """,
            agreement_id,
            str(namespace_id),
            f"authored://{agreement_id}",
            100.0,
        )

    document = "Rejected agreement — do not honour."
    engine = _EngineStub(pg_pool)
    requested = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "rogue@example.test",
        },
    )
    result = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": requested["session_id"],
            "signed_document": document,
            "signer": "rogue@example.test",
        },
    )
    # The signature itself still records (status ok) — but the veto stands.
    assert result["status"] == "ok"

    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        status = await conn.fetchval(
            """
            SELECT review_status FROM agreement_review_queue
            WHERE agreement_id = $1 AND namespace_id = $2::uuid
            """,
            agreement_id,
            str(namespace_id),
        )
    assert status == "manual_red", "signing wrongly overrode a human reject"


# ---------------------------------------------------------------------------
# Signature must bind to the TERMS and to the REQUESTED SIGNER, not just to a
# document. Recording is what promotes a row to auto_green (money becomes
# reconcilable), so a signature that does not cover the terms in force must not
# record.
# ---------------------------------------------------------------------------


async def _seed_review_row(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
    agreement_id: uuid.UUID,
    extracted: dict,
) -> None:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref,
                extraction_confidence, review_status, extracted
            ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
            ON CONFLICT (agreement_id, namespace_id) DO UPDATE
                SET extracted = EXCLUDED.extracted
            """,
            agreement_id,
            str(namespace_id),
            f"test://{agreement_id}",
            100.0,
            "needs_review_yellow",
            json.dumps(extracted),
        )


async def _review_status(
    pg_pool: asyncpg.Pool, namespace_id: uuid.UUID, agreement_id: uuid.UUID
) -> str | None:
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        return await conn.fetchval(
            """
            SELECT review_status FROM agreement_review_queue
            WHERE agreement_id = $1 AND namespace_id = $2::uuid
            """,
            agreement_id,
            str(namespace_id),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terms_changed_between_request_and_record_refuses_to_sign(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Swapping the money terms after requesting a signature must invalidate it.

    The document fingerprint proves only that some bytes are unchanged; it says
    nothing about `extracted`, which is what the §9.3 promotion makes
    reconcilable. Without a terms binding, an agreement could be signed at 2%
    and recorded at 25%.
    """
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    engine = _EngineStub(pg_pool)
    document = "Frame agreement — kickback 2%."

    await _seed_review_row(
        pg_pool,
        namespace_id,
        agreement_id,
        {"kickbackTiers": {"value": [{"threshold": 100_000, "pct": 2.0}]}},
    )
    req = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "signer@vendor.example",
        },
    )

    # Terms are rewritten AFTER the signature was requested.
    await _seed_review_row(
        pg_pool,
        namespace_id,
        agreement_id,
        {"kickbackTiers": {"value": [{"threshold": 100_000, "pct": 25.0}]}},
    )

    res = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": req["session_id"],
            "signed_document": document,  # same bytes — only the TERMS moved
            "signer": "signer@vendor.example",
        },
    )

    assert res["status"] == "terms_changed", res
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_RECORDED) == 0
    assert await _review_status(pg_pool, namespace_id, agreement_id) == "needs_review_yellow", (
        "terms-changed signature must not promote the row to auto_green"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unchanged_terms_still_sign_and_promote(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """Control: the terms binding must not break the legitimate happy path."""
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    engine = _EngineStub(pg_pool)
    document = "Frame agreement — kickback 2%."

    await _seed_review_row(
        pg_pool,
        namespace_id,
        agreement_id,
        {"kickbackTiers": {"value": [{"threshold": 100_000, "pct": 2.0}]}},
    )
    req = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "signer@vendor.example",
        },
    )
    res = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": req["session_id"],
            "signed_document": document,
            "signer": "signer@vendor.example",
        },
    )

    assert res["status"] == "ok", res
    assert await _review_status(pg_pool, namespace_id, agreement_id) == "auto_green"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recording_under_a_different_signer_is_refused(
    pg_pool: asyncpg.Pool,
    namespace_id: uuid.UUID,
) -> None:
    """The session was opened FOR a specific signer.

    Recording under another name would attribute one party's signature to
    another — and that name lands in `reviewed_by` on the §9.3 promotion.
    """
    await _seed_ownership(pg_pool, namespace_id)
    agreement_id = uuid.uuid4()
    engine = _EngineStub(pg_pool)
    document = "Frame agreement — terms v1."

    req = await do_request_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "document": document,
            "signer": "alice@vendor.example",
        },
    )
    res = await do_record_signature(
        engine,
        {
            "namespace_id": str(namespace_id),
            "agreement_id": str(agreement_id),
            "session_id": req["session_id"],
            "signed_document": document,
            "signer": "mallory@attacker.example",
        },
    )

    assert res["status"] == "signer_mismatch", res
    assert res["expected_signer"] == "alice@vendor.example"
    assert await _count_ledger_by_kind(pg_pool, namespace_id, agreement_id, _KIND_RECORDED) == 0
