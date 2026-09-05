"""
tests/test_system_design_node_state.py
======================================
Module 6 Wave 16 — per-node LIFECYCLE STATE (``system_design_node_state``,
migration 061): ``status`` / ``revision`` / ``salience`` for a DEVICE, a RACK
or a CABLE.

The filename matches the ``tests/test_system_design_*.py`` CI glob B067a wired
(``.github/workflows/ci.yml``), so this file runs in CI with no workflow edit.
Any other filename would need a ``ci.yml`` change, which is a scope change.

What these tests actually gate
------------------------------
1. **THE ONE-WAY DOOR: a row means somebody declared something.**  A state row
   is written only when the node is genuinely NEW to the authoring call, or the
   caller supplied a lifecycle key.  A pre-existing node re-authored in silence
   keeps having **no row**, and W17's retirement guard denies on that absence —
   permanently, not merely until the next canvas save.  Round 1 wrote a row for
   every node it touched, which turned an ordinary save, a geometry-only drag
   and 67f's "backfill by re-author" data-fix into a mass mint of ``'planned'``
   over legacy as-built equipment.  Each of those three paths has a test here.

2. **Nothing backfills, and that is gated for real.**  Round 1's version of
   this claim could not see a backfill at all: it built its legacy node *after*
   the migration had run, so an auditor appended a genuine
   ``INSERT … SELECT … FROM kg_nodes … 'planned'`` to migration 061 and the
   suite stayed green.  :class:`TestNoBackfill` RE-APPLIES the migration file
   against a namespace that already holds legacy nodes and asserts the table
   stays empty, and separately asserts the file's executable text has no
   ``INSERT``.

3. **The status CHECK is COMPOSITE, per ``node_type`` — not a union.**  A union
   CHECK (``status IN (<every value from every type>)``) would accept a CABLE
   whose status is ``'inventory'`` and a DEVICE whose status is ``'connected'``,
   and would pass any test that only proves the *right* values are accepted.
   Every node type therefore gets a rejection test against **another type's
   whole vocabulary**, and the acceptance sibling exists so a CHECK of ``FALSE``
   — which rejects everything — cannot pass the rejection tests either.

4. **Deny by default on ``node_type``.**  The CASE's ``ELSE FALSE`` refuses an
   unknown ``node_type``, PORT included: NetBox has no lifecycle status for a
   port and none is invented here.

5. **``salience`` is finite and non-negative, in Python AND in the database.**
   PostgreSQL ``numeric`` NaN is not IEEE NaN — it compares GREATER than every
   finite value — so a stored NaN sorts as the largest salience in the tenant
   and silently flips any W17 threshold predicate.  ``NaN`` and ``Infinity``
   are ``float`` instances and sail through a bare ``isinstance`` check, and
   ``Request.json`` accepts a bare ``NaN`` on the wire, so the guard is tested
   through the **public surface** and not only through the private helper.

6. **A misplaced lifecycle key is refused, not dropped.**  An unprefixed
   ``status`` on a connection, a ``cable_*`` key on a device or a port, any key
   on a port, a ``cable_*`` key with no ``cable_ref``, and casing variants.

7. **Owner-pool tenant isolation on the WRITE.**  ``nce_app`` serves no request
   in this deployment, so every request runs on a pool that ``FORCE ROW LEVEL
   SECURITY`` does not constrain.  What isolates tenants here is the explicit
   ``namespace_id`` in ``_upsert_node_state``'s ``INSERT`` values, in its
   ``ON CONFLICT`` target and in its prior-status probe — and in the existence
   probe that decides newness.  The two tenants below collide on **every
   identifier** — same design_id, same device/rack/cable refs, therefore
   byte-identical node labels — and differ **only in content**.  A fixture that
   gave the two tenants different labels could not detect a predicate that
   filters by label, and B067b failed TAG on exactly that.

   This wave adds no READ query: the read join and the live ``statuses`` filter
   are B067g2's, and ``read.py`` is not this wave's file.  So the isolation
   asserted here is write-side, and the reads below are the test's own SQL.

The per-predicate mutation table (one row per predicate, no grouped results) is
in the wave report.  Every row was produced by mutating a single predicate in a
scratchpad COPY of the tree — never in the tree itself — and asserting the edit
landed before running.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from nce.vertical_modules.system_design.devices import (
    DEFAULT_NODE_STATUS,
    _connection_confidence,
    _has_explicit_lifecycle,
    _is_lifecycle_spelling,
    _refuse_misplaced_lifecycle_keys,
    _should_record_state,
    _state_of,
    cable_label,
    device_label,
    port_label,
    rack_label,
)

# ---------------------------------------------------------------------------
# The contractual vocabulary.  NetBox's, per node type, and Copper follows it
# as a binding ADR.
#
# It is spelled here — in the TEST — and deliberately not in devices.py: the
# one definition the write path is checked against is the CHECK in the DDL, and
# a second copy in the module under test would make the test agree with the
# module by construction rather than with the contract.
# ---------------------------------------------------------------------------

_DEVICE_STATUSES = (
    "planned",
    "staged",
    "active",
    "offline",
    "decommissioning",
    "inventory",
    "failed",
)
_CABLE_STATUSES = ("planned", "connected", "decommissioning")
_RACK_STATUSES = ("reserved", "available", "planned", "active", "deprecated")

_VOCABULARY: dict[str, tuple[str, ...]] = {
    "DEVICE": _DEVICE_STATUSES,
    "CABLE": _CABLE_STATUSES,
    "RACK": _RACK_STATUSES,
}

#: For each node type, the values that belong to some OTHER type and to no
#: legal value of this one.  These are what a union CHECK would wrongly accept.
_FOREIGN_ONLY: dict[str, tuple[str, ...]] = {
    node_type: tuple(
        sorted(
            {
                value
                for other, values in _VOCABULARY.items()
                if other != node_type
                for value in values
            }
            - set(own)
        )
    )
    for node_type, own in _VOCABULARY.items()
}

#: Accepted lifecycle spellings per bucket, mirrored from devices.py so the
#: nested-object tests can name them without importing four privates.
_DEV_KEYS: frozenset[str] = frozenset({"status", "revision", "salience"})
_CNX_KEYS: frozenset[str] = frozenset({"cable_status", "cable_revision", "cable_salience"})

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = _REPO_ROOT / "nce" / "migrations" / "061_system_design_node_state.sql"
_SCHEMA_PATH = _REPO_ROOT / "nce" / "schema.sql"
_TABLE_MARKER = "CREATE TABLE IF NOT EXISTS system_design_node_state ("

# ---------------------------------------------------------------------------
# Fixture data.
#
# EVERY identifier below is shared by both tenants.  Only content differs.  See
# the module docstring for why that is not a stylistic choice.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W16-STATE-001"
_NS_SLUG = "w16-state"
_SITE = "SiteState"
_BUILDING = "BuildingState"
_FLOOR = "FloorState"
_ROOM = "RoomState"

_DEVICE_REF = "SW-W16"
_PORT_REF = "ETH-1"
_RACK_REF = "RACK-W16"
_CABLE_REF = "CBL-W16"

_DEVICE_LABEL = device_label(_DESIGN_ID, _DEVICE_REF)
_PORT_LABEL = port_label(_DESIGN_ID, _DEVICE_REF, _PORT_REF)
_RACK_LABEL = rack_label(_DESIGN_ID, _RACK_REF)
_CABLE_LABEL = cable_label(_DESIGN_ID, _CABLE_REF)

#: A DEVICE that predates this wave.  Authored straight into ``kg_nodes`` with
#: its ``contains`` edge, which is what a pre-W16 author left behind: node and
#: edge, and no state row, because there was no table.
_LEGACY_REF = "LEGACY-ASBUILT"
_LEGACY_DEVICE_LABEL = device_label(_DESIGN_ID, _LEGACY_REF)

#: A CABLE that predates this wave — the subject of the 67f re-author data-fix.
_LEGACY_CABLE_REF = "LEGACY-CABLE"
_LEGACY_CABLE_LABEL = cable_label(_DESIGN_ID, _LEGACY_CABLE_REF)

_TOPOLOGY_TOOL = "system_design_author_topology"
_FL_TOOL = "system_design_author_functional_location"
_READ_TOOL = "system_design_get_topology"

#: Per-tenant CONTENT.  Every one of these differs between the two tenants and
#: none of them is an identifier, so a namespace predicate that went missing
#: shows up as the wrong VALUE under the right key.
_TENANT_STATE: dict[str, dict[str, Any]] = {
    "ALPHA": {"status": "staged", "revision": "ALPHA-REV-7", "salience": 0.25},
    "BETA": {"status": "offline", "revision": "BETA-REV-91", "salience": 0.75},
}
_TENANT_RACK_STATUS = {"ALPHA": "reserved", "BETA": "deprecated"}
_TENANT_CABLE_STATUS = {"ALPHA": "connected", "BETA": "decommissioning"}


class _NullCacheRedis:
    """Never a cache hit, but ``incr`` exists.

    ``mcp_stdio_dispatch`` calls ``bump_cache_generation`` unguarded after every
    successful ``mutation=True`` tool, so a ``None`` client turns every
    authoring call into ``McpError(-32603)``.  Never returning a hit means every
    read-back here proves the query ran rather than that a payload was replayed.
    """

    def __init__(self) -> None:
        self.generation = 0

    async def get(self, key: str) -> None:
        return None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        return None

    async def incr(self, key: str) -> int:
        self.generation += 1
        return self.generation


class _EngineStub:
    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.redis_client = _NullCacheRedis()


def _fl_args(ns_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "namespace_slug": _NS_SLUG,
        "design_id": _DESIGN_ID,
        "site_name": _SITE,
        "buildings": [
            {
                "name": _BUILDING,
                "floors": [{"name": _FLOOR, "rooms": [{"name": _ROOM, "positions": []}]}],
            }
        ],
    }
    args.update(overrides)
    return args


def _topology_args(ns_id: uuid.UUID, tag: str | None = None, **overrides: Any) -> dict[str, Any]:
    """The authoring bag.  ``tag=None`` supplies NO lifecycle state at all."""
    device: dict[str, Any] = {
        "device_ref": _DEVICE_REF,
        "ports": [{"port_ref": _PORT_REF, "capability": {"port_direction": "input"}}],
        "rack_ref": _RACK_REF,
    }
    rack: dict[str, Any] = {"rack_ref": _RACK_REF}
    connection: dict[str, Any] = {
        "from_device_ref": _DEVICE_REF,
        "from_port_ref": _PORT_REF,
        "to_device_ref": _DEVICE_REF,
        "to_port_ref": _PORT_REF,
        "cable_ref": _CABLE_REF,
    }
    if tag is not None:
        device.update(_TENANT_STATE[tag])
        rack.update(dict(_TENANT_STATE[tag], status=_TENANT_RACK_STATUS[tag]))
        connection.update(
            {
                "cable_status": _TENANT_CABLE_STATUS[tag],
                "cable_revision": _TENANT_STATE[tag]["revision"],
                "cable_salience": _TENANT_STATE[tag]["salience"],
            }
        )

    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": _DESIGN_ID,
        "devices": [device],
        "connections": [connection],
        "racks": [rack],
    }
    args.update(overrides)
    return args


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    from nce.auth import set_namespace_context
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)


async def _dispatch(engine: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call *tool* through the real MCP dispatch path and return its payload."""
    from nce.mcp_stdio_dispatch import execute_call_tool

    parts = await execute_call_tool(engine, tool, arguments)
    assert parts, f"dispatch returned no content for {tool}"
    return json.loads(parts[0].text)


async def _dispatch_ok(engine: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload = await _dispatch(engine, tool, arguments)
    assert "error" not in payload, f"{tool} returned an error envelope: {payload}"
    return payload


async def _state_rows(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """``{node_label: row}`` for one namespace — the test's own read.

    This wave adds no read query (``read.py`` is B067g2's), so the namespace
    predicate below is the TEST's, not the code's.  It is here so the test can
    see one tenant at a time; the predicates actually under test are the ones
    in ``_upsert_node_state``'s ``INSERT``, ``ON CONFLICT`` and prior probe.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT node_label, node_type, status, revision, salience
            FROM system_design_node_state
            WHERE namespace_id = $1::uuid
            """,
            str(ns_id),
        )
    return {r["node_label"]: dict(r) for r in rows}


async def _authoring_events(pg_pool: Any, ns_id: uuid.UUID, tool: str) -> list[dict[str, Any]]:
    """``system_design_authored`` event params for *tool*, oldest first."""
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT params FROM event_log
            WHERE namespace_id = $1::uuid
              AND event_type = 'system_design_authored'
            ORDER BY event_seq
            """,
            str(ns_id),
        )
    events = [
        json.loads(r["params"]) if isinstance(r["params"], str) else dict(r["params"]) for r in rows
    ]
    return [event for event in events if event.get("tool") == tool]


async def _insert_pre_wave_node(
    pg_pool: Any,
    ns_id: uuid.UUID,
    label: str,
    entity_type: str,
    *,
    predicate: str,
) -> None:
    """Author a node the way everything before W16 did: kg_nodes + its edge.

    The EDGE matters, and its absence is why round 1's read-surface test was
    doubly dead: ``read.py`` walks out from the DESIGN label, so a node with no
    edge into the design is unreachable and the read can never return it,
    whatever the join does.  A real pre-wave author wrote both.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, $2, $3::uuid, 'sync')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            entity_type,
            str(ns_id),
        )
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence,
                 namespace_id, change_origin)
            VALUES ($1, $2, $3, 1.0, $4::uuid, 'sync')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            f"DESIGN:{_DESIGN_ID}",
            predicate,
            label,
            str(ns_id),
        )


async def _insert_state_row(
    conn: Any,
    ns_id: uuid.UUID,
    node_label: str,
    node_type: str | None,
    status: str | None,
) -> None:
    """Raw INSERT used by the CHECK tests — deliberately not through the writer.

    The constraint has to hold against ``psql`` and against every future writer,
    not only against the one function this wave adds.
    """
    await conn.execute(
        """
        INSERT INTO system_design_node_state
            (namespace_id, node_label, node_type, status)
        VALUES ($1::uuid, $2, $3, $4)
        """,
        str(ns_id),
        node_label,
        node_type,
        status,
    )


def _executable_sql(text: str) -> str:
    """Return *text* with comments and string-literal CONTENTS removed.

    Migration 061's header discusses the very shape it forbids ("a backfill
    here -- INSERT ... SELECT ... FROM kg_nodes"), so a naive scan of the file
    reports the documentation as the defect.

    Round 3 stripped only WHOLE-LINE ``--`` comments, which left two traps for
    the next migration wave:

    * a TRAILING comment (``);  -- INSERT INTO ... would be a backfill``) is not
      at the start of its line, so it survived into the scanned text;
    * a ``COMMENT ON TABLE ... IS '...'`` literal is the normal place to put
      schema documentation, and this table's prose is a strong candidate for
      being moved there one day.  A single sentence mentioning
      ``INSERT INTO system_design_node_state`` inside such a literal would have
      turned the backfill gate permanently RED against correct code.

    Both are handled by walking the text once: ``--`` starts a comment only
    OUTSIDE a string literal, and a literal's body is dropped while its quotes
    are kept, so statement structure (and the ``;`` splitting that
    ``schema-whole`` relies on) survives.  ``''`` inside a literal is SQL's
    escaped apostrophe and does not end it — migration 061's comments use it
    heavily.

    DISCLOSED: the ``''`` branch is NOT gated, and I could not construct an
    input where removing it changes the result.  Without it a doubled quote
    simply closes one literal and opens the next, so the text between stays
    inside SOME literal region and is dropped either way; only the emitted quote
    characters differ, and nothing asserts on those.  Mutating it away leaves
    the suite green.  It is kept because it makes the tokenizer say what SQL
    actually means rather than rely on that parity argument holding for every
    future input — the same call, and the same disclosure, as the ``status IS
    NULL`` disjunct in migration 061.
    """
    out: list[str] = []
    i = 0
    end = len(text)
    in_literal = False
    while i < end:
        char = text[i]
        if in_literal:
            if char != "'":
                i += 1  # literal body: dropped
                continue
            if text.startswith("''", i):
                i += 2  # SQL's escaped apostrophe — still inside the literal
                continue
            in_literal = False
            out.append("'")
            i += 1
            continue
        if char == "'":
            in_literal = True
            out.append("'")
            i += 1
            continue
        if text.startswith("--", i):
            newline = text.find("\n", i)
            if newline == -1:
                break
            i = newline  # keep the newline itself on the next pass
            continue
        out.append(char)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 0. Unit — shape guards, the refusal, and the one-way-door predicate.
#    No database.
# ---------------------------------------------------------------------------


def test_the_default_status_is_planned() -> None:
    """The seed for a GENUINELY NEW node, and the only place the value exists.

    Migration 061 deliberately gives the column no ``DEFAULT``: a column default
    would be a second, independent source of the one dangerous value, mintable
    by any writer or data-fix that never touches ``devices.py``.  So this
    constant is not "the same as the DDL"; it is the sole definition, and
    ``TestCompositeStatusCheck::test_the_status_column_has_no_default`` pins the
    other half of that decision.
    """
    assert DEFAULT_NODE_STATUS == "planned"


def test_state_of_reads_the_unprefixed_keys() -> None:
    assert _state_of({"status": "active", "revision": "r1", "salience": 3}) == {
        "status": "active",
        "revision": "r1",
        "salience": 3,
    }


def test_state_of_reads_the_cable_prefixed_keys() -> None:
    """A connection is an EDGE; the prefix says these describe the CABLE NODE."""
    cnx = {"cable_status": "connected", "cable_revision": "r2", "cable_salience": 1.5}
    assert _state_of(cnx, "cable_") == {
        "status": "connected",
        "revision": "r2",
        "salience": 1.5,
    }


def test_state_of_treats_absent_and_null_alike() -> None:
    """A DECISION, not an oversight — and it carries the round-2 fix.

    Under migration 061's nullable ``status`` the two *could* be told apart.
    They must not be: JSON serialisers routinely emit explicit nulls for unset
    optional fields, so a client sending ``{"status": null, "revision": null,
    "salience": null}`` on every save would mint a row for every node on every
    save and re-open exactly the door round 2 closes.
    ``mcp_handlers.expected_version_of`` already reads an explicit ``null`` as
    absence, so this is the engine's established reading rather than a local
    invention.
    """
    silent = {"status": None, "revision": None, "salience": None}
    assert _state_of({}) == _state_of(silent)
    assert not _has_explicit_lifecycle(_state_of(silent))
    assert not _should_record_state(_state_of(silent), node_is_new=False)


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"status": 3}, "status"),
        ({"status": ""}, "status"),
        ({"status": "   "}, "status"),
        ({"status": ["active"]}, "status"),
        ({"revision": 7}, "revision"),
        ({"revision": ""}, "revision"),
        ({"salience": "high"}, "salience"),
        ({"salience": True}, "salience"),
        ({"salience": False}, "salience"),
        ({"salience": float("nan")}, "salience"),
        ({"salience": float("inf")}, "salience"),
        ({"salience": float("-inf")}, "salience"),
        # INTEGER literals, not floats. ``math.isfinite`` COERCES, and coercing
        # an int too large for a C double raises OverflowError — an
        # ArithmeticError, NOT a ValueError — so it escaped every
        # ``except ValueError`` on both surfaces and arrived as -32603 / HTTP
        # 500. ``json.loads`` yields arbitrary-precision ints, so this is
        # reachable from the wire. Round 2's acceptance test reached for "a
        # large finite salience" and wrote ``1e308``, a FLOAT literal, so the
        # integer path was never exercised at all.
        ({"salience": 10**309}, "salience"),
        ({"salience": 10**400}, "salience"),
        ({"salience": -(10**309)}, "salience"),
    ],
)
def test_state_of_refuses_a_wrong_shape(payload: dict[str, Any], field: str) -> None:
    """SHAPE is Python's job; VOCABULARY is the database's.

    ``salience: True`` is here because ``bool`` is an ``int`` subclass: without
    the explicit refusal a ``true`` on the wire becomes the salience ``1``,
    which is a plausible number and therefore invisible.

    The three non-finite floats are here because they are ``float`` instances
    and pass every ``isinstance`` check.  ``Request.json`` is a plain
    ``json.loads``, which accepts a bare ``NaN`` on the wire by default, so this
    is reachable from outside — and a stored NaN is not a rendering nuisance:
    PostgreSQL ``numeric`` NaN compares GREATER than every finite value, so it
    sorts as the largest salience in the tenant and silently flips any W17
    threshold predicate.
    """
    with pytest.raises(ValueError, match=field):
        _state_of(payload)


def test_state_of_accepts_zero_and_a_large_finite_salience() -> None:
    """The sibling of the refusal test.

    Without it, a guard that raised for EVERY salience — which also rejects NaN
    — would satisfy every row above.

    The boundary case is an INTEGER, deliberately.  Round 2 wrote ``1e308``
    here, a float literal, which meant the guard was never exercised on the
    int path where the overflow actually lives: ``isfinite(10**308)`` is fine
    and ``10**309`` raises, so ``10**308`` is exactly the largest value that
    must still be ACCEPTED.  A guard that simply refused all ints would pass a
    float-only acceptance test.
    """
    assert _state_of({"salience": 0})["salience"] == 0
    assert _state_of({"salience": 1e308})["salience"] == 1e308
    assert _state_of({"salience": 10**308})["salience"] == 10**308


@pytest.mark.parametrize(
    "key,expected",
    [
        ("status", True),
        ("Status", True),
        ("SALIENCE", True),
        ("revision", True),
        ("cable_status", True),
        ("Cable_Revision", True),
        ("CABLE_SALIENCE", True),
        ("cable_geometry", False),
        ("geometry", False),
        ("device_ref", False),
        ("capability", False),
        ("cable_ref", False),
    ],
)
def test_is_lifecycle_spelling(key: str, expected: bool) -> None:
    """Breadth is the point: this feeds a REFUSAL, so near-misses must be seen.

    ``cable_geometry`` is the control — stripping the prefix leaves
    ``geometry``, which is not a lifecycle key, and a guard that flagged it
    would refuse a valid W14 payload.
    """
    assert _is_lifecycle_spelling(key) is expected


@pytest.mark.parametrize(
    "payload,accepted,where",
    [
        ({"status": "active"}, frozenset(), "a port"),
        ({"cable_status": "connected"}, frozenset(), "a port"),
        ({"Salience": 1.0}, frozenset(), "a port"),
        (
            {"cable_status": "connected"},
            frozenset({"status", "revision", "salience"}),
            "a device",
        ),
        ({"STATUS": "active"}, frozenset({"status", "revision", "salience"}), "a device"),
        (
            {"status": "connected"},
            frozenset({"cable_status", "cable_revision", "cable_salience"}),
            "a connection",
        ),
        (
            {"Cable_Status": "connected"},
            frozenset({"cable_status", "cable_revision", "cable_salience"}),
            "a connection",
        ),
    ],
)
def test_a_misplaced_lifecycle_key_is_refused(
    payload: dict[str, Any], accepted: frozenset[str], where: str
) -> None:
    """A 200 that throws away what the caller sent is the defect, not the fix.

    Every row here was silently accepted-and-discarded before round 2.
    """
    with pytest.raises(ValueError, match="lifecycle key"):
        _refuse_misplaced_lifecycle_keys(payload, accepted=accepted, where=where)


@pytest.mark.parametrize(
    "payload,accepted",
    [
        ({"port_ref": "P1", "capability": {}, "geometry": {"x": 1}}, frozenset()),
        (
            {"device_ref": "D1", "status": "active", "geometry": {"x": 1}},
            frozenset({"status", "revision", "salience"}),
        ),
        (
            {"cable_ref": "C1", "cable_status": "connected", "cable_geometry": {"x": 1}},
            frozenset({"cable_status", "cable_revision", "cable_salience"}),
        ),
        # An explicit null says nothing, so a misplaced nothing is nothing.
        ({"port_ref": "P1", "status": None, "cable_salience": None}, frozenset()),
    ],
)
def test_a_correctly_placed_key_is_not_refused(
    payload: dict[str, Any], accepted: frozenset[str]
) -> None:
    """The sibling of the refusal test.

    Without it a guard that refused every payload would pass every row above,
    and a W14 ``geometry`` / ``cable_geometry`` payload would start 422-ing.
    """
    _refuse_misplaced_lifecycle_keys(payload, accepted=accepted, where="anywhere")


@pytest.mark.parametrize(
    "state,node_is_new,expected",
    [
        ({"status": None, "revision": None, "salience": None}, True, True),
        ({"status": None, "revision": None, "salience": None}, False, False),
        ({"status": "active", "revision": None, "salience": None}, False, True),
        # The one the correctness lens caught: "explicit STATUS or new" would
        # discard this revision silently, with a 200.
        ({"status": None, "revision": "REV-ONLY", "salience": None}, False, True),
        ({"status": None, "revision": None, "salience": 0.5}, False, True),
        # salience 0 is a value, not an absence.
        ({"status": None, "revision": None, "salience": 0}, False, True),
    ],
)
def test_should_record_state_truth_table(
    state: dict[str, Any], node_is_new: bool, expected: bool
) -> None:
    """The one-way door, enumerated.

    Row 2 is the door itself: a pre-existing node about which the caller said
    nothing gets NO row, so W17 keeps denying on it forever.
    """
    assert _should_record_state(state, node_is_new=node_is_new) is expected


def test_the_status_vocabulary_is_not_a_python_collection_in_the_write_path() -> None:
    """One definition of the legal values, and it is the DDL's.

    Narrowed from round 1, which grepped the module source and therefore failed
    on a COMMENT that merely mentioned a status — and whose "one definition"
    claim was untrue anyway, because ``mcp_stdio_tools.py`` carries a second
    copy in its advertised description that a sibling test REQUIRES.

    What actually matters is that no ``list``/``tuple``/``set`` of status
    literals exists in the write path for a writer to validate against instead
    of the constraint.  Parsing the AST sees only real collection literals, so
    prose, comments and docstrings cannot trip it.
    """
    from nce.vertical_modules.system_design import devices

    every_value = {v for values in _VOCABULARY.values() for v in values}
    tree = ast.parse(inspect.getsource(devices))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        literals = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        overlap = literals & every_value
        assert len(overlap) < 2, (
            f"devices.py holds a status vocabulary collection {sorted(overlap)}; "
            f"the CHECK in migration 061 is the one definition of the legal values"
        )


def test_the_tool_schema_documents_the_status_contract() -> None:
    """Copper reads the tool schema, not this repository.

    The three buckets carry the three vocabularies, and the schema must also say
    the thing a client would otherwise get wrong: that re-authoring an existing
    node records nothing.
    """
    from nce.mcp_stdio_tools import TOOLS

    (tool,) = [t for t in TOOLS if t.name == _TOPOLOGY_TOOL]
    props = tool.inputSchema["properties"]
    for bucket, values in (
        ("devices", _DEVICE_STATUSES),
        ("connections", _CABLE_STATUSES),
        ("racks", _RACK_STATUSES),
    ):
        description = props[bucket]["description"]
        for value in values:
            assert value in description, f"{bucket} description does not name status {value!r}"
    devices_doc = props["devices"]["description"]
    assert "ONLY FOR A DEVICE THIS CALL CREATES" in devices_doc
    assert "NON-NEGATIVE" in devices_doc
    assert "PORTS take" in devices_doc
    assert "cable_status" in props["connections"]["description"]


@pytest.mark.parametrize(
    "payload,accepted,where",
    [
        ({"device_ref": "D", "capability": {"status": "active"}}, _DEV_KEYS, "a device"),
        ({"device_ref": "D", "capability": {"revision": "R"}}, _DEV_KEYS, "a device"),
        ({"device_ref": "D", "capability": {"salience": 1.0}}, _DEV_KEYS, "a device"),
        ({"device_ref": "D", "geometry": {"status": "active"}}, _DEV_KEYS, "a device"),
        ({"port_ref": "P", "capability": {"status": "active"}}, frozenset(), "a port"),
        ({"port_ref": "P", "geometry": {"Cable_Status": "connected"}}, frozenset(), "a port"),
        (
            {"cable_ref": "C", "cable_geometry": {"salience": 2.0}},
            _CNX_KEYS,
            "a connection",
        ),
    ],
)
def test_a_lifecycle_key_inside_a_nested_object_is_refused(
    payload: dict[str, Any], accepted: frozenset[str], where: str
) -> None:
    """One nesting level down, and it used to be a silent 200.

    ``_refuse_misplaced_lifecycle_keys`` walked ``payload.items()`` and stopped.
    ``capability`` and ``geometry`` are plain dicts, nothing recursed, and
    ``_upsert_capability`` writes only ``_CAP_COLUMNS`` and drops the rest
    without a word.  Measured through real dispatch before the fix::

        devices: [{"device_ref": "X", "capability": {"status": "active"}}]
        -> HTTP 200, no error envelope
        -> system_design_node_state row: status = 'planned'

    The caller declared ``active`` and the row said the retirable value.

    This is NOT a re-opening of the one-way door — a pre-wave device carrying a
    nested key still mints no row, because the key never reaches
    ``_has_explicit_lifecycle``.  What it breaks is the weaker rule this engine
    enforces everywhere else: a write must not return 200 while throwing away
    what the caller sent.  The bare-connection-key version of exactly this was
    fixed one loop away in round 2.
    """
    with pytest.raises(ValueError, match="nested object"):
        _refuse_misplaced_lifecycle_keys(payload, accepted=accepted, where=where)


def test_a_nested_object_without_lifecycle_keys_is_not_refused() -> None:
    """The sibling: every real W12/W14 payload nests capability and geometry.

    A guard that refused any nested dict would 422 every caller this engine
    has.
    """
    _refuse_misplaced_lifecycle_keys(
        {
            "device_ref": "D",
            "status": "active",
            "capability": {"manufacturer": "ACME", "model_number": "X-1"},
            "geometry": {"x": 1.0, "y": 2.0, "meta": {"copper.room.w": 4.0}},
        },
        accepted=_DEV_KEYS,
        where="a device",
    )
    _refuse_misplaced_lifecycle_keys(
        {"port_ref": "P", "capability": {"port_direction": "input"}, "geometry": {"x": 1}},
        accepted=frozenset(),
        where="a port",
    )


@pytest.mark.parametrize(
    "key", ["cable_cable_status", "salience ", " Status", "CABLE_CABLE_REVISION"]
)
def test_the_pathological_spellings_are_also_refused(key: str) -> None:
    """ "Refused, not dropped" with two known exceptions is a habit, not a rule.

    Round 2 stripped at most one ``cable_`` prefix and did not strip
    whitespace, so ``cable_cable_status`` and ``"salience "`` were accepted and
    silently discarded.  The cost of a false positive here is a 422 telling the
    caller to fix a key name; the cost of a false negative is data loss with a
    200.
    """
    assert _is_lifecycle_spelling(key) is True


# ---------------------------------------------------------------------------
# 1. The table itself — the composite CHECK, the salience CHECK, the absent
#    column DEFAULT.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestCompositeStatusCheck:
    """The constraint is per ``node_type``.  A union CHECK is the wrong answer.

    Each rejection test below fires a whole FOREIGN vocabulary at one node type.
    A union CHECK would accept every one of them, which is the failure this
    class exists to catch; a CHECK of ``FALSE`` would pass every one of them,
    which is what the acceptance test exists to catch.
    """

    @pytest.mark.parametrize("node_type", sorted(_VOCABULARY))
    async def test_a_foreign_vocabulary_is_rejected(
        self, pg_pool: Any, make_namespace: Any, node_type: str
    ) -> None:
        from nce.db_utils import scoped_pg_session

        foreign = _FOREIGN_ONLY[node_type]
        assert foreign, f"{node_type} has no foreign-only values — the fixture is wrong"

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for bad in foreign:
                # Each attempt gets its own SAVEPOINT. Without it the first
                # CheckViolation aborts the surrounding transaction and every
                # later attempt fails with InFailedSQLTransactionError instead —
                # which pytest.raises would NOT catch, so the loop would report a
                # failure for the wrong reason and, worse, a weakened CHECK would
                # still look "rejected" from the second value on. pytest.raises
                # is OUTSIDE the savepoint on purpose: the exception has to
                # propagate out of conn.transaction() so it ROLLS BACK TO the
                # savepoint.
                with pytest.raises(
                    asyncpg.exceptions.CheckViolationError,
                    match="system_design_node_state_status_per_node_type",
                ):
                    async with conn.transaction():
                        await _insert_state_row(
                            conn, ns_id, f"{node_type}:CHECK:{bad}", node_type, bad
                        )

    async def test_a_cable_cannot_be_inventory(self, pg_pool: Any, make_namespace: Any) -> None:
        """Named on its own because it is the example in the wave brief.

        'inventory' is a DEVICE status. A cable in inventory is not a fact this
        engine can represent, and a union CHECK would store it.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await _insert_state_row(conn, ns_id, _CABLE_LABEL, "CABLE", "inventory")

    async def test_a_device_cannot_be_connected(self, pg_pool: Any, make_namespace: Any) -> None:
        """The other half of the brief's example. 'connected' is a CABLE status."""
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await _insert_state_row(conn, ns_id, _DEVICE_LABEL, "DEVICE", "connected")

    @pytest.mark.parametrize("node_type", sorted(_VOCABULARY))
    async def test_every_own_value_is_accepted(
        self, pg_pool: Any, make_namespace: Any, node_type: str
    ) -> None:
        """The sibling of the rejection tests.

        Without it, a CHECK of ``FALSE`` — which rejects everything, including
        every legal value — would satisfy every rejection test above.  A refusal
        test proves nothing about a constraint unless an acceptance test proves
        the constraint is not simply refusing everything.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for good in _VOCABULARY[node_type]:
                await _insert_state_row(conn, ns_id, f"{node_type}:OK:{good}", node_type, good)

        stored = await _state_rows(pg_pool, ns_id)
        assert {row["status"] for row in stored.values()} == set(_VOCABULARY[node_type])

    @pytest.mark.parametrize("node_type", sorted(_VOCABULARY))
    async def test_a_null_status_is_accepted_for_every_node_type(
        self, pg_pool: Any, make_namespace: Any, node_type: str
    ) -> None:
        """ "We hold data for this node; nobody declared its lifecycle."

        This is the third state, and a revision-only update on a pre-existing
        node produces it.  What it gates is that a NULL status is ACCEPTED for
        each of the three known node types — a CHECK spelled
        ``status IS NOT NULL AND status IN (...)`` rejects it and this test goes
        RED.

        It does NOT gate the explicit ``status IS NULL`` disjunct in the DDL, and
        an earlier version of this docstring wrongly claimed it did.  The mutation
        sweep removed the disjunct outright and the whole suite stayed GREEN,
        because ``NULL IN ('planned', ...)`` evaluates to NULL and a CHECK that
        evaluates to NULL PASSES — so the two spellings are semantically identical
        and no test can separate them.  The disjunct is kept for explicitness, and
        migration 061 records that it is documentation rather than enforcement.
        Claiming otherwise here would be the defect round 1 shipped: a failure
        message that promises more than the assertion delivers.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await _insert_state_row(conn, ns_id, f"{node_type}:NULLSTATUS", node_type, None)

        stored = await _state_rows(pg_pool, ns_id)
        assert stored[f"{node_type}:NULLSTATUS"]["status"] is None

    @pytest.mark.parametrize("node_type", ["PORT", "FL", "DESIGN", "DESIGN_LINE", "device", ""])
    async def test_an_unknown_node_type_is_denied(
        self, pg_pool: Any, make_namespace: Any, node_type: str
    ) -> None:
        """``ELSE FALSE``: deny by default, PORT included.

        'device' (lower case) is in the list because a CASE on a text column is
        case-sensitive and a writer that forgot to upper-case would otherwise
        get a row whose status nothing had validated.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for status in ("planned", "active", "connected", "reserved"):
                with pytest.raises(
                    asyncpg.exceptions.CheckViolationError,
                    match="system_design_node_state_status_per_node_type",
                ):
                    async with conn.transaction():
                        await _insert_state_row(
                            conn, ns_id, f"X:{node_type}:{status}", node_type, status
                        )

    async def test_an_unknown_node_type_is_denied_even_with_a_null_status(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """``status IS NULL`` must not become a way past ``ELSE FALSE``.

        A PORT row with no status is still a PORT row, and it would still make a
        node type that has no lifecycle look as though it had one.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await _insert_state_row(conn, ns_id, _PORT_LABEL, "PORT", None)

    async def test_node_type_cannot_be_null(self, pg_pool: Any, make_namespace: Any) -> None:
        """A CHECK that evaluates to NULL PASSES.

        ``node_type`` must therefore be NOT NULL, or a row with a NULL node_type
        slips past the CASE entirely and the vocabulary is unenforced for it.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(asyncpg.exceptions.NotNullViolationError):
                await _insert_state_row(conn, ns_id, "NULLTYPE", None, "planned")

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-0.0001", "-99999"])
    async def test_a_non_finite_or_negative_salience_is_refused_by_the_database(
        self, pg_pool: Any, make_namespace: Any, bad: str
    ) -> None:
        """The Python guard protects only the writers that go through it.

        NaN is the dangerous one, and it is dangerous in a way that is easy to
        get backwards: PostgreSQL ``numeric`` NaN is NOT IEEE NaN.  It compares
        EQUAL to itself and GREATER than every finite value, so it passes
        ``>= 0`` and is caught only by ``< Infinity`` — and a stored one would
        sort as the tenant's largest salience and silently flip any W17
        threshold predicate.
        """
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(
                asyncpg.exceptions.CheckViolationError,
                match="system_design_node_state_salience_finite_non_negative",
            ):
                await conn.execute(
                    """
                    INSERT INTO system_design_node_state
                        (namespace_id, node_label, node_type, salience)
                    VALUES ($1::uuid, $2, 'DEVICE', $3::numeric)
                    """,
                    str(ns_id),
                    f"DEVICE:SAL:{bad}",
                    bad,
                )

    async def test_a_finite_non_negative_salience_is_accepted(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The sibling: a CHECK of ``FALSE`` would pass every refusal above."""
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            for good in ("0", "0.5", "1", "999999.999"):
                await conn.execute(
                    """
                    INSERT INTO system_design_node_state
                        (namespace_id, node_label, node_type, salience)
                    VALUES ($1::uuid, $2, 'DEVICE', $3::numeric)
                    """,
                    str(ns_id),
                    f"DEVICE:SALOK:{good}",
                    good,
                )
        assert len(await _state_rows(pg_pool, ns_id)) == 4

    async def test_the_status_column_has_no_default(self, pg_admin_conn: Any) -> None:
        """The second, independent source of ``'planned'`` — removed, and pinned out.

        The deny-by-default lens found it: with ``DEFAULT 'planned'`` on the
        column, any future writer or manual data-fix doing
        ``INSERT … (namespace_id, node_label, node_type)`` mints a fully
        retirable row without a single review touching ``devices.py``.  The one
        remaining source of the value is ``devices.DEFAULT_NODE_STATUS``, which
        applies only to a genuinely new node.
        """
        row = await pg_admin_conn.fetchrow(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'system_design_node_state'
              AND column_name = 'status'
            """
        )
        assert row["column_default"] is None, (
            f"status carries a column DEFAULT ({row['column_default']!r}) — a second "
            f"source of a retirable lifecycle that no review of the write path sees"
        )
        assert row["is_nullable"] == "YES", (
            "status must be nullable, or a revision-only update on a pre-existing "
            "node cannot be stored without inventing a lifecycle nobody declared"
        )

    async def test_a_blank_node_label_is_refused(self, pg_pool: Any, make_namespace: Any) -> None:
        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await _insert_state_row(conn, ns_id, "   ", "DEVICE", "planned")

    async def test_the_table_is_force_rls_with_the_tenant_policy(self, pg_admin_conn: Any) -> None:
        """The backstop, asserted even though it is not the boundary.

        The pools that serve requests are owner pools and bypass FORCE RLS, so
        this proves the backstop exists — not that tenants are isolated.  What
        isolates them is asserted in ``TestOwnerPoolIsolation``.
        """
        row = await pg_admin_conn.fetchrow(
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE relname = 'system_design_node_state'
            """
        )
        assert row["relrowsecurity"] and row["relforcerowsecurity"]
        policies = await pg_admin_conn.fetch(
            "SELECT policyname FROM pg_policies WHERE tablename = 'system_design_node_state'"
        )
        assert "tenant_isolation_policy" in {p["policyname"] for p in policies}

    async def test_namespace_deletion_cascades_the_state_rows(self, pg_admin_conn: Any) -> None:
        """The FK is ``ON DELETE CASCADE``, like every sibling table's.

        Asserted from the CATALOG rather than by performing a delete: a design
        this table can hold state for has also written HMAC-chained
        ``event_log`` rows, whose own FK is deliberately not CASCADE, so the
        namespace cannot actually be dropped and a behavioural test here would
        be measuring ``event_log``'s constraint instead of this one.
        """
        confdeltype = await pg_admin_conn.fetchval(
            """
            SELECT confdeltype::text
            FROM pg_constraint
            WHERE conrelid = 'system_design_node_state'::regclass
              AND contype = 'f'
              AND confrelid = 'namespaces'::regclass
            """
        )
        # 'c' is CASCADE in pg_constraint.confdeltype; 'a' would be NO ACTION.
        assert confdeltype == "c", (
            f"the namespaces FK delete rule is {confdeltype!r}, not CASCADE — a "
            f"dropped tenant would leave its lifecycle state behind"
        )


# ---------------------------------------------------------------------------
# 2. NOTHING BACKFILLS — and this time it is gated.
# ---------------------------------------------------------------------------


#: Word-boundary INSERT scan.  Built with ``re.compile`` from an explicitly
#: spelled pattern so the boundary metacharacters are visible in one place.
#:
#: WHY THIS CONSTANT EXISTS AT ALL: the round-2 version of this pattern was
#: written inline as a raw string that, through a shell layer, ended up
#: containing two literal 0x08 BACKSPACE bytes instead of the two ``\b``
#: escapes.  The regex then demanded a backspace immediately before ``INSERT``
#: and after ``INTO``, which no SQL file has, so the assertion could NEVER fire
#: — in either parametrisation.  Nothing rendered it: ``grep``, ``cat``,
#: ``sed``, ``inspect.getsource`` and every editor consume the backspaces and
#: show ``r"\bINSERT\s+INTO\b"``, and ``ruff`` does not flag it.  It was found
#: by disassembling the loaded function.
#:
#: If you change this pattern, verify the RESULT BY BYTES on the committed blob
#: (``git show <sha>:<path> | od -c``), not by looking at the line.
_INSERT_SCAN = re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE)


def _migration_ddl() -> bytes:
    """Migration 061's DDL: the marker to the end of that file."""
    migration = _MIGRATION_PATH.read_bytes()
    marker = _TABLE_MARKER.encode("utf-8")
    assert migration.count(marker) == 1
    return migration[migration.index(marker) :]


def _schema_mirror_block(schema_bytes: bytes | None = None) -> bytes:
    """``nce/schema.sql``'s mirror block, bounded by the MIGRATION's length.

    THE SINGLE SLICING IMPLEMENTATION.  Both the parity assertion and the
    ``schema-tail`` scan go through here, so there is one place to get this
    right and one place for a mutation to land.

    Round 3 sliced ``marker → EOF`` in both files, which really asserted "the
    TAIL of schema.sql equals the TAIL of 061".  That holds only while this
    block happens to be last in ``schema.sql``.  The next wave that appends an
    ordinary new table would get a RED reading "the fresh-install path and the
    upgrade path have diverged" — a false alarm, with a message pointing at the
    wrong thing entirely, on a file every migration wave touches.

    Bounding by ``len(ddl)`` makes the assertion say what it means: the bytes
    following the marker in ``schema.sql`` ARE migration 061's DDL, whatever
    comes after them.  A truncated mirror still fails, because the slice then
    runs into whatever follows and stops matching.

    *schema_bytes* exists so a positive control can drive this with a synthetic
    file that HAS something appended — the real file does not, so nothing else
    could tell a bounded slice from an unbounded one.
    """
    ddl = _migration_ddl()
    marker = _TABLE_MARKER.encode("utf-8")
    schema = _SCHEMA_PATH.read_bytes() if schema_bytes is None else schema_bytes
    assert schema.count(marker) == 1
    start = schema.index(marker)
    return schema[start : start + len(ddl)]


def _offending_insert_statements(sql: str) -> list[str]:
    """Statements in *sql* that INSERT INTO this table.

    THE SINGLE FILTER IMPLEMENTATION, for the same reason as above: the
    ``schema-whole`` scan and its positive control must exercise one code path,
    or the control proves nothing about the scan.

    The membership test is LOWER-CASED to match ``_INSERT_SCAN``'s
    ``re.IGNORECASE``.  Round 3 had a case-SENSITIVE ``in`` behind a
    case-insensitive regex, so ``INSERT INTO SYSTEM_DESIGN_NODE_STATE ...``
    matched the pattern and was then thrown away by the filter — it passed all
    seven backfill gates.  SQL identifiers are case-insensitive, so that
    spelling is not exotic, it is just shouting.

    That path is the one that matters most: ``_init_pg_schema``
    (nce/orchestrator.py) applies ``schema.sql`` on EVERY STARTUP, not only on a
    fresh install, so a backfill that slipped through would re-run at every boot
    against a populated database.
    """
    return [
        statement
        for statement in _executable_sql(sql).split(";")
        if _INSERT_SCAN.search(statement) and "system_design_node_state" in statement.lower()
    ]


def test_the_insert_scan_pattern_actually_matches_an_insert() -> None:
    """GUARD THE GUARD — and this is the test that would have caught the 0x08 bug.

    Every assertion built on :data:`_INSERT_SCAN` is of the form "no INSERT is
    present", and a pattern that matches NOTHING satisfies all of them
    permanently.  That is not a hypothetical: round 2's pattern contained two
    literal 0x08 BACKSPACE bytes instead of two ``\b`` escapes, so it demanded a
    backspace either side of the statement, could never fire, and looked
    completely normal in ``grep``, ``cat``, ``sed``, ``inspect.getsource`` and
    every editor — all of which consume backspaces on render.  ``ruff`` did not
    flag it.  It was found by disassembling the loaded function.

    A negative assertion needs a positive control.  The samples below are real
    backfill shapes; if the pattern stops matching them, every backfill gate in
    this file has silently become decoration.
    """
    positives = [
        "INSERT INTO system_design_node_state (namespace_id) SELECT id FROM kg_nodes;",
        "insert into system_design_node_state values (1);",
        "INSERT   INTO   system_design_node_state (x) VALUES (1);",
        "INSERT" + chr(10) + "INTO system_design_node_state (x) VALUES (1);",
        ");" + chr(10) + "INSERT INTO system_design_node_state (x) VALUES (1);",
    ]
    for sample in positives:
        assert _INSERT_SCAN.search(sample), (
            f"the INSERT scan does not match {sample!r} — every backfill gate "
            f"built on it is now permanently vacuous"
        )

    negatives = [
        "-- a comment mentioning INSERT INTO something",
        "CREATE TABLE IF NOT EXISTS system_design_node_state (id UUID);",
        "COMMENT ON TABLE t IS 'never INSERTINTO anything';",
    ]
    assert not _INSERT_SCAN.search(negatives[1])
    assert not _INSERT_SCAN.search(negatives[2]), (
        "the pattern matched INSERTINTO with no separator — the word boundaries "
        "are not doing what they claim"
    )


def test_the_candidate_filter_is_case_insensitive() -> None:
    """POSITIVE CONTROL for the lower-cased membership test.

    The real files contain no backfill in any casing, so nothing else here can
    tell a case-insensitive filter from a case-sensitive one — the scan stays
    green either way.  That is the same vacuity that let the 0x08 pattern
    survive a whole round: an assertion which only ever sees clean input cannot
    detect a broken filter.
    """
    for spelling in (
        "INSERT INTO SYSTEM_DESIGN_NODE_STATE (x) VALUES (1);",
        "insert into System_Design_Node_State (x) values (1);",
        "INSERT INTO system_design_node_state (x) VALUES (1);",
    ):
        assert _offending_insert_statements(spelling), (
            f"a backfill spelled {spelling!r} is not caught — SQL identifiers "
            f"are case-insensitive, so every one of these runs"
        )

    # ...and the filter must stay QUALIFIED: schema.sql legitimately INSERTs
    # into other tables, and flagging those would make the gate unusable.
    assert not _offending_insert_statements("INSERT INTO some_other_table (x) VALUES (1);")


def test_the_mirror_slice_is_bounded_by_the_migrations_length() -> None:
    """POSITIVE CONTROL for the bounded slice.

    ``system_design_node_state`` is NOT the last block in ``schema.sql`` (the
    telemetry merge appended ``telemetry_samples`` after it) -- this test does
    not depend on that claim. It is a positive control for a bounded mirror
    slice: without something appended, ``marker -> EOF`` and
    ``marker -> marker+len(ddl)`` would return the same bytes regardless, so
    the parity assertion would pass either way and prove nothing. Only a
    synthetic file with something appended can tell them apart — and appending a
    table is exactly what the next migration wave will do.
    """
    ddl = _migration_ddl()
    appended = _SCHEMA_PATH.read_bytes() + (
        b"\r\n\r\nCREATE TABLE IF NOT EXISTS a_future_table (id UUID);\r\n"
    )
    assert _schema_mirror_block(appended) == ddl, (
        "appending an ordinary new table to schema.sql broke the mirror parity "
        "assertion — the next migration wave would get a RED telling it the two "
        "install paths had diverged, which would not be true"
    )


def test_executable_sql_strips_trailing_comments_and_string_literals() -> None:
    """POSITIVE CONTROL for the comment/literal stripper.

    Round 3 stripped only WHOLE-LINE ``--`` comments.  Neither trap is
    hypothetical: a trailing comment is ordinary SQL style, and
    ``COMMENT ON TABLE ... IS '...'`` is the normal place for schema
    documentation — this table's prose is a strong candidate for being moved
    there, and one sentence naming the forbidden statement would have turned
    every backfill gate permanently RED against correct code.
    """
    assert not _offending_insert_statements(
        "CREATE TABLE t (x int);  -- INSERT INTO system_design_node_state is a backfill"
    )
    assert not _offending_insert_statements(
        "COMMENT ON TABLE t IS 'never INSERT INTO system_design_node_state here';"
    )
    # SQL escapes an apostrophe by doubling it; that must not end the literal
    # early and expose the remainder to the scan.
    assert not _offending_insert_statements(
        "COMMENT ON TABLE t IS 'W17''s rule: no INSERT INTO system_design_node_state';"
    )

    # STATEMENT STRUCTURE must survive the stripping. The scan is QUALIFIED by
    # splitting on ";" -- schema.sql legitimately INSERTs into other tables, and
    # a stripper that swallowed the terminator would fuse those seed INSERTs
    # with any statement merely NAMING this table, flagging correct SQL forever.
    assert not _offending_insert_statements(
        "INSERT INTO some_other_table (x) VALUES (1);"
        + chr(10)
        + "COMMENT ON TABLE system_design_node_state IS 'fine';"
    )

    # ...and none of that may hide a REAL statement.
    assert _offending_insert_statements(
        "INSERT INTO system_design_node_state (x) VALUES (1);  -- trailing comment"
    )
    assert _offending_insert_statements(
        "COMMENT ON TABLE t IS 'fine';"
        + chr(10)
        + "INSERT INTO system_design_node_state (x) VALUES (1);"
    )


@pytest.mark.parametrize("source", ["migration", "schema-tail", "schema-whole"])
def test_the_ddl_contains_no_insert_statement(source: str) -> None:
    """The cheap fast-fail beside the dynamic gates in :class:`TestNoBackfill`.

    Module-level and synchronous on purpose: it needs no database, and inside an
    ``@pytest.mark.asyncio`` class ``pytest-asyncio`` fails a sync test outright.

    Whole-line ``--`` comments are stripped first: migration 061's header
    discusses the very shape it forbids, so a naive scan would report the
    documentation as the defect.

    THREE parametrisations, because two of them cover different INSTALL PATHS
    and the third covers a placement the other two would miss:

    * ``migration``     — what an existing database runs.
    * ``schema-tail``   — the MIRROR block, which a fresh install runs.
    * ``schema-whole``  — the ENTIRE ``schema.sql``.  A backfill for this table
      does not have to be appended after the mirror marker; dropped anywhere in
      that file it still runs on every fresh install, and the tail slice would
      never see it.
    """
    if source == "migration":
        text = _MIGRATION_PATH.read_text(encoding="utf-8")
    elif source == "schema-tail":
        text = _schema_mirror_block().decode("utf-8")
    else:
        # The whole file, but only statements naming THIS table: schema.sql
        # legitimately INSERTs into other tables (seed rows), so an unqualified
        # scan of it would be a permanent false positive.
        #
        # See _offending_insert_statements for why the membership test is
        # lower-cased and why this is the install path that matters most.
        offending = _offending_insert_statements(_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert not offending, (
            "nce/schema.sql INSERTs into system_design_node_state somewhere "
            f"outside the mirror block: {offending[:1]}"
        )
        return

    statements = _executable_sql(text)
    assert not _INSERT_SCAN.search(statements), (
        f"the {source} DDL for system_design_node_state contains an INSERT; "
        "this table is created empty and stays empty until somebody declares "
        "something about a node"
    )


def test_the_schema_mirror_is_byte_identical_to_the_migration() -> None:
    """The parity that makes the two install paths one artefact.

    Migration 061 declares its DDL a MIRROR of the block at the end of
    ``nce/schema.sql``.  Nothing asserted that until round 3 — an auditor
    grepped for such a check and found none — so the two could drift, and a
    backfill added to only one of them would be invisible to any test that
    reads the other.

    Byte-identical, not merely equivalent: the two paths must produce the same
    catalog IDENTITY, which is why every constraint carries an explicit name
    (an anonymous CHECK is auto-named differently per path — the divergence
    that caused the Batch 132 rejection).
    """
    ddl = _migration_ddl()
    mirror = _schema_mirror_block()

    assert mirror == ddl, (
        "nce/schema.sql's system_design_node_state block is no longer "
        "byte-identical to migration 061's DDL — the fresh-install path and the "
        "upgrade path have diverged, and a change to one is now invisible to "
        "any gate that reads the other"
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestNoBackfill:
    """Round 1 claimed this and could not see it.

    Its ``test_a_pre_wave_node_has_no_state_row`` created its legacy node AFTER
    the migration had already run, so an auditor appended a genuine
    ``INSERT … SELECT … FROM kg_nodes … 'planned'`` to migration 061, rebuilt,
    and the suite stayed 50/50 green with a real legacy DEVICE sitting at
    ``status='planned'``.  The failure message even claimed to gate "nothing in
    migration 061 or the write path may backfill one".

    The dynamic test below RE-APPLIES the migration file against a namespace
    that already holds legacy nodes, inside a transaction it rolls back, and
    asserts the table is still empty.  The migration is idempotent by design
    (``CREATE TABLE IF NOT EXISTS`` / ``DROP POLICY IF EXISTS`` / ``DO $$``), so
    re-applying it is exactly what a boot does to an existing database.
    """

    async def _seed_legacy(self, pg_pool: Any, make_namespace: Any) -> uuid.UUID:
        ns_id: uuid.UUID = await make_namespace()
        await _insert_pre_wave_node(
            pg_pool, ns_id, _LEGACY_DEVICE_LABEL, "DEVICE", predicate="contains"
        )
        await _insert_pre_wave_node(
            pg_pool, ns_id, _LEGACY_CABLE_LABEL, "CABLE", predicate="uses_cable"
        )
        assert await _state_rows(pg_pool, ns_id) == {}
        return ns_id

    async def test_re_applying_the_migration_writes_no_rows(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        ns_id = await self._seed_legacy(pg_pool, make_namespace)
        sql = _MIGRATION_PATH.read_text(encoding="utf-8")

        async with pg_pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            try:
                await conn.execute(sql)
                minted = await conn.fetch(
                    """
                    SELECT node_label, status FROM system_design_node_state
                    WHERE namespace_id = $1::uuid
                    """,
                    str(ns_id),
                )
            finally:
                # Roll back the DDL either way: the policy re-creation and the
                # grant block must not outlive this test.
                await transaction.rollback()

        assert minted == [], (
            "re-applying migration 061 wrote lifecycle rows for nodes nobody "
            f"declared anything about: {[dict(r) for r in minted]}. A backfill here "
            "hands every legacy as-built node a lifecycle it never had, which is "
            "the one-way door this wave exists to keep shut."
        )

    async def test_re_applying_the_schema_mirror_writes_no_rows(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The same gate, for the OTHER install path — and it had none.

        Round 2 re-applied migration 061 and stopped there.  ``nce/schema.sql``
        is what ``scripts/apply_integration_schema.py`` and every fresh install
        run FIRST, so a backfill added to the mirror block ran on every new
        database with nothing watching: an auditor appended a real
        ``INSERT … SELECT … FROM kg_nodes … 'planned'`` to ``schema.sql`` and
        the suite stayed at 114 passed, GREEN.

        THE LESSON, because it outlives this wave: a static scan and a dynamic
        gate are NOT redundant, and neither are two dynamic gates.  Ask which
        INSTALL PATH each one covers.  Round 2 had two checks over one path and
        zero over the other, which looks like belt-and-braces in a diff and is
        actually a hole.
        """
        ns_id = await self._seed_legacy(pg_pool, make_namespace)
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        mirror = schema[schema.index(_TABLE_MARKER) :]

        async with pg_pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            try:
                await conn.execute(mirror)
                minted = await conn.fetch(
                    """
                    SELECT node_label, status FROM system_design_node_state
                    WHERE namespace_id = $1::uuid
                    """,
                    str(ns_id),
                )
            finally:
                await transaction.rollback()

        assert minted == [], (
            "re-applying nce/schema.sql's system_design_node_state block wrote "
            f"lifecycle rows for nodes nobody declared anything about: "
            f"{[dict(r) for r in minted]}. This is the path a FRESH INSTALL "
            "takes, and until round 3 nothing gated it."
        )

    async def test_a_67f_shaped_re_author_changes_no_lifecycle_state(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recorded 67f data-fix runs over PHYSICALLY INSTALLED cable.

        ``devices.py``'s module docstring documents 67f's fix as "backfill is by
        re-author", and that pass goes through ``do_author_device_topology``
        naming a ``cable_ref`` for every connection in the estate.  Under round
        1's unconditional write, one run stamped ``'planned'`` on every already
        installed cable.  This is that pass, in miniature.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id = await self._seed_legacy(pg_pool, make_namespace)
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        # The 67f shape: the same devices and the same cable_ref, re-authored to
        # add the missing second uses_cable edge.  No lifecycle key anywhere.
        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF, "ports": [{"port_ref": _PORT_REF}]}],
                "connections": [
                    {
                        "from_device_ref": _LEGACY_REF,
                        "from_port_ref": _PORT_REF,
                        "to_device_ref": _LEGACY_REF,
                        "to_port_ref": _PORT_REF,
                        "cable_ref": _LEGACY_CABLE_REF,
                    }
                ],
            },
        )

        rows = await _state_rows(pg_pool, ns_id)
        assert _LEGACY_DEVICE_LABEL not in rows, "the data-fix stamped the legacy device"
        assert _LEGACY_CABLE_LABEL not in rows, (
            "the data-fix stamped a physically-installed cable with a lifecycle nobody declared"
        )
        # The port is new, but a PORT never gets state, so the whole pass wrote
        # nothing at all.
        assert rows == {}
        assert result["authored"]["state"] == 0


# ---------------------------------------------------------------------------
# 3. ABSENCE IS PRESERVED — the one-way door, from both ends.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestAbsenceIsPreserved:
    """ "No row" and a stored ``'planned'`` are different facts and must stay so.

    W17's retirement guard denies on an absent state.  It can only do that while
    the two remain distinguishable.
    """

    async def _legacy_namespace(self, pg_pool: Any, make_namespace: Any) -> tuple[uuid.UUID, Any]:
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        await _insert_pre_wave_node(
            pg_pool, ns_id, _LEGACY_DEVICE_LABEL, "DEVICE", predicate="contains"
        )
        assert await _state_rows(pg_pool, ns_id) == {}
        return ns_id, engine

    async def test_a_pre_wave_node_has_no_state_row(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        ns_id: uuid.UUID = await make_namespace()
        await _insert_pre_wave_node(
            pg_pool, ns_id, _LEGACY_DEVICE_LABEL, "DEVICE", predicate="contains"
        )
        assert await _state_rows(pg_pool, ns_id) == {}, (
            "a node authored before this wave acquired a state row"
        )

    async def test_an_ordinary_re_author_leaves_a_pre_wave_node_with_no_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE round-1 defect, in one test.

        A canvas save re-authors every node in the design and names no lifecycle
        key.  Round 1 wrote a row for each of them, so the first save after
        deployment made the entire legacy estate retirable.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._legacy_namespace(pg_pool, make_namespace)
        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF}],
            },
        )

        assert await _state_rows(pg_pool, ns_id) == {}, (
            "re-authoring a pre-existing device with no lifecycle key minted a "
            "state row — the whole legacy estate just became retirable"
        )
        assert result["authored"]["state"] == 0

    async def test_a_geometry_only_canvas_save_leaves_a_pre_wave_node_with_no_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dragging a legacy device 20 pixels is not a lifecycle declaration.

        Geometry goes through the SAME author call, which is why this path needs
        its own test rather than an argument that it is covered.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._legacy_namespace(pg_pool, make_namespace)
        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF, "geometry": {"x": 20.5, "y": 4.0}}],
            },
        )

        assert await _state_rows(pg_pool, ns_id) == {}
        # …and the geometry really was written, so this is not passing because
        # the whole call quietly did nothing.
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            x = await conn.fetchval(
                """
                SELECT x FROM system_design_geometry
                WHERE namespace_id = $1::uuid AND node_label = $2
                """,
                str(ns_id),
                _LEGACY_DEVICE_LABEL,
            )
        assert x is not None and float(x) == 20.5

    async def test_the_read_surface_does_not_report_the_default_for_a_node_with_no_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing may COALESCE absence on READ — and this test can now SEE it.

        Round 1's version was PERMANENTLY VACUOUS twice over: it filtered on
        ``device.get("label")`` while ``read.py`` nests the node under
        ``device["node"]["label"]``, so the list was always empty and the
        assertion body never ran; and its legacy node had no edge to the DESIGN,
        so the scope walk could never have reached it anyway.  It went RED under
        none of 33 mutations.

        Fixed by giving the legacy node the ``contains`` edge a real pre-wave
        author wrote, asserting the device is actually FOUND, and scanning the
        whole serialised device rather than one guessed key — so it fires
        wherever B067g2 chooses to put the status.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._legacy_namespace(pg_pool, make_namespace)
        payload = await _dispatch_ok(
            engine, _READ_TOOL, {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
        )

        legacy = [
            device
            for device in payload.get("devices", [])
            if device["node"]["label"] == _LEGACY_DEVICE_LABEL
        ]
        assert legacy, (
            "the read surface did not return the legacy device at all, so this "
            "test would assert nothing — the fixture, not the code, is broken"
        )
        for device in legacy:
            assert DEFAULT_NODE_STATUS not in json.dumps(device), (
                "the read surface reported the default status for a node that has "
                "NO state row — absence has been coalesced to a default, and W17's "
                "retirement guard can no longer deny on it"
            )


# ---------------------------------------------------------------------------
# 4. Write semantics through the real dispatch path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestWriteSemantics:
    async def _fresh(self, pg_pool: Any, make_namespace: Any) -> tuple[uuid.UUID, Any]:
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        return ns_id, engine

    async def _legacy(self, pg_pool: Any, make_namespace: Any) -> tuple[uuid.UUID, Any]:
        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _insert_pre_wave_node(
            pg_pool, ns_id, _LEGACY_DEVICE_LABEL, "DEVICE", predicate="contains"
        )
        return ns_id, engine

    async def test_a_new_node_authored_without_a_status_is_planned(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEWNESS earns the seed.  Silence on an existing node does not."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        rows = await _state_rows(pg_pool, ns_id)
        assert set(rows) == {_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL}
        for label, row in rows.items():
            assert row["status"] == DEFAULT_NODE_STATUS, label
            assert row["revision"] is None
            assert row["salience"] is None
        assert rows[_DEVICE_LABEL]["node_type"] == "DEVICE"
        assert rows[_RACK_LABEL]["node_type"] == "RACK"
        assert rows[_CABLE_LABEL]["node_type"] == "CABLE"

    async def test_a_port_never_gets_a_state_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PORT is new on this call and still gets nothing.

        Asserted as an exact key set, not merely as ``_PORT_LABEL not in rows``:
        a writer that recorded ports under a mislabelled ``node_type`` would slip
        past the CHECK and past a bare not-in assertion.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        rows = await _state_rows(pg_pool, ns_id)
        assert _PORT_LABEL not in rows, "a PORT acquired a lifecycle state row"
        assert set(rows) == {_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL}

    async def test_an_explicit_status_is_stored_verbatim(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "ALPHA"))

        rows = await _state_rows(pg_pool, ns_id)
        assert rows[_DEVICE_LABEL]["status"] == _TENANT_STATE["ALPHA"]["status"]
        assert rows[_RACK_LABEL]["status"] == _TENANT_RACK_STATUS["ALPHA"]
        assert rows[_CABLE_LABEL]["status"] == _TENANT_CABLE_STATUS["ALPHA"]
        assert rows[_DEVICE_LABEL]["revision"] == _TENANT_STATE["ALPHA"]["revision"]
        assert float(rows[_DEVICE_LABEL]["salience"]) == _TENANT_STATE["ALPHA"]["salience"]

    async def test_a_re_author_without_a_status_keeps_the_stored_one(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The conflict branch reads the caller's raw parameter, not ``EXCLUDED``.

        ``EXCLUDED.status`` has already had the seed applied by the ``COALESCE``
        in ``VALUES``, so using it would stamp the seed over a stored
        ``'staged'``.  Reached here by re-authoring with a REVISION only, which
        after round 2 is the way a silent-on-status re-author reaches the writer
        at all.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "ALPHA"))

        args = _topology_args(ns_id)
        args["devices"][0]["revision"] = "REV-SECOND"
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, args)

        rows = await _state_rows(pg_pool, ns_id)
        assert rows[_DEVICE_LABEL]["status"] == _TENANT_STATE["ALPHA"]["status"], (
            "a re-author that named no status overwrote the stored one — the "
            "conflict branch is reading EXCLUDED.status instead of the caller's "
            "raw parameter"
        )
        assert rows[_DEVICE_LABEL]["revision"] == "REV-SECOND"

    async def test_a_re_author_with_a_new_status_replaces_it(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling: a branch that never updated ``status`` also keeps it."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "ALPHA"))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "BETA"))

        rows = await _state_rows(pg_pool, ns_id)
        assert rows[_DEVICE_LABEL]["status"] == _TENANT_STATE["BETA"]["status"]
        assert rows[_DEVICE_LABEL]["revision"] == _TENANT_STATE["BETA"]["revision"]

    async def test_a_revision_only_update_on_a_pre_wave_node_stores_a_null_status(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third state, and the reason ``status`` is nullable.

        The caller sent a revision about a node that already existed.  That is
        data worth holding and it is NOT a lifecycle declaration — so the row
        exists with ``status IS NULL``, which W17 denies on exactly as it denies
        on a missing row.  Implemented as "explicit STATUS or new", this update
        would have written nothing at all and returned 200.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._legacy(pg_pool, make_namespace)
        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF, "revision": "REV-ONLY"}],
            },
        )

        rows = await _state_rows(pg_pool, ns_id)
        assert _LEGACY_DEVICE_LABEL in rows, "the revision was silently discarded"
        row = rows[_LEGACY_DEVICE_LABEL]
        assert row["revision"] == "REV-ONLY"
        assert row["status"] is None, (
            "a revision-only update invented a lifecycle declaration nobody made"
        )

    async def test_a_salience_only_update_also_records(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``salience`` counts as an explicit key too — the rule is not status-only."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._legacy(pg_pool, make_namespace)
        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF, "salience": 0.125}],
            },
        )

        row = (await _state_rows(pg_pool, ns_id))[_LEGACY_DEVICE_LABEL]
        assert float(row["salience"]) == 0.125
        assert row["status"] is None

    async def test_a_partial_state_update_leaves_the_other_fields_alone(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same partial-update contract as capability and geometry."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "ALPHA"))

        args = _topology_args(ns_id)
        args["devices"][0]["status"] = "failed"
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, args)

        rows = await _state_rows(pg_pool, ns_id)
        assert rows[_DEVICE_LABEL]["status"] == "failed"
        assert rows[_DEVICE_LABEL]["revision"] == _TENANT_STATE["ALPHA"]["revision"]
        assert float(rows[_DEVICE_LABEL]["salience"]) == _TENANT_STATE["ALPHA"]["salience"]

    async def test_the_authored_count_is_the_number_of_distinct_rows(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The count is ``len`` of the delta map, so it cannot overcount.

        Two ``devices`` entries whose refs differ only in CASE produce the same
        upper-cased label and therefore ONE row.  The round-1 counter
        incremented per item and reported two — and that wrong number landed in
        the WORM audit event, where it cannot be corrected.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        result = await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(ns_id),
                "design_id": _DESIGN_ID,
                "devices": [
                    {"device_ref": "dup-ref", "status": "planned"},
                    {"device_ref": "DUP-REF", "status": "active"},
                ],
            },
        )

        rows = await _state_rows(pg_pool, ns_id)
        assert len(rows) == 1, "the two entries did not collapse onto one label"
        assert result["authored"]["state"] == 1, (
            f"state counted {result['authored']['state']} for one row — the count "
            f"is per item rather than per distinct label"
        )

    async def test_the_authored_count_matches_the_rows_for_a_fresh_design(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        result = await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, "ALPHA"))

        assert result["authored"]["state"] == 3, result["authored"]
        assert len(await _state_rows(pg_pool, ns_id)) == 3

    async def test_cable_state_needs_a_cable_ref_and_is_refused_without_one(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused, not dropped: there is no CABLE node for the keys to describe.

        Before round 2, ``_state_of(cnx, "cable_")`` was only reached inside
        ``if cable_ref_str:``, so a connection carrying ``cable_status`` and no
        ``cable_ref`` returned 200 with the value neither validated nor stored.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id, "ALPHA")
        args["connections"][0].pop("cable_ref")
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload, "cable_* keys with no cable_ref were silently dropped"
        assert payload["error"]["code"] == -32602, payload["error"]
        assert await _state_rows(pg_pool, ns_id) == {}

    async def test_a_malformed_cable_salience_is_refused_even_with_no_cable_ref(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shape is validated UNCONDITIONALLY, before the cable_ref branch."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["connections"][0].pop("cable_ref")
        args["connections"][0]["cable_salience"] = "not-a-number"
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload
        assert payload["error"]["code"] == -32602, payload["error"]

    async def test_a_connection_without_cable_keys_still_authors(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling of the two refusals above.

        Without it, a guard that refused every connection naming no ``cable_ref``
        would satisfy both — and would break every W13b caller.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["connections"][0].pop("cable_ref")
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, args)

        rows = await _state_rows(pg_pool, ns_id)
        assert set(rows) == {_DEVICE_LABEL, _RACK_LABEL}

    async def test_a_wrong_vocabulary_status_is_422_not_500_and_writes_nothing(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deny-by-default is the database's; the ERROR SHAPE is this code's.

        A ``CheckViolationError`` is not a ``ValueError``, so before round 2 a
        wrong-vocabulary status escaped as ``-32603`` / a bare 500 with no
        indication of what was wrong in production, while the shape refusal one
        line earlier correctly answered ``-32602`` / 422.  Same fault, two
        answers, and the useless one for the case a client can actually fix.

        Rollback is asserted too: the state write shares the authoring
        transaction, so the graph rows and the version bump go with it.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["devices"][0]["status"] = "connected"  # a CABLE status on a DEVICE
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload, "a foreign-vocabulary status was accepted"
        assert payload["error"]["code"] == -32602, (
            f"a status a client can fix was reported as {payload['error']['code']} — "
            f"clients cannot tell that from a server fault: {payload['error']}"
        )
        assert payload["error"]["data"]["reason"] == "invalid_arguments"

        assert await _state_rows(pg_pool, ns_id) == {}
        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            devices = await conn.fetchval(
                """
                SELECT count(*) FROM kg_nodes
                WHERE namespace_id = $1::uuid AND entity_type = 'DEVICE'
                """,
                str(ns_id),
            )
        assert devices == 0, "the graph rows survived a refused state write"

    async def test_a_negative_salience_is_422_not_500(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative is refused, and the refusal reaches the caller as an argument fault.

        The Python guard accepts a negative (it is finite), so this exercises the
        DATABASE constraint and its translation — the only path that proves the
        two layers are wired to each other.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["devices"][0]["salience"] = -1.5
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload
        assert payload["error"]["code"] == -32602, payload["error"]
        assert await _state_rows(pg_pool, ns_id) == {}

    @pytest.mark.parametrize(
        "bad_device",
        [
            {"salience": True},
            {"salience": float("nan")},
            {"salience": float("inf")},
            {"status": ""},
            {"cable_status": "connected"},
            {"Status": "active"},
        ],
    )
    async def test_the_shape_guards_hold_on_the_public_surface(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
        bad_device: dict[str, Any],
    ) -> None:
        """``_state_of`` is the ONLY validator on the wire, so test it from the wire.

        ``mcp_stdio_tools`` declares ``devices`` / ``racks`` / ``connections`` as
        ``additionalProperties: True`` with no typing, so nothing upstream checks
        these values.  Proving the guard only through the private helper leaves
        the question of whether it is reachable unanswered.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["devices"][0].update(bad_device)
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload, f"{bad_device} was accepted on the public surface"
        assert payload["error"]["code"] == -32602, payload["error"]
        assert await _state_rows(pg_pool, ns_id) == {}

    @pytest.mark.parametrize(
        "bad_connection",
        [
            {"status": "connected"},
            {"revision": "R1"},
            {"salience": 0.5},
            {"Cable_Status": "connected"},
            {"CABLE_SALIENCE": 0.5},
        ],
    )
    async def test_a_misplaced_lifecycle_key_on_a_connection_is_refused_through_the_surface(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
        bad_connection: dict[str, Any],
    ) -> None:
        """The CONNECTION refusal, exercised at its CALL SITE rather than as a helper.

        Found by the mutation sweep: deleting
        ``_refuse_misplaced_lifecycle_keys(cnx, ...)`` from the connections loop
        left the whole suite GREEN.  The unit test for that helper calls it
        directly, so it passes whether or not anything invokes it — a guard
        proven to work and never proven to run.  With the call site gone, an
        unprefixed ``status`` on a connection is silently dropped again, which
        is the exact failure the guard exists to prevent.

        A bare ``status`` on a connection is not a typo to be forgiven: a
        connection is an EDGE, and the ``cable_``-prefixed keys describe the
        CABLE NODE, so ``status`` there means something the engine cannot
        store.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["connections"][0].update(bad_connection)
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload, f"{bad_connection} was accepted on a connection"
        assert payload["error"]["code"] == -32602, payload["error"]
        assert await _state_rows(pg_pool, ns_id) == {}

    async def test_a_port_carrying_a_lifecycle_key_is_refused_through_the_surface(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args["devices"][0]["ports"][0]["status"] = "active"
        payload = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in payload
        assert payload["error"]["code"] == -32602, payload["error"]

    @pytest.mark.parametrize(
        "bucket,payload",
        [
            ("devices", {"salience": 10**309}),
            ("racks", {"salience": 10**309}),
            ("connections", {"cable_salience": 10**309}),
        ],
    )
    async def test_an_oversized_INTEGER_salience_is_422_not_500_on_every_bucket(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
        bucket: str,
        payload: dict[str, Any],
    ) -> None:
        """An INTEGER literal, and all three buckets — the round-2 blind spot.

        ``math.isfinite`` COERCES, and coercing a Python int too large for a C
        double raises ``OverflowError``, which is an ``ArithmeticError`` and NOT
        a ``ValueError``.  It therefore sailed past every ``except ValueError``
        on both surfaces and arrived as ``-32603`` / a bare HTTP 500 — in
        production, with ``cfg.IS_DEV`` false, giving the caller no indication
        of what was wrong.  That is the exact failure round 2's error-shape fix
        was meant to close, reappearing one guard later.

        ``json.loads`` yields arbitrary-precision ints, so this is reachable
        from the wire.  Round 2's tests could not see it because the acceptance
        case wrote ``1e308`` — a FLOAT literal — so the integer path was never
        exercised at all.

        Parametrized over all three buckets because the guard is called from
        three separate sites and fixing one would leave the others.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args[bucket][0].update(payload)
        result = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in result, f"an oversized int salience was accepted on {bucket}"
        assert result["error"]["code"] == -32602, (
            f"{bucket}: an argument a client can fix was reported as "
            f"{result['error']['code']} — indistinguishable from a server fault: "
            f"{result['error']}"
        )
        assert await _state_rows(pg_pool, ns_id) == {}

    @pytest.mark.parametrize(
        "bucket,payload",
        [
            ("devices", {"capability": {"status": "active"}}),
            ("devices", {"capability": {"salience": 1.0}}),
            ("devices", {"geometry": {"status": "active"}}),
            ("racks", {"capability": {"status": "active"}}),
        ],
    )
    async def test_a_nested_lifecycle_key_is_refused_through_the_surface(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
        bucket: str,
        payload: dict[str, Any],
    ) -> None:
        """The measured defect, at the wire.

        Before round 3 this returned **HTTP 200** and stored ``'planned'`` — the
        caller declared ``active`` and the row said the retirable value.  The
        one-way door held (a pre-wave node still minted no row), but the rule
        that a write must not silently discard what the caller sent did not.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        args = _topology_args(ns_id)
        args[bucket][0].update(payload)
        result = await _dispatch(engine, _TOPOLOGY_TOOL, args)

        assert "error" in result, f"nested {payload} was accepted on {bucket}"
        assert result["error"]["code"] == -32602, result["error"]
        assert await _state_rows(pg_pool, ns_id) == {}

    async def test_a_resurrection_is_distinguishable_from_a_no_op(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An orphan state row outliving its node, then a re-author of the label.

        No FK ties a state row to its node (``kg_nodes`` is HASH-partitioned on
        label), so W17 is OBLIGED to delete the row with the node.  If it does
        not, a later re-author of the same deterministic label lands on the
        orphan through ``ON CONFLICT DO UPDATE`` and inherits its status.

        Without the ``resurrected`` marker that write reports
        ``{"from": "decommissioning", "to": "decommissioning",
        "state_row_created": false}`` — honest about the table, and
        indistinguishable from nothing having happened.  This is the wave whose
        purpose is to make that record trustworthy, and this is 67h's blast
        radius, so it is disambiguated here rather than left for later.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        from nce.db_utils import scoped_pg_session

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        # W17 retires the node and — the bug being modelled — leaves the row.
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await conn.execute(
                """
                UPDATE system_design_node_state SET status = 'decommissioning'
                WHERE namespace_id = $1::uuid AND node_label = $2
                """,
                str(ns_id),
                _DEVICE_LABEL,
            )
            await conn.execute(
                "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2",
                str(ns_id),
                _DEVICE_LABEL,
            )

        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        events = await _authoring_events(pg_pool, ns_id, _TOPOLOGY_TOOL)
        delta = {c["node_label"]: c for c in events[-1]["state_changes"]}[_DEVICE_LABEL]
        assert delta["resurrected"] is True, (
            "a node whose state row outlived it was re-authored and the audit "
            f"record cannot be told from a no-op: {delta}"
        )
        assert delta["state_row_created"] is False
        assert delta["from"] == "decommissioning", (
            "the resurrected node did not inherit the orphan's status, so this "
            "test is no longer modelling the hazard it describes"
        )

    async def test_the_authoring_event_carries_the_per_node_delta(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counts alone cannot say WHICH node became retirable, FROM WHAT, BY WHOM.

        This is the one wave whose purpose is to gate a destructive operation,
        and ``event_log`` is the substrate that is INSERT-only, HMAC-signed and
        Merkle-chained — so the delta belongs there rather than in a log line.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        args = _topology_args(ns_id)
        args["devices"][0]["status"] = "active"
        args["actor"] = "actor@example.test"
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, args)

        events = await _authoring_events(pg_pool, ns_id, _TOPOLOGY_TOOL)
        assert len(events) == 2
        first, second = events

        created = {c["node_label"]: c for c in first["state_changes"]}
        assert created[_DEVICE_LABEL] == {
            "node_label": _DEVICE_LABEL,
            "node_type": "DEVICE",
            "from": None,
            "to": DEFAULT_NODE_STATUS,
            "state_row_created": True,
            # A first author is not a resurrection: the node is new AND there
            # was no prior row. Both halves matter — see the resurrection test.
            "resurrected": False,
        }

        assert second["state_changes"] == [
            {
                "node_label": _DEVICE_LABEL,
                "node_type": "DEVICE",
                "from": DEFAULT_NODE_STATUS,
                "to": "active",
                "state_row_created": False,
                "resurrected": False,
            }
        ], second["state_changes"]
        assert second["actor"] == "actor@example.test"

    async def test_an_event_for_a_write_that_changed_no_state_omits_the_key(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent means absent, as with ``actor``.

        An empty list would be indistinguishable from a write by a caller that
        predates the field.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id, engine = await self._fresh(pg_pool, make_namespace)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        events = await _authoring_events(pg_pool, ns_id, _TOPOLOGY_TOOL)
        assert "state_changes" in events[0]
        assert "state_changes" not in events[1]


@pytest.mark.integration
@pytest.mark.asyncio
class TestRestSurface:
    """W16 through the REST route, not only through MCP dispatch.

    Round 2 DOCUMENTED the 422 mapping at
    ``admin_handlers/system_design.py`` and never exercised it: every W16 test
    went through ``execute_call_tool`` and asserted ``-32602``.  The two
    surfaces share one adapter, which is exactly why the claim is plausible —
    and exactly why an untested claim about the other surface is worth nothing.
    """

    async def _request(self, ns_id: uuid.UUID, body: dict[str, Any]) -> Any:
        from nce.admin_handlers import system_design as admin_sd

        class _Request:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            async def json(self) -> dict[str, Any]:
                return self._payload

        return await admin_sd.api_system_design_author_topology(_Request(body))

    @pytest.mark.parametrize(
        "bad",
        [
            {"status": "connected"},  # a CABLE status on a DEVICE — DB CHECK
            {"status": ""},  # shape guard
            {"salience": 10**309},  # the overflow guard
            {"capability": {"status": "active"}},  # the nested guard
            {"cable_status": "connected"},  # misplaced key
        ],
    )
    async def test_a_bad_lifecycle_key_is_422_on_the_rest_route(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
        bad: dict[str, Any],
    ) -> None:
        """422, not 500 — proven on the path whose docstring promises it.

        All five reach the route as ``ValueError`` (the DB CHECK one via
        ``devices._STATE_CONSTRAINT_REASONS``), so all five must be 422.  A 500
        here would mean a caller cannot tell a fixable argument from a server
        fault, which is what the round-2 error-shape fix was for.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        from nce.admin_handlers import _shared

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(_shared.admin_state, "engine", engine, raising=False)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        args = _topology_args(ns_id)
        args["devices"][0].update(bad)
        response = await self._request(ns_id, args)

        assert response.status_code == 422, (
            f"{bad} produced HTTP {response.status_code} on the REST route; a "
            f"fixable argument must not be reported as a server fault"
        )
        assert await _state_rows(pg_pool, ns_id) == {}

    async def test_the_rest_route_still_authors_a_valid_payload(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling: a route that 422s everything would pass every row above."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        from nce.admin_handlers import _shared

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(_shared.admin_state, "engine", engine, raising=False)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        response = await self._request(ns_id, _topology_args(ns_id, "ALPHA"))

        assert response.status_code == 200, response.status_code
        rows = await _state_rows(pg_pool, ns_id)
        assert rows[_DEVICE_LABEL]["status"] == _TENANT_STATE["ALPHA"]["status"]


# ---------------------------------------------------------------------------
# 5. Owner-pool tenant isolation on the WRITE.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnerPoolIsolation:
    """Two tenants colliding on EVERY identifier, differing ONLY in content.

    Same design_id, same slug, same site/building/floor/room, same
    device/port/rack/cable refs — so every node label, and therefore every state
    row's key, is byte-identical across the two tenants.  What differs is
    ``status``, ``revision`` and ``salience``.

    A fixture that gave the two tenants different labels could not detect a
    predicate that filters by label, and would leave the write's namespace
    scoping deletable with the suite green.  B067b failed TAG on exactly that.
    """

    async def _seed_both(
        self, pg_pool: Any, make_namespace: Any
    ) -> tuple[uuid.UUID, uuid.UUID, Any]:
        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_a)
        await _seed_ownership(pg_pool, ns_b)
        engine = _EngineStub(pg_pool)
        for ns_id, tag in ((ns_a, "ALPHA"), (ns_b, "BETA")):
            await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
            await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, tag))
        return ns_a, ns_b, engine

    async def test_a_write_cannot_land_on_another_tenants_row(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write boundary, and it is the ONLY boundary on this connection.

        If the ``ON CONFLICT`` target were ``(node_label)`` rather than
        ``(namespace_id, node_label)``, the second tenant would overwrite the
        first tenant's state instead of creating its own row — a silent
        cross-tenant write that no read test can see, because afterwards both
        tenants read the same (wrong) row consistently.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a, ns_b, _ = await self._seed_both(pg_pool, make_namespace)

        rows_a = await _state_rows(pg_pool, ns_a)
        rows_b = await _state_rows(pg_pool, ns_b)

        for tag, rows, foreign in (("ALPHA", rows_a, "BETA"), ("BETA", rows_b, "ALPHA")):
            assert set(rows) == {_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL}, (
                f"{tag} holds the wrong key set: {sorted(rows)}"
            )
            own = _TENANT_STATE[tag]
            other = _TENANT_STATE[foreign]
            device = rows[_DEVICE_LABEL]
            assert device["status"] == own["status"], (
                f"{tag}'s device carries {foreign}'s status — the only thing "
                f"separating these two tenants is the namespace in the write, "
                f"and it is gone"
            )
            assert device["revision"] == own["revision"]
            assert device["revision"] != other["revision"]
            assert float(device["salience"]) == own["salience"]
            assert rows[_RACK_LABEL]["status"] == _TENANT_RACK_STATUS[tag]
            assert rows[_CABLE_LABEL]["status"] == _TENANT_CABLE_STATUS[tag]

    async def test_both_tenants_keep_their_own_row_count(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-tenant upsert collapses six rows onto three.

        Asserted separately from the content check because a writer that wrote
        the RIGHT content onto the WRONG namespace would leave one tenant with
        three rows and the other with none — and the surviving tenant's content
        check would still pass.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a, ns_b, _ = await self._seed_both(pg_pool, make_namespace)

        assert len(await _state_rows(pg_pool, ns_a)) == 3
        assert len(await _state_rows(pg_pool, ns_b)) == 3

        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            total = await conn.fetchval(
                """
                SELECT count(*) FROM system_design_node_state
                WHERE namespace_id = ANY($1::uuid[])
                """,
                [str(ns_a), str(ns_b)],
            )
        assert total == 6, "the two tenants' colliding node labels resolved to one set of rows"

    async def test_a_second_tenants_write_does_not_disturb_the_first(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering-sensitive form of the same property.

        Tenant A is written and read back BEFORE tenant B writes, then read
        again after.  A cross-tenant conflict target shows up as A's row
        changing under it — which the simultaneous form can miss if the writer
        happens to key on insertion order.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_a)
        await _seed_ownership(pg_pool, ns_b)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_a))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_a, "ALPHA"))
        before = await _state_rows(pg_pool, ns_a)
        assert before[_DEVICE_LABEL]["status"] == _TENANT_STATE["ALPHA"]["status"]

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_b))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_b, "BETA"))

        after = await _state_rows(pg_pool, ns_a)
        assert after[_DEVICE_LABEL]["status"] == _TENANT_STATE["ALPHA"]["status"], (
            "tenant B's write landed on tenant A's state row"
        )
        assert after[_DEVICE_LABEL]["revision"] == _TENANT_STATE["ALPHA"]["revision"]

    async def test_the_state_delta_is_not_borrowed_from_another_tenant(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PRIOR-STATUS probe carries the namespace predicate too.

        That probe is what fills ``from`` and ``state_row_created`` in the WORM
        audit event.  Without its namespace predicate, tenant B's brand-new
        device reports ``from: "staged"`` and ``state_row_created: false`` —
        borrowed from tenant A's identically-labelled row.  The stored ROWS stay
        correct, so no content-comparison test can see it: the leak is entirely
        inside the audit record, on the one wave whose purpose is to make that
        record trustworthy.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_a)
        await _seed_ownership(pg_pool, ns_b)
        engine = _EngineStub(pg_pool)

        for ns_id in (ns_a, ns_b):
            await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_a, "ALPHA"))
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_b, "BETA"))

        (event,) = await _authoring_events(pg_pool, ns_b, _TOPOLOGY_TOOL)
        delta = {c["node_label"]: c for c in event["state_changes"]}[_DEVICE_LABEL]
        assert delta["from"] is None, (
            f"tenant B's audit record says its new device came FROM {delta['from']!r} "
            f"— that is tenant A's status, read through a probe with no namespace "
            f"predicate"
        )
        assert delta["state_row_created"] is True
        assert delta["to"] == _TENANT_STATE["BETA"]["status"]

    async def test_newness_is_judged_per_tenant(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The existence probe carries the namespace predicate too.

        Without it, tenant A having authored ``DEVICE:…:SW-W16`` would make the
        identically-labelled node look pre-existing to tenant B, and B's
        genuinely new device would silently get no lifecycle at all — a
        cross-tenant leak that shows up as MISSING data rather than as wrong
        data, which no content-comparison test would notice.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_a)
        await _seed_ownership(pg_pool, ns_b)
        engine = _EngineStub(pg_pool)

        for ns_id in (ns_a, ns_b):
            await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_a))
        result_b = await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_b))

        assert result_b["authored"]["state"] == 3, (
            "tenant B's brand-new nodes were judged pre-existing because tenant A "
            "had authored the same labels — the existence probe has lost its "
            "namespace predicate"
        )
        assert set(await _state_rows(pg_pool, ns_b)) == {
            _DEVICE_LABEL,
            _RACK_LABEL,
            _CABLE_LABEL,
        }


# ---------------------------------------------------------------------------
# D18 -- a caller-supplied connection confidence must be a real number in [0, 1]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "1e400",  # -> inf via float(), silently, with a 200
        1e400,  # same, already a float
        float("inf"),
        float("-inf"),
        float("nan"),  # every comparison against it is false
        -5,  # documented 0-1, unenforced
        1e9,
        1.0000001,
        None,
        "abc",
        {},
        [],
        True,  # bool is an int subclass; a flag is not a confidence
    ],
)
def test_a_connection_confidence_outside_zero_to_one_is_refused(value: Any) -> None:
    """``mcp_handlers`` forwards ``connections`` items VERBATIM, so this value is
    caller-controlled, and the bare ``float(...)`` this replaced accepted all of it.

    The ones that mattered are not the ones that raised. ``"abc"``, ``None`` and
    ``{}`` already produced ValueError/TypeError and mapped to -32602 correctly.
    The defects were the values that SUCCEEDED: ``inf`` was written onto the edge
    and then nulled on READ by ``graph_query.py:106``, so a caller authored a
    connection, was told it worked, and never saw the value again; and ``nan``
    made every threshold comparison false, so the edge silently vanished from
    filtered queries while the write returned 200.
    """
    with pytest.raises(ValueError, match=r"confidence must be a number in \[0, 1\]"):
        _connection_confidence({"confidence": value})


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.5, 0.9999])
def test_a_valid_connection_confidence_is_accepted(value: Any) -> None:
    """The sibling, without which a guard that refused everything would pass above."""
    assert _connection_confidence({"confidence": value}) == float(value)


def test_an_absent_connection_confidence_keeps_the_structural_default() -> None:
    """Omitting it is not an error -- structural connections are authored without one."""
    assert _connection_confidence({}) == 1.0
