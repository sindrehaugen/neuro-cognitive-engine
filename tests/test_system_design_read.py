"""
tests/test_system_design_read.py
================================
Module 6 Wave 13a — the ``system_design_get_topology`` read surface.

What these tests actually gate
------------------------------
1. **Author → read-back through the DISPATCH path.**  The point of this wave is
   that the topology cores stopped being unreachable, so the read-back goes
   through ``execute_call_tool`` (registry lookup, auth, governance, cache,
   quota, handler) — *not* by calling ``do_get_topology`` directly.  Calling the
   core directly would still pass if the tool were never registered, which is
   precisely the defect this wave exists to fix.

2. **Owner-pool tenant isolation.**  ``nce_app`` is used for exactly one thing
   in this deployment (a boot-time WORM self-check) and never to serve a
   request, and the capability table's RLS policy is written ``FOR ALL TO
   nce_app``.  Every request therefore runs on a pool whose role that policy
   does not cover — so what actually isolates tenants is the explicit
   ``namespace_id`` predicate in each of read.py's **leaf** queries, not RLS.
   The test collides **every** label across two namespaces so that fixture
   uniqueness cannot pass it, and asserts on content and cardinality.  Removing
   any one of the four leaf predicates — in ``_fetch_nodes_by_labels``,
   ``_fetch_edges_within``, ``_fetch_capabilities_by_labels`` or (W16b)
   ``_fetch_node_state_by_labels`` — turns this test RED.  The namespace pin
   inside ``_fetch_nodes_by_labels``' ``statuses`` sub-query is a fifth, and it
   is gated in ``tests/test_system_design_status_filter.py`` instead: it is
   reachable only on a FILTERED read, which this class does not make.  The
   scope-walk pair in ``_fetch_design_scope_labels`` is NOT
   gated here and is not part of the tenant boundary: neutering it (either half
   or both) leaves this suite green.  See the §6.4 table in the wave report.

3. **Rev 2 §5 ``extra`` passthrough.**  The reserved ``copper.*`` keys must come
   back with every key and value intact — unfiltered and unvalidated.  NCE
   stores, Copper interprets.  Note the column is ``JSONB``, which does not
   preserve key order, so the guarantee is *value* identity (``==``, and
   ``json.dumps(..., sort_keys=True)``), never literal byte identity.

4. **No documented no-op is left.**  ``statuses`` was the last one: W13a
   declared it and ignored it, and this file pinned that ignoring so the wave
   implementing it would have to change a test on purpose.  **M6.W16b is that
   wave**, so the no-op test MOVED to the live behaviour rather than being
   deleted — exactly what happened to ``version``, which W14 made live and whose
   "reads null" test became "reads the real stored token".  The filter's own
   depth (absence, NULL status, tenant isolation, the per-predicate mutation
   table) is in ``tests/test_system_design_status_filter.py``; what stays here is
   the *read contract* claim — passing ``statuses`` narrows the three
   state-bearing buckets and leaves the rest of the shape alone.

5. **W14 additions — ``racks``, ``geometry`` and a live ``version``.**  RACK
   nodes have been authored since W12 and this reader projected none of them
   (debt D5, wider than the ledger's wording: the *node* was missing, not only
   its capability row), so ``racks`` is asserted both as a bucket and by
   content.  ``geometry`` is asserted to be keyed by node label, to carry
   JSON-native numbers rather than ``Decimal``, and to OMIT a node that has no
   geometry row — "never placed" and "placed at the origin" must stay
   distinguishable.  The per-predicate mutation table for the two new leaf
   queries lives in ``tests/test_system_design_geometry.py``.

6. **W16b addition — ``state``.**  Per-node lifecycle state, a flat map keyed by
   node label like ``geometry``, carrying ``status``/``revision``/``salience``.
   Asserted here for the contract shape and, load-bearingly, for the two nodes
   that must NOT be in it: a PORT (migration 061's CHECK refuses a PORT row) and
   the DESIGN itself.  ``salience`` must arrive JSON-native, not as a
   ``Decimal``-turned-string.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).  The file
name matches the ``tests/test_system_design_*.py`` CI glob wired by B067a, so it
runs in CI with no workflow edit.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.system_design.read import do_get_topology

# ---------------------------------------------------------------------------
# Shared seed data.
# ---------------------------------------------------------------------------

_MOCK_EMIT_GRAPH = "nce.vertical_modules.system_design.graph.emit_graph_write"
_MOCK_EMIT_DEVICES = "nce.vertical_modules.system_design.devices.emit_graph_write"

#: The rack every seeded design mounts its device into (W14, debt D5).  Before
#: W14 the reader dropped the whole RACK bucket on the floor, so a rack in the
#: fixture was authored, stored and unreadable.
_RACK_REF = "RACK-W13A-A"


#: Geometry seeded for the device (W14).  Deliberately a DIFFERENT number in
#: every member, so a transposed assignment between x/y or between
#: rack_position and any other numeric fails rather than passing by coincidence.
#: ``rack_position`` is a half-U value to prove NUMERIC(4,1) keeps the .5.
#:
#: **Parameterised by tenant.**  Round 1 handed both tenants the identical
#: geometry, which meant this file could not have detected a geometry leak at
#: all — the two tenants were indistinguishable in exactly the bucket the wave
#: added.  ``cable_type`` carries the tenant tag because it is a STRING: every
#: number could coincide across tenants unnoticed, a tag cannot.
def _device_geometry(tag: str = "ALPHA") -> dict[str, Any]:
    alpha = tag == "ALPHA"
    return {
        "x": 41.0 if alpha else 902.5,
        "y": 73.5 if alpha else 913.25,
        "rack_position": 12.5 if alpha else 33.5,
        "rack_face": "rear" if alpha else "front",
        "cable_type": f"{tag}-READTAG",
        "meta": {
            "copper.room.w": 6.25 if alpha else 17.75,
            "copper.room.d": 4.75,
            "copper.room.h": 2.85,
            "tenant": tag,
        },
    }


#: The default tenant's geometry, kept as a module constant so the
#: single-tenant assertions below read unchanged.
_DEVICE_GEOMETRY: dict[str, Any] = _device_geometry()

# The reserved Copper component-class keys (Rev 2 §5).  NCE must store and
# return these verbatim and must NOT give them meaning.
_COPPER_EXTRA: dict[str, Any] = {
    "copper.port_kind": "front",
    "copper.rear_port": "PORT:DESIGN-W13A-READ-001:PATCHPANEL:REAR1",
    "copper.rear_position": 3,
}

_BUILDINGS = [
    {
        "name": "MainBuilding",
        "floors": [
            {
                "name": "Floor1",
                "rooms": [{"name": "ConfRoom101", "positions": ["POS-A"]}],
            }
        ],
    }
]


# Every seeded design uses these refs.  The isolation test deliberately reuses
# them across BOTH namespaces so that every label collides and the tenants can
# be told apart ONLY by capability content — see TestOwnerPoolIsolation.
_DEVICE_REF = "SWITCH"
_PORT_REF = "HDMI_OUT1"


def _devices_for(
    device_ref: str = _DEVICE_REF,
    *,
    manufacturer: str = "Extron",
    extra: dict[str, Any] | None = None,
    revision: str | None = None,
) -> list[dict[str, Any]]:
    """One device with an output port carrying the reserved ``copper.*`` keys.

    ``manufacturer``, ``extra`` and ``revision`` are the only
    tenant-distinguishing content, so a caller can seed two namespaces whose
    labels are byte-identical.

    ``revision`` (W16b) is what makes the two tenants' **lifecycle state** rows
    differ.  Without it both tenants' devices carry the same seeded ``'planned'``
    status and the same nulls, and an isolation assertion on ``state`` would be
    satisfied by reading either tenant's row — a confounded fixture of exactly
    the kind that failed B067b.  It is a lifecycle key, so it also proves the
    state row reaches this file's fixture at all.
    """
    device_state: dict[str, Any] = {} if revision is None else {"revision": revision}
    return [
        {
            "device_ref": device_ref,
            **device_state,
            "capability": {
                "device_category": "AV Switchers",
                "manufacturer": manufacturer,
                "model_number": "SW-4-HDMI",
                "power_draw_watts": 12.0,
                "heat_btu_hr": 41.0,
                "redundancy_role": "standalone",
            },
            "ports": [
                {
                    "port_ref": _PORT_REF,
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "output",
                        "extra": _COPPER_EXTRA if extra is None else extra,
                    },
                }
            ],
            "rack_ref": _RACK_REF,
        }
    ]


class _EngineStub:
    """Engine surface the dispatch loop touches.

    ``redis_client=None`` is deliberate: it makes the response cache a no-op, so
    a passing read-back proves the query ran rather than that a cached payload
    was replayed.
    """

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.redis_client = None


async def _seed_design(
    pg_pool: Any,
    ns_id: uuid.UUID,
    *,
    namespace_slug: str,
    design_id: str,
    device_ref: str = _DEVICE_REF,
    manufacturer: str = "Extron",
    extra: dict[str, Any] | None = None,
    revision: str | None = None,
    with_geometry: bool = True,
    geometry_tag: str = "ALPHA",
) -> None:
    """Author a DESIGN with its FL tree, one device and one rack (W14)."""
    from nce.auth import set_namespace_context
    from nce.db_utils import scoped_pg_session
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
    from nce.vertical_modules.system_design.devices import do_author_device_topology
    from nce.vertical_modules.system_design.geometry import do_author_geometry
    from nce.vertical_modules.system_design.graph import do_author_functional_location

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await set_namespace_context(conn, ns_id)
            await seed_node_ownership_registry(conn, ns_id)

    with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_functional_location(
                conn,
                ns_id,
                namespace_slug=namespace_slug,
                design_id=design_id,
                site_name="SiteAlpha",
                buildings=_BUILDINGS,
            )

    devices = _devices_for(device_ref, manufacturer=manufacturer, extra=extra, revision=revision)
    racks = [
        {
            "rack_ref": _RACK_REF,
            "capability": {
                "device_category": "Rack Enclosure",
                "manufacturer": manufacturer,
                "model_number": "MRK-W13A",
            },
        }
    ]

    with patch(_MOCK_EMIT_DEVICES, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_device_topology(
                conn,
                ns_id,
                design_id=design_id,
                devices=devices,
                racks=racks,
            )

    # Geometry is written through the geometry core on its own, NOT through the
    # authoring adapter: the adapter would also create the design's version row,
    # and this file's version test has to be able to observe a design that has
    # never been authored through a surface.
    if with_geometry:
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_geometry(
                conn,
                ns_id,
                design_id=design_id,
                devices=[dict(devices[0], geometry=_device_geometry(geometry_tag))],
                racks=racks,
            )


async def _read_through_dispatch(
    engine: Any,
    ns_id: uuid.UUID,
    design_id: str,
    **extra_args: Any,
) -> dict[str, Any]:
    """Call ``system_design_get_topology`` through the real MCP dispatch path."""
    from nce.mcp_stdio_dispatch import execute_call_tool

    arguments: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": design_id,
    }
    arguments.update(extra_args)

    parts = await execute_call_tool(engine, "system_design_get_topology", arguments)
    assert parts, "dispatch returned no content"
    payload = json.loads(parts[0].text)
    assert "error" not in payload, f"dispatch returned an error envelope: {payload}"
    return payload


# ---------------------------------------------------------------------------
# 1. Registration — the surface exists on BOTH registries.
#
# These are the unit-level half of the §6.4 rows "tool removed from
# TOOL_REGISTRY" and "tool removed from TOOLS": a tool missing from TOOL_REGISTRY
# is undispatchable, and one missing from TOOLS is invisible to tools/list.
# ---------------------------------------------------------------------------


def test_tool_is_dispatchable() -> None:
    """system_design_get_topology is in TOOL_REGISTRY with Copper's exact flags."""
    from nce.tool_registry import TOOL_REGISTRY

    assert "system_design_get_topology" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["system_design_get_topology"]
    assert spec.cacheable is True
    assert spec.admin_only is False
    assert spec.mutation is False


def test_tool_is_advertised() -> None:
    """system_design_get_topology is in TOOLS, so tools/list can see it."""
    from nce.mcp_stdio_tools import TOOLS

    advertised = {tool.name for tool in TOOLS}
    assert "system_design_get_topology" in advertised, (
        "Tool is dispatchable but not advertised: absent from TOOLS it is "
        "invisible to tools/list, which is how a client discovers it."
    )


def test_rest_route_is_wired() -> None:
    """GET /api/system-design/topology is mounted on the admin app."""
    from nce.admin_app import app

    matches = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/system-design/topology"
    ]
    assert matches, "GET /api/system-design/topology is not mounted"
    assert "GET" in matches[0].methods


def test_validation_queries_imports_the_one_query_set() -> None:
    """validation_queries must reuse read.py's readers, not hold a second copy."""
    from nce.vertical_modules.system_design import read, validation_queries

    for name in (
        "_fetch_port_directions",
        "_fetch_connections_and_capabilities",
        "_fetch_device_capabilities",
        "_fetch_port_capabilities",
    ):
        assert getattr(validation_queries, name) is getattr(read, name), (
            f"{name} is not the same object in both modules — the query set has "
            f"been copied instead of shared."
        )


# ---------------------------------------------------------------------------
# 2. Integration — author, then read back through dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestGetTopologyReadBack:
    _NS_SLUG = "w13a-read"
    _DESIGN_ID = "DESIGN-W13A-READ-001"
    _DEVICE_REF = "SWITCH"

    async def test_read_back_through_dispatch_returns_every_contract_field(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Author a design, then read it back through the MCP dispatch path."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )

        payload = await _read_through_dispatch(_EngineStub(pg_pool), ns_id, self._DESIGN_ID)

        # Every field of the contract shape is present.
        for field in (
            "design",
            "functional_locations",
            "devices",
            # W14 (debt D5): RACK was authored from W12 and projected by nothing.
            "racks",
            "cables",
            "edges",
            # W14: the canvas layout, keyed by node label.
            "geometry",
            # W16b: per-node lifecycle state, keyed by node label.
            "state",
            "version",
        ):
            assert field in payload, f"contract field {field!r} missing from result"

        assert payload["design"] is not None
        assert payload["design"]["label"] == f"DESIGN:{self._DESIGN_ID}"
        assert payload["design"]["entity_type"] == "DESIGN"

        # The functional-location tree came back.
        fl_labels = [fl["label"] for fl in payload["functional_locations"]]
        assert fl_labels, "functional_locations is empty"
        assert all(
            fl["entity_type"] == "FUNCTIONAL_LOCATION" for fl in payload["functional_locations"]
        )

        # The device, its capabilities, and its nested ports came back.
        assert len(payload["devices"]) == 1
        device = payload["devices"][0]
        assert device["node"]["label"] == f"DEVICE:{self._DESIGN_ID}:{self._DEVICE_REF}"
        assert device["node"]["entity_type"] == "DEVICE"
        assert device["capabilities"]["manufacturer"] == "Extron"
        # NUMERIC columns must arrive JSON-native, not as Decimal-turned-string.
        assert device["capabilities"]["power_draw_watts"] == 12.0
        assert isinstance(device["capabilities"]["power_draw_watts"], float)

        assert len(device["ports"]) == 1
        port = device["ports"][0]
        assert port["node"]["entity_type"] == "PORT"
        assert port["capabilities"]["signal_format"] == "HDMI"

        # Edges use the {subject, predicate, object} contract shape.
        assert payload["edges"]
        for edge in payload["edges"]:
            assert set(edge) == {"subject", "predicate", "object"}
        predicates = {edge["predicate"] for edge in payload["edges"]}
        assert "contains" in predicates
        assert "has_port" in predicates

        # W14 / debt D5 — the RACK bucket. Before this wave `_SCOPE_PREDICATES`
        # pulled RACK into scope and `_group_nodes_by_type` bucketed it, and the
        # result dict then dropped the bucket entirely: a rack was write-only.
        rack_label = f"RACK:{self._DESIGN_ID}:{_RACK_REF}"
        assert len(payload["racks"]) == 1, (
            f"expected exactly the one seeded rack, got {payload['racks']}"
        )
        rack = payload["racks"][0]
        assert set(rack) == {"node", "capabilities"}, (
            "racks must use the same {node, capabilities} shape devices uses "
            "(minus ports — a rack has none; devices mount INTO it via mounted_in)"
        )
        assert rack["node"]["label"] == rack_label
        assert rack["node"]["entity_type"] == "RACK"
        # The capability row too, not merely the node: authoring wrote one and
        # nothing read it back.
        assert rack["capabilities"]["model_number"] == "MRK-W13A"
        assert rack["capabilities"]["device_category"] == "Rack Enclosure"
        # And the rack really is reachable from the device, so this is the rack
        # the elevation would be drawn from.
        assert {
            "subject": device["node"]["label"],
            "predicate": "mounted_in",
            "object": rack_label,
        } in payload["edges"]

        # W14 — geometry, keyed by node label.
        geometry = payload["geometry"]
        assert isinstance(geometry, dict)
        device_geometry = geometry[device["node"]["label"]]
        assert device_geometry["x"] == 41.0
        assert device_geometry["y"] == 73.5
        # NUMERIC(4,1) keeps the half-U, and it must arrive JSON-native rather
        # than as a Decimal-turned-string — the same guarantee the capability
        # NUMERICs carry, and the reason the conversion lives in the core.
        assert device_geometry["rack_position"] == 12.5
        assert isinstance(device_geometry["rack_position"], float)
        assert device_geometry["rack_face"] == "rear"
        # Room dimensions are NOT x/y: they are in meta, in METERS (Rev 2 §4).
        assert device_geometry["meta"]["copper.room.w"] == 6.25
        assert device_geometry["meta"]["copper.room.h"] == 2.85
        # A node with NO geometry row is ABSENT from the map, not present with
        # null members — otherwise "never placed" and "placed at the origin"
        # would be indistinguishable.
        assert port["node"]["label"] not in geometry
        assert payload["design"]["label"] not in geometry, (
            "the DESIGN label keys the VERSION row, the other key grain — it "
            "must never surface as geometry"
        )

        # W16b — state, keyed by node label, same flat-map shape as geometry.
        state = payload["state"]
        assert isinstance(state, dict)
        # This fixture authors the device and the rack through the core, so 67g's
        # writer sees them as NEW to the call and seeds the default status.  That
        # is the only path on which a status appears without the caller naming
        # one, and it is newness — never silence, never a missing row.
        device_state = state[device["node"]["label"]]
        assert set(device_state) == {"status", "revision", "salience"}, (
            f"state entries carry exactly status/revision/salience, got {device_state}"
        )
        assert device_state["status"] == "planned"
        # Nothing was declared beyond the lifecycle, so these are null rather
        # than zero/empty-string: the reader defaults NOTHING.
        assert device_state["revision"] is None
        assert device_state["salience"] is None
        # The rack is new to the same call, and 'planned' is in the RACK
        # vocabulary too — the default applies to newness, not to a node type.
        assert state[rack_label]["status"] == "planned"
        # A PORT cannot carry lifecycle state at all — migration 061's composite
        # CHECK refuses the row structurally, so the map cannot hold the key.
        assert port["node"]["label"] not in state, (
            "a PORT appeared in state; NetBox has no lifecycle status for a port "
            "and migration 061 refuses the row"
        )
        assert payload["design"]["label"] not in state
        for fl in payload["functional_locations"]:
            assert fl["label"] not in state

    async def test_extra_passthrough_is_byte_identical(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reserved copper.* keys round-trip verbatim — unfiltered, unvalidated.

        Rev 2 §5: NCE has one PORT node type; Copper distinguishes NetBox
        component classes and persists that distinction in ``extra``.  NCE stores,
        Copper interprets — so every key must survive untouched, including the
        int-valued ``copper.rear_position``.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )

        payload = await _read_through_dispatch(_EngineStub(pg_pool), ns_id, self._DESIGN_ID)
        port = payload["devices"][0]["ports"][0]
        extra = port["capabilities"]["extra"]

        assert extra == _COPPER_EXTRA, (
            f"extra was not returned verbatim.\nseeded: {_COPPER_EXTRA}\nread:   {extra}"
        )
        # Byte-identical once canonicalised: same keys, same values, same types.
        assert json.dumps(extra, sort_keys=True) == json.dumps(_COPPER_EXTRA, sort_keys=True)
        assert extra["copper.rear_position"] == 3
        assert isinstance(extra["copper.rear_position"], int), (
            "copper.rear_position must survive as an int — Copper reads it as one"
        )

    async def test_version_is_zero_for_a_design_never_authored_through_a_surface(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """W14 made version live; an unauthored design reads 0, not null.

        This fixture calls the domain cores directly, so no authoring adapter
        ran and no version row exists.  ``0`` rather than ``null`` because
        ``0`` is a token the caller can pass straight back as
        ``expected_version`` to mean "I expect this design to be untouched" —
        which ``null`` cannot express.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )

        payload = await _read_through_dispatch(_EngineStub(pg_pool), ns_id, self._DESIGN_ID)
        assert "version" in payload, "version must be present in the contract from W13a"
        assert payload["version"] == 0, (
            "a design with no version row reads 0 — the live token's floor"
        )
        assert isinstance(payload["version"], int) and not isinstance(payload["version"], bool)

    async def test_version_reflects_the_stored_token(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reader returns the REAL stored version, not a synthesised one.

        Without this the previous test passes against a reader that still
        hard-codes a constant — 0 is as easy to hard-code as None.  Here the
        version row is bumped twice out of band and the reader has to follow.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.geometry import bump_design_version

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )

        async with scoped_pg_session(pg_pool, ns_id) as conn:
            assert await bump_design_version(conn, ns_id, self._DESIGN_ID, None) == 1
            assert await bump_design_version(conn, ns_id, self._DESIGN_ID, None) == 2

        payload = await _read_through_dispatch(_EngineStub(pg_pool), ns_id, self._DESIGN_ID)
        assert payload["version"] == 2, "the reader must return the stored token, not a constant"

    async def test_statuses_is_a_live_filter_and_narrows_only_the_state_buckets(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """THE MOVED TEST (M6.W16b).

        This was ``test_statuses_is_accepted_and_ignored_until_w16``, which
        asserted ``with_statuses == without``.  W13a wrote it precisely so the
        wave that implemented filtering could not leave the no-op documented; it
        is moved to the live behaviour here, not deleted.

        The three values below are the ones the no-op test used, and they are
        still exactly right for the live one: none of them is in any NetBox
        vocabulary, so the correct live answer is that every state-bearing bucket
        empties while the structure stays.  ``read.py`` validates the SHAPE of
        ``statuses`` and never its VOCABULARY — that lives once, in migration
        061's CHECK — so an unknown value is a well-formed request that matches
        nothing rather than an error.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )
        engine = _EngineStub(pg_pool)

        without = await _read_through_dispatch(engine, ns_id, self._DESIGN_ID)
        # POSITIVE CONTROL for the assertions below: unfiltered, the buckets
        # this filter empties are non-empty, so "they are empty" is a result and
        # not the fixture never having had anything in them.
        assert without["devices"] and without["racks"], (
            "the fixture seeded no device or no rack, so the filter assertions "
            "below would pass against any implementation"
        )

        with_statuses = await _read_through_dispatch(
            engine,
            ns_id,
            self._DESIGN_ID,
            statuses=["DRAFT", "APPROVED", "NONSENSE"],
        )

        assert with_statuses != without, (
            "statuses is LIVE from M6.W16b and must change the result — this is "
            "the assertion that replaced the no-op claim"
        )
        assert with_statuses["devices"] == []
        assert with_statuses["racks"] == []
        assert with_statuses["cables"] == []

        # …and it narrows ONLY those three.  The structure, the canvas and the
        # token are untouched, so a caller can still see what a filtered-out
        # node was attached to and why it was excluded.  Changing this is a
        # contract change and has to change this assertion on purpose.
        for untouched in ("design", "functional_locations", "edges", "geometry", "version"):
            assert with_statuses[untouched] == without[untouched], (
                f"{untouched!r} was narrowed by statuses; the filter is a view of "
                f"the lifecycle-bearing nodes, not a subgraph"
            )
        assert with_statuses["state"] == without["state"], (
            "state was narrowed by statuses; the caller must still be able to see "
            "the status that excluded a node"
        )

        # A REAL status the fixture does hold selects the device back.  Without
        # this the assertions above pass against a filter that always returns
        # nothing.
        matching = await _read_through_dispatch(
            engine, ns_id, self._DESIGN_ID, statuses=["planned"]
        )
        assert [d["node"]["label"] for d in matching["devices"]] == [
            d["node"]["label"] for d in without["devices"]
        ]
        assert [r["node"]["label"] for r in matching["racks"]] == [
            r["node"]["label"] for r in without["racks"]
        ]

    async def test_statuses_empty_and_absent_both_mean_no_filter(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``[]`` is "I named no statuses", not "match nothing".

        The REST adapter already encodes that reading — it maps an absent query
        parameter to ``None`` through ``getlist(...) or None`` — so the core has
        to agree with it, or the two surfaces answer the same request
        differently.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_id,
            namespace_slug=self._NS_SLUG,
            design_id=self._DESIGN_ID,
            device_ref=self._DEVICE_REF,
        )
        engine = _EngineStub(pg_pool)

        without = await _read_through_dispatch(engine, ns_id, self._DESIGN_ID)
        assert without["devices"], "fixture seeded no device"

        # Annotated: mypy cannot infer ``list[str] | None`` from the tuple.
        no_filters: list[list[str] | None] = [[], None]
        for empty in no_filters:
            same = await _read_through_dispatch(engine, ns_id, self._DESIGN_ID, statuses=empty)
            assert same == without, f"statuses={empty!r} changed the result"

    async def test_unknown_design_reads_as_absent(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A design that does not exist in this namespace reads as empty, not as an error."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        payload = await _read_through_dispatch(
            _EngineStub(pg_pool), ns_id, "DESIGN-W13A-DOES-NOT-EXIST"
        )

        assert payload["design"] is None
        assert payload["devices"] == []
        assert payload["edges"] == []


# ---------------------------------------------------------------------------
# 3. Owner-pool tenant isolation — the load-bearing claim of this wave.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnerPoolIsolation:
    """Two tenants whose designs collide on EVERY label must not bleed.

    Construction note (§6.4) — this is the whole point of the test.  Both
    namespaces are seeded with the identical namespace slug, design id, device
    ref and port ref, so **every** node label, and therefore every edge, is
    byte-identical across the two tenants.  The only difference is capability
    *content*: ``manufacturer`` and the reserved ``copper.*`` keys in ``extra``.

    An earlier version of this test gave the two tenants different device refs.
    That was a confounded fixture: only the ``DESIGN:`` label collided, and a
    DESIGN node carries no capability row and no distinguishing content, so a
    foreign DESIGN row read through the node or capability query was invisible.
    Two of read.py's namespace predicates could then be deleted with the whole
    suite green.  Colliding every label is what makes each leaf predicate both
    load-bearing and observable.

    Why the owner pool matters: ``nce_app`` is used for exactly one thing in
    this deployment (a boot-time WORM self-check) and never to serve a request,
    and ``system_design_device_capabilities``'s RLS policy is written ``FOR ALL
    TO nce_app``.  Every request therefore runs on a role that policy does not
    cover, so what actually separates these tenants is the explicit
    ``namespace_id`` predicate in each of read.py's leaf queries — not RLS.
    """

    _DESIGN_ID = "DESIGN-W13A-SHARED-LABEL"
    _SLUG = "w13a-shared"

    _MFR_A = "ALPHA-CORP"
    _MFR_B = "BETA-CORP"
    _EXTRA_A = {"copper.port_kind": "front", "copper.rear_position": 1}
    _EXTRA_B = {"copper.port_kind": "rear", "copper.rear_position": 99}

    # W16b — the per-tenant LIFECYCLE content.  Both tenants' devices are new to
    # their own authoring call, so both carry the same seeded 'planned' status;
    # the revision is what tells the two state rows apart, and it is why the
    # ``state`` assertion below can detect a missing namespace predicate at all.
    _REVISION_A = "ALPHA-REV-13A"
    _REVISION_B = "BETA-REV-13A"

    # Every label below is identical in BOTH namespaces.
    _DEVICE_LABEL = f"DEVICE:{_DESIGN_ID}:{_DEVICE_REF}"
    _PORT_LABEL = f"PORT:{_DESIGN_ID}:{_DEVICE_REF}:{_PORT_REF}"
    # W14 / debt D5 — identical in BOTH namespaces, like every other label here.
    _RACK_LABEL = f"RACK:{_DESIGN_ID}:{_RACK_REF}"

    async def _seed_both(self, pg_pool: Any, make_namespace: Any) -> tuple[Any, Any]:
        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed_design(
            pg_pool,
            ns_a,
            namespace_slug=self._SLUG,
            design_id=self._DESIGN_ID,
            manufacturer=self._MFR_A,
            extra=self._EXTRA_A,
            revision=self._REVISION_A,
            geometry_tag="ALPHA",
        )
        await _seed_design(
            pg_pool,
            ns_b,
            namespace_slug=self._SLUG,
            design_id=self._DESIGN_ID,
            manufacturer=self._MFR_B,
            extra=self._EXTRA_B,
            revision=self._REVISION_B,
            geometry_tag="BETA",
        )
        return ns_a, ns_b

    def _assert_tenant_sees_only_itself(
        self,
        payload: dict[str, Any],
        *,
        own_mfr: str,
        own_extra: dict[str, Any],
        own_tag: str,
        own_revision: str,
    ) -> None:
        """Assert the read holds this tenant's content and nothing else.

        Each assertion is tied to one leaf predicate:
          * device / FL / rack count -> ``_fetch_nodes_by_labels``
          * capability content       -> ``_fetch_capabilities_by_labels``
          * edge count               -> ``_fetch_edges_within``
          * geometry content         -> ``geometry.fetch_geometry_by_labels``
          * version                  -> ``geometry.fetch_design_version``
          * state content            -> ``_fetch_node_state_by_labels`` (W16b)

        The last three are W14's and were missing from this helper in round 1,
        so the two buckets the wave added went unchecked here.  ``TestOwner
        PoolIsolation`` in ``tests/test_system_design_geometry.py`` covers them
        properly; this is the same claim asserted where this file already makes
        it, rather than a file that quietly stops short of its own new surface.
        """
        # _fetch_edges_within FIRST: every edge collides across the two tenants,
        # so losing its predicate duplicates each edge.  This check leads because
        # duplicated ``has_port`` edges also feed ``ports_of_device`` in
        # read.py's _build_devices — without it the port-count assertion below
        # trips first and reports a symptom instead of the cause.
        edges = [(e["subject"], e["predicate"], e["object"]) for e in payload["edges"]]
        assert len(edges) == len(set(edges)), (
            f"duplicate edges — the other tenant's identically-labelled edges "
            f"leaked in: {sorted({e for e in edges if edges.count(e) > 1})}"
        )

        # _fetch_nodes_by_labels: the label exists in both tenants, so losing
        # its predicate returns the row twice.
        assert len(payload["devices"]) == 1, (
            f"expected exactly 1 device, got {len(payload['devices'])} — the "
            f"other tenant's identically-labelled DEVICE node leaked in"
        )
        device = payload["devices"][0]
        assert device["node"]["label"] == self._DEVICE_LABEL

        # _fetch_capabilities_by_labels: both tenants hold a capability row on
        # the SAME node_label, so losing its predicate lets one silently
        # overwrite the other in the by-label dict.  ``== own_mfr`` is the whole
        # assertion: reading the foreign value fails it, and so does reading any
        # other tenant's value, which a ``!= foreign_mfr`` check would miss.
        assert device["capabilities"]["manufacturer"] == own_mfr, (
            f"read the wrong tenant's capability row: expected {own_mfr!r}, "
            f"got {device['capabilities']['manufacturer']!r}"
        )

        assert len(device["ports"]) == 1, (
            f"expected exactly 1 port, got {len(device['ports'])} — the other "
            f"tenant's identically-labelled PORT node or has_port edge leaked in"
        )
        port = device["ports"][0]
        assert port["node"]["label"] == self._PORT_LABEL
        assert port["capabilities"]["extra"] == own_extra, (
            f"read the wrong tenant's port capability extra: expected "
            f"{own_extra}, got {port['capabilities']['extra']}"
        )

        fl_labels = [fl["label"] for fl in payload["functional_locations"]]
        assert len(fl_labels) == len(set(fl_labels)), (
            f"duplicate functional locations: {sorted(fl_labels)}"
        )

        # W14 / debt D5 — the rack node and its capability row.
        rack_labels = [r["node"]["label"] for r in payload["racks"]]
        assert rack_labels == [self._RACK_LABEL], (
            f"expected exactly this tenant's one rack, got {rack_labels} — the "
            f"other tenant's identically-labelled RACK node leaked in"
        )
        assert payload["racks"][0]["capabilities"]["manufacturer"] == own_mfr

        # W14 — geometry.fetch_geometry_by_labels' namespace predicate. Both
        # tenants hold a geometry row under the SAME node_label, so losing it
        # lets one silently overwrite the other in the by-label dict.
        own_geometry = _device_geometry(own_tag)
        entry = payload["geometry"][self._DEVICE_LABEL]
        assert entry["cable_type"] == own_geometry["cable_type"], (
            f"read the wrong tenant's geometry row: expected "
            f"{own_geometry['cable_type']!r}, got {entry['cable_type']!r}"
        )
        assert entry["x"] == own_geometry["x"]
        assert entry["rack_face"] == own_geometry["rack_face"]
        assert entry["meta"]["tenant"] == own_tag

        # W14 — geometry.fetch_design_version. Both tenants' version rows share
        # the design label; neither was authored through a surface, so both
        # stand at the initial token.
        assert payload["version"] == 0

        # W16b — _fetch_node_state_by_labels' namespace predicate. Both tenants
        # hold a state row under the SAME node_label, so losing it lets one
        # silently overwrite the other in the by-label dict. ``revision`` is the
        # assertion, not ``status``: both tenants' devices are new to their own
        # authoring call and therefore both carry the seeded 'planned', so a
        # status check here would pass while reading the wrong tenant's row.
        device_state = payload["state"][self._DEVICE_LABEL]
        assert device_state["revision"] == own_revision, (
            f"read the wrong tenant's state row: expected {own_revision!r}, "
            f"got {device_state['revision']!r}"
        )
        assert device_state["status"] == "planned"
        # The PORT cannot hold state at all, in either tenant.
        assert self._PORT_LABEL not in payload["state"]

    async def test_same_design_label_in_two_namespaces_does_not_bleed(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read both tenants through dispatch; each must see only its own content."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_a, ns_b = await self._seed_both(pg_pool, make_namespace)
        engine = _EngineStub(pg_pool)

        payload_a = await _read_through_dispatch(engine, ns_a, self._DESIGN_ID)
        payload_b = await _read_through_dispatch(engine, ns_b, self._DESIGN_ID)

        self._assert_tenant_sees_only_itself(
            payload_a,
            own_mfr=self._MFR_A,
            own_extra=self._EXTRA_A,
            own_tag="ALPHA",
            own_revision=self._REVISION_A,
        )
        self._assert_tenant_sees_only_itself(
            payload_b,
            own_mfr=self._MFR_B,
            own_extra=self._EXTRA_B,
            own_tag="BETA",
            own_revision=self._REVISION_B,
        )

    async def test_core_read_is_isolated_without_the_dispatch_layer(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """The isolation lives in read.py's SQL, not in anything above it.

        Same assertions, calling the core directly, so a future change that adds
        tenant filtering at the dispatch layer cannot make the SQL predicates
        look unnecessary.
        """
        ns_a, ns_b = await self._seed_both(pg_pool, make_namespace)
        engine = _EngineStub(pg_pool)

        result_a = await do_get_topology(
            engine, {"namespace_id": ns_a, "design_id": self._DESIGN_ID}
        )
        self._assert_tenant_sees_only_itself(
            result_a,
            own_mfr=self._MFR_A,
            own_extra=self._EXTRA_A,
            own_tag="ALPHA",
            own_revision=self._REVISION_A,
        )

        # BOTH tenants, not just the first. Reading only one side cannot tell a
        # predicate that always returns tenant A's rows from one that works:
        # tenant A's assertions pass either way. (Round-1 omission in this file;
        # the dispatch-level test above already read both.)
        result_b = await do_get_topology(
            engine, {"namespace_id": ns_b, "design_id": self._DESIGN_ID}
        )
        self._assert_tenant_sees_only_itself(
            result_b,
            own_mfr=self._MFR_B,
            own_extra=self._EXTRA_B,
            own_tag="BETA",
            own_revision=self._REVISION_B,
        )


# ---------------------------------------------------------------------------
# 4. Argument validation (pure — no DB).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_namespace_id_raises() -> None:
    with pytest.raises(ValueError, match="namespace_id"):
        await do_get_topology(_EngineStub(None), {"design_id": "DESIGN-X"})


@pytest.mark.asyncio
async def test_missing_design_id_raises() -> None:
    with pytest.raises(ValueError, match="design_id"):
        await do_get_topology(_EngineStub(None), {"namespace_id": str(uuid.uuid4())})
