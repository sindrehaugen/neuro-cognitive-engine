"""
nce/vertical_modules/system_design/read.py
==========================================
Domain-core **read** layer for the System Design vertical module (M6.W13a).

This module owns the *single* query set for reading System Design topology out
of the graph.  It was created by promoting the four module-private ``_fetch_*``
readers out of ``validation_queries.py`` (which now imports them from here —
one query set, never a copy) and adding the public ``do_get_topology`` composer
that puts the first row of Copper's tool contract on the wire.

Public entry-points
-------------------
``do_get_topology(engine, params)``
    Read a DESIGN's full topology: the DESIGN node, its functional-location
    tree, its devices (each with capabilities and ports), its **racks** (each
    with capabilities), its cables, the edges connecting them, the **canvas
    geometry** of every in-scope node, the per-node **lifecycle state**, and the
    design's **version**.  Optionally narrows the three state-bearing buckets to
    a set of lifecycle ``statuses``.

Per-node lifecycle state, and the absence that must survive (M6.W16b)
---------------------------------------------------------------------
``system_design_node_state`` (migration 061) holds ``status``/``revision``/
``salience`` for a DEVICE, a RACK or a CABLE.  Three facts are distinguishable
there and **all three have to survive this reader**, because W17's retirement
guard denies on the first two:

* **no row**      — nothing was ever declared about this node.  Every node
  authored before W16 is here, and 67g's writer keeps it here: it creates a row
  only for a node genuinely new to the authoring call, or one that carried an
  explicit lifecycle key.  An ordinary canvas save, a geometry-only drag and
  67f's re-author data-fix all leave a pre-existing node with no row.
* **row, ``status`` NULL** — data is held (a revision, a salience); nobody
  declared a lifecycle.
* **row, ``status`` set** — a lifecycle was declared.

**Nothing in this module may COALESCE the first two into ``'planned'``**, or any
other value.  A missing row is reported by the node's label being *absent* from
the ``state`` map; a NULL status is reported as ``null`` under a present key.
Coalescing either would hand every legacy as-built device a lifecycle it never
had and make it look retirable — the one-way door this wave was written around.

Re-exported readers (consumed by ``validation_queries.py``)
-----------------------------------------------------------
``_fetch_port_directions``, ``_fetch_connections_and_capabilities``,
``_fetch_device_capabilities``, ``_fetch_port_capabilities`` — moved here
verbatim in behaviour.  They keep their original private names so that
``validation_queries.py`` (and anything importing them from there) is a
one-line import change with identical semantics.

Namespace scoping (WORM/RLS invariant, wave rule 7)
----------------------------------------------------
**Every** query in this module carries an explicit ``namespace_id = $n::uuid``
predicate on **every** joined relation.  This is deliberate: owner pools bypass
``FORCE ROW LEVEL SECURITY``, so RLS alone does not isolate tenants on the
connection this code actually runs on.

The tenant boundary is specifically the **leaf** predicates — in
``_fetch_nodes_by_labels`` (which from M6.W16b carries **two**: its own
``kg_nodes`` pin and the ``namespace_id`` pin inside the ``statuses`` EXISTS
sub-query), ``_fetch_edges_within``, ``_fetch_capabilities_by_labels`` and
``_fetch_node_state_by_labels`` here, plus ``fetch_geometry_by_labels`` and
``fetch_design_version`` in ``geometry.py`` (M6.W14), which this composer calls
on the same connection.  Each is individually load-bearing and each is
individually gated — ``TestOwnerPoolIsolation`` in
``tests/test_system_design_author_surface.py`` for the first three, the
per-predicate mutation table in ``tests/test_system_design_geometry.py`` for
the geometry pair, and ``TestOwnerPoolIsolation`` in
``tests/test_system_design_status_filter.py`` for the two W16b ones; drop any
one and a tenant reads another tenant's rows.  **The ``statuses`` filter is a
SQL predicate for this reason and never a Python-side discard of already-fetched
rows**: on this connection the predicate *is* the boundary, so a filter applied
after the fetch would be both a wasted round trip and a tenancy smell.  The
scope-walk pair in
``_fetch_design_scope_labels`` is a scope/cost bound and a backstop, not part of
that boundary — see its docstring.  An earlier live run was
burned by a ``LIMIT 1`` label-to-namespace subquery that picked an arbitrary
namespace for a design label shared across tenants (ML.md, Batch 67).  That
subquery is never to be reintroduced — the namespace is threaded in as a bound
parameter instead.

LIKE-wildcard safety
--------------------
This module builds **no** ``LIKE`` patterns at all — every label match is an
equality or ``= ANY($n::text[])`` comparison against a bound parameter.  The
``_``/``%`` wildcard bug class documented in ``cascade.py`` therefore cannot
arise here, and no label-alphabet argument is needed.

Design invariants (uncle-bob-craft)
------------------------------------
- Dependencies point **inward**: no web / HTTP / admin / MCP imports.  The MCP
  handler and the REST route depend on this module, never the reverse.
- One query per function, one job per function; composition is pure Python over
  already-fetched rows.
- Read-only.  No mutations, no ``event_log`` writes.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.system_design.geometry import (
    fetch_design_version,
    fetch_geometry_by_labels,
)

log = logging.getLogger("nce.vertical_modules.system_design.read")

# ---------------------------------------------------------------------------
# Structural predicates that define "inside this design".
#
# Mirrors the edge topology authored by graph.py and devices.py:
#   DESIGN              -[contains]->   DEVICE | DESIGN_LINE | FUNCTIONAL_LOCATION
#   DESIGN              -[has_rack]->   RACK
#   DEVICE              -[has_port]->   PORT
#   DEVICE              -[mounted_in]-> RACK
#   PORT                -[uses_cable]-> CABLE
#   FUNCTIONAL_LOCATION -[parent_of]->  FUNCTIONAL_LOCATION
#   FUNCTIONAL_LOCATION -[needs]->      DESIGN_LINE
#
# ``connected_to`` is deliberately NOT a scope predicate: it is a signal path
# between two ports that are already in scope via has_port, and traversing it
# would not widen the design.  Cable-path tracing (front/rear traversal per
# NetBox semantics) is explicitly out of scope for this wave (Rev 2 §8).
# ---------------------------------------------------------------------------
_SCOPE_PREDICATES: tuple[str, ...] = (
    "contains",
    "has_rack",
    "has_port",
    "mounted_in",
    "uses_cable",
    "parent_of",
    "needs",
)

# Entity types this reader projects into their own result buckets.
_ENTITY_DESIGN: str = "DESIGN"
_ENTITY_DEVICE: str = "DEVICE"
_ENTITY_PORT: str = "PORT"
_ENTITY_RACK: str = "RACK"
_ENTITY_CABLE: str = "CABLE"
_ENTITY_FUNCTIONAL_LOCATION: str = "FUNCTIONAL_LOCATION"

_PRED_HAS_PORT: str = "has_port"

# The entity types that can carry a row in ``system_design_node_state``, and
# therefore the ONLY types the ``statuses`` filter narrows (M6.W16b).
#
# Built from this module's own entity constants — no new string literals — and
# it is a list of node TYPES, not of status values.  The status *vocabulary*
# stays where migration 061's composite CHECK put it and is deliberately not
# copied into Python anywhere, here included: this module never validates a
# status value, it only matches it.
#
# Why the filter must skip everything else: DESIGN, FUNCTIONAL_LOCATION and
# PORT cannot hold a state row at all (the CHECK's ``ELSE FALSE`` refuses them
# structurally), so filtering them by status would return ``design: None`` for
# every filtered read — and ``design: None`` already means "this design does not
# exist in your namespace", a load-bearing isolation signal.  Two very different
# facts would then share one spelling.
_STATE_BEARING_ENTITY_TYPES: tuple[str, ...] = (
    _ENTITY_DEVICE,
    _ENTITY_RACK,
    _ENTITY_CABLE,
)

# Full capability projection — every column the Copper contract may surface.
# ``extra`` is included because Rev 2 §5 requires verbatim passthrough of the
# reserved ``copper.*`` port component-class keys.
_CAPABILITY_COLUMNS: tuple[str, ...] = (
    "signal_format",
    "signal_version",
    "port_direction",
    "poe_class",
    "poe_watts",
    "dante_rx_channels",
    "dante_tx_channels",
    "power_draw_watts",
    "heat_btu_hr",
    "redundancy_role",
    "device_category",
    "manufacturer",
    "model_number",
    "extra",
)

# Per-node lifecycle state projection (M6.W16b, migration 061).
#
# ``node_type`` is deliberately NOT projected: the node's ``entity_type``
# already reaches the caller on the node row itself, and a second spelling of
# the same fact is one more thing that can disagree with the first.
_NODE_STATE_COLUMNS: tuple[str, ...] = ("status", "revision", "salience")


def _design_label(design_id: str) -> str:
    """Canonical DESIGN label: ``DESIGN:<DESIGN_ID>`` (matches graph.py)."""
    return f"DESIGN:{design_id.upper()}"


# ---------------------------------------------------------------------------
# Fetch helpers — MOVED VERBATIM from validation_queries.py (M6.W13a).
#
# These four are the validation read path.  validation_queries.py imports them
# from here; there is exactly one copy of each query in the codebase.
# ---------------------------------------------------------------------------


async def _fetch_port_directions(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> tuple[list[str], set[str]]:
    """Return (input_port_labels, connected_to_targets).

    Fetches:
    - All PORT nodes reachable from the DESIGN via DEVICE -[has_port]-> PORT,
      filtered to those with port_direction='input'.
    - All target labels of ``connected_to`` edges within the design's scope.

    Both queries are explicitly scoped to ``ns_uuid`` so that identical design
    labels in different namespaces (e.g. multiple ``make_namespace()`` test runs)
    do not bleed into each other.  This avoids the ``LIMIT 1`` subquery that
    previously picked an arbitrary namespace for the same design label.
    """
    # Input ports: PORT nodes with direction='input' under this DESIGN,
    # all rows pinned to ns_uuid.
    input_rows = await conn.fetch(
        """
        SELECT kn.label
        FROM kg_nodes kn
        JOIN kg_edges dev_port
            ON dev_port.object_label = kn.label
            AND dev_port.predicate = 'has_port'
            AND dev_port.namespace_id = $2::uuid
        JOIN kg_edges design_dev
            ON design_dev.object_label = dev_port.subject_label
            AND design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        JOIN system_design_device_capabilities sddc
            ON sddc.node_label = kn.label
            AND sddc.namespace_id = $2::uuid
        WHERE kn.entity_type = 'PORT'
          AND kn.namespace_id = $2::uuid
          AND sddc.port_direction = 'input'
        """,
        design_label,
        ns_uuid,
    )
    input_port_labels = [r["label"] for r in input_rows]

    # connected_to targets within this namespace (any PORT that receives a signal).
    # Directly scoped — no label→namespace subquery needed.
    target_rows = await conn.fetch(
        """
        SELECT DISTINCT object_label
        FROM kg_edges
        WHERE predicate = 'connected_to'
          AND namespace_id = $1::uuid
        """,
        ns_uuid,
    )
    connected_to_targets = {r["object_label"] for r in target_rows}

    return input_port_labels, connected_to_targets


async def _fetch_connections_and_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (connections, capability_by_label) for the PORT format check.

    connections: list of {from_port, to_port}
    capability_by_label: {label: {signal_format, signal_version, dante_*}}

    All queries are explicitly scoped to ``ns_uuid``.
    """
    cnx_rows = await conn.fetch(
        """
        SELECT ke.subject_label AS from_port, ke.object_label AS to_port
        FROM kg_edges ke
        JOIN kg_edges design_dev
            ON design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        JOIN kg_edges dev_port
            ON dev_port.predicate = 'has_port'
            AND dev_port.subject_label = design_dev.object_label
            AND dev_port.namespace_id = $2::uuid
        WHERE ke.predicate = 'connected_to'
          AND ke.subject_label = dev_port.object_label
          AND ke.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    connections = [{"from_port": r["from_port"], "to_port": r["to_port"]} for r in cnx_rows]

    # Collect distinct port labels from this design.
    port_labels: set[str] = set()
    for c in connections:
        port_labels.add(c["from_port"])
        port_labels.add(c["to_port"])

    cap_by_label: dict[str, dict[str, Any]] = {}
    if port_labels:
        cap_rows = await conn.fetch(
            """
            SELECT node_label, signal_format, signal_version,
                   dante_rx_channels, dante_tx_channels
            FROM system_design_device_capabilities
            WHERE node_label = ANY($1::text[])
              AND namespace_id = $2::uuid
            """,
            list(port_labels),
            ns_uuid,
        )
        for r in cap_rows:
            cap_by_label[r["node_label"]] = dict(r)

    return connections, cap_by_label


async def _fetch_device_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> list[dict[str, Any]]:
    """Fetch capability rows for all DEVICE nodes under this DESIGN.

    Explicitly scoped to ``ns_uuid`` so parallel test namespaces do not bleed.
    """
    rows = await conn.fetch(
        """
        SELECT sddc.node_label, sddc.power_draw_watts, sddc.heat_btu_hr,
               sddc.redundancy_role, sddc.device_category
        FROM system_design_device_capabilities sddc
        JOIN kg_edges ke
            ON ke.object_label = sddc.node_label
            AND ke.predicate = 'contains'
            AND ke.subject_label = $1
            AND ke.namespace_id = $2::uuid
        JOIN kg_nodes kn
            ON kn.label = sddc.node_label
            AND kn.entity_type = 'DEVICE'
            AND kn.namespace_id = $2::uuid
        WHERE sddc.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    return [dict(r) for r in rows]


async def _fetch_port_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> list[dict[str, Any]]:
    """Fetch capability rows for all PORT nodes under this DESIGN.

    Explicitly scoped to ``ns_uuid`` so parallel test namespaces do not bleed.
    """
    rows = await conn.fetch(
        """
        SELECT sddc.node_label, sddc.signal_format, sddc.port_direction
        FROM system_design_device_capabilities sddc
        JOIN kg_nodes kn
            ON kn.label = sddc.node_label
            AND kn.entity_type = 'PORT'
            AND kn.namespace_id = $2::uuid
        JOIN kg_edges dev_port
            ON dev_port.object_label = kn.label
            AND dev_port.predicate = 'has_port'
            AND dev_port.namespace_id = $2::uuid
        JOIN kg_edges design_dev
            ON design_dev.object_label = dev_port.subject_label
            AND design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        WHERE sddc.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Topology readers — the do_get_topology query set (M6.W13a).
# ---------------------------------------------------------------------------


async def _fetch_design_scope_labels(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> list[str]:
    """Return every node label inside *design_label*, including the DESIGN itself.

    Walks the structural predicates in ``_SCOPE_PREDICATES`` outward from the
    DESIGN node.  ``UNION`` (not ``UNION ALL``) both de-duplicates and makes the
    recursion terminate on a cyclic graph.

    Both the anchor and the recursive term pin ``namespace_id`` on every
    relation, so a design label that exists in two namespaces yields only the
    caller's own nodes.

    **These two predicates are a scope and cost bound, NOT the tenant
    boundary.**  Neutering either one — or both together — leaves the whole
    ``system_design`` suite green, because every leaf query re-filters by
    namespace on its own.  Do not read a passing isolation test as protection
    for this predicate: the tenant boundary is the three leaf predicates, in
    ``_fetch_nodes_by_labels``, ``_fetch_edges_within`` and
    ``_fetch_capabilities_by_labels``, and those three are each individually
    gated by ``TestOwnerPoolIsolation``.

    Keep these two anyway.  With both neutered the walk really does pull in
    labels belonging to another tenant's larger design, so they bound the label
    set this function hands to the leaf queries (correctness of *scope*, and the
    size of the ``ANY($1::text[])`` lists), and they are the backstop for any
    future leaf query written without a namespace predicate of its own.
    """
    rows = await conn.fetch(
        """
        WITH RECURSIVE design_scope(label) AS (
            SELECT kn.label
            FROM kg_nodes kn
            WHERE kn.label = $1
              AND kn.namespace_id = $2::uuid
        UNION
            SELECT ke.object_label
            FROM kg_edges ke
            JOIN design_scope ds
                ON ke.subject_label = ds.label
            WHERE ke.namespace_id = $2::uuid
              AND ke.predicate = ANY($3::text[])
        )
        SELECT label FROM design_scope
        """,
        design_label,
        ns_uuid,
        list(_SCOPE_PREDICATES),
    )
    return [r["label"] for r in rows]


async def _fetch_nodes_by_labels(
    conn: Any,
    ns_uuid: Any,
    labels: list[str],
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch ``(label, entity_type)`` for *labels*, pinned to ``ns_uuid``.

    **The ``statuses`` filter lives here, in SQL (M6.W16b).**  It is one
    predicate on one query rather than a discard over already-fetched rows,
    because on this connection — an owner pool that bypasses ``FORCE ROW LEVEL
    SECURITY`` — the predicate *is* the tenant boundary, and a filter that runs
    in Python has already read the rows it claims to be excluding.

    ``statuses=None`` (and, per :func:`_normalise_statuses`, an empty list) is
    **no filter at all**: the ``$3::text[] IS NULL`` arm below short-circuits the
    whole disjunction, so the unfiltered read is byte-for-byte the query W13a
    shipped plus a constant-folded ``TRUE``.

    Three properties of the filtered arm, each deliberate:

    * **A node with NO state row does not match any filter.**  ``EXISTS`` over
      an empty sub-query is ``FALSE``.  Absence is not silently read as the
      default status — see the module docstring's one-way door.
    * **A node whose ``status`` IS NULL does not match either.**  ``NULL = ANY
      (...)`` is ``NULL``, so the ``EXISTS`` row is not produced.  "we hold a
      revision for this node" is not a lifecycle declaration and must not answer
      a lifecycle question.
    * **Only ``_STATE_BEARING_ENTITY_TYPES`` are narrowed.**  DESIGN, PORT and
      FUNCTIONAL_LOCATION are structure; see that constant for why filtering
      them would collide with the "design does not exist here" signal.

    The sub-query carries its **own** ``namespace_id`` pin.  Without it a
    tenant's device is admitted to (or excluded from) the result on the strength
    of *another tenant's* status for the same label — every label collides
    across tenants by construction — which is a cross-tenant read even though
    the foreign row's contents never leave the database.

    Status **values are matched verbatim**: no case folding, no trimming, and no
    vocabulary check.  The vocabulary is migration 061's composite CHECK and
    exists in exactly one place; a copy here is how a read path and its
    constraint drift apart while both suites stay green.  An unknown status
    therefore matches nothing, which is the correct answer to "give me the nodes
    that are 'NONSENSE'".
    """
    if not labels:
        return []
    rows = await conn.fetch(
        """
        SELECT kn.label, kn.entity_type
        FROM kg_nodes kn
        WHERE kn.label = ANY($1::text[])
          AND kn.namespace_id = $2::uuid
          AND (
                $3::text[] IS NULL
             OR kn.entity_type <> ALL($4::text[])
             OR EXISTS (
                    SELECT 1
                    FROM system_design_node_state sdns
                    WHERE sdns.node_label = kn.label
                      AND sdns.namespace_id = $2::uuid
                      AND sdns.status = ANY($3::text[])
                )
          )
        """,
        labels,
        ns_uuid,
        statuses,
        list(_STATE_BEARING_ENTITY_TYPES),
    )
    return [{"label": r["label"], "entity_type": r["entity_type"]} for r in rows]


async def _fetch_edges_within(
    conn: Any,
    ns_uuid: Any,
    labels: list[str],
) -> list[dict[str, Any]]:
    """Fetch every edge whose subject is inside the design, pinned to ``ns_uuid``.

    Returns the contract shape ``{subject, predicate, object}``.  Unlike the
    scope walk this is not restricted to ``_SCOPE_PREDICATES``: ``connected_to``
    and any other edge hanging off an in-scope node is part of the topology the
    caller asked for.
    """
    if not labels:
        return []
    rows = await conn.fetch(
        """
        SELECT ke.subject_label, ke.predicate, ke.object_label
        FROM kg_edges ke
        WHERE ke.subject_label = ANY($1::text[])
          AND ke.namespace_id = $2::uuid
        ORDER BY ke.subject_label, ke.predicate, ke.object_label
        """,
        labels,
        ns_uuid,
    )
    return [
        {
            "subject": r["subject_label"],
            "predicate": r["predicate"],
            "object": r["object_label"],
        }
        for r in rows
    ]


def _json_native(value: Any) -> Any:
    """Coerce a driver-native scalar into a JSON-native one.

    The ``NUMERIC`` capability columns (``poe_watts``, ``power_draw_watts``,
    ``heat_btu_hr``) arrive as :class:`decimal.Decimal`, which neither
    ``json.dumps`` nor Starlette's ``JSONResponse`` can encode.  Converting
    **here, in the core** — rather than separately in each adapter — is what
    makes the MCP tool and the REST route return the *same* JSON type for the
    same field.  Encoding it per-adapter is how the two surfaces drift (one
    emitting ``65.0``, the other ``"65.0"``), which would be a silent contract
    break for Copper.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


def _decode_extra(raw: Any) -> Any:
    """Return the ``extra`` JSONB column as the value that was stored.

    asyncpg hands JSONB back as ``str`` unless a codec is registered, and none
    is registered on this pool.  Decoding here means the caller sees exactly the
    dict that ``do_author_device_topology`` wrote.

    **Rev 2 §5 — verbatim passthrough.**  The reserved ``copper.*`` keys
    (``copper.port_kind``, ``copper.rear_port``, ``copper.rear_position``) are
    NOT filtered, NOT validated, and carry no meaning in NCE.  NCE stores,
    Copper interprets.  ``JSONB`` does not preserve key order, so what is
    guaranteed is that every key and value survives unchanged — not the literal
    byte layout of the stored document.  A malformed blob is returned as-is rather than being
    "repaired" — silently rewriting a tenant's stored value would be worse than
    handing it back unchanged.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


async def _fetch_capabilities_by_labels(
    conn: Any,
    ns_uuid: Any,
    labels: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{node_label: full capability row}`` for *labels*, pinned to ``ns_uuid``.

    This is the **contract** projection: every capability column including
    ``extra``.  It is deliberately distinct from ``_fetch_device_capabilities``
    /``_fetch_port_capabilities``, which project the narrow column subsets the
    five validation checks consume and must keep their behaviour verbatim.
    """
    if not labels:
        return {}
    # The interpolated column list is a module constant (_CAPABILITY_COLUMNS),
    # never caller input; the two values are bound parameters.
    rows = await conn.fetch(
        f"""
        SELECT node_label, {", ".join(_CAPABILITY_COLUMNS)}
        FROM system_design_device_capabilities
        WHERE node_label = ANY($1::text[])
          AND namespace_id = $2::uuid
        """,
        labels,
        ns_uuid,
    )
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        cap = {key: _json_native(value) for key, value in row.items() if key != "node_label"}
        cap["extra"] = _decode_extra(row["extra"])
        by_label[row["node_label"]] = cap
    return by_label


async def _fetch_node_state_by_labels(
    conn: Any,
    ns_uuid: Any,
    labels: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{node_label: {status, revision, salience}}``, pinned to ``ns_uuid``.

    **A node with no row is ABSENT from the returned map** — it is not present
    with null members, and no member is ever defaulted.  That absence is the
    fact W17's retirement guard denies on, and every node authored before W16
    has it.  There is no ``COALESCE`` in this query and none may be added: see
    the module docstring.

    A row whose ``status`` is NULL comes back present, with ``status`` ``None``.
    That is the second of the three distinguishable states — "we hold data for
    this node, nobody declared a lifecycle" — and it is not the same fact as a
    missing row even though W17 denies on both.

    ``salience`` is ``NUMERIC`` and arrives as :class:`decimal.Decimal`; it goes
    through :func:`_json_native` here, in the core, for the same reason the
    capability NUMERICs do — so the MCP tool and the REST route cannot emit
    different JSON types for the same field.

    The ``namespace_id`` predicate is the tenant boundary.  Both tenants hold a
    row under the *same* ``node_label`` whenever their designs collide, so
    losing it lets one tenant's state silently overwrite the other's in the
    by-label dict.
    """
    if not labels:
        return {}
    # The interpolated column list is a module constant (_NODE_STATE_COLUMNS),
    # never caller input; the two values are bound parameters.
    rows = await conn.fetch(
        f"""
        SELECT node_label, {", ".join(_NODE_STATE_COLUMNS)}
        FROM system_design_node_state
        WHERE node_label = ANY($1::text[])
          AND namespace_id = $2::uuid
        """,
        labels,
        ns_uuid,
    )
    return {
        row["node_label"]: {
            key: _json_native(value) for key, value in row.items() if key != "node_label"
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Pure composition — no DB below this line.
# ---------------------------------------------------------------------------


def _build_devices(
    nodes_by_type: dict[str, list[dict[str, Any]]],
    edges: list[dict[str, Any]],
    capabilities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose the ``devices[]`` bucket: each device with its capabilities and ports.

    Pure function over already-fetched rows — no DB, no I/O.
    """
    ports_by_label = {p["label"]: p for p in nodes_by_type.get(_ENTITY_PORT, [])}

    ports_of_device: dict[str, list[str]] = {}
    for edge in edges:
        if edge["predicate"] == _PRED_HAS_PORT and edge["object"] in ports_by_label:
            ports_of_device.setdefault(edge["subject"], []).append(edge["object"])

    devices: list[dict[str, Any]] = []
    for device in sorted(nodes_by_type.get(_ENTITY_DEVICE, []), key=lambda n: n["label"]):
        port_labels = sorted(ports_of_device.get(device["label"], []))
        devices.append(
            {
                "node": device,
                "capabilities": capabilities.get(device["label"], {}),
                "ports": [
                    {
                        "node": ports_by_label[label],
                        "capabilities": capabilities.get(label, {}),
                    }
                    for label in port_labels
                ],
            }
        )
    return devices


def _build_racks(
    nodes_by_type: dict[str, list[dict[str, Any]]],
    capabilities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose the ``racks[]`` bucket: each rack with its capability row.

    **Debt D5, closed here (M6.W14).**  ``devices.py`` has written RACK nodes,
    ``DESIGN -[has_rack]-> RACK`` / ``DEVICE -[mounted_in]-> RACK`` edges and
    RACK capability rows since W12, and ``_SCOPE_PREDICATES`` has pulled RACK
    into scope and ``_group_nodes_by_type`` has bucketed it — but the result
    dict surfaced ``design``, ``functional_locations``, ``devices``, ``cables``
    and ``edges`` only, so the whole RACK bucket was dropped on the floor.  A
    rack was write-only: authored, stored, and unreadable through the contract.
    That is wider than the ledger's D5 wording ("capability rows are projected
    for DEVICE/PORT only") — the *node* was missing too, not merely its
    capabilities.

    Same ``{node, capabilities}`` shape ``devices`` uses (minus ``ports`` — a
    rack has none; devices mount *into* it via ``mounted_in``, which is already
    in ``edges``), and the same label sort as every other bucket.

    Pure function over already-fetched rows — no DB, no I/O.
    """
    return [
        {"node": rack, "capabilities": capabilities.get(rack["label"], {})}
        for rack in sorted(nodes_by_type.get(_ENTITY_RACK, []), key=lambda n: n["label"])
    ]


def _group_nodes_by_type(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket node rows by ``entity_type``.  Pure."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        grouped.setdefault(node["entity_type"], []).append(node)
    return grouped


def _normalise_statuses(raw: Any) -> list[str] | None:
    """Validate the ``statuses`` argument's SHAPE and return it, or ``None``.

    ``None`` means **no filter**, and so does an empty list: "I named no
    statuses" is not "match nothing", and the REST adapter already encodes that
    reading — it turns an absent query parameter into ``None`` via
    ``getlist(...) or None``.  A caller that really wants an empty result asks
    for a status nothing has.

    SHAPE only.  This function does not case-fold, does not trim, and above all
    does not check the values against the NetBox vocabulary: that vocabulary is
    migration 061's composite ``CHECK`` and lives in exactly one place, which is
    the same rule ``devices.py`` follows on the write side.  ``['NONSENSE']``
    is therefore a well-formed request that matches nothing.

    A bare ``str`` is REFUSED rather than wrapped: ``statuses="active"`` is a
    client bug, and silently reading it as ``["active"]`` — or worse, as its
    characters — hides it.

    Raises:
        ValueError: not an array, or an element that is not a string.
    """
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"do_get_topology: 'statuses' must be an array of strings, got {type(raw).__name__}"
        )
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(
                "do_get_topology: every 'statuses' entry must be a string, "
                f"got {type(item).__name__}"
            )
        values.append(item)
    return values or None


# ---------------------------------------------------------------------------
# Public composer — do_get_topology
# ---------------------------------------------------------------------------


async def do_get_topology(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Read a DESIGN's full topology.

    This is the domain core behind the ``system_design_get_topology`` MCP tool
    and ``GET /api/system-design/topology``.  It is read-only.

    Parameters
    ----------
    engine:
        NCEEngine instance with a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,       # required
            "design_id": str,                 # required
            "statuses": list[str] | None,     # optional LIVE filter (M6.W16b)
        }``

        ``statuses`` is **live from M6.W16b**.  W13a declared it and ignored it;
        this wave made it a real predicate, so the parameter that Copper could
        already send now changes the result.  What it narrows, exactly:

        * It filters **``devices``, ``racks`` and ``cables``** — the three
          buckets whose nodes can carry a row in ``system_design_node_state`` —
          to nodes whose stored ``status`` is one of the named values.  A
          device's ``ports`` follow their device: a filtered-out device takes
          its ports with it, because ports are nested under it.
        * It does **not** touch ``design``, ``functional_locations``, ``edges``,
          ``geometry``, ``state`` or ``version``.  Those are the design's
          structure and canvas, and a filtered read is a *view of the
          lifecycle-bearing nodes*, not a subgraph.  ``edges`` in particular
          stays whole on purpose, so a caller can still see what a filtered-out
          device was attached to; ``state`` stays whole so the caller can see
          the status that excluded it.  Changing that is a contract change and
          has to change a test on purpose — see
          ``tests/test_system_design_status_filter.py``.
        * **A node with no state row never matches, and neither does one whose
          ``status`` is NULL.**  Absence is not the default status.  This is the
          same absence-vs-default distinction the ``state`` map preserves, and
          it is where a subtle leak would live: coalesce it and a
          ``statuses=['planned']`` read starts returning the entire legacy
          as-built estate as though somebody had planned it.
        * ``None`` and ``[]`` both mean **no filter**.  Values are matched
          verbatim — no case folding, no trimming, no vocabulary check.

        The filter is a **SQL predicate** (:func:`_fetch_nodes_by_labels`),
        never a discard over rows already fetched: on an owner pool the
        predicate is the tenant boundary.

    Returns
    -------
    dict
        ``{
            "design": {"label": str, "entity_type": "DESIGN"} | None,
            "functional_locations": [ {"label": ..., "entity_type": ...}, ... ],
            "devices": [
                {
                    "node": {...},
                    "capabilities": {...},
                    "ports": [ {"node": {...}, "capabilities": {...}}, ... ],
                },
                ...
            ],
            "racks": [ {"node": {...}, "capabilities": {...}}, ... ],
            "cables": [ {"label": ..., "entity_type": ...}, ... ],
            "edges": [ {"subject": ..., "predicate": ..., "object": ...}, ... ],
            "geometry": { "<node label>": {"x", "y", "rack_position",
                                           "rack_face", "cable_length_m",
                                           "cable_type", "meta"}, ... },
            "state": { "<node label>": {"status": str | None,
                                        "revision": str | None,
                                        "salience": float | None}, ... },
            "version": int,
        }``

        **Additive only (M6.W14, M6.W16b).**  ``racks`` and ``geometry`` were
        W14's new keys, ``state`` is W16b's; every field W13a/W13b/W13c/W14
        returned keeps its **name** and its **type**, and no existing bucket
        changed shape.  Copper is already consuming this shape.

        Member *position* within the result dict is deliberately NOT claimed as
        verified: reordering the keys leaves the suite green, because member
        order is not observable to a conforming JSON client (RFC 8259 §4 —
        object members are unordered).  Name and type are gated; order is not,
        and saying otherwise would be crediting a gate that does not exist.

        ``racks`` closes debt **D5**: RACK nodes have been authored since W12
        and were never projected at all — see :func:`_build_racks`.

        ``geometry`` is a **flat map keyed by node label**, not a key nested
        into each bucket.  One map covers devices, ports, racks, cables and
        functional locations uniformly, and adding it costs exactly one new
        top-level key rather than a shape change inside four existing buckets.
        A node with no geometry row is simply absent from the map — it is not
        present with null members, so "never placed" and "placed at the origin"
        stay distinguishable.  ``x``/``y`` are **canvas grid units**, origin
        **top-left**, **y-down**; room dimensions are in ``meta`` under
        ``copper.room.w``/``.d``/``.h``, in **meters** (Rev 2 §4).

        ``state`` is a **flat map keyed by node label**, for the same reason
        ``geometry`` is — and for one more that is specific to it.  The generic
        reason: one map covers DEVICE, RACK and CABLE uniformly and costs one
        new top-level key, where nesting would be a shape change inside three
        buckets that do not even share a shape (``racks`` items are
        ``{node, capabilities}`` and ``cables`` items are bare node rows, both
        pinned by W14's tests).  The specific reason is the **one-way door**: a
        flat map can express "no row" as *the key is not there*, which is
        exactly the fact W17's retirement guard denies on.  A nested
        ``device["state"]`` would have to spell that absence as an empty dict or
        a null — a second encoding of "nothing was declared", and the kind of
        thing a later reader coalesces to a default without noticing.

        Only DEVICE, RACK and CABLE can appear in ``state``; migration 061's
        composite CHECK refuses a row for anything else, PORT included.  Its
        three members are ``status`` (NetBox lifecycle, **never defaulted**),
        ``revision`` (inert storage, interpreted by Copper) and ``salience``
        (finite, non-negative, JSON-native ``float``).

        ``version`` is the per-design optimistic-concurrency token (Rev 2 §2)
        and is now **live**: it is the design's real stored version, and ``0``
        means no authoring write has been recorded against this design yet.
        ``0`` rather than ``null`` because ``0`` is a token the caller can pass
        straight back as ``expected_version``.

        Port ``capabilities.extra`` is returned **verbatim** — the reserved
        ``copper.*`` component-class keys are stored by NCE and interpreted by
        Copper (Rev 2 §5).

        ``design`` is ``None`` when the design does not exist **in the caller's
        namespace** — a design that exists only in another tenant reads as
        absent, which is the intended isolation behaviour.

    Raises
    ------
    ValueError
        When ``namespace_id`` or ``design_id`` is missing, or when ``statuses``
        is not an array of strings (SHAPE only — see
        :func:`_normalise_statuses`).

    Notes
    -----
    Cable-path tracing (front/rear traversal per NetBox semantics) is **out of
    scope** for this wave (Rev 2 §8) — cables are returned as nodes, and the
    front/rear relationships are not walked.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_get_topology: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw: str = str(params.get("design_id") or "").strip()
    if not design_id_raw:
        raise ValueError("do_get_topology: 'design_id' is required in params")

    design_lbl = _design_label(design_id_raw)
    # M6.W16b.  Validated BEFORE the session opens: a malformed ``statuses``
    # is a caller error, and taking a connection to discover it would make a
    # -32602 cost a pool checkout.
    statuses = _normalise_statuses(params.get("statuses"))

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        scope_labels = await _fetch_design_scope_labels(conn, ns_uuid, design_lbl)
        # M6.W16b: ``statuses`` narrows the STATE-BEARING nodes here, in SQL.
        # Everything below is deliberately handed the UNFILTERED scope: the
        # structure and the canvas are not what the caller filtered.
        nodes = await _fetch_nodes_by_labels(conn, ns_uuid, scope_labels, statuses)
        edges = await _fetch_edges_within(conn, ns_uuid, scope_labels)
        capabilities = await _fetch_capabilities_by_labels(conn, ns_uuid, scope_labels)
        # M6.W16b: per-node lifecycle state.  No COALESCE — a node with no row
        # is absent from this map, and that absence is load-bearing for W17.
        state = await _fetch_node_state_by_labels(conn, ns_uuid, scope_labels)
        # M6.W14.  Two grains, two queries: geometry rows are keyed by NODE
        # label, the version row by the DESIGN label.  ``scope_labels``
        # contains the design label too, so ``fetch_geometry_by_labels`` filters
        # the version row out structurally (``version IS NULL``) rather than
        # relying on the caller not to ask for it — see its docstring.
        geometry = await fetch_geometry_by_labels(conn, ns_uuid, scope_labels)
        version = await fetch_design_version(conn, ns_uuid, design_lbl)

    nodes_by_type = _group_nodes_by_type(nodes)

    design_nodes = nodes_by_type.get(_ENTITY_DESIGN, [])
    result: dict[str, Any] = {
        "design": design_nodes[0] if design_nodes else None,
        "functional_locations": sorted(
            nodes_by_type.get(_ENTITY_FUNCTIONAL_LOCATION, []),
            key=lambda n: n["label"],
        ),
        "devices": _build_devices(nodes_by_type, edges, capabilities),
        # Debt D5 (M6.W14): RACK was authored since W12 and never projected.
        "racks": _build_racks(nodes_by_type, capabilities),
        "cables": sorted(nodes_by_type.get(_ENTITY_CABLE, []), key=lambda n: n["label"]),
        "edges": edges,
        # M6.W14: canvas geometry, keyed by node label; nodes without a
        # geometry row are absent rather than present-and-null.
        "geometry": geometry,
        # M6.W16b: per-node lifecycle state, keyed by node label.  A node with
        # no state row is ABSENT — never present with a defaulted status.
        "state": state,
        # M6.W14: the live optimistic-concurrency token; 0 = never authored.
        "version": version,
    }

    log.info(
        "do_get_topology: ns=%s design=%s nodes=%d devices=%d racks=%d edges=%d "
        "geometry=%d state=%d statuses=%s version=%s",
        ns_uuid,
        design_id_raw,
        len(nodes),
        len(result["devices"]),
        len(result["racks"]),
        len(edges),
        len(geometry),
        len(state),
        "*" if statuses is None else len(statuses),
        version,
    )
    return result
