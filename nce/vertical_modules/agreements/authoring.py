"""
nce/vertical_modules/agreements/authoring.py
=============================================
Agreement authoring + negotiation core — Module 3.Wave 7 ("create-negotiate").

This is the **authored** (Oneflow CLM) path, the counterpart to the OCR/incoming
path in ``extract.py`` + ``review.py``.  We author these agreements ourselves,
so — unlike OCR'd documents — there is NO field-level extraction confidence, NO
``needs_review_yellow``/``manual_red`` gate, and NO ``agreement_review_queue``
row.  Everything is written straight into the cognitive graph at confidence 1.0.

Three concepts are lifted natively onto the existing NCE primitives:

  1. **Create** (``do_create_agreement``) — Agreements is the SOLE writer of
     AGREEMENT / AGREEMENT_TERM nodes (node-ownership.json), so creation reuses
     the B107 graph writers (``graph.upsert_agreement_node`` +
     ``graph.upsert_agreement_term_node``) verbatim.  Lifecycle state is stored
     **graph-natively** as a ``lifecycleState`` AGREEMENT_TERM node — kg_nodes
     has no arbitrary attribute columns and every other agreement attribute is a
     term node, so this is the established pattern, not an improvisation.  A new
     agreement is born ``DRAFT``.

  2. **Suggest a revision** (``do_suggest_revision``) — an ADVISOR action that is
     strictly PROPOSE-ONLY: it never mutates the AGREEMENT node or any
     AGREEMENT_TERM.  A revision proposal is recorded as one append-only row in
     ``v3_cognitive_ledger`` and applied by nobody automatically.

  3. **Add a comment** (``do_add_comment``) — negotiation commentary, likewise a
     single append-only ``v3_cognitive_ledger`` row with no graph mutation.

Design invariants (mirrors kickback.py / uncle-bob-craft)
---------------------------------------------------------
- SRP per function; one append helper (``_append_ledger_entry``) is reused by
  both propose-only entry points (DRY).
- Dependencies point inward: nothing is imported from admin_handlers.
- Every SQL query carries an explicit ``namespace_id = $N::uuid`` predicate —
  never RLS-only (owner-pool test roles can bypass FORCE RLS; repo lesson).
- Ledger writes are append-only INSERTs; this module never UPDATEs or DELETEs a
  ``v3_cognitive_ledger`` row (append-only audit trail).
- The recorded timestamp comes from the DB clock (``SELECT now()``), not the
  client clock, so the audit trail is single-sourced.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.agreements.graph import (
    store_agreement_text_memory,
    upsert_agreement_node,
    upsert_agreement_term_node,
)

log = logging.getLogger("nce.vertical_modules.agreements.authoring")

# model_version discriminator for this module's v3_cognitive_ledger rows.
_MODEL_VERSION = "agreements-authoring-v1"

# Zero tensor matching the NOT NULL empathic_tensor column (vector(6) in the
# live schema — array→vector assignment cast) — mirrors kickback.py.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Payload ``kind`` discriminators inside tlx_scores.
_KIND_SUGGESTION = "revision_suggestion"
_KIND_COMMENT = "comment"

# Graph-native lifecycle: a dedicated AGREEMENT_TERM node holds the state.
_LIFECYCLE_TERM_TYPE = "lifecycleState"
_LIFECYCLE_DRAFT = "DRAFT"

# ``agreement_review_queue`` is the SOLE machine-readable term store — lookup
# (B109), coverage (B108) and kickback (B110) all read structured terms from
# ``agreement_review_queue.extracted`` (kg_nodes has no attribute column, so the
# AGREEMENT_TERM graph nodes are identity/edge anchors only, and the text memory
# is unstructured).  An authored agreement therefore also lands a review-queue
# row so it is first-class readable immediately.
#
# The row is born ``needs_review_yellow`` — NOT because OCR needs reviewing, but
# because §9.3 requires a human signature before money/legal terms may reconcile
# against real GL: kickback's gate reconciles ONLY ``auto_green`` rows, so an
# unsigned DRAFT is intentionally non-reconcilable.  Signing (W9,
# ``do_record_signature``) promotes the row to ``auto_green``.
_DRAFT_ROW_STATUS = "needs_review_yellow"
# Authored terms are human-entered → full extraction confidence (0–100 scale,
# matching extract.py's ``extractionConfidence``).
_AUTHORED_CONFIDENCE = 100.0

# The money/legal term fields written as AGREEMENT_TERM nodes on creation.
# Mirrors the term list in graph.write_agreement_to_graph_and_memories exactly:
# supplierId / customerId are NOT term nodes — they drive the Vendor/Customer
# ``under`` edges via the dedicated supplier_id / customer_id params.
_KNOWN_TERM_FIELDS: tuple[str, ...] = (
    "validFrom",
    "validTo",
    "paymentTermsDays",
    "frameDiscountPct",
    "volumeCommitment",
    "kickbackTiers",
)


def _authored_field(value: Any) -> dict[str, Any]:
    """One extracted field in the nested shape lookup/coverage/kickback read.

    Mirrors extract.py's per-field ``{value, extractionConfidence, reviewStatus}``.
    Authored fields carry full confidence but the DRAFT review status — they are
    human-entered yet not yet signed.
    """
    return {
        "value": value,
        "extractionConfidence": _AUTHORED_CONFIDENCE,
        "reviewStatus": _DRAFT_ROW_STATUS,
    }


def _build_extracted(
    supplier_id: str | None,
    customer_id: str | None,
    terms: dict[str, Any],
    terms_written: list[str],
) -> dict[str, Any]:
    """Assemble the ``extracted`` JSONB for the review-queue row.

    Keys match what the readers expect: ``supplierId`` (kickback/coverage
    reconcile supplier identity from it) + ``customerId`` + each written term.
    """
    extracted: dict[str, Any] = {}
    if supplier_id:
        extracted["supplierId"] = _authored_field(supplier_id)
    if customer_id:
        extracted["customerId"] = _authored_field(customer_id)
    for field in terms_written:
        extracted[field] = _authored_field(terms[field])
    return extracted


# ---------------------------------------------------------------------------
# Append-only ledger helper — reused by suggest + comment (DRY)
# ---------------------------------------------------------------------------


async def _append_ledger_entry(
    conn: Any,
    ns_uuid: uuid.UUID,
    payload: dict[str, Any],
) -> str:
    """Append ONE row to ``v3_cognitive_ledger`` and return its id (append-only).

    ``payload`` must already carry ``agreement_id`` and ``kind``; the recorded
    timestamp is stamped here from the DB clock (``SELECT now()``) so the audit
    trail is single-sourced.  Rows written here are never mutated or removed by
    this module.
    """
    ledger_id = uuid.uuid4()
    recorded_at = await conn.fetchval("SELECT now()")
    full_payload: dict[str, Any] = {**payload, "recorded_at_iso": recorded_at.isoformat()}
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


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def do_create_agreement(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Author a new agreement graph-natively (the Oneflow CLM path).

    Authored agreements are NOT OCR'd, so there is no confidence review and no
    ``agreement_review_queue`` row — nodes are written straight to the graph at
    confidence 1.0.  Agreements is the SOLE writer of AGREEMENT / AGREEMENT_TERM.

    Parameters
    ----------
    engine:
        NCEEngine instance (or test stub) providing ``pg_pool``.
    params:
        ``{
            "namespace_id":  str | UUID,          # required
            "supplier_id":   str | None,          # drives Vendor -under-> edge
            "customer_id":   str | None,          # drives Customer -under-> edge
            "terms":         dict | None,         # flat money/legal term values
            "agreement_id":  str | UUID | None,   # generated when absent
        }``

        ``terms`` is a flat dict of the known money/legal fields, e.g.
        ``{"paymentTermsDays": 30, "frameDiscountPct": 2.5,
           "kickbackTiers": [{"threshold": 100000, "pct": 3.0}]}``.
        Unknown keys are ignored.

    Returns
    -------
    dict::

        {
            "status":         "ok",
            "agreement_id":   str,
            "lifecycle_state": "DRAFT",
            "review_status":  "needs_review_yellow",  # not auto_green until signed
            "terms_written":  [<field names actually written>],
            "memory_id":      str | None,   # searchable memory persisting the values
            "label":          "Agreement:<agreement_id>",
        }

    The drafted term VALUES are persisted to ``agreement_review_queue.extracted``
    — the structured store lookup (B109), coverage (B108) and kickback (B110)
    read — so an authored agreement is first-class readable and reconcilable
    (after signing) without any Oneflow round-trip.  A searchable text memory is
    also written (the AGREEMENT_TERM graph nodes are identity anchors with no
    attribute column).  The review-queue row is born ``needs_review_yellow`` so
    kickback's §9.3 gate keeps the unsigned DRAFT non-reconcilable until signing
    (W9) promotes it to ``auto_green``.
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))

    supplier_id = params.get("supplier_id")
    customer_id = params.get("customer_id")

    terms = params.get("terms") or {}
    if not isinstance(terms, dict):
        raise ValueError("terms must be a JSON object (dict) when provided")

    agreement_id_raw = params.get("agreement_id")
    agreement_uuid = uuid.UUID(str(agreement_id_raw)) if agreement_id_raw else uuid.uuid4()
    source_id = str(agreement_uuid)

    terms_written: list[str] = []
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # 1. AGREEMENT node (+ Vendor/Customer ``under`` edges when provided).
        await upsert_agreement_node(
            conn,
            ns_uuid,
            agreement_id=agreement_uuid,
            supplier_id=supplier_id,
            customer_id=customer_id,
            agreements_source_id=source_id,
        )

        # 2. Graph-native lifecycle: a DRAFT ``lifecycleState`` term node.
        await upsert_agreement_term_node(
            conn,
            ns_uuid,
            agreement_id=agreement_uuid,
            term_type=_LIFECYCLE_TERM_TYPE,
            value=_LIFECYCLE_DRAFT,
            confidence=1.0,
            agreements_source_id=source_id,
        )

        # 3. Money/legal term nodes — known field set only, unknown keys ignored.
        for field in _KNOWN_TERM_FIELDS:
            if field not in terms:
                continue
            val = terms[field]
            if val is None:
                continue
            val_str = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
            await upsert_agreement_term_node(
                conn,
                ns_uuid,
                agreement_id=agreement_uuid,
                term_type=field,
                value=val_str,
                confidence=1.0,
                agreements_source_id=source_id,
            )
            terms_written.append(field)

        # 4. Persist the terms to the structured review-queue store — the sole
        #    machine-readable term store that lookup (B109), coverage (B108) and
        #    kickback (B110) read.  review_status = needs_review_yellow keeps this
        #    unsigned DRAFT OUT of kickback's §9.3 reconcile gate until signing
        #    (W9) flips the row to auto_green.
        #
        #    ON CONFLICT must never leave a row asserting "signed" over terms no
        #    human signed.  `extracted` is the money/legal blob compliance.py and
        #    kickback.py treat as signed ground truth, so whenever it actually
        #    CHANGES the row falls back to needs_review_yellow and must be
        #    re-signed before it can authorise money again.  Two carve-outs keep
        #    that from becoming its own bug:
        #      - terms unchanged (idempotent re-author) → status preserved, so a
        #        harmless replay never invalidates a real signature;
        #      - manual_red preserved unconditionally → a human veto is never
        #        lifted by re-authoring (yellow would be an UPGRADE from red).
        extracted = _build_extracted(supplier_id, customer_id, terms, terms_written)
        await conn.execute(
            """
            INSERT INTO agreement_review_queue (
                agreement_id, namespace_id, source_doc_ref,
                extraction_confidence, review_status, extracted
            ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)
            ON CONFLICT (agreement_id, namespace_id) DO UPDATE
                SET extracted = EXCLUDED.extracted,
                    source_doc_ref = EXCLUDED.source_doc_ref,
                    extraction_confidence = EXCLUDED.extraction_confidence,
                    review_status = CASE
                        WHEN agreement_review_queue.extracted
                             IS NOT DISTINCT FROM EXCLUDED.extracted
                            THEN agreement_review_queue.review_status
                        WHEN agreement_review_queue.review_status = 'manual_red'
                            THEN agreement_review_queue.review_status
                        ELSE EXCLUDED.review_status
                    END
            """,
            agreement_uuid,
            str(ns_uuid),
            f"authored://{agreement_uuid}",
            _AUTHORED_CONFIDENCE,
            _DRAFT_ROW_STATUS,
            json.dumps(extracted),
        )

    # 5. Persist the authored terms as a searchable text memory.
    #    kg_nodes has no attribute column, so the AGREEMENT_TERM nodes above are
    #    identity anchors only — the term VALUES would otherwise be written
    #    nowhere retrievable.  Mirror the OCR path's compensating memory write
    #    (graph.write_agreement_to_graph_and_memories) so the drafted values are
    #    durable and answerable ("what are our payment terms with X") the moment
    #    the agreement is authored, not just after signing.
    summary_parts = [f"Agreement ID: {agreement_uuid}", f"Lifecycle: {_LIFECYCLE_DRAFT} (authored)"]
    if supplier_id:
        summary_parts.append(f"Supplier ID / Vendor: {supplier_id}")
    if customer_id:
        summary_parts.append(f"Customer ID: {customer_id}")
    for field in terms_written:
        val = terms[field]
        val_str = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
        summary_parts.append(f"{field}: {val_str}")
    memory_result = await store_agreement_text_memory(
        engine.pg_pool,
        ns_uuid,
        agreement_id=agreement_uuid,
        text="\n".join(summary_parts),
        source="agreement_authored",
        trigger="agent",
    )

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "lifecycle_state": _LIFECYCLE_DRAFT,
        "review_status": _DRAFT_ROW_STATUS,
        "terms_written": terms_written,
        "memory_id": memory_result.get("memory_id"),
        "label": f"Agreement:{agreement_uuid}",
    }


async def do_suggest_revision(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record a PROPOSE-ONLY revision suggestion for one agreement's field.

    This is an ADVISOR action: it MUST NOT modify the AGREEMENT node or any
    AGREEMENT_TERM.  It only appends one ``v3_cognitive_ledger`` row (kind
    ``revision_suggestion``).  Application is a separate, human-driven decision —
    nothing here auto-applies (``applied`` is always ``false``).

    Parameters
    ----------
    params:
        ``{
            "namespace_id":   str | UUID,   # required
            "agreement_id":   str | UUID,   # required
            "field":          str,          # required — the term field to change
            "proposed_value": Any,          # required — the proposed new value
            "rationale":      str | None,
            "author":         str | None,
        }``

    Returns
    -------
    dict::

        {"status": "ok", "agreement_id": str, "suggestion_id": str, "applied": False}
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))

    agreement_id_raw = params.get("agreement_id")
    if not agreement_id_raw:
        raise ValueError("agreement_id is required")
    agreement_uuid = uuid.UUID(str(agreement_id_raw))

    field = params.get("field")
    if not field or not isinstance(field, str):
        raise ValueError("field is required")

    # proposed_value may legitimately be falsy (0, "", False) — require the key
    # to be present with a non-None value rather than testing truthiness.
    if params.get("proposed_value") is None:
        raise ValueError("proposed_value is required")
    proposed_value = params.get("proposed_value")

    payload: dict[str, Any] = {
        "agreement_id": str(agreement_uuid),
        "kind": _KIND_SUGGESTION,
        "field": field,
        "proposed_value": proposed_value,
        "rationale": params.get("rationale"),
        "author": params.get("author"),
    }
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        suggestion_id = await _append_ledger_entry(conn, ns_uuid, payload)

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "suggestion_id": suggestion_id,
        "applied": False,
    }


async def do_add_comment(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Record a PROPOSE-ONLY negotiation comment on one agreement.

    Appends one ``v3_cognitive_ledger`` row (kind ``comment``) and performs no
    graph mutation whatsoever.

    Parameters
    ----------
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "agreement_id": str | UUID,   # required
            "comment":      str,          # required
            "author":       str | None,
        }``

    Returns
    -------
    dict::

        {"status": "ok", "agreement_id": str, "comment_id": str}
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))

    agreement_id_raw = params.get("agreement_id")
    if not agreement_id_raw:
        raise ValueError("agreement_id is required")
    agreement_uuid = uuid.UUID(str(agreement_id_raw))

    comment = params.get("comment")
    if not comment or not isinstance(comment, str):
        raise ValueError("comment is required")

    payload: dict[str, Any] = {
        "agreement_id": str(agreement_uuid),
        "kind": _KIND_COMMENT,
        "comment": comment,
        "author": params.get("author"),
    }
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        comment_id = await _append_ledger_entry(conn, ns_uuid, payload)

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "comment_id": comment_id,
    }


async def get_agreement_activity(
    pool: asyncpg.Pool,
    namespace_id: str | uuid.UUID,
    agreement_id: str | uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only, newest-first suggestion + comment activity for one agreement.

    Reads the append-only ledger rows written by ``do_suggest_revision`` and
    ``do_add_comment`` — both share ``model_version == _MODEL_VERSION``, so
    filtering on that plus the ``agreement_id`` payload key captures exactly the
    two kinds.  Namespace-scoped with an explicit predicate (never RLS-only).

    Returns a list of::

        {
            "ledger_id":       str,           # v3_cognitive_ledger.id
            "agreement_id":    str,
            "kind":            str,           # revision_suggestion | comment
            "field":           str | None,    # suggestions only
            "proposed_value":  Any,           # suggestions only
            "rationale":       str | None,    # suggestions only
            "comment":         str | None,    # comments only
            "author":          str | None,
            "recorded_at_iso": str,           # DB clock at record time
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

    activity: list[dict[str, Any]] = []
    for row in rows:
        payload = row["tlx_scores"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload or {}
        activity.append(
            {
                "ledger_id": str(row["id"]),
                "agreement_id": payload.get("agreement_id"),
                "kind": payload.get("kind"),
                "field": payload.get("field"),
                "proposed_value": payload.get("proposed_value"),
                "rationale": payload.get("rationale"),
                "comment": payload.get("comment"),
                "author": payload.get("author"),
                "recorded_at_iso": payload.get("recorded_at_iso"),
                "created_at_iso": row["created_at"].isoformat(),
            }
        )
    return activity
