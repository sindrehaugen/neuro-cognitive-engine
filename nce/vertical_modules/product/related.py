"""
nce/vertical_modules/product/related.py
=========================================
Core: do_related_products — derives accessory/warranty/mount/replacement
relations between products in the catalog and persists them as graph edges.

Graph contribution
------------------
Four predicates are written to ``kg_edges``:

  ``PRODUCT -[accessory_of]-> PRODUCT``   confidence 0–1
  ``PRODUCT -[warranty_for]-> PRODUCT``   confidence 0–1
  ``PRODUCT -[mounts]-> PRODUCT``         confidence 0–1
  ``PRODUCT -[replaced_by]-> PRODUCT``    confidence 0–1

``confidence`` lives **on the edge only** (rule 7 — never on the node).

Derivation logic (reconstructed from Portal sidecar description)
-----------------------------------------------------------------
1. ``_extract_model_tokens(manufacturer, mfr_part_no)`` — split the part
   number on ``[-/ _.]`` and upper-case each token; prepend the manufacturer
   token.  Tokens are the matching surface used by ``_classify_relation`` and
   ``_find_replacements``.

2. ``_classify_relation(subject, candidate)`` — deterministic keyword-based
   classifier that returns (predicate, confidence) or None:
   * ``warranty_for``  — candidate ``mfr_part_no`` contains tokens like
     "WARR", "WARRANTY", "CARE", "MAINT", "SVC", "SERVICE"; conf 0.9.
   * ``mounts``        — candidate ``mfr_part_no`` contains "MOUNT", "RACK",
     "BRACKET", "TRAY", "RAIL"; conf 0.85.
   * ``accessory_of``  — candidate ``mfr_part_no`` shares at least 2 model
     tokens with subject AND is not already matched above; conf
     proportional to shared-token ratio ∈ [0.5, 0.8].
   Returns ``None`` when nothing matches.

3. ``_find_replacements(subject, candidates)`` — identifies products that
   replace the subject.  A candidate ``replaced_by`` subject when:
   * the candidate's ``lifecycle_status`` is ``"active"`` while subject's is
     ``"eol"`` or ``"discontinued"``; AND
   * they share the same manufacturer AND at least 2 model tokens.
   Confidence is fixed at 0.95 (direct manufacturer match + lifecycle signal).

Design invariants (uncle-bob-craft)
------------------------------------
  - No web / HTTP / admin imports; pure domain-core.
  - One function, one job.
  - All derivation helpers are private (_) and pure (no I/O).
  - DB writes are delegated entirely to ``graph.upsert_product_relation_edge``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nce.db_utils import scoped_pg_session
from nce.mcp_args import require_namespace_id
from nce.vertical_modules.product.graph import upsert_product_relation_edge

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.product.related")


# ---------------------------------------------------------------------------
# Config-as-IP: load business weights from product-relation-weights.json
# ---------------------------------------------------------------------------


def _load_relation_weights() -> dict[str, Any]:
    """Load relation confidence weights from config_data (config-as-IP)."""
    config_path = (
        Path(__file__).resolve().parent.parent.parent
        / "config_data"
        / "product-relation-weights.json"
    )
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


_WEIGHTS: dict[str, Any] = _load_relation_weights()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Predicates this engine is authorised to write.
_PREDICATES: frozenset[str] = frozenset({"accessory_of", "warranty_for", "mounts", "replaced_by"})

#: Part-number token splitter — splits on dash, slash, space, underscore, dot.
_TOKEN_SPLIT_RE: re.Pattern[str] = re.compile(r"[-/\s_.]")

#: Keywords that indicate a warranty / service extension product.
_WARRANTY_TOKENS: frozenset[str] = frozenset(
    {"WARR", "WARRANTY", "CARE", "MAINT", "SVC", "SERVICE", "SUPPORT", "SUP"}
)

#: Keywords that indicate a mounting / rack accessory.
_MOUNT_TOKENS: frozenset[str] = frozenset(
    {"MOUNT", "RACK", "BRACKET", "TRAY", "RAIL", "KIT", "RKMNT"}
)

#: Lifecycle values that signal a product is at end-of-life.
_EOL_STATUSES: frozenset[str] = frozenset({"eol", "discontinued", "obsolete"})

# Business weights loaded from config_data/product-relation-weights.json (config-as-IP).
#: Minimum shared model tokens for an accessory or replacement match.
_MIN_SHARED_TOKENS: int = int(_WEIGHTS["min_shared_tokens"])

#: Confidence scores — loaded from JSON, not literals.
_CONF_WARRANTY: float = float(_WEIGHTS["conf_warranty"])
_CONF_MOUNT: float = float(_WEIGHTS["conf_mount"])
_CONF_REPLACEMENT: float = float(_WEIGHTS["conf_replacement"])

#: Accessory confidence range — loaded from JSON.
_CONF_ACCESSORY_MIN: float = float(_WEIGHTS["conf_accessory_min"])
_CONF_ACCESSORY_MAX: float = float(_WEIGHTS["conf_accessory_max"])

#: Maximum catalog rows to process in one call (guard against table scans).
_CATALOG_QUERY_LIMIT: int = int(_WEIGHTS["catalog_query_limit"])


# ---------------------------------------------------------------------------
# Private: token extraction
# ---------------------------------------------------------------------------


def _extract_model_tokens(manufacturer: str, mfr_part_no: str) -> list[str]:
    """Return upper-cased, split tokens for (manufacturer, part_no).

    Example: ("Cisco", "SFP-10G-SR") → ["CISCO", "SFP", "10G", "SR"]

    The manufacturer is prepended as its own token so cross-manufacturer
    matches are naturally penalised (they can share many part-number tokens
    while the first token differs).
    """
    tokens: list[str] = [manufacturer.upper().strip()]
    part_tokens = [t for t in _TOKEN_SPLIT_RE.split(mfr_part_no.upper()) if t]
    tokens.extend(part_tokens)
    return tokens


# ---------------------------------------------------------------------------
# Private: relation classifiers (pure — no I/O)
# ---------------------------------------------------------------------------


def _classify_relation(
    subject_tokens: list[str],
    candidate_mfr: str,
    candidate_part: str,
) -> tuple[str, float] | None:
    """Return (predicate, confidence) or None.

    Priority order (highest specificity first):
      1. warranty_for  — part-number keyword match.
      2. mounts        — mounting keyword match.
      3. accessory_of  — shared model-token match (>= _MIN_SHARED_TOKENS,
                          same manufacturer).

    Parameters
    ----------
    subject_tokens:
        Tokens of the subject product (manufacturer prepended).
    candidate_mfr:
        Manufacturer string of the candidate.
    candidate_part:
        Part-number string of the candidate.
    """
    candidate_tokens = _extract_model_tokens(candidate_mfr, candidate_part)
    part_upper = candidate_part.upper()

    # 1. Warranty signal — keyword in the candidate part number.
    if any(kw in part_upper for kw in _WARRANTY_TOKENS):
        return ("warranty_for", _CONF_WARRANTY)

    # 2. Mount signal — keyword in the candidate part number.
    if any(kw in part_upper for kw in _MOUNT_TOKENS):
        return ("mounts", _CONF_MOUNT)

    # 3. Accessory — shared manufacturer + enough shared model tokens.
    #    Skip if the manufacturers differ (first token is the manufacturer).
    if subject_tokens[0] != candidate_tokens[0]:
        return None

    # Count shared non-manufacturer tokens (skip index 0).
    shared = len(set(subject_tokens[1:]) & set(candidate_tokens[1:]))
    if shared < _MIN_SHARED_TOKENS:
        return None

    # Confidence scales with the fraction of shared tokens relative to the
    # smaller part's token set, clamped to [_CONF_ACCESSORY_MIN, _CONF_ACCESSORY_MAX].
    total_unique = len(set(subject_tokens[1:]) | set(candidate_tokens[1:]))
    raw = shared / max(total_unique, 1)
    conf = _CONF_ACCESSORY_MIN + raw * (_CONF_ACCESSORY_MAX - _CONF_ACCESSORY_MIN)
    conf = max(_CONF_ACCESSORY_MIN, min(_CONF_ACCESSORY_MAX, conf))
    return ("accessory_of", conf)


def _find_replacements(
    subject_status: str,
    subject_tokens: list[str],
    candidates: list[dict[str, Any]],
) -> list[tuple[str, str, float]]:
    """Return list of (mfr, mfr_part_no, confidence) for products that replace subject.

    A candidate replaces the subject when:
    * subject is EOL/discontinued/obsolete AND candidate is active.
    * Same manufacturer (first token matches).
    * At least _MIN_SHARED_TOKENS model tokens in common.
    """
    if subject_status not in _EOL_STATUSES:
        return []

    results: list[tuple[str, str, float]] = []
    for cand in candidates:
        cand_status = (cand.get("lifecycle_status") or "").lower()
        if cand_status != "active":
            continue
        cand_tokens = _extract_model_tokens(cand["manufacturer"], cand["mfr_part_no"])
        if cand_tokens[0] != subject_tokens[0]:
            continue
        shared = len(set(subject_tokens[1:]) & set(cand_tokens[1:]))
        if shared >= _MIN_SHARED_TOKENS:
            results.append((cand["manufacturer"], cand["mfr_part_no"], _CONF_REPLACEMENT))
    return results


# ---------------------------------------------------------------------------
# Public core
# ---------------------------------------------------------------------------


async def do_related_products(engine: NCEEngine, params: dict[str, Any]) -> dict[str, Any]:
    """Derive and persist related-product graph edges for a given product.

    Derives ``accessory_of``, ``warranty_for``, ``mounts``, and
    ``replaced_by`` relations for the subject product from the namespace's
    ``product_catalog``.  Each derived relation is persisted as a
    ``kg_edges`` row with ``confidence`` **on the edge**.

    Parameters
    ----------
    engine:
        Live NCEEngine instance (provides ``pg_pool``).
    params:
        ``namespace_id``  (str, required) — tenant namespace UUID.
        ``mfr_part_no``   (str, required) — part number of the subject product.
        ``manufacturer``  (str, required) — manufacturer of the subject product.

    Returns
    -------
    dict with keys:
      ``subject``           — canonical label of the subject PRODUCT node.
      ``accessory_of``      — list of {label, confidence} dicts.
      ``warranty_for``      — list of {label, confidence} dicts.
      ``mounts``            — list of {label, confidence} dicts.
      ``replaced_by``       — list of {label, confidence} dicts.
      ``edges_written``     — total number of edge upserts performed.
    """
    namespace_id = require_namespace_id(params)

    raw_part = str(params.get("mfr_part_no") or "").strip()
    if not raw_part:
        raise ValueError("'mfr_part_no' is required")

    raw_mfr = str(params.get("manufacturer") or "").strip()
    if not raw_mfr:
        raise ValueError("'manufacturer' is required")

    async with scoped_pg_session(engine.pg_pool, namespace_id) as conn:
        # Fetch subject product to confirm existence and get lifecycle_status.
        subject_row = await conn.fetchrow(
            """
            SELECT manufacturer, mfr_part_no, lifecycle_status
            FROM   product_catalog
            WHERE  is_deleted = false
              AND  mfr_part_no  = $1
              AND  manufacturer = $2
            LIMIT  1
            """,
            raw_part,
            raw_mfr,
        )

        if subject_row is None:
            raise ValueError(
                f"Product not found: manufacturer={raw_mfr!r} mfr_part_no={raw_part!r}"
            )

        subject_mfr = subject_row["manufacturer"]
        subject_part = subject_row["mfr_part_no"]
        subject_status = (subject_row["lifecycle_status"] or "").lower()

        # Fetch candidate products (same namespace, not the subject itself).
        candidate_rows = await conn.fetch(
            """
            SELECT manufacturer, mfr_part_no, lifecycle_status
            FROM   product_catalog
            WHERE  is_deleted = false
              AND  NOT (mfr_part_no = $1 AND manufacturer = $2)
            LIMIT  $3
            """,
            raw_part,
            raw_mfr,
            _CATALOG_QUERY_LIMIT,
        )

        subject_tokens = _extract_model_tokens(subject_mfr, subject_part)
        subject_label = f"PRODUCT:{subject_mfr.upper()}:{subject_part.upper()}"

        # Classify candidates into accessory/warranty/mount groups.
        groups: dict[str, list[dict[str, Any]]] = {
            "accessory_of": [],
            "warranty_for": [],
            "mounts": [],
            "replaced_by": [],
        }

        replacement_hits = _find_replacements(
            subject_status,
            subject_tokens,
            [dict(r) for r in candidate_rows],
        )
        replacement_labels: set[str] = set()
        for cand_mfr, cand_part, conf in replacement_hits:
            cand_label = f"PRODUCT:{cand_mfr.upper()}:{cand_part.upper()}"
            replacement_labels.add(cand_label)
            groups["replaced_by"].append({"label": cand_label, "confidence": conf})

        for row in candidate_rows:
            cand_mfr = row["manufacturer"]
            cand_part = row["mfr_part_no"]
            cand_label = f"PRODUCT:{cand_mfr.upper()}:{cand_part.upper()}"
            if cand_label in replacement_labels:
                continue  # already captured as replaced_by

            result = _classify_relation(subject_tokens, cand_mfr, cand_part)
            if result is None:
                continue
            predicate, confidence = result
            groups[predicate].append({"label": cand_label, "confidence": confidence})

        # Persist edges.
        edges_written = 0
        for predicate, hits in groups.items():
            for hit in hits:
                await upsert_product_relation_edge(
                    conn,
                    namespace_id,
                    subject_label=subject_label,
                    predicate=predicate,
                    object_label=hit["label"],
                    confidence=hit["confidence"],
                )
                edges_written += 1

        log.info(
            "do_related_products: subject=%s edges_written=%d",
            subject_label,
            edges_written,
        )

    return {
        "subject": subject_label,
        "accessory_of": groups["accessory_of"],
        "warranty_for": groups["warranty_for"],
        "mounts": groups["mounts"],
        "replaced_by": groups["replaced_by"],
        "edges_written": edges_written,
    }
