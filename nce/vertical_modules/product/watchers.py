"""
nce/vertical_modules/product/watchers.py
=========================================
EOL/EOS Watcher for the Product vertical module (Wave 12).

Role: **Watcher** — observes product lifecycle state and writes
``PRODUCT -[replaced_by]-> PRODUCT`` edges only.

Invariants (never violate):
  - NEVER mutates ``product_catalog`` rows (no UPDATE on prices/status/payload).
  - ``replaced_by`` edges are written via
    :func:`~nce.vertical_modules.product.graph.upsert_product_relation_edge`.
  - ``confidence`` (0–1) lives on ``kg_edges`` only.
  - ``failure_pattern`` edge surfacing is read-only.
  - Depends inward: this module does NOT import the cron scheduler
    (the cron entry-point calls this core; not the reverse).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.product.graph import upsert_product_relation_edge

log = logging.getLogger("nce.vertical_modules.product.watchers")

# ---------------------------------------------------------------------------
# EOL signal — config-seeded list (graceful degradation when absent)
# ---------------------------------------------------------------------------
# Format: JSON list of objects:
#   [{"mfr_part_no": "...", "manufacturer": "...", "successor_mfr_part_no": "...",
#     "successor_manufacturer": "...", "confidence": 0.9}, ...]
# When NCE_PRODUCT_EOL_LIST is unset the watcher is a no-op (no error).
_EOL_LIST_ENV = "NCE_PRODUCT_EOL_LIST"
_DEFAULT_CONFIDENCE: float = 0.9


def _load_eol_list() -> list[dict[str, Any]]:
    """Return the config-seeded EOL list, or [] when absent / unparseable."""
    raw = os.environ.get(_EOL_LIST_ENV, "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
        log.warning("NCE_PRODUCT_EOL_LIST is not a JSON array — ignored")
        return []
    except json.JSONDecodeError:
        log.warning("NCE_PRODUCT_EOL_LIST is not valid JSON — ignored")
        return []


# ---------------------------------------------------------------------------
# Manufacturer adapter EOL signal (W11 adapter — optional)
# ---------------------------------------------------------------------------


def _eol_from_manufacturer_adapter() -> list[dict[str, Any]] | None:
    """Return EOL rows from the W11 manufacturer adapter if available.

    The manufacturer API adapter (W11) may expose an ``eol_products()`` method
    returning dicts with the same keys as the config-seeded list.  When the
    method does not exist or the adapter is not configured, returns ``None``
    so the caller degrades to the config list.
    """
    try:
        from nce.vertical_modules.product.sources.manufacturer_api import (
            ManufacturerApiAdapter,
        )

        adapter = ManufacturerApiAdapter()
        if not hasattr(adapter, "eol_products"):
            return None
        return adapter.eol_products()  # type: ignore[return-value]
    except Exception:
        return None


def _resolve_eol_entries() -> list[dict[str, Any]]:
    """Return EOL entries from the W11 adapter if available, else config list.

    Degrades gracefully: if neither source is present, returns an empty list
    so the watcher becomes a harmless no-op.
    """
    adapter_rows = _eol_from_manufacturer_adapter()
    if adapter_rows is not None:
        return adapter_rows
    return _load_eol_list()


# ---------------------------------------------------------------------------
# Product-node existence check
# ---------------------------------------------------------------------------


async def _product_label_for(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    manufacturer: str,
    mfr_part_no: str,
) -> str | None:
    """Return the ``kg_nodes`` label if the PRODUCT_SKU node exists, else None."""
    label = f"PRODUCT:{manufacturer.upper()}:{mfr_part_no.upper()}"
    row = await conn.fetchrow(
        """
        SELECT label FROM kg_nodes
        WHERE label = $1 AND namespace_id = $2::uuid
        """,
        label,
        str(namespace_id),
    )
    return row["label"] if row else None


# ---------------------------------------------------------------------------
# failure_pattern edge surfacing (read-only Advisor helper)
# ---------------------------------------------------------------------------


async def get_failure_patterns(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    product_label: str,
) -> list[dict[str, Any]]:
    """Return all ``failure_pattern`` edges for a product node (read-only).

    This is the Watcher/Advisor surfacing point for service/failure data
    captured by other engines (Support, Assets).  The result is included
    in the :func:`do_check_eol` output dict so callers see both EOL
    replacement edges and any associated failure patterns.

    Parameters
    ----------
    conn:
        asyncpg connection with RLS GUC already set.
    namespace_id:
        Active namespace UUID.
    product_label:
        The ``kg_nodes`` label for the target PRODUCT_SKU node.

    Returns
    -------
    list[dict[str, Any]]
        Each dict contains ``subject_label``, ``predicate``, ``object_label``,
        ``confidence``.  Empty when no edges exist.
    """
    rows = await conn.fetch(
        """
        SELECT subject_label, predicate, object_label, confidence
        FROM kg_edges
        WHERE predicate = 'failure_pattern'
          AND (subject_label = $1 OR object_label = $1)
          AND namespace_id = $2::uuid
        ORDER BY confidence DESC NULLS LAST
        """,
        product_label,
        str(namespace_id),
    )
    return [
        {
            "subject_label": r["subject_label"],
            "predicate": r["predicate"],
            "object_label": r["object_label"],
            "confidence": r["confidence"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Core watcher: do_check_eol
# ---------------------------------------------------------------------------


async def do_check_eol(
    engine: Any,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Detect EOL/EOS products and write ``replaced_by`` edges to successors.

    Watcher discipline:
      - Reads ``product_catalog`` and ``kg_nodes`` (SELECT only).
      - Writes ``replaced_by`` edges via
        :func:`~nce.vertical_modules.product.graph.upsert_product_relation_edge`.
      - Never mutates ``product_catalog`` rows, prices, or lifecycle_status.

    EOL signal priority:
      1. W11 manufacturer adapter ``eol_products()`` if available.
      2. ``NCE_PRODUCT_EOL_LIST`` JSON env-var (config-seeded list).
      3. ``product_catalog`` rows with ``lifecycle_status`` in
         ``{'eol', 'eos', 'end_of_life', 'end_of_sale', 'discontinued'}``
         that have a ``successor_sku`` column (if present in schema).
      4. No signal → no-op, returns ``{"edges_written": 0}``.

    Parameters
    ----------
    engine:
        Object with a ``pg_pool`` attribute (``asyncpg.Pool``).
    args:
        Must contain ``namespace_id`` (str or UUID).

    Returns
    -------
    dict
        ``edges_written``: number of ``replaced_by`` edges upserted.
        ``failure_patterns``: list of failure_pattern edge dicts for each
        processed product (read-only Advisor output).
        ``skipped``: number of entries skipped (missing node / bad data).
    """
    namespace_id: UUID = UUID(str(args["namespace_id"]))
    pool: asyncpg.Pool = engine.pg_pool

    eol_entries = _resolve_eol_entries()

    edges_written = 0
    skipped = 0
    all_failure_patterns: list[dict[str, Any]] = []

    if not eol_entries:
        # Degrade to product_catalog scan for known EOL lifecycle statuses
        eol_entries = await _scan_catalog_for_eol(pool, namespace_id)

    if not eol_entries:
        log.debug("do_check_eol: no EOL signal for namespace %s — no-op", namespace_id)
        return {"edges_written": 0, "failure_patterns": [], "skipped": 0}

    for entry in eol_entries:
        try:
            result = await _process_eol_entry(pool, namespace_id, entry, all_failure_patterns)
            edges_written += result["edges_written"]
            skipped += result["skipped"]
        except Exception:
            log.exception(
                "do_check_eol: failed to process entry %r for namespace %s",
                entry,
                namespace_id,
            )
            skipped += 1

    log.info(
        "do_check_eol: namespace=%s edges_written=%s skipped=%s failure_patterns=%s",
        namespace_id,
        edges_written,
        skipped,
        len(all_failure_patterns),
    )
    return {
        "edges_written": edges_written,
        "failure_patterns": all_failure_patterns,
        "skipped": skipped,
    }


async def _scan_catalog_for_eol(
    pool: asyncpg.Pool,
    namespace_id: UUID,
) -> list[dict[str, Any]]:
    """Scan ``product_catalog`` for EOL lifecycle statuses.

    Returns entries in the same shape as the config-seeded list.
    Only includes rows that have a non-empty ``successor_sku`` column
    (if that column exists in the schema).
    """
    _EOL_STATUSES = frozenset({"eol", "eos", "end_of_life", "end_of_sale", "discontinued"})

    entries: list[dict[str, Any]] = []
    try:
        async with scoped_pg_session(pool, namespace_id) as conn:
            # Check if successor_sku column exists
            has_successor = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'product_catalog'
                      AND column_name = 'successor_sku'
                )
                """
            )
            if not has_successor:
                return entries

            rows = await conn.fetch(
                """
                SELECT manufacturer, mfr_part_no, successor_sku,
                       COALESCE(lifecycle_confidence, $1) AS confidence
                FROM product_catalog
                WHERE LOWER(lifecycle_status) = ANY($2::text[])
                  AND successor_sku IS NOT NULL
                  AND successor_sku <> ''
                """,
                _DEFAULT_CONFIDENCE,
                list(_EOL_STATUSES),
            )
            for row in rows:
                # successor_sku may be "MANUFACTURER:PART_NO" or just "PART_NO"
                successor = str(row["successor_sku"]).strip()
                if ":" in successor:
                    succ_mfr, succ_part = successor.split(":", 1)
                else:
                    succ_mfr = row["manufacturer"]
                    succ_part = successor
                entries.append(
                    {
                        "manufacturer": row["manufacturer"],
                        "mfr_part_no": row["mfr_part_no"],
                        "successor_manufacturer": succ_mfr,
                        "successor_mfr_part_no": succ_part,
                        "confidence": float(row["confidence"]),
                    }
                )
    except Exception:
        log.exception("do_check_eol: catalog scan failed for namespace %s — no-op", namespace_id)
    return entries


async def _process_eol_entry(
    pool: asyncpg.Pool,
    namespace_id: UUID,
    entry: dict[str, Any],
    all_failure_patterns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Process a single EOL entry: upsert replaced_by edge + surface failure patterns.

    Returns ``{"edges_written": int, "skipped": int}``.
    """
    manufacturer = str(entry.get("manufacturer", "")).strip()
    mfr_part_no = str(entry.get("mfr_part_no", "")).strip()
    succ_manufacturer = str(
        entry.get("successor_manufacturer", entry.get("manufacturer", ""))
    ).strip()
    succ_part_no = str(entry.get("successor_mfr_part_no", "")).strip()
    confidence = float(entry.get("confidence", _DEFAULT_CONFIDENCE))

    if not manufacturer or not mfr_part_no or not succ_part_no:
        log.debug("do_check_eol: skipping entry with missing required fields: %r", entry)
        return {"edges_written": 0, "skipped": 1}

    async with scoped_pg_session(pool, namespace_id) as conn:
        subject_label = await _product_label_for(conn, namespace_id, manufacturer, mfr_part_no)
        if subject_label is None:
            log.debug(
                "do_check_eol: subject node not found for %s:%s — skipping",
                manufacturer,
                mfr_part_no,
            )
            return {"edges_written": 0, "skipped": 1}

        object_label = await _product_label_for(conn, namespace_id, succ_manufacturer, succ_part_no)
        if object_label is None:
            log.debug(
                "do_check_eol: successor node not found for %s:%s — skipping",
                succ_manufacturer,
                succ_part_no,
            )
            return {"edges_written": 0, "skipped": 1}

        # Write replaced_by edge — the ONLY write this watcher performs.
        await upsert_product_relation_edge(
            conn,
            namespace_id,
            subject_label=subject_label,
            predicate="replaced_by",
            object_label=object_label,
            confidence=confidence,
        )

        # Surface failure_pattern edges (read-only Advisor output).
        fp = await get_failure_patterns(conn, namespace_id, subject_label)
        all_failure_patterns.extend(fp)

    return {"edges_written": 1, "skipped": 0}
