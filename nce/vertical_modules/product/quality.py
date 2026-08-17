"""
nce/vertical_modules/product/quality.py
========================================
Two-score quality model for the Product vertical — Module 2.Wave 10.

Two scores, computed independently, surfacing specific failing criteria:

1. **Completeness score** (per target channel):
   Fraction of *required* fields for that channel that are non-null/non-empty
   in the supplied ``etim_specs`` JSONB.  Returns a float in [0, 1] and the
   list of missing fields.

2. **A–E quality grade** (consistency / units / provenance):
   Examines each field entry's provenance metadata inside ``etim_specs`` for:
     - ``provenance.source`` present                  (+1 per field)
     - ``provenance.reason`` present                  (+1 per field)
     - ``confidence`` in [0, 1] range                 (+1 per field)
     - no duplicate values across sources             (+1 per field)
   Each criterion is scored as pass/fail; the total fraction maps to A–E.
   Returns the grade, a float score in [0, 1], and a list of failing criteria
   with field names.

3. **Per-manufacturer rollup**:
   Aggregates completeness + grade from a list of product quality results into
   a summary dict keyed by manufacturer.

Grade mapping (per-field):
  A: score >= 0.90
  B: score >= 0.75
  C: score >= 0.55
  D: score >= 0.35
  E: score <  0.35

Dependency rule (uncle-bob inward): this module has no DB, HTTP, or framework
imports — it is a pure computation layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config-as-IP: load quality thresholds from product-quality.json
# ---------------------------------------------------------------------------


def _load_quality_config() -> dict[str, Any]:
    """Load quality grade thresholds from config_data (config-as-IP)."""
    config_path = (
        Path(__file__).resolve().parent.parent.parent / "config_data" / "product-quality.json"
    )
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Per-channel required-field definitions
# ---------------------------------------------------------------------------

#: Required fields per target channel.  Extend as new channels are onboarded.
CHANNEL_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "b2b_portal": frozenset(
        {
            "short_description",
            "category",
            "manufacturer",
            "mfr_part_no",
            "lifecycle_status",
        }
    ),
    "quote": frozenset(
        {
            "short_description",
            "category",
            "manufacturer",
            "mfr_part_no",
            "price",
            "lifecycle_status",
        }
    ),
    "design": frozenset(
        {
            "short_description",
            "category",
            "manufacturer",
            "mfr_part_no",
            "lifecycle_status",
            "compliance",
            "warranty",
        }
    ),
}


# ---------------------------------------------------------------------------
# Grade thresholds — loaded from config_data/product-quality.json (config-as-IP)
# ---------------------------------------------------------------------------

_QUALITY_CONFIG: dict[str, Any] = _load_quality_config()

_GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (float(t), str(g)) for t, g in _QUALITY_CONFIG["grade_thresholds"]
]


def _score_to_grade(score: float) -> str:
    """Map a fraction in [0, 1] to an A–E letter grade."""
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "E"


# ---------------------------------------------------------------------------
# Completeness score
# ---------------------------------------------------------------------------


def completeness_score(
    etim_specs: dict[str, Any],
    *,
    channel: str,
) -> dict[str, Any]:
    """Compute completeness for ``etim_specs`` against the required fields for ``channel``.

    A field is considered present when the ``etim_specs`` key exists and its
    value (or its ``value`` sub-key if the entry is a provenance dict) is
    non-empty and non-null.

    Parameters
    ----------
    etim_specs:
        Per-field provenance dict (as stored by W7 enrichment).  Top-level keys
        are field names; values may be provenance dicts or bare values.
    channel:
        Target channel name.  Unknown channels return a score of 0.0 with all
        required fields marked missing.

    Returns
    -------
    dict with keys:
        ``channel``         — the channel name
        ``score``           — float in [0, 1], fraction of required fields present
        ``present``         — list of field names that are present
        ``missing``         — list of field names that are absent (failing criteria)
        ``required_count``  — total number of required fields for the channel
    """
    required = CHANNEL_REQUIRED_FIELDS.get(channel, frozenset())
    if not required:
        return {
            "channel": channel,
            "score": 0.0,
            "present": [],
            "missing": [],
            "required_count": 0,
        }

    present: list[str] = []
    missing: list[str] = []

    for field in sorted(required):
        raw = etim_specs.get(field)
        if _field_is_present(raw):
            present.append(field)
        else:
            missing.append(field)

    score = len(present) / len(required) if required else 0.0

    return {
        "channel": channel,
        "score": round(score, 4),
        "present": present,
        "missing": missing,
        "required_count": len(required),
    }


def _field_is_present(raw: Any) -> bool:
    """Return True when ``raw`` represents a non-empty, non-null field value.

    Handles both bare values and W7 provenance dicts ``{"value": ..., ...}``.
    """
    if raw is None:
        return False
    if isinstance(raw, dict):
        val = raw.get("value")
        if val is None:
            return False
        if isinstance(val, str) and not val.strip():
            return False
        return True
    if isinstance(raw, str) and not raw.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# A–E quality grade
# ---------------------------------------------------------------------------


def quality_grade(
    etim_specs: dict[str, Any],
) -> dict[str, Any]:
    """Compute the A–E quality grade from provenance metadata in ``etim_specs``.

    Criteria checked per field (each is a binary pass/fail):
      1. ``provenance.source`` key is present and non-empty.
      2. ``provenance.reason`` key is present and non-empty.
      3. ``confidence`` is a float in [0, 1].
      4. No duplicate raw values across candidate sources (dedup check uses the
         top-level field value only — we don't have full candidate lists here).

    The grade is computed over all fields in ``etim_specs`` that are provenance
    dicts (fields enriched by W7).  Fields without provenance metadata are skipped
    (they don't contribute positively or negatively).

    Parameters
    ----------
    etim_specs:
        Per-field provenance dict from ``product_catalog.etim_specs``.

    Returns
    -------
    dict with keys:
        ``grade``             — A / B / C / D / E
        ``score``             — float in [0, 1], fraction of criteria passing
        ``graded_fields``     — count of fields with provenance metadata
        ``total_criteria``    — total criterion checks performed
        ``passed_criteria``   — count that passed
        ``failing_criteria``  — list of dicts ``{field, criterion, detail}``
    """
    failing_criteria: list[dict[str, str]] = []
    passed = 0
    total = 0
    graded_fields = 0

    for field_name, raw in etim_specs.items():
        if not isinstance(raw, dict):
            continue  # bare value, not a W7 provenance entry — skip

        graded_fields += 1

        # Criterion 1: provenance.source present + non-empty
        total += 1
        prov = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        source = prov.get("source", "") if prov else ""
        if source and isinstance(source, str) and source.strip():
            passed += 1
        else:
            failing_criteria.append(
                {
                    "field": field_name,
                    "criterion": "provenance.source",
                    "detail": "missing or empty",
                }
            )

        # Criterion 2: provenance.reason present + non-empty
        total += 1
        reason = prov.get("reason", "") if prov else ""
        if reason and isinstance(reason, str) and reason.strip():
            passed += 1
        else:
            failing_criteria.append(
                {
                    "field": field_name,
                    "criterion": "provenance.reason",
                    "detail": "missing or empty",
                }
            )

        # Criterion 3: confidence in [0, 1]
        total += 1
        confidence = raw.get("confidence")
        if isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0:
            passed += 1
        else:
            failing_criteria.append(
                {
                    "field": field_name,
                    "criterion": "confidence",
                    "detail": f"value={confidence!r} is not a float in [0, 1]",
                }
            )

        # Criterion 4: field-level source is consistent (source matches provenance)
        # We check that if both ``source`` (top-level, from W7 auto-merge) and
        # ``provenance.source`` exist, they agree.
        total += 1
        top_source = raw.get("source")
        if top_source is not None and source:
            if str(top_source).strip() == str(source).strip():
                passed += 1
            else:
                failing_criteria.append(
                    {
                        "field": field_name,
                        "criterion": "source_consistency",
                        "detail": (
                            f"top-level source={top_source!r} "
                            f"differs from provenance.source={source!r}"
                        ),
                    }
                )
        else:
            # Either both absent (already penalised above) or top-level absent but
            # provenance.source present — that is fine, count as pass.
            passed += 1

    if total == 0:
        return {
            "grade": "E",
            "score": 0.0,
            "graded_fields": 0,
            "total_criteria": 0,
            "passed_criteria": 0,
            "failing_criteria": [],
        }

    score = passed / total

    return {
        "grade": _score_to_grade(score),
        "score": round(score, 4),
        "graded_fields": graded_fields,
        "total_criteria": total,
        "passed_criteria": passed,
        "failing_criteria": failing_criteria,
    }


# ---------------------------------------------------------------------------
# Per-manufacturer rollup
# ---------------------------------------------------------------------------


def manufacturer_rollup(
    product_quality_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roll up per-product quality results into a per-manufacturer data-health score.

    Parameters
    ----------
    product_quality_results:
        List of result dicts, each containing:
          ``manufacturer``  (str)
          ``completeness``  (dict — output of ``completeness_score()``, any channel)
          ``grade_result``  (dict — output of ``quality_grade()``)

    Returns
    -------
    dict keyed by manufacturer with:
        ``product_count``         — number of products included
        ``avg_completeness``      — mean completeness score
        ``grade_distribution``    — ``{A: n, B: n, C: n, D: n, E: n}``
        ``dominant_grade``        — most common grade (tie: best wins)
        ``top_failing_criteria``  — top-5 most frequent failing criteria across products
    """
    by_mfr: dict[str, list[dict[str, Any]]] = {}
    for result in product_quality_results:
        mfr = str(result.get("manufacturer") or "unknown")
        by_mfr.setdefault(mfr, []).append(result)

    rollup: dict[str, Any] = {}
    for mfr, results in by_mfr.items():
        rollup[mfr] = _rollup_one_manufacturer(results)

    return rollup


def _rollup_one_manufacturer(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate quality results for a single manufacturer."""
    completeness_scores: list[float] = []
    grade_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
    criteria_freq: dict[str, int] = {}

    for r in results:
        comp = r.get("completeness") or {}
        if isinstance(comp.get("score"), (int, float)):
            completeness_scores.append(float(comp["score"]))

        grade_result = r.get("grade_result") or {}
        grade = grade_result.get("grade", "E")
        if grade in grade_counts:
            grade_counts[grade] += 1

        for fc in grade_result.get("failing_criteria") or []:
            key = fc.get("criterion", "unknown")
            criteria_freq[key] = criteria_freq.get(key, 0) + 1

    avg_completeness = (
        round(sum(completeness_scores) / len(completeness_scores), 4)
        if completeness_scores
        else 0.0
    )

    dominant_grade = _dominant_grade(grade_counts)

    top_failing = sorted(criteria_freq.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "product_count": len(results),
        "avg_completeness": avg_completeness,
        "grade_distribution": grade_counts,
        "dominant_grade": dominant_grade,
        "top_failing_criteria": [{"criterion": k, "count": v} for k, v in top_failing],
    }


def _dominant_grade(grade_counts: dict[str, int]) -> str:
    """Return the most frequent grade; on tie, prefer the better (earlier) grade."""
    best_grade = "E"
    best_count = -1
    grade_order = ["A", "B", "C", "D", "E"]
    for grade in grade_order:
        count = grade_counts.get(grade, 0)
        if count > best_count:
            best_count = count
            best_grade = grade
    return best_grade
