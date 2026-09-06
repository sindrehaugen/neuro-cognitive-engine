"""
nce/vertical_modules/product/enrich.py
=======================================
On-demand product enrichment — Module 2.Wave 7.

``do_enrich_product`` fills ONLY the missing fields for exactly ONE product
(scoped to ``product_id``), is idempotent (same watermark → no new work),
confidence-scored (verbalized A4 float), and goes through the C2 ``@governed``
wrapper (confirm-only default + idempotency key + ``event_log`` audit).

§9.3 invariants (never violated here):
  - Money/legal fields always written to ``product_enrichment_log`` with
    ``needs_review=True``, regardless of confidence.
  - Sub-threshold fields (below ``NCE_PRODUCT_ENRICH_MIN_CONFIDENCE``) are
    written with ``needs_review=True`` and never merged to catalog.
  - The function NEVER iterates the product catalog; it operates on exactly
    the one ``product_id`` supplied by the caller.
  - Cost/margin/BID columns are never returned or logged (ADR-0017).

Dependency rule (uncle-bob inward): this module imports from ``nce.autonomy``,
``nce.providers``, ``nce.db_utils``, stdlib, and ``os`` only.  No web / admin / HTTP modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from nce.autonomy.governor import governed
from nce.providers.base import Message
from nce.providers.factory import get_provider

if TYPE_CHECKING:
    from nce.providers.base import LLMProvider

log = logging.getLogger("nce.vertical_modules.product.enrich")

# ---------------------------------------------------------------------------
# §9.3 — Money/legal field names that ALWAYS require human review
# ---------------------------------------------------------------------------

_MONEY_LEGAL_FIELDS: frozenset[str] = frozenset(
    {
        "price",
        "cost",
        "msrp",
        "list_price",
        "warranty",
        "warranty_terms",
        "compliance",
        "certification",
        "legal",
        "contract_terms",
    }
)

# ---------------------------------------------------------------------------
# Confidence threshold (env-configurable, default 0.70)
# ---------------------------------------------------------------------------


def _min_confidence() -> float:
    """Read ``NCE_PRODUCT_ENRICH_MIN_CONFIDENCE`` from env (default 0.70)."""
    raw = os.getenv("NCE_PRODUCT_ENRICH_MIN_CONFIDENCE", "0.70").strip()
    try:
        val = float(raw)
        if 0.0 <= val <= 1.0:
            return val
    except ValueError:
        pass
    log.warning(
        "[enrich] invalid NCE_PRODUCT_ENRICH_MIN_CONFIDENCE=%r, using 0.70",
        raw,
    )
    return 0.70


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


def _derive_idempotency_key(
    product_id: str,
    missing_fields: list[str],
    source_watermark: str,
) -> str:
    """Stable hash of ``(product_id, sorted(missing_fields), source_watermark)``.

    A re-run with the same product, the same missing fields, and the same
    source watermark produces the identical key → governed NO-OP.
    """
    payload = json.dumps(
        {
            "product_id": product_id,
            "missing_fields": sorted(missing_fields),
            "source_watermark": source_watermark,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Field-level confidence classifier (verbalized A4 — not logprobs)
# ---------------------------------------------------------------------------


def _verbalize_confidence(value: float) -> str:
    """Return a human-readable confidence label (A4 verbalization).

    Bands:
      0.90–1.00 → "very_high"
      0.75–0.89 → "high"
      0.55–0.74 → "medium"
      0.00–0.54 → "low"
    """
    if value >= 0.90:
        return "very_high"
    if value >= 0.75:
        return "high"
    if value >= 0.55:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Single-product catalog look-up (NEVER bulk)
# ---------------------------------------------------------------------------


async def _fetch_product_row(
    conn: asyncpg.Connection,
    product_id: str,
) -> dict[str, Any] | None:
    """Fetch the single product row identified by ``product_id``.

    The WHERE clause filters by ``id = $1`` — exactly one product, never a
    scan.  Returns ``None`` when the product does not exist in this namespace.
    """
    row = await conn.fetchrow(
        """
        SELECT id, manufacturer, mfr_part_no, product_source_id, etim_specs
        FROM   product_catalog
        WHERE  id = $1
          AND  is_deleted = false
        """,
        uuid.UUID(product_id),
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Pydantic Enrichment Models
# ---------------------------------------------------------------------------


class EnrichedFieldProposal(BaseModel):
    field_name: str = Field(description="Name of the product specification or attribute field")
    field_value: str = Field(description="Enriched value for the field based on product details")
    confidence: float = Field(
        default=0.0, description="Confidence score from 0.0 to 1.0 (or 0.0 to 100.0)"
    )


class ProductEnrichmentModel(BaseModel):
    proposals: list[EnrichedFieldProposal] = Field(
        default_factory=list,
        description="List of enriched field proposals for missing product specifications",
    )


# ---------------------------------------------------------------------------
# Isolated LLM Enrichment Caller
# ---------------------------------------------------------------------------


async def _call_product_enrichment(
    provider: LLMProvider,
    product_row: dict[str, Any],
    missing_fields: list[str],
    trigger_context: dict[str, Any],
) -> ProductEnrichmentModel:
    """Invoke the cognitive provider to enrich missing fields for exactly one product."""
    prompt = (
        f"Enrich the following missing product specification fields: {', '.join(missing_fields)}.\n"
        f"Product details:\n"
        f"- Manufacturer: {product_row.get('manufacturer') or 'unknown'}\n"
        f"- Manufacturer Part Number: {product_row.get('mfr_part_no') or 'unknown'}\n"
        f"- Existing Specifications: {json.dumps(product_row.get('etim_specs') or {})}\n"
        f"- Trigger Context: {json.dumps(trigger_context)}\n\n"
        "For each requested missing field, predict or extract the accurate value and estimate "
        "your confidence as a score between 0.0 and 1.0 (or 0.0 to 100.0)."
    )
    messages = [
        Message.system(
            "You are an expert technical product catalog enrichment system. "
            "You accurately complete missing product specifications, attributes, dimensions, and classifications."
        ),
        Message.user(prompt),
    ]
    return await provider.complete(messages, ProductEnrichmentModel)


# ---------------------------------------------------------------------------
# Enrichment proposals (routes through LLMProvider)
# ---------------------------------------------------------------------------


async def _build_proposals(
    provider: LLMProvider,
    missing_fields: list[str],
    product_row: dict[str, Any],
    trigger_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return per-field enrichment proposals for the missing fields via LLMProvider.

    Invokes the configured LLMProvider to obtain real estimates and per-field
    calibrated confidence scores.  Never generates synthetic template strings.
    """
    try:
        enrichment_result = await _call_product_enrichment(
            provider,
            product_row,
            missing_fields,
            trigger_context,
        )
        proposals_by_field = {p.field_name: p for p in enrichment_result.proposals if p.field_name}
    except Exception as exc:
        log.warning("[enrich] cognitive provider call failed: %s", exc)
        proposals_by_field = {}

    proposals: list[dict[str, Any]] = []
    for field in missing_fields:
        prop = proposals_by_field.get(field)
        if prop is not None:
            field_value = str(prop.field_value)
            conf = float(prop.confidence)
            if conf > 1.0:
                conf = conf / 100.0
            conf = max(0.0, min(1.0, conf))
        else:
            field_value = ""
            conf = 0.0

        proposals.append(
            {
                "field_name": field,
                "field_value": field_value,
                "confidence": conf,
                "verbalized": _verbalize_confidence(conf),
            }
        )
    return proposals


# ---------------------------------------------------------------------------
# Core governed handler
# ---------------------------------------------------------------------------


@governed(action_type="product_enrich")
async def do_enrich_product(
    conn: asyncpg.Connection,
    namespace_id: Any,
    *,
    idempotency_key: str,
    confirm: bool = False,
    product_id: str,
    trigger_context: dict[str, Any],
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    """Enrich exactly ONE product's missing fields under the C2 ``@governed`` gate.

    The ``@governed`` decorator enforces:
      1. confirm-only default (no side effect without ``confirm=True``).
      2. non-empty ``idempotency_key`` required (raises ``MissingIdempotencyKeyError``).
      3. dedup: same key → ``already_executed`` NO-OP on replay.
      4. ``event_log`` audit entry on first confirmed execution.

    Parameters
    ----------
    conn:
        asyncpg connection already inside a transaction (``scoped_pg_session``).
    namespace_id:
        Tenant UUID — all writes are scoped to this namespace.
    idempotency_key:
        Stable hash of ``(product_id, sorted(missing_fields), source_watermark)``.
        Derived by the MCP handler via ``_derive_idempotency_key``.
    confirm:
        ``False`` (default) → governed returns ``pending_approval`` without
        calling this body.  ``True`` → executes once.
    product_id:
        UUID string of the single product to enrich.  NEVER a list.
    trigger_context:
        ``{kind: "quote"|"design", ref_id: str, missing_fields: [str], source_watermark: str}``
    provider:
        Optional ``LLMProvider`` instance override (for tests / direct injection).
        When ``None``, resolves the provider from namespace metadata via ``get_provider``.

    Returns
    -------
    dict with ``product_id``, ``proposals_written`` (count), ``auto_merged``
    (count of high-confidence non-money/legal fields merged to etim_specs),
    ``needs_review_count`` (count that need human review).

    Raises
    ------
    ValueError
        When ``product_id`` is missing or the product is not found in this namespace.
    """
    if not product_id:
        raise ValueError("product_id is required")

    missing_fields: list[str] = list(trigger_context.get("missing_fields") or [])
    # NB: source_watermark feeds the idempotency key, which the MCP handler
    # derives and passes in via idempotency_key — it is not needed in this body.
    min_conf = _min_confidence()

    # --- Fetch the single product row (NEVER bulk scan) ---
    product_row = await _fetch_product_row(conn, product_id)
    if product_row is None:
        raise ValueError(f"product_id={product_id!r} not found in namespace")

    product_source_id: str = str(product_row.get("product_source_id") or "")

    # --- Resolve cognitive provider for this tenant ---
    if provider is None:
        row = await conn.fetchrow(
            "SELECT metadata FROM namespaces WHERE id = $1::uuid",
            uuid.UUID(str(namespace_id)),
        )
        metadata = (
            json.loads(row["metadata"])
            if row and isinstance(row["metadata"], str)
            else (row["metadata"] if row and isinstance(row["metadata"], dict) else {})
        )
        provider = get_provider(metadata)

    # --- Build per-field proposals via cognitive provider ---
    proposals = await _build_proposals(provider, missing_fields, product_row, trigger_context)

    proposals_written = 0
    auto_merged = 0
    needs_review_count = 0

    for proposal in proposals:
        field_name: str = proposal["field_name"]
        field_value: str = proposal["field_value"]
        confidence: float = proposal["confidence"]

        # §9.3: money/legal ALWAYS needs_review, regardless of confidence.
        is_money_legal = field_name in _MONEY_LEGAL_FIELDS
        below_threshold = confidence < min_conf
        needs_review: bool = is_money_legal or below_threshold

        # --- Write to product_enrichment_log (always) ---
        await conn.execute(
            """
            INSERT INTO product_enrichment_log
                (namespace_id, product_id, trigger_context, field_name,
                 field_value, confidence, needs_review, product_source_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.UUID(str(namespace_id)),
            uuid.UUID(product_id),
            json.dumps(trigger_context),
            field_name,
            field_value,
            confidence,
            needs_review,
            product_source_id or None,
        )
        proposals_written += 1

        if needs_review:
            needs_review_count += 1
            log.info(
                "[enrich] product=%s field=%r needs_review=True "
                "(money_legal=%s below_threshold=%s confidence=%.4f)",
                product_id[:8],
                field_name,
                is_money_legal,
                below_threshold,
                confidence,
            )
        else:
            # High-confidence, non-money/legal field: auto-merge to etim_specs.
            verbalized = proposal["verbalized"]
            patch: dict[str, Any] = {
                field_name: {
                    "value": field_value,
                    "confidence": confidence,
                    "verbalized": verbalized,
                    "source": product_source_id,
                }
            }
            await conn.execute(
                """
                UPDATE product_catalog
                SET    etim_specs  = etim_specs || $1::jsonb,
                       updated_at = now()
                WHERE  id = $2
                """,
                json.dumps(patch),
                uuid.UUID(product_id),
            )
            auto_merged += 1
            log.info(
                "[enrich] product=%s field=%r auto_merged confidence=%.4f (%s)",
                product_id[:8],
                field_name,
                confidence,
                verbalized,
            )

    return {
        "product_id": product_id,
        "proposals_written": proposals_written,
        "auto_merged": auto_merged,
        "needs_review_count": needs_review_count,
        "min_confidence_threshold": min_conf,
    }
