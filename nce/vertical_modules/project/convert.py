"""
nce/vertical_modules/project/convert.py
=========================================
Sales→Project bridge: ``do_convert_signed_quote``.

CRITICAL invariant (roadmap §9.1, Contract A)
----------------------------------------------
``SIGNED_BASELINE`` is owned and frozen **ONCE by Sales** in
``sales_signed_baselines``.  This module:

  - READS the Sales-frozen row via the injectable A2A seam imported from
    ``nce.vertical_modules.project.baseline._read_signed_baseline``.
  - Creates ``PROJECT_PROJECT``, ``PROJECT_GATE`` (at G0), and initial
    ``PROJECT_TASK`` nodes, plus ``PROJECT -[contains]-> BOM_LINE`` edges
    onto the quote's existing ``BOM_LINE`` nodes (never re-creating them).
  - References the signed baseline **by id only** — NEVER writes a
    ``SIGNED_BASELINE`` node and NEVER creates a ``project_signed_baselines``
    table or object.

Degraded conversions
--------------------
A conversion can succeed structurally and still be incomplete — most
importantly, when the quote has no ``BOM_LINE`` nodes in the graph the
project is created with an EMPTY bill of materials.  Nothing in NCE creates
``BOM_LINE`` nodes today, so that case is the norm, not an edge case.  The
result therefore carries an explicit ``degraded`` / ``degraded_reasons`` /
``degraded_detail`` signal so a caller can distinguish "the quote genuinely
had no lines" from "the line data does not exist in NCE" instead of reading
HTTP 200 as full success.

Idempotency
-----------
``do_convert_signed_quote`` is idempotent on ``quote_id``:  a re-run
returns the same ``project_id`` and creates no duplicate nodes or edges.
Idempotency is enforced by deriving the PROJECT label deterministically
from ``quote_id`` and using ``ON CONFLICT … DO UPDATE`` / ``DO NOTHING``
at the SQL level.

Design invariants (uncle-bob-craft)
-------------------------------------
- SRP per function: one job each — ``_project_label``, ``_gate_label``,
  ``_task_label`` build labels; ``_upsert_project_node`` / ``_upsert_gate_node``
  / ``_upsert_task_node`` write nodes; ``_upsert_contains_edges`` writes edges;
  ``do_convert_signed_quote`` orchestrates.
- Dependencies point inward: no web/HTTP/admin imports at module level.
- ``confidence`` only on kg_edges (rule 7); kg_nodes has no confidence col.
- Ownership-guarded via ``assert_owner`` before every owned-node write.
- ``emit_graph_write`` inside the SAME transaction as the INSERT.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write
from nce.vertical_modules.project.baseline import _read_signed_baseline

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.convert")

# ---------------------------------------------------------------------------
# Engine identifier — must match node-ownership.json and assert_owner calls
# ---------------------------------------------------------------------------
_PROJECT_ENGINE: str = "project"

_NODE_TYPE_PROJECT: str = "PROJECT_PROJECT"
_NODE_TYPE_GATE: str = "PROJECT_GATE"
_NODE_TYPE_TASK: str = "PROJECT_TASK"

# Edges the project engine writes.
_PREDICATE_CONTAINS: str = "contains"
_PREDICATE_IN_PHASE: str = "in_phase"

# Gate names (G0 = inception gate, opened at conversion).
_GATE_G0: str = "G0"

# Default confidence score for the contains edges (structural, not predictive).
_CONTAINS_CONFIDENCE: float = 1.0
_IN_PHASE_CONFIDENCE: float = 1.0

# ---------------------------------------------------------------------------
# Degradation reason codes — stable, machine-readable, safe to switch on.
#
# A conversion can succeed structurally (PROJECT/GATE/TASK written, HTTP 200)
# while still being INCOMPLETE.  Callers previously had no way to tell that
# apart from a fully-populated conversion, because the only signal was the
# absence of an exception.  These codes make the incompleteness explicit.
# ---------------------------------------------------------------------------
_DEGRADED_NO_BOM_LINES: str = "no_bom_lines_in_graph"
_DEGRADED_NO_SALES_BASELINE: str = "sales_baseline_unavailable"

_DEGRADED_DETAIL: dict[str, str] = {
    _DEGRADED_NO_BOM_LINES: (
        "No BOM_LINE nodes exist in NCE for this quote, so the project was created "
        "with an empty bill of materials. NCE has no path that creates BOM_LINE "
        "nodes today, so a zero count does NOT confirm the quote itself had no "
        "lines — treat it as missing line data, not as an empty quote."
    ),
    _DEGRADED_NO_SALES_BASELINE: (
        "The Sales-frozen signed baseline could not be read, so the project is not "
        "linked to a signed_baseline_id."
    ),
}


# ---------------------------------------------------------------------------
# Label helpers — deterministic, so idempotency holds across re-runs
# ---------------------------------------------------------------------------


def _project_label(quote_id: str) -> str:
    """Canonical kg_nodes label for the PROJECT_PROJECT node.

    Derived from the quote_id so the same quote always maps to the same
    project node — the idempotency anchor.
    """
    return f"PROJECT:{quote_id.upper()}"


def _gate_label(quote_id: str, gate: str) -> str:
    """Canonical label for a PROJECT_GATE node at *gate* (e.g. G0)."""
    return f"GATE:{quote_id.upper()}:{gate}"


def _task_label(quote_id: str, sequence: int) -> str:
    """Canonical label for an initial PROJECT_TASK node."""
    return f"TASK:{quote_id.upper()}:INIT:{sequence:03d}"


def _bom_line_label(quote_id: str, line_ref: str) -> str:
    """Reconstruct the BOM_LINE label for an existing node.

    These nodes are owned by System Design / Sales — this engine never
    re-creates them.  Only the label is needed to write the contains edge.
    """
    return f"BOM_LINE:{quote_id.upper()}:{line_ref.upper()}"


# ---------------------------------------------------------------------------
# Private: single-node upserts (one function, one responsibility)
# ---------------------------------------------------------------------------


async def _upsert_project_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
    *,
    signed_by: str,
    signature_ref: str,
    signed_baseline_id: str | None,
) -> str:
    """Upsert the PROJECT_PROJECT node; return its label."""
    label = _project_label(quote_id)
    await assert_owner(conn, ns_uuid, _NODE_TYPE_PROJECT, _PROJECT_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        label,
        _NODE_TYPE_PROJECT,
        str(ns_uuid),
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_PROJECT,
        op="upserted",
        node_id=label,
    )
    return label


async def _upsert_gate_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
    gate: str,
) -> str:
    """Upsert a PROJECT_GATE node at *gate*; return its label."""
    label = _gate_label(quote_id, gate)
    await assert_owner(conn, ns_uuid, _NODE_TYPE_GATE, _PROJECT_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        label,
        _NODE_TYPE_GATE,
        str(ns_uuid),
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_GATE,
        op="upserted",
        node_id=label,
    )
    return label


async def _upsert_task_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
    sequence: int,
) -> str:
    """Upsert an initial PROJECT_TASK node; return its label."""
    label = _task_label(quote_id, sequence)
    await assert_owner(conn, ns_uuid, _NODE_TYPE_TASK, _PROJECT_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        label,
        _NODE_TYPE_TASK,
        str(ns_uuid),
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_TASK,
        op="upserted",
        node_id=label,
    )
    return label


# ---------------------------------------------------------------------------
# Private: edge upserts
# ---------------------------------------------------------------------------


async def _upsert_in_phase_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_label: str,
    gate_label: str,
) -> None:
    """Upsert PROJECT -[in_phase]-> GATE edge."""
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id)
        VALUES ($1, $2, $3, $4, $5::uuid)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                updated_at = NOW()
        """,
        project_label,
        _PREDICATE_IN_PHASE,
        gate_label,
        _IN_PHASE_CONFIDENCE,
        str(ns_uuid),
    )


async def _upsert_contains_edges(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_label: str,
    bom_line_labels: list[str],
) -> None:
    """Upsert PROJECT -[contains]-> BOM_LINE edges for each existing BOM_LINE.

    ``kg_edges`` has no FK to ``kg_nodes`` — the BOM_LINE nodes are owned
    by other engines (System Design / Sales); we reference them by label
    only.  Do NOT call ``assert_owner`` for BOM_LINE — only owned
    PROJECT_* nodes require that guard.
    """
    for bom_label in bom_line_labels:
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence, namespace_id)
            VALUES ($1, $2, $3, $4, $5::uuid)
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
                SET confidence = EXCLUDED.confidence,
                    updated_at = NOW()
            """,
            project_label,
            _PREDICATE_CONTAINS,
            bom_label,
            _CONTAINS_CONFIDENCE,
            str(ns_uuid),
        )


# ---------------------------------------------------------------------------
# Private: BOM_LINE label discovery
# ---------------------------------------------------------------------------


async def _fetch_bom_line_labels(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    quote_id: str,
) -> list[str]:
    """Return all existing BOM_LINE labels for *quote_id* in *ns_uuid*.

    Queries ``kg_nodes`` for rows whose entity_type = 'BOM_LINE' and whose
    label starts with the ``BOM_LINE:{QUOTE}:`` prefix.  This engine never
    re-creates these nodes — it edges onto them.

    Matched via a literal ``starts_with()`` prefix test, never SQL ``LIKE``:
    ``quote_id`` is caller-supplied, and ``_``/``%`` are ordinary LIKE
    metacharacters (``_`` matches any single character, ``%`` matches any
    sequence). Against a raw ``LIKE`` pattern, a quote id containing either
    would silently widen the match to a DIFFERENT quote's BOM lines, so the
    WRONG quote's lines would get edged onto this project (confirmed live:
    ``'BOM_LINE:QA1:AMP01' LIKE 'BOM_LINE:Q_1:%'`` is true). ``starts_with()``
    is a plain literal-prefix test with no pattern semantics at all, so no
    ``quote_id`` can ever be crafted to widen the match. Mirrors
    ``economy/cascade.py``'s ``_read_actual_cost_total`` (Batch 120).
    """
    rows = await conn.fetch(
        """
        SELECT label FROM kg_nodes
        WHERE entity_type  = 'BOM_LINE'
          AND namespace_id = $1::uuid
          AND starts_with(label, $2)
        ORDER BY label
        """,
        str(ns_uuid),
        f"BOM_LINE:{quote_id.upper()}:",
    )
    return [r["label"] for r in rows]


# ---------------------------------------------------------------------------
# Private: degradation signal (pure — no DB, no I/O)
# ---------------------------------------------------------------------------


def _degradation_reasons(*, bom_line_count: int, sales_available: bool) -> list[str]:
    """Return a stable, ordered list of reason codes for an incomplete conversion.

    Empty list == the conversion is fully populated.  Pure function: the
    caller supplies the two facts, this decides what to report.

    ``bom_line_count == 0`` is reported as a degradation rather than as a
    legitimately empty bill of materials on purpose.  Nothing in NCE creates
    ``BOM_LINE`` nodes today, so zero lines cannot be read as "the quote had
    no lines" — it means the line data is not in NCE.  Reporting it as a
    normal success would be reporting a project with no bill of materials as
    complete.
    """
    reasons: list[str] = []
    if bom_line_count == 0:
        reasons.append(_DEGRADED_NO_BOM_LINES)
    if not sales_available:
        reasons.append(_DEGRADED_NO_SALES_BASELINE)
    return reasons


def _degradation_detail(reasons: list[str]) -> str | None:
    """Human-readable explanation for *reasons*; ``None`` when not degraded."""
    if not reasons:
        return None
    return " ".join(_DEGRADED_DETAIL[code] for code in reasons if code in _DEGRADED_DETAIL)


# ---------------------------------------------------------------------------
# Public: do_convert_signed_quote
# ---------------------------------------------------------------------------


async def do_convert_signed_quote(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Convert a signed quote to a Project — the Sales→Project bridge.

    Reads the Sales-frozen ``sales_signed_baselines`` row via the A2A seam
    ``_read_signed_baseline``.  Then, inside a single transaction, materialises:

    * ``PROJECT_PROJECT`` node (derived from ``quote_id`` — idempotent).
    * ``PROJECT_GATE`` at G0 (opened at conversion).
    * One initial ``PROJECT_TASK`` node (INIT:000 — seed for auto-tasking).
    * ``PROJECT -[in_phase]-> GATE@G0`` edge.
    * ``PROJECT -[contains]-> BOM_LINE`` edges onto **existing** BOM_LINE nodes
      (never re-creates them; writes the edge by label only).

    **Invariants enforced:**
    - Never writes a ``SIGNED_BASELINE`` node.
    - Never creates a ``project_signed_baselines`` table or object.
    - If the Sales baseline is unavailable, degrades gracefully (returns
      ``{"sales_available": False, ...}``) rather than fabricating data.
    - Degradation is always reported EXPLICITLY via ``degraded`` /
      ``degraded_reasons`` — an incomplete conversion never returns a
      success-shaped payload with no signal.
    - Idempotent on ``quote_id``: a re-run returns the same ``project_id``
      and creates no duplicate nodes or edges (ON CONFLICT DO UPDATE/NOTHING).

    Parameters
    ----------
    engine:
        NCEEngine instance (provides ``pg_pool`` for DB access).
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "quote_id": str,              # required — Sales QUOTE identifier
            "signed_by": str,             # required — actor who signed
            "signature_ref": str,         # required — signature reference
        }``

    Returns
    -------
    dict
        ``{
            "project_id": str,            # label of the PROJECT_PROJECT node
            "gate": str,                  # "G0"
            "bom_lines_linked": int,      # number of contains edges written
            "degraded": bool,             # True when the conversion is incomplete
            "degraded_reasons": list[str],# stable codes; empty when not degraded
            "degraded_detail": str | None,# human-readable explanation, or None
            "baseline": {                 # from Sales-frozen baseline
                "signed_baseline_id": str | None,
                "sales_available": bool,
            },
        }``

        A structurally successful conversion can still be incomplete.  When
        ``degraded`` is True the project WAS created, but part of the data it
        should reference is missing — see ``degraded_reasons``:

        - ``no_bom_lines_in_graph`` — zero ``BOM_LINE`` nodes were found, so
          the project has an empty bill of materials.  Nothing in NCE creates
          ``BOM_LINE`` nodes today, so this means the line data is absent from
          NCE; it does NOT mean the quote had no lines.
        - ``sales_baseline_unavailable`` — the Sales-frozen baseline could not
          be read (mirrors ``baseline.sales_available == False``).

        Callers must not treat a 200 / no-exception result as proof the
        project is fully populated — check ``degraded``.

    Raises
    ------
    ValueError
        When required params are missing.
    """
    # --- Validate required params ---
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("do_convert_signed_quote: 'namespace_id' is required")
    ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns

    quote_id: str = str(params.get("quote_id") or "").strip()
    if not quote_id:
        raise ValueError("do_convert_signed_quote: 'quote_id' is required")

    signed_by: str = str(params.get("signed_by") or "").strip()
    signature_ref: str = str(params.get("signature_ref") or "").strip()

    # --- Step 1: Read Sales-frozen baseline via A2A seam (degrade if absent) ---
    signed_baseline_id: str | None = None
    sales_available: bool = False
    try:
        signed_row = await _read_signed_baseline(engine, ns_uuid, quote_id)
        if signed_row is not None:
            signed_baseline_id = signed_row.get("id")
            sales_available = True
        else:
            log.info(
                "do_convert_signed_quote: no Sales baseline for quote=%s ns=%s — degraded",
                quote_id,
                ns_uuid,
            )
    except NotImplementedError:
        # Sales engine not yet built — degrade gracefully, never block or fabricate.
        log.info(
            "do_convert_signed_quote: Sales engine not available (NotImplementedError) "
            "for quote=%s ns=%s — degraded",
            quote_id,
            ns_uuid,
        )
    except Exception:
        log.warning(
            "do_convert_signed_quote: A2A read failed for quote=%s ns=%s — degraded",
            quote_id,
            ns_uuid,
            exc_info=True,
        )

    # --- Steps 2–4: Graph writes inside a single scoped transaction ---
    project_label: str = ""
    bom_line_labels: list[str] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        async with conn.transaction():
            # 2a. Discover existing BOM_LINE labels before writing project nodes.
            bom_line_labels = await _fetch_bom_line_labels(conn, ns_uuid, quote_id)

            # 2b. Upsert PROJECT_PROJECT node (idempotency anchor on quote_id).
            project_label = await _upsert_project_node(
                conn,
                ns_uuid,
                quote_id,
                signed_by=signed_by,
                signature_ref=signature_ref,
                signed_baseline_id=signed_baseline_id,
            )

            # 2c. Upsert PROJECT_GATE at G0 (opened at conversion).
            gate_label = await _upsert_gate_node(conn, ns_uuid, quote_id, _GATE_G0)

            # 2d. Upsert one initial PROJECT_TASK (seed for auto-tasking in P3).
            await _upsert_task_node(conn, ns_uuid, quote_id, sequence=0)

            # 2e. PROJECT -[in_phase]-> GATE@G0.
            await _upsert_in_phase_edge(conn, ns_uuid, project_label, gate_label)

            # 2f. PROJECT -[contains]-> BOM_LINE for each existing BOM_LINE.
            await _upsert_contains_edges(conn, ns_uuid, project_label, bom_line_labels)

    degraded_reasons = _degradation_reasons(
        bom_line_count=len(bom_line_labels),
        sales_available=sales_available,
    )

    log_at = log.warning if degraded_reasons else log.info
    log_at(
        "do_convert_signed_quote: ns=%s quote=%s project=%s gate=%s "
        "bom_lines=%d sales_available=%s degraded=%s reasons=%s",
        ns_uuid,
        quote_id,
        project_label,
        _GATE_G0,
        len(bom_line_labels),
        sales_available,
        bool(degraded_reasons),
        degraded_reasons,
    )

    return {
        "project_id": project_label,
        "gate": _GATE_G0,
        "bom_lines_linked": len(bom_line_labels),
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "degraded_detail": _degradation_detail(degraded_reasons),
        "baseline": {
            "signed_baseline_id": signed_baseline_id,
            "sales_available": sales_available,
        },
    }
