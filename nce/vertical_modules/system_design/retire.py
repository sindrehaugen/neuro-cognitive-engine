"""
nce/vertical_modules/system_design/retire.py
=============================================
Module 6, Wave 17 (B067h) — **the first delete path in this codebase.**

Before this file there was not one ``DELETE FROM kg_nodes`` or
``DELETE FROM kg_edges`` in any vertical module.  Every design write was
additive: ``do_author_device_topology`` upserts and never removes, so a device
a user deleted on the canvas survived in the graph with its ports, edges and
capability row.  Expressing a removal is this wave's job, and everything below
is shaped by the fact that a mistake here is not recoverable by re-running
anything.

THE TOOL'S NAME IS A DELIBERATE MISMATCH WITH ITS DEFAULT BEHAVIOUR
-------------------------------------------------------------------
The MCP tool is ``system_design_delete_planned`` and the REST route is
``DELETE /api/system-design/planned``.  Both are pinned by Copper's published
contract and renaming either breaks the front end, so both keep those names —
**but the default behaviour is a SOFT RETIRE, not a delete.**  Nothing is
removed unless the caller passes ``permanent=true``.

That mismatch is stated in the first line of every docstring on the path rather
than quietly tolerated, and the safety argument is the direction of the
surprise: a caller who reads the *name* and not the docstring expects a delete
and gets a status change, which is recoverable.  The reverse arrangement —
a reassuring name over a destructive default — is the one that cannot be
walked back.

WHAT "RETIRE" WRITES, AND WHY IT IS NOT A CONFIDENCE DROP
----------------------------------------------------------
A soft retire writes ``system_design_node_state.status`` (migration 061) and
drops the node's ``salience`` to :data:`RETIRED_SALIENCE`.  It does **not**
express retirement by writing down ``kg_edges.confidence``, which is the
obvious-looking shortcut and is wrong twice over: ``confidence`` means "how
sure are we this edge is true", which is a different fact from "this equipment
is on its way out", and ``nce/garbage_collector.py``'s edge prune deletes edges
whose ``confidence < 0.15`` past an age threshold, sparing only
``change_origin = 'sync'`` rows.  A design edge is not a sync row, so
overloading confidence would silently enrol every retired design in a prune
rule written for something else — a second, invisible delete path bolted onto
the first one.

THE RETIRED STATUS IS PER NODE TYPE.  ``'decommissioning'`` IS NOT UNIVERSAL
-----------------------------------------------------------------------------
Migration 061's status CHECK is composite and the vocabularies are DISJOINT:

    DEVICE -> planned | staged | active | offline | decommissioning |
              inventory | failed
    CABLE  -> planned | connected | decommissioning
    RACK   -> reserved | available | planned | active | deprecated

**RACK has no ``'decommissioning'``.**  A single retired-status constant would
therefore be refused by the database on every rack, which is the good failure;
the bad one is a future reader "fixing" that by relaxing the CHECK.  The
mapping is :data:`RETIRE_STATUS_BY_NODE_TYPE`, it is keyed by node type for
that reason, and the vocabulary itself still lives only in the DDL.

SALIENCE IS SET TO AN ABSOLUTE FLOOR, NEVER DECAYED
-----------------------------------------------------
:data:`RETIRED_SALIENCE` is ``0`` — written absolutely, not multiplied down.
PostgreSQL ``numeric`` NaN is **not** IEEE NaN: ``'NaN' > <any finite>`` is
TRUE and ``'NaN' = 'NaN'`` is TRUE, so a stored NaN sorts as the largest
salience in the tenant.  A multiplicative decay (``salience * 0.1``) preserves
NaN exactly, so a retired node would keep the highest salience in the tenant
forever; an absolute write cannot.  Migration 061's CHECK already refuses NaN
on the way in, so this is defence-in-depth rather than a live hole — but it
costs nothing, and "the only value that survives every arithmetic operation" is
precisely the value a destructive path must not depend on being absent.

Zero is also the value with a precedent: this engine's own salience decay
clamps at a floor of zero (``GREATEST(0.0, ...)``, ``nce/me_app.py``), so a
retired node lands on a value the rest of NCE already treats as the bottom.

No predicate in this file compares salience against a threshold.  That is
deliberate: any "salience below X -> act" test would have the NaN ordering
problem above, and there is no reason to write one.

EVERY GUARD FAILS CLOSED
-------------------------
Nothing is retired unless the state row says, positively, that this node was
declared ``'planned'``:

* **No state row -> DENY.**  This is the load-bearing one.  Migration 061's
  writer creates a row only for a node genuinely new to an authoring call or
  one the caller sent an explicit lifecycle key for, so **absence is the normal
  case for everything authored before W16** — the whole legacy as-built estate,
  permanently, not merely until the next canvas save.  It is the only thing
  standing between this tool and real, installed equipment.
* **``status IS NULL`` -> DENY.**  "We hold a revision or a salience for this
  node, nobody declared its lifecycle."  NULL is never read as planned.
* **``status <> 'planned'`` -> DENY**, including ``'active'`` and every other
  live value.
* **The node itself missing from ``kg_nodes`` -> DENY.**  A state row whose
  node is gone is an orphan, and reporting a successful retirement of something
  that is not there is a lie in an audit record.
* **A label outside this design -> REFUSED** as a malformed argument, so a
  caller cannot name a node in another design (or the design's own version row)
  by passing a well-formed label.
* **PORT -> REFUSED.**  NetBox has no lifecycle status for a port, migration
  061's CHECK cannot store one, so a port can never be *declared* planned and
  can never be retired on its own.  Ports go with their device on the permanent
  path and only there.
* **``actor`` is MANDATORY on the permanent path.**  Rev 2 §1 says ``actor`` is
  optional everywhere else and is never invented; this is the one place the
  omission is refused, because an unattributable permanent delete is exactly
  what should fail closed.

ALL OR NOTHING
---------------
One denied label denies the whole call.  A partial success on a destructive
operation forces the caller to diff what they asked for against what happened,
in a payload they have to trust, and gets the answer wrong under concurrency.
Refusing the request outright also makes "a mid-call failure leaves nothing
deleted" a property of the transaction rather than a claim about ordering.

``active`` deletion is OUT OF SCOPE.  This path acts on ``'planned'`` and only
``'planned'``; retiring live equipment is a different decision with a different
approval and is not expressible here.

🔴 D12 — THE OBLIGATION MIGRATION 061 HANDED THIS WAVE
-------------------------------------------------------
**No foreign key ties any side-table row to its ``kg_nodes`` node.**  True of
all three: ``system_design_device_capabilities`` (migration 039),
``system_design_geometry`` (060) and ``system_design_node_state`` (061) each
reference only ``namespaces(id)``.  ``kg_nodes`` is HASH-partitioned on
``label`` and its natural key is ``(label, namespace_id)``, so a cheap FK is
not available.

That was inert until now **because no delete path existed.  This file is the
delete path.**  Delete a node and leave its state row and the row becomes an
orphan keyed by a label that no longer exists — and the labels are
deterministic, so re-authoring the same ``design_id`` and ``device_ref``
produces the same label, lands on the orphan through ``ON CONFLICT DO UPDATE``
and **silently inherits its status**.  A device permanently deleted while
``'planned'`` would come back already declared, which is the exact distinction
migration 061's three-state model exists to protect.  An auditor reproduced
this on 67g.

:func:`do_retire_planned` therefore deletes the state row, the geometry row and
the capability row in the **same transaction** as the node and its edges.  The
resurrect scenario is gated end to end by
``tests/test_system_design_retire.py::TestResurrection``.

PORTS GO WITH THEIR DEVICE, FOR THE SAME REASON
-------------------------------------------------
A ``PORT`` node has no state row but it does have capability and geometry rows,
and its label ``PORT:<DESIGN>:<DEVICE_REF>:<PORT_REF>`` is just as
deterministic.  Deleting a DEVICE and leaving its ports behind leaves
unreachable nodes whose ``has_port`` edge is gone, and re-authoring the device
re-creates ports that inherit the orphans' capability rows — the same defect
family one level down.  The permanent path therefore expands each DEVICE to its
ports through the ``has_port`` edges **before** deleting those edges.

WHAT ISOLATES TENANTS HERE
----------------------------
Every statement below carries an explicit ``namespace_id`` predicate.  That is
the boundary, not ``FORCE ROW LEVEL SECURITY``: the pools that serve requests
are owner pools and bypass FORCE RLS, so a colliding node label in another
tenant must be excluded by the KEY, not by a policy that is not being enforced
on this connection.  Gated by
``tests/test_system_design_retire.py::TestOwnerPoolTenantIsolation``, whose two
tenants collide on **every** identifier and differ only in content.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.system_design.retire")

#: This engine's name in the node-ownership registry.  Same value
#: ``devices.py`` writes with; ``assert_owner`` is deny-by-default, so a
#: namespace with no registry row refuses the delete rather than allowing it.
_SYSTEM_DESIGN_ENGINE: str = "system_design"

#: The ONE status this path will act on.  Not a list, not a set: widening it is
#: a decision that needs its own review, and a collection invites a future
#: reader to append ``'staged'`` to it in passing.
RETIRABLE_STATUS: str = "planned"

#: Node types that can be retired on their own.  PORT is absent on purpose (see
#: the module docstring), and so is DESIGN — the design's own label keys the
#: optimistic-concurrency version row in ``system_design_geometry``, and that
#: row is a different grain entirely.
RETIRABLE_NODE_TYPES: frozenset[str] = frozenset({"DEVICE", "RACK", "CABLE"})

#: Retired status **per node type**.  The vocabularies in migration 061's
#: composite CHECK are disjoint and RACK has no ``'decommissioning'``, so one
#: universal constant would be refused by the database on every rack.  The
#: legal values themselves still live only in the DDL; this maps a node type to
#: the one this path writes.
RETIRE_STATUS_BY_NODE_TYPE: dict[str, str] = {
    "DEVICE": "decommissioning",
    "CABLE": "decommissioning",
    "RACK": "deprecated",
}

#: Salience written on a retire.  Absolute, never a decay — a decay preserves
#: PostgreSQL ``numeric`` NaN, which sorts above every finite value.  ``0`` is
#: the floor this engine's own salience decay already clamps to.
RETIRED_SALIENCE: Decimal = Decimal(0)

#: The edge that ties a DEVICE to its PORTs.  Mirrors ``devices._PRED_HAS_PORT``
#: and is spelled here rather than imported so this module's delete does not
#: change shape the day the authoring module reorganises its privates.
_PRED_HAS_PORT: str = "has_port"

#: Denial reasons.  Machine-readable and stable: a caller decides what to do
#: next from these, not from the prose.
DENY_STATE_ROW_ABSENT: str = "state_row_absent"
DENY_STATUS_UNDECLARED: str = "status_undeclared"
DENY_STATUS_NOT_PLANNED: str = "status_not_planned"
DENY_NODE_ABSENT: str = "node_absent"

#: Human-readable expansion of each reason, for the error message.  Kept beside
#: the codes so the two cannot drift.
_DENY_MESSAGES: dict[str, str] = {
    DENY_STATE_ROW_ABSENT: (
        "no lifecycle state row exists for this node, so nobody has ever declared "
        "it planned (this is the normal state of every node authored before W16)"
    ),
    DENY_STATUS_UNDECLARED: (
        "a lifecycle state row exists but its status is NULL — data is held for "
        "this node, no lifecycle was declared"
    ),
    DENY_STATUS_NOT_PLANNED: (
        f"the node's declared status is not {RETIRABLE_STATUS!r}; this path acts "
        f"on {RETIRABLE_STATUS!r} and only {RETIRABLE_STATUS!r}"
    ),
    DENY_NODE_ABSENT: (
        "the node does not exist in the graph for this namespace (a state row "
        "without its node is an orphan and is not this tool's to act on)"
    ),
}


class RetireDeniedError(Exception):
    """One or more named nodes are not in a retirable state.

    **Its own class, deliberately not a ``ValueError``**, for the same reason
    ``geometry.VersionConflictError`` is: neither ``@mcp_handler``'s generic
    "Invalid parameters" branch nor the REST routes' ``except ValueError``
    branch may swallow it and render it as a malformed argument.  The arguments
    were fine.  The *resource* was not in the state the request required.

    Both surfaces translate it as a **conflict**, not a validation failure and
    not an authorisation failure:

    * REST — **409**, "the request could not be completed due to a conflict
      with the current state of the target resource", which is literally this.
      Deliberately not **422** (the request is well formed) and deliberately
      not **403** (403 says *you* may not do this; this says *this node, in
      this state,* may not be done to — a caller with every permission in the
      system still gets this answer, and a caller who re-reads and retries
      after the node's status changes gets a different one).
    * MCP — :data:`RETIRE_DENIED_MCP_CODE`, in the JSON-RPC server-defined
      range, and not ``-32602``.

    ``denials`` is the per-node detail, one entry per refused label, so a
    caller can show a user exactly which nodes were refused and why without a
    second round trip.  It is deliberately the WHOLE list rather than the first
    failure: a canvas selecting forty devices needs all forty answers.

    Attributes:
        denials: ``[{"node_label", "reason", "status"}, ...]`` — ``status`` is
            the node's declared status where there was one, else ``None``.
    """

    reason: str = "retire_denied"

    def __init__(self, denials: list[dict[str, Any]]) -> None:
        self.denials = denials
        detail = "; ".join(
            f"{d['node_label']}: {_DENY_MESSAGES.get(str(d.get('reason')), str(d.get('reason')))}"
            for d in denials
        )
        super().__init__(
            f"{len(denials)} node(s) are not retirable and the whole request was "
            f"refused (nothing was changed): {detail}"
        )


def node_type_of_label(label: str) -> str | None:
    """Return the node type encoded in *label*'s prefix, or ``None``.

    Every label this engine writes is built by ``devices.device_label`` /
    ``port_label`` / ``rack_label`` / ``cable_label``, each of which emits a
    fixed upper-case type prefix followed by ``':'``.  Reading the prefix back
    is therefore exact rather than a heuristic — and it is the only way to
    learn a node's type without a round trip, which matters because the type
    decides both the ownership assertion and the retired status.

    ``None`` for anything with no ``':'``, so a caller that sends a bare string
    is refused rather than silently treated as some default type.
    """
    prefix, sep, _rest = label.partition(":")
    if not sep:
        return None
    return prefix


def design_of_label(label: str) -> str | None:
    """Return the design id encoded in *label*, or ``None`` when there is none.

    All four label shapes put the design id in the second colon-separated
    segment: ``DEVICE:<DESIGN_ID>:<DEVICE_REF>``,
    ``PORT:<DESIGN_ID>:<DEVICE_REF>:<PORT_REF>``, ``RACK:<DESIGN_ID>:<REF>``,
    ``CABLE:<DESIGN_ID>:<REF>``.  Reading it back is what lets this module
    refuse a well-formed label belonging to a *different* design, which is the
    one argument-level attack a caller can mount against a delete that takes
    labels: labels are guessable, so "you may only name nodes in the design you
    named" has to be enforced rather than assumed.
    """
    parts = label.split(":")
    if len(parts) < 3:
        return None
    return parts[1]


def _normalise_labels(node_labels: Any) -> list[str]:
    """Validate and de-duplicate the caller's label list.

    De-duplication is not cosmetic: the counts this function's caller reports
    are per DISTINCT label, and a payload naming the same device twice must not
    read as two deletions.  Order is preserved so the denial list a caller gets
    back matches the order they asked in.

    Raises:
        ValueError: not a list, empty, or holding a non-string / blank entry.
    """
    if not isinstance(node_labels, list):
        raise ValueError("node_labels is required and must be a list")
    if not node_labels:
        raise ValueError("node_labels must name at least one node")
    seen: set[str] = set()
    out: list[str] = []
    for raw in node_labels:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("every entry in node_labels must be a non-blank string")
        label = raw.strip()
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def _validate_label_shape(label: str, design_id: str) -> str:
    """Return *label*'s node type, refusing anything this path may not touch.

    Three refusals, all argument-level and all reported as ``ValueError`` (so
    ``-32602`` / 422) rather than as a denial: a denial says "this node is not
    in a retirable state", which is a fact about the graph, while these say
    "this is not a thing you may name here", which is a fact about the request.

    Raises:
        ValueError: unparseable label, a node type outside
            :data:`RETIRABLE_NODE_TYPES` (PORT and DESIGN included), or a label
            belonging to some other design.
    """
    node_type = node_type_of_label(label)
    if node_type is None:
        raise ValueError(f"{label!r} is not a node label (expected '<TYPE>:<DESIGN_ID>:...')")
    if node_type not in RETIRABLE_NODE_TYPES:
        raise ValueError(
            f"{label!r} is a {node_type} node and cannot be retired directly; "
            f"only {', '.join(sorted(RETIRABLE_NODE_TYPES))} carry a lifecycle status "
            f"(a PORT is removed with its device on the permanent path)"
        )
    label_design = design_of_label(label)
    if label_design is None or label_design != design_id.upper():
        raise ValueError(
            f"{label!r} does not belong to design {design_id!r}; a label may only be "
            f"retired through the design it belongs to"
        )
    return node_type


async def _fetch_state(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    labels: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{node_label: {"node_type", "status"}}`` for the labels that HAVE a row.

    A label missing from the result is a label with no state row, which is the
    deny-on-absence case — so this function must never invent an entry for a
    label it did not find.  The explicit ``namespace_id`` predicate is the
    tenant boundary (owner pools bypass FORCE RLS).
    """
    rows = await conn.fetch(
        """
        SELECT node_label, node_type, status
        FROM system_design_node_state
        WHERE namespace_id = $1::uuid
          AND node_label = ANY($2::text[])
        """,
        str(ns_uuid),
        labels,
    )
    return {r["node_label"]: {"node_type": r["node_type"], "status": r["status"]} for r in rows}


async def _fetch_existing_nodes(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    labels: list[str],
) -> set[str]:
    """Return the subset of *labels* that exist in ``kg_nodes`` for this tenant."""
    rows = await conn.fetch(
        """
        SELECT label
        FROM kg_nodes
        WHERE namespace_id = $1::uuid
          AND label = ANY($2::text[])
        """,
        str(ns_uuid),
        labels,
    )
    return {r["label"] for r in rows}


async def _port_labels_of(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    device_labels: list[str],
) -> list[str]:
    """Return the PORT labels reachable from *device_labels* via ``has_port``.

    The EDGE is the authority, not the label formula.  Rebuilding
    ``PORT:<DESIGN>:<DEVICE_REF>:<PORT_REF>`` here would need the port refs,
    which this module does not have and must not guess; the edge is what the
    authoring path actually wrote and is what the read surface walks.

    Read **before** the edges are deleted — after, this returns nothing and the
    ports are orphaned in silence.  Ordering the two statements is the whole
    correctness argument, so they are not separable.
    """
    if not device_labels:
        return []
    rows = await conn.fetch(
        """
        SELECT object_label
        FROM kg_edges
        WHERE namespace_id = $1::uuid
          AND predicate = $2
          AND subject_label = ANY($3::text[])
        """,
        str(ns_uuid),
        _PRED_HAS_PORT,
        device_labels,
    )
    return sorted({r["object_label"] for r in rows})


async def do_retire_planned(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    design_id: str,
    node_labels: list[Any],
    permanent: bool = False,
    actor: str | None = None,
) -> dict[str, Any]:
    """Retire — or, with ``permanent=True``, genuinely delete — planned nodes.

    🔴 **The default is a SOFT RETIRE.  Despite the tool name
    ``system_design_delete_planned`` and the route
    ``DELETE /api/system-design/planned``, nothing is removed unless the caller
    passes ``permanent=True``.**  Both names are Copper's published contract and
    are not adjustable; see the module docstring for why the mismatch is stated
    rather than resolved.

    Runs entirely on the caller's *conn*, inside the caller's transaction, and
    never commits or rolls back for itself.  That is what makes "a mid-call
    failure leaves nothing deleted" true by construction rather than by
    ordering: every statement below is in one transaction with the caller's
    ``bump_design_version`` and its audit event.

    Guards (all fail closed, and all evaluated before anything is written):
        * every label must parse, name a DEVICE / RACK / CABLE, and belong to
          *design_id* — otherwise ``ValueError``;
        * this engine must own the node type in the ownership registry, which
          is deny-by-default — otherwise ``OwnershipError``;
        * each node must exist, must have a state row, that row's ``status``
          must not be NULL, and it must be exactly
          :data:`RETIRABLE_STATUS` — otherwise :class:`RetireDeniedError`;
        * ``actor`` must be supplied when *permanent* — otherwise
          ``ValueError``.

    **One denied label denies the whole call.**  See the module docstring.

    Arguments:
        conn: Open asyncpg connection inside a transaction, namespace GUC set.
        namespace_id: The tenant.
        design_id: The design every label must belong to.
        node_labels: Canonical labels, as returned by
            ``system_design_get_topology``.  Duplicates collapse.
        permanent: ``False`` (default) soft-retires.  ``True`` deletes the
            nodes, their edges, their PORT children, and every side-table row
            keyed by those labels — see D12 in the module docstring for why the
            side tables are not optional.
        actor: The human's UPN.  **Mandatory when** *permanent*.

    Returns:
        ``{"permanent": bool, "retired": [...], "removed": {...} | None}``.

        ``retired`` holds one entry per node — ``{"node_label", "node_type",
        "from", "to"}`` — on both paths.  On the permanent path ``to`` is
        ``None``: there is no status afterwards because there is no row.

        ``removed`` is ``None`` on the soft path and, on the permanent path,
        the row counts this call actually deleted: ``{"nodes", "edges",
        "capabilities", "geometry", "state", "ports"}``.  They are counts of
        rows removed, not of labels requested, so a caller can tell a node that
        had a geometry row from one that never did.

    Raises:
        ValueError: a malformed argument, a label this path may not name, or a
            missing ``actor`` on the permanent path.
        OwnershipError: this engine does not own the node type here.
        RetireDeniedError: one or more nodes are not in a retirable state.
            Nothing was written.
    """
    ns_uuid = namespace_id if isinstance(namespace_id, UUID) else UUID(str(namespace_id))
    if not isinstance(design_id, str) or not design_id.strip():
        raise ValueError("design_id is required")

    labels = _normalise_labels(node_labels)
    types_by_label: dict[str, str] = {
        label: _validate_label_shape(label, design_id) for label in labels
    }

    # 🔴 The one place Rev 2 §1's "an omitted actor is fine" is refused.  It is
    # checked BEFORE any read so a caller who forgot it cannot learn from the
    # error which of their nodes were retirable — and, more to the point, so
    # the refusal cannot be reordered behind a write by a later edit.
    if permanent and not (isinstance(actor, str) and actor.strip()):
        raise ValueError(
            "actor is required when permanent=true: a permanent delete must be "
            "attributable to a human, and an unattributable one fails closed"
        )

    # Deny-by-default ownership, per distinct node type, exactly as the
    # authoring path asserts before it writes.  A namespace with no registry
    # row refuses.
    for node_type in sorted(set(types_by_label.values())):
        await assert_owner(conn, ns_uuid, node_type, _SYSTEM_DESIGN_ENGINE)

    state = await _fetch_state(conn, ns_uuid, labels)
    existing = await _fetch_existing_nodes(conn, ns_uuid, labels)

    denials: list[dict[str, Any]] = []
    for label in labels:
        row = state.get(label)
        if label not in existing:
            denials.append({"node_label": label, "reason": DENY_NODE_ABSENT, "status": None})
            continue
        if row is None:
            denials.append({"node_label": label, "reason": DENY_STATE_ROW_ABSENT, "status": None})
            continue
        status = row["status"]
        if status is None:
            denials.append({"node_label": label, "reason": DENY_STATUS_UNDECLARED, "status": None})
            continue
        if status != RETIRABLE_STATUS:
            denials.append(
                {"node_label": label, "reason": DENY_STATUS_NOT_PLANNED, "status": status}
            )
    if denials:
        raise RetireDeniedError(denials)

    if permanent:
        return await _delete_permanently(conn, ns_uuid, labels, types_by_label)
    return await _retire_softly(conn, ns_uuid, labels, types_by_label)


async def _retire_softly(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    labels: list[str],
    types_by_label: dict[str, str],
) -> dict[str, Any]:
    """Write the retired status and floor the salience.  Nothing is removed.

    The ``status = $4`` predicate in the WHERE clause is a second, independent
    read of the guard :func:`do_retire_planned` already applied: it re-checks
    against the row as it stands at UPDATE time, so a concurrent writer that
    flipped the node to ``'active'`` between the guard's SELECT and this
    statement loses rather than being overwritten.  A caller holding the
    design's row lock (which every call through the two adapters does) cannot
    hit that window, but the core is callable directly and this is the whole
    file's failure mode, so it is checked twice.

    A row that does not match is reported rather than swallowed: the UPDATE
    returns nothing for it, which surfaces as a missing entry and an explicit
    ``RuntimeError``, because a retire that silently did nothing is the one
    outcome a caller must never be told was a success.

    The status written is per node type — RACK has no ``'decommissioning'``.
    Should a state row's ``node_type`` ever disagree with its label prefix, the
    value chosen here belongs to the *label's* type and migration 061's
    composite CHECK refuses it against the *row's* type: both directions of
    that disagreement fail closed at the database rather than silently writing
    a status from the wrong vocabulary.
    """
    retired: list[dict[str, Any]] = []
    for label in labels:
        node_type = types_by_label[label]
        target = RETIRE_STATUS_BY_NODE_TYPE[node_type]
        row = await conn.fetchrow(
            """
            UPDATE system_design_node_state
               SET status     = $4,
                   salience   = $5,
                   updated_at = NOW()
             WHERE namespace_id = $1::uuid
               AND node_label   = $2
               AND status       = $3
            RETURNING status
            """,
            str(ns_uuid),
            label,
            RETIRABLE_STATUS,
            target,
            RETIRED_SALIENCE,
        )
        if row is None:
            raise RuntimeError(
                f"{label} was retirable a moment ago and is not now — its status changed "
                f"under this transaction; nothing has been retired"
            )
        retired.append(
            {
                "node_label": label,
                "node_type": node_type,
                "from": RETIRABLE_STATUS,
                "to": row["status"],
            }
        )
        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=node_type,
            op="retired",
            node_id=label,
        )
    return {"permanent": False, "retired": retired, "removed": None}


async def _delete_permanently(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    labels: list[str],
    types_by_label: dict[str, str],
) -> dict[str, Any]:
    """Delete the nodes, their edges, their ports and every side-table row.

    🔴 **All five DELETEs are obligatory and none is an optimisation.**  No
    foreign key ties a side-table row to its node (see D12 in the module
    docstring), so a row left behind is an orphan that a later re-author of the
    same deterministic label inherits through ``ON CONFLICT DO UPDATE``.
    Dropping any one of the state / geometry / capability statements below
    re-opens that, and each has its own RED test.

    Order:

    1. **Expand DEVICEs to their PORTs first**, through the ``has_port`` edges,
       because step 4 deletes those edges and after that the ports are
       unreachable.
    2. Side tables, keyed by ``(namespace_id, node_label)``.
    3. — with the geometry statement carrying ``version IS NULL``, a GRAIN
       GUARD: ``system_design_geometry`` holds node-geometry rows *and* the one
       per-design optimistic-concurrency version row, distinguished only by
       ``version``.  Deleting the version row would silently reset the design's
       token to 0 and hand every stale client a free win.  No DESIGN label can
       reach here (:func:`_validate_label_shape` refuses it), so this is
       defence-in-depth of the same kind as ``bump_design_version``'s own grain
       guard — unreachable through the surfaces today, load-bearing the day
       anything else writes a DESIGN-labelled geometry row.
    4. Edges in **both directions**: a node is the subject of its ``has_port``
       and ``mounted_in`` edges and the object of the design's ``contains`` /
       ``has_rack`` edges, so a subject-only delete leaves dangling halves that
       the read surface still walks.
    5. The nodes themselves.

    Every statement carries the explicit ``namespace_id`` predicate; owner pools
    bypass FORCE RLS, so that predicate is the tenant boundary.
    """
    device_labels = [label for label in labels if types_by_label[label] == "DEVICE"]
    port_labels = await _port_labels_of(conn, ns_uuid, device_labels)
    all_labels = labels + [label for label in port_labels if label not in set(labels)]
    ns = str(ns_uuid)

    state_deleted = _affected(
        await conn.execute(
            """
            DELETE FROM system_design_node_state
             WHERE namespace_id = $1::uuid
               AND node_label = ANY($2::text[])
            """,
            ns,
            all_labels,
        )
    )
    geometry_deleted = _affected(
        await conn.execute(
            """
            DELETE FROM system_design_geometry
             WHERE namespace_id = $1::uuid
               AND node_label = ANY($2::text[])
               AND version IS NULL
            """,
            ns,
            all_labels,
        )
    )
    capabilities_deleted = _affected(
        await conn.execute(
            """
            DELETE FROM system_design_device_capabilities
             WHERE namespace_id = $1::uuid
               AND node_label = ANY($2::text[])
            """,
            ns,
            all_labels,
        )
    )
    edges_deleted = _affected(
        await conn.execute(
            """
            DELETE FROM kg_edges
             WHERE namespace_id = $1::uuid
               AND (subject_label = ANY($2::text[]) OR object_label = ANY($2::text[]))
            """,
            ns,
            all_labels,
        )
    )
    nodes_deleted = _affected(
        await conn.execute(
            """
            DELETE FROM kg_nodes
             WHERE namespace_id = $1::uuid
               AND label = ANY($2::text[])
            """,
            ns,
            all_labels,
        )
    )

    retired: list[dict[str, Any]] = [
        {
            "node_label": label,
            "node_type": types_by_label[label],
            "from": RETIRABLE_STATUS,
            # No status afterwards: there is no row afterwards.
            "to": None,
        }
        for label in labels
    ]
    for label in all_labels:
        await emit_graph_write(
            conn,
            namespace_id=ns_uuid,
            node_type=types_by_label.get(label, "PORT"),
            op="deleted",
            node_id=label,
        )

    return {
        "permanent": True,
        "retired": retired,
        "removed": {
            "nodes": nodes_deleted,
            "edges": edges_deleted,
            "capabilities": capabilities_deleted,
            "geometry": geometry_deleted,
            "state": state_deleted,
            "ports": len(port_labels),
        },
    }


def _affected(status: str) -> int:
    """Row count from an asyncpg command tag (``'DELETE 3'``).

    Reported rather than assumed: the counts in ``removed`` are what the
    database actually removed, so a caller can tell "this node had no geometry
    row" from "the geometry statement did not run".  A tag this cannot parse
    yields ``0`` rather than raising — an unreadable count must not fail a
    delete that already happened.
    """
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):  # pragma: no cover - asyncpg always tags
        return 0
