"""
nce/vertical_modules/system_design/devices.py
=============================================
Phase-2 (additive moat layer) — device-capability model for the System Design
vertical module.

Responsibilities
----------------
* Define DEVICE / PORT / RACK / CABLE node types as kg_nodes
  with entity_type prefixes, hung off the existing DESIGN node via kg_edges.
* Write capability attributes (AVIXA Revit Parameter schema) to the
  ``system_design_device_capabilities`` table, keyed by (namespace_id, node_label).
* Guard every owned-node write with ``assert_owner`` + ``emit_graph_write``
  inside the same asyncpg transaction — follows graph.py EXACTLY.

Edge topology written by this module
-------------------------------------
  DESIGN        -[contains]->   DEVICE
  DEVICE        -[has_port]->   PORT
  PORT          -[connected_to]-> PORT   (signal path; confidence = signal confidence)
  DEVICE        -[mounted_in]-> RACK
  PORT          -[uses_cable]-> CABLE    (both terminations; optional cable ref)
  DESIGN        -[has_rack]->   RACK

Design invariants (uncle-bob-craft / dependency rule)
------------------------------------------------------
- No web / HTTP / admin imports — domain core only.
- One function, one job; no shared mutable state.
- ``confidence`` on edges ONLY — never on kg_nodes (wave rule 7).
- ``kg_nodes`` has NO payload/metadata column — capability attributes go into
  ``system_design_device_capabilities`` (the typed side-table).
- ``assert_owner`` is called before every own-node INSERT — deny-by-default.
- Every own-node write is followed by ``emit_graph_write`` in the same tx.
- ENRICH-NEVER-REWRITE: this module does not import or alter Phase-1 symbols
  (graph.py public functions, validate.py, propose.py, etc.).

Ownership (Contract A §9.1)
----------------------------
DEVICE / PORT / RACK / CABLE are owned by system_design.
Entries are in nce/config_data/node-ownership.json (appended in this wave).

SIGNAL_CHAIN is RETIRED (Batch 067i). No module ever wrote a node of that
type, so the type is no longer declared here. A signal chain is a
``connected_to`` walk over PORT nodes -- see validation_queries.py's
signal_flow_continuity check and this package's README.md. The
node-ownership.json row is deliberately LEFT INERT as a name reservation so
no other engine can claim the type; no code in this module reads it.

Decision — a CABLE is two-ended (Batch 067f)
---------------------------------------------
This docstring used to declare ``DEVICE -[uses_cable]-> CABLE`` while the code
wrote ``PORT -[uses_cable]-> CABLE`` from the source port only.  Both were
wrong: a canvas has to draw a cable *between two ports*, and a single edge
cannot say where the far end lands.  The subject is the PORT, not the DEVICE
(a device with eight ports would otherwise collapse eight distinct cable runs
onto one node), and the edge is written from **both** terminations.

Backfill is by re-author: the pre-wave rows kept their source edge, and the
``UNIQUE (subject_label, predicate, object_label, namespace_id)`` constraint on
``kg_edges`` (nce/schema.sql) makes the re-author an upsert, so re-running the
author adds only the missing destination edge and never duplicates the one that
was already there.  See tests/test_system_design_cables.py.

**That data-fix is safe to re-run after W16, and it is worth saying why** —
this note is the recorded procedure, so anybody following it is running a mass
re-author over physically-installed cable.  It writes no lifecycle state,
because none of those cables is new to the call and the fix names no
``cable_status``.  Under the round-1 rule, which wrote a state row for every
node it touched, one pass of this procedure would have stamped ``'planned'``
onto every already-installed cable in the estate.  Gated by
``tests/test_system_design_node_state.py::TestNoBackfill::
test_a_67f_shaped_re_author_changes_no_lifecycle_state``.

Per-node LIFECYCLE STATE (Batch 067g, M6.W16)
----------------------------------------------
A DEVICE, RACK or CABLE may carry one row in ``system_design_node_state``
(migration 061) holding ``status``, ``revision`` and ``salience``.  PORT may
not: NetBox has no lifecycle status for a port, none is invented here, and the
table's composite CHECK refuses a PORT row structurally rather than by
convention.  A port that *carries* one of the keys is refused, not ignored.

THE RULE (ratified by Sindre, round 2) — a state row is written **only** when

    the node is GENUINELY NEW to this call, OR the caller supplied an
    explicit lifecycle key.

A pre-existing node re-authored with no lifecycle keys keeps having **no row**.
That is not a detail: an ordinary canvas save, a geometry-only drag and the
67f data-fix above all run through this one function, and the round-1 rule —
write a row for every node touched — turned every one of them into a mass
mint of ``'planned'`` over legacy as-built equipment.

Three distinguishable states, and W17's retirement guard needs all three:

* **no row**       — nothing was ever declared about this node.  W17 denies.
* **status NULL**  — we hold data (a revision, a salience); nobody declared a
  lifecycle.  W17 denies.  This is what a revision-only update on a
  pre-existing node produces, and it is why ``status`` is nullable and carries
  no column ``DEFAULT``.
* **status set**   — a lifecycle was declared.  W17 decides on the value.

``DEFAULT_NODE_STATUS`` applies to NEWNESS, never to silence, and never to a
missing row.  Nothing in this engine may read an absent state row as
``'planned'``.

The status vocabulary is NetBox's, it is per node type, and it lives in ONE
place -- the CHECK in the DDL.  It is deliberately NOT duplicated as a Python
collection here: a second copy is how a write path and its constraint drift
apart while both suites stay green.  This module validates SHAPE (a status is a
non-empty string, a salience is a finite non-negative number); the database
validates VOCABULARY, and this module translates the database's refusal into
the same ``ValueError`` the shape refusals raise, so both arrive at the caller
as ``-32602`` / 422 instead of one of them as an opaque 500.

Promote (``planned -> active``) is NOT here.  That flow is Copper's, via
``do_validate_design`` plus the first ``action_approval_queue`` writer, and is
coordinated in a later wave.  This module stores the status it is handed and
performs no transition of its own.
"""

from __future__ import annotations

import logging
import math
import sys
from typing import Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.entity_resolution.ownership import assert_owner
from nce.events.emit import emit_graph_write

log = logging.getLogger("nce.vertical_modules.system_design.devices")

# ---------------------------------------------------------------------------
# Engine identifier — must match node-ownership.json owner_engine value.
# ---------------------------------------------------------------------------
_SYSTEM_DESIGN_ENGINE: str = "system_design"

# ---------------------------------------------------------------------------
# Node type constants — must match node-ownership.json node_type strings.
# ---------------------------------------------------------------------------
_NODE_TYPE_DEVICE: str = "DEVICE"
_NODE_TYPE_PORT: str = "PORT"
_NODE_TYPE_RACK: str = "RACK"
_NODE_TYPE_CABLE: str = "CABLE"


# ---------------------------------------------------------------------------
# Edge predicates.
# ---------------------------------------------------------------------------
_PRED_CONTAINS: str = "contains"
_PRED_HAS_PORT: str = "has_port"
_PRED_CONNECTED_TO: str = "connected_to"
_PRED_MOUNTED_IN: str = "mounted_in"
_PRED_USES_CABLE: str = "uses_cable"
_PRED_HAS_RACK: str = "has_rack"

# Structural edges are certain.
_STRUCTURAL_CONFIDENCE: float = 1.0

#: The status a GENUINELY NEW node gets when the author call names none.
#:
#: It is bound as a statement parameter and it is the ONLY place this value
#: exists.  The column carries no ``DEFAULT`` (migration 061): a column default
#: would be a second, independent source of the one dangerous value, mintable
#: by any future writer or manual data-fix that never touches this file.
#:
#: It applies to NEWNESS, not to silence.  A node that already existed before
#: this call does not acquire a status by being re-authored, and a node that
#: has no state row does not read as ``'planned'`` anywhere, ever — W17's
#: retirement guard denies on an absent or NULL state and depends on both.
DEFAULT_NODE_STATUS: str = "planned"

#: The per-item keys that carry lifecycle state on a ``devices`` or ``racks``
#: entry.  A ``connections`` entry uses the ``cable_``-prefixed spellings for
#: the same reason ``cable_geometry`` is not called ``geometry``: a connection
#: is an EDGE, and an unprefixed ``status`` there would read as the edge's own
#: status rather than the CABLE node's.
_STATE_KEYS: tuple[str, ...] = ("status", "revision", "salience")

#: The prefix that moves the three keys from "this item" to "the CABLE node
#: this item names".
_CABLE_PREFIX: str = "cable_"

#: Largest magnitude a salience may have.  ``sys.float_info.max`` is the last
#: value a C double can hold, so any int above it cannot be coerced and
#: ``math.isfinite`` raises ``OverflowError`` on the attempt — see
#: :func:`_state_of` for why that particular exception is dangerous here.
_MAX_FINITE_SALIENCE: float = sys.float_info.max

#: The exact spellings each authoring bucket accepts.  Anything that *reads* as
#: a lifecycle key but is not in the bucket's set is REFUSED rather than
#: dropped — see :func:`_refuse_misplaced_lifecycle_keys`.
_DEVICE_STATE_KEYS: frozenset[str] = frozenset(_STATE_KEYS)
_RACK_STATE_KEYS: frozenset[str] = frozenset(_STATE_KEYS)
_CONNECTION_STATE_KEYS: frozenset[str] = frozenset(f"{_CABLE_PREFIX}{k}" for k in _STATE_KEYS)
_PORT_STATE_KEYS: frozenset[str] = frozenset()

# Capability table column names — used in _upsert_capability.
_CAP_COLUMNS: tuple[str, ...] = (
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


# ---------------------------------------------------------------------------
# Label helpers (deterministic, upper-cased).
# ---------------------------------------------------------------------------


def device_label(design_id: str, device_ref: str) -> str:
    """Canonical DEVICE label: ``DEVICE:<DESIGN_ID>:<DEVICE_REF>``."""
    return f"DEVICE:{design_id.upper()}:{device_ref.upper()}"


def port_label(design_id: str, device_ref: str, port_ref: str) -> str:
    """Canonical PORT label: ``PORT:<DESIGN_ID>:<DEVICE_REF>:<PORT_REF>``."""
    return f"PORT:{design_id.upper()}:{device_ref.upper()}:{port_ref.upper()}"


def rack_label(design_id: str, rack_ref: str) -> str:
    """Canonical RACK label: ``RACK:<DESIGN_ID>:<RACK_REF>``."""
    return f"RACK:{design_id.upper()}:{rack_ref.upper()}"


def cable_label(design_id: str, cable_ref: str) -> str:
    """Canonical CABLE label: ``CABLE:<DESIGN_ID>:<CABLE_REF>``."""
    return f"CABLE:{design_id.upper()}:{cable_ref.upper()}"


# ---------------------------------------------------------------------------
# Private: node upserts (assert_owner-guarded + emit_graph_write).
# ---------------------------------------------------------------------------


async def _upsert_node(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    label: str,
    entity_type: str,
    source_id: str | None,
) -> bool:
    """Upsert one owned graph node.  Always: assert_owner → probe → INSERT → emit.

    Returns:
        ``True`` when this call CREATED the node, ``False`` when the node
        already existed.  W16 needs that fact and needs it captured **here**:
        the ``_upsert_edge`` calls that follow take FK-ish locks on the
        just-written parent, so any after-the-fact "was it there?" test is
        answering a question about a row this transaction has already touched.

    How newness is established, and why it is not the obvious way
    -------------------------------------------------------------
    ``RETURNING (xmax = 0)`` is the usual trick and it **does not work on this
    table**: ``kg_nodes`` carries ``FORCE ROW LEVEL SECURITY``, and PostgreSQL
    refuses system columns under an RLS rewrite —
    ``FeatureNotSupportedError: cannot retrieve a system column in this
    context``.  (``kg_nodes`` is also HASH-partitioned on ``label``.)

    ``RETURNING (created_at = updated_at)`` does work, but it is an inference
    from timestamps: it reads ``t`` for a row created earlier in the *same*
    transaction, and it would read ``t`` for any pre-existing row that some
    other writer had left with ``created_at == updated_at``.  On a one-way door
    whose failure mode is "a legacy as-built node silently acquires a
    lifecycle", an inference is the wrong instrument.

    So this is an explicit, exact, RLS-safe existence probe.  It costs one
    round trip per node — accepted deliberately: authoring is a canvas save,
    not a hot loop, and the alternative trades exactness for latency on the
    single fact this wave exists to get right.

    The probe is race-free in the path that matters: ``bump_design_version``
    has already taken the design's row lock on this same connection, so two
    concurrent authors of one design serialise, and labels embed the design id
    so two different designs cannot collide.  A caller that invokes this core
    directly, without that lock, does not get that guarantee — stated rather
    than assumed.
    """
    await assert_owner(conn, ns_uuid, entity_type, _SYSTEM_DESIGN_ENGINE)
    existed = bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM kg_nodes
                WHERE label = $1 AND namespace_id = $2::uuid
            )
            """,
            label,
            str(ns_uuid),
        )
    )
    await conn.execute(
        """
        INSERT INTO kg_nodes
            (label, entity_type, namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3::uuid, 'sync', $4)
        ON CONFLICT (label, namespace_id) DO UPDATE
            SET entity_type               = EXCLUDED.entity_type,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_nodes.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        label,
        entity_type,
        str(ns_uuid),
        source_id,
    )
    await emit_graph_write(
        conn,
        namespace_id=ns_uuid,
        node_type=entity_type,
        op="upserted",
        node_id=label,
    )
    return not existed


# ---------------------------------------------------------------------------
# Private: edge upsert (no ownership guard — edges are always safe to write).
# ---------------------------------------------------------------------------


async def _upsert_edge(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float,
    source_id: str | None,
) -> None:
    """Upsert one kg_edge.  confidence (0–1) on edges only (rule 7)."""
    await conn.execute(
        """
        INSERT INTO kg_edges
            (subject_label, predicate, object_label, confidence,
             namespace_id, change_origin, system_design_source_id)
        VALUES ($1, $2, $3, $4, $5::uuid, 'sync', $6)
        ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO UPDATE
            SET confidence                = EXCLUDED.confidence,
                change_origin             = 'sync',
                system_design_source_id   = COALESCE(
                    EXCLUDED.system_design_source_id,
                    kg_edges.system_design_source_id
                ),
                updated_at                = NOW()
        """,
        subject,
        predicate,
        obj,
        float(confidence),
        str(ns_uuid),
        source_id,
    )


# ---------------------------------------------------------------------------
# Private: capability upsert (typed side-table, AVIXA Revit param schema).
# ---------------------------------------------------------------------------


async def _upsert_capability(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    node_label: str,
    cap: dict[str, Any],
) -> None:
    """Write capability attributes to system_design_device_capabilities.

    Only columns present in ``cap`` are written; absent keys keep their
    existing DB values via the ON CONFLICT DO UPDATE COALESCE pattern.
    ``extra`` defaults to the DB DEFAULT ``'{}'::jsonb`` on first insert.
    """
    import json as _json

    extra = cap.get("extra", {})
    extra_json: str = _json.dumps(extra) if isinstance(extra, dict) else str(extra)

    await conn.execute(
        """
        INSERT INTO system_design_device_capabilities (
            namespace_id, node_label,
            signal_format, signal_version, port_direction,
            poe_class, poe_watts,
            dante_rx_channels, dante_tx_channels,
            power_draw_watts, heat_btu_hr,
            redundancy_role,
            device_category, manufacturer, model_number,
            extra
        )
        VALUES (
            $1::uuid, $2,
            $3, $4, $5,
            $6, $7,
            $8, $9,
            $10, $11,
            $12,
            $13, $14, $15,
            $16::jsonb
        )
        ON CONFLICT (namespace_id, node_label) DO UPDATE
            SET signal_format        = COALESCE(EXCLUDED.signal_format,        system_design_device_capabilities.signal_format),
                signal_version       = COALESCE(EXCLUDED.signal_version,       system_design_device_capabilities.signal_version),
                port_direction       = COALESCE(EXCLUDED.port_direction,       system_design_device_capabilities.port_direction),
                poe_class            = COALESCE(EXCLUDED.poe_class,            system_design_device_capabilities.poe_class),
                poe_watts            = COALESCE(EXCLUDED.poe_watts,            system_design_device_capabilities.poe_watts),
                dante_rx_channels    = COALESCE(EXCLUDED.dante_rx_channels,    system_design_device_capabilities.dante_rx_channels),
                dante_tx_channels    = COALESCE(EXCLUDED.dante_tx_channels,    system_design_device_capabilities.dante_tx_channels),
                power_draw_watts     = COALESCE(EXCLUDED.power_draw_watts,     system_design_device_capabilities.power_draw_watts),
                heat_btu_hr          = COALESCE(EXCLUDED.heat_btu_hr,          system_design_device_capabilities.heat_btu_hr),
                redundancy_role      = COALESCE(EXCLUDED.redundancy_role,      system_design_device_capabilities.redundancy_role),
                device_category      = COALESCE(EXCLUDED.device_category,      system_design_device_capabilities.device_category),
                manufacturer         = COALESCE(EXCLUDED.manufacturer,         system_design_device_capabilities.manufacturer),
                model_number         = COALESCE(EXCLUDED.model_number,         system_design_device_capabilities.model_number),
                extra                = EXCLUDED.extra,
                updated_at           = NOW()
        """,
        str(ns_uuid),
        node_label,
        cap.get("signal_format"),
        cap.get("signal_version"),
        cap.get("port_direction"),
        cap.get("poe_class"),
        cap.get("poe_watts"),
        cap.get("dante_rx_channels"),
        cap.get("dante_tx_channels"),
        cap.get("power_draw_watts"),
        cap.get("heat_btu_hr"),
        cap.get("redundancy_role"),
        cap.get("device_category"),
        cap.get("manufacturer"),
        cap.get("model_number"),
        extra_json,
    )


# ---------------------------------------------------------------------------
# Private: lifecycle state (W16 — migration 061).
#
# THE RULE, ratified by Sindre in round 2:
#
#     write a state row ONLY when the node is GENUINELY NEW to this call,
#     OR the caller supplied an explicit lifecycle key.
#
# Everything else in this section exists to make that rule true in every path,
# including the ones nobody thinks of as authoring: an ordinary canvas save, a
# geometry-only drag of one device, and 67f's "backfill by re-author" data-fix
# all run through here, and under round 1's unconditional write each of them
# would have stamped 'planned' onto physically-installed equipment.
#
# The condition is "any explicit lifecycle key", NOT "an explicit status".
# Spelled the narrower way, a pre-existing node re-authored with
# ``{"revision": "REV-7"}`` and no status writes NOTHING — no row, the revision
# silently discarded, HTTP 200 — which is the same silent-drop failure this
# module refuses misplaced keys to prevent.
# ---------------------------------------------------------------------------


def _is_lifecycle_spelling(key: str) -> bool:
    """True when *key* READS as one of the three lifecycle keys.

    Case-insensitive, whitespace-insensitive, and it sees through ANY number of
    ``cable_`` prefixes, so ``Status``, ``SALIENCE``, ``Cable_Revision``,
    ``"salience "`` and ``cable_cable_status`` all answer ``True``.  That
    breadth is the point: this is the input to a REFUSAL, and the cost of a
    false positive (a 422 telling the caller to fix a key name) is far below the
    cost of a false negative (a 200 that silently discards what they sent).

    Round 2 stripped at most one prefix and did not strip whitespace, so
    ``cable_cable_status`` and ``"salience "`` were both accepted and dropped.
    Pathological spellings, but the stated principle is "refused, not dropped",
    and a principle with two known exceptions is a habit.

    ``cable_geometry`` answers ``False`` — stripping the prefix leaves
    ``geometry``, which is not a lifecycle key.
    """
    lowered = key.strip().lower()
    while lowered.startswith(_CABLE_PREFIX):
        lowered = lowered[len(_CABLE_PREFIX) :]
    return lowered in _STATE_KEYS


def _connection_confidence(cnx: dict[str, Any]) -> float:
    """Caller-supplied ``confidence``, refused unless it is a real number in [0, 1].

    ``mcp_handlers`` forwards ``connections`` items **verbatim** from tool
    arguments, so this value is fully caller-controlled, and the bare
    ``float(...)`` this replaced accepted three things it should not have:

    * ``"1e400"`` and ``1e400`` -> ``inf``. No exception, 200 returned, an
      INFINITE confidence written onto the edge -- and ``graph_query.py:106``
      then nulls any non-finite confidence on READ, so the caller authors a
      connection, is told it succeeded, and never sees the value again.
    * ``float("nan")`` -> stored. Every comparison against NaN is false, so a
      threshold filter silently excludes the edge rather than reporting it.
    * ``-5`` or ``1e9`` -> stored, while ``_upsert_edge``'s own docstring says
      "confidence (0-1)". The contract was documented and unenforced.

    ``ValueError`` is deliberate and is NOT the D49 family: this genuinely IS a
    caller error -- a different argument fixes it -- so ``-32602 Invalid
    parameters`` / 422 is the right wire class. Contrast
    ``DeploymentConfigurationError``, which exists because an unset config key
    is not something any argument can fix.
    """
    value_raw = cnx.get("confidence", _STRUCTURAL_CONFIDENCE)
    if isinstance(value_raw, bool) or not isinstance(value_raw, (int, float)):
        raise ValueError(
            f"connection confidence must be a number in [0, 1]; got {type(value_raw).__name__}"
        )
    value = float(value_raw)
    if not math.isfinite(value) or not (0.0 <= value <= 1.0):
        raise ValueError(f"connection confidence must be a number in [0, 1]; got {value_raw!r}")
    return value


def _refuse_misplaced_lifecycle_keys(
    payload: dict[str, Any],
    *,
    accepted: frozenset[str],
    where: str,
) -> None:
    """Refuse a lifecycle key this bucket does not accept, rather than dropping it.

    Covers the shapes that were all silently accepted-and-discarded before:
    ``status`` on a **connection** (the keys there are ``cable_``-prefixed
    because they describe the CABLE node, not the edge), ``cable_status`` on a
    **device** or a **port**, any lifecycle key at all on a **port**, every
    casing variant of those, and — since round 3 — the same keys one level
    down, INSIDE a nested authoring object.

    A write that returns 200 while throwing away what the caller sent is the
    failure mode W13b refused ``expected_version`` for: a client that believes
    it set something it did not set is strictly worse off than one whose
    request was rejected.

    NESTED OBJECTS ARE SCANNED TOO, and that is round 3's fix.  This used to
    walk ``payload.items()`` and stop, so ``capability`` and ``geometry`` — both
    plain dicts — were never looked into.  ``_upsert_capability`` writes only
    ``_CAP_COLUMNS`` and drops the rest without a word, so

        {"device_ref": "X", "capability": {"status": "active"}}

    returned **HTTP 200** and stored ``status = 'planned'``: the caller declared
    ``active`` and the row said the retirable value.  That is not a re-opening
    of the one-way door — a pre-wave device with a nested key still mints no row
    — but it is a straight violation of the rule this function exists to
    enforce, one nesting level below where it was being enforced.

    ONE LEVEL, DELIBERATELY, AND HERE IS WHAT THAT DOES NOT COVER.  Every
    authoring sub-object in this contract (``capability``, ``geometry``,
    ``cable_geometry``) is flat, and ``ports`` is a list of items that get their
    own top-level call.  A key nested one level down is ALWAYS misplaced — no
    sub-object legitimately carries a lifecycle key — so ``accepted`` does not
    apply inside them.

    But ``capability.extra`` and ``geometry.meta`` are CALLER-OWNED JSON
    documents that NCE stores verbatim and never interprets (Rev 2 §5, the
    reserved ``copper.*`` keys).  ``{"capability": {"extra": {"status":
    "active"}}}`` is therefore still accepted and still silently ignored as a
    lifecycle declaration — and it must be, because refusing it would mean
    inspecting a document this engine has promised not to read.  Recursing
    further would trade a small silent-drop for breaking the passthrough
    contract, which is the worse deal.  Disclosed rather than fixed.

    An explicit ``null`` is not a refusal trigger — ``{"status": null}`` says
    nothing, so a misplaced *nothing* is nothing.  See :func:`_state_of`.

    Raises:
        ValueError: mapped to ``McpError(-32602)`` / HTTP 422 by the surfaces.
    """
    misplaced = sorted(
        key
        for key, value in payload.items()
        if value is not None and _is_lifecycle_spelling(key) and key not in accepted
    )
    if misplaced:
        allowed = ", ".join(sorted(accepted)) if accepted else "none"
        raise ValueError(
            f"{where} does not accept the lifecycle key(s) "
            + ", ".join(misplaced)
            + f"; accepted here: {allowed}"
        )

    nested = sorted(
        f"{outer}.{inner}"
        for outer, value in payload.items()
        if isinstance(value, dict)
        for inner, inner_value in value.items()
        if inner_value is not None and _is_lifecycle_spelling(inner)
    )
    if nested:
        raise ValueError(
            f"{where} carries lifecycle key(s) inside a nested object: "
            + ", ".join(nested)
            + "; lifecycle keys belong on the item itself, never inside "
            "capability or geometry, and a nested one would be silently dropped"
        )


def _state_of(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Read the lifecycle-state keys off one authoring payload item.

    *prefix* is ``""`` for a device or a rack and :data:`_CABLE_PREFIX` for a
    connection.  Returned keys are always the three unprefixed names, so the
    writer never has to know which spelling they came from.

    ABSENT AND EXPLICIT ``null`` ARE THE SAME THING, and that is a decision
    rather than an oversight.  Under migration 061's nullable ``status`` the
    two *could* be told apart — ``{"status": null}`` could mean "I decline to
    declare a lifecycle" and mint a row.  It must not, for one decisive reason:
    JSON serialisers routinely emit explicit nulls for unset optional fields,
    so a client sending ``{"status": null, "revision": null, "salience": null}``
    on every save would mint a row for every node on every save and re-open
    exactly the door round 2 closes.  ``mcp_handlers.expected_version_of``
    already reads an explicit ``null`` as absence for the same reason, so this
    is the engine's established reading rather than a local invention.
    Declining to declare needs no spelling of its own: not creating a row says
    it, and W17 denies on both.

    SHAPE only.  The status VOCABULARY is the database's — see the module
    docstring.  Duplicating it here would give this engine two definitions of
    what a legal status is, and they would drift.

    ``salience`` must be FINITE.  ``NaN`` and ``±Infinity`` are ``float``
    instances and sail through a bare ``isinstance`` check, and
    ``Request.json`` is a plain ``json.loads``, which accepts a bare ``NaN`` on
    the wire by default.  A stored ``NaN`` is not a rendering nuisance: in
    PostgreSQL ``numeric`` it compares GREATER than every finite value, so it
    sorts as the largest salience in the tenant and silently flips any W17
    threshold predicate.  ``math.isfinite`` is used directly rather than
    reaching for ``admin_handlers._shared``'s copy — this module is domain core
    and may not import the web layer.  The database repeats the check, because
    a Python guard only protects the writers that go through it.

    Raises:
        ValueError: a key is present with the wrong type, or ``salience`` is
            non-finite.  Mapped to ``McpError(-32602)`` / HTTP 422.
    """
    state: dict[str, Any] = {}
    for key in _STATE_KEYS:
        value = payload.get(f"{prefix}{key}")
        if value is None:
            state[key] = None
            continue
        if key == "salience":
            # bool is an int subclass in Python; a ``true`` on the wire is a
            # client bug and must not become the salience 1.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{prefix}{key} must be a number when supplied")
            # ``math.isfinite`` COERCES its argument, and coercing a Python int
            # too large for a C double raises ``OverflowError`` — which is an
            # ``ArithmeticError``, NOT a ``ValueError``, so it sails straight
            # past every ``except ValueError`` on both surfaces and arrives as
            # ``-32603`` / a bare HTTP 500.  ``json.loads`` yields
            # arbitrary-precision ints, so ``{"salience": 10**309}`` on the wire
            # reaches this line; the threshold is exactly there
            # (``isfinite(10**308)`` is fine).
            #
            # The magnitude test therefore happens BEFORE any coercion, on the
            # int itself, where Python's arbitrary precision cannot overflow.
            # ``except OverflowError`` would also work, but a guard that avoids
            # the coercion is easier to keep correct than one that catches what
            # the coercion throws.
            if isinstance(value, int) and abs(value) > _MAX_FINITE_SALIENCE:
                raise ValueError(f"{prefix}{key} is too large to store as a finite number")
            if not math.isfinite(value):
                raise ValueError(f"{prefix}{key} must be a finite number (not NaN or Infinity)")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{prefix}{key} must be a non-empty string when supplied")
        state[key] = value
    return state


def _has_explicit_lifecycle(state: dict[str, Any]) -> bool:
    """True when the caller named ANY of the three keys with a real value.

    Any, not just ``status`` — see this section's header for why the narrower
    reading silently discards a revision-only update.
    """
    return any(state.get(key) is not None for key in _STATE_KEYS)


def _should_record_state(state: dict[str, Any], *, node_is_new: bool) -> bool:
    """THE one-way door, in one expression.

    ``True`` only for a node this call created, or a node the caller said
    something about.  A pre-existing node re-authored in silence keeps having
    **no row**, which is what lets W17 deny on absence for the whole legacy
    estate — permanently, not merely until the next canvas save.
    """
    return node_is_new or _has_explicit_lifecycle(state)


#: Constraint names on ``system_design_node_state`` and the caller-facing
#: reason each one stands for.  A ``CheckViolationError`` is not a
#: ``ValueError``, so without this map a wrong-vocabulary status escapes as an
#: INTERNAL error — ``-32603`` on MCP and, in production, a bare 500 on REST
#: with no indication of what was wrong — while the shape refusal one line
#: earlier correctly answers ``-32602`` / 422.  Same fault, two answers.
#:
#: The vocabulary itself is NOT duplicated here: these are constraint names and
#: prose, and the legal values stay in the DDL alone.
_STATE_CONSTRAINT_REASONS: dict[str, str] = {
    "system_design_node_state_status_per_node_type": (
        "status is not in this node type's NetBox vocabulary "
        "(the vocabularies are per node type and disjoint)"
    ),
    "system_design_node_state_salience_finite_non_negative": (
        "salience must be a finite, non-negative number"
    ),
    # ``system_design_node_state_node_label_not_blank`` is deliberately ABSENT.
    # No caller can reach it: every label this module writes is built by
    # ``device_label`` / ``rack_label`` / ``cable_label``, which always emit a
    # prefix, so a blank label means an internal bug in THIS module. Mapping it
    # would report that bug to the caller as their malformed argument — the
    # mirror image of the defect this table exists to fix — so it falls through
    # to the generic internal-error path, where it belongs.
}


async def _upsert_node_state(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    ns_uuid: UUID,
    node_label: str,
    node_type: str,
    state: dict[str, Any],
    *,
    node_is_new: bool,
) -> dict[str, Any]:
    """Write one lifecycle-state row and return the delta it produced.

    Only ever reached when :func:`_should_record_state` said so.  PORT is never
    passed here (see the module docstring); the table's composite CHECK would
    refuse it anyway.

    ``node_is_new`` decides the SEED status, and only the seed: a node this
    call created gets :data:`DEFAULT_NODE_STATUS` when the caller named none,
    while a pre-existing node that reached this function because the caller
    sent a ``revision`` gets ``status = NULL`` — "we hold data for this node,
    nobody declared its lifecycle", which W17 denies on exactly as it denies on
    a missing row.  Inventing ``'planned'`` there would be declaring a
    lifecycle on the caller's behalf.

    Partial update, exactly like ``_upsert_capability`` and
    ``geometry.upsert_node_geometry``: a key the caller omitted keeps its
    stored value.  ``$4`` is referenced directly on the conflict branch rather
    than through ``EXCLUDED.status`` — ``EXCLUDED.status`` has already had the
    seed applied by the ``COALESCE`` in ``VALUES``, so using it would overwrite
    a stored ``'active'`` on every re-author that named no status.

    ``namespace_id`` is in the ``INSERT`` values and, load-bearingly, in the
    ``ON CONFLICT`` target ``(namespace_id, node_label)`` — the owner pools
    that serve requests bypass ``FORCE ROW LEVEL SECURITY``, so a colliding
    node label in another tenant must be a different row by the KEY, not by a
    policy that is not being enforced on this connection.  The prior-status
    probe below carries the same predicate for the same reason.

    Returns:
        ``{"node_label", "node_type", "from", "to", "state_row_created",
        "resurrected"}``.  ``from``/``to`` are the status before and after.
        ``state_row_created`` is what tells "there was no row" apart from
        "there was a row whose status was NULL" — two different facts that W17
        happens to treat alike but that an audit reader must not have to guess
        between.  ``resurrected`` is true when a row already existed for a node
        this call CREATED, i.e. the row outlived its node and the new node has
        just inherited it; see migration 061's W17 obligation.

    Raises:
        ValueError: a database CHECK on this table refused the row.
    """
    previous = await conn.fetchrow(
        """
        SELECT status
        FROM system_design_node_state
        WHERE namespace_id = $1::uuid
          AND node_label = $2
        """,
        str(ns_uuid),
        node_label,
    )
    seed_status: str | None = DEFAULT_NODE_STATUS if node_is_new else None

    try:
        resulting = await conn.fetchval(
            """
            INSERT INTO system_design_node_state (
                namespace_id, node_label, node_type,
                status, revision, salience
            )
            VALUES (
                $1::uuid, $2, $3,
                COALESCE($4, $7), $5, $6
            )
            ON CONFLICT (namespace_id, node_label) DO UPDATE
                SET node_type  = EXCLUDED.node_type,
                    status     = COALESCE($4, system_design_node_state.status),
                    revision   = COALESCE($5, system_design_node_state.revision),
                    salience   = COALESCE($6, system_design_node_state.salience),
                    updated_at = NOW()
            RETURNING status
            """,
            str(ns_uuid),
            node_label,
            node_type,
            state.get("status"),
            state.get("revision"),
            state.get("salience"),
            seed_status,
        )
    except asyncpg.exceptions.CheckViolationError as exc:
        reason = _STATE_CONSTRAINT_REASONS.get(getattr(exc, "constraint_name", "") or "")
        if reason is None:
            # Not one of this table's constraints — not this module's to
            # reinterpret, and pretending otherwise would mislabel a genuine
            # server fault as the caller's mistake.
            raise
        # The offending VALUE is echoed: "status is not in this node type's
        # vocabulary" tells a caller what rule they broke but not which of the
        # values they sent broke it, and a payload can carry many.
        offending = state.get("status") if "status" in reason else state.get("salience")
        raise ValueError(f"{node_label} ({node_type}): {reason}; got {offending!r}") from exc

    return {
        "node_label": node_label,
        "node_type": node_type,
        "from": previous["status"] if previous is not None else None,
        "to": resulting,
        "state_row_created": previous is None,
        # RESURRECTION MARKER. A state row whose node does not exist is an
        # orphan: W17 is obliged to delete the row with the node (see migration
        # 061's header), and if it does not, re-authoring the same
        # deterministic label lands on the orphan and INHERITS its status.
        # Without this flag such a write reports {"from": "decommissioning",
        # "to": "decommissioning", "state_row_created": false} — honest about
        # the table, and indistinguishable from nothing having happened. An
        # audit reader must be able to tell a resurrection from a no-op, and
        # this is the wave whose whole purpose is to make that record
        # trustworthy. ``node_is_new`` is exactly the fact needed and is
        # already in hand, so this costs nothing.
        "resurrected": previous is not None and node_is_new,
    }


def _merge_state_delta(
    deltas: dict[str, dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    """Fold *delta* into *deltas*, keyed by node label.

    One payload can name the same node twice — two ``connections`` sharing a
    ``cable_ref`` is the ordinary case — and the second write is an update of
    the first.  The call's delta for that node is therefore the FIRST ``from``
    (with its ``state_row_created``) and the LAST ``to``; keeping the second
    write's ``from`` would report a change from a value that only ever existed
    inside this transaction.

    Keying by label is also what makes the reported count right: two ``devices``
    entries whose refs differ only in case produce ONE row, because labels are
    upper-cased, and must be counted once.
    """
    existing = deltas.get(delta["node_label"])
    if existing is None:
        deltas[delta["node_label"]] = delta
        return
    existing["to"] = delta["to"]
    existing["node_type"] = delta["node_type"]


# ---------------------------------------------------------------------------
# Public: do_author_device_topology
# ---------------------------------------------------------------------------


async def do_author_device_topology(
    conn: asyncpg.Connection,  # type: ignore[type-arg]
    namespace_id: str | UUID,
    *,
    design_id: str,
    devices: list[dict[str, Any]],
    connections: list[dict[str, Any]] | None = None,
    racks: list[dict[str, Any]] | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Author device-topology nodes/edges + capability attributes for a DESIGN.

    Writes DEVICE / PORT / RACK / CABLE nodes as kg_nodes hung off the existing
    DESIGN node.  Capability attributes are written to
    ``system_design_device_capabilities``.  Signal-path connections become
    ``PORT -[connected_to]-> PORT`` edges.

    Parameters
    ----------
    conn:
        asyncpg connection with RLS namespace GUC already set.
    namespace_id:
        Active namespace UUID.
    design_id:
        The DESIGN node id this topology belongs to.  The DESIGN node must
        already exist (authored by Phase-1 graph.py).
    devices:
        List of device dicts::

            {
                "device_ref": str,          # unique within design
                "capability": {             # AVIXA Revit param fields (all optional)
                    "device_category": str,
                    "manufacturer": str,
                    "model_number": str,
                    "power_draw_watts": float,
                    "heat_btu_hr": float,
                    "redundancy_role": "primary"|"secondary"|"standalone"|None,
                    "extra": dict,
                },
                "ports": [                  # optional list of PORT dicts
                    {
                        "port_ref": str,
                        "capability": {
                            "signal_format": str,   # e.g. "HDMI", "Dante", "DP"
                            "signal_version": str,  # e.g. "2.1", "2.0"
                            "port_direction": "input"|"output"|"bidirectional",
                            "poe_class": int|None,
                            "poe_watts": float|None,
                            "dante_rx_channels": int|None,
                            "dante_tx_channels": int|None,
                        },
                    },
                    ...
                ],
                "rack_ref": str | None,     # optional — which rack this device mounts in
                "status": str | None,       # W16 lifecycle (NEW node -> 'planned')
                "revision": str | None,     # W16, inert storage
                "salience": float | None,   # W16, finite and non-negative
            }

        ``status`` / ``revision`` / ``salience`` reach
        ``system_design_node_state`` **only when the device is new to this call
        or one of the three is supplied**.  A device that already existed and
        names none of them keeps having no state row at all.  A new device that
        names no ``status`` is stored as ``'planned'``; a pre-existing device
        that supplies only ``revision`` gets a row whose ``status`` is NULL —
        data held, no lifecycle declared.  An explicit ``null`` says nothing and
        is read as absence.  The vocabulary is enforced by the database, per
        node type.  A device takes them at the TOP LEVEL only: putting one
        inside ``capability`` or ``geometry`` is REFUSED, because those
        objects store their own fields and the lifecycle key would be dropped.
        A device's ``ports`` take NONE of the three, in any casing or prefix,
        at the top level or nested — PORT carries no lifecycle status, and a
        port that carries one is REFUSED rather than silently ignored.

    connections:
        Optional list of port-to-port signal connections::

            {
                "from_device_ref": str,
                "from_port_ref": str,
                "to_device_ref": str,
                "to_port_ref": str,
                "confidence": float,        # default 1.0
                "cable_ref": str | None,    # optional cable label; when set,
                                            # BOTH ports get a uses_cable edge
                "cable_status": str | None,     # W16 — the CABLE NODE's status
                "cable_revision": str | None,   # W16, inert storage
                "cable_salience": float | None, # W16
            }

        The ``cable_``-prefixed spelling is deliberate and matches
        ``cable_geometry``: a connection is an EDGE, and a bare ``status``
        there would read as the edge's own status — which is why an unprefixed
        lifecycle key on a connection is REFUSED rather than accepted and
        dropped.  These keys describe the CABLE NODE, they are shape-validated
        whether or not the connection names a ``cable_ref``, and supplying one
        WITHOUT a ``cable_ref`` is refused: there is no node for them to
        describe.

    racks:
        Optional list of rack dicts::

            {
                "rack_ref": str,
                "capability": { ... },  # same AVIXA param shape
                "status": str | None,       # W16 lifecycle (NEW node -> 'planned')
                "revision": str | None,     # W16, inert storage
                "salience": float | None,   # W16, finite and non-negative
            }

        Same rule as ``devices``: a rack that already existed and names none of
        the three keeps having no state row.

    source_id:
        Optional system_design source record ID for retirement tracking.

    Returns
    -------
    dict
        ``{"authored": {"nodes", "edges", "capabilities", "state"},
        "state_changes": [...]}``.

        ``state`` is the number of DISTINCT node labels whose lifecycle row this
        call wrote — distinct, because one payload can name the same node twice
        and two ``devices`` entries whose refs differ only in case collapse onto
        one row.  It is ``len(state_changes)`` by construction, so the number
        cannot drift from the rows it claims to describe; the round-1 per-item
        counter could, and that wrong number landed in the WORM audit event.

        ``state_changes`` is the per-node delta — ``{"node_label",
        "node_type", "from", "to", "state_row_created", "resurrected"}`` per
        entry, sorted by label.  The adapter puts it in the authoring audit
        event: on the one wave whose purpose is to gate a destructive
        operation, counts alone cannot answer *which* node became retirable,
        *from what*, and (with ``actor``) *by whom*.

    Raises:
        ValueError: a lifecycle key is present with the wrong type or a
            non-finite/negative ``salience``; a lifecycle key appears on a
            bucket that does not accept it (any casing, prefixed or not); a
            ``cable_*`` key appears on a connection naming no ``cable_ref``; or
            the database refused the row's status vocabulary.
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    conn_list: list[dict[str, Any]] = connections or []
    rack_list: list[dict[str, Any]] = racks or []

    node_count = 0
    edge_count = 0
    cap_count = 0
    #: Lifecycle deltas, keyed by node label so a node named twice in one
    #: payload is one entry — see :func:`_merge_state_delta`.  ``len`` of this
    #: is the reported ``state`` count, which is why the count cannot drift
    #: from the rows.
    state_deltas: dict[str, dict[str, Any]] = {}

    design_lbl = f"DESIGN:{design_id.upper()}"

    # ------------------------------------------------------------------
    # 0. Validation, over the CALLER'S lists in the CALLER'S order.
    #
    # The four write loops below iterate SORTED copies (D2 - canonical write
    # order, see the note on loop 1), but a caller who sent two bad items must
    # still be told about the first one THEY sent, not the one that happens to
    # sort lowest.  So every raising check runs here first, in receipt order.
    # All of them are pure, so the write loops keep calling them in place
    # rather than being restructured around this pass.
    # ------------------------------------------------------------------
    for raw_rack in rack_list:
        _refuse_misplaced_lifecycle_keys(raw_rack, accepted=_RACK_STATE_KEYS, where="a rack")
    for raw_dev in devices:
        _refuse_misplaced_lifecycle_keys(raw_dev, accepted=_DEVICE_STATE_KEYS, where="a device")
        for raw_port in raw_dev.get("ports", []):
            _refuse_misplaced_lifecycle_keys(raw_port, accepted=_PORT_STATE_KEYS, where="a port")
    for raw_cnx in conn_list:
        _refuse_misplaced_lifecycle_keys(
            raw_cnx, accepted=_CONNECTION_STATE_KEYS, where="a connection"
        )
        port_label(design_id, raw_cnx["from_device_ref"], raw_cnx["from_port_ref"])
        port_label(design_id, raw_cnx["to_device_ref"], raw_cnx["to_port_ref"])
        _connection_confidence(raw_cnx)
        if _has_explicit_lifecycle(_state_of(raw_cnx, _CABLE_PREFIX)) and not raw_cnx.get(
            "cable_ref"
        ):
            raise ValueError(
                "cable_status / cable_revision / cable_salience describe the CABLE node; "
                "supply cable_ref on the connection, or remove them"
            )

    # ------------------------------------------------------------------
    # 1. Racks (optional — write before devices so mounted_in edges resolve)
    # ------------------------------------------------------------------
    # D2: iterate in CANONICAL LABEL order, never the caller's list order.  Row
    # locks on kg_nodes/kg_edges are taken in iteration order, so two concurrent
    # authoring calls sending the same set in opposite orders formed a wait cycle
    # (12 deadlocks in 12 attempts).  Sorting on the label makes the acquisition
    # order a pure function of the RESOURCE IDENTITIES and not of the request -
    # the M11 Inventory pattern, nce/vertical_modules/inventory/stock.py
    # ``_canonical_lock_order``.  sorted() is stable, so equal labels keep
    # receipt order.  The four loops keep their relative order: racks -> devices
    # -> ports -> connections is a dependency order, not a lock order.
    for rack in sorted(rack_list, key=lambda r: rack_label(design_id, r["rack_ref"])):
        _refuse_misplaced_lifecycle_keys(rack, accepted=_RACK_STATE_KEYS, where="a rack")
        rack_state = _state_of(rack)
        rack_ref: str = rack["rack_ref"]
        rack_lbl = rack_label(design_id, rack_ref)

        rack_is_new = await _upsert_node(conn, ns_uuid, rack_lbl, _NODE_TYPE_RACK, source_id)
        node_count += 1

        await _upsert_edge(
            conn, ns_uuid, design_lbl, _PRED_HAS_RACK, rack_lbl, _STRUCTURAL_CONFIDENCE, source_id
        )
        edge_count += 1

        rack_cap = rack.get("capability", {})
        if rack_cap:
            await _upsert_capability(conn, ns_uuid, rack_lbl, rack_cap)
            cap_count += 1

        # W16 lifecycle state — NEW node, or the caller said something.  A rack
        # that already existed and was re-authored in silence keeps no row.
        if _should_record_state(rack_state, node_is_new=rack_is_new):
            _merge_state_delta(
                state_deltas,
                await _upsert_node_state(
                    conn,
                    ns_uuid,
                    rack_lbl,
                    _NODE_TYPE_RACK,
                    rack_state,
                    node_is_new=rack_is_new,
                ),
            )

    # ------------------------------------------------------------------
    # 2. Devices + their ports
    # ------------------------------------------------------------------
    for dev in sorted(devices, key=lambda d: device_label(design_id, d["device_ref"])):
        _refuse_misplaced_lifecycle_keys(dev, accepted=_DEVICE_STATE_KEYS, where="a device")
        dev_state = _state_of(dev)
        dev_ref: str = dev["device_ref"]
        dev_lbl = device_label(design_id, dev_ref)

        dev_is_new = await _upsert_node(conn, ns_uuid, dev_lbl, _NODE_TYPE_DEVICE, source_id)
        node_count += 1

        # DESIGN -[contains]-> DEVICE
        await _upsert_edge(
            conn, ns_uuid, design_lbl, _PRED_CONTAINS, dev_lbl, _STRUCTURAL_CONFIDENCE, source_id
        )
        edge_count += 1

        # Device capability attributes.
        dev_cap = dev.get("capability", {})
        if dev_cap:
            await _upsert_capability(conn, ns_uuid, dev_lbl, dev_cap)
            cap_count += 1

        # W16 lifecycle state — see the RACK block above.
        if _should_record_state(dev_state, node_is_new=dev_is_new):
            _merge_state_delta(
                state_deltas,
                await _upsert_node_state(
                    conn,
                    ns_uuid,
                    dev_lbl,
                    _NODE_TYPE_DEVICE,
                    dev_state,
                    node_is_new=dev_is_new,
                ),
            )

        # Rack mounting edge (optional).
        rack_ref_str: str | None = dev.get("rack_ref")
        if rack_ref_str:
            r_lbl = rack_label(design_id, rack_ref_str)
            await _upsert_edge(
                conn, ns_uuid, dev_lbl, _PRED_MOUNTED_IN, r_lbl, _STRUCTURAL_CONFIDENCE, source_id
            )
            edge_count += 1

        # Ports.
        for port in sorted(
            dev.get("ports", []),
            key=lambda p: port_label(design_id, dev_ref, p["port_ref"]),
        ):
            # A PORT has no lifecycle status anywhere in NetBox, so it accepts
            # NONE of the keys — in any spelling, prefixed or not.
            _refuse_misplaced_lifecycle_keys(port, accepted=_PORT_STATE_KEYS, where="a port")
            port_ref: str = port["port_ref"]
            port_lbl = port_label(design_id, dev_ref, port_ref)

            await _upsert_node(conn, ns_uuid, port_lbl, _NODE_TYPE_PORT, source_id)
            node_count += 1

            # DEVICE -[has_port]-> PORT
            await _upsert_edge(
                conn, ns_uuid, dev_lbl, _PRED_HAS_PORT, port_lbl, _STRUCTURAL_CONFIDENCE, source_id
            )
            edge_count += 1

            # Port capability attributes.
            port_cap = port.get("capability", {})
            if port_cap:
                await _upsert_capability(conn, ns_uuid, port_lbl, port_cap)
                cap_count += 1

    # ------------------------------------------------------------------
    # 3. Signal connections (PORT -[connected_to]-> PORT)
    # ------------------------------------------------------------------
    # D2, and the half that matters most: _upsert_edge writes kg_edges rows keyed
    # by (subject, predicate, object), so ordering the NODE loops alone is not a
    # fix - the wait cycle simply relocates onto kg_edges.  The key is therefore
    # the EDGE IDENTITY, the (from_port_lbl, to_port_lbl) pair, not the
    # connection dict's position in the caller's list.
    for cnx in sorted(
        conn_list,
        key=lambda c: (
            port_label(design_id, c["from_device_ref"], c["from_port_ref"]),
            port_label(design_id, c["to_device_ref"], c["to_port_ref"]),
        ),
    ):
        # Validated UNCONDITIONALLY, before the cable_ref branch below decides
        # whether a CABLE node exists at all.  Reached only inside that branch,
        # a malformed cable_salience on a connection naming no cable returned
        # 200 with the value unexamined and discarded.
        _refuse_misplaced_lifecycle_keys(cnx, accepted=_CONNECTION_STATE_KEYS, where="a connection")
        cable_state = _state_of(cnx, _CABLE_PREFIX)
        from_port_lbl = port_label(design_id, cnx["from_device_ref"], cnx["from_port_ref"])
        to_port_lbl = port_label(design_id, cnx["to_device_ref"], cnx["to_port_ref"])
        cnx_conf: float = _connection_confidence(cnx)
        cnx_source: str | None = cnx.get("source_id") or source_id

        # Optional cable reference.
        cable_ref_str: str | None = cnx.get("cable_ref")
        if _has_explicit_lifecycle(cable_state) and not cable_ref_str:
            # Refused rather than dropped: these keys describe the CABLE NODE,
            # and without a cable_ref there is no node for them to describe.
            raise ValueError(
                "cable_status / cable_revision / cable_salience describe the CABLE node; "
                "supply cable_ref on the connection, or remove them"
            )

        await _upsert_edge(
            conn, ns_uuid, from_port_lbl, _PRED_CONNECTED_TO, to_port_lbl, cnx_conf, cnx_source
        )
        edge_count += 1

        if cable_ref_str:
            cable_lbl = cable_label(design_id, cable_ref_str)
            cable_is_new = await _upsert_node(
                conn, ns_uuid, cable_lbl, _NODE_TYPE_CABLE, cnx_source
            )
            node_count += 1
            # W16 lifecycle state for the CABLE NODE.  The keys are read with
            # the ``cable_`` prefix off the CONNECTION dict.  A cable that
            # already existed and carries no cable_* keys keeps no row — which
            # is what stops 67f's "backfill by re-author" data-fix from
            # stamping 'planned' on physically-installed cable across the
            # estate (see the module docstring).
            if _should_record_state(cable_state, node_is_new=cable_is_new):
                _merge_state_delta(
                    state_deltas,
                    await _upsert_node_state(
                        conn,
                        ns_uuid,
                        cable_lbl,
                        _NODE_TYPE_CABLE,
                        cable_state,
                        node_is_new=cable_is_new,
                    ),
                )
            # A cable is two-ended: both terminations point at the CABLE node so
            # it is traversable from either port (Batch 067f — see module
            # docstring).  Re-authoring upserts on the kg_edges UNIQUE
            # constraint, so this also backfills pre-wave one-ended rows.
            for termination_lbl in (from_port_lbl, to_port_lbl):
                await _upsert_edge(
                    conn,
                    ns_uuid,
                    termination_lbl,
                    _PRED_USES_CABLE,
                    cable_lbl,
                    _STRUCTURAL_CONFIDENCE,
                    cnx_source,
                )
                edge_count += 1

    state_changes = [state_deltas[label] for label in sorted(state_deltas)]
    log.info(
        "do_author_device_topology: ns=%s design=%s nodes=%d edges=%d capabilities=%d state=%d",
        ns_uuid,
        design_id,
        node_count,
        edge_count,
        cap_count,
        len(state_changes),
    )
    return {
        "authored": {
            "nodes": node_count,
            "edges": edge_count,
            "capabilities": cap_count,
            "state": len(state_changes),
        },
        "state_changes": state_changes,
    }
