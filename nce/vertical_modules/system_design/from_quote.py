"""
nce/vertical_modules/system_design/from_quote.py
=================================================
Quote-first entry point for the System Design vertical module (Wave 4).

Entry-point: ``do_design_from_quote(engine, params) -> dict``

Goal
----
Given a Sales-owned ``QUOTE`` (identified by ``quote_id``), lift each
quote line — already tagged to a functional location — into a ``DESIGN``
(one ``DESIGN_LINE`` per quote line on the same ``FUNCTIONAL_LOCATION``),
then gap-fill missing accessories/infrastructure/labor via the Wave 3 recall
loop.  Finally, write the cross-engine edge
``QUOTE -[realized_as]-> DESIGN`` with confidence.

Ownership (Contract A §9.1)
---------------------------
Sales owns the ``QUOTE`` node.  This engine:

  - NEVER writes or mutates a ``QUOTE`` kg_nodes row.
  - Only authors ``DESIGN`` + ``DESIGN_LINE`` + ``FUNCTIONAL_LOCATION``
    nodes (via ``graph.do_author_functional_location``).
  - Writes the cross-engine edge ``QUOTE -[realized_as]-> DESIGN`` by
    label only (kg_edges has no FK constraint to kg_nodes — referencing a
    QUOTE label in an edge is safe; upsert-QUOTE-into-kg_nodes is NOT).

A2A seam
--------
Sales owns the QUOTE data.  This module reads quote lines through the
injectable coroutine ``_read_quote_lines(engine, namespace_id, quote_id)``.
The default implementation delegates to the Sales engine tool by name via the
A2A transport (resolved at runtime); the test suite replaces it with a mock
that supplies deterministic quote lines — no Sales engine is required in
integration tests.

The seam is a module-level reference so ``unittest.mock.patch`` on
``nce.vertical_modules.system_design.from_quote._read_quote_lines``
replaces it cleanly.

Design invariants (uncle-bob-craft)
------------------------------------
- SRP per function: each private helper has one job.
- Dependencies point inward: no web/HTTP/admin imports.
- confidence on edges only (wave rule 7 — never on kg_nodes).
- No phantom payload/metadata/state column (kg_nodes has none).
- PROPOSE-ONLY gap-fill: every gap-fill line has ``validated=False``.
- No slow I/O inside scoped_pg_session transactions.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.events.emit import emit_graph_write
from nce.vertical_modules.system_design.graph import (
    _design_label,
    _upsert_edge,
    do_author_functional_location,
)
from nce.vertical_modules.system_design.propose import do_propose_design

log = logging.getLogger("nce.vertical_modules.system_design.from_quote")

# Edge predicate: QUOTE → DESIGN cross-engine link.
_PRED_REALIZED_AS: str = "realized_as"

# Quote label prefix — matches the Sales engine label convention.
_QUOTE_LABEL_PREFIX: str = "QUOTE:"

# Confidence for the quote→design realization edge.
_REALIZATION_CONFIDENCE: float = 0.9

# Namespace slug used when one is not supplied (tests / unknown slug paths).
_FALLBACK_NS_SLUG: str = "ns"


# ---------------------------------------------------------------------------
# A2A seam — injectable for tests
# ---------------------------------------------------------------------------


async def _read_quote_lines(
    engine: Any,
    namespace_id: UUID,
    quote_id: str,
) -> list[dict[str, Any]]:
    """Read quote lines from the Sales engine (tool ``sales_get_quote_lines``).

    This is the **loose-coupling seam** between System Design and Sales. Until
    Batch 132f it raised ``NotImplementedError`` unconditionally, which made
    ``POST /api/system-design/from-quote`` return 500 on every call once Batch
    230a mounted it (ledger defect **D47**). It now delegates to the Sales
    read, which is namespace-scoped in SQL.

    Signature is load-bearing: ``from_quote`` calls it positionally and tests
    patch it by this path. Do not change it.

    Returned dicts are ``bom_line_content`` rows, so each carries ``line_ref``
    and ``qty`` -- the only fields ``do_design_from_quote`` needs. The other
    four fields this seam once documented (``fl_path``, ``manufacturer``,
    ``mfr_part_no``, ``confidence``) have **no column in the store**; that is
    ledger defect **D37**, tracked separately. ``do_design_from_quote`` already
    supplies ``"UNKNOWN"``/``[]``/``1.0`` for them via ``.get()``. Nothing here
    fabricates a value -- a fabricated attribute is indistinguishable
    downstream from an authored one.

    A2A tool name (advertised for external callers): ``sales_get_quote_lines``
    """
    # Lazy import: System Design must not hard-import a Sales module at module
    # load. Deferring it here keeps the two verticals independently importable.
    from nce.vertical_modules.sales.lines import do_get_quote_lines

    return await do_get_quote_lines(engine, namespace_id, quote_id)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _quote_label(quote_id: str) -> str:
    """Canonical QUOTE label: ``QUOTE:<QUOTE_ID>`` (upper-cased)."""
    return f"{_QUOTE_LABEL_PREFIX}{quote_id.upper()}"


def _extract_site_name(fl_path: list[str]) -> str:
    """Return the site name (first element of the functional-location path).

    Falls back to ``"SITE"`` when the path is empty to keep the graph
    writer happy — callers are responsible for supplying well-formed paths.
    """
    return fl_path[0] if fl_path else "SITE"


def _build_buildings_from_fl_path(fl_path: list[str]) -> list[dict[str, Any]]:
    """Convert a flat FL path into the ``buildings`` structure expected by graph.py.

    Supports paths of the form ``[site, building]``,
    ``[site, building, floor]``, ``[site, building, floor, room]``, and
    ``[site, building, floor, room, position]``.  Elements beyond position
    are ignored.
    """
    if len(fl_path) < 2:  # noqa: PLR2004
        # No building info — return empty tree; DESIGN will contain no SITE→BUILDING edge.
        return []

    building_name = fl_path[1]
    floor_name = fl_path[2] if len(fl_path) > 2 else None  # noqa: PLR2004
    room_name = fl_path[3] if len(fl_path) > 3 else None  # noqa: PLR2004
    position_name = fl_path[4] if len(fl_path) > 4 else None  # noqa: PLR2004

    room: dict[str, Any] = (
        {"name": room_name, "positions": [position_name] if position_name else []}
        if room_name
        else {}
    )
    floor: dict[str, Any] = (
        {"name": floor_name, "rooms": [room] if room else []} if floor_name else {}
    )
    building: dict[str, Any] = {"name": building_name, "floors": [floor] if floor else []}
    return [building]


def _clamp_confidence(value: Any) -> float:
    """Clamp a confidence value into the ``[0.0, 1.0]`` range required by kg_edges.

    Recall similarity is ``1 - cosine_distance``; with un-normalised or
    fallback embedding vectors the distance can exceed 1, yielding a negative
    similarity.  kg_edges enforces ``confidence`` in ``[0, 1]`` (wave rule 7 +
    DB CHECK), so any recall-derived score must be clamped before it is written
    as an edge.
    """
    return max(0.0, min(1.0, float(value)))


def _build_gap_fill_lines(
    proposed_lines: list[dict[str, Any]],
    line_ref_prefix: str,
) -> list[dict[str, Any]]:
    """Convert Wave 3 proposed lines into DESIGN_LINE dicts for graph.py.

    Every gap-fill line is ``validated=False`` (propose-only invariant).
    ``line_ref`` is generated from the prefix + index to avoid collisions
    with quote-originated lines.
    """
    result: list[dict[str, Any]] = []
    for idx, pl in enumerate(proposed_lines):
        product_ref: str = pl.get("product_ref", "")
        if not product_ref:
            continue
        # product_ref may be "MANUFACTURER:PART_NO" or just a part number.
        parts = product_ref.split(":", 1)
        manufacturer = parts[0] if len(parts) >= 2 else "UNKNOWN"  # noqa: PLR2004
        mfr_part_no = parts[1] if len(parts) >= 2 else product_ref  # noqa: PLR2004
        result.append(
            {
                "line_ref": f"{line_ref_prefix}-GAPFILL-{idx}",
                "manufacturer": manufacturer,
                "mfr_part_no": mfr_part_no,
                "confidence": _clamp_confidence(pl.get("confidence", 0.0)),
                "validated": False,  # PROPOSE-ONLY invariant
            }
        )
    return result


async def _gap_fill_for_lines(
    engine: Any,
    namespace_id: UUID,
    design_id: str,
    fl_labels_seen: list[str],
) -> list[dict[str, Any]]:
    """Run the Wave 3 recall loop to gap-fill missing accessories/infra/labor.

    Issues one recall per distinct functional-location label and collects
    proposed DESIGN_LINE dicts.  All returned lines have ``validated=False``.
    """
    gap_fill_lines: list[dict[str, Any]] = []
    for fl_label in fl_labels_seen:
        room_brief = f"functional location: {fl_label}"
        try:
            propose_result = await do_propose_design(
                engine,
                {"namespace_id": str(namespace_id), "room_brief": room_brief},
            )
        except Exception:
            log.warning(
                "do_design_from_quote: gap-fill recall failed for fl=%s (skipped)",
                fl_label,
                exc_info=True,
            )
            continue

        proposed = propose_result.get("proposed_lines", [])
        gap_fill_lines.extend(
            _build_gap_fill_lines(proposed, line_ref_prefix=f"{design_id}-{fl_label[:16]}")
        )

    return gap_fill_lines


# ---------------------------------------------------------------------------
# Public: do_design_from_quote
# ---------------------------------------------------------------------------


async def do_design_from_quote(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Realise a Sales-owned QUOTE into a DESIGN proposal.

    Reads quote lines via the A2A seam (``_read_quote_lines``), lifts each
    line into a ``DESIGN`` + one ``DESIGN_LINE`` per line on the same
    ``FUNCTIONAL_LOCATION``, gap-fills missing accessories/infra/labor via
    the Wave 3 recall loop, and writes the cross-engine edge
    ``QUOTE -[realized_as]-> DESIGN``.

    **Contract A (§9.1):** This function NEVER writes or mutates a
    ``QUOTE`` kg_nodes row.  The QUOTE label appears only in the
    ``subject_label`` of a kg_edge — never in kg_nodes.

    Parameters
    ----------
    engine:
        NCEEngine instance.  Must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "quote_id": str,             # required — Sales QUOTE identifier
            "design_id": str,            # optional — defaults to DESIGN-<quote_id>
            "namespace_slug": str,       # optional — for FL label prefix
            "source_id": str | None,     # optional — system_design source id
        }``

    Returns
    -------
    dict
        ``{
            "design_id": str,
            "quote_label": str,            # QUOTE:<QUOTE_ID>
            "design_label": str,           # DESIGN:<DESIGN_ID>
            "authored": {"nodes": int, "edges": int},
            "quote_lines_realized": int,   # lines from the quote
            "gap_fill_lines": int,         # additional recalled lines
            "realized_as_edge": str,       # "QUOTE:... -[realized_as]-> DESIGN:..."
        }``

    Raises
    ------
    ValueError
        When ``namespace_id`` or ``quote_id`` is missing.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_design_from_quote: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    quote_id: str = params.get("quote_id", "")
    if not quote_id:
        raise ValueError("do_design_from_quote: 'quote_id' is required in params")

    design_id: str = params.get("design_id") or f"DESIGN-{quote_id}"
    namespace_slug: str = params.get("namespace_slug") or _FALLBACK_NS_SLUG
    source_id: str | None = params.get("source_id")

    # 1. Read quote lines via A2A seam — NO DB transaction open yet.
    #    (Do not perform slow I/O inside a scoped_pg_session transaction.)
    quote_lines: list[dict[str, Any]] = await _read_quote_lines(engine, ns_uuid, quote_id)

    if not quote_lines:
        log.warning(
            "do_design_from_quote: quote %s returned 0 lines (empty design)",
            quote_id,
        )

    # 2. Gap-fill via Wave 3 recall — also outside the DB transaction.
    fl_labels_seen: list[str] = [
        ":".join([namespace_slug.upper()] + [p.upper() for p in ql.get("fl_path", [])])
        for ql in quote_lines
        if ql.get("fl_path")
    ]
    gap_fill_dl: list[dict[str, Any]] = await _gap_fill_for_lines(
        engine, ns_uuid, design_id, fl_labels_seen
    )

    # 3. Build DESIGN_LINE list: quote lines + gap-fill lines.
    design_lines: list[dict[str, Any]] = []
    for ql in quote_lines:
        design_lines.append(
            {
                "line_ref": ql["line_ref"],
                "manufacturer": ql.get("manufacturer", "UNKNOWN"),
                "mfr_part_no": ql.get("mfr_part_no", "UNKNOWN"),
                "confidence": float(ql.get("confidence", 1.0)),
                "source_id": source_id,
            }
        )
    for gfl in gap_fill_dl:
        design_lines.append(
            {
                "line_ref": gfl["line_ref"],
                "manufacturer": gfl["manufacturer"],
                "mfr_part_no": gfl["mfr_part_no"],
                "confidence": gfl["confidence"],
                "source_id": source_id,
            }
        )

    # 4. Determine site structure from the first quote line with an fl_path.
    site_name = "SITE"
    buildings: list[dict[str, Any]] = []
    for ql in quote_lines:
        fl_path: list[str] = ql.get("fl_path", [])
        if fl_path:
            site_name = _extract_site_name(fl_path)
            buildings = _build_buildings_from_fl_path(fl_path)
            break

    # 5. Write DESIGN + FUNCTIONAL_LOCATION tree + DESIGN_LINE nodes inside
    #    a single scoped session (RLS-guarded transaction).
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        authored = await do_author_functional_location(
            conn,
            ns_uuid,
            namespace_slug=namespace_slug,
            design_id=design_id,
            site_name=site_name,
            buildings=buildings,
            design_lines=design_lines,
            source_id=source_id,
        )

        # 6. Write QUOTE -[realized_as]-> DESIGN edge (Contract A: edge only,
        #    no QUOTE kg_nodes row).
        quote_lbl = _quote_label(quote_id)
        design_lbl = _design_label(design_id)
        await _upsert_edge(
            conn,
            ns_uuid,
            quote_lbl,
            _PRED_REALIZED_AS,
            design_lbl,
            _REALIZATION_CONFIDENCE,
            source_id,
        )
        authored["edges"] = authored.get("edges", 0) + 1

        # 7. Emit graph-write event for the realization edge.
        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type="QUOTE",
            op="edge_realized_as",
            node_id=quote_lbl,
        )

    realized_as_desc = f"{quote_lbl} -[{_PRED_REALIZED_AS}]-> {design_lbl}"
    log.info(
        "do_design_from_quote: ns=%s quote=%s design=%s lines=%d gap_fill=%d edge=%s",
        ns_uuid,
        quote_id,
        design_id,
        len(quote_lines),
        len(gap_fill_dl),
        realized_as_desc,
    )

    return {
        "design_id": design_id,
        "quote_label": quote_lbl,
        "design_label": design_lbl,
        "authored": authored,
        "quote_lines_realized": len(quote_lines),
        "gap_fill_lines": len(gap_fill_dl),
        "realized_as_edge": realized_as_desc,
    }
