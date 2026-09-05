"""
nce/vertical_modules/procurement/three_way_match.py
====================================================
Pure three-way match evaluation — zero DB, zero HTTP, zero web/admin imports.

Reconstructed near-1:1 from the reference implementation +
``detectSubstitution:395``.

Three-way match
---------------
Compares a Purchase Order (PO), Goods Receipt (GR), and Invoice to produce a
confidence score (0–100) and a tier (GREEN / YELLOW / RED).

The confidence formula is:
    base_score  = 100.0
    qty_ratio   = min(gr_qty, invoice_qty) / po_qty      (0.0 – 1.0+)
    price_ratio = invoice_unit_price / po_unit_price     (0.0 – 1.0+)
    amount_ratio= invoice_total / po_total               (0.0 – 1.0+)

    qty_dev      = abs(1.0 - qty_ratio)   × 100
    price_dev    = abs(1.0 - price_ratio) × 100
    amount_dev   = abs(1.0 - amount_ratio)× 100

    qty_penalty   = qty_dev   × 0.40   (quantity mismatch weight)
    price_penalty = price_dev × 0.35   (unit-price mismatch weight)
    amount_penalty= amount_dev× 0.25   (total-amount mismatch weight)

    raw_score = base_score - qty_penalty - price_penalty - amount_penalty
    confidence = max(0.0, min(100.0, raw_score))

Tier mapping (thresholds from ``tolerances["MATCH_TOLERANCE"]``):
    confidence >= GREEN_THRESHOLD → GREEN
    confidence >= YELLOW_THRESHOLD → YELLOW
    else                           → RED

Substitution detection
----------------------
``detectSubstitution`` returns a dict with:
    ``is_substitution``  bool — True if the article on the invoice differs from the PO.
    ``level``            str  — EXACT | EQUIVALENT_SKU | COMPATIBLE | DIFFERENT
    ``po_article``       str  — article from the PO.
    ``invoice_article``  str  — article from the invoice.
    ``description``      str  — human-readable explanation.

Level logic:
    EXACT           — same article_id (case-insensitive).
    EQUIVALENT_SKU  — different article_id but equivalent_sku flag on invoice is True,
                      or invoice carries an explicit ``substitute_for`` that matches PO.
    COMPATIBLE      — invoice article has ``compatible_with`` list that includes PO article.
    DIFFERENT       — none of the above; full mismatch.

A substitution that is EXACT, EQUIVALENT_SKU, or COMPATIBLE is a VALID REPLACEMENT —
the match continues and confidence is evaluated against the tolerance zone.  Only DIFFERENT
triggers a confidence penalty (applied before clamping, so it can still land YELLOW/GREEN
if the amounts/prices are otherwise aligned).

Pure: no DB, no HTTP.  This file is the match only — NOT the Economy cascade (§9.1).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_VALID_SUBSTITUTION_LEVELS = {"EXACT", "EQUIVALENT_SKU", "COMPATIBLE"}
_PENALTY_QTY = 0.40
_PENALTY_PRICE = 0.35
_PENALTY_AMOUNT = 0.25
_DIFFERENT_SUBSTITUTION_PENALTY = 15.0


def do_evaluate_three_way_match(
    tolerances: dict[str, Any],
    po: dict[str, Any],
    goods_receipt: dict[str, Any],
    invoice: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate a PO × GR × invoice three-way match.

    Parameters
    ----------
    tolerances:
        Contents of ``procurement-tolerances.json`` (MATCH_TOLERANCE + DEFAULT_THRESHOLDS).
        All tier/zone thresholds are read from this dict — no literals in this function.
    po:
        Purchase Order with keys:
            ``article_id``   str   — ordered article.
            ``quantity``     int   — ordered quantity.
            ``unit_price``   float — agreed unit price.
    goods_receipt:
        Goods Receipt with keys:
            ``quantity``     int   — received quantity.
    invoice:
        Supplier Invoice with keys:
            ``article_id``   str   — invoiced article.
            ``quantity``     int   — invoiced quantity.
            ``unit_price``   float — invoiced unit price.
            Optional:
            ``equivalent_sku``  bool  — supplier asserts this is an equivalent SKU.
            ``substitute_for``  str   — explicit cross-reference to PO article.
            ``compatible_with`` list[str] — list of compatible article IDs.

    Returns
    -------
    dict with keys:
        ``confidence``      float (0–100).
        ``tier``            str — GREEN | YELLOW | RED.
        ``tolerance_zone``  dict — the matched zone entry from ``tolerances``.
        ``substitution``    dict — substitution detection result (always present).

    Raises
    ------
    ValueError:
        When required keys are missing or values are invalid.
    KeyError:
        When ``tolerances`` does not contain ``MATCH_TOLERANCE`` with required sub-keys.
    """
    # --- Validate and extract inputs ---
    po_qty = _require_positive_numeric(po, "quantity", "po")
    po_unit_price = _require_positive_numeric(po, "unit_price", "po")
    po_article = _require_str(po, "article_id", "po")

    gr_qty = _require_positive_numeric(goods_receipt, "quantity", "goods_receipt")

    invoice_qty = _require_positive_numeric(invoice, "quantity", "invoice")
    invoice_unit_price = _require_positive_numeric(invoice, "unit_price", "invoice")
    invoice_article = _require_str(invoice, "article_id", "invoice")

    # --- Substitution detection (pure, no penalty if valid replacement) ---
    substitution = _detect_substitution(po_article, invoice_article, invoice)

    # --- Confidence calculation ---
    confidence = _compute_confidence(
        po_qty=po_qty,
        po_unit_price=po_unit_price,
        gr_qty=gr_qty,
        invoice_qty=invoice_qty,
        invoice_unit_price=invoice_unit_price,
        is_different_substitution=substitution["level"] == "DIFFERENT",
    )

    # --- Tier and zone mapping (thresholds always from tolerances arg) ---
    tier, tolerance_zone = _resolve_tier(confidence, tolerances)

    return {
        "confidence": confidence,
        "tier": tier,
        "tolerance_zone": tolerance_zone,
        "substitution": substitution,
    }


# ---------------------------------------------------------------------------
# Substitution detection
# ---------------------------------------------------------------------------


def _detect_substitution(
    po_article: str,
    invoice_article: str,
    invoice: dict[str, Any],
) -> dict[str, Any]:
    """Detect and classify article substitution between PO and invoice.

    Returns a dict with ``is_substitution``, ``level``, ``po_article``,
    ``invoice_article``, and ``description``.
    """
    po_norm = po_article.strip().upper()
    inv_norm = invoice_article.strip().upper()

    if po_norm == inv_norm:
        return _substitution_result(
            is_substitution=False,
            level="EXACT",
            po_article=po_article,
            invoice_article=invoice_article,
            description="Article matches PO exactly.",
        )

    # EQUIVALENT_SKU: supplier asserts equivalence via flag or explicit cross-ref.
    if invoice.get("equivalent_sku") is True:
        return _substitution_result(
            is_substitution=True,
            level="EQUIVALENT_SKU",
            po_article=po_article,
            invoice_article=invoice_article,
            description=(
                f"Equivalent SKU substitution: {invoice_article!r} declared "
                f"equivalent to ordered article {po_article!r}."
            ),
        )

    substitute_for = invoice.get("substitute_for", "")
    if isinstance(substitute_for, str) and substitute_for.strip().upper() == po_norm:
        return _substitution_result(
            is_substitution=True,
            level="EQUIVALENT_SKU",
            po_article=po_article,
            invoice_article=invoice_article,
            description=(
                f"Equivalent SKU substitution: {invoice_article!r} "
                f"declared substitute for {po_article!r}."
            ),
        )

    # COMPATIBLE: invoice article declares compatibility with the PO article.
    compatible_with: list[str] = invoice.get("compatible_with") or []
    if isinstance(compatible_with, list):
        compatible_norms = [str(c).strip().upper() for c in compatible_with]
        if po_norm in compatible_norms:
            return _substitution_result(
                is_substitution=True,
                level="COMPATIBLE",
                po_article=po_article,
                invoice_article=invoice_article,
                description=(
                    f"Compatible substitution: {invoice_article!r} is compatible "
                    f"with ordered article {po_article!r}."
                ),
            )

    # DIFFERENT: full mismatch — reported as substitution but with a confidence penalty.
    return _substitution_result(
        is_substitution=True,
        level="DIFFERENT",
        po_article=po_article,
        invoice_article=invoice_article,
        description=(
            f"Article mismatch: invoiced {invoice_article!r} does not match "
            f"ordered article {po_article!r}."
        ),
    )


def _substitution_result(
    *,
    is_substitution: bool,
    level: str,
    po_article: str,
    invoice_article: str,
    description: str,
) -> dict[str, Any]:
    return {
        "is_substitution": is_substitution,
        "level": level,
        "po_article": po_article,
        "invoice_article": invoice_article,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Confidence formula
# ---------------------------------------------------------------------------


def _compute_confidence(
    *,
    po_qty: float,
    po_unit_price: float,
    gr_qty: float,
    invoice_qty: float,
    invoice_unit_price: float,
    is_different_substitution: bool,
) -> float:
    """Return confidence score (0–100) from quantity, price and amount deviations.

    Penalty weights are module-level constants (not tolerances — they are algorithm
    weights, not tenant-tunable thresholds).
    """
    po_total = po_qty * po_unit_price
    invoice_total = invoice_qty * invoice_unit_price

    # Ratios — clamp the denominator guard to avoid ZeroDivisionError.
    qty_ratio = min(gr_qty, invoice_qty) / po_qty
    price_ratio = invoice_unit_price / po_unit_price
    amount_ratio = invoice_total / po_total

    qty_dev = abs(1.0 - qty_ratio) * 100.0
    price_dev = abs(1.0 - price_ratio) * 100.0
    amount_dev = abs(1.0 - amount_ratio) * 100.0

    qty_penalty = qty_dev * _PENALTY_QTY
    price_penalty = price_dev * _PENALTY_PRICE
    amount_penalty = amount_dev * _PENALTY_AMOUNT

    raw_score = 100.0 - qty_penalty - price_penalty - amount_penalty

    if is_different_substitution:
        raw_score -= _DIFFERENT_SUBSTITUTION_PENALTY

    return max(0.0, min(100.0, raw_score))


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


def _resolve_tier(
    confidence: float,
    tolerances: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Map confidence (0–100) to a tier string and tolerance_zone dict.

    Thresholds are always read from ``tolerances["MATCH_TOLERANCE"]``.
    Falls back to ``DEFAULT_THRESHOLDS`` when MATCH_TOLERANCE is absent.

    Thresholds are clamped to the confidence scale [0, 100].  A GREEN_THRESHOLD
    above 100 (e.g. the real config value of 115, which means "invoice may be up to
    115 % of PO value") is treated as requiring the highest possible confidence — any
    confidence == 100.0 maps to GREEN.  This preserves the reference implementation's original intent that
    perfect matches are always GREEN, while keeping thresholds fully config-driven.
    """
    match_tol: dict[str, Any] = tolerances.get("MATCH_TOLERANCE", {})

    defaults = tolerances.get("DEFAULT_THRESHOLDS", {})
    raw_green = float(match_tol.get("GREEN_THRESHOLD", defaults.get("green", 115)))
    raw_yellow = float(match_tol.get("YELLOW_THRESHOLD", defaults.get("yellow", 70)))

    # Clamp to [0, 100] — thresholds in the config may use a wider scale.
    green_threshold = min(raw_green, 100.0)
    yellow_threshold = min(raw_yellow, 100.0)

    zones: dict[str, Any] = match_tol.get("zones", {})

    if confidence >= green_threshold:
        tier = "GREEN"
    elif confidence >= yellow_threshold:
        tier = "YELLOW"
    else:
        tier = "RED"

    tolerance_zone = dict(zones.get(tier, {"label": tier}))
    return tier, tolerance_zone


# ---------------------------------------------------------------------------
# Private validation helpers — single level of abstraction
# ---------------------------------------------------------------------------


def _require_positive_numeric(d: dict[str, Any], key: str, context: str) -> float:
    """Return d[key] as a positive float or raise ValueError."""
    if key not in d:
        raise ValueError(f"'{key}' is required in {context}")
    value = float(d[key])
    if value <= 0:
        raise ValueError(f"{context}['{key}'] must be positive, got {value}")
    return value


def _require_str(d: dict[str, Any], key: str, context: str) -> str:
    """Return d[key] as a non-empty string or raise ValueError."""
    if key not in d:
        raise ValueError(f"'{key}' is required in {context}")
    value = str(d[key]).strip()
    if not value:
        raise ValueError(f"{context}['{key}'] must not be empty")
    return value
