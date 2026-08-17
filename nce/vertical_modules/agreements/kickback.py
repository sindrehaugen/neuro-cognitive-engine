"""
nce/vertical_modules/agreements/kickback.py
=============================================
Kickback-tier reconciliation for the Agreements vertical module — M3.W6.

``do_reconcile_kickback`` reconciles ONE agreement's kickback-tier terms
against real Economy GL spend and reports:

  - ``spend_to_date_nok``   — GL spend attributed to the agreement's supplier.
  - ``earned_to_date_nok``  — kickback earned at the active tier
                              (retroactive-on-total: ``spend × active_pct / 100``).
  - ``to_next_tier_nok``    — spend gap to the next tier (None at top tier).
  - ``projection_drift_nok``— earned minus the caller-supplied projection.
  - ``term_drift``          — whether the money terms changed since the last
                              recorded snapshot.

This is a MONEY wave — §9.3 discipline is the core design constraint:

  1. **Sign-off gate FIRST.**  Money/legal fields (kickbackTiers,
     frameDiscountPct, paymentTermsDays, volumeCommitment) NEVER auto-green
     at extraction (see ``extract.py:_map_confidence_to_status``).  The only
     way a row reaches ``review_status = 'auto_green'`` with money fields
     present is a human 'confirm' decision (``review.py``).  Row-level
     ``auto_green`` is therefore the machine-checkable proxy for "a human
     signed off on the money fields".  Anything else returns
     ``status="unconfirmed_terms"`` and does NOTHING else — no GL read,
     no math, no ledger write.
  2. **Identity via C1, never raw strings.**  GL rows are attributed to the
     agreement's supplier only when BOTH sides resolve to the same canonical
     VENDOR kg_node via ``coverage._resolve_vendor_node_id`` (the
     vendors/registry.py gate(>=0.2) + exact-suffix-confirm pattern — the
     B108 anti-false-match lesson).  Unresolvable sides never match
     (``None == None`` is explicitly guarded against).
  3. **Auditable term history.**  Every successful reconcile snapshots the
     money terms into ``v3_cognitive_ledger`` (append-only INSERT mirroring
     ``procurement/recalibration.py``; never mutated or removed) when the
     terms differ from the latest recorded snapshot.  Timestamps come from
     the DB clock (``SELECT now()``), not the client clock.

pct convention (verified)
--------------------------
``kickbackTiers`` entries are ``{"threshold": float, "pct": float}`` where
``pct`` is a PERCENT — e.g. ``3.0`` means 3 % — per the extraction schema
(``extract.py:36``: "The rebate percentage for this tier").  Earned kickback
is therefore ``spend × pct / 100``.  Money arithmetic uses ``Decimal``
internally; the result dict emits floats.

GL data + test seam
--------------------
GL data is consumed via the A2A Economy-engine seam
``coverage._read_economy_gl_rows`` (raises ``NotImplementedError`` until
Module 8 ships).  The symbol is imported into THIS module's namespace, so
tests must patch ``nce.vertical_modules.agreements.kickback._read_economy_gl_rows``.
Any seam failure degrades gracefully to ``status="gl_unavailable"`` (terms
echoed, no earned math, no ledger write).

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per function; pure tier math is DB-free and unit-testable.
- Dependencies point inward: nothing imported from admin_handlers.
- Explicit ``namespace_id = $N`` predicate on every SQL query (no RLS-only
  reliance; owner-pool test roles can bypass FORCE RLS).
- Ledger writes are append-only INSERTs; this module never mutates or
  removes ledger rows.
- Secrets never logged or hard-coded.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.agreements.coverage import (
    _parse_date_or_none,
    _read_economy_gl_rows,
    _resolve_vendor_node_id,
    _unwrap_field,
)

log = logging.getLogger("nce.vertical_modules.agreements.kickback")


class MalformedTermsError(ValueError):
    """Raised when confirmed money terms cannot be normalized without guessing.

    Surfaced to callers as ``status="malformed_terms"`` — a discarded tier
    understates earned kickback and a bool coerced to 1.0 can overstate it,
    so tier tables that fail strict normalization never reach the money math.
    """


# model_version discriminator for this module's v3_cognitive_ledger rows.
_MODEL_VERSION = "agreements-kickback-v1"

# Zero tensor matching the NOT NULL empathic_tensor column (float[6] in the
# live schema) — mirrors nce/vertical_modules/procurement/recalibration.py.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Payload discriminator inside tlx_scores for term-snapshot rows.
_SNAPSHOT_KIND = "term_snapshot"

# Money/legal fields captured in each term snapshot (the §9.3 field set from
# extract.py:_map_confidence_to_status).
_TERM_FIELDS: tuple[str, ...] = (
    "kickbackTiers",
    "volumeCommitment",
    "frameDiscountPct",
    "paymentTermsDays",
)

# Money results are quantized to øre (2 decimal places).
_MONEY_QUANT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Pure domain helpers — zero DB, zero HTTP
# ---------------------------------------------------------------------------


def _coerce_extracted(raw: Any) -> dict[str, Any]:
    """Coerce the ``extracted`` jsonb column (str | dict | None) to a dict."""
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


def _snapshot_terms(extracted: dict[str, Any]) -> dict[str, Any]:
    """Extract the money/legal term subset used for tier math + the snapshot.

    ``_unwrap_field`` tolerates both the nested per-field shape
    (``{"value": ..., "extractionConfidence": ..., "reviewStatus": ...}``)
    and flat corrected_terms values.
    """
    return {field: _unwrap_field(extracted, field) for field in _TERM_FIELDS}


def _normalize_tiers(raw_tiers: Any) -> list[dict[str, float]]:
    """Coerce raw kickbackTiers into a threshold-ascending list — FAIL CLOSED.

    Money math must never guess: any entry that cannot be normalized
    unambiguously raises :exc:`MalformedTermsError` instead of being silently
    skipped.  Corrected_terms reach this code verbatim and shape-unvalidated
    (review.py stores reviewer input as-is), so localized numerics
    (``"3,5"``, ``"1 000 000"``), bools (``float(True) == 1.0``) and
    duplicate thresholds are all real inputs — each would silently under- or
    overstate earned kickback if coerced or dropped.

    Absent/empty tiers are a valid no-tier agreement and return ``[]``.
    """
    if raw_tiers is None:
        return []
    if not isinstance(raw_tiers, list):
        raise MalformedTermsError(f"kickbackTiers is not a list: {type(raw_tiers).__name__}")
    tiers: list[dict[str, float]] = []
    for i, entry in enumerate(raw_tiers):
        if not isinstance(entry, dict):
            raise MalformedTermsError(f"tier[{i}] is not an object")
        threshold = entry.get("threshold")
        pct = entry.get("pct")
        if threshold is None or pct is None:
            raise MalformedTermsError(f"tier[{i}] missing threshold/pct")
        if isinstance(threshold, bool) or isinstance(pct, bool):
            raise MalformedTermsError(f"tier[{i}] has a boolean threshold/pct")
        try:
            threshold_f = float(threshold)
            pct_f = float(pct)
        except (TypeError, ValueError) as exc:
            raise MalformedTermsError(f"tier[{i}] threshold/pct not numeric: {exc}") from exc
        # float() accepts "nan"/"inf"/"-Infinity" — non-finite values would
        # poison the money math (NaN survives quantize) or create an
        # always-active tier, so they fail closed like any other guess.
        if not math.isfinite(threshold_f) or not math.isfinite(pct_f):
            raise MalformedTermsError(f"tier[{i}] has a non-finite threshold/pct")
        # Negative thresholds are nonsensical (an always-active tier); a
        # negative pct (malus clause) has no confirmed real-world case yet —
        # both are far more likely OCR/typo than intent, so fail closed
        # until a real malus agreement forces an explicit policy.
        if threshold_f < 0 or pct_f < 0:
            raise MalformedTermsError(f"tier[{i}] has a negative threshold/pct")
        tiers.append({"threshold": threshold_f, "pct": pct_f})
    tiers.sort(key=lambda t: t["threshold"])
    for prev, nxt in zip(tiers, tiers[1:]):
        if prev["threshold"] == nxt["threshold"]:
            raise MalformedTermsError(
                f"duplicate tier threshold {prev['threshold']} — ambiguous pct"
            )
    return tiers


def _tier_progression(tiers: list[dict[str, float]], spend: Decimal) -> dict[str, Any]:
    """Compute active/next tier + earned/to-next amounts for a total spend.

    Retroactive-on-total model: earned = ``spend × active_pct / 100`` where
    the active tier is the highest tier whose threshold <= spend (singular
    active tier).  ``pct`` is a PERCENT (extract.py:36), hence the ``/ 100``.

    Below the first tier: earned 0, active None.  At the top tier: next None,
    to_next None.  ``tiers`` must be threshold-ascending (see
    ``_normalize_tiers``).
    """
    active: dict[str, float] | None = None
    nxt: dict[str, float] | None = None
    for tier in tiers:
        if Decimal(str(tier["threshold"])) <= spend:
            active = tier
        else:
            nxt = tier
            break

    if active is not None:
        earned = (spend * Decimal(str(active["pct"])) / Decimal("100")).quantize(
            _MONEY_QUANT, ROUND_HALF_UP
        )
    else:
        earned = Decimal("0")

    to_next: Decimal | None = None
    if nxt is not None:
        to_next = (Decimal(str(nxt["threshold"])) - spend).quantize(_MONEY_QUANT, ROUND_HALF_UP)

    return {
        "active_tier": active,
        "next_tier": nxt,
        "earned_to_date": earned,
        "to_next_tier": to_next,
    }


def _within_period(gl_date_raw: Any, since: date | None, until: date | None) -> bool:
    """Return True when a GL row's date falls inside the requested bounds.

    Bounds are inclusive.  When bounds are requested but the row's date is
    unparseable, the row is EXCLUDED — money attribution must never guess a
    row into a period.  Without bounds every row passes.
    """
    if since is None and until is None:
        return True
    gl_date = _parse_date_or_none(gl_date_raw)
    if gl_date is None:
        return False
    if since is not None and gl_date < since:
        return False
    if until is not None and gl_date > until:
        return False
    return True


def _terms_equal(a: Any, b: Any) -> bool:
    """Deterministic deep-equality for term dicts (JSON-canonical compare)."""
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Ledger helpers — append-only term-change history (mirrors recalibration.py)
# ---------------------------------------------------------------------------


async def _read_latest_term_snapshot(
    conn: Any,
    ns_uuid: uuid.UUID,
    agreement_id_str: str,
) -> dict[str, Any] | None:
    """Read the most recent term snapshot payload for one agreement, or None.

    Newest-first on ``created_at`` (the ledger's timestamp column — see
    migration 008 and recalibration.py's read-back).
    """
    row = await conn.fetchrow(
        """
        SELECT tlx_scores
        FROM   v3_cognitive_ledger
        WHERE  namespace_id = $1::uuid
          AND  model_version = $2
          AND  tlx_scores->>'kind' = $3
          AND  tlx_scores->>'agreement_id' = $4
        ORDER BY created_at DESC
        LIMIT  1
        """,
        str(ns_uuid),
        _MODEL_VERSION,
        _SNAPSHOT_KIND,
        agreement_id_str,
    )
    if row is None:
        return None
    payload = row["tlx_scores"]
    return json.loads(payload) if isinstance(payload, str) else payload


async def _append_term_snapshot(
    conn: Any,
    ns_uuid: uuid.UUID,
    agreement_id_str: str,
    terms: dict[str, Any],
) -> str:
    """Append one term-snapshot row to ``v3_cognitive_ledger`` (append-only).

    The recorded timestamp comes from the DB clock (``SELECT now()``), not
    the client clock, so the audit trail is single-sourced.  Rows written
    here are never mutated or removed by this module.
    """
    ledger_id = uuid.uuid4()
    recorded_at = await conn.fetchval("SELECT now()")
    payload: dict[str, Any] = {
        "agreement_id": agreement_id_str,
        "kind": _SNAPSHOT_KIND,
        "terms": terms,
        "recorded_at_iso": recorded_at.isoformat(),
    }
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
        json.dumps(payload),
        json.dumps({}),
        _MODEL_VERSION,
    )
    return str(ledger_id)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def do_reconcile_kickback(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile ONE agreement's kickback tiers against real Economy GL spend.

    §9.3 sign-off gate runs FIRST: unreviewed money terms must never
    reconcile against real GL.  Only a row-level ``review_status`` of
    ``auto_green`` (which, for money fields, only a human 'confirm' can
    produce) proceeds to GL math and the ledger snapshot.

    Parameters
    ----------
    engine:
        NCEEngine instance (passed to the A2A seam; may be a test stub).
    params:
        ``{
            "namespace_id":           str | UUID,    # required
            "agreement_id":           str | UUID,    # required
            "since_iso":              str | None,    # inclusive GL date lower bound
            "until_iso":              str | None,    # inclusive GL date upper bound
            "projected_kickback_nok": float | None,  # Procurement forecast (A2A later)
        }``

    Returns
    -------
    dict
        On success::

            {
                "status": "ok",
                "agreement_id":         str,
                "supplier_node_id":     str | None,   # resolved VENDOR kg_node
                "spend_to_date_nok":    float,
                "earned_to_date_nok":   float,        # spend × active_pct / 100
                "active_tier":          {"threshold": float, "pct": float} | None,
                "next_tier":            {"threshold": float, "pct": float} | None,
                "to_next_tier_nok":     float | None, # None at top tier
                "projection_drift_nok": float | None, # earned − projection
                "term_drift":           bool,         # terms changed vs last snapshot
                "gl_rows_matched":      int,
                "period":               {"since_iso": ..., "until_iso": ...},
            }

        Early-exit shapes (no GL math, no ledger write):

        - ``{"status": "not_found", "agreement_id"}`` — no review-queue row.
        - ``{"status": "unconfirmed_terms", "agreement_id", "review_status"}``
          — row not human-confirmed (§9.3 gate).
        - ``{"status": "malformed_terms", "agreement_id", "detail"}`` — the
          confirmed tier table cannot be normalized without guessing
          (localized numerics, bools, duplicate thresholds).
        - ``{"status": "gl_unavailable", "agreement_id", "terms", "period"}``
          — Economy seam not available / failed.

        Raises ``ValueError`` on a missing/invalid ``agreement_id`` or a
        supplied-but-unparseable ``since_iso``/``until_iso`` (a bad period
        bound must never silently widen to all-time spend).
    """
    namespace_id = require_namespace_id(params)
    ns_uuid = uuid.UUID(str(namespace_id))
    agreement_id_raw = params.get("agreement_id")
    if not agreement_id_raw:
        raise ValueError("agreement_id is required")
    agreement_uuid = uuid.UUID(str(agreement_id_raw))

    since_iso: str | None = params.get("since_iso") or None
    until_iso: str | None = params.get("until_iso") or None
    # A supplied-but-unparseable bound must FAIL, not silently widen the
    # period to all-time spend (that would mislabel earned kickback with
    # status "ok" — same strictness as the agreement_id validation above).
    since_date = _parse_date_or_none(since_iso)
    until_date = _parse_date_or_none(until_iso)
    if since_iso is not None and since_date is None:
        raise ValueError(f"since_iso is not a parseable ISO date: {since_iso!r}")
    if until_iso is not None and until_date is None:
        raise ValueError(f"until_iso is not a parseable ISO date: {until_iso!r}")
    # Validate the caller-supplied projection up-front (fail loud BEFORE any
    # DB read or ledger write — not with an InvalidOperation after step 6).
    projected: Any = params.get("projected_kickback_nok")
    projected_dec: Decimal | None = None
    if projected is not None:
        if isinstance(projected, bool):
            raise ValueError("projected_kickback_nok must be numeric, got bool")
        try:
            projected_dec = Decimal(str(projected))
        except InvalidOperation as exc:
            raise ValueError(f"projected_kickback_nok is not numeric: {projected!r}") from exc
        if not projected_dec.is_finite():
            raise ValueError(f"projected_kickback_nok is not finite: {projected!r}")
    period = {"since_iso": since_iso, "until_iso": until_iso}

    # Step 1: §9.3 sign-off gate FIRST — before ANY GL access or ledger write.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT review_status, extracted
            FROM   agreement_review_queue
            WHERE  agreement_id = $1 AND namespace_id = $2::uuid
            """,
            agreement_uuid,
            str(ns_uuid),
        )
    if row is None:
        return {"status": "not_found", "agreement_id": str(agreement_uuid)}
    if row["review_status"] != "auto_green":
        log.info(
            "do_reconcile_kickback: §9.3 gate blocked agreement=%s review_status=%s ns=%s",
            agreement_uuid,
            row["review_status"],
            ns_uuid,
        )
        return {
            "status": "unconfirmed_terms",
            "agreement_id": str(agreement_uuid),
            "review_status": row["review_status"],
        }

    # Step 2: unwrap money terms (nested per-field shape or flat values).
    # Fail closed on tier tables that cannot be normalized without guessing —
    # BEFORE any GL access or ledger write.
    extracted = _coerce_extracted(row["extracted"])
    terms = _snapshot_terms(extracted)
    try:
        tiers = _normalize_tiers(terms["kickbackTiers"])
    except MalformedTermsError as exc:
        log.warning(
            "do_reconcile_kickback: malformed confirmed terms agreement=%s ns=%s: %s",
            agreement_uuid,
            ns_uuid,
            exc,
        )
        return {
            "status": "malformed_terms",
            "agreement_id": str(agreement_uuid),
            "detail": str(exc),
        }
    supplier_raw = _unwrap_field(extracted, "supplierId")

    # Step 3: GL spend via the A2A Economy seam (graceful degrade).
    try:
        gl_rows = await _read_economy_gl_rows(engine, ns_uuid, since_iso=since_iso)
    except NotImplementedError:
        log.info(
            "do_reconcile_kickback: Economy engine not available (NotImplementedError) "
            "agreement=%s ns=%s — no earned math, no ledger write",
            agreement_uuid,
            ns_uuid,
        )
        return {
            "status": "gl_unavailable",
            "agreement_id": str(agreement_uuid),
            "terms": terms,
            "period": period,
        }
    except Exception:
        log.warning(
            "do_reconcile_kickback: A2A GL read failed agreement=%s ns=%s",
            agreement_uuid,
            ns_uuid,
            exc_info=True,
        )
        return {
            "status": "gl_unavailable",
            "agreement_id": str(agreement_uuid),
            "terms": terms,
            "period": period,
        }

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Resolve the agreement's supplier to a canonical VENDOR node (C1).
        supplier_node_id = await _resolve_vendor_node_id(
            conn,
            ns_uuid,
            raw_id=str(supplier_raw) if supplier_raw else None,
        )

        # Step 4: filter GL rows to THIS agreement's supplier by node identity.
        # Both sides must resolve to a node; None never matches None.
        # Amounts are strict: a missing/unparseable amount_nok is a seam-
        # contract violation — the row is SKIPPED and COUNTED (visible),
        # never guessed to 0 into the spend basis.
        spend = Decimal("0")
        gl_rows_matched = 0
        gl_rows_skipped = 0
        for gl_row in gl_rows:
            if not _within_period(gl_row.get("gl_date"), since_date, until_date):
                # An unparseable date under requested bounds is a skipped
                # row, not merely an out-of-period one — keep it VISIBLE.
                if (since_date is not None or until_date is not None) and _parse_date_or_none(
                    gl_row.get("gl_date")
                ) is None:
                    gl_rows_skipped += 1
                continue
            gl_node_id = await _resolve_vendor_node_id(
                conn,
                ns_uuid,
                raw_id=gl_row.get("supplier_id"),
            )
            if supplier_node_id is None or gl_node_id is None or gl_node_id != supplier_node_id:
                continue
            amount_raw = gl_row.get("amount_nok")
            if amount_raw is None or isinstance(amount_raw, bool):
                gl_rows_skipped += 1
                continue
            try:
                amount = Decimal(str(amount_raw))
            except InvalidOperation:
                gl_rows_skipped += 1
                continue
            # Decimal('NaN')/'Infinity' parse fine but poison the spend
            # basis (NaN survives quantize) — skip-and-count like any other
            # unusable amount.
            if not amount.is_finite():
                gl_rows_skipped += 1
                continue
            spend += amount
            gl_rows_matched += 1
        if gl_rows_skipped:
            log.warning(
                "do_reconcile_kickback: %d GL row(s) skipped for bad amount_nok "
                "agreement=%s ns=%s — spend basis may be incomplete",
                gl_rows_skipped,
                agreement_uuid,
                ns_uuid,
            )

        # Step 5: tier math (pure, retroactive-on-total).
        progression = _tier_progression(tiers, spend)

        # Step 6: ledger-backed term-change history (the only writes this
        # module makes — append-only).  Snapshot only when terms changed.
        previous = await _read_latest_term_snapshot(conn, ns_uuid, str(agreement_uuid))
        term_drift = False
        if previous is None:
            await _append_term_snapshot(conn, ns_uuid, str(agreement_uuid), terms)
        elif not _terms_equal(previous.get("terms"), terms):
            await _append_term_snapshot(conn, ns_uuid, str(agreement_uuid), terms)
            term_drift = True

    earned: Decimal = progression["earned_to_date"]
    to_next: Decimal | None = progression["to_next_tier"]
    projection_drift: float | None = None
    if projected_dec is not None:
        projection_drift = float((earned - projected_dec).quantize(_MONEY_QUANT, ROUND_HALF_UP))

    return {
        "status": "ok",
        "agreement_id": str(agreement_uuid),
        "supplier_node_id": str(supplier_node_id) if supplier_node_id else None,
        "spend_to_date_nok": float(spend.quantize(_MONEY_QUANT, ROUND_HALF_UP)),
        "earned_to_date_nok": float(earned),
        "active_tier": progression["active_tier"],
        "next_tier": progression["next_tier"],
        "to_next_tier_nok": float(to_next) if to_next is not None else None,
        "projection_drift_nok": projection_drift,
        "term_drift": term_drift,
        "gl_rows_matched": gl_rows_matched,
        "gl_rows_skipped": gl_rows_skipped,
        "period": period,
    }


async def get_term_change_history(
    pool: asyncpg.Pool,
    namespace_id: str | uuid.UUID,
    agreement_id: str | uuid.UUID,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Read-only, newest-first term-snapshot history for one agreement.

    Auditable history: each entry is one append-only ledger row written by
    ``do_reconcile_kickback``.  Namespace-scoped with an explicit predicate
    (never RLS-only).

    Returns a list of::

        {
            "ledger_id":       str,          # v3_cognitive_ledger.id
            "agreement_id":    str,
            "terms":           dict,         # money/legal term subset
            "recorded_at_iso": str,          # DB clock at snapshot time
            "created_at_iso":  str,          # ledger row created_at
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
              AND  tlx_scores->>'kind' = $3
              AND  tlx_scores->>'agreement_id' = $4
            ORDER BY created_at DESC
            LIMIT  $5
            """,
            str(ns_uuid),
            _MODEL_VERSION,
            _SNAPSHOT_KIND,
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
                "terms": payload.get("terms"),
                "recorded_at_iso": payload.get("recorded_at_iso"),
                "created_at_iso": row["created_at"].isoformat(),
            }
        )
    return history
