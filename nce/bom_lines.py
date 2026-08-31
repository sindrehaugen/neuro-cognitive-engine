"""
nce/bom_lines.py
=================
The BOM_LINE content store's guarded writer module (Module 0, Wave 31 —
Batch 132a).

BOM_LINE is referenced by five-or-more engines (sales/dealroom.py,
project/convert.py, project/tasks.py all SELECT it) but no wave in the
original 231 was ever assigned to WRITE it. This module is that write path.
It is top-level, shared foundation — deliberately NOT under
``vertical_modules/`` — because both ``system_design`` (manual design
authoring) and ``sales`` (manual pick, package expansion, external ingest)
write it, and ``system_design`` must not import ``sales``.

This wave builds the STORE and its guarded writer only. It does NOT build any
origination path — manual pick, design-generated, package expansion and
external ingest are Batches 132d-132i. Nothing in this repo calls these
functions yet.

Trust boundary -- READ BEFORE ADDING A CALLER
-----------------------------------------------
``origin_kind``, ``origin_ref`` and ``writer_engine`` are set by the CALLING
FLOW'S OWN CODE, never read from caller-supplied tool arguments. There is no
public parameter named ``origin_kind`` on any write function in this module:
``flow`` is accepted as a closed ``CreateFlow`` literal and this module's own
mapping (``flow`` IS ``origin_kind``, verbatim) is the only thing that ever
produces an ``origin_kind`` or a ``transition`` string. This is structural,
not a convention to remember: a caller cannot make ``flow`` disagree with
the ``origin_kind`` that gets stored, because nothing accepts the value
directly. ``origin_kind`` is an open ``TEXT`` column with no CHECK (see
migration 058's header) specifically so new flows never need a DDL change --
which is exactly why it must never be caller-writable: a manually entered
line could otherwise claim ``origin_kind='design'``, and a later
reconciliation report against legally signed baselines would trust it.

Field ownership (roadmap Section 9.1, the "5-writer BOM_LINE" decomposition)
-----------------------------------------------------------------------------
CONTENT (qty/unit_price/line_total/currency) is authored by whichever flow
created the line and Sales-freezes at contract signature (a
``BEFORE UPDATE`` trigger in migration 058, not a GRANT and not the
registry). STATUS advances independently through
Procurement -> Inventory -> Field Tech, and keeps advancing even after
content is frozen -- freezing content must never block a status transition.
``actual_cost`` belongs to the Economy cascade alone
(``economy_bom_actual_costs``, migration 047) and has no column here.

Freeze helper
--------------
``freeze_bom_lines_for_quote`` is exposed here for Batch 132i to call from
``do_freeze_baseline`` (``nce/vertical_modules/sales/baseline.py``) -- this
wave does NOT edit ``baseline.py``. Every creator path's "is this quote
frozen" check must import THIS helper, never reimplement the question --
Batches 123 and 126 are both records of what happens when a rule gets a
second implementation.

``has_status`` graph projection -- declared, not built
----------------------------------------------------------
The build plan designs a ``has_status`` kg_edges projection from this
table's ``status`` column (Appendix B, finding 5) and no wave builds one.
That gap is NOT closed here. This table's ``status`` column is the
authoritative source of truth; the graph projection is UNBUILT. This is a
declared omission, not a silent one.

Namespace scoping
-------------------
Every query below carries an explicit ``namespace_id`` predicate and never
relies on RLS alone: the owner/superuser connection pool used by background
jobs and by most of this test suite BYPASSES FORCE RLS, so an unscoped query
would pass its own test and leak in production. This has already bitten
three prior waves (B67, B120, B130).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.bom_lines")

# ---------------------------------------------------------------------------
# Engine identifier and node type -- must match node-ownership.json verbatim.
# ---------------------------------------------------------------------------
NODE_TYPE_BOM_LINE: str = "BOM_LINE"

# The four content-authoring flows. Closed by construction: a caller can only
# ever pass one of these four strings, and each maps to exactly one
# transition suffix and exactly one origin_kind (the same string, here).
CreateFlow = Literal["design", "manual", "package", "external"]

# The three status states this wave's registry rows cover. status:ordered has
# no builder yet (procurement/po.py writes no BOM_LINE status today) -- that
# is a known, out-of-scope gap, not something this module can detect.
StatusState = Literal["ORDERED", "DELIVERED", "INSTALLED"]

# kg_nodes.change_origin's own CHECK-constrained vocabulary (sync/webhook/
# agent/operator/consolidation/replay/unknown). Unrelated to origin_kind,
# which is this table's own open-by-construction provenance column -- do not
# conflate the two.
_CHANGE_ORIGIN: str = "agent"


def bom_line_label(quote_id: str, line_ref: str) -> str:
    """The one place the BOM_LINE label convention is built.

    Verbatim in shape from ``nce/vertical_modules/project/convert.py:144``
    (``_bom_line_label``) -- that engine only ever RECONSTRUCTS this label to
    write a ``contains`` edge; this module is the sole AUTHOR of the
    underlying node and content row, so the convention lives here and every
    other module should read it from here rather than re-deriving it.
    """
    return f"BOM_LINE:{quote_id.upper()}:{line_ref.upper()}"


def _ns_uuid(namespace_id: str | UUID) -> UUID:
    return namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))


def _as_row_dict(row: asyncpg.Record) -> dict[str, Any]:  # type: ignore[type-arg]
    """Convert a bom_line_content row to a JSON-friendly dict.

    Money/qty columns are NUMERIC in Postgres; converted to float here to
    match the convention already used for money elsewhere in this repo
    (e.g. sales/baseline.py's signed_total_nok).
    """
    return {
        "id": str(row["id"]),
        "namespace_id": str(row["namespace_id"]),
        "bom_line_label": row["bom_line_label"],
        "quote_id": row["quote_id"],
        "line_ref": row["line_ref"],
        "qty": float(row["qty"]),
        "unit_price": float(row["unit_price"]),
        "line_total": float(row["line_total"]),
        "currency": row["currency"],
        "origin_kind": row["origin_kind"],
        "origin_ref": row["origin_ref"],
        "writer_engine": row["writer_engine"],
        "status": row["status"],
        "status_changed_at": (
            row["status_changed_at"].isoformat() if row["status_changed_at"] else None
        ),
        "frozen_at": row["frozen_at"].isoformat() if row["frozen_at"] else None,
    }


async def _upsert_bom_line_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: UUID,
    label: str,
) -> None:
    """Upsert the BOM_LINE kg_node.

    No ``*_source_id`` column is written -- none exists for this shared node
    type. ``confidence`` is NOT written either: ``kg_nodes`` has no such
    column and never should (rule 7).
    """
    await conn.execute(
        """
        INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
        VALUES ($1, $2, $3::uuid, $4)
        ON CONFLICT (label, namespace_id) DO NOTHING
        """,
        label,
        NODE_TYPE_BOM_LINE,
        str(namespace_id),
        _CHANGE_ORIGIN,
    )


async def create_bom_line(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    flow: CreateFlow,
    writer_engine: str,
    quote_id: str,
    line_ref: str,
    qty: Decimal | float,
    unit_price: Decimal | float,
    line_total: Decimal | float,
    currency: str = "NOK",
    origin_ref: str | None = None,
) -> dict[str, Any]:
    """Author a new BOM_LINE: the ``bom_line_content`` row AND the
    ``kg_nodes`` ``BOM_LINE`` row, in one transaction (the caller's -- this
    function issues no BEGIN/COMMIT of its own; call it inside
    ``scoped_pg_session``).

    Guarded by ``assert_owner`` against ``content:create:{flow}`` -- deny by
    default when unregistered or when ``writer_engine`` is not the flow's
    registered owner.

    Idempotent: replaying the same ``(namespace_id, quote_id, line_ref)``
    does not create a second row -- the natural key (``namespace_id``,
    ``bom_line_label``) makes the INSERT a no-op via
    ``ON CONFLICT ... DO NOTHING``, and the existing row is returned.
    """
    ns_uuid = _ns_uuid(namespace_id)
    transition = f"content:create:{flow}"
    origin_kind = flow  # structural: flow IS origin_kind, never a free string.

    await assert_owner(conn, ns_uuid, NODE_TYPE_BOM_LINE, writer_engine, transition)

    label = bom_line_label(quote_id, line_ref)

    row = await conn.fetchrow(
        """
        INSERT INTO bom_line_content (
            namespace_id, bom_line_label, quote_id, line_ref,
            qty, unit_price, line_total, currency,
            origin_kind, origin_ref, writer_engine
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (namespace_id, bom_line_label) DO NOTHING
        RETURNING *
        """,
        ns_uuid,
        label,
        quote_id,
        line_ref,
        qty,
        unit_price,
        line_total,
        currency,
        origin_kind,
        origin_ref,
        writer_engine,
    )

    if row is None:
        # Already existed -- idempotent replay. Fetch the existing row rather
        # than silently returning nothing.
        row = await conn.fetchrow(
            "SELECT * FROM bom_line_content WHERE namespace_id = $1 AND bom_line_label = $2",
            ns_uuid,
            label,
        )
        assert row is not None  # the conflict proves a row exists
        await _upsert_bom_line_node(conn, ns_uuid, label)
        return _as_row_dict(row)

    await _upsert_bom_line_node(conn, ns_uuid, label)
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=NODE_TYPE_BOM_LINE,
        op="upserted",
        node_id=label,
    )
    return _as_row_dict(row)


async def update_bom_line_content(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    flow: CreateFlow,
    writer_engine: str,
    quote_id: str,
    line_ref: str,
    qty: Decimal | float | None = None,
    unit_price: Decimal | float | None = None,
    line_total: Decimal | float | None = None,
    currency: str | None = None,
    origin_ref: str | None = None,
) -> dict[str, Any]:
    """Edit an EXISTING line's content pre-freeze (draft editing).

    Guarded by ``assert_owner`` against ``content:update:{flow}`` -- the row
    this wave's brief calls out as the plan's omitted, functional hole:
    without it, pre-freeze editing denies for every engine, including the
    line's own creator.

    Only supplied (non-``None``) fields are updated -- ``COALESCE(new,
    old)`` per column. If the line is frozen, the DB trigger
    (``reject_frozen_bom_line_mutation``, migration 058) refuses any content
    change and raises; this function does not pre-check frozen state itself,
    so the trigger is the single source of truth for that rule.
    """
    ns_uuid = _ns_uuid(namespace_id)
    transition = f"content:update:{flow}"

    await assert_owner(conn, ns_uuid, NODE_TYPE_BOM_LINE, writer_engine, transition)

    label = bom_line_label(quote_id, line_ref)

    row = await conn.fetchrow(
        """
        UPDATE bom_line_content
        SET qty         = COALESCE($3, qty),
            unit_price  = COALESCE($4, unit_price),
            line_total  = COALESCE($5, line_total),
            currency    = COALESCE($6, currency),
            origin_ref  = COALESCE($7, origin_ref),
            updated_at  = now()
        WHERE namespace_id = $1 AND bom_line_label = $2
        RETURNING *
        """,
        ns_uuid,
        label,
        qty,
        unit_price,
        line_total,
        currency,
        origin_ref,
    )
    if row is None:
        raise ValueError(f"bom_line_content: no row for label={label!r} in this namespace")
    return _as_row_dict(row)


async def update_bom_line_status(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    writer_engine: str,
    quote_id: str,
    line_ref: str,
    status: StatusState,
) -> dict[str, Any]:
    """Advance a line's status. Entry point Batch 133b will call to flip
    fully-received lines to ``DELIVERED``.

    Guarded by ``assert_owner`` against ``status:{state}`` (lower-cased) --
    e.g. ``update_bom_line_status(..., status="DELIVERED",
    writer_engine="inventory")`` checks ``status:delivered``.

    This ALWAYS succeeds against a frozen line -- status and
    status_changed_at are deliberately outside the freeze trigger's
    protected column set (migration 058). Content freezing must never block
    status from advancing.
    """
    ns_uuid = _ns_uuid(namespace_id)
    transition = f"status:{status.lower()}"

    await assert_owner(conn, ns_uuid, NODE_TYPE_BOM_LINE, writer_engine, transition)

    label = bom_line_label(quote_id, line_ref)

    row = await conn.fetchrow(
        """
        UPDATE bom_line_content
        SET status = $3, status_changed_at = now(), updated_at = now()
        WHERE namespace_id = $1 AND bom_line_label = $2
        RETURNING *
        """,
        ns_uuid,
        label,
        status,
    )
    if row is None:
        raise ValueError(f"bom_line_content: no row for label={label!r} in this namespace")
    return _as_row_dict(row)


async def freeze_bom_lines_for_quote(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    writer_engine: str,
    quote_id: str,
) -> int:
    """Freeze every not-yet-frozen BOM_LINE row for *quote_id*.

    Exposed here for Batch 132i to call from ``do_freeze_baseline`` /
    ``_do_freeze_baseline_direct``
    (``nce/vertical_modules/sales/baseline.py:120-210``) -- that function
    already runs in one ``scoped_pg_session`` transaction and is already
    idempotent; this helper is written to be safe to call from inside it (no
    nested transaction, no re-entrant ownership check per row).

    Guarded once, by ``assert_owner`` against ``content:freeze`` -- not
    per-row, since freezing is one operation over a quote's lines, not a
    per-flow content edit.

    Idempotent: rows already frozen (``frozen_at IS NOT NULL``) are excluded
    from the WHERE clause, so a repeat call freezes nothing further and
    never touches ``frozen_at`` on an already-frozen row (which the trigger
    would refuse in any case).
    """
    ns_uuid = _ns_uuid(namespace_id)
    await assert_owner(conn, ns_uuid, NODE_TYPE_BOM_LINE, writer_engine, "content:freeze")

    status = await conn.execute(
        """
        UPDATE bom_line_content
        SET frozen_at = now(), updated_at = now()
        WHERE namespace_id = $1 AND quote_id = $2 AND frozen_at IS NULL
        """,
        ns_uuid,
        quote_id,
    )
    try:
        return int(status.split()[-1])
    except (AttributeError, ValueError, IndexError):
        return 0


async def get_bom_line(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    quote_id: str,
    line_ref: str,
) -> dict[str, Any] | None:
    """Read a single line by (quote_id, line_ref). Explicitly namespace_id-
    scoped -- never relies on RLS alone (see module docstring)."""
    ns_uuid = _ns_uuid(namespace_id)
    label = bom_line_label(quote_id, line_ref)
    row = await conn.fetchrow(
        "SELECT * FROM bom_line_content WHERE namespace_id = $1 AND bom_line_label = $2",
        ns_uuid,
        label,
    )
    return _as_row_dict(row) if row is not None else None


async def list_bom_lines_for_quote(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    quote_id: str,
) -> list[dict[str, Any]]:
    """Read every line for a quote. For 132f / Batch 142's readers.
    Explicitly namespace_id-scoped -- never relies on RLS alone."""
    ns_uuid = _ns_uuid(namespace_id)
    rows = await conn.fetch(
        "SELECT * FROM bom_line_content WHERE namespace_id = $1 AND quote_id = $2 "
        "ORDER BY line_ref",
        ns_uuid,
        quote_id,
    )
    return [_as_row_dict(r) for r in rows]
