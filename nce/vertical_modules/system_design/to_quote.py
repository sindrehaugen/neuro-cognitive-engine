"""
nce/vertical_modules/system_design/to_quote.py
===============================================
Design-first exit for the System Design vertical module (Wave 7).

Entry-point: ``do_design_to_quote(engine, params) -> dict``

Goal
----
Given a completed ``DESIGN`` (identified by ``design_id``), freeze the
design's BOM at the current design version, write the cross-engine edge
``DESIGN -[becomes]-> QUOTE`` with confidence, and hand a **quote
proposal** to Sales via A2A.

Ownership (Contract A §9.1)
---------------------------
Sales owns the ``QUOTE`` node.  This engine:

  - NEVER writes or mutates a ``QUOTE`` kg_nodes row.
  - Writes only the cross-engine edge ``DESIGN -[becomes]-> QUOTE`` by
    label only (kg_edges has no FK constraint to kg_nodes — referencing
    a QUOTE label in an edge is safe; upsert-QUOTE-into-kg_nodes is NOT).
  - After freeze the design loses write authority over the frozen lines
    (Correction #3 — freeze is the hand-off point to Sales).

A2A seam
--------
Sales owns the QUOTE.  This module **proposes** the quote to Sales via
the injectable coroutine
``_propose_quote_to_sales(engine, namespace_id, proposal)``.

The default implementation raises ``NotImplementedError`` (Sales engine
Module 5 is not built yet).  Tests replace it via
``unittest.mock.patch`` on
``nce.vertical_modules.system_design.to_quote._propose_quote_to_sales``.

Design invariants (uncle-bob-craft)
------------------------------------
- SRP per function: each private helper has one job.
- Dependencies point inward: no web/HTTP/admin imports.
- confidence on edges only (wave rule 7 — never on kg_nodes).
- No phantom payload/metadata/state column (kg_nodes has none).
- NEVER write a QUOTE kg_nodes row; edge reference is safe.
- No slow I/O inside scoped_pg_session transactions.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.system_design.graph import (
    _design_label,
    _upsert_edge,
)
from nce.vertical_modules.system_design.sow import _derive_version_number, _read_design_meta

log = logging.getLogger("nce.vertical_modules.system_design.to_quote")

# Edge predicate: DESIGN → QUOTE cross-engine link.
_PRED_BECOMES: str = "becomes"

# Quote label prefix — matches the Sales engine label convention.
_QUOTE_LABEL_PREFIX: str = "QUOTE:"

# Confidence for the design→quote freeze edge.
_BECOMES_CONFIDENCE: float = 0.95


# ---------------------------------------------------------------------------
# A2A seam — injectable for tests
# ---------------------------------------------------------------------------


async def _propose_quote_to_sales(
    engine: Any,
    namespace_id: UUID,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Propose a quote to the Sales engine via A2A tool delegation.

    This is the **loose-coupling seam** between System Design and Sales.
    At runtime the call is resolved via the generic A2A transport (tool name
    ``sales_propose_quote``).  In tests this function is replaced with a
    mock via ``unittest.mock.patch``.

    The proposal dict has the shape::

        {
            "namespace_id": str,          # UUID of the owning namespace
            "design_id": str,             # source design identifier
            "design_label": str,          # DESIGN:<DESIGN_ID>
            "design_version": int,        # frozen version number at hand-off
            "quote_label": str,           # QUOTE:<QUOTE_ID> (Sales will own this)
            "bom_lines": list[dict],      # frozen BOM snapshot
        }

    Returns a dict with at minimum ``{"accepted": bool, "quote_id": str}``.

    The Sales engine is NOT built yet.  Until it ships, this function must be
    mocked in tests.  Do NOT add a direct import of any Sales module here.

    A2A tool name (resolved at runtime): ``sales_propose_quote``
    """
    raise NotImplementedError(
        "_propose_quote_to_sales: Sales engine (Module 5) is not built yet. "
        "Mock this function in integration tests: "
        "patch('nce.vertical_modules.system_design.to_quote._propose_quote_to_sales', ...)"
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _quote_label(design_id: str) -> str:
    """Canonical QUOTE label derived from the design id.

    Format: ``QUOTE:<DESIGN_ID>`` — upper-cased.  Sales will re-key it
    internally; we just need a stable label for the edge.
    """
    return f"{_QUOTE_LABEL_PREFIX}{design_id.upper()}"


async def _read_design_bom_lines(
    conn: Any,
    ns_uuid: UUID,
    design_label: str,
) -> list[dict[str, Any]]:
    """Return all DESIGN_LINE labels contained by the DESIGN node.

    Used to assemble the frozen BOM snapshot sent to Sales.
    """
    rows = await conn.fetch(
        """
        SELECT n.label
        FROM kg_nodes n
        JOIN kg_edges e
             ON e.object_label = n.label
            AND e.namespace_id = n.namespace_id
        WHERE e.subject_label = $1
          AND e.predicate      = 'contains'
          AND n.entity_type    = 'DESIGN_LINE'
          AND n.namespace_id   = $2::uuid
        """,
        design_label,
        str(ns_uuid),
    )
    return [{"label": r["label"]} for r in rows]


# ---------------------------------------------------------------------------
# Public: do_design_to_quote
# ---------------------------------------------------------------------------


async def do_design_to_quote(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Freeze a DESIGN's BOM and propose a quote to Sales via A2A.

    Freezes the design version, writes ``DESIGN -[becomes]-> QUOTE`` (edge
    only — Contract A), and hands the quote proposal to Sales via the A2A
    seam.  After freeze, system_design loses write authority over the frozen
    lines (Correction #3).

    **Contract A (§9.1):** This function NEVER writes or mutates a
    ``QUOTE`` kg_nodes row.  The QUOTE label appears only in
    ``object_label`` of a kg_edge — never in kg_nodes.

    Parameters
    ----------
    engine:
        NCEEngine instance.  Must have a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,  # required
            "design_id": str,            # required — the DESIGN node id
            "source_id": str | None,     # optional — system_design source id
        }``

    Returns
    -------
    dict
        ``{
            "design_id": str,
            "design_label": str,           # DESIGN:<DESIGN_ID>
            "quote_label": str,            # QUOTE:<DESIGN_ID>
            "design_version": int,         # frozen version number
            "becomes_edge": str,           # "DESIGN:... -[becomes]-> QUOTE:..."
            "bom_line_count": int,         # number of frozen BOM lines
            "proposal_sent": bool,         # True when A2A accepted the proposal
        }``

    Raises
    ------
    ValueError
        When ``namespace_id`` or ``design_id`` is missing, or the DESIGN
        node does not exist.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_design_to_quote: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw: str = params.get("design_id", "")
    if not design_id_raw:
        raise ValueError("do_design_to_quote: 'design_id' is required in params")

    source_id: str | None = params.get("source_id")
    design_lbl = _design_label(design_id_raw)
    quote_lbl = _quote_label(design_id_raw)

    # 1. Read design metadata and BOM lines — outside the write transaction
    #    to keep the write transaction short.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        design_meta = await _read_design_meta(conn, ns_uuid, design_lbl)
        if not design_meta:
            raise ValueError(
                f"do_design_to_quote: DESIGN node not found for design_id={design_id_raw!r} "
                f"in namespace={ns_uuid}"
            )
        bom_lines = await _read_design_bom_lines(conn, ns_uuid, design_lbl)

    # 2. Derive the frozen version number from the current design state.
    frozen_version = _derive_version_number(design_lbl, design_meta)

    # 3. Build the quote proposal (assembled outside DB transactions).
    proposal: dict[str, Any] = {
        "namespace_id": str(ns_uuid),
        "design_id": design_id_raw,
        "design_label": design_lbl,
        "design_version": frozen_version,
        "quote_label": quote_lbl,
        "bom_lines": bom_lines,
    }

    # 4. Write the DESIGN -[becomes]-> QUOTE edge inside a scoped session.
    #    This is the freeze hand-off: after this point Sales owns the quote.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        await _upsert_edge(
            conn,
            ns_uuid,
            design_lbl,
            _PRED_BECOMES,
            quote_lbl,
            _BECOMES_CONFIDENCE,
            source_id,
        )

    becomes_desc = f"{design_lbl} -[{_PRED_BECOMES}]-> {quote_lbl}"

    # 5. Propose to Sales via A2A seam — outside any DB transaction.
    proposal_accepted = False
    try:
        sales_response = await _propose_quote_to_sales(engine, ns_uuid, proposal)
        proposal_accepted = bool(sales_response.get("accepted", True))
    except NotImplementedError:
        # Sales engine not built yet — treated as pending proposal.
        log.warning(
            "do_design_to_quote: Sales A2A seam not implemented; "
            "proposal staged but not delivered (design=%s)",
            design_id_raw,
        )

    log.info(
        "do_design_to_quote: ns=%s design=%s version=%d bom_lines=%d edge=%s proposal=%s",
        ns_uuid,
        design_id_raw,
        frozen_version,
        len(bom_lines),
        becomes_desc,
        proposal_accepted,
    )

    return {
        "design_id": design_id_raw,
        "design_label": design_lbl,
        "quote_label": quote_lbl,
        "design_version": frozen_version,
        "becomes_edge": becomes_desc,
        "bom_line_count": len(bom_lines),
        "proposal_sent": proposal_accepted,
    }
