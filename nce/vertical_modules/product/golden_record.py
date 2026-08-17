"""
nce/vertical_modules/product/golden_record.py
=============================================
Field-level golden record for the Product vertical — Module 2.Wave 10.

``do_golden_record`` computes the per-field winning value for a deduped product
by delegating to the **C1 survivorship primitive** (``nce.entity_resolution.
survivorship.survive()``).  It does NOT re-implement source-trust > recency >
confidence ordering — that logic lives exclusively in C1.

After resolving field winners the function:
  1. Runs the two-score quality model (completeness + A–E grade via
     ``nce.vertical_modules.product.quality``).
  2. Calls the publish gate to determine whether the product may be promoted to
     "trusted" status.
  3. Returns a structured result dict that callers can persist or return to MCP.

§9.3 publish gate rules (hard-coded — no env override, per spec):
  - Grade below ``TRUSTED_MIN_GRADE`` (default "C") → blocked.
  - Any money/legal field that still has an unreviewed enrichment log row
    (``needs_review=True``) → blocked.  The gate queries
    ``product_enrichment_log`` under the namespace RLS context.

Dependency rule (uncle-bob inward): this module imports from
  ``nce.entity_resolution.survivorship`` (C1 pure core),
  ``nce.vertical_modules.product.quality`` (inner module),
  ``nce.vertical_modules.product.enrich`` (_MONEY_LEGAL_FIELDS),
  ``nce.db_utils`` (scoped_pg_session),
  ``nce.mcp_args`` (require_namespace_id),
  stdlib + asyncpg only.
No web / admin / HTTP imports.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.survivorship import (
    append_survivorship_provenance,
    survive,
)
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.product.enrich import _MONEY_LEGAL_FIELDS
from nce.vertical_modules.product.quality import (
    CHANNEL_REQUIRED_FIELDS,
    completeness_score,
    quality_grade,
)

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.product.golden_record")


# ---------------------------------------------------------------------------
# Config-as-IP: load publish-gate threshold from product-quality.json
# ---------------------------------------------------------------------------


def _load_trusted_min_grade() -> str:
    """Load TRUSTED_MIN_GRADE from config_data/product-quality.json (config-as-IP)."""
    config_path = (
        Path(__file__).resolve().parent.parent.parent / "config_data" / "product-quality.json"
    )
    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return str(data["trusted_min_grade"])


# ---------------------------------------------------------------------------
# Publish-gate threshold — loaded from config_data (config-as-IP)
# ---------------------------------------------------------------------------

#: Minimum quality grade required for trusted promotion.
#: Grade order: A (best) → E (worst).  "C" means A, B, or C pass.
#: Value is loaded from config_data/product-quality.json — not a code literal.
TRUSTED_MIN_GRADE: str = _load_trusted_min_grade()

_GRADE_ORDER: list[str] = ["A", "B", "C", "D", "E"]


def _grade_passes(grade: str, min_grade: str = TRUSTED_MIN_GRADE) -> bool:
    """Return True when ``grade`` is at least as good as ``min_grade``."""
    try:
        return _GRADE_ORDER.index(grade) <= _GRADE_ORDER.index(min_grade)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_product_etim_specs(
    conn: asyncpg.Connection,
    product_id: UUID,
) -> dict[str, Any]:
    """Return ``etim_specs`` JSONB for one product row, or {} if not found."""
    row = await conn.fetchrow(
        """
        SELECT etim_specs
        FROM   product_catalog
        WHERE  id = $1
          AND  is_deleted = false
        """,
        product_id,
    )
    if row is None:
        return {}
    raw = row["etim_specs"]
    if raw is None:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


async def _fetch_unreviewed_money_fields(
    conn: asyncpg.Connection,
    product_id: UUID,
) -> list[str]:
    """Return money/legal field names that have unreviewed enrichment log rows.

    Queries ``product_enrichment_log`` scoped to the current namespace (RLS
    enforced by the caller's ``scoped_pg_session``).  Only returns field names
    that are in ``_MONEY_LEGAL_FIELDS`` AND have ``needs_review=True``.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT field_name
        FROM   product_enrichment_log
        WHERE  product_id = $1
          AND  needs_review = true
          AND  field_name = ANY($2::text[])
        """,
        product_id,
        list(_MONEY_LEGAL_FIELDS),
    )
    return [row["field_name"] for row in rows]


def _build_field_candidates(
    etim_specs: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Extract per-field candidate lists from the W7 provenance JSONB.

    W7 writes entries of the form::

        {
            "field_name": {
                "value":      <val>,
                "confidence": <float>,
                "verbalized": <str>,
                "source":     <str>,
            }
        }

    The golden-record pass expects the C1 ``survive()`` contract::

        {
            "value":        <val>,
            "source":       <str>,
            "source_trust": <float>,
            "as_of":        <ISO-8601 str>,
            "confidence":   <float>,
        }

    When there is only a single entry per field (the W7 auto-merge case) the
    source_trust defaults to 0.5 and as_of defaults to the epoch so that
    ``survive()`` can still run without special-casing.

    Fields whose entry is a bare (non-dict) value are skipped — they have no
    provenance and cannot participate in survivorship.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}

    for field_name, raw in etim_specs.items():
        if not isinstance(raw, dict):
            continue

        prov = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}

        source = (
            prov.get("source") or raw.get("source") or "unknown"
            if prov
            else raw.get("source") or "unknown"
        )
        source_trust: float = float(raw.get("source_trust", 0.5))
        as_of: str = str(raw.get("as_of") or "1970-01-01T00:00:00+00:00")
        confidence: float = float(raw.get("confidence", 0.5))

        candidates[field_name] = [
            {
                "value": raw.get("value"),
                "source": source,
                "source_trust": source_trust,
                "as_of": as_of,
                "confidence": confidence,
            }
        ]

    return candidates


def _run_publish_gate(
    grade: str,
    unreviewed_money_fields: list[str],
) -> dict[str, Any]:
    """Evaluate the publish gate and return a structured verdict.

    Parameters
    ----------
    grade:
        The A–E quality grade from ``quality_grade()``.
    unreviewed_money_fields:
        List of money/legal field names with ``needs_review=True`` entries in
        the enrichment log.

    Returns
    -------
    dict with keys:
        ``allowed``               — bool; True means promotion to trusted is allowed.
        ``blocked_by_grade``      — bool; True when grade is below ``TRUSTED_MIN_GRADE``.
        ``blocked_by_money_field``— bool; True when any money/legal field is unreviewed.
        ``unreviewed_money_fields``— list of blocking field names (may be empty).
        ``reason``                — human-readable summary string.
    """
    blocked_grade = not _grade_passes(grade)
    blocked_money = len(unreviewed_money_fields) > 0
    allowed = not blocked_grade and not blocked_money

    if allowed:
        reason = "all gate criteria passed"
    elif blocked_grade and blocked_money:
        reason = (
            f"grade {grade!r} is below minimum {TRUSTED_MIN_GRADE!r} and "
            f"money/legal fields need review: {unreviewed_money_fields}"
        )
    elif blocked_grade:
        reason = f"grade {grade!r} is below minimum {TRUSTED_MIN_GRADE!r}"
    else:
        reason = f"money/legal fields need review: {unreviewed_money_fields}"

    return {
        "allowed": allowed,
        "blocked_by_grade": blocked_grade,
        "blocked_by_money_field": blocked_money,
        "unreviewed_money_fields": unreviewed_money_fields,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Core entry-point
# ---------------------------------------------------------------------------


async def do_golden_record(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compute the field-level golden record for a single product.

    Per-field winners are resolved via the C1 ``survive()`` pure function;
    survivorship provenance is appended to ``v3_cognitive_ledger`` for
    auditability.

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id``  (str, required)
        ``product_id``    (str UUID, required)
        ``channel``       (str, optional, default "b2b_portal") — target channel
                          for completeness scoring.

    Returns
    -------
    dict with keys:
        ``product_id``        — echoed back
        ``channel``           — target channel used
        ``field_winners``     — dict of ``{field_name: {value, source, reason}}``
        ``completeness``      — output of ``completeness_score()``
        ``grade_result``      — output of ``quality_grade()``
        ``publish_gate``      — output of ``_run_publish_gate()``

    Raises
    ------
    ValueError
        When ``product_id`` is absent or the product is not found.
    """
    namespace_id_str = require_namespace_id(params)
    namespace_id = UUID(namespace_id_str)

    product_id_raw = str(params.get("product_id") or "").strip()
    if not product_id_raw:
        raise ValueError("product_id is required")
    product_id = UUID(product_id_raw)

    channel = str(params.get("channel") or "b2b_portal").strip()
    if channel not in CHANNEL_REQUIRED_FIELDS:
        channel = "b2b_portal"

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        etim_specs = await _fetch_product_etim_specs(conn, product_id)
        if not etim_specs and not await _product_exists(conn, product_id):
            raise ValueError(f"product_id={product_id_raw!r} not found in namespace")

        unreviewed_money = await _fetch_unreviewed_money_fields(conn, product_id)

    # --- Per-field survivorship via C1 pure function (no re-implementation) ---
    candidates_by_field = _build_field_candidates(etim_specs)

    field_winners: dict[str, dict[str, Any]] = {}
    for field_name, candidates in candidates_by_field.items():
        result = survive(candidates)
        field_winners[field_name] = {
            "value": result["value"],
            "source": result["provenance"]["source"],
            "reason": result["provenance"]["reason"],
        }
        log.debug(
            "[golden_record] product=%s field=%r winner_source=%r reason=%r",
            product_id_raw[:8],
            field_name,
            result["provenance"]["source"],
            result["provenance"]["reason"],
        )

    # Append survivorship provenance to v3_cognitive_ledger for each field.
    for field_name, candidates in candidates_by_field.items():
        winner = field_winners[field_name]
        try:
            async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
                await append_survivorship_provenance(
                    engine.pg_pool,
                    namespace_id=namespace_id,
                    entity_id=product_id_raw,
                    field_name=field_name,
                    winning_value=winner["value"],
                    winning_source=winner["source"],
                    reason=winner["reason"],
                    all_candidates=candidates_by_field[field_name],
                )
        except Exception:
            log.warning(
                "[golden_record] provenance append failed product=%s field=%r — continuing",
                product_id_raw[:8],
                field_name,
                exc_info=True,
            )

    # --- Two-score quality model ---
    comp = completeness_score(etim_specs, channel=channel)
    grade_result = quality_grade(etim_specs)

    # --- Publish gate (§9.3) ---
    gate = _run_publish_gate(grade_result["grade"], unreviewed_money)

    return {
        "product_id": product_id_raw,
        "channel": channel,
        "field_winners": field_winners,
        "completeness": comp,
        "grade_result": grade_result,
        "publish_gate": gate,
    }


async def _product_exists(conn: asyncpg.Connection, product_id: UUID) -> bool:
    """Return True when the product row exists (not deleted)."""
    val = await conn.fetchval(
        "SELECT 1 FROM product_catalog WHERE id = $1 AND is_deleted = false",
        product_id,
    )
    return val is not None
