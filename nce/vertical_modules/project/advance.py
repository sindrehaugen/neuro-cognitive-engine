"""
nce/vertical_modules/project/advance.py
=========================================
Domain core: ``do_advance_phase`` — the phase-transition Actor.

Reads the project's current phase from the ``PROJECT -[in_phase]-> GATE``
edge in the knowledge graph, validates the requested transition via the
pure ``can_enter_phase`` gate, and — when the gate passes — atomically
upserts the new ``PROJECT_GATE`` node, moves the ``in_phase`` edge to
it, and appends a ``project_phase_advanced`` row to the WORM ``event_log``.

Design invariants (uncle-bob-craft / Dependency Rule)
------------------------------------------------------
- Domain core: NO web/HTTP/admin imports at module level.
- SRP per function: one responsibility per function (single level of
  abstraction).  The orchestrator ``do_advance_phase`` composes them.
- Dependencies point inward: ``asyncpg``, ``nce.db_utils``,
  ``nce.entity_resolution.ownership``, ``nce.events.emit``,
  ``nce.event_log``, and the pure sibling ``phase_gates`` — nothing else.
- ``confidence`` lives on ``kg_edges`` only, never on ``kg_nodes``.
- ``kg_nodes`` has NO ``metadata`` column.
- WORM invariant: ``event_log`` rows are INSERT-only; this module has
  no UPDATE/DELETE code path.

Caller-supplied ``criteria_met`` contract
------------------------------------------
``criteria_met`` is a list of criterion keys the caller asserts are
currently satisfied.  Auto-resolution of cross-engine criteria (Sales
baseline, Procurement PO, HR PL assignment) is **DEFERRED** — those
engines are not yet built.  Do NOT fabricate criteria or call unbuilt
engines from here.

Idempotency
-----------
Advancing to the phase that is already current returns
``{"ok": True, "phase": current, "noop": True}`` with no writes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.event_log import append_event
from nce.events.emit import emit_graph_write
from nce.vertical_modules.project.phase_gates import can_enter_phase

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.advance")

# ---------------------------------------------------------------------------
# Engine / node-type constants
# ---------------------------------------------------------------------------

_PROJECT_ENGINE: str = "project"
_NODE_TYPE_GATE: str = "PROJECT_GATE"
_PREDICATE_IN_PHASE: str = "in_phase"
_IN_PHASE_CONFIDENCE: float = 1.0

# WORM event type — declared in nce/event_types.py (PROJECT_EVENTS group).
_EVENT_TYPE_PHASE_ADVANCED: str = "project_phase_advanced"
_AGENT_ID: str = "project-advance-phase"


# ---------------------------------------------------------------------------
# Label helpers — deterministic; must stay consistent with convert.py
# ---------------------------------------------------------------------------


def _gate_label(quote_id: str, gate: str) -> str:
    """Canonical label for a ``PROJECT_GATE`` node at *gate* (e.g. ``G1``)."""
    return f"GATE:{quote_id.upper()}:{gate}"


def _quote_id_from_project_label(project_label: str) -> str:
    """Extract the quote_id fragment from a ``PROJECT:{QUOTE}`` label.

    Raises ``ValueError`` when the label does not match the expected format.
    """
    prefix = "PROJECT:"
    if not project_label.upper().startswith(prefix):
        raise ValueError(f"project_label {project_label!r} does not start with 'PROJECT:'")
    return project_label[len(prefix) :]


# ---------------------------------------------------------------------------
# Read helper: fetch the current phase from the graph
# ---------------------------------------------------------------------------


async def _read_current_phase(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_label: str,
) -> str | None:
    """Return the current gate name (e.g. ``"G0"``) or ``None`` if absent.

    Reads the single ``PROJECT -[in_phase]-> GATE:{QUOTE}:{Gn}`` edge and
    parses the gate name from the ``object_label``.
    """
    row = await conn.fetchrow(
        """
        SELECT object_label
        FROM   kg_edges
        WHERE  subject_label = $1
          AND  predicate      = $2
          AND  namespace_id   = $3::uuid
        LIMIT 1
        """,
        project_label,
        _PREDICATE_IN_PHASE,
        str(ns_uuid),
    )
    if row is None:
        return None
    return _parse_gate_from_label(row["object_label"])


def _parse_gate_from_label(gate_label: str) -> str:
    """Extract the ``Gn`` part from a ``GATE:{QUOTE}:{Gn}`` label.

    Examples
    --------
    >>> _parse_gate_from_label("GATE:Q123:G1")
    'G1'
    """
    parts = gate_label.split(":")
    if len(parts) < 3:
        raise ValueError(
            f"Cannot parse gate from label {gate_label!r}: expected format GATE:QUOTE:Gn"
        )
    return parts[-1]


# ---------------------------------------------------------------------------
# Write helpers: guarded GATE upsert + in_phase edge move
# ---------------------------------------------------------------------------


async def _upsert_gate_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    gate_label: str,
) -> None:
    """Upsert a ``PROJECT_GATE`` node at *gate_label* (assert_owner-guarded)."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_GATE, _PROJECT_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        gate_label,
        _NODE_TYPE_GATE,
        str(ns_uuid),
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=_NODE_TYPE_GATE,
        op="upserted",
        node_id=gate_label,
    )


async def _move_in_phase_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_label: str,
    new_gate_label: str,
) -> None:
    """Move the ``PROJECT -[in_phase]-> GATE`` edge to the new gate.

    The old ``GATE`` node stays in the graph as history.  We upsert the
    new edge with ``ON CONFLICT … DO UPDATE`` — if the edge already points
    to the target (idempotency race) this is a harmless no-op.

    Because the unique constraint is ``(subject_label, predicate,
    object_label, namespace_id)``, a new target gate creates a NEW edge row;
    we also delete the old in_phase edge so only one active pointer exists.
    """
    # Remove every existing in_phase edge from this project (there should be
    # exactly one, but the delete is safe even if multiple exist somehow).
    await conn.execute(
        """
        DELETE FROM kg_edges
        WHERE  subject_label = $1
          AND  predicate      = $2
          AND  namespace_id   = $3::uuid
        """,
        project_label,
        _PREDICATE_IN_PHASE,
        str(ns_uuid),
    )
    # Insert the new in_phase edge pointing to the new gate.
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
        new_gate_label,
        _IN_PHASE_CONFIDENCE,
        str(ns_uuid),
    )


# ---------------------------------------------------------------------------
# Event log append
# ---------------------------------------------------------------------------


async def _append_phase_transition_event(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_label: str,
    from_phase: str,
    to_phase: str,
    actor: str,
) -> None:
    """Append one ``project_phase_advanced`` row to the WORM ``event_log``.

    Must be called inside an active transaction (WORM / ``append_event``
    contract).  Never called for no-op (idempotent) transitions.
    """
    await append_event(
        conn=conn,
        namespace_id=ns_uuid,
        agent_id=_AGENT_ID,
        event_type=_EVENT_TYPE_PHASE_ADVANCED,
        params={
            "project_id": project_label,
            "from_phase": from_phase,
            "to_phase": to_phase,
            "actor": actor,
        },
    )


# ---------------------------------------------------------------------------
# Public read helper: current phase (for REST GET route)
# ---------------------------------------------------------------------------


async def read_current_phase(
    engine: NCEEngine,
    namespace_id: str | UUID,
    project_id: str,
) -> str | None:
    """Return the current gate name (e.g. ``"G0"``) or ``None`` if absent.

    Public facade over the private ``_read_current_phase`` so the admin
    handler can stay a thin wrapper without importing the ``_``-name.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    namespace_id:
        Namespace UUID (str or UUID).
    project_id:
        Project label, e.g. ``"PROJECT:Q123"``.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        return await _read_current_phase(conn, ns_uuid, project_id)


# ---------------------------------------------------------------------------
# Public domain core
# ---------------------------------------------------------------------------


async def do_advance_phase(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Advance the project to ``target_phase`` if the gate criteria are met.

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool`` for DB access).
    params:
        ``{
            "namespace_id":  str | UUID,       # required
            "project_id":    str,              # required — e.g. "PROJECT:Q123"
            "target_phase":  str,              # required — e.g. "G1"
            "actor":         str,              # required — who is requesting
            "criteria_met":  list[str],        # optional, default []
        }``

    Returns
    -------
    dict
        One of:

        - ``{"ok": True, "phase": <new_phase>}``
          — transition succeeded; graph + event_log updated.
        - ``{"ok": True, "phase": <current>, "noop": True}``
          — already in the requested phase; no writes.
        - ``{"ok": False, "missing_criteria": [...], "current_phase": <g>}``
          — gate refused; criteria unmet; no writes.
        - ``{"ok": False, "error": "..."}``
          — project/edge absent or bad params; no writes; never raises.

    Notes
    -----
    Never raises into the MCP dispatch layer.  All error paths return a
    structured ``{"ok": False, ...}`` dict.
    """
    # --- Validate required params (return error, never raise to caller) -----
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_advance_phase: 'namespace_id' is required"}

    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_advance_phase: invalid namespace_id: {exc}"}

    project_label: str = str(params.get("project_id") or "").strip()
    if not project_label:
        return {"ok": False, "error": "do_advance_phase: 'project_id' is required"}

    target_phase: str = str(params.get("target_phase") or "").strip()
    if not target_phase:
        return {"ok": False, "error": "do_advance_phase: 'target_phase' is required"}

    actor: str = str(params.get("actor") or "").strip()
    if not actor:
        return {"ok": False, "error": "do_advance_phase: 'actor' is required"}

    criteria_met: list[str] = list(params.get("criteria_met") or [])

    # Extract quote_id from the project label for gate-label construction.
    try:
        quote_id = _quote_id_from_project_label(project_label)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # --- Step 1: Read current phase from the graph (namespace-scoped) -------
    current_phase: str | None = None
    try:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            current_phase = await _read_current_phase(conn, ns_uuid, project_label)
    except Exception as exc:
        log.warning(
            "do_advance_phase: failed to read current phase ns=%s project=%s: %s",
            ns_uuid,
            project_label,
            exc,
            exc_info=True,
        )
        return {"ok": False, "error": f"do_advance_phase: failed to read current phase: {exc}"}

    if current_phase is None:
        return {
            "ok": False,
            "error": (
                f"do_advance_phase: project {project_label!r} has no 'in_phase' edge "
                f"in namespace {ns_uuid} — it may not have been converted yet"
            ),
        }

    # --- Step 2: Idempotency guard ------------------------------------------
    if target_phase == current_phase:
        return {"ok": True, "phase": current_phase, "noop": True}

    # --- Step 3: Pure gate check (no DB, no side-effects) -------------------
    gate_result = can_enter_phase(
        {"current_phase": current_phase, "criteria_met": criteria_met},
        target_phase,
    )
    if not gate_result["ok"]:
        return {
            "ok": False,
            "missing_criteria": gate_result["missing_criteria"],
            "current_phase": current_phase,
        }

    # --- Step 4: Write new gate + move edge + append event_log ---------------
    new_gate_label = _gate_label(quote_id, target_phase)

    try:
        async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
            async with conn.transaction():
                await _upsert_gate_node(conn, ns_uuid, new_gate_label)
                await _move_in_phase_edge(conn, ns_uuid, project_label, new_gate_label)
                await _append_phase_transition_event(
                    conn,
                    ns_uuid,
                    project_label,
                    from_phase=current_phase,
                    to_phase=target_phase,
                    actor=actor,
                )
    except Exception as exc:
        log.error(
            "do_advance_phase: write failed ns=%s project=%s %s→%s: %s",
            ns_uuid,
            project_label,
            current_phase,
            target_phase,
            exc,
            exc_info=True,
        )
        return {"ok": False, "error": f"do_advance_phase: write failed: {exc}"}

    log.info(
        "do_advance_phase: ns=%s project=%s %s→%s actor=%s",
        ns_uuid,
        project_label,
        current_phase,
        target_phase,
        actor,
    )
    return {"ok": True, "phase": target_phase}
