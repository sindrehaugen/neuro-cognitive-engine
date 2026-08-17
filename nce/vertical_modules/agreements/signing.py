"""
nce/vertical_modules/agreements/signing.py
============================================
Signing lifecycle for the Agreements vertical module — M3.W9.

Two flow-invoked core entry points drive an agreement through the e-sign
ceremony behind the shared C7 :class:`~nce.signing_service.transport.SignTransport`
abstraction:

``do_request_signature``
    SHA-256-fingerprints the contract document (tamper-evidence handle), opens
    a signing session on the transport, writes an ``AGREEMENT_SIGNATURE``
    kg_node + ``has_signature`` edge, and appends a ``signature_request`` row
    to ``v3_cognitive_ledger`` (append-only) carrying the fingerprint.

``do_record_signature``
    Recomputes the fingerprint of the *signed* document and compares it to the
    fingerprint recorded at request time **FIRST** — a tampered document can
    never record as signed (no transport call, no signed node, no ledger
    write).  On a match it records via the transport, appends a
    ``signature_recorded`` ledger row, optionally marks a
    ``lifecycleState=SIGNED`` ``AGREEMENT_TERM`` (mirrors B111's DRAFT term),
    and best-effort runs ``engine.verify_memory`` on the agreement's memory.

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per function; the ledger-append and fingerprint-read helpers are shared
  (DRY) between the two entry points.
- Dependencies point inward: this module depends on the ``signing_service``
  abstraction (never a concrete vendor), the ``graph`` helpers, and ``db_utils``
  — nothing from admin_handlers / the MCP layer.
- Fingerprint durable home: the SHA-256 hex digest lives in the append-only
  ``v3_cognitive_ledger`` payload (``tlx_scores`` jsonb), the same audit-trail
  home ``kickback.py`` uses.  No schema change, no new event_type, no new
  table — the wave is pure-core.
- Signed-state is NOT stored on ``kg_nodes`` (there is no value column) — it is
  represented by the ``signature_recorded`` ledger row's ``status`` field.
- Explicit ``namespace_id = $N::uuid`` predicate on every SQL query (never
  RLS-only: owner-pool test roles can bypass FORCE RLS).
- Ledger writes are append-only INSERTs; this module never UPDATEs or DELETEs
  ledger rows.
- Secrets never logged; the fingerprint is a content-identity handle, not a
  signing key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write
from nce.mcp_args import require_namespace_id
from nce.signing import canonical_json
from nce.signing_service import (
    ManualTransport,
    SignTransport,
    TransportMethod,
    sha256_fingerprint,
)
from nce.vertical_modules.agreements.graph import (
    upsert_agreement_edge,
    upsert_agreement_term_node,
)

log = logging.getLogger("nce.vertical_modules.agreements.signing")

_AGREEMENTS_ENGINE = "agreements"

# model_version discriminator for this module's v3_cognitive_ledger rows.
_MODEL_VERSION = "agreements-signing-v1"

# Zero tensor matching the NOT NULL empathic_tensor column (float[6] in the
# live schema) — mirrors kickback.py / procurement/recalibration.py.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Signature lifecycle statuses.
_STATUS_PENDING = "pending"
_STATUS_SIGNED = "signed"
_STATUS_DECLINED = "declined"

# Ledger payload discriminators (the ``kind`` field inside tlx_scores).
_KIND_REQUEST = "signature_request"
_KIND_RECORDED = "signature_recorded"

# Transport rail — the credential-free ``manual`` transport is the default.
_METHOD: TransportMethod = "manual"

# Process-wide ManualTransport fallback so a session opened by
# do_request_signature is still resolvable by do_record_signature's on_signed
# within the same process (ManualTransport stores sessions per-instance).
_TRANSPORT: SignTransport | None = None


# ---------------------------------------------------------------------------
# Transport accessor
# ---------------------------------------------------------------------------


def _get_transport(engine: Any) -> SignTransport:
    """Return the SignTransport to use for this call.

    Prefers an engine-supplied ``sign_transport`` (lets a deployment inject a
    vendor rail — oneflow/criipto/signicat — without touching this module);
    otherwise falls back to a process-wide :class:`ManualTransport` singleton so
    the in-memory session opened at request time survives to record time.
    """
    injected = getattr(engine, "sign_transport", None)
    if injected is not None:
        return injected  # type: ignore[no-any-return]
    global _TRANSPORT
    if _TRANSPORT is None:
        _TRANSPORT = ManualTransport()
    return _TRANSPORT


# ---------------------------------------------------------------------------
# Small param helpers — fail loud, no guessing
# ---------------------------------------------------------------------------


def _require_str(params: dict[str, Any], key: str) -> str:
    """Return a required non-empty string param, else raise ``ValueError``."""
    raw = params.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError(f"{key} is required")
    return str(raw)


def _doc_bytes(document: Any) -> bytes:
    """Coerce a document param (str or bytes) to bytes for fingerprinting."""
    if isinstance(document, bytes):
        return document
    return str(document).encode("utf-8")


# ---------------------------------------------------------------------------
# Ledger helpers — append-only signature audit trail (mirrors kickback.py)
# ---------------------------------------------------------------------------


async def _append_signature_ledger(
    conn: Any,
    ns_uuid: uuid.UUID,
    payload: dict[str, Any],
) -> str:
    """Append one signature-event row to ``v3_cognitive_ledger`` (append-only).

    The recorded timestamp comes from the DB clock (``SELECT now()``), not the
    client clock, so the audit trail is single-sourced.  Rows written here are
    never mutated or removed by this module.
    """
    ledger_id = uuid.uuid4()
    recorded_at = await conn.fetchval("SELECT now()")
    full_payload = {**payload, "recorded_at_iso": recorded_at.isoformat()}
    await conn.execute(
        """
        INSERT INTO v3_cognitive_ledger (
            id, namespace_id, memory_id,
            empathic_tensor, tlx_scores, vad_scores, model_version
        ) VALUES (
            $1::uuid, $2::uuid, NULL,
            $3::float[], $4::jsonb, $5::jsonb, $6
        )
        """,
        str(ledger_id),
        str(ns_uuid),
        _ZERO_TENSOR,
        json.dumps(full_payload),
        json.dumps({}),
        _MODEL_VERSION,
    )
    return str(ledger_id)


async def _read_request_payload(
    conn: Any,
    ns_uuid: uuid.UUID,
    agreement_id_str: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Return the full ledger payload recorded at request time.

    Newest-first on ``created_at`` (the ledger's timestamp column).  Returns
    ``None`` when no ``signature_request`` row exists for the pair.  Callers
    need more than the document fingerprint: the recorded ``signer`` and
    ``terms_fingerprint`` are what bind a signature to *who* was asked and to
    *which* money terms (see ``_compute_terms_fingerprint``).
    """
    row = await conn.fetchrow(
        """
        SELECT tlx_scores
        FROM   v3_cognitive_ledger
        WHERE  namespace_id = $1::uuid
          AND  model_version = $2
          AND  tlx_scores->>'kind' = $3
          AND  tlx_scores->>'agreement_id' = $4
          AND  tlx_scores->>'session_id' = $5
        ORDER BY created_at DESC
        LIMIT  1
        """,
        str(ns_uuid),
        _MODEL_VERSION,
        _KIND_REQUEST,
        agreement_id_str,
        session_id,
    )
    if row is None:
        return None
    payload = row["tlx_scores"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    return payload if isinstance(payload, dict) else None


async def _compute_terms_fingerprint(
    conn: Any,
    ns_uuid: uuid.UUID,
    agreement_id_str: str,
) -> str | None:
    """SHA-256 over the RFC 8785-canonical rendering of the agreement's terms.

    ``agreement_review_queue.extracted`` is the machine-readable term store that
    compliance.py and kickback.py treat as signed ground truth, so it — not the
    free-form document — is what a signature must actually attest to.  Recording
    this at request time and re-checking it at record time makes the ceremony
    bind to SPECIFIC NUMBERS: swapping the money terms between "please sign" and
    "signed" invalidates the signature instead of silently authorising the new
    ones.

    Returns ``None`` when the agreement has no review-queue row (nothing to
    bind, and the §9.3 promotion is a 0-row no-op in that case anyway).
    Canonicalisation is RFC 8785, so key order and formatting cannot shift the
    fingerprint.
    """
    row = await conn.fetchrow(
        """
        SELECT extracted
        FROM   agreement_review_queue
        WHERE  agreement_id = $1 AND namespace_id = $2::uuid
        """,
        uuid.UUID(agreement_id_str),
        str(ns_uuid),
    )
    if row is None or row["extracted"] is None:
        return None
    extracted = row["extracted"]
    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    return hashlib.sha256(canonical_json(extracted)).hexdigest()


# ---------------------------------------------------------------------------
# Graph helper — AGREEMENT_SIGNATURE node + has_signature edge
# ---------------------------------------------------------------------------


async def _upsert_signature_node(
    conn: asyncpg.Connection,
    ns_uuid: uuid.UUID,
    *,
    agreement_uuid: uuid.UUID,
    session_id: str,
) -> str:
    """Upsert an AGREEMENT_SIGNATURE node and link it under its AGREEMENT.

    Label format: ``AgreementSignature:<agreement_id>:<session_id>``.  Follows
    the ``graph.upsert_agreement_node`` template (assert_owner + INSERT ...
    ON CONFLICT + emit_graph_write) but for the ``AGREEMENT_SIGNATURE`` type
    (owner ``agreements``, already declared in node-ownership.json).
    """
    await assert_owner(conn, ns_uuid, "AGREEMENT_SIGNATURE", _AGREEMENTS_ENGINE)

    label = f"AgreementSignature:{agreement_uuid}:{session_id}"
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, agreements_source_id, change_origin)
        VALUES ($1, 'AGREEMENT_SIGNATURE', $2::uuid, $3, 'agent')
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                agreements_source_id = COALESCE(EXCLUDED.agreements_source_id, kg_nodes.agreements_source_id),
                updated_at = NOW()
        """,
        label,
        str(ns_uuid),
        str(agreement_uuid),
    )

    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type="AGREEMENT_SIGNATURE",
        op="upserted",
        node_id=label,
    )

    # Link: AGREEMENT -[has_signature]-> AGREEMENT_SIGNATURE.
    await upsert_agreement_edge(
        conn,
        ns_uuid,
        subject_label=f"Agreement:{agreement_uuid}",
        predicate="has_signature",
        object_label=label,
        confidence=1.0,
        agreements_source_id=str(agreement_uuid),
    )
    return label


async def _resolve_agreement_memory_id(
    conn: Any,
    ns_uuid: uuid.UUID,
    agreement_id_str: str,
) -> str | None:
    """Resolve the current agreement-text memory id, or ``None`` when absent.

    ``graph.store_agreement_text_memory`` stamps ``metadata->>'agreement_id'``
    on the memory it writes.  When no such memory exists in scope we return
    ``None`` and the caller skips ``verify_memory`` gracefully — we never
    fabricate a memory_id.
    """
    row = await conn.fetchrow(
        """
        SELECT id
        FROM   memories
        WHERE  namespace_id = $1::uuid
          AND  metadata->>'agreement_id' = $2
          AND  valid_to IS NULL
        ORDER BY created_at DESC
        LIMIT  1
        """,
        str(ns_uuid),
        agreement_id_str,
    )
    return str(row["id"]) if row is not None else None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def do_request_signature(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Open a signing session for an agreement and record the request.

    Parameters
    ----------
    engine:
        NCEEngine instance (may carry an injected ``sign_transport``; a test
        stub only needs ``pg_pool``).
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "agreement_id": str | UUID,   # required
            "document":     str | bytes,  # required — the contract content
            "signer":       str,          # required — signer email / id
        }``

    Returns
    -------
    dict::

        {
            "status": "ok",
            "agreement_id":     str,
            "session_id":       str,
            "fingerprint":      str,   # SHA-256 hex of the document bytes
            "signature_status": "pending",
        }

    Raises ``ValueError`` on a missing/invalid required param.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))
    agreement_uuid = uuid.UUID(_require_str(params, "agreement_id"))
    signer = _require_str(params, "signer")
    if params.get("document") is None or (
        isinstance(params["document"], str) and not params["document"].strip()
    ):
        raise ValueError("document is required")
    doc = _doc_bytes(params["document"])

    # 1. Tamper-evidence handle over the original document bytes.
    fingerprint = sha256_fingerprint(doc)

    # 2. Open a signing session on the transport (C7).
    transport = _get_transport(engine)
    session = transport.request_signature(doc, {"id": signer}, _METHOD)
    session_id = str(session["session_id"])

    # 3. Graph write + append-only ledger record (one transaction).
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Bind the request to the money terms as they stand RIGHT NOW.  The
        # document is free-form; `extracted` is what compliance/kickback
        # actually authorise money from, so record its canonical fingerprint
        # and re-check it at record time.
        terms_fingerprint = await _compute_terms_fingerprint(conn, ns_uuid, str(agreement_uuid))
        await _upsert_signature_node(
            conn,
            ns_uuid,
            agreement_uuid=agreement_uuid,
            session_id=session_id,
        )
        await _append_signature_ledger(
            conn,
            ns_uuid,
            {
                "agreement_id": str(agreement_uuid),
                "kind": _KIND_REQUEST,
                "session_id": session_id,
                "signer": signer,
                "fingerprint": fingerprint,
                "terms_fingerprint": terms_fingerprint,
                "status": _STATUS_PENDING,
            },
        )

    log.info(
        "do_request_signature: opened session=%s agreement=%s ns=%s",
        session_id,
        agreement_uuid,
        ns_uuid,
    )
    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "session_id": session_id,
        "fingerprint": fingerprint,
        "signature_status": _STATUS_PENDING,
    }


async def do_record_signature(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record a completed signature after a tamper check on the signed document.

    Parameters
    ----------
    engine:
        NCEEngine instance (may carry ``sign_transport`` + ``verify_memory``).
    params:
        ``{
            "namespace_id":    str | UUID,   # required
            "agreement_id":    str | UUID,   # required
            "session_id":      str,          # required — from do_request_signature
            "signed_document": str | bytes,  # required — the returned signed content
            "signer":          str,          # required
        }``

    Returns
    -------
    dict
        On a matching fingerprint::

            {
                "status": "ok",
                "agreement_id":     str,
                "session_id":       str,
                "signature_status": "signed",
                "fingerprint":      str,
            }

        Early-exit shapes (NO transport call, NO signed node, NO ledger write):

        - ``{"status": "session_not_found", "agreement_id", "session_id"}`` —
          no ``signature_request`` row for this agreement+session.
        - ``{"status": "fingerprint_mismatch", "agreement_id", "session_id",
          "expected_fingerprint", "actual_fingerprint"}`` — the signed document
          differs from the one that was requested; a tampered document must
          never record as signed.

    Raises ``ValueError`` on a missing/invalid required param.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))
    agreement_uuid = uuid.UUID(_require_str(params, "agreement_id"))
    session_id = _require_str(params, "session_id")
    signer = _require_str(params, "signer")
    if params.get("signed_document") is None or (
        isinstance(params["signed_document"], str) and not params["signed_document"].strip()
    ):
        raise ValueError("signed_document is required")
    signed_doc = _doc_bytes(params["signed_document"])
    agreement_id_str = str(agreement_uuid)

    # 1. TAMPER CHECK FIRST — recompute the fingerprint of the signed document
    #    and compare it to the fingerprint recorded at request time.  This runs
    #    before ANY transport call or ledger write.
    actual_fingerprint = sha256_fingerprint(signed_doc)
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        request_payload = await _read_request_payload(conn, ns_uuid, agreement_id_str, session_id)
        current_terms_fingerprint = await _compute_terms_fingerprint(
            conn, ns_uuid, agreement_id_str
        )

    expected_fingerprint = (
        str(request_payload.get("fingerprint"))
        if request_payload is not None and request_payload.get("fingerprint") is not None
        else None
    )

    if expected_fingerprint is None:
        log.info(
            "do_record_signature: no signature_request for agreement=%s session=%s ns=%s",
            agreement_uuid,
            session_id,
            ns_uuid,
        )
        return {
            "status": "session_not_found",
            "agreement_id": agreement_id_str,
            "session_id": session_id,
        }

    # Signer binding — the session was opened FOR a specific signer.  Recording
    # a different signer would let one party's signature be attributed to
    # another (and `reviewed_by` on the §9.3 promotion records that name).
    requested_signer = request_payload.get("signer") if request_payload else None
    if requested_signer is not None and str(requested_signer) != signer:
        log.warning(
            "do_record_signature: signer mismatch agreement=%s session=%s ns=%s "
            "(requested=%s recorded=%s) — refusing to record as signed",
            agreement_uuid,
            session_id,
            ns_uuid,
            requested_signer,
            signer,
        )
        return {
            "status": "signer_mismatch",
            "agreement_id": agreement_id_str,
            "session_id": session_id,
            "expected_signer": str(requested_signer),
            "actual_signer": signer,
        }

    # Terms binding — the signature must attest to the SPECIFIC money terms in
    # play when it was requested.  A document fingerprint alone proves only that
    # some bytes are unchanged; it says nothing about `extracted`, which is what
    # the §9.3 promotion below makes reconcilable.  If the terms moved between
    # request and record, this signature does not cover them.
    expected_terms_fingerprint = (
        request_payload.get("terms_fingerprint") if request_payload else None
    )
    if current_terms_fingerprint != expected_terms_fingerprint:
        log.warning(
            "do_record_signature: terms changed since request agreement=%s session=%s ns=%s "
            "(requested_terms=%s current_terms=%s) — refusing to record as signed",
            agreement_uuid,
            session_id,
            ns_uuid,
            expected_terms_fingerprint,
            current_terms_fingerprint,
        )
        return {
            "status": "terms_changed",
            "agreement_id": agreement_id_str,
            "session_id": session_id,
            "expected_terms_fingerprint": expected_terms_fingerprint,
            "actual_terms_fingerprint": current_terms_fingerprint,
        }

    if actual_fingerprint != expected_fingerprint:
        # Tampered / substituted document — record NOTHING.
        log.warning(
            "do_record_signature: fingerprint mismatch agreement=%s session=%s ns=%s "
            "(expected=%s actual=%s) — refusing to record as signed",
            agreement_uuid,
            session_id,
            ns_uuid,
            expected_fingerprint,
            actual_fingerprint,
        )
        return {
            "status": "fingerprint_mismatch",
            "agreement_id": agreement_id_str,
            "session_id": session_id,
            "expected_fingerprint": expected_fingerprint,
            "actual_fingerprint": actual_fingerprint,
        }

    # 2. Fingerprint matches — record the signature on the transport, then in
    #    the graph/ledger.  Signed-state lives in the ledger row (kg_nodes has
    #    no value column).
    transport = _get_transport(engine)
    transport.on_signed(
        session_id,
        {
            "agreement_id": agreement_id_str,
            "fingerprint": actual_fingerprint,
            "signer": signer,
        },
    )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await _append_signature_ledger(
            conn,
            ns_uuid,
            {
                "agreement_id": agreement_id_str,
                "kind": _KIND_RECORDED,
                "session_id": session_id,
                "signer": signer,
                "fingerprint": actual_fingerprint,
                "status": _STATUS_SIGNED,
            },
        )
        # Mark the agreement's lifecycle state SIGNED in the graph (mirrors
        # B111's DRAFT term).  AGREEMENT_TERM is owned by the agreements engine.
        await upsert_agreement_term_node(
            conn,
            ns_uuid,
            agreement_id=agreement_uuid,
            term_type="lifecycleState",
            value=_STATUS_SIGNED.upper(),
            confidence=1.0,
            agreements_source_id=agreement_id_str,
        )
        # §9.3 promotion: a recorded signature IS the human sign-off that money
        # terms require, so promote the agreement's review-queue row to
        # ``auto_green`` — this is what makes an authored DRAFT (B111 writes it
        # ``needs_review_yellow``) reconcilable by kickback.  Affects 0 rows
        # harmlessly when no queue row exists (e.g. an already-signed agreement).
        #
        # NEVER override an explicit human reject: a ``manual_red`` row is a
        # deliberate veto (review.py), and a countersignature must not silently
        # flip it reconcilable — that would need an explicit re-review, not a
        # signature.  The guard advances needs_review_yellow → auto_green (and is
        # an idempotent no-op on an already-auto_green row).
        await conn.execute(
            """
            UPDATE agreement_review_queue
            SET    review_status = 'auto_green',
                   reviewed_by = $3,
                   reviewed_at = now()
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
              AND  review_status <> 'manual_red'
            """,
            agreement_uuid,
            str(ns_uuid),
            signer,
        )
        memory_id = await _resolve_agreement_memory_id(conn, ns_uuid, agreement_id_str)

    # 3. Best-effort integrity check on the agreement's memory.  Skip gracefully
    #    (log) when no memory is resolvable — never fabricate a memory_id.
    if memory_id is not None and hasattr(engine, "verify_memory"):
        try:
            verify_result = await engine.verify_memory(memory_id)
            log.info(
                "do_record_signature: verify_memory(%s) agreement=%s -> %s",
                memory_id,
                agreement_uuid,
                verify_result.get("valid") if isinstance(verify_result, dict) else verify_result,
            )
        except Exception:
            log.warning(
                "do_record_signature: verify_memory failed for memory=%s agreement=%s",
                memory_id,
                agreement_uuid,
                exc_info=True,
            )
    else:
        log.info(
            "do_record_signature: no resolvable memory for agreement=%s ns=%s — "
            "skipping verify_memory",
            agreement_uuid,
            ns_uuid,
        )

    log.info(
        "do_record_signature: recorded signature session=%s agreement=%s ns=%s",
        session_id,
        agreement_uuid,
        ns_uuid,
    )
    return {
        "status": "ok",
        "agreement_id": agreement_id_str,
        "session_id": session_id,
        "signature_status": _STATUS_SIGNED,
        "fingerprint": actual_fingerprint,
    }


async def get_signature_history(
    pool: asyncpg.Pool,
    namespace_id: str | uuid.UUID,
    agreement_id: str | uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only, newest-first signature-event history for one agreement.

    Each entry is one append-only ledger row written by
    ``do_request_signature`` / ``do_record_signature``.  Namespace-scoped with
    an explicit predicate (never RLS-only).

    Returns a list of::

        {
            "ledger_id":       str,           # v3_cognitive_ledger.id
            "agreement_id":    str,
            "kind":            str,           # signature_request | signature_recorded
            "session_id":      str,
            "signer":          str,
            "fingerprint":     str,
            "status":          str,           # pending | signed | declined
            "recorded_at_iso": str,           # DB clock at event time
            "created_at_iso":  str,           # ledger row created_at
        }
    """
    ns_uuid = uuid.UUID(str(namespace_id))
    agreement_id_str = str(uuid.UUID(str(agreement_id)))

    async with scoped_pg_session(pool, ns_uuid) as conn:
        rows = await conn.fetch(
            """
            SELECT id, tlx_scores, created_at
            FROM   v3_cognitive_ledger
            WHERE  namespace_id = $1::uuid
              AND  model_version = $2
              AND  tlx_scores->>'agreement_id' = $3
            ORDER BY created_at DESC
            LIMIT  $4
            """,
            str(ns_uuid),
            _MODEL_VERSION,
            agreement_id_str,
            limit,
        )

    history: list[dict[str, Any]] = []
    for row in rows:
        payload = row["tlx_scores"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload or {}
        history.append(
            {
                "ledger_id": str(row["id"]),
                "agreement_id": payload.get("agreement_id"),
                "kind": payload.get("kind"),
                "session_id": payload.get("session_id"),
                "signer": payload.get("signer"),
                "fingerprint": payload.get("fingerprint"),
                "status": payload.get("status"),
                "recorded_at_iso": payload.get("recorded_at_iso"),
                "created_at_iso": row["created_at"].isoformat(),
            }
        )
    return history
