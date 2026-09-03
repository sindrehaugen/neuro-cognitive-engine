"""
tests/test_system_design_author_surface.py
==========================================
Module 6 Wave 13b — the ``system_design_author_*`` write surface.

This is the first external **write** path into the design graph:
``do_author_device_topology`` (devices.py) and ``do_author_functional_location``
(graph.py) were implemented, test-covered and unreachable by any MCP tool, REST
route or A2A skill.  This file gates the surface that reaches them.

What these tests actually gate
------------------------------
1. **Author → read-back through the DISPATCH path.**  Every write goes through
   ``execute_call_tool`` (registry lookup, governance, cache, quota, handler)
   and every read-back goes through W13a's ``system_design_get_topology`` the
   same way.  Calling the cores directly would still pass if the tools were
   never registered — precisely the defect this wave exists to close.

2. **Idempotency, proved by its mechanism.**  A double call must produce an
   identical read-back.  That alone is weak evidence: an ordered-stable writer
   would also pass it.  The mechanism is ``kg_edges``' ``UNIQUE (subject_label,
   predicate, object_label, namespace_id)`` reached through ``ON CONFLICT``, and
   two out-of-tree mutations isolate the claim (§6.4 table in the wave report):
   UNIQUE dropped *and* ``ON CONFLICT`` stripped yields real duplicate rows;
   UNIQUE kept *and* ``ON CONFLICT`` stripped yields ``UniqueViolationError``.
   Dropping the UNIQUE on its own proves nothing about idempotency — it only
   makes ``ON CONFLICT`` unresolvable, so the failure is a schema error.

3. **Owner-pool tenant isolation.**  ``nce_app`` serves no request in this
   deployment (it runs one boot-time WORM self-check), so every request runs on
   a pool that ``FORCE ROW LEVEL SECURITY`` does not constrain.  The guard that
   actually isolates tenants is the explicit ``namespace_id`` predicate in the
   SQL.  The two tenants here collide on **every identifier** — design_id, slug,
   site/building/floor/room/position, device refs, port refs, rack ref, cable
   ref, design-line ref — and differ **only in content**.  Seeding merely "the
   same design label" is not enough and has already been shown not to be: with
   differing device refs the label difference does the filtering, only one of
   ``read.py``'s five namespace predicates gets exercised, and two of the other
   four can be deleted with the suite green — one of them leaking a foreign
   tenant's ``manufacturer``, ``model_number`` and reserved ``copper.*`` keys.

4. **Ownership denial is not vacuous.**  ``assert_owner`` is deny-by-default, so
   a namespace with no ownership registry row must be refused *and must leave
   nothing behind*.  Asserting only "an error came back" would pass against a
   database where the write could never have worked for unrelated reasons, so
   the sibling test proves the same call succeeds once the registry is seeded.

5. **``actor`` (Rev 2 §1).**  Present → present in the event payload.  Omitted →
   the key is **absent**, not ``""`` and not the service identity that the API
   key authenticates.  Both branches asserted, on both tools.

6. **``expected_version`` is LIVE (Rev 2 §2; W14/B067e).**  W13b refused the
   parameter because a silent success would leave a client believing it holds
   a lock it does not hold; W14 created its storage row, so the refusal is gone
   and the parameter performs a real compare-and-swap.  What these tests gate
   now is that a stale token yields a **distinct** conflict error rather than
   the generic 422 / ``McpError`` these paths raise for a malformed argument —
   409 and JSON-RPC ``-32040`` respectively — and that nothing is written when
   it does.  The deeper properties (the increment is in the write's own
   transaction, exactly one of two concurrent writers wins) are gated in
   ``tests/test_system_design_geometry.py``, which owns the mutation table.

7. **Cache invalidation through the CACHED path (Rev 2 §3).**  Asserted by
   **object identity**: the test builds the exact Redis key for this design's
   ``system_design_get_topology`` entry, proves that entry physically exists and
   still holds the pre-write payload, and shows the post-write read returns
   fresh data anyway.  Removing the ``mutation`` cache-generation bump turns
   that test RED.  A re-read with a cold cache would prove nothing, which is why
   these tests need a real Redis rather than a mock.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).  The file
name matches the ``tests/test_system_design_*.py`` CI glob wired by B067a, so it
runs in CI with no workflow edit.

CI GAP (reported to the orchestrator, not silently absorbed): the M6 System
Design CI job provides Postgres but **no Redis service**, and ``ci.yml`` is
outside this wave's ``Files:`` list.  The cached-path tests below therefore skip
in that job and were verified locally against a real Redis.  They follow the
``NCE_TEST_REDIS_URL`` convention already used by
``tests/test_rest_cache_invalidation.py``, whose own CI job does supply Redis.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixture data.
#
# Every field carries a value distinct from every other field of the same type,
# so a transposed assignment inside the adapter or the core fails rather than
# passing on a coincidence.  No label part contains ``_`` or ``%``: labels are
# matched with LIKE elsewhere in the codebase and those are wildcards
# (``cascade.py``'s incident).
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W13B-AUTH-001"
_DESIGN_LABEL = f"DESIGN:{_DESIGN_ID}"
_NS_SLUG = "w13b-author"

_SITE = "SiteAcme"
_BUILDING = "BuildingNorth"
_FLOOR = "FloorThree"
_ROOM = "RoomThreeOhSeven"
_POSITION = "PosDelta"

_SWITCH_REF = "SWITCH-W13B"
_ENCODER_REF = "ENCODER-W13B"
_SWITCH_PORT_REF = "DANTE-OUT-1"
_ENCODER_PORT_REF = "DANTE-IN-9"
_RACK_REF = "RACK-W13B-A"
_CABLE_REF = "CBL-W13B-7"
_LINE_REF = "DL-W13B-1"

_ACTOR = "actor@example.test"

# ---------------------------------------------------------------------------
# Geometry (W14).  Every number below is distinct from every other number in
# this file's fixtures, so a transposed assignment inside the geometry writer
# fails rather than passing on a coincidence.  ``rack_position`` is a half-U
# value because the column is NUMERIC(4,1) and a silent truncation to 8 would
# otherwise be invisible.
# ---------------------------------------------------------------------------
_SWITCH_GEOMETRY: dict[str, Any] = {
    "x": 21.0,
    "y": 34.5,
    "rack_position": 8.5,
    "rack_face": "front",
}
_PORT_GEOMETRY: dict[str, Any] = {"x": 22.25, "y": 35.75}
_RACK_GEOMETRY: dict[str, Any] = {"x": 5.5, "y": 6.75}
_CABLE_GEOMETRY: dict[str, Any] = {"cable_length_m": 13.25, "cable_type": "Cat6A-W13B"}
#: Room dimensions are NOT x/y — they are meta, in METERS (Rev 2 §4).
_ROOM_GEOMETRY: dict[str, Any] = {
    "x": 101.5,
    "y": 102.25,
    "meta": {"copper.room.w": 7.5, "copper.room.d": 5.25, "copper.room.h": 3.1},
}
_BUILDING_GEOMETRY: dict[str, Any] = {"x": 201.5, "y": 202.25}
_FLOOR_GEOMETRY: dict[str, Any] = {"x": 301.5, "y": 302.25}

#: Reserved Copper component-class keys (Rev 2 §5).  NCE stores them verbatim
#: inside ``system_design_device_capabilities.extra`` and gives them no meaning.
_COPPER_EXTRA: dict[str, Any] = {
    "copper.port_kind": "rear",
    "copper.rear_port": f"PORT:{_DESIGN_ID}:PATCHPANEL:REAR-4",
    "copper.rear_position": 11,
}

_BUILDINGS: list[dict[str, Any]] = [
    {
        "name": _BUILDING,
        "geometry": _BUILDING_GEOMETRY,
        "floors": [
            {
                "name": _FLOOR,
                "geometry": _FLOOR_GEOMETRY,
                # POSITIONS carry no geometry: they are bare strings in the tool
                # contract, so there is nowhere to hang one. A shape limit, not
                # a decision — reported with the wave, not worked around here.
                "rooms": [{"name": _ROOM, "positions": [_POSITION], "geometry": _ROOM_GEOMETRY}],
            }
        ],
    }
]

_DESIGN_LINES: list[dict[str, Any]] = [
    {
        "line_ref": _LINE_REF,
        "manufacturer": "Shure",
        "mfr_part_no": "MXA920-W",
        "confidence": 0.61,
    }
]

_RACKS: list[dict[str, Any]] = [
    {
        "rack_ref": _RACK_REF,
        "capability": {
            "device_category": "Rack Enclosure",
            "manufacturer": "MiddleAtlantic",
            "model_number": "MRK-4426",
        },
        "geometry": _RACK_GEOMETRY,
    }
]

_DEVICES: list[dict[str, Any]] = [
    {
        "device_ref": _SWITCH_REF,
        "capability": {
            "device_category": "AV Matrix Switcher",
            "manufacturer": "Crestron",
            "model_number": "DM-NVX-384-W13B",
            "power_draw_watts": 37.5,
            "heat_btu_hr": 128.25,
            "redundancy_role": "primary",
        },
        "ports": [
            {
                "port_ref": _SWITCH_PORT_REF,
                "capability": {
                    "signal_format": "Dante",
                    "signal_version": "4.2",
                    "port_direction": "output",
                    "poe_class": 4,
                    "poe_watts": 25.5,
                    "dante_rx_channels": 17,
                    "dante_tx_channels": 23,
                    "extra": _COPPER_EXTRA,
                },
                "geometry": _PORT_GEOMETRY,
            }
        ],
        "rack_ref": _RACK_REF,
        "geometry": _SWITCH_GEOMETRY,
    },
    {
        "device_ref": _ENCODER_REF,
        "capability": {
            "device_category": "AV Encoder",
            "manufacturer": "Extron",
            "model_number": "NAV-E-201-W13B",
            "power_draw_watts": 19.75,
            "heat_btu_hr": 67.5,
            "redundancy_role": "secondary",
        },
        "ports": [
            {
                "port_ref": _ENCODER_PORT_REF,
                "capability": {
                    "signal_format": "HDBaseT",
                    "signal_version": "3.1",
                    "port_direction": "input",
                    "poe_class": 6,
                    "poe_watts": 51.25,
                    "dante_rx_channels": 29,
                    "dante_tx_channels": 31,
                },
            }
        ],
        "rack_ref": None,
    },
]

_CONNECTIONS: list[dict[str, Any]] = [
    {
        "from_device_ref": _SWITCH_REF,
        "from_port_ref": _SWITCH_PORT_REF,
        "to_device_ref": _ENCODER_REF,
        "to_port_ref": _ENCODER_PORT_REF,
        "confidence": 0.77,
        "cable_ref": _CABLE_REF,
        "cable_geometry": _CABLE_GEOMETRY,
    }
]

# ---------------------------------------------------------------------------
# The exact return contract, counted by hand from the fixture above.
#
# Three DIFFERENT numbers per tool on purpose: a swap of two counters inside the
# core or the adapter changes the answer instead of hiding in it.
#
#   topology  1 rack node + 2 device nodes + 2 port nodes + 1 cable node  = 6
#             has_rack + 2x contains + mounted_in + 2x has_port
#                      + connected_to + 2x uses_cable                     = 9
#
#             B067f (M6.W15) made a CABLE two-ended: uses_cable is now written
#             from BOTH terminations, not the source port only, so a cabled
#             connection contributes 2 edges rather than 1.  8 -> 9.
#             rack cap + 2 device caps + 2 port caps                      = 5
#
#             W14 geometry: rack + switch + switch port + cable            = 4
#             (the encoder and its port carry no geometry on purpose, so a
#             writer that wrote a row per node regardless of whether one was
#             supplied would report 6 and fail here.)
#
#             W16 state: rack + 2 devices + cable                          = 4
#             ON A FRESH DESIGN ONLY. A lifecycle row is recorded only when
#             the node is GENUINELY NEW to the call or the caller sent a
#             lifecycle key, so this is 4 for the first author of these four
#             nodes and 0 for an identical second call. TestIdempotency
#             asserts that difference rather than papering over it: if it were
#             4 twice, every canvas save would be minting a lifecycle for
#             already-installed equipment. The two ports are excluded (a PORT
#             has no lifecycle status), so this number also fails if a writer
#             starts giving ports one.
#
#   FL        DESIGN + SITE + BUILDING + FLOOR + ROOM + POSITION + LINE   = 7
#             contains(SITE) + 4x parent_of + contains(LINE)
#                      + needs + references                               = 8
#             W14 geometry: building + floor + room                        = 3
#             (SITE and POSITION carry none — POSITION *cannot*, being a bare
#             string in the contract.)
# ---------------------------------------------------------------------------
_EXPECTED_TOPOLOGY_AUTHORED = {
    "nodes": 6,
    "edges": 9,
    "capabilities": 5,
    "state": 4,
    "geometry": 4,
}
_EXPECTED_FL_AUTHORED = {"nodes": 7, "edges": 8, "geometry": 3}

_TOPOLOGY_TOOL = "system_design_author_topology"
_FL_TOOL = "system_design_author_functional_location"
_READ_TOOL = "system_design_get_topology"

#: Bound on the relay drain loop. High enough to clear a backlog left by earlier
#: tests, low enough that a relay making no progress fails the test rather than
#: hanging.
_MAX_RELAY_DRAIN_PASSES = 20

#: Redis logical database these tests may write to and clean up.  Never the
#: app's own cache db — the repo's ``.env`` points ``REDIS_URL`` at ``…/1``.
_TEST_REDIS_DB = 14


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


class _NullCacheRedis:
    """A Redis that never returns a cache hit but does count generation bumps.

    Two separate reasons this exists rather than ``redis_client=None``:

    * **Never a hit.**  ``get`` always misses, so every read-back in this file
      proves the query ran rather than that a cached payload was replayed.  The
      cached path gets its own tests, against a real Redis, in
      :class:`TestCacheInvalidation`.
    * **``incr`` must exist.**  ``mcp_stdio_dispatch`` calls
      ``bump_cache_generation(engine.redis_client)`` unguarded after every
      successful ``mutation=True`` tool, so a ``None`` client turns every
      authoring call into ``McpError(-32603)``.  That is a pre-existing hole in
      the dispatch loop — ``_shared.bump_mcp_cache_generation`` handles both a
      missing client and a Redis failure, the dispatch loop handles neither —
      and it is reported with this wave rather than patched here, because
      ``mcp_stdio_dispatch.py`` is outside this wave's ``Files:`` list.
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
    """The engine surface the dispatch loop touches."""

    def __init__(self, pg_pool: Any, redis_client: Any = None) -> None:
        self.pg_pool = pg_pool
        self.redis_client = redis_client if redis_client is not None else _NullCacheRedis()


class _StubRequest:
    """Minimal duck-typed Starlette request: the routes read only ``.json()``."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.path_params: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    """Seed the node-ownership registry so ``assert_owner`` permits the write.

    Deliberately a separate step from authoring: the denial test below is the
    same call with this omitted, which is the only way that test means anything.
    """
    from nce.auth import set_namespace_context
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)


async def _dispatch(
    engine: Any,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call *tool* through the real MCP dispatch path and return its payload.

    Returns the parsed JSON whether it is a result or a JSON-RPC error envelope;
    callers that require success use :func:`_dispatch_ok`.
    """
    from nce.mcp_stdio_dispatch import execute_call_tool

    parts = await execute_call_tool(engine, tool, arguments)
    assert parts, f"dispatch returned no content for {tool}"
    return json.loads(parts[0].text)


async def _dispatch_ok(engine: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch *tool* and assert it did not return an error envelope."""
    payload = await _dispatch(engine, tool, arguments)
    assert "error" not in payload, f"{tool} returned an error envelope: {payload}"
    return payload


def _fl_args(ns_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    """Argument bag for ``system_design_author_functional_location``."""
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "namespace_slug": _NS_SLUG,
        "design_id": _DESIGN_ID,
        "site_name": _SITE,
        "buildings": _BUILDINGS,
        "design_lines": _DESIGN_LINES,
    }
    args.update(overrides)
    return args


def _topology_args(ns_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    """Argument bag for ``system_design_author_topology``."""
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": _DESIGN_ID,
        "devices": _DEVICES,
        "connections": _CONNECTIONS,
        "racks": _RACKS,
    }
    args.update(overrides)
    return args


def _read_args(ns_id: uuid.UUID) -> dict[str, Any]:
    return {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}


# ---------------------------------------------------------------------------
# Tenant-collision fixtures.
#
# The isolation tests below need two tenants whose graphs are byte-identical in
# every IDENTIFIER and differ only in CONTENT.  This is not a stylistic
# preference: B067b's isolation test collided the DESIGN label but gave the two
# tenants different device refs, so the label difference did the filtering, only
# one of ``read.py``'s five namespace predicates was exercised, and two others
# could be deleted with the whole suite green — one of them leaking a foreign
# tenant's manufacturer, model_number and reserved ``copper.*`` keys.
#
# A test that differentiates tenants on labels cannot detect a predicate that
# filters by label.  So: same design_id, same slug, same site/building/floor/
# room/position, same device refs, same port refs, same rack ref, same cable
# ref, same design-line ref — and every value that can be read back differs.
# ---------------------------------------------------------------------------


def _tenant_extra(tag: str) -> dict[str, Any]:
    """Reserved ``copper.*`` keys whose VALUES identify the owning tenant."""
    return {
        "copper.port_kind": f"{tag}-rear",
        "copper.rear_port": f"PORT:{_DESIGN_ID}:PATCHPANEL:{tag}-REAR",
        "copper.rear_position": 11 if tag == "ALPHA" else 97,
    }


def _private_device_ref(tag: str) -> str:
    """A device ref that exists in exactly one tenant.

    The colliding devices below catch a predicate that leaks foreign CONTENT.
    They cannot catch one that widens the SCOPE WALK, because when every label
    is shared, a walk that strays into the neighbouring tenant collects the same
    label set it already had.  This private device gives the walk something to
    stray onto, so a scope leak shows up as a foreign label in the read-back.
    """
    return f"PRIVATE-{tag}"


def _tenant_devices(tag: str) -> list[dict[str, Any]]:
    """The same device/port refs as every other tenant; different content.

    Plus one device (``_private_device_ref``) that only this tenant has — see
    that helper for why both halves are needed.
    """
    return [
        {
            "device_ref": _private_device_ref(tag),
            "capability": {
                "device_category": "AV Endpoint",
                "manufacturer": f"{tag}-CORP",
                "model_number": f"{tag}-PRIVATE-1",
            },
            "ports": [],
            "rack_ref": None,
        },
        {
            "device_ref": _SWITCH_REF,
            "capability": {
                "device_category": "AV Matrix Switcher",
                "manufacturer": f"{tag}-CORP",
                "model_number": f"{tag}-MODEL-9",
                "power_draw_watts": 37.5 if tag == "ALPHA" else 88.25,
                "redundancy_role": "primary",
            },
            "ports": [
                {
                    "port_ref": _SWITCH_PORT_REF,
                    "capability": {
                        "signal_format": f"{tag}-Dante",
                        "signal_version": "4.2" if tag == "ALPHA" else "9.9",
                        "port_direction": "output",
                        "extra": _tenant_extra(tag),
                    },
                }
            ],
            "rack_ref": _RACK_REF,
        },
        {
            "device_ref": _ENCODER_REF,
            "capability": {
                "device_category": "AV Encoder",
                "manufacturer": f"{tag}-CORP",
                "model_number": f"{tag}-ENCODER-3",
            },
            "ports": [
                {
                    "port_ref": _ENCODER_PORT_REF,
                    "capability": {
                        "signal_format": f"{tag}-HDBaseT",
                        "port_direction": "input",
                    },
                }
            ],
            "rack_ref": None,
        },
    ]


def _tenant_racks(tag: str) -> list[dict[str, Any]]:
    """Racks that collide on ``rack_ref`` and differ only in content.

    ``cable_type`` on the geometry row is the tenant marker for the geometry
    map: every geometry NUMBER could coincide across tenants without anyone
    noticing, but a string cannot, and ``json.dumps(payload)`` in the isolation
    assertion searches the geometry map along with everything else.
    """
    return [
        {
            "rack_ref": _RACK_REF,
            "capability": {
                "device_category": "Rack Enclosure",
                "manufacturer": f"{tag}-RACKWORKS",
                "model_number": f"{tag}-MRK",
            },
            "geometry": {
                "x": 5.5 if tag == "ALPHA" else 77.25,
                "y": 6.75 if tag == "ALPHA" else 88.5,
                "cable_type": f"{tag}-RACKTAG",
            },
        }
    ]


def _tenant_design_lines(tag: str) -> list[dict[str, Any]]:
    """Design lines that are IDENTICAL across tenants.

    ``manufacturer`` and ``mfr_part_no`` are not content here: ``graph.py``
    builds the cross-engine ``PRODUCT:<MFR>:<PART>`` label out of them, so
    varying them per tenant would put a label difference back into the graph
    and hand the isolation test a free discriminator.  Only ``confidence``
    varies, and ``do_get_topology`` does not project edge confidence — which is
    exactly why the tenant marker lives on the capability rows instead.
    """
    return [
        {
            "line_ref": _LINE_REF,
            "manufacturer": "Shure",
            "mfr_part_no": "MXA920-W",
            "confidence": 0.61 if tag == "ALPHA" else 0.13,
        }
    ]


def _tenant_strings(tag: str) -> set[str]:
    """Every content string that identifies *tag* AND is projected by the reader.

    The rack's ``manufacturer``/``model_number`` used to be excluded here,
    because W13a's ``do_get_topology`` projected DEVICE and PORT capability rows
    only and a RACK capability was written by the authoring tool and never read
    back — the read-contract gap this file reported as debt D5.  **W14 closes
    it**: ``racks`` is now a projected bucket carrying the rack node *and* its
    capability row, so the rack's content is readable and therefore leakable,
    and it belongs in this set.  Leaving it out would leave the widest bucket
    this wave added ungated by the isolation test.
    """
    extra = _tenant_extra(tag)
    return {
        f"{tag}-CORP",
        f"{tag}-MODEL-9",
        f"{tag}-ENCODER-3",
        f"{tag}-Dante",
        f"{tag}-HDBaseT",
        # W14 / debt D5 — readable from this wave on.
        f"{tag}-RACKWORKS",
        f"{tag}-MRK",
        extra["copper.port_kind"],
        extra["copper.rear_port"],
        # W14 — the geometry map is a projected bucket too, and this string is
        # the only thing in it that can identify its owner.
        f"{tag}-RACKTAG",
    }


async def _author_colliding_design(engine: Any, ns_id: uuid.UUID, tag: str) -> None:
    """Author one tenant's copy of the fully colliding design."""
    await _dispatch_ok(
        engine,
        _FL_TOOL,
        _fl_args(ns_id, design_lines=_tenant_design_lines(tag)),
    )
    await _dispatch_ok(
        engine,
        _TOPOLOGY_TOOL,
        _topology_args(ns_id, devices=_tenant_devices(tag), racks=_tenant_racks(tag)),
    )


async def _author_full_design(engine: Any, ns_id: uuid.UUID, **extra: Any) -> None:
    """Author the FL tree (which creates the DESIGN node), then the devices."""
    await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id, **extra))
    await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, **extra))


async def _authoring_events(pg_pool: Any, ns_id: uuid.UUID, tool: str) -> list[dict[str, Any]]:
    """Return the ``system_design_authored`` event_log params, newest last.

    ``event_log``, not ``outbox_events``: the attribution record is INSERT-only,
    HMAC-signed and Merkle-chained.  ``outbox_events`` is a delivery queue that
    the ``nce_app`` role holds UPDATE and DELETE on, which makes it unfit to
    hold a record of who authorised a write.

    The ``namespace_id`` predicate is in the SQL, not left to RLS: owner pools
    bypass ``FORCE ROW LEVEL SECURITY``.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT params, agent_id
            FROM event_log
            WHERE namespace_id = $1::uuid
              AND event_type = 'system_design_authored'
            ORDER BY event_seq
            """,
            str(ns_id),
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        params = row["params"]
        params = json.loads(params) if isinstance(params, str) else dict(params)
        if params.get("tool") == tool:
            params["_agent_id"] = row["agent_id"]
            out.append(params)
    return out


async def _count_design_nodes(pg_pool: Any, ns_id: uuid.UUID) -> int:
    """Count kg_nodes rows for this design's namespace (explicit ns predicate)."""
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM kg_nodes WHERE namespace_id = $1::uuid",
                str(ns_id),
            )
        )


async def _purge_orphaned_outbox_rows(pg_pool: Any) -> int:
    """Delete unpublished outbox rows whose namespace no longer exists.

    Not housekeeping — without it the relay tests below are order-dependent and
    can fail for a reason that has nothing to do with the code under test.

    ``outbox_events.namespace_id`` carries **no** foreign key, but
    ``dead_letter_queue.namespace_id`` is ``REFERENCES namespaces(id) ON DELETE
    CASCADE``.  So an undelivered outbox row survives its tenant's deletion, and
    the next relay pass that tries to dead-letter it raises
    ``ForeignKeyViolationError`` — which aborts the whole relay transaction, not
    merely that row.  One deleted tenant's undelivered events can therefore
    stall delivery for every other tenant.  Recorded in the wave report; the fix
    is outside this wave.

    Returns the number of rows removed.
    """
    async with pg_pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM outbox_events oe
            WHERE oe.published_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM namespaces n WHERE n.id = oe.namespace_id
              )
            """
        )
    return int(result.rsplit(" ", 1)[-1]) if result.startswith("DELETE") else 0


def _redis_url() -> str:
    """Test Redis DSN, pinned to a scratch database.

    ``NCE_TEST_REDIS_URL`` is taken verbatim (an explicit opt-in).  Otherwise the
    ambient ``REDIS_URL`` supplies host and credentials only, with its database
    forced to :data:`_TEST_REDIS_DB` — these tests delete keys, and the repo's
    ``.env`` points ``REDIS_URL`` at the live dev cache.
    """
    explicit = os.environ.get("NCE_TEST_REDIS_URL")
    if explicit:
        return explicit
    ambient = os.environ.get("REDIS_URL")
    if not ambient:
        return f"redis://localhost:6379/{_TEST_REDIS_DB}"
    parts = urlsplit(ambient)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{_TEST_REDIS_DB}", parts.query, parts.fragment)
    )


async def _clear_test_cache_keys(client: Any) -> None:
    """Remove only the MCP cache keys — never ``flushdb``."""
    keys = await client.keys("mcp_cache:v*")
    if keys:
        await client.delete(*keys)
    await client.delete("mcp_cache_generation")


async def _cache_generation(client: Any) -> int:
    raw = await client.get("mcp_cache_generation")
    return int(raw.decode()) if raw else 0


async def _topology_cache_key(client: Any, ns_id: uuid.UUID) -> str:
    """The exact Redis key this design's cached topology read lives under.

    Object identity, not a name prefix: the last time this defect class was
    fixed, a prefix filter under-scoped 6 of 19 routes *and the gate encoded the
    same filter*.  This key is derived the way the dispatch loop derives it.
    """
    from nce.mcp_args import build_cache_key

    return build_cache_key(
        tool_name=_READ_TOOL,
        arguments=_read_args(ns_id),
        generation=await _cache_generation(client),
        namespace_id=str(ns_id),
    )


# ---------------------------------------------------------------------------
# 1. Registration — the surface exists on BOTH registries and both routes.
#
# The unit-level half of the §6.4 rows "tool removed from TOOL_REGISTRY" and
# "tool removed from TOOLS": a tool missing from TOOL_REGISTRY is
# undispatchable, and one missing from TOOLS is invisible to ``tools/list``, so
# no client can discover it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
def test_tool_is_dispatchable_with_coppers_flags(tool_name: str) -> None:
    """Both authoring tools are in TOOL_REGISTRY with Copper's exact flags."""
    from nce.tool_registry import TOOL_REGISTRY

    assert tool_name in TOOL_REGISTRY
    spec = TOOL_REGISTRY[tool_name]
    assert spec.mutation is True, (
        "mutation=False would stop the dispatch loop bumping the cache "
        "generation, leaving the cacheable topology read stale for 300 s"
    )
    assert spec.cacheable is False
    assert spec.admin_only is False


@pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
def test_tool_is_advertised(tool_name: str) -> None:
    """Both authoring tools are in TOOLS, so tools/list can see them."""
    from nce.mcp_stdio_tools import TOOLS

    advertised = {tool.name for tool in TOOLS}
    assert tool_name in advertised, (
        f"{tool_name} is dispatchable but not advertised: absent from TOOLS it "
        f"is invisible to tools/list, which is how a client discovers it."
    )


def test_rest_routes_are_wired() -> None:
    """Both POST routes are mounted, and the W13a GET still resolves."""
    from nce.admin_app import build_admin_routes

    by_key = {
        (getattr(route, "path", None), method): route.endpoint.__name__
        for route in build_admin_routes()
        for method in (getattr(route, "methods", None) or set())
    }

    assert by_key.get(("/api/system-design/topology", "POST")) == (
        "api_system_design_author_topology"
    )
    assert by_key.get(("/api/system-design/functional-location", "POST")) == (
        "api_system_design_author_functional_location"
    )
    # The POST above shares its path with W13a's GET; adding it must not have
    # shadowed the read route.
    assert by_key.get(("/api/system-design/topology", "GET")) == ("api_system_design_get_topology")


def test_author_tools_wrap_the_cores_verbatim() -> None:
    """The adapters call the untouched domain cores — no shadow reimplementation.

    ``devices.py`` / ``graph.py`` are outside this wave's ``Files:`` list; this
    pins that the surface reaches *those* functions rather than a copy that
    could drift from them.
    """
    import inspect

    from nce.vertical_modules.system_design import devices, graph, mcp_handlers

    assert mcp_handlers.do_author_device_topology is devices.do_author_device_topology
    assert mcp_handlers.do_author_functional_location is graph.do_author_functional_location

    # The cores' signatures are the contract the adapters are forbidden to bend.
    topology_params = inspect.signature(devices.do_author_device_topology).parameters
    assert list(topology_params) == [
        "conn",
        "namespace_id",
        "design_id",
        "devices",
        "connections",
        "racks",
        "source_id",
    ]
    fl_params = inspect.signature(graph.do_author_functional_location).parameters
    assert list(fl_params) == [
        "conn",
        "namespace_id",
        "namespace_slug",
        "design_id",
        "site_name",
        "buildings",
        "design_lines",
        "source_id",
    ]


# ---------------------------------------------------------------------------
# 2. ``expected_version`` fails closed — no DB needed.
# ---------------------------------------------------------------------------


def test_version_conflict_reason_is_not_a_generic_validation_reason() -> None:
    """The conflict reason must not collide with @mcp_handler's generic ones.

    If it did, a client could not tell "you are behind, re-read and retry" from
    "your argument is malformed" — a retryable server-state fact from a
    permanent fault in the request. That distinction is the whole point of
    giving the conflict its own error.
    """
    from nce.vertical_modules.system_design.geometry import VersionConflictError

    assert VersionConflictError.reason not in {
        "invalid_arguments",
        "validation_error",
        "missing_field",
        "internal_error",
        "quota_exceeded",
        # And it is not the W13b fail-closed reason either: that parameter is
        # live now and a client must never see the old discriminator again.
        "optimistic_concurrency_not_enabled",
    }


def test_version_conflict_mcp_code_is_distinct_from_invalid_params() -> None:
    """-32040, not -32602, and inside JSON-RPC's server-defined range.

    ``McpError`` takes any int, so nothing but this test stops the conflict
    being raised as ``MCP_INVALID_PARAMS`` and becoming indistinguishable from
    a malformed argument on the wire.
    """
    from nce.mcp_errors import MCP_INTERNAL_ERROR, MCP_INVALID_PARAMS
    from nce.vertical_modules.system_design.mcp_handlers import VERSION_CONFLICT_MCP_CODE

    assert VERSION_CONFLICT_MCP_CODE not in {MCP_INVALID_PARAMS, MCP_INTERNAL_ERROR}
    assert -32099 <= VERSION_CONFLICT_MCP_CODE <= -32000, (
        "must sit in JSON-RPC's implementation-defined server-error range"
    )


@pytest.mark.parametrize(
    "tool_name,args_builder",
    [(_TOPOLOGY_TOOL, _topology_args), (_FL_TOOL, _fl_args)],
)
@pytest.mark.asyncio
async def test_malformed_expected_version_is_a_validation_error_not_a_conflict(
    tool_name: str,
    args_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer token is a MALFORMED ARGUMENT, not a conflict.

    The two failures must not be collapsed: retrying a malformed request never
    succeeds, retrying a conflict usually does. ``engine.pg_pool`` is None on
    purpose — the parse has to happen before anything opens a session, so a
    conflict error here would prove the check ran too late to be safe.

    ``True`` is included because ``bool`` is an ``int`` subclass in Python: a
    ``true`` on the wire coerced to the token ``1`` would compare against a real
    version and occasionally *succeed*. ``2**63`` is included because
    ``bump_design_version`` binds the token to ``$4::bigint`` and asyncpg raises
    ``DataError`` past that — not a ``ValueError``, so it would escape as a 500
    / ``-32603``.

    🔴 **The assertion is POSITIVE — ``reason == "invalid_arguments"`` — and
    that is the entire point of this round's edit.** The earlier version
    asserted only ``reason != VersionConflictError.reason``, which with a
    ``None`` pool holds no matter what: every path errors, so the test passed
    with the ``bool`` guard deleted AND with the negative guard deleted. It
    credited itself with gating the very case its own docstring names.
    ``expected_version_of`` is the ONLY enforcement — ``mcp_stdio_dispatch``
    does no JSON-schema validation, so the ``"minimum": 0`` in ``inputSchema``
    is advisory — which is exactly why this has to discriminate.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    from nce.mcp_errors import MCP_INVALID_PARAMS
    from nce.vertical_modules.system_design.geometry import VersionConflictError

    for bad in ("7", 1.5, True, -1, [], 2**63, 2**70):
        ns_id = uuid.uuid4()
        payload = await _dispatch(
            _EngineStub(pg_pool=None), tool_name, args_builder(ns_id, expected_version=bad)
        )
        assert "error" in payload, f"expected_version={bad!r} was accepted"
        error = payload["error"]
        reason = error.get("data", {}).get("reason")
        assert reason == "invalid_arguments", (
            f"expected_version={bad!r} must be refused AS A MALFORMED ARGUMENT. "
            f"Got reason={reason!r}, code={error.get('code')!r}. A `reason != "
            f"version_conflict` assertion would pass here even with the guard "
            f"deleted, because a None pool makes every path error."
        )
        assert error["code"] == MCP_INVALID_PARAMS
        assert reason != VersionConflictError.reason


@pytest.mark.parametrize(
    "tool_name,args_builder",
    [(_TOPOLOGY_TOOL, _topology_args), (_FL_TOOL, _fl_args)],
)
@pytest.mark.asyncio
async def test_expected_version_null_is_absence_not_a_token(
    tool_name: str,
    args_builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit JSON null expresses no version expectation.

    Unchanged from W13b: null must fall through to ordinary processing. Here
    that means the call gets as far as opening a session and fails on the
    ABSENT ENGINE — not on a conflict, and not on a validation error about the
    token.

    🔴 **The assertion is POSITIVE, and that is this round's edit.** The earlier
    version asserted only ``reason != VersionConflictError.reason``, which with
    a ``None`` pool holds no matter what happens: every path errors. Measured —
    making ``expected_version_of`` raise ``ValueError`` on an explicit ``null``
    left it GREEN, so the half of its own docstring that claims "NOT a
    validation error about the token" was gated by nothing. This is the same
    shape the sibling test above was rejected for in round 1; the sweep for
    non-discriminating ``!=`` assertions across these files ends here.

    ``-32603``/``internal_error`` is precisely what "it got past argument
    parsing and tripped over the missing pool" looks like: a rejected token
    would be ``-32602``/``invalid_arguments`` instead. The behavioural claim —
    that a ``null`` token lets the write actually succeed — is gated against a
    real database by
    ``TestExpectedVersionIsLive::test_a_null_token_writes_normally_and_still_bumps``.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    from nce.mcp_errors import MCP_INTERNAL_ERROR
    from nce.vertical_modules.system_design.geometry import VersionConflictError

    ns_id = uuid.uuid4()
    args = args_builder(ns_id, expected_version=None)
    payload = await _dispatch(_EngineStub(pg_pool=None), tool_name, args)

    error = payload.get("error", {})
    reason = error.get("data", {}).get("reason")
    assert reason == "internal_error", (
        f"an explicit null token must fall THROUGH argument parsing and fail on "
        f"the absent engine; got reason={reason!r}, code={error.get('code')!r}. "
        f"A bare `reason != version_conflict` assertion passes here even when "
        f"the token is wrongly rejected, which is why this one is positive."
    )
    assert error.get("code") == MCP_INTERNAL_ERROR
    assert reason != VersionConflictError.reason
    assert reason != "invalid_arguments", "null was treated as a malformed token"


@pytest.mark.integration
@pytest.mark.asyncio
class TestExpectedVersionIsLive:
    """``expected_version`` performs a real compare-and-swap (Rev 2 §2, W14).

    The deeper properties — that the increment lands in the write's own
    transaction, and that exactly one of two genuinely concurrent writers wins —
    are gated in ``tests/test_system_design_geometry.py``, which also carries
    the per-predicate mutation table. What this class gates is the SURFACE: the
    two tools, through the real dispatch path, and the two REST routes.
    """

    async def test_matching_token_succeeds_and_advances_the_version(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        # A fresh design is at 0, and 0 is a usable token.
        first = await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id, expected_version=0))
        assert first["version"] == 1

        second = await _dispatch_ok(
            engine, _TOPOLOGY_TOOL, _topology_args(ns_id, expected_version=1)
        )
        assert second["version"] == 2

        read_back = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert read_back["version"] == 2, "the read surface must agree with the writer"

    async def test_a_null_token_writes_normally_and_still_bumps(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The behavioural half of "an explicit null is absence, not a token".

        Its unit-level sibling runs on ``pg_pool=None``, so it can only show
        that parsing let the value through — it can never show that the WRITE
        then succeeds. This does, against a real database, and it also pins the
        half that matters for concurrency: an absent token still advances the
        version, so a client holding one can always tell it is behind.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        first = await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id, expected_version=None))
        assert first["version"] == 1, "an explicit null must not block the write"

        second = await _dispatch_ok(
            engine, _TOPOLOGY_TOOL, _topology_args(ns_id, expected_version=None)
        )
        assert second["version"] == 2, "an absent token must still advance the version"

        read_back = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert read_back["version"] == 2

    @pytest.mark.parametrize(
        "tool_name,args_builder",
        [(_TOPOLOGY_TOOL, _topology_args), (_FL_TOOL, _fl_args)],
    )
    async def test_stale_token_is_a_distinct_conflict_and_writes_nothing(
        self,
        tool_name: str,
        args_builder: Any,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale token yields -32040 and leaves the graph exactly as it was.

        "Writes nothing" is asserted by node count, not by the absence of an
        error envelope: an error envelope alone says nothing about what the
        refused call left behind.

        🔴 **What this does NOT gate: the ORDER of the bump and the graph
        writes.** An earlier version of this docstring claimed counting told
        those apart. It does not — moving ``bump_design_version`` to run
        *after* the graph writes on the same connection leaves this test GREEN,
        because the transaction rolls back either way and the node count is
        identical. Running the compare-and-swap first is a fail-fast choice
        (don't do the work you are about to discard, and take the design's row
        lock for the whole write), not a correctness property this test holds.
        The property that IS gated is ATOMICITY — that the increment shares the
        write's transaction — and it is gated by
        ``TestOptimisticConcurrency::test_the_increment_is_inside_the_writes_own_transaction``
        plus mutation row P13, which moves the bump onto its own connection and
        goes RED.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.vertical_modules.system_design.geometry import VersionConflictError
        from nce.vertical_modules.system_design.mcp_handlers import VERSION_CONFLICT_MCP_CODE

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        # Establish a design at version 1.
        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        before = await _count_design_nodes(pg_pool, ns_id)

        payload = await _dispatch(engine, tool_name, args_builder(ns_id, expected_version=99))

        assert "error" in payload, "a stale expected_version was accepted"
        error = payload["error"]
        assert error["code"] == VERSION_CONFLICT_MCP_CODE, (
            "a stale token must not be reported with the generic invalid-params code"
        )
        assert error["data"]["reason"] == VersionConflictError.reason
        assert error["data"]["parameter"] == "expected_version"
        assert error["data"]["expected_version"] == 99
        assert error["data"]["actual_version"] == 1, (
            "the caller must be told where the design actually is, so it can "
            "re-drive without a second round trip"
        )

        assert await _count_design_nodes(pg_pool, ns_id) == before, (
            "the refused write left rows behind — the conflict check ran after "
            "the graph writes, or the transaction did not roll back"
        )
        read_back = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert read_back["version"] == 1, "a refused write must not advance the version"

    @pytest.mark.parametrize(
        "route_name,args_builder",
        [
            ("api_system_design_author_topology", _topology_args),
            ("api_system_design_author_functional_location", _fl_args),
        ],
    )
    async def test_rest_reports_a_stale_token_as_409_not_422(
        self,
        route_name: str,
        args_builder: Any,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """409 Conflict, distinct from the 422 every other failure here returns.

        Both statuses are asserted in the same test on purpose: showing that a
        conflict is 409 proves nothing unless the ordinary validation failure on
        the same route is still 422.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes
        from nce.vertical_modules.system_design.geometry import VersionConflictError

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        route = getattr(routes, route_name)
        conflict = await route(_StubRequest(args_builder(ns_id, expected_version=99)))
        assert conflict.status_code == 409, (
            "a stale token is a conflict with the resource's current state, not "
            "an unprocessable entity"
        )
        body = json.loads(conflict.body)
        assert body["reason"] == VersionConflictError.reason
        assert body["expected_version"] == 99
        assert body["actual_version"] == 1

        malformed = await route(_StubRequest(args_builder(ns_id, expected_version="七")))
        assert malformed.status_code == 422, (
            "a malformed token must stay a 422 — otherwise 409 carries no "
            "information a client can act on"
        )


# ---------------------------------------------------------------------------
# 3. Integration — author, read back, and hold the return contract field by field.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestAuthorReadBack:
    async def test_return_contract_is_exact_field_by_field(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both tools return their counted authoring totals, key by key."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        fl_result = await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        assert set(fl_result) == {"authored", "version"}
        assert set(fl_result["authored"]) == {"nodes", "edges", "geometry"}
        assert fl_result["authored"]["nodes"] == _EXPECTED_FL_AUTHORED["nodes"]
        assert fl_result["authored"]["edges"] == _EXPECTED_FL_AUTHORED["edges"]
        assert fl_result["authored"]["geometry"] == _EXPECTED_FL_AUTHORED["geometry"]
        # W14: the design's NEW version — the token for the caller's next write.
        # First write on a fresh design, so 0 -> 1.
        assert fl_result["version"] == 1

        topo_result = await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        assert set(topo_result) == {"authored", "version"}
        assert set(topo_result["authored"]) == {
            "nodes",
            "edges",
            "capabilities",
            "state",
            "geometry",
        }
        assert topo_result["authored"]["nodes"] == _EXPECTED_TOPOLOGY_AUTHORED["nodes"]
        assert topo_result["authored"]["edges"] == _EXPECTED_TOPOLOGY_AUTHORED["edges"]
        assert (
            topo_result["authored"]["capabilities"] == _EXPECTED_TOPOLOGY_AUTHORED["capabilities"]
        )
        assert topo_result["authored"]["geometry"] == _EXPECTED_TOPOLOGY_AUTHORED["geometry"]
        # W16: one system_design_node_state row per DEVICE / RACK / CABLE
        # authored — written whether or not the caller named a status, which is
        # what makes 'planned' the status of a newly authored node.
        assert topo_result["authored"]["state"] == _EXPECTED_TOPOLOGY_AUTHORED["state"]
        # Second write on the same design — the token advanced, and it advanced
        # for a write that supplied no expected_version at all. A token that
        # untracked writes do not advance cannot detect them.
        assert topo_result["version"] == 2

    async def test_read_back_through_dispatch_returns_what_was_authored(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Author through dispatch, read back through W13a's tool, field by field."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id)

        payload = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))

        assert payload["design"] is not None
        assert payload["design"]["label"] == _DESIGN_LABEL
        assert payload["design"]["entity_type"] == "DESIGN"
        # W14: version is live. _author_full_design made two writes (FL then
        # topology), each of which increments, so the design stands at 2.
        assert payload["version"] == 2, "version is live from W14 and must advance per write"

        # W14 / debt D5: the rack the switch mounts into is projected now.
        assert [r["node"]["label"] for r in payload["racks"]] == [f"RACK:{_DESIGN_ID}:{_RACK_REF}"]
        assert payload["racks"][0]["capabilities"]["model_number"] == "MRK-4426"

        # W14: geometry, keyed by node label, exactly for the nodes that got it.
        assert set(payload["geometry"]) == {
            f"RACK:{_DESIGN_ID}:{_RACK_REF}",
            f"DEVICE:{_DESIGN_ID}:{_SWITCH_REF}",
            f"PORT:{_DESIGN_ID}:{_SWITCH_REF}:{_SWITCH_PORT_REF}",
            f"CABLE:{_DESIGN_ID}:{_CABLE_REF}",
            f"FL:{_NS_SLUG.upper()}:{_SITE.upper()}:{_BUILDING.upper()}",
            f"FL:{_NS_SLUG.upper()}:{_SITE.upper()}:{_BUILDING.upper()}:{_FLOOR.upper()}",
            f"FL:{_NS_SLUG.upper()}:{_SITE.upper()}:{_BUILDING.upper()}:"
            f"{_FLOOR.upper()}:{_ROOM.upper()}",
        }, (
            "geometry must contain exactly the nodes that were given geometry — "
            "no more (a writer that writes a row per node regardless) and no "
            "fewer (a writer that drops a nesting level)"
        )
        switch_geom = payload["geometry"][f"DEVICE:{_DESIGN_ID}:{_SWITCH_REF}"]
        assert switch_geom["x"] == 21.0 and switch_geom["y"] == 34.5
        assert switch_geom["rack_position"] == 8.5, "NUMERIC(4,1) must keep the half-U"
        assert switch_geom["rack_face"] == "front"
        cable_geom = payload["geometry"][f"CABLE:{_DESIGN_ID}:{_CABLE_REF}"]
        assert cable_geom["cable_length_m"] == 13.25
        assert cable_geom["cable_type"] == "Cat6A-W13B"
        room_geom = payload["geometry"][
            f"FL:{_NS_SLUG.upper()}:{_SITE.upper()}:{_BUILDING.upper()}:"
            f"{_FLOOR.upper()}:{_ROOM.upper()}"
        ]
        # Room DIMENSIONS are meta, in METERS — never x/y, which are grid units.
        assert room_geom["meta"]["copper.room.w"] == 7.5
        assert room_geom["meta"]["copper.room.h"] == 3.1
        assert room_geom["x"] == 101.5

        # The functional-location tree: SITE > BUILDING > FLOOR > ROOM > POSITION.
        fl_labels = [fl["label"] for fl in payload["functional_locations"]]
        prefix = f"FL:{_NS_SLUG.upper()}:{_SITE.upper()}"
        assert fl_labels == sorted(fl_labels)
        assert len(fl_labels) == 5, f"expected the 5-level FL tree, got {fl_labels}"
        assert prefix in fl_labels
        assert (
            f"{prefix}:{_BUILDING.upper()}:{_FLOOR.upper()}:"
            f"{_ROOM.upper()}:{_POSITION.upper()}" in fl_labels
        )

        # Devices, sorted by label, each with its own distinct capability values.
        devices_by_label = {d["node"]["label"]: d for d in payload["devices"]}
        switch_label = f"DEVICE:{_DESIGN_ID}:{_SWITCH_REF}"
        encoder_label = f"DEVICE:{_DESIGN_ID}:{_ENCODER_REF}"
        assert set(devices_by_label) == {switch_label, encoder_label}

        switch = devices_by_label[switch_label]
        assert switch["capabilities"]["manufacturer"] == "Crestron"
        assert switch["capabilities"]["model_number"] == "DM-NVX-384-W13B"
        assert switch["capabilities"]["power_draw_watts"] == 37.5
        assert switch["capabilities"]["heat_btu_hr"] == 128.25
        assert switch["capabilities"]["redundancy_role"] == "primary"

        encoder = devices_by_label[encoder_label]
        assert encoder["capabilities"]["manufacturer"] == "Extron"
        assert encoder["capabilities"]["model_number"] == "NAV-E-201-W13B"
        assert encoder["capabilities"]["power_draw_watts"] == 19.75
        assert encoder["capabilities"]["heat_btu_hr"] == 67.5
        assert encoder["capabilities"]["redundancy_role"] == "secondary"

        # Ports and their distinct signal attributes.
        assert len(switch["ports"]) == 1
        switch_port = switch["ports"][0]
        assert switch_port["node"]["label"] == (
            f"PORT:{_DESIGN_ID}:{_SWITCH_REF}:{_SWITCH_PORT_REF}"
        )
        assert switch_port["capabilities"]["signal_format"] == "Dante"
        assert switch_port["capabilities"]["signal_version"] == "4.2"
        assert switch_port["capabilities"]["port_direction"] == "output"
        assert switch_port["capabilities"]["poe_class"] == 4
        assert switch_port["capabilities"]["poe_watts"] == 25.5
        assert switch_port["capabilities"]["dante_rx_channels"] == 17
        assert switch_port["capabilities"]["dante_tx_channels"] == 23

        encoder_port = encoder["ports"][0]
        assert encoder_port["capabilities"]["signal_format"] == "HDBaseT"
        assert encoder_port["capabilities"]["port_direction"] == "input"
        assert encoder_port["capabilities"]["poe_class"] == 6
        assert encoder_port["capabilities"]["dante_rx_channels"] == 29

        # The cable authored off the connection.
        assert [c["label"] for c in payload["cables"]] == [f"CABLE:{_DESIGN_ID}:{_CABLE_REF}"]

        # Edges carry the {subject, predicate, object} contract shape.
        for edge in payload["edges"]:
            assert set(edge) == {"subject", "predicate", "object"}
        triples = {(e["subject"], e["predicate"], e["object"]) for e in payload["edges"]}
        assert (
            switch_port["node"]["label"],
            "connected_to",
            encoder_port["node"]["label"],
        ) in triples
        assert (switch_label, "mounted_in", f"RACK:{_DESIGN_ID}:{_RACK_REF}") in triples
        assert (_DESIGN_LABEL, "contains", f"DESIGN-LINE:{_DESIGN_ID}:{_LINE_REF}") in triples or (
            _DESIGN_LABEL,
            "contains",
            f"DESIGN_LINE:{_DESIGN_ID}:{_LINE_REF}",
        ) in triples

    async def test_copper_extra_keys_survive_the_write_verbatim(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rev 2 §5: NCE stores the reserved copper.* keys and interprets none of them."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id)

        payload = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        devices_by_label = {d["node"]["label"]: d for d in payload["devices"]}
        port = devices_by_label[f"DEVICE:{_DESIGN_ID}:{_SWITCH_REF}"]["ports"][0]
        extra = port["capabilities"]["extra"]

        assert extra == _COPPER_EXTRA
        assert json.dumps(extra, sort_keys=True) == json.dumps(_COPPER_EXTRA, sort_keys=True)
        assert isinstance(extra["copper.rear_position"], int), (
            "copper.rear_position must survive as an int — Copper reads it as one"
        )


# ---------------------------------------------------------------------------
# 4. Idempotency — the claim Copper's canvas depends on.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestIdempotency:
    async def test_double_call_produces_an_identical_read_back(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-authoring identical input converges: same read-back, same row counts.

        The row-count assertion is the load-bearing half.  An identical read-back
        alone could hide duplicate rows behind a de-duplicating reader; counting
        ``kg_nodes`` before and after proves nothing was written twice.  See the
        module docstring for the two out-of-tree mutations that isolate WHY.

        ``version`` is compared SEPARATELY and is expected to DIFFER (W14).
        It is the one field in the payload that must not converge: it counts
        writes, not rows.  If two identical authoring calls left it unchanged,
        a client holding the token from the first could overwrite the second
        write and never be told — which is the entire failure the token exists
        to prevent.  Popping it before the comparison rather than comparing the
        whole payload minus a hand-listed set is deliberate: a future key added
        to the reader stays inside this assertion instead of quietly escaping
        it.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _author_full_design(engine, ns_id)
        first = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        nodes_after_first = await _count_design_nodes(pg_pool, ns_id)

        await _author_full_design(engine, ns_id)
        second = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        nodes_after_second = await _count_design_nodes(pg_pool, ns_id)

        first_version = first.pop("version")
        second_version = second.pop("version")
        assert second == first, "a second identical authoring call changed the topology"
        assert nodes_after_second == nodes_after_first, (
            f"re-authoring duplicated kg_nodes rows: {nodes_after_first} -> {nodes_after_second}"
        )
        # Two writes per _author_full_design (FL then topology), so 2 then 4.
        assert (first_version, second_version) == (2, 4), (
            "version must advance on every write, including a re-author of "
            "identical input — otherwise a stale token holder is never told"
        )

    async def test_re_authoring_without_a_device_does_not_remove_it(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADDITIVE ONLY — idempotent is not the same as convergent.

        Neither core issues a ``DELETE``. Re-authoring a design with a device
        omitted leaves that device, its ports, its edges and its capability row
        in the graph, and the read surface still returns it. That is a real
        limitation of this surface, not an accident, and it is pinned here so
        the tool descriptions cannot drift back into promising a full-state sync
        — and so W17, which adds the removal path, has to change this test on
        purpose rather than silently invert the contract.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _author_full_design(engine, ns_id)
        encoder_label = f"DEVICE:{_DESIGN_ID}:{_ENCODER_REF}"

        before = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert encoder_label in {d["node"]["label"] for d in before["devices"]}

        # Re-author with the encoder dropped and no connections referencing it.
        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            _topology_args(ns_id, devices=[_DEVICES[0]], connections=[]),
        )

        after = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        labels = {d["node"]["label"] for d in after["devices"]}
        assert encoder_label in labels, (
            "the omitted device disappeared — if removal has landed, this surface "
            "is no longer additive-only and both tool descriptions plus the "
            "adapter docstrings must be updated with it"
        )
        assert after["cables"] == before["cables"], (
            "the cable authored by the dropped connection was removed"
        )

    async def test_double_call_returns_the_same_counts(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The counters report work attempted, and both calls attempt the same work.

        **``state`` is the one counter that legitimately differs, and W16 made it
        so deliberately.**  The graph and geometry writes are upserts, so both
        calls attempt the same work and report the same numbers.  A lifecycle row
        is not an upsert of the same work: it is recorded only when the node is
        genuinely NEW to the call or the caller sent a lifecycle key, so the
        second call — same payload, nothing new, no lifecycle key — correctly
        records nothing.

        That asymmetry IS the wave's one-way door.  If ``state`` were 4 twice,
        every ordinary canvas save would be minting a lifecycle for equipment
        that is already installed, which is what round 1 did and what B067g was
        rejected for.  So this test asserts the difference rather than papering
        over it.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        first = await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        second = await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))

        assert first["authored"] == _EXPECTED_TOPOLOGY_AUTHORED
        assert second["authored"] == {**_EXPECTED_TOPOLOGY_AUTHORED, "state": 0}, (
            "the second identical call recorded lifecycle state again — nothing "
            "was new and no lifecycle key was sent, so a canvas save is stamping "
            "already-installed equipment"
        )
        for counter in ("nodes", "edges", "capabilities", "geometry"):
            assert first["authored"][counter] == second["authored"][counter], counter
        # `version` is the ONE key that must differ between two identical calls:
        # idempotent means the same rows, not the same concurrency token. If it
        # did not advance, a client holding the first token could overwrite the
        # second write without ever being told.
        assert (first["version"], second["version"]) == (1, 2)

    async def test_edge_confidence_converges_on_the_last_write(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The UNIQUE index is reached through ON CONFLICT DO UPDATE, not DO NOTHING.

        Re-authoring the same connection with a different confidence must land
        the new value on the SAME row.  If the constraint were absent the second
        write could not resolve its conflict target at all, and if it were
        DO NOTHING the old confidence would survive — the assertion below tells
        those apart.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.db_utils import scoped_pg_session

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        revised = [dict(_CONNECTIONS[0], confidence=0.42)]
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, connections=revised))

        subject = f"PORT:{_DESIGN_ID}:{_SWITCH_REF}:{_SWITCH_PORT_REF}"
        obj = f"PORT:{_DESIGN_ID}:{_ENCODER_REF}:{_ENCODER_PORT_REF}"
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            rows = await conn.fetch(
                """
                SELECT confidence FROM kg_edges
                WHERE subject_label = $1 AND predicate = 'connected_to'
                  AND object_label = $2 AND namespace_id = $3::uuid
                """,
                subject,
                obj,
                str(ns_id),
            )
        assert len(rows) == 1, f"expected exactly one connected_to row, got {len(rows)}"
        assert rows[0]["confidence"] == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# 5. Owner-pool tenant isolation and ownership denial.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestTenancyAndOwnership:
    async def test_fully_colliding_tenants_never_see_each_others_content(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two tenants share every shared identifier and differ only in content.

        Same design_id, same slug, same site/building/floor/room/position, same
        device refs, same port refs, same rack ref, same cable ref, same
        design-line ref — so every *shared* label is byte-identical and no label
        difference can do the filtering for the predicate under test.  What
        differs is only what a caller reads back: manufacturer, model_number,
        signal_format, power_draw_watts, and the reserved ``copper.*`` values.

        This shape is deliberate.  A version of this test that gave the tenants
        different device refs exercised exactly ONE of ``read.py``'s five
        namespace predicates; two of the others could be deleted outright with
        the suite still green, and one of those leaked a foreign tenant's
        capability row — manufacturer, model_number and the ``copper.*`` keys
        Copper consumes.

        Each tenant additionally owns ONE private device the other does not:
        full collision alone is blind to a widened scope walk, because a walk
        that strays into the neighbouring tenant collects labels it already had.
        The private device is what a strayed walk can actually pick up.

        The §6.4 table in the wave report neuters each of the five predicates
        individually against this test and reports each result separately.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        for ns_id in (ns_a, ns_b):
            await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _author_colliding_design(engine, ns_a, "ALPHA")
        await _author_colliding_design(engine, ns_b, "BETA")

        read_a = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_a))
        read_b = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_b))

        # Precondition A: every SHARED label is identical, so nothing below can
        # pass because of a name difference.
        assert read_a["design"] == read_b["design"]
        assert [n["label"] for n in read_a["functional_locations"]] == [
            n["label"] for n in read_b["functional_locations"]
        ]
        assert [c["label"] for c in read_a["cables"]] == [c["label"] for c in read_b["cables"]]

        # Precondition B: the ONLY label that differs is each tenant's private
        # device — so a widened scope walk has something to stray onto.
        labels_a = {d["node"]["label"] for d in read_a["devices"]}
        labels_b = {d["node"]["label"] for d in read_b["devices"]}
        private_a = f"DEVICE:{_DESIGN_ID}:{_private_device_ref('ALPHA')}"
        private_b = f"DEVICE:{_DESIGN_ID}:{_private_device_ref('BETA')}"
        assert labels_a - labels_b == {private_a}
        assert labels_b - labels_a == {private_b}

        # No tenant may see the other's private node, in any bucket.
        for payload, foreign_label in ((read_a, private_b), (read_b, private_a)):
            assert foreign_label not in json.dumps(payload), (
                f"a foreign tenant's private node {foreign_label!r} appeared in "
                f"the read-back — the scope walk is crossing tenants"
            )

        # No bucket may contain the same label twice.  This is the only symptom
        # a cross-tenant read has when the two tenants' rows are label-identical
        # and carry no other projected content: the foreign row is not visibly
        # foreign, it just DOUBLES the caller's own.  Without this assertion a
        # missing namespace predicate on the node fetch is invisible.
        for payload, who in ((read_a, "ALPHA"), (read_b, "BETA")):
            buckets = {
                "devices": [d["node"]["label"] for d in payload["devices"]],
                # W14 / debt D5 — the newest bucket needs the same scan as the
                # rest of them; leaving it out would let exactly the merge this
                # test exists to catch through, in the one bucket this wave added.
                "racks": [r["node"]["label"] for r in payload["racks"]],
                "functional_locations": [n["label"] for n in payload["functional_locations"]],
                "cables": [c["label"] for c in payload["cables"]],
                "edges": [(e["subject"], e["predicate"], e["object"]) for e in payload["edges"]],
                # The geometry map is keyed by label, so duplicates are
                # impossible by construction — its keys are scanned for FOREIGN
                # labels instead, below.
            }
            for bucket, items in buckets.items():
                assert len(items) == len(set(items)), (
                    f"{who}'s {bucket} bucket contains duplicates — a foreign "
                    f"tenant's identically-labelled rows are being merged in: "
                    f"{items}"
                )
            for device in payload["devices"]:
                port_labels = [p["node"]["label"] for p in device["ports"]]
                assert len(port_labels) == len(set(port_labels)), (
                    f"{who}'s {device['node']['label']} has duplicate ports: {port_labels}"
                )

        # Each tenant's payload carries its OWN content and none of the other's.
        for tag, payload, foreign in (
            ("ALPHA", read_a, "BETA"),
            ("BETA", read_b, "ALPHA"),
        ):
            blob = json.dumps(payload)
            for own in _tenant_strings(tag):
                assert own in blob, f"{tag} lost its own content: {own!r} missing"
            for leaked in _tenant_strings(foreign):
                assert leaked not in blob, (
                    f"{tag}'s read leaked {foreign}'s content: {leaked!r}. Every "
                    f"label in the two graphs is identical, so the only thing "
                    f"that can keep them apart is the SQL namespace predicate."
                )

        # Spelled out at the exact field the earlier weak fixture let leak.
        switch_label = f"DEVICE:{_DESIGN_ID}:{_SWITCH_REF}"
        caps_a = {d["node"]["label"]: d for d in read_a["devices"]}[switch_label]
        caps_b = {d["node"]["label"]: d for d in read_b["devices"]}[switch_label]
        assert caps_a["capabilities"]["manufacturer"] == "ALPHA-CORP"
        assert caps_b["capabilities"]["manufacturer"] == "BETA-CORP"
        assert caps_a["ports"][0]["capabilities"]["extra"] == _tenant_extra("ALPHA")
        assert caps_b["ports"][0]["capabilities"]["extra"] == _tenant_extra("BETA")

    async def test_a_tenant_that_authored_nothing_reads_nothing(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The absence half: a design that exists only in another tenant reads as absent.

        Complements the collision test above — that one proves content does not
        cross between two populated tenants, this one proves an empty tenant
        does not inherit a populated one's design wholesale.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        for ns_id in (ns_a, ns_b):
            await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        await _author_full_design(engine, ns_a)

        read_b = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_b))
        assert read_b["design"] is None
        assert read_b["devices"] == []
        assert read_b["cables"] == []
        # W14 — the two buckets this wave added need the same assertion. An
        # empty-tenant test that omits them cannot see a leak into exactly the
        # surface area that is new, which is where a leak is most likely.
        assert read_b["racks"] == []
        assert read_b["geometry"] == {}
        assert read_b["version"] == 0, (
            "a tenant that authored nothing must read the initial token, not "
            "the colliding design's version from the tenant that did"
        )
        assert read_b["edges"] == []
        assert read_b["functional_locations"] == []

    @pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
    async def test_authoring_into_an_unseeded_namespace_is_denied(
        self,
        tool_name: str,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """assert_owner is deny-by-default: no registry row, no write, nothing left behind.

        The "nothing left behind" half matters: an error response alone would
        also be produced by a partial write that then failed.

        Both tools, not one: the two cores guard through different helpers —
        ``devices.py::_upsert_node`` covers DEVICE/PORT/RACK/CABLE, while
        ``graph.py`` guards FUNCTIONAL_LOCATION, DESIGN and DESIGN_LINE in three
        separate functions.  Exercising only the FL tool leaves the whole
        devices-side guard ungated, and it would then survive deletion.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        args = _topology_args if tool_name == _TOPOLOGY_TOOL else _fl_args
        ns_id: uuid.UUID = await make_namespace()  # deliberately NOT seeded
        engine = _EngineStub(pg_pool)

        payload = await _dispatch(engine, tool_name, args(ns_id))
        assert "error" in payload, "an unowned namespace was allowed to author nodes"
        assert await _count_design_nodes(pg_pool, ns_id) == 0, (
            "the denied call still left kg_nodes rows behind"
        )

    @pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
    async def test_the_same_call_succeeds_once_ownership_is_seeded(
        self,
        tool_name: str,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The control for the denial test above — otherwise it is vacuous.

        Without this, ``test_authoring_into_an_unseeded_namespace_is_denied``
        would keep passing if the tool were broken for some unrelated reason, or
        deleted outright.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        args = _topology_args if tool_name == _TOPOLOGY_TOOL else _fl_args
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)

        result = await _dispatch_ok(engine, tool_name, args(ns_id))
        assert result["authored"]["nodes"] > 0
        assert await _count_design_nodes(pg_pool, ns_id) > 0

    async def test_every_design_node_type_is_ownership_guarded(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Denial must hold per node type, not merely for whichever is written first.

        ``do_author_functional_location`` writes DESIGN before it writes any
        FUNCTIONAL_LOCATION, so a denial test that only checks "the call failed"
        is satisfied by the DESIGN guard alone and says nothing about the other
        two.  Seeding the registry with every row EXCEPT one node type at a time
        is what makes each individual guard load-bearing.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.db_utils import scoped_pg_session

        engine = _EngineStub(pg_pool)

        for node_type, tool_name in (
            ("DESIGN", _FL_TOOL),
            ("FUNCTIONAL_LOCATION", _FL_TOOL),
            ("DESIGN_LINE", _FL_TOOL),
            ("DEVICE", _TOPOLOGY_TOOL),
            ("RACK", _TOPOLOGY_TOOL),
            ("PORT", _TOPOLOGY_TOOL),
            ("CABLE", _TOPOLOGY_TOOL),
        ):
            ns_id: uuid.UUID = await make_namespace()
            await _seed_ownership(pg_pool, ns_id)
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                deleted = await conn.execute(
                    """
                    DELETE FROM node_ownership_registry
                    WHERE namespace_id = $1::uuid AND node_type = $2
                    """,
                    str(ns_id),
                    node_type,
                )
            assert deleted != "DELETE 0", (
                f"no ownership row for {node_type} to remove — this loop would "
                f"then be asserting nothing"
            )

            args = _topology_args if tool_name == _TOPOLOGY_TOOL else _fl_args
            payload = await _dispatch(engine, tool_name, args(ns_id))
            assert "error" in payload, (
                f"{tool_name} wrote a {node_type} node with no ownership row — "
                f"assert_owner is not guarding that type"
            )


# ---------------------------------------------------------------------------
# 6. ``actor`` — Rev 2 §1.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestActorAttribution:
    @pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
    async def test_actor_is_recorded_when_supplied(
        self,
        tool_name: str,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A supplied actor reaches the event payload verbatim."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id, actor=_ACTOR)

        events = await _authoring_events(pg_pool, ns_id, tool_name)
        assert events, f"no system_design_authored event recorded for {tool_name}"
        assert events[-1]["actor"] == _ACTOR

    @pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
    async def test_actor_is_absent_not_empty_when_omitted(
        self,
        tool_name: str,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An omitted actor is recorded as ABSENT — never "" and never a service id.

        The distinction is the point: a consumer must be able to tell "no human
        was named" from "a human named the empty string".
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id)

        events = await _authoring_events(pg_pool, ns_id, tool_name)
        assert events, f"no system_design_authored event recorded for {tool_name}"
        payload = events[-1]
        assert "actor" not in payload, (
            f"actor must be absent when omitted, got {payload.get('actor')!r}"
        )

    @pytest.mark.parametrize("tool_name", [_TOPOLOGY_TOOL, _FL_TOOL])
    async def test_actor_is_never_synthesised_from_the_authenticating_key(
        self,
        tool_name: str,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The API key proves which SERVICE is calling; it never becomes the actor.

        The call carries an ``mcp_api_key`` and a blank ``actor``.  Neither may
        turn into an attribution: the key is present in the argument bag, so a
        careless ``actor or mcp_api_key`` fallback would show up here.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        api_key = os.environ.get("NCE_MCP_API_KEY", "test-mcp-api-key-for-unit-tests")
        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id, actor="   ", mcp_api_key=api_key)

        events = await _authoring_events(pg_pool, ns_id, tool_name)
        assert events, f"no system_design_authored event recorded for {tool_name}"
        payload = events[-1]
        assert "actor" not in payload, (
            f"a whitespace-only actor became an attribution: {payload.get('actor')!r}"
        )
        assert api_key not in json.dumps(payload), (
            "the authenticating key leaked into the event payload"
        )

    async def test_event_payload_keeps_the_graph_write_base_contract(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The authoring event carries emit_graph_write's four base keys plus facts."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id, actor=_ACTOR)

        events = await _authoring_events(pg_pool, ns_id, _TOPOLOGY_TOOL)
        payload = events[-1]
        assert payload["design_label"] == _DESIGN_LABEL
        assert payload["namespace"] == str(ns_id)
        assert payload["design_id"] == _DESIGN_ID
        assert payload["tool"] == _TOPOLOGY_TOOL
        assert payload["authored"] == _EXPECTED_TOPOLOGY_AUTHORED
        # W14: the audit row records the geometry count too — an attribution
        # record that omits part of what was written is an incomplete record.
        assert payload["authored"]["geometry"] == _EXPECTED_TOPOLOGY_AUTHORED["geometry"]
        # W14 (F-A8): and the VERSION the write produced. Without it the WORM
        # log cannot be used to reconstruct the version timeline, which is the
        # one thing an append-only Merkle-chained audit substrate is for.
        # _author_full_design writes twice (FL then topology), so this row is 2.
        assert payload["version"] == 2, (
            "the audit row for an optimistic-concurrency write must carry the token it produced"
        )
        # agent_id (the service) and actor (the human) are separate facts and
        # must never be collapsed into one another.
        assert payload["_agent_id"] == "system-design-author"
        assert payload["actor"] == _ACTOR
        assert payload["_agent_id"] != payload["actor"]


# ---------------------------------------------------------------------------
# 7. Cache invalidation through the CACHED path — Rev 2 §3.
#
# These need a real Redis: the cache is a no-op without one, and a mocked client
# stores nothing, so a mocked version of these tests would pass on broken code.
# See the module docstring for the CI gap this leaves in the M6 job.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestCacheInvalidation:
    @pytest_asyncio.fixture()
    async def redis_client(self):
        """A real Redis, or a decision about WHY there isn't one.

        The distinction matters. "No Redis configured here" is a legitimate
        environment and a skip. "A Redis is configured but rejects us" is a
        broken environment, and skipping on it means the cached-path gate can
        silently stop running the day someone puts a password on the test Redis
        or fat-fingers ``NCE_TEST_REDIS_URL`` — the gate would report green
        forever while testing nothing. Only the first case skips; a
        misconfigured Redis FAILS, loudly, naming what it saw.
        """
        aioredis = pytest.importorskip("redis.asyncio")
        from redis.exceptions import AuthenticationError, ResponseError

        url = _redis_url()
        explicitly_configured = bool(
            os.environ.get("NCE_TEST_REDIS_URL") or os.environ.get("REDIS_URL")
        )
        client = aioredis.from_url(url)
        try:
            await client.ping()
        except (AuthenticationError, ResponseError) as exc:
            await client.aclose()
            pytest.fail(
                f"Redis at {url!r} is reachable but rejected us ({type(exc).__name__}: "
                f"{exc}). That is a misconfigured test environment, not an absent "
                f"one — supply working credentials in NCE_TEST_REDIS_URL. Skipping "
                f"here would leave the cache-invalidation gate silently inert."
            )
        except Exception as exc:
            await client.aclose()
            if explicitly_configured:
                pytest.fail(
                    f"A test Redis is configured at {url!r} but cannot be reached "
                    f"({type(exc).__name__}: {exc}). Configured-but-unreachable is a "
                    f"broken environment, not an absent one."
                )
            pytest.skip(
                f"No test Redis configured and none reachable at {url!r} "
                f"({type(exc).__name__}: {exc}). Set NCE_TEST_REDIS_URL to run the "
                f"cache-invalidation gate; CI's integration job sets it."
            )
        await _clear_test_cache_keys(client)
        try:
            yield client
        finally:
            await _clear_test_cache_keys(client)
            await client.aclose()

    async def test_mcp_write_makes_the_cached_read_serve_fresh_data(
        self,
        pg_pool: Any,
        make_namespace: Any,
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read (cached) → write → read (same cached path) must return fresh data.

        Invalidation is asserted by **object identity**: the exact key this
        design's read lives under is computed, proved to exist, and proved to
        still hold the STALE payload afterwards.  So the freshness cannot come
        from the entry having expired or never existed — it can only come from
        the cache-generation bump making that key unreachable.  Remove the bump
        in ``mcp_stdio_dispatch`` and this test goes RED.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool, redis_client=redis_client)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))

        # 1. Prime the cache through the cacheable read path.
        before = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert before["devices"] == []
        stale_key = await _topology_cache_key(redis_client, ns_id)
        stale_raw = await redis_client.get(stale_key)
        assert stale_raw is not None, (
            "the read did not cache anything, so this test cannot detect staleness"
        )

        # 2. Mutate through the authoring tool.
        generation_before = await _cache_generation(redis_client)
        await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id))
        generation_after = await _cache_generation(redis_client)
        assert generation_after > generation_before, (
            "the mutation did not bump the cache generation"
        )

        # 3. Read again through the SAME cacheable path.
        after = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert len(after["devices"]) == 2, "the cached read served pre-write data after a mutation"

        # 4. The stale entry is still physically there — unreachable, not evicted.
        assert await redis_client.get(stale_key) == stale_raw, (
            "the stale entry vanished, so this test proves nothing about the generation bump"
        )
        assert json.loads(stale_raw.decode())["devices"] == []

    async def test_rest_write_makes_the_cached_mcp_read_serve_fresh_data(
        self,
        pg_pool: Any,
        make_namespace: Any,
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The REST route never reaches the dispatch loop, so it must bump for itself.

        Deleting ``bump_mcp_cache_generation`` from
        ``api_system_design_author_topology`` turns this RED while every other
        test in this file stays green — which is what makes that call
        load-bearing rather than decorative.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool, redis_client=redis_client)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        await _dispatch_ok(engine, _FL_TOOL, _fl_args(ns_id))
        before = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert before["devices"] == []
        stale_key = await _topology_cache_key(redis_client, ns_id)
        assert await redis_client.get(stale_key) is not None

        response = await routes.api_system_design_author_topology(
            _StubRequest(_topology_args(ns_id))
        )
        assert response.status_code == 200, response.body
        rest_body = json.loads(response.body)
        assert rest_body["authored"] == _EXPECTED_TOPOLOGY_AUTHORED
        # The REST twin returns the same `version` its MCP twin does. Without it
        # a REST client cannot stay in the optimistic-concurrency loop without a
        # re-read, and the two surfaces would have different payload shapes.
        assert rest_body["version"] == 2

        after = await _dispatch_ok(engine, _READ_TOOL, _read_args(ns_id))
        assert len(after["devices"]) == 2, (
            "a REST-surface write left the cacheable MCP read serving stale data"
        )
        assert await redis_client.get(stale_key) is not None


# ---------------------------------------------------------------------------
# 8. REST surface behaviour that is not about the cache.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestRestSurface:
    async def test_functional_location_route_authors_and_reports_counts(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await routes.api_system_design_author_functional_location(
            _StubRequest(_fl_args(ns_id))
        )
        assert response.status_code == 200, response.body
        assert json.loads(response.body) == {
            "status": "ok",
            "authored": _EXPECTED_FL_AUTHORED,
            "version": 1,
        }

    @pytest.mark.parametrize(
        "route_name,args_builder",
        [
            ("api_system_design_author_topology", _topology_args),
            ("api_system_design_author_functional_location", _fl_args),
        ],
    )
    async def test_rest_conflict_carries_the_same_discriminator_as_mcp(
        self,
        route_name: str,
        args_builder: Any,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The HTTP conflict carries the same discriminator as the MCP one.

        Both surfaces read ``reason`` from the one definition on
        ``VersionConflictError``, so this is what stops them drifting apart the
        way a hand-copied string would.

        A stale token against a design that has NEVER been authored is the
        sharpest case: version 0 is the floor, so 3 can only be stale, and
        "wrote nothing" is provable as a node count of exactly zero rather than
        as a delta.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes
        from nce.vertical_modules.system_design.geometry import VersionConflictError

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)

        response = await getattr(routes, route_name)(
            _StubRequest(args_builder(ns_id, expected_version=3))
        )
        assert response.status_code == 409
        body = json.loads(response.body)
        assert body["reason"] == VersionConflictError.reason
        assert body["parameter"] == "expected_version"
        assert body["actual_version"] == 0
        assert await _count_design_nodes(pg_pool, ns_id) == 0, (
            "the refused request still wrote to the graph — the conflict check "
            "ran after the graph writes, or the transaction did not roll back"
        )

    @pytest.mark.parametrize(
        "route_name,body",
        [
            ("api_system_design_author_topology", {"design_id": _DESIGN_ID}),
            ("api_system_design_author_functional_location", {"design_id": _DESIGN_ID}),
        ],
    )
    async def test_rest_requires_namespace_id(
        self,
        route_name: str,
        body: dict[str, Any],
        pg_pool: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)
        response = await getattr(routes, route_name)(_StubRequest(body))
        assert response.status_code == 422
        assert "namespace_id" in json.loads(response.body)["error"]

    @pytest.mark.parametrize(
        "route_name,args_builder",
        [
            ("api_system_design_author_topology", _topology_args),
            ("api_system_design_author_functional_location", _fl_args),
        ],
    )
    async def test_rest_ownership_denial_is_403_not_500(
        self,
        route_name: str,
        args_builder: Any,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A deny-by-default refusal is an authorisation outcome, not a server fault.

        ``OwnershipError`` is not a ``ValueError``, so before this fix both routes
        let it reach ``except Exception`` and answered a correct, expected refusal
        with HTTP 500 — putting the reason in ``detail`` where a caller cannot act
        on it, and showing operators a phantom 5xx for ordinary tenant behaviour.

        The MCP path had denial coverage; the REST path had none at all.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        ns_id: uuid.UUID = await make_namespace()  # deliberately NOT seeded
        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)

        response = await getattr(routes, route_name)(_StubRequest(args_builder(ns_id)))
        assert response.status_code == 403, (
            f"ownership denial answered {response.status_code}: {response.body!r}"
        )
        body = json.loads(response.body)
        assert body["reason"] == "ownership_denied"
        assert await _count_design_nodes(pg_pool, ns_id) == 0, (
            "the denied request still wrote to the graph"
        )

    async def test_rest_ownership_denial_is_distinguishable_from_validation(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """403 (not permitted) and 422 (malformed) must not collapse into each other.

        A client retrying a 422 with corrected arguments is doing the right
        thing; retrying a 403 that way never succeeds. Same route, same tenant —
        only the failure differs.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        ns_id: uuid.UUID = await make_namespace()  # NOT seeded
        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)

        denied = await routes.api_system_design_author_topology(_StubRequest(_topology_args(ns_id)))
        malformed = await routes.api_system_design_author_topology(
            _StubRequest({"namespace_id": str(ns_id), "design_id": _DESIGN_ID})
        )
        assert denied.status_code == 403
        assert malformed.status_code == 422
        assert json.loads(malformed.body).get("reason") != "ownership_denied"

    async def test_rest_requires_devices_on_the_topology_route(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing required core argument is a 422, not a 500."""
        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        ns_id: uuid.UUID = await make_namespace()
        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)

        response = await routes.api_system_design_author_topology(
            _StubRequest({"namespace_id": str(ns_id), "design_id": _DESIGN_ID})
        )
        assert response.status_code == 422
        assert "devices" in json.loads(response.body)["error"]


# ---------------------------------------------------------------------------
# 9. Outbox delivery — the authoring events must have somewhere to land.
#
# The two cores emit one ``<TYPE>.upserted`` outbox event per owned node.  Those
# cores were unreachable before this wave, so those events had never fired in
# production; putting them on the wire fires them for the first time.  The relay
# treats an event with no registered handler as a hard delivery failure:
# ``deliver_one`` raises ``OutboxDeliveryError``, the row is stamped
# ``attempt_count = MAX_OUTBOX_ATTEMPTS`` and copied into ``dead_letter_queue``,
# and -- this is the part that compounds -- ``published_at`` is never set, so
# the row also stays in ``outbox_events`` forever, inside the
# ``idx_outbox_unpublished`` partial index that every tenant's relay poll reads
# oldest-first.  Two unretained tables grow per authoring call.
#
# These tests fail on the commit that first put the tools on the wire.  That is
# deliberate: they are the RED half of the fix.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOutboxDelivery:
    @pytest.fixture(autouse=True)
    def _subscribers_registered(self):
        """Register the System Design subscribers for the duration of each test.

        ``OUTBOX_HANDLERS`` is process-global mutable state, so it is snapshotted
        and restored rather than left mutated for whatever runs next.

        This fixture makes the handler reachable; it says nothing about whether
        production registers it.  That is a separate claim with its own tests --
        ``test_cron_boot_registers_system_design_subscribers`` and
        ``test_mcp_stdio_main_registers_subscribers_before_the_relay_starts`` --
        because a ``register_*`` function that nothing calls is exactly the
        defect that left the other bootstraps dead on main.
        """
        from nce.outbox_relay import restore_handlers, snapshot_handlers
        from nce.vertical_modules.system_design.subscribers import (
            register_system_design_subscribers,
        )

        saved = snapshot_handlers()
        register_system_design_subscribers()
        try:
            yield
        finally:
            restore_handlers(saved)

    async def test_authoring_events_are_delivered_not_dead_lettered(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every event an authoring call emits must be delivered by the relay.

        Asserts three things that fail together on an unsubscribed selector:
        nothing reaches the DLQ, every outbox row is marked published, and the
        relay reports delivering exactly the number of events that were written.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.db_utils import scoped_pg_session
        from nce.outbox_relay import run_outbox_relay_once

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            emitted = int(
                await conn.fetchval(
                    "SELECT count(*) FROM outbox_events WHERE namespace_id = $1::uuid",
                    str(ns_id),
                )
            )
        assert emitted > 0, "the authoring calls emitted no outbox events at all"

        await _purge_orphaned_outbox_rows(pg_pool)

        # Drain rather than take a single pass. The relay polls GLOBALLY and
        # oldest-first, so one bounded pass can be filled entirely by rows from
        # namespaces earlier tests left behind -- and since this wave writes WORM
        # ``event_log`` rows, those namespaces now survive teardown by design
        # (``tests/conftest.py::_drop_namespaces``), so their outbox rows are not
        # orphans and the purge above does not touch them. A single pass would
        # then report this namespace's rows as "unpublished" when the truth is
        # "not yet polled" -- a different bug, and one that would make this gate
        # flaky instead of strict.
        delivered = 0
        for _ in range(_MAX_RELAY_DRAIN_PASSES):
            passed = await run_outbox_relay_once(pg_pool, batch_size=200)
            delivered += passed
            if passed == 0:
                break

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            unpublished = await conn.fetch(
                """
                SELECT event_type, attempt_count, error_message
                FROM outbox_events
                WHERE namespace_id = $1::uuid AND published_at IS NULL
                ORDER BY event_type
                """,
                str(ns_id),
            )
            dlq = await conn.fetch(
                """
                SELECT task_name, attempt_count
                FROM dead_letter_queue
                WHERE namespace_id = $1::uuid
                ORDER BY task_name
                """,
                str(ns_id),
            )

        assert not dlq, (
            f"{len(dlq)} authoring event(s) dead-lettered instead of delivered: "
            f"{[(r['task_name'], r['attempt_count']) for r in dlq]}. Every event "
            f"type these cores emit needs a registered outbox subscriber."
        )
        assert not unpublished, (
            f"{len(unpublished)} outbox row(s) left unpublished — they stay in "
            f"idx_outbox_unpublished forever and slow every tenant's relay poll: "
            f"{[(r['event_type'], r['attempt_count']) for r in unpublished]}"
        )
        assert delivered >= emitted, (
            f"relay delivered {delivered} events in total but this namespace "
            f"alone emitted {emitted}"
        )

    async def test_every_emitted_selector_has_a_registered_subscriber(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Name the gap precisely: which selectors do these cores emit unhandled?

        The behavioural test above says "something dead-lettered".  This one says
        WHICH event types have no subscriber, so a future node type added to the
        cores fails with a message that names it rather than a bare count.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.db_utils import scoped_pg_session
        from nce.outbox_relay import OUTBOX_HANDLERS

        ns_id: uuid.UUID = await make_namespace()
        await _seed_ownership(pg_pool, ns_id)
        engine = _EngineStub(pg_pool)
        await _author_full_design(engine, ns_id)

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT event_type
                FROM outbox_events
                WHERE namespace_id = $1::uuid
                ORDER BY event_type
                """,
                str(ns_id),
            )
        emitted_selectors = {r["event_type"] for r in rows}
        assert emitted_selectors, "no outbox events emitted"

        unhandled = sorted(s for s in emitted_selectors if not OUTBOX_HANDLERS.get(s))
        assert not unhandled, (
            f"these selectors are emitted by the System Design authoring cores "
            f"but have no registered outbox subscriber, so every one of them "
            f"dead-letters: {unhandled}"
        )


# ---------------------------------------------------------------------------
# 10. Registration wiring — a register_*() nobody calls is the actual defect.
#
# `register_automation_subscribers()` has sat on main with zero callers, which
# is why "the module exists and exports a register function" is not evidence of
# anything. OUTBOX_HANDLERS is per-process state, so BOTH relay-running
# processes must call it: nce/cron.py and nce/mcp_stdio_main.py.
# ---------------------------------------------------------------------------


_SD_SELECTORS = frozenset(
    {
        "DESIGN.upserted",
        "DESIGN_LINE.upserted",
        "FUNCTIONAL_LOCATION.upserted",
        "DEVICE.upserted",
        "PORT.upserted",
        "RACK.upserted",
        "CABLE.upserted",
    }
)


def test_subscriber_is_a_module_level_coroutine_so_it_deduplicates() -> None:
    """Registering twice must not double-invoke the handler on every event.

    ``register_handler`` dedups with ``if fn in handlers`` — by equality. A
    closure or ``functools.partial`` built per call is a fresh object each time
    and compares unequal, so a double bootstrap would append it once per call
    and the relay would run it N times per event. Only a single module-level
    function has the identity needed to be deduplicated.
    """
    from nce.outbox_relay import OUTBOX_HANDLERS, restore_handlers, snapshot_handlers
    from nce.vertical_modules.system_design.subscribers import (
        handle_system_design_node_upserted,
        register_system_design_subscribers,
    )

    saved = snapshot_handlers()
    try:
        register_system_design_subscribers()
        register_system_design_subscribers()
        register_system_design_subscribers()
        for selector in _SD_SELECTORS:
            handlers = OUTBOX_HANDLERS.get(selector) or []
            occurrences = [h for h in handlers if h is handle_system_design_node_upserted]
            assert len(occurrences) == 1, (
                f"{selector} registered {len(occurrences)} times after three "
                f"register calls — the handler is not deduplicating, so the relay "
                f"would invoke it once per registration for every event"
            )
    finally:
        restore_handlers(saved)


@pytest.mark.asyncio
async def test_cron_boot_registers_system_design_subscribers() -> None:
    """cron's scheduler startup must register the subscribers.

    Drives the real ``async_main`` with the tick functions patched out, in the
    same shape as ``tests/test_cron_chain_verify.py``. This is a behavioural
    check: it fails if someone deletes the call, moves it after the relay job is
    scheduled, or lets an exception skip it.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from nce.cron import cfg as cron_cfg
    from nce.outbox_relay import OUTBOX_HANDLERS, restore_handlers, snapshot_handlers

    class _StopMain(Exception):
        pass

    saved = snapshot_handlers()
    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    try:
        # Start from a state where the selectors are definitively absent, so a
        # pass cannot be inherited from an earlier test in this session.
        restore_handlers({})
        assert not any(OUTBOX_HANDLERS.get(s) for s in _SD_SELECTORS)

        with (
            patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("asyncio.Event.wait", side_effect=_StopMain),
            # cfg.validate() enforces MINIO_ACCESS_KEY and friends, which are not
            # part of this claim and are unset in a bare checkout. The sibling
            # cron boot test (tests/test_cron_chain_verify.py) omits this and is
            # red on main for exactly that reason; not inheriting it.
            patch("nce.cron.cfg.validate"),
            # async_main opens with a real sleep of
            # U(0, cfg.CRON_STARTUP_JITTER_MAX_SECONDS), and that config defaults to
            # 60.0 (config.py) -- so without this the test's runtime is a uniform draw
            # across the entire 60s pytest timeout and it dies on roughly one run in
            # five. And it does not die alone: --timeout-method=thread kills the whole
            # pytest process, so one bad draw takes every other M6 file with it and
            # reds the integration job on unrelated PRs.
            #
            # Pinned via the config rather than by patching random.uniform: patching
            # that would replace the attribute on the shared stdlib module for the
            # duration, and this way the real draw still happens (returning 0.0) and
            # the real `if jitter > 0.0` branch is still evaluated.
            patch.object(cron_cfg, "CRON_STARTUP_JITTER_MAX_SECONDS", 0.0),
            patch("nce.cron._renewal_tick", new_callable=AsyncMock),
            patch("nce.cron._reembedding_tick", new_callable=AsyncMock),
            patch("nce.cron._consolidation_tick", new_callable=AsyncMock),
            patch("nce.cron._partition_maintenance_tick", new_callable=AsyncMock),
            patch("nce.cron._saga_recovery_tick", new_callable=AsyncMock),
            patch("nce.cron._outbox_relay_tick", new_callable=AsyncMock),
            patch("nce.cron._decay_prune_tick", new_callable=AsyncMock),
            patch("nce.cron._chain_verification_tick", new_callable=AsyncMock),
            patch("nce.cron._d365_sync_tick", new_callable=AsyncMock),
            patch("nce.cron._d365_netbox_bridge_tick", new_callable=AsyncMock),
            patch("nce.cron.AsyncIOScheduler") as scheduler_cls,
        ):
            scheduler_cls.return_value = MagicMock()
            from nce.cron import async_main

            try:
                await async_main()
            except _StopMain:
                pass

        missing = sorted(s for s in _SD_SELECTORS if not OUTBOX_HANDLERS.get(s))
        assert not missing, (
            f"cron booted its outbox relay without registering these selectors: "
            f"{missing}. Every event they carry would dead-letter in the cron "
            f"process even though the MCP process handles them."
        )
    finally:
        restore_handlers(saved)


def test_mcp_stdio_main_registers_subscribers_before_the_relay_starts() -> None:
    """The MCP stdio process must register, and register BEFORE it starts polling.

    Structural, not behavioural, and labelled as such: ``run_stdio_server``
    connects a real engine, warms governance and takes over stdio, so driving it
    under pytest would test the mocks rather than the wiring. The AST check below
    is weaker than ``test_cron_boot_registers_system_design_subscribers`` and is
    the honest best available for this entry point — it catches deletion and
    mis-ordering, but not a call that is skipped at runtime.
    """
    import ast
    import pathlib

    source = pathlib.Path("nce/mcp_stdio_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    server_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_stdio_server"
        ),
        None,
    )
    assert server_fn is not None, "run_stdio_server not found"

    register_lines = [
        node.lineno
        for node in ast.walk(server_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_system_design_subscribers"
    ]
    assert register_lines, (
        "run_stdio_server never calls register_system_design_subscribers(); the "
        "System Design outbox selectors would have no handler in the MCP process "
        "and every authoring event it relays would dead-letter"
    )

    relay_task_lines = [
        node.lineno
        for node in ast.walk(server_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_tracked_task"
        and any(
            isinstance(kw.value, ast.Constant) and kw.value.value == "outbox_relay_loop"
            for kw in node.keywords
        )
    ]
    assert relay_task_lines, "the outbox_relay_loop task was not found"
    assert min(register_lines) < min(relay_task_lines), (
        "register_system_design_subscribers() runs AFTER the relay loop task is "
        "created — the first poll can race it and dead-letter live events"
    )
