"""nce.vertical_modules.system_design.backfill_fl — Wave G-3: FL tree backfill.

Reads the host's functional-location / room-map records from a configured
source schema on the same Postgres instance and decides, per row, whether
it is already imported, matches an existing FUNCTIONAL_LOCATION node, or
is a genuine new node — without ever writing or auto-merging.

Charter contract (MLV16_ORCH_CHARTER_2026-09-18.md §9 "Lane G"):
  - Source schema name from ``NCE_IMPORT_SOURCE_SCHEMA_FL``, never a literal.
  - Idempotent on (source_system, source_id); resumable via a watermark.
  - Dry-run first, with counts; a re-run inserts 0.
  - Collisions go to the C1 merge queue (``nce.entity_resolution.merge_queue``);
    never auto-merged.
  - Runs against the rig namespace S01 only while Q-26 is OPEN.

TWO GENUINE UNKNOWNS, both left as explicit stubs rather than invented
(MLV16G.md carries the full reasoning):

1. WRITE PATH. The node-creation call is
   ``nce.vertical_modules.system_design.graph.upsert_fl_path`` — a public
   upsert core Lane C is adding (ML-orch dispatch, 2026-09-19) so this
   importer and ``do_author_functional_location`` share one write path
   instead of two. Until it lands, ``apply_fl_backfill`` raises
   ``NotImplementedError``. Everything else in this module needs no write
   path and is real, tested code today.
2. SOURCE SHAPE. The host's FL/room-map mirror's actual table and column
   names have not been confirmed against the shared dev DB — the analysis
   this charter is built from only measured *route families*
   (``room-map`` 48 routes, ``adresse-fl``, ``fl-feil``), never a schema.
   ``fetch_source_fl_rows`` raises ``NotImplementedError`` with the
   ``information_schema`` query to run first, rather than guessing a
   table name that would silently fail (or worse, silently succeed
   against the wrong table) once a real connection exists.

WATERMARK, WITHOUT A NEW TABLE. Lane G may never create a new table
(charter §9's "never" list). Rather than add a dedicated cursor table,
the resume watermark is *derived* from already-imported FL nodes: the
latest ``kg_nodes.updated_at`` among rows carrying this importer's
``system_design_source_id`` prefix, minus a clock-skew overlap — the same
shape as the existing incremental-sync convention in
``nce/vertical_modules/dynamics365/sync.py`` (``CURSOR_OVERLAP_SECONDS``),
reused rather than reinvented.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from nce.entity_resolution.merge_queue import enqueue as merge_enqueue
from nce.entity_resolution.resolver import resolve as c1_resolve
from nce.vertical_modules.system_design.fold_rules import (
    DEFAULT_FOLD_RULES,
    FoldRules,
)

log = logging.getLogger("nce.vertical_modules.system_design.backfill_fl")

SOURCE_SCHEMA_ENV = "NCE_IMPORT_SOURCE_SCHEMA_FL"
_NODE_TYPE_FL = "FUNCTIONAL_LOCATION"

# The (source_system, source_id) composite the charter asks for, folded into
# the one existing TEXT column (kg_nodes.system_design_source_id) as
# "<source_system>:<source_id>" rather than adding a column — there is
# exactly one FL source system in the v1.6 backfill set (§5), so a prefix
# is sufficient and needs no migration.
SOURCE_SYSTEM = "d365_fl"

# Clock-skew safety window applied to the derived watermark, mirroring
# dynamics365/sync.py's CURSOR_OVERLAP_SECONDS convention.
WATERMARK_OVERLAP = timedelta(seconds=300)

# A match at or above this score is treated as "this is the same node" —
# reported as a match, never silently written. Below it but at/above
# _QUEUE_FLOOR_SCORE is genuinely ambiguous and goes to the merge queue.
# Below the floor is treated as no match (insert candidate).
# PROPOSED STARTING POINT, NOT A DECIDED CONTRACT — re-tune against a real
# sample from the source schema at dispatch; do not trust these numbers
# any further than the rest of this document's unmeasured claims.
AUTO_MATCH_SCORE = 0.92
QUEUE_FLOOR_SCORE = 0.55


class BackfillConfigError(RuntimeError):
    """Raised when required importer configuration is missing."""


@dataclass(frozen=True, slots=True)
class SourceFLRow:
    """One row read from the host's FL/room-map source schema.

    ``path_parts`` is the ordered hierarchy (site, building, floor, room,
    ...) as the source names it; folding/normalisation happens in this
    module via ``fold_rules``, not at the source.
    """

    source_id: str
    path_parts: tuple[str, ...]
    address: str | None = None
    coordinates: tuple[float, float] | None = None
    modified_at: str | None = None  # ISO-8601, source-side last-modified


@dataclass(slots=True)
class FLBackfillDecision:
    """The decision made for one source row, before any write happens."""

    source_row: SourceFLRow
    candidate_label: str
    action: str  # "insert" | "match" | "queue"
    matched_node_id: UUID | None = None
    match_score: float | None = None
    match_reason: str | None = None


@dataclass(slots=True)
class DryRunReport:
    """Dry-run counts — the wave's first required gate (charter §9)."""

    total_source_rows: int = 0
    would_insert: int = 0
    would_match_existing: int = 0
    would_queue_for_review: int = 0
    decisions: list[FLBackfillDecision] = field(default_factory=list)


def source_schema_name() -> str:
    """Read the configured source schema name.

    Never a literal in the tree (charter §9 identity gate) — always read
    from ``NCE_IMPORT_SOURCE_SCHEMA_FL`` at call time, never cached as a
    module constant, so a test or a re-dispatch can change it cleanly.
    """
    schema = os.environ.get(SOURCE_SCHEMA_ENV)
    if not schema:
        raise BackfillConfigError(
            f"{SOURCE_SCHEMA_ENV} is not set — refuse to guess the host's schema name."
        )
    return schema


def source_key(row_source_id: str) -> str:
    """Build the ``system_design_source_id`` value for one source row."""
    return f"{SOURCE_SYSTEM}:{row_source_id}"


def candidate_fl_label(namespace_slug: str, path_parts: tuple[str, ...]) -> str:
    """Deterministic FUNCTIONAL_LOCATION label for a source row.

    Mirrors ``nce.vertical_modules.system_design.graph._fl_label`` exactly
    (``FL:<namespace_slug>:<PART>:<PART>...``, upper-cased) — duplicated
    here only because that helper is private to Lane C's module and is a
    3-line pure string format, not an invariant with write-side
    consequences like the ON CONFLICT logic. ``tests/unit/
    test_system_design_backfill_fl.py`` asserts the two stay identical as
    a drift guard; if graph.py's convention ever changes, that test goes
    red here, not silently.
    """
    parts = ":".join(p.upper() for p in path_parts)
    return f"FL:{namespace_slug.upper()}:{parts}"


async def load_watermark(conn: Any, namespace_id: str | UUID) -> str | None:
    """Derive the resume watermark from already-imported FL nodes.

    No dedicated watermark table — Lane G may never create a new table.
    Returns an ISO-8601 timestamp string (minus ``WATERMARK_OVERLAP``) or
    ``None`` when nothing has been imported yet (triggers a full pull).
    """
    if conn is None or not hasattr(conn, "fetchval"):
        return None
    max_updated = await conn.fetchval(
        """
        SELECT MAX(updated_at) FROM kg_nodes
        WHERE entity_type = $1
          AND namespace_id = $2::uuid
          AND system_design_source_id LIKE $3
        """,
        _NODE_TYPE_FL,
        str(namespace_id),
        f"{SOURCE_SYSTEM}:%",
    )
    if max_updated is None:
        return None
    return (max_updated - WATERMARK_OVERLAP).isoformat()


async def fetch_source_fl_rows(
    conn: Any,
    schema: str,
    *,
    since: str | None = None,
) -> list[SourceFLRow]:
    """Read FL/room-map rows from the configured schema. NOT YET IMPLEMENTED.

    ``schema`` is always ``source_schema_name()``'s return value, passed in
    rather than read here so callers (and tests) control it explicitly.

    Deliberately unimplemented: the source table's real name and columns
    have not been confirmed against the shared dev DB. Run this first,
    against the schema actually configured at dispatch, and update this
    function (and ``SourceFLRow``'s fields, if the shape differs) from the
    result — never from this module's guess:

        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = '<the configured schema>'
        ORDER BY table_name, ordinal_position;
    """
    raise NotImplementedError(
        "fetch_source_fl_rows: source table shape not yet confirmed against "
        f"schema '{schema}'. Run the information_schema query in this "
        "function's docstring against the live rig namespace S01 database "
        "first; see MLV16G.md Wave G-3 for status."
    )


async def _find_node_by_source_key(conn: Any, namespace_id: str | UUID, key: str) -> UUID | None:
    if conn is None or not hasattr(conn, "fetchval"):
        return None
    row_id = await conn.fetchval(
        """
        SELECT id FROM kg_nodes
        WHERE entity_type = $1 AND namespace_id = $2::uuid
          AND system_design_source_id = $3
        LIMIT 1
        """,
        _NODE_TYPE_FL,
        str(namespace_id),
        key,
    )
    return UUID(str(row_id)) if row_id is not None else None


async def _find_node_by_label(conn: Any, namespace_id: str | UUID, label: str) -> UUID | None:
    if conn is None or not hasattr(conn, "fetchval"):
        return None
    row_id = await conn.fetchval(
        """
        SELECT id FROM kg_nodes
        WHERE entity_type = $1 AND namespace_id = $2::uuid AND label = $3
        LIMIT 1
        """,
        _NODE_TYPE_FL,
        str(namespace_id),
        label,
    )
    return UUID(str(row_id)) if row_id is not None else None


async def decide_fl_backfill_action(
    conn: Any,
    namespace_id: str | UUID,
    row: SourceFLRow,
    *,
    namespace_slug: str,
    fold_rules: FoldRules = DEFAULT_FOLD_RULES,  # noqa: ARG001 -- reserved for pre-C1 fold pass; see note below
) -> FLBackfillDecision:
    """Decide insert / match / queue for one source row. Never writes.

    Order of checks, cheapest and most certain first:

    1. Already imported — an FL node already carries this row's
       ``system_design_source_id``. Re-running the importer must be a
       no-op (charter gate: "a re-run inserts 0"); this is that check.
    2. Exact deterministic-label match — the row's computed label already
       exists under a different source id (e.g. authored by hand or by
       ``do_author_functional_location`` before the backfill ran).
    3. C1 fuzzy match (``entity_resolution.resolver.resolve``) by name
       against existing FUNCTIONAL_LOCATION nodes. A high-confidence
       match is reported as "match" — it is *never* written here;
       reconciling two distinct kg_nodes rows is ``merge_fl_nodes``'s job
       (an explicit, human/confirmed operation), not this importer's. A
       sub-threshold match is queued via
       ``entity_resolution.merge_queue.enqueue`` for review.
    4. No match at all → insert.

    No-auto-merge is not a promise this function keeps by convention —
    it is enforced structurally one layer down: ``merge_queue.enqueue``
    can only ever create a pending row, and ``confirm``/``reject`` touch
    only that row, never ``kg_nodes``/``kg_edges`` (see that module's own
    docstring). This function cannot violate the invariant even if it
    tried to.

    ``fold_rules`` is accepted now and threaded through once the source
    row shape is confirmed (``fetch_source_fl_rows``) — the fold pass
    (stripping "Room "/"møterom " etc. before the exact-label check) reads
    ``row.path_parts`` in a way that depends on knowing which part is the
    room-name component in the *real* source shape, not the placeholder
    tuple used here. Wiring it in now against an unconfirmed shape would
    be exactly the kind of invented behaviour this module is trying not
    to ship.
    """
    key = source_key(row.source_id)
    label = candidate_fl_label(namespace_slug, row.path_parts)

    already = await _find_node_by_source_key(conn, namespace_id, key)
    if already is not None:
        return FLBackfillDecision(
            source_row=row,
            candidate_label=label,
            action="match",
            matched_node_id=already,
            match_reason="already imported (system_design_source_id)",
        )

    exact = await _find_node_by_label(conn, namespace_id, label)
    if exact is not None:
        return FLBackfillDecision(
            source_row=row,
            candidate_label=label,
            action="match",
            matched_node_id=exact,
            match_reason="exact deterministic label match",
        )

    name = row.path_parts[-1] if row.path_parts else ""
    matches = await c1_resolve(
        conn,
        namespace_id=namespace_id,
        candidate={"name": name},
        keys=["name"],
        node_type=_NODE_TYPE_FL,
    )
    top = matches[0] if matches else None

    if top is not None and top.score >= AUTO_MATCH_SCORE:
        return FLBackfillDecision(
            source_row=row,
            candidate_label=label,
            action="match",
            matched_node_id=top.node_id,
            match_score=top.score,
            match_reason=f"C1 resolve high-confidence match on {top.matched_on}",
        )

    if top is not None and top.score >= QUEUE_FLOOR_SCORE:
        queue_id = await merge_enqueue(
            conn,
            namespace_id=namespace_id,
            node_type=_NODE_TYPE_FL,
            candidate={
                "source_id": row.source_id,
                "path_parts": list(row.path_parts),
                "label": label,
            },
            target=top.node_id,
            score=top.score,
        )
        return FLBackfillDecision(
            source_row=row,
            candidate_label=label,
            action="queue",
            matched_node_id=top.node_id,
            match_score=top.score,
            match_reason=f"queued for review as {queue_id}",
        )

    return FLBackfillDecision(source_row=row, candidate_label=label, action="insert")


async def dry_run_fl_backfill(
    conn: Any,
    namespace_id: str | UUID,
    rows: list[SourceFLRow],
    *,
    namespace_slug: str,
    fold_rules: FoldRules = DEFAULT_FOLD_RULES,
) -> DryRunReport:
    """Run the decision logic over already-fetched rows. Never writes.

    Takes ``rows`` rather than fetching them itself so this function (the
    part of the wave that is fully real today) does not depend on
    ``fetch_source_fl_rows`` (the part that is not). Wire the two together
    once the source shape is confirmed.
    """
    report = DryRunReport(total_source_rows=len(rows))
    for row in rows:
        decision = await decide_fl_backfill_action(
            conn, namespace_id, row, namespace_slug=namespace_slug, fold_rules=fold_rules
        )
        report.decisions.append(decision)
        if decision.action == "insert":
            report.would_insert += 1
        elif decision.action == "match":
            report.would_match_existing += 1
        elif decision.action == "queue":
            report.would_queue_for_review += 1
    return report


async def apply_fl_backfill(*_args: Any, **_kwargs: Any) -> None:
    """Write decided inserts through the FL tree's upsert core. NOT YET IMPLEMENTED.

    Blocked on ``nce.vertical_modules.system_design.graph.upsert_fl_path``,
    the public upsert core Lane C is adding so this importer and
    ``do_author_functional_location`` share one write path (ML-orch
    dispatch, 2026-09-19 — see MLV16G.md). Do not work around this by
    calling ``do_author_functional_location`` directly (it fabricates an
    unrelated DESIGN node per call) or by duplicating
    ``graph._upsert_fl_node``'s SQL here (a second copy of an
    ``ON CONFLICT`` invariant this estate has already been bitten by three
    times this week).
    """
    raise NotImplementedError(
        "apply_fl_backfill: blocked on system_design.graph.upsert_fl_path "
        "(Lane C, dispatched 2026-09-19). See MLV16G.md Wave G-3."
    )
