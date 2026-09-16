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
# Config-as-IP: load source trust weights from manufacturer-sources.json
# ---------------------------------------------------------------------------


def load_manufacturer_sources() -> dict[str, Any]:
    """Load source trust weights and manufacturer adapter mapping (config-as-IP)."""
    config_path = (
        Path(__file__).resolve().parent.parent.parent / "config_data" / "manufacturer-sources.json"
    )
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_source_trust(source: str | None, manufacturer: str | None = None) -> float:
    """Resolve the numeric source trust weight [0.0, 1.0] for a source and optional manufacturer.

    Precedence order per A2 (§3.1):
      manual_override (1.0) > manufacturer_verified (0.95) > distributor (0.80) > ai_derived (0.60) > scraped (0.40)
    """
    cfg = load_manufacturer_sources()
    weights: dict[str, Any] = cfg.get("source_trust_weights", {})
    adapters: dict[str, Any] = cfg.get("manufacturer_adapters", {})

    # Check manufacturer-specific adapter override
    if manufacturer:
        mfr_norm = str(manufacturer).strip().casefold()
        if mfr_norm in adapters:
            mfr_cfg = adapters[mfr_norm]
            if isinstance(mfr_cfg, dict):
                src_clean = (source or "").strip().lower().replace("-", "_")
                adapter_name = str(mfr_cfg.get("adapter", "")).lower().replace("-", "_")
                if (
                    not source
                    or "manufacturer" in src_clean
                    or src_clean == adapter_name
                    or src_clean in adapter_name
                ):
                    return float(mfr_cfg.get("trust_weight", 0.95))

    if not source:
        return float(weights.get("default", 0.50))

    src_raw = str(source).strip().casefold()
    src_norm = src_raw.replace("-", "_")

    # 1. Direct match in source_trust_weights
    if src_norm in weights:
        return float(weights[src_norm])
    if src_raw in weights:
        return float(weights[src_raw])

    # 2. Semantic prefix / substring category matching
    if any(k in src_norm for k in ("manual", "human_accepted")):
        return float(weights.get("manual_override", 1.0))
    if any(k in src_norm for k in ("manufacturer", "mfr")):
        return float(weights.get("manufacturer_verified", 0.95))
    if any(k in src_norm for k in ("distributor", "nettailer", "supplier", "netset")):
        return float(weights.get("distributor", 0.80))
    if any(k in src_norm for k in ("ai", "enrich", "llm", "claude", "gpt")):
        return float(weights.get("ai_derived", 0.60))
    if any(k in src_norm for k in ("ocr", "spec", "datasheet", "pdf")):
        return float(weights.get("datasheet_ocr", 0.50))
    if any(k in src_norm for k in ("scrape", "crawler", "web")):
        return float(weights.get("scraped", 0.40))

    return float(weights.get("default", 0.50))


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


async def _fetch_product_info(
    conn: asyncpg.Connection,
    product_id: UUID,
) -> tuple[dict[str, Any], str | None]:
    """Return (etim_specs, manufacturer) for one product row, or ({}, None) if not found."""
    row = await conn.fetchrow(
        """
        SELECT etim_specs, manufacturer
        FROM   product_catalog
        WHERE  id = $1
          AND  is_deleted = false
        """,
        product_id,
    )
    if row is None:
        return {}, None
    mfr = row.get("manufacturer") if hasattr(row, "get") else None
    if not mfr and hasattr(row, "get"):
        mfr = row.get("brand")
    raw = (
        row.get("etim_specs")
        if hasattr(row, "get")
        else (row["etim_specs"] if "etim_specs" in row else None)
    )
    if raw is None:
        return {}, mfr
    if isinstance(raw, str):
        return json.loads(raw), mfr
    return dict(raw), mfr


async def _fetch_product_etim_specs(
    conn: asyncpg.Connection,
    product_id: UUID,
) -> dict[str, Any]:
    """Return ``etim_specs`` JSONB for one product row, or {} if not found."""
    specs, _ = await _fetch_product_info(conn, product_id)
    return specs


async def _fetch_enrichment_candidates(
    conn: asyncpg.Connection,
    product_id: UUID,
) -> list[dict[str, Any]]:
    """Return accepted enrichment log rows (needs_review=false) as candidate values."""
    rows = await conn.fetch(
        """
        SELECT field_name, field_value, confidence, product_source_id, created_at
        FROM   product_enrichment_log
        WHERE  product_id = $1
          AND  needs_review = false
        ORDER BY created_at DESC
        """,
        product_id,
    )
    candidates = []
    for r in rows:
        candidates.append(
            {
                "field_name": r["field_name"],
                "value": r["field_value"],
                "confidence": float(r["confidence"]),
                "source": r["product_source_id"] or "ai_enrichment",
                "as_of": r["created_at"].isoformat()
                if r["created_at"]
                else "1970-01-01T00:00:00+00:00",
            }
        )
    return candidates


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
    manufacturer: str | None = None,
    extra_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Extract per-field candidate lists from etim_specs and optional extra candidates.

    The golden-record pass expects the C1 ``survive()`` contract::

        {
            "value":        <val>,
            "source":       <str>,
            "source_trust": <float>,
            "as_of":        <ISO-8601 str>,
            "confidence":   <float>,
        }

    When source_trust is not explicitly provided, it is resolved dynamically
    from ``manufacturer-sources.json`` (config-as-IP).

    Fields whose entry is a bare (non-dict) value are skipped — they have no
    provenance and cannot participate in survivorship.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}

    def _normalize_candidate(
        raw: dict[str, Any], default_source: str = "unknown"
    ) -> dict[str, Any]:
        prov = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        source = prov.get("source") or raw.get("source") or default_source

        # Explicit source_trust takes precedence if present; otherwise resolve from config
        if "source_trust" in raw and raw["source_trust"] is not None:
            source_trust = float(raw["source_trust"])
        elif prov and "source_trust" in prov and prov["source_trust"] is not None:
            source_trust = float(prov["source_trust"])
        else:
            source_trust = resolve_source_trust(source, manufacturer)

        as_of = str(raw.get("as_of") or prov.get("as_of") or "1970-01-01T00:00:00+00:00")
        confidence = float(raw.get("confidence", prov.get("confidence", 0.5)))

        return {
            "value": raw.get("value"),
            "source": source,
            "source_trust": source_trust,
            "as_of": as_of,
            "confidence": confidence,
        }

    for field_name, raw in etim_specs.items():
        if isinstance(raw, list):
            cand_list = []
            for item in raw:
                if isinstance(item, dict):
                    cand_list.append(_normalize_candidate(item))
            if cand_list:
                candidates[field_name] = cand_list
        elif isinstance(raw, dict):
            if "candidates" in raw and isinstance(raw["candidates"], list):
                cand_list = []
                for item in raw["candidates"]:
                    if isinstance(item, dict):
                        cand_list.append(_normalize_candidate(item))
                if cand_list:
                    candidates[field_name] = cand_list
            else:
                candidates[field_name] = [_normalize_candidate(raw)]

    if extra_candidates:
        for ec in extra_candidates:
            fname = ec.get("field_name")
            if not fname:
                continue
            cand_cand = {k: v for k, v in ec.items() if k != "field_name"}
            cand = _normalize_candidate(cand_cand, default_source="ai_enrichment")
            if fname in candidates:
                candidates[fname].append(cand)
            else:
                candidates[fname] = [cand]

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
    engine: NCEEngine | asyncpg.Pool,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compute the field-level golden record for a single product.

    Per-field winners are resolved via the C1 ``survive()`` pure function;
    survivorship provenance is appended to ``v3_cognitive_ledger`` for
    auditability.

    Parameters
    ----------
    engine:
        Live NCEEngine instance or asyncpg connection pool.
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

    pool = (
        engine.pg_pool
        if ("pg_pool" in getattr(engine, "__dict__", {}) or hasattr(type(engine), "pg_pool"))
        else engine
    )

    async with scoped_pg_session(pool, namespace_id) as conn:
        etim_specs, manufacturer = await _fetch_product_info(conn, product_id)
        if not etim_specs and not await _product_exists(conn, product_id):
            raise ValueError(f"product_id={product_id_raw!r} not found in namespace")

        unreviewed_money = await _fetch_unreviewed_money_fields(conn, product_id)
        enrichment_candidates = await _fetch_enrichment_candidates(conn, product_id)

    # --- Per-field survivorship via C1 pure function (no re-implementation) ---
    candidates_by_field = _build_field_candidates(
        etim_specs,
        manufacturer=manufacturer,
        extra_candidates=enrichment_candidates,
    )

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
            await append_survivorship_provenance(
                pool,
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
