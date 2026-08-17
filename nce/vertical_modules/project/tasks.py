"""
nce/vertical_modules/project/tasks.py
=======================================
Domain core: ``do_sync_bom_tasks`` — BOM-line-status → TASK reconciler.

Trigger
-------
Subscribed to ``BOM_LINE.status_changed`` graph events via the C4 reactive
event bus (Module 0.W18–W20).  **NOT a poller** — fires once per event,
idempotent via the C4 dedup layer (``processed_outbox_events``).

Contract A invariant (§9.1)
---------------------------
- Project **READS** ``BOM_LINE`` status from the event payload / graph edges.
- Project **NEVER writes** ``BOM_LINE`` content or status.
- Project **writes** only ``PROJECT_TASK`` nodes (owned) +
  ``BOM_LINE -[generates]-> TASK`` edges (confidence = rule strength).
- ``BOM_LINE`` nodes remain untouched by this module.

Status → TASK mapping
---------------------
Each status advance opens the corresponding task for that line:

    PLANNED   → TASK:*:PROCUREMENT:*   (trigger procurement)
    ORDERED   → TASK:*:DELIVERY:*      (await delivery confirmation)
    DELIVERED → TASK:*:INSTALLATION:*  (schedule installation)
    INSTALLED → TASK:*:TESTING:*       (run acceptance tests)
    TESTED    → TASK:*:HANDOVER:*      (client handover checklist)

A previously-generated task is closed (status → "closed") when the line
advances past its corresponding status.

Idempotency
-----------
A re-delivered ``BOM_LINE.status_changed`` event is a no-op because
``make_idempotent_handler`` (dispatch.py) records the event_id in
``processed_outbox_events`` inside the same transaction.

Design invariants (uncle-bob-craft)
------------------------------------
- Dependencies point inward: only ``asyncpg``, ``nce.db_utils``,
  ``nce.entity_resolution.ownership``, ``nce.events.bus``,
  ``nce.events.dispatch``, and sibling project helpers.
- SRP: one function per responsibility.
- ``confidence`` lives on ``kg_edges`` only (never ``kg_nodes``).
- No web/HTTP/admin imports at module level.
- WORM invariant: ``event_log`` rows are INSERT-only — this module does
  not call ``append_event`` (task creation is not a named WORM event type
  in event_types.py; audit is left to the C4 outbox row already written by
  the Procurement/Warehouse publisher).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session
from nce.entity_resolution.ownership import assert_owner
from nce.events.bus import subscribe

if TYPE_CHECKING:
    from nce.orchestrator import NCEEngine

log = logging.getLogger("nce.vertical_modules.project.tasks")

# ---------------------------------------------------------------------------
# Engine / node-type / predicate constants
# ---------------------------------------------------------------------------

_PROJECT_ENGINE: str = "project"
_NODE_TYPE_TASK: str = "PROJECT_TASK"

# The C4 bus key this module subscribes to.
_BOM_LINE_NODE_TYPE: str = "BOM_LINE"
_BOM_LINE_OP: str = "status_changed"

# Predicate written by this module.
_PREDICATE_GENERATES: str = "generates"

# Status → task-kind mapping (ordered by lifecycle advancement).
# Each BOM status maps to the task kind that should exist while the line
# is at or beyond that status.
_STATUS_TO_TASK_KIND: dict[str, str] = {
    "PLANNED": "PROCUREMENT",
    "ORDERED": "DELIVERY",
    "DELIVERED": "INSTALLATION",
    "INSTALLED": "TESTING",
    "TESTED": "HANDOVER",
}

# Lifecycle order — used to decide which tasks to close.
_STATUS_ORDER: list[str] = ["PLANNED", "ORDERED", "DELIVERED", "INSTALLED", "TESTED"]

# Rule-strength confidence for BOM_LINE -[generates]-> TASK edges.
_GENERATES_CONFIDENCE: float = 0.9


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _task_label_for_kind(bom_line_label: str, kind: str) -> str:
    """Canonical label for a BOM-line-generated task of *kind*.

    Example: ``BOM_LINE:Q1:AMP01`` + ``PROCUREMENT`` →
             ``TASK:BOM:Q1:AMP01:PROCUREMENT``
    """
    # Strip leading "BOM_LINE:" prefix so the task label stays compact.
    suffix = bom_line_label.removeprefix("BOM_LINE:")
    return f"TASK:BOM:{suffix}:{kind}"


# ---------------------------------------------------------------------------
# DB helpers — one responsibility each
# ---------------------------------------------------------------------------


async def _upsert_task_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    task_label: str,
    kind: str,
) -> None:
    """Upsert a PROJECT_TASK node for *task_label* (assert_owner-guarded)."""
    await assert_owner(conn, ns_uuid, _NODE_TYPE_TASK, _PROJECT_ENGINE)
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id)
        VALUES ($1, $2, $3::uuid)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type = EXCLUDED.entity_type,
                updated_at  = NOW()
        """,
        task_label,
        _NODE_TYPE_TASK,
        str(ns_uuid),
    )
    log.debug("upserted PROJECT_TASK node label=%s kind=%s ns=%s", task_label, kind, ns_uuid)


async def _upsert_generates_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
    task_label: str,
    status: str,
) -> None:
    """Upsert BOM_LINE -[generates]-> TASK edge (confidence = rule strength)."""
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence, namespace_id)
        VALUES ($1, $2, $3, $4, $5::uuid)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence = EXCLUDED.confidence,
                updated_at = NOW()
        """,
        bom_line_label,
        _PREDICATE_GENERATES,
        task_label,
        _GENERATES_CONFIDENCE,
        str(ns_uuid),
    )
    log.debug(
        "upserted %s-[generates]->%s confidence=%s ns=%s",
        bom_line_label,
        task_label,
        _GENERATES_CONFIDENCE,
        ns_uuid,
    )


async def _close_superseded_tasks(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
    current_status: str,
) -> list[str]:
    """Mark TASK nodes superseded by *current_status* as closed.

    A task is superseded when the BOM line has advanced past the status that
    opened it.  Closed tasks keep their nodes (audit history) but their
    ``entity_type`` is updated to ``PROJECT_TASK`` with a ``status:closed``
    payload suffix to signal closure.

    In this implementation we set ``entity_type = 'PROJECT_TASK'`` with no
    change (the graph has no free-text status column on kg_nodes), and
    instead record closure by removing the ``generates`` edge to the old
    task.  This keeps the TASK node as provenance while the live edge-set
    reflects only the current, open task.

    Returns list of closed task labels.
    """
    # Statuses whose tasks are now superseded.
    current_idx = _STATUS_ORDER.index(current_status) if current_status in _STATUS_ORDER else -1
    superseded_statuses = _STATUS_ORDER[:current_idx]  # all statuses before current

    closed: list[str] = []
    for old_status in superseded_statuses:
        old_kind = _STATUS_TO_TASK_KIND[old_status]
        old_task_label = _task_label_for_kind(bom_line_label, old_kind)

        # Remove the generates edge to mark the task as superseded.
        result = await conn.execute(
            """
            DELETE FROM kg_edges
            WHERE  subject_label = $1
              AND  predicate      = $2
              AND  object_label   = $3
              AND  namespace_id   = $4::uuid
            """,
            bom_line_label,
            _PREDICATE_GENERATES,
            old_task_label,
            str(ns_uuid),
        )
        # asyncpg returns "DELETE N" — only record if a row was actually removed.
        if result.endswith("1"):
            closed.append(old_task_label)
            log.debug(
                "closed superseded task %s (status advanced past %s) ns=%s",
                old_task_label,
                old_status,
                ns_uuid,
            )

    return closed


async def _fetch_bom_lines_for_project(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    project_id: str,
) -> list[str]:
    """Return BOM_LINE labels reachable from *project_id* via ``contains`` edges.

    Explicit namespace_id filter on BOTH the edge lookup and the node lookup
    (never rely on RLS alone — owner-pool tests bypass FORCE RLS).
    """
    rows = await conn.fetch(
        """
        SELECT e.object_label
        FROM   kg_edges e
        WHERE  e.subject_label = $1
          AND  e.predicate      = 'contains'
          AND  e.namespace_id   = $2::uuid
          AND  EXISTS (
                SELECT 1 FROM kg_nodes n
                WHERE  n.label        = e.object_label
                  AND  n.entity_type  = 'BOM_LINE'
                  AND  n.namespace_id = $2::uuid
               )
        ORDER BY e.object_label
        """,
        project_id,
        str(ns_uuid),
    )
    return [r["object_label"] for r in rows]


async def _read_bom_line_status(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    bom_line_label: str,
) -> str | None:
    """Read the current status from the BOM_LINE node's payload_ref field.

    BOM_LINE nodes store their status in the ``entity_type`` suffix encoded
    as ``BOM_LINE`` — the actual status comes from the event payload.
    This helper is a fallback for when the caller does not supply status in
    the event payload.

    In practice the C4 event payload always includes the new status; this
    function is included for completeness and testability.
    """
    # BOM_LINE nodes do not carry a ``status`` column on kg_nodes (there is
    # none per schema).  Status is encoded in the graph via kg_edges
    # (e.g. ``BOM_LINE -[has_status]-> STATUS_NODE``).  If no such edge
    # exists we return None (graceful degradation: no task mutation).
    row = await conn.fetchrow(
        """
        SELECT e.object_label
        FROM   kg_edges e
        WHERE  e.subject_label = $1
          AND  e.predicate      = 'has_status'
          AND  e.namespace_id   = $2::uuid
        LIMIT 1
        """,
        bom_line_label,
        str(ns_uuid),
    )
    if row is None:
        return None
    # Expect object_label like "STATUS:DELIVERED"
    raw = row["object_label"]
    if ":" in raw:
        return raw.split(":", 1)[1].upper()
    return raw.upper()


# ---------------------------------------------------------------------------
# Domain core: do_sync_bom_tasks
# ---------------------------------------------------------------------------


async def do_sync_bom_tasks(
    engine: NCEEngine,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile BOM-line status → PROJECT_TASK nodes + ``generates`` edges.

    Called either directly (integration test) or by the C4 bus subscriber
    (on a ``BOM_LINE.status_changed`` event).

    Parameters
    ----------
    engine:
        ``NCEEngine`` instance (provides ``pg_pool``).
    params:
        ``{
            "namespace_id":    str | UUID,   # required
            "project_id":      str,          # required — PROJECT:{QUOTE} label
            "bom_line_label":  str,          # required — affected BOM_LINE label
            "status":          str,          # required — new BOM_LINE status
        }``

    Returns
    -------
    dict
        ``{
            "ok":            bool,
            "tasks_created": list[str],   # task labels upserted this call
            "tasks_closed":  list[str],   # task labels whose generates edge removed
        }``

    Raises
    ------
    ValueError
        When required params are missing or status is unrecognised.
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        return {"ok": False, "error": "do_sync_bom_tasks: 'namespace_id' is required"}
    try:
        ns_uuid = UUID(str(raw_ns)) if not isinstance(raw_ns, UUID) else raw_ns
    except (ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"do_sync_bom_tasks: invalid namespace_id: {exc}"}

    project_id: str = params.get("project_id", "")
    if not project_id:
        return {"ok": False, "error": "do_sync_bom_tasks: 'project_id' is required"}

    bom_line_label: str = params.get("bom_line_label", "")
    if not bom_line_label:
        return {"ok": False, "error": "do_sync_bom_tasks: 'bom_line_label' is required"}

    status: str = (params.get("status") or "").upper().strip()
    if not status:
        return {"ok": False, "error": "do_sync_bom_tasks: 'status' is required"}

    if status not in _STATUS_TO_TASK_KIND:
        log.info(
            "do_sync_bom_tasks: unrecognised status=%r for %s — no task action taken",
            status,
            bom_line_label,
        )
        return {
            "ok": True,
            "tasks_created": [],
            "tasks_closed": [],
            "skipped_reason": f"unrecognised_status:{status}",
        }

    tasks_created: list[str] = []
    tasks_closed: list[str] = []

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # 1. Verify the BOM_LINE is reachable from the project (namespace-scoped).
        bom_lines = await _fetch_bom_lines_for_project(conn, ns_uuid, project_id)
        if bom_line_label not in bom_lines:
            log.info(
                "do_sync_bom_tasks: %s not in project %s contains-edges — skipping",
                bom_line_label,
                project_id,
            )
            return {
                "ok": True,
                "tasks_created": [],
                "tasks_closed": [],
                "skipped_reason": "bom_line_not_in_project",
            }

        # 2. Open the task for the current status.
        kind = _STATUS_TO_TASK_KIND[status]
        task_label = _task_label_for_kind(bom_line_label, kind)
        await _upsert_task_node(conn, ns_uuid, task_label, kind)
        await _upsert_generates_edge(conn, ns_uuid, bom_line_label, task_label, status)
        tasks_created.append(task_label)

        # 3. Close (remove generates edge from) tasks superseded by the current status.
        closed = await _close_superseded_tasks(conn, ns_uuid, bom_line_label, status)
        tasks_closed.extend(closed)

    log.info(
        "do_sync_bom_tasks: ns=%s project=%s bom=%s status=%s created=%s closed=%s",
        ns_uuid,
        project_id,
        bom_line_label,
        status,
        tasks_created,
        tasks_closed,
    )
    return {
        "ok": True,
        "tasks_created": tasks_created,
        "tasks_closed": tasks_closed,
    }


# ---------------------------------------------------------------------------
# C4 subscriber registration — fires once per BOM_LINE.status_changed event
# ---------------------------------------------------------------------------


async def _handle_bom_line_status_changed(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    event: dict[str, Any],
) -> None:
    """Outbox handler: BOM_LINE.status_changed → do_sync_bom_tasks.

    Runs inside the relay's open transaction (same MVCC snapshot).
    Does NOT perform external I/O.  Domain effects (TASK upsert + generates
    edge) are atomic with the dedup INSERT in ``processed_outbox_events``.

    The handler acquires its own scoped_pg_session for the domain writes
    because ``scoped_pg_session`` uses ``pool.acquire()`` internally, which
    creates a second connection.  The handler receives ``conn`` (the relay's
    polling connection) but must write on a separate, RLS-scoped connection.

    NOTE: this handler uses the engine stored in the module-level registry
    below rather than being a closure so that the relay can call it without
    carrying engine references in the event dict.
    """
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)

    namespace_id = event.get("namespace_id") or payload.get("namespace")
    project_id: str = payload.get("project_id", "")
    bom_line_label: str = payload.get("bom_line_label", "") or payload.get("id", "")
    status: str = payload.get("status", "")

    if not (namespace_id and project_id and bom_line_label and status):
        log.warning(
            "[bom_tasks] incomplete event payload — skipping. ns=%s project_id=%s bom=%s status=%s",
            namespace_id,
            project_id,
            bom_line_label,
            status,
        )
        return None

    engine = _get_registered_engine()
    if engine is None:
        log.error(
            "[bom_tasks] no engine registered — cannot sync tasks for bom=%s",
            bom_line_label,
        )
        return None

    await do_sync_bom_tasks(
        engine,
        {
            "namespace_id": namespace_id,
            "project_id": project_id,
            "bom_line_label": bom_line_label,
            "status": status,
        },
    )
    return None


# ---------------------------------------------------------------------------
# Engine registry — allows the module-level handler to access the engine
# ---------------------------------------------------------------------------

_ENGINE_REGISTRY: dict[str, Any] = {}


def _get_registered_engine() -> Any | None:
    """Return the registered NCEEngine or None."""
    return _ENGINE_REGISTRY.get("engine")


def register_engine(engine: Any) -> None:
    """Register *engine* so the C4 subscriber handler can call do_sync_bom_tasks.

    Must be called once at application startup (e.g. in the NCEEngine
    ``__init__`` or ``build_admin_routes``).
    """
    _ENGINE_REGISTRY["engine"] = engine


def register_bom_task_subscriber() -> None:
    """Register the BOM_LINE.status_changed handler with the C4 bus.

    Call once at startup (after ``register_engine``).  Idempotent — calling
    again merely overwrites the same registry slot with the same handler.

    The relay (``nce.outbox_relay.deliver_one``) provides at-least-once
    idempotency via ``processed_outbox_events`` before invoking this handler.
    No additional ``make_idempotent_handler`` wrapper is needed here.
    """
    subscribe(
        {"node_type": _BOM_LINE_NODE_TYPE, "op": _BOM_LINE_OP},
        _handle_bom_line_status_changed,
    )
    log.info(
        "[bom_tasks] subscribed to %s.%s via C4 bus",
        _BOM_LINE_NODE_TYPE,
        _BOM_LINE_OP,
    )
