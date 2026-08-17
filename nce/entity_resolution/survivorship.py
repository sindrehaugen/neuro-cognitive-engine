"""C1 field-level survivorship — precedence decision + provenance.

``survive()`` is the pure domain core: given multiple field values from
different sources, it picks the surviving value by applying the precedence
chain **source-trust > recency (as_of) > confidence** and records *why* it
won.  No DB, HTTP, or framework imports here (dependency rule).

``append_survivorship_provenance()`` is the thin DB helper that persists the
per-field provenance audit record to ``v3_cognitive_ledger`` via the existing
append pattern (``scoped_pg_session``).  It is intentionally kept separate
from the pure core so callers can use ``survive()`` without a DB connection
(e.g. in tests, batch pipelines).

Caller contract:
  Each entry in ``field_values`` must be a dict with the following keys:
    - ``value``        — the field value (any JSON-serialisable type)
    - ``source``       — source identifier (str)
    - ``source_trust`` — numeric trust score in [0, 1] (float)
    - ``as_of``        — ISO-8601 datetime string (RFC 3339) or ``datetime``
    - ``confidence``   — confidence score in [0, 1] (float)

  ``survive()`` raises ``ValueError`` when ``field_values`` is empty.

  ``append_survivorship_provenance()`` requires a ``pool`` obtained via the
  application DSN and a valid ``namespace_id``.  The ``conn`` is acquired
  inside ``scoped_pg_session`` so RLS is enforced and the insert is
  namespace-scoped.  ``memory_id`` is optional (``None`` is valid for
  provenance records not yet attached to a specific memory row).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: A single field-value candidate supplied to ``survive()``.
FieldCandidate = dict[str, Any]

#: The result of ``survive()``: the winning value plus its provenance.
SurvivorResult = dict[str, Any]


# ---------------------------------------------------------------------------
# Precedence levels (exported for tests)
# ---------------------------------------------------------------------------

REASON_SOURCE_TRUST: str = "source_trust"
REASON_RECENCY: str = "recency"
REASON_CONFIDENCE: str = "confidence"


# ---------------------------------------------------------------------------
# Pure domain core
# ---------------------------------------------------------------------------


def _parse_as_of(raw: Any) -> datetime:
    """Return a timezone-aware ``datetime`` from a string or existing ``datetime``.

    Strings are parsed as ISO-8601 / RFC-3339.  Naïve ``datetime`` objects
    are assumed UTC.  Raises ``ValueError`` for unparseable strings.
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, str):
        # Python 3.11+: fromisoformat handles RFC-3339 with trailing 'Z'.
        # For older runtimes we normalise 'Z' → '+00:00' first.
        normalised = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"Cannot parse as_of value: {raw!r}")


def survive(field_values: list[FieldCandidate]) -> SurvivorResult:
    """Pick the surviving field value by precedence: source-trust > recency > confidence.

    Pure function — no DB, HTTP, or I/O of any kind.

    Parameters
    ----------
    field_values:
        One or more candidates.  Each must contain ``value``, ``source``,
        ``source_trust`` (float in [0, 1]), ``as_of`` (ISO-8601 str or
        datetime), and ``confidence`` (float in [0, 1]).

    Returns
    -------
    dict with keys:
        ``value``       — the surviving field value
        ``provenance``  — dict with keys:
            ``source``  — source identifier of the winner
            ``reason``  — which precedence level decided the winner
                          (one of: ``"source_trust"``, ``"recency"``,
                           ``"confidence"``)

    Raises
    ------
    ValueError:
        ``field_values`` is empty.
    """
    if not field_values:
        raise ValueError("survive() requires at least one field candidate.")

    winner = _elect_winner(field_values)
    return {
        "value": winner["value"],
        "provenance": {
            "source": winner["source"],
            "reason": winner["_reason"],
        },
    }


def _elect_winner(candidates: list[FieldCandidate]) -> dict[str, Any]:
    """Return the winning candidate dict, augmented with ``_reason``.

    Precedence chain (each level is a single comparison at the same
    abstraction — no nesting):
      1. Highest ``source_trust`` wins.
      2. On tie: most recent ``as_of`` wins.
      3. On tie: highest ``confidence`` wins.
      4. On tie at all levels: the first candidate (stable) wins.
    """
    best = _with_reason(candidates[0], REASON_SOURCE_TRUST)

    for candidate in candidates[1:]:
        best = _compare(best, candidate)

    return best


def _with_reason(candidate: FieldCandidate, reason: str) -> dict[str, Any]:
    """Return a copy of ``candidate`` with ``_reason`` set."""
    return {**candidate, "_reason": reason}


def _compare(
    current_best: dict[str, Any],
    challenger: FieldCandidate,
) -> dict[str, Any]:
    """Return the winner between ``current_best`` and ``challenger``.

    Each comparison is one step in the precedence chain.  The function reads
    as a flat sequence of three comparisons, not nested ifs.
    """
    trust_decision = _compare_by_trust(current_best, challenger)
    if trust_decision is not None:
        return trust_decision

    recency_decision = _compare_by_recency(current_best, challenger)
    if recency_decision is not None:
        return recency_decision

    confidence_decision = _compare_by_confidence(current_best, challenger)
    if confidence_decision is not None:
        return confidence_decision

    # Complete tie on all dimensions — stable: keep current_best.
    return current_best


def _compare_by_trust(
    current_best: dict[str, Any],
    challenger: FieldCandidate,
) -> dict[str, Any] | None:
    """Return the winner if ``source_trust`` is not equal, else ``None``."""
    best_trust: float = float(current_best["source_trust"])
    chal_trust: float = float(challenger["source_trust"])
    if chal_trust > best_trust:
        return _with_reason(challenger, REASON_SOURCE_TRUST)
    if best_trust > chal_trust:
        return current_best  # already has its reason from previous comparison
    return None  # tie — escalate


def _compare_by_recency(
    current_best: dict[str, Any],
    challenger: FieldCandidate,
) -> dict[str, Any] | None:
    """Return the winner if ``as_of`` is not equal, else ``None``."""
    best_ts: datetime = _parse_as_of(current_best["as_of"])
    chal_ts: datetime = _parse_as_of(challenger["as_of"])
    if chal_ts > best_ts:
        return _with_reason(challenger, REASON_RECENCY)
    if best_ts > chal_ts:
        # Preserve current_best's existing reason (it already won on trust
        # in an earlier call, or we escalated here).  Re-tag for recency so
        # the provenance record is accurate for *this* tie-break.
        return _with_reason(current_best, REASON_RECENCY)
    return None  # tie — escalate


def _compare_by_confidence(
    current_best: dict[str, Any],
    challenger: FieldCandidate,
) -> dict[str, Any] | None:
    """Return the winner if ``confidence`` is not equal, else ``None``."""
    best_conf: float = float(current_best["confidence"])
    chal_conf: float = float(challenger["confidence"])
    if chal_conf > best_conf:
        return _with_reason(challenger, REASON_CONFIDENCE)
    if best_conf > chal_conf:
        return _with_reason(current_best, REASON_CONFIDENCE)
    return None  # complete tie — caller keeps current_best


# ---------------------------------------------------------------------------
# Thin DB helper — provenance append to v3_cognitive_ledger
# ---------------------------------------------------------------------------

_SURVIVORSHIP_MODEL_VERSION: str = "survivorship/v1"

# Empathic tensor placeholder: six zeros.  This is a structural audit row,
# not an affective measurement.  The vector(6) column is NOT NULL per schema.
_ZERO_TENSOR: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


async def append_survivorship_provenance(
    pool: Any,  # asyncpg.Pool — typed as Any to avoid hard asyncpg import at module level
    *,
    namespace_id: str | UUID,
    entity_id: str,
    field_name: str,
    winning_value: Any,
    winning_source: str,
    reason: str,
    all_candidates: list[FieldCandidate],
    memory_id: UUID | None = None,
) -> UUID:
    """Append a per-field survivorship provenance record to ``v3_cognitive_ledger``.

    Reuses ``scoped_pg_session`` for RLS enforcement (no new table, no new
    abstraction — rule 4 / rule 6).  The provenance payload is stored in
    ``tlx_scores`` JSONB so it is query-accessible without schema changes.
    ``model_version`` is set to ``"survivorship/v1"`` to distinguish these
    rows from affective tensor records.

    Parameters
    ----------
    pool:
        asyncpg connection pool.  Must be the application pool (not GC pool).
    namespace_id:
        Active namespace UUID.  Used for RLS scoping.
    entity_id:
        Identifier of the entity whose field is being resolved (opaque string).
    field_name:
        The name of the field that was resolved (e.g. ``"hostname"``).
    winning_value:
        The surviving value (must be JSON-serialisable).
    winning_source:
        Source identifier of the winning candidate.
    reason:
        The precedence level that decided the winner (one of the
        ``REASON_*`` constants from this module).
    all_candidates:
        The full list of candidates supplied to ``survive()``.  Stored in
        ``tlx_scores`` for full auditability.
    memory_id:
        Optional UUID of a related ``memories`` row.  ``None`` is valid.

    Returns
    -------
    UUID:
        The ``id`` of the newly inserted ``v3_cognitive_ledger`` row.
    """

    from nce.db_utils import scoped_pg_session

    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id

    # Build the provenance payload for tlx_scores JSONB.
    # This is the auditable record — field name, winner, reason, all inputs.
    provenance_payload: dict[str, Any] = {
        "event": "field_survivorship",
        "entity_id": entity_id,
        "field_name": field_name,
        "winning_value": winning_value,
        "winning_source": winning_source,
        "reason": reason,
        "candidates": [
            {
                "source": c["source"],
                "source_trust": float(c["source_trust"]),
                "as_of": (
                    c["as_of"].isoformat() if isinstance(c["as_of"], datetime) else str(c["as_of"])
                ),
                "confidence": float(c["confidence"]),
            }
            for c in all_candidates
        ],
    }

    tlx_json: str = json.dumps(provenance_payload)
    vad_json: str = json.dumps({})

    inserted_id: UUID

    async with scoped_pg_session(pool, ns_uuid) as conn:
        inserted_id = await conn.fetchval(
            """
            INSERT INTO v3_cognitive_ledger (
                namespace_id,
                memory_id,
                empathic_tensor,
                tlx_scores,
                vad_scores,
                model_version
            ) VALUES (
                $1::uuid,
                $2::uuid,
                $3::float[],
                $4::jsonb,
                $5::jsonb,
                $6
            )
            RETURNING id
            """,
            ns_uuid,
            memory_id,
            _ZERO_TENSOR,
            tlx_json,
            vad_json,
            _SURVIVORSHIP_MODEL_VERSION,
        )

    return UUID(str(inserted_id))
