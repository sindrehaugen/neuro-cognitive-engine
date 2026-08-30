"""
tests/test_system_design_status_filter.py
=========================================
Module 6 Wave 16b — the LIVE ``statuses`` filter and the per-node ``state`` map
on ``system_design_get_topology``.

The filename matches the ``tests/test_system_design_*.py`` CI glob B067a wired
(``.github/workflows/ci.yml``), so this file runs in CI with no workflow edit.
Any other filename would need a ``ci.yml`` change, which is a scope change.

What these tests actually gate
------------------------------
1. **THE ONE-WAY DOOR: absence is never coalesced.**  ``system_design_node_state``
   distinguishes three facts and W17's retirement guard denies on two of them:
   *no row at all*, *a row whose ``status`` is NULL*, and *a row with a status*.
   Everything authored before M6.W16 has no row, and 67g's writer keeps it that
   way.  This file asserts, directly and from both ends:

   * a node with **no state row** is **absent from ``state``** and does not
     appear anywhere in the payload carrying a status;
   * a node with **no state row never matches a ``statuses`` filter** — not
     ``['planned']``, not any other value;
   * a node whose **``status`` is NULL** is *present* in ``state`` with
     ``status: null`` (a different fact from the first) and **also matches no
     filter**.

   Coalesce any of those and legacy as-built equipment silently starts looking
   planned, and therefore retirable.

2. **The filter is a SQL predicate, and it is the tenant boundary.**  The pools
   that serve requests are owner pools and bypass ``FORCE ROW LEVEL SECURITY``,
   so a Python-side discard would be both a wasted fetch and a tenancy smell.
   Two new predicates carry the boundary here — the ``namespace_id`` pin inside
   ``_fetch_nodes_by_labels``' ``statuses`` sub-query, and the one in
   ``_fetch_node_state_by_labels`` — and each is mutated on its own in the wave
   report's table.

   **Fixture construction (§6.4), and it is the whole point.**  Both tenants are
   seeded with the identical namespace slug, design id and every device / port /
   rack / cable ref, so **every node label is byte-identical across the two
   tenants**.  They differ only in *content*: the ``revision`` and ``salience``
   on every state row, and — load-bearingly for the filter's own predicate — the
   ``status`` of one device, which is ``'staged'`` in ALPHA and ``'offline'`` in
   BETA.  Without that last difference the sub-query's namespace pin could be
   deleted with this file green, because a filter that matched the *other*
   tenant's identical status would return the same rows.  A fixture whose
   tenants differ by label cannot detect a predicate that filters by label, and
   B067b failed TAG on exactly that.

3. **What the filter narrows, and what it must not.**  ``devices``, ``racks``
   and ``cables`` narrow; ``design``, ``functional_locations``, ``edges``,
   ``geometry``, ``state`` and ``version`` do not.  A filtered read is a view of
   the lifecycle-bearing nodes, not a subgraph — so the caller can still see what
   a filtered-out node was attached to, and the status that excluded it.  DESIGN,
   FUNCTIONAL_LOCATION and PORT cannot hold a state row at all (migration 061's
   ``ELSE FALSE``), so filtering them would return ``design: null`` for every
   filtered read — and ``design: null`` already means "this design does not exist
   in your namespace", which is a load-bearing isolation signal.

4. **Shape is validated, vocabulary is not.**  ``read.py`` refuses a ``statuses``
   that is not an array of strings, and refuses a bare ``str`` rather than
   wrapping it.  It does **not** check values against the NetBox vocabulary:
   that vocabulary is migration 061's composite CHECK and lives in exactly one
   place, the same rule ``devices.py`` follows on the write side.  An unknown
   status is a well-formed request that matches nothing.

5. **The cache key discriminates on ``statuses``.**  ``system_design_get_topology``
   is ``cacheable=True``.  A filtered read that hashed to the same key as an
   unfiltered one would serve the unfiltered payload — silently, and only under
   a warm cache, which no DB-level test can see.

Every negative assertion in this file has a positive control next to it: if a
test says "X is not there", something nearby proves the same check *can* see an
X when one exists.  A dead pattern satisfies a negative assertion forever.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.system_design.devices import (
    DEFAULT_NODE_STATUS,
    cable_label,
    device_label,
    port_label,
    rack_label,
)
from nce.vertical_modules.system_design.read import do_get_topology

_MOCK_EMIT_GRAPH = "nce.vertical_modules.system_design.graph.emit_graph_write"
_MOCK_EMIT_DEVICES = "nce.vertical_modules.system_design.devices.emit_graph_write"

_TOOL = "system_design_get_topology"

# ---------------------------------------------------------------------------
# Fixture identifiers.  EVERY ONE of these is shared by both tenants; only
# CONTENT differs.  See the module docstring for why that is not a style choice.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W16B-FILTER-001"
_NS_SLUG = "w16b-filter"
_DESIGN_LABEL = f"DESIGN:{_DESIGN_ID}"

#: Authored with an explicit ``status='active'``.
_REF_ACTIVE = "DEV-ACTIVE"
#: Authored with an explicit ``status='planned'`` — the same value the writer
#: seeds for a new node, so a test that filters on it cannot tell "declared" from
#: "seeded" unless it also uses :data:`_REF_LEGACY`, which has neither.
_REF_PLANNED = "DEV-PLANNED"
#: Pre-wave node, then re-authored with a ``revision`` and nothing else: a row
#: whose ``status`` IS NULL.  The middle of the three states.
_REF_NULL_STATUS = "DEV-REVONLY"
#: Pre-wave node, never re-authored: NO ROW AT ALL.  The one-way door.
_REF_LEGACY = "DEV-LEGACY"
#: The device whose STATUS differs per tenant — the only way this file can see
#: the namespace pin inside the filter's sub-query.
_REF_PER_TENANT = "DEV-TENANT"

_PORT_REF = "ETH-1"
_RACK_REF = "RACK-A"
_CABLE_REF = "CBL-A"

_LABEL_ACTIVE = device_label(_DESIGN_ID, _REF_ACTIVE)
_LABEL_PLANNED = device_label(_DESIGN_ID, _REF_PLANNED)
_LABEL_NULL_STATUS = device_label(_DESIGN_ID, _REF_NULL_STATUS)
_LABEL_LEGACY = device_label(_DESIGN_ID, _REF_LEGACY)
_LABEL_PER_TENANT = device_label(_DESIGN_ID, _REF_PER_TENANT)
_LABEL_PORT = port_label(_DESIGN_ID, _REF_ACTIVE, _PORT_REF)
_LABEL_RACK = rack_label(_DESIGN_ID, _RACK_REF)
_LABEL_CABLE = cable_label(_DESIGN_ID, _CABLE_REF)

#: Per-tenant CONTENT.  ``status`` differs ONLY on :data:`_REF_PER_TENANT`;
#: every other node carries the same status in both tenants on purpose, so the
#: filter tests read the same in either tenant and the isolation tests have one
#: node where a leak is visible through the filter itself.
_TENANT_STATUS = {"ALPHA": "staged", "BETA": "offline"}
_TENANT_REVISION = {"ALPHA": "ALPHA-REV-16B", "BETA": "BETA-REV-16B"}
_TENANT_SALIENCE = {"ALPHA": 0.25, "BETA": 0.75}

_STATUS_ACTIVE = "active"
_STATUS_PLANNED = "planned"
_STATUS_CABLE = "connected"
_STATUS_RACK = "active"


class _EngineStub:
    """Engine surface the dispatch loop touches.

    ``redis_client=None`` makes the response cache a no-op, so every read below
    proves the query ran rather than that a payload was replayed.  The cache
    KEY is gated separately, and without a Redis, in
    :func:`test_the_cache_key_discriminates_on_statuses`.
    """

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.redis_client = None


async def _read(
    engine: Any,
    ns_id: uuid.UUID,
    **extra: Any,
) -> dict[str, Any]:
    """Read the fixture design through the real MCP dispatch path."""
    from nce.mcp_stdio_dispatch import execute_call_tool

    arguments: dict[str, Any] = {"namespace_id": str(ns_id), "design_id": _DESIGN_ID}
    arguments.update(extra)
    parts = await execute_call_tool(engine, _TOOL, arguments)
    assert parts, "dispatch returned no content"
    payload = json.loads(parts[0].text)
    assert "error" not in payload, f"dispatch returned an error envelope: {payload}"
    return payload


def _device_labels(payload: dict[str, Any]) -> list[str]:
    return sorted(d["node"]["label"] for d in payload["devices"])


def _rack_labels(payload: dict[str, Any]) -> list[str]:
    return sorted(r["node"]["label"] for r in payload["racks"])


def _cable_labels(payload: dict[str, Any]) -> list[str]:
    return sorted(c["label"] for c in payload["cables"])


async def _insert_pre_wave_node(
    pg_pool: Any,
    ns_id: uuid.UUID,
    label: str,
    entity_type: str,
    *,
    predicate: str,
) -> None:
    """Author a node the way everything before W16 did: kg_nodes + its edge.

    The EDGE matters.  ``read.py`` walks out from the DESIGN label, so a node
    with no edge into the design is unreachable and the reader can never return
    it, whatever the join does — a legacy fixture without one makes every
    assertion about it vacuous.  A real pre-wave author wrote both.
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
            _DESIGN_LABEL,
            predicate,
            label,
            str(ns_id),
        )


async def _state_rows(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """``{node_label: row}`` read by the TEST's own SQL, not by ``read.py``.

    Used to prove the fixture really is in the state the tests claim before any
    assertion about the reader is made.
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


async def _seed(pg_pool: Any, ns_id: uuid.UUID, tag: str) -> None:
    """Seed one tenant.

    Order matters and each step is here for a reason:

    1. the ownership registry, or ``assert_owner`` refuses every node write;
    2. the DESIGN node and its FL tree;
    3. the two PRE-WAVE nodes, straight into ``kg_nodes`` with their ``contains``
       edge — this is what a pre-W16 author left behind, and it is the only way
       to produce a node that is *not new* to a later authoring call;
    4. one authoring call carrying explicit lifecycle keys for the declared
       nodes and a ``revision``-only entry for :data:`_REF_NULL_STATUS`, which
       is not new and names no status, so 67g's writer stores ``status = NULL``;
       :data:`_REF_LEGACY` is deliberately NOT in this call, so it keeps having
       no row at all.
    """
    from nce.auth import set_namespace_context
    from nce.db_utils import scoped_pg_session
    from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
    from nce.vertical_modules.system_design.devices import do_author_device_topology
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
                namespace_slug=_NS_SLUG,
                design_id=_DESIGN_ID,
                site_name="SiteW16b",
                buildings=[
                    {
                        "name": "BuildingW16b",
                        "floors": [
                            {"name": "FloorW16b", "rooms": [{"name": "RoomW16b", "positions": []}]}
                        ],
                    }
                ],
            )

    for ref in (_REF_NULL_STATUS, _REF_LEGACY):
        await _insert_pre_wave_node(
            pg_pool, ns_id, device_label(_DESIGN_ID, ref), "DEVICE", predicate="contains"
        )

    devices: list[dict[str, Any]] = [
        {
            "device_ref": _REF_ACTIVE,
            "status": _STATUS_ACTIVE,
            "revision": _TENANT_REVISION[tag],
            "salience": _TENANT_SALIENCE[tag],
            "ports": [{"port_ref": _PORT_REF, "capability": {"port_direction": "output"}}],
            "rack_ref": _RACK_REF,
        },
        {
            "device_ref": _REF_PLANNED,
            "status": _STATUS_PLANNED,
            "revision": _TENANT_REVISION[tag],
        },
        {
            "device_ref": _REF_PER_TENANT,
            "status": _TENANT_STATUS[tag],
            "revision": _TENANT_REVISION[tag],
        },
        # NOT new to this call (step 3 created it) and it names NO status, so the
        # writer stores a row with status NULL.  This is the middle state.
        {
            "device_ref": _REF_NULL_STATUS,
            "revision": _TENANT_REVISION[tag],
        },
    ]
    racks = [{"rack_ref": _RACK_REF, "status": _STATUS_RACK, "revision": _TENANT_REVISION[tag]}]
    connections = [
        {
            "from_device_ref": _REF_ACTIVE,
            "from_port_ref": _PORT_REF,
            "to_device_ref": _REF_ACTIVE,
            "to_port_ref": _PORT_REF,
            "cable_ref": _CABLE_REF,
            "cable_status": _STATUS_CABLE,
            "cable_revision": _TENANT_REVISION[tag],
        }
    ]

    with patch(_MOCK_EMIT_DEVICES, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_device_topology(
                conn,
                ns_id,
                design_id=_DESIGN_ID,
                devices=devices,
                racks=racks,
                connections=connections,
            )


async def _seed_other_design(pg_pool: Any, ns_id: uuid.UUID) -> str:
    """Author a SECOND design in the SAME namespace and return its device label.

    Exists for one predicate: ``_fetch_node_state_by_labels``' ``node_label``
    filter.  It is not a tenant boundary — the namespace pin next to it is — so
    the two-tenant fixture cannot see it: with one design per namespace, dropping
    it changes nothing.  A second design is what makes it observable, and what it
    protects is the contract claim that ``state`` is keyed by *this design's*
    in-scope nodes rather than by everything the tenant owns.
    """
    from nce.db_utils import scoped_pg_session
    from nce.vertical_modules.system_design.devices import do_author_device_topology
    from nce.vertical_modules.system_design.graph import do_author_functional_location

    other_design = "DESIGN-W16B-OTHER-001"
    with patch(_MOCK_EMIT_GRAPH, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_functional_location(
                conn,
                ns_id,
                namespace_slug=_NS_SLUG,
                design_id=other_design,
                site_name="SiteOther",
                buildings=[
                    {
                        "name": "BuildingOther",
                        "floors": [
                            {
                                "name": "FloorOther",
                                "rooms": [{"name": "RoomOther", "positions": []}],
                            }
                        ],
                    }
                ],
            )
    with patch(_MOCK_EMIT_DEVICES, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_device_topology(
                conn,
                ns_id,
                design_id=other_design,
                devices=[
                    {
                        "device_ref": "DEV-OTHER",
                        "status": _STATUS_ACTIVE,
                        "revision": "OTHER-DESIGN-REV",
                    }
                ],
            )
    return device_label(other_design, "DEV-OTHER")


async def _assert_fixture_is_what_it_claims(pg_pool: Any, ns_id: uuid.UUID, tag: str) -> None:
    """Prove the three lifecycle states really exist before anything is asserted.

    Read with the TEST's own SQL.  Without this, "the legacy device has no row"
    could be true because the seeding silently failed, and every one-way-door
    assertion below would pass against any implementation at all.
    """
    rows = await _state_rows(pg_pool, ns_id)
    assert set(rows) == {
        _LABEL_ACTIVE,
        _LABEL_PLANNED,
        _LABEL_PER_TENANT,
        _LABEL_NULL_STATUS,
        _LABEL_RACK,
        _LABEL_CABLE,
    }, f"the fixture did not produce the expected state rows: {sorted(rows)}"
    assert _LABEL_LEGACY not in rows, "the legacy device acquired a state row"
    assert rows[_LABEL_NULL_STATUS]["status"] is None, (
        "the revision-only re-author stored a status; the middle lifecycle state "
        "is not in this fixture and the NULL-status tests below prove nothing"
    )
    assert rows[_LABEL_NULL_STATUS]["revision"] == _TENANT_REVISION[tag]
    assert rows[_LABEL_ACTIVE]["status"] == _STATUS_ACTIVE
    assert rows[_LABEL_PER_TENANT]["status"] == _TENANT_STATUS[tag]


# ---------------------------------------------------------------------------
# 1. Argument shape (pure — no DB, no Redis).
# ---------------------------------------------------------------------------


def test_normalise_statuses_treats_none_and_empty_as_no_filter() -> None:
    from nce.vertical_modules.system_design.read import _normalise_statuses

    assert _normalise_statuses(None) is None
    assert _normalise_statuses([]) is None
    assert _normalise_statuses(()) is None
    # POSITIVE CONTROL: a non-empty list is NOT turned into "no filter", so the
    # two assertions above are about emptiness and not about the function
    # returning None for everything.
    assert _normalise_statuses(["active"]) == ["active"]
    assert _normalise_statuses(("active", "planned")) == ["active", "planned"]


def test_normalise_statuses_passes_values_through_verbatim() -> None:
    """No case folding, no trimming, no vocabulary check.

    The vocabulary is migration 061's composite CHECK and exists once.  A copy
    here — or in ``read.py`` — is how a read path and its constraint drift apart
    while both suites stay green, which is the rule ``devices.py`` already
    follows on the write side.
    """
    from nce.vertical_modules.system_design.read import _normalise_statuses

    weird = ["ACTIVE", " planned ", "NONSENSE", ""]
    assert _normalise_statuses(weird) == weird


def test_the_filter_is_in_the_sql_and_not_in_python() -> None:
    """A SOURCE-TEXT gate, and it is weaker than the rest of this file — say so.

    A Python-side discard over already-fetched rows would produce **byte-identical
    payloads**, so no behavioural test in this file can tell the two apart.  What
    makes the SQL placement load-bearing is not the output: it is that on an
    owner pool, which bypasses ``FORCE ROW LEVEL SECURITY``, the predicate *is*
    the tenant boundary, and a Python filter has already read the rows it claims
    to exclude.  That is a property of the code, so this is a check on the code.

    The positive control is the point of the second half: the same substring
    check returns ``False`` for a sibling query that genuinely does not touch the
    state table, so it is discriminating rather than always-true.
    """
    import inspect

    from nce.vertical_modules.system_design import read

    nodes_sql = inspect.getsource(read._fetch_nodes_by_labels)
    assert "system_design_node_state" in nodes_sql, (
        "the node query no longer touches the state table; the statuses filter has moved out of SQL"
    )
    assert "$3::text[]" in nodes_sql, "the statuses parameter is not bound in the query"

    # POSITIVE CONTROL: the same check is False where the table really is absent.
    edges_sql = inspect.getsource(read._fetch_edges_within)
    assert "system_design_node_state" not in edges_sql, (
        "the control query now mentions the state table, so the assertion above "
        "is satisfied by something other than the filter"
    )

    # …and the composer hands the argument to that query, not to a later pass.
    composer = " ".join(inspect.getsource(read.do_get_topology).split())
    assert "_fetch_nodes_by_labels(conn, ns_uuid, scope_labels, statuses)" in composer, (
        "the composer no longer passes statuses to the node query"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "active",  # a bare str is REFUSED, never wrapped or iterated as chars
        b"active",
        {"active": True},
        7,
        ["active", 7],
        [None],
        [["active"]],
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_statuses_is_refused(bad: Any) -> None:
    """Shape errors raise ValueError, which the adapters turn into -32602 / 422.

    ``statuses="active"`` is the one that matters: silently reading it as
    ``["active"]`` would hide a client bug, and passing it to
    ``= ANY($n::text[])`` would surface as an opaque driver error instead.
    """
    with pytest.raises(ValueError, match="statuses"):
        await do_get_topology(
            _EngineStub(None),
            {"namespace_id": str(uuid.uuid4()), "design_id": _DESIGN_ID, "statuses": bad},
        )


@pytest.mark.asyncio
async def test_a_well_formed_statuses_is_not_refused_by_the_shape_check() -> None:
    """POSITIVE CONTROL for the parametrised refusal above.

    Without this, a ``_normalise_statuses`` that raised on *everything* would
    satisfy every row of that table.  A well-formed value gets past the shape
    check and fails later, on the missing engine — which is proof it got past.
    """
    from nce.vertical_modules.system_design.read import _normalise_statuses

    assert _normalise_statuses(["active", "NONSENSE"]) == ["active", "NONSENSE"]


def test_the_tool_schema_no_longer_advertises_a_no_op() -> None:
    """Copper reads the tool schema, not this repository.

    An advertised no-op that silently went live is a false claim on the surface
    a client actually consumes.
    """
    from nce.mcp_stdio_tools import TOOLS

    (tool,) = [t for t in TOOLS if t.name == _TOOL]
    description = tool.inputSchema["properties"]["statuses"]["description"]

    lowered = description.lower()
    assert "ignored" not in lowered, (
        f"the statuses schema still advertises the W13a no-op: {description!r}"
    )
    assert "until w16" not in lowered
    # POSITIVE CONTROL: the negative assertions above are checking a string that
    # is really there and really describes this parameter — a missing or empty
    # description would satisfy them forever.
    assert "statuses" in tool.inputSchema["properties"]
    assert len(description) > 100
    assert "filter" in lowered


def test_the_cache_key_discriminates_on_statuses() -> None:
    """``system_design_get_topology`` is ``cacheable=True``.

    If ``statuses`` did not reach the cache key, a filtered read would be served
    the unfiltered payload from a warm cache — silently, and invisibly to every
    DB-level test in this file, which all run with the cache disabled.
    """
    from nce.mcp_args import build_cache_key
    from nce.tool_registry import TOOL_REGISTRY

    assert TOOL_REGISTRY[_TOOL].cacheable is True, (
        "this test exists because the tool is cached; if it stopped being "
        "cached, say so deliberately rather than deleting the test"
    )

    ns = str(uuid.uuid4())
    base: dict[str, Any] = {"namespace_id": ns, "design_id": _DESIGN_ID}
    unfiltered = build_cache_key(_TOOL, dict(base))
    planned = build_cache_key(_TOOL, dict(base, statuses=["planned"]))
    active = build_cache_key(_TOOL, dict(base, statuses=["active"]))

    assert len({unfiltered, planned, active}) == 3, (
        "two different statuses requests share a cache key; a warm cache would "
        "answer one with the other's payload"
    )
    # POSITIVE CONTROL: the same request really does hash to the same key, so
    # the inequality above is discrimination and not just per-call randomness.
    assert build_cache_key(_TOOL, dict(base, statuses=["planned"])) == planned


# ---------------------------------------------------------------------------
# 2. The one-way door — absence, and NULL, through the real read surface.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestAbsenceIsNeverCoalesced:
    async def _tenant(self, pg_pool: Any, make_namespace: Any) -> tuple[uuid.UUID, Any]:
        ns_id: uuid.UUID = await make_namespace()
        await _seed(pg_pool, ns_id, "ALPHA")
        await _assert_fixture_is_what_it_claims(pg_pool, ns_id, "ALPHA")
        return ns_id, _EngineStub(pg_pool)

    async def test_a_node_with_no_state_row_is_absent_from_state(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE one-way door, read side.

        ``no row`` must not become ``status: 'planned'`` and must not become
        ``status: null`` either — the latter is the *other* lifecycle state and
        W17 distinguishes them.  The label is simply not a key.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        payload = await _read(engine, ns_id)

        assert _LABEL_LEGACY in _device_labels(payload), (
            "the reader did not return the legacy device at all, so every "
            "assertion below would be vacuous — the FIXTURE is broken, not the code"
        )
        assert _LABEL_LEGACY not in payload["state"], (
            "a node with NO state row appeared in the state map; absence has "
            "been synthesised into a row and W17 can no longer deny on it"
        )
        # POSITIVE CONTROL: the map is not simply empty — a node that DOES have
        # a row is in it, so "not in" above is a fact about this node.
        assert _LABEL_ACTIVE in payload["state"]
        assert payload["state"][_LABEL_ACTIVE]["status"] == _STATUS_ACTIVE

    async def test_the_legacy_device_carries_no_status_anywhere_in_the_payload(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wherever a future wave decides to surface status, it must not be here.

        Scanning the serialised device rather than one guessed key is deliberate:
        the round-1 version of the equivalent test in
        ``tests/test_system_design_node_state.py`` was permanently vacuous
        because it guessed the wrong key, and went RED under none of 33
        mutations.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        payload = await _read(engine, ns_id)
        legacy = [d for d in payload["devices"] if d["node"]["label"] == _LABEL_LEGACY]
        assert legacy, "the legacy device is not in the payload; the fixture is broken"

        serialised = json.dumps(legacy[0])
        assert DEFAULT_NODE_STATUS not in serialised, (
            f"the read surface reported {DEFAULT_NODE_STATUS!r} for a device with "
            f"NO state row: {serialised}"
        )
        # POSITIVE CONTROL: the same scan DOES find a status when one exists, so
        # it is not satisfied by a serialisation that never carries statuses.
        declared = [d for d in payload["devices"] if d["node"]["label"] == _LABEL_PLANNED]
        assert declared, "the declared-planned device is missing; the fixture is broken"
        assert DEFAULT_NODE_STATUS in json.dumps(
            {"device": declared[0], "state": payload["state"][_LABEL_PLANNED]}
        ), (
            "the scan cannot see a status even when one is stored, so the "
            "negative assertion above is dead"
        )

    async def test_a_node_with_no_state_row_matches_no_filter(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak this wave exists to prevent.

        ``statuses=['planned']`` must not return the legacy estate.  A COALESCE
        in the filter's sub-query — ``COALESCE(status, 'planned') = ANY($3)`` —
        makes exactly that happen, and nothing else in the suite notices.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        planned = await _read(engine, ns_id, statuses=[_STATUS_PLANNED])
        assert _LABEL_LEGACY not in _device_labels(planned), (
            "a device with NO state row matched statuses=['planned'] — absence "
            "was coalesced to the default and the legacy estate is now retirable"
        )
        # POSITIVE CONTROL: the filter is not returning nothing at all.
        assert _device_labels(planned) == [_LABEL_PLANNED]

        # And it matches no OTHER value either, so the assertion above is about
        # absence rather than about 'planned' specifically.
        for status in (_STATUS_ACTIVE, _TENANT_STATUS["ALPHA"], _STATUS_CABLE, "NONSENSE"):
            filtered = await _read(engine, ns_id, statuses=[status])
            assert _LABEL_LEGACY not in _device_labels(filtered), (
                f"the legacy device matched statuses=[{status!r}]"
            )

    async def test_a_null_status_is_present_but_matches_no_filter(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The middle lifecycle state, from both ends.

        ``status IS NULL`` means "we hold data for this node, nobody declared a
        lifecycle".  It is *present* in ``state`` — that is what distinguishes it
        from a missing row — and it answers no lifecycle question.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        payload = await _read(engine, ns_id)
        entry = payload["state"][_LABEL_NULL_STATUS]
        assert entry["status"] is None, f"a row whose status IS NULL was coalesced on read: {entry}"
        # It is a ROW, not an absence: the revision it holds is exactly what
        # makes the two states different, and it survived.
        assert entry["revision"] == _TENANT_REVISION["ALPHA"]

        for status in (_STATUS_PLANNED, _STATUS_ACTIVE, "NONSENSE"):
            filtered = await _read(engine, ns_id, statuses=[status])
            assert _LABEL_NULL_STATUS not in _device_labels(filtered), (
                f"a device whose status IS NULL matched statuses=[{status!r}]"
            )
        # POSITIVE CONTROL: the unfiltered read does return it, so "not in the
        # filtered result" is the filter's doing and not the node's absence.
        assert _LABEL_NULL_STATUS in _device_labels(payload)

    async def test_salience_is_json_native_and_nulls_are_not_defaulted(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NUMERIC`` must not reach the wire as a ``Decimal``-turned-string.

        Converting in the core is what makes the MCP tool and the REST route
        emit the same JSON type for the same field.  And a member nobody set
        stays ``null`` — the reader defaults nothing, not the status and not the
        other two either.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        payload = await _read(engine, ns_id)
        active = payload["state"][_LABEL_ACTIVE]
        assert active["salience"] == _TENANT_SALIENCE["ALPHA"]
        assert isinstance(active["salience"], float)
        # The rack was authored with a status and a revision and NO salience.
        assert payload["state"][_LABEL_RACK]["salience"] is None
        assert payload["state"][_LABEL_RACK]["status"] == _STATUS_RACK


# ---------------------------------------------------------------------------
# 3. What the filter narrows — and what it must leave alone.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestWhatTheFilterNarrows:
    async def _tenant(self, pg_pool: Any, make_namespace: Any) -> tuple[uuid.UUID, Any]:
        ns_id: uuid.UUID = await make_namespace()
        await _seed(pg_pool, ns_id, "ALPHA")
        await _assert_fixture_is_what_it_claims(pg_pool, ns_id, "ALPHA")
        return ns_id, _EngineStub(pg_pool)

    async def test_it_narrows_devices_racks_and_cables(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One filter value, three buckets, and the vocabularies are disjoint.

        ``'active'`` is in the DEVICE and RACK vocabularies and not in CABLE's;
        ``'connected'`` is in CABLE's and in neither of the others.  Filtering on
        each in turn proves the predicate matches on the stored VALUE and is not
        keyed on the bucket.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        unfiltered = await _read(engine, ns_id)
        assert unfiltered["devices"] and unfiltered["racks"] and unfiltered["cables"], (
            "the fixture seeded an empty bucket, so the emptiness assertions "
            "below would pass against any implementation"
        )

        by_active = await _read(engine, ns_id, statuses=[_STATUS_ACTIVE])
        assert _device_labels(by_active) == [_LABEL_ACTIVE]
        assert _rack_labels(by_active) == [_LABEL_RACK]
        assert _cable_labels(by_active) == [], (
            "the cable is 'connected', not 'active' — a filter keyed on the "
            "bucket rather than the stored value would have returned it"
        )

        by_connected = await _read(engine, ns_id, statuses=[_STATUS_CABLE])
        assert _cable_labels(by_connected) == [_LABEL_CABLE]
        assert _device_labels(by_connected) == []
        assert _rack_labels(by_connected) == []

        both = await _read(engine, ns_id, statuses=[_STATUS_ACTIVE, _STATUS_CABLE])
        assert _device_labels(both) == [_LABEL_ACTIVE]
        assert _rack_labels(both) == [_LABEL_RACK]
        assert _cable_labels(both) == [_LABEL_CABLE]

    async def test_a_filtered_out_device_takes_its_ports_with_it(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ports are nested under their device, so they follow it.

        PORT is not filtered in its own right — migration 061 refuses a PORT
        state row, so it has no status to filter on — but a port only reaches the
        caller through its device.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        # POSITIVE CONTROL first: the port IS reachable when its device matches.
        by_active = await _read(engine, ns_id, statuses=[_STATUS_ACTIVE])
        (device,) = [d for d in by_active["devices"] if d["node"]["label"] == _LABEL_ACTIVE]
        assert [p["node"]["label"] for p in device["ports"]] == [_LABEL_PORT]

        by_planned = await _read(engine, ns_id, statuses=[_STATUS_PLANNED])
        assert _LABEL_ACTIVE not in _device_labels(by_planned)
        ports = [p["node"]["label"] for d in by_planned["devices"] for p in d["ports"]]
        assert _LABEL_PORT not in ports

    async def test_it_narrows_nothing_else(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filtered read is a VIEW, not a subgraph — and that is on purpose.

        ``edges`` stays whole so the caller can still see what a filtered-out
        node was attached to; ``state`` stays whole so it can see the status that
        excluded it; ``design`` stays because ``design: null`` already means
        "this design does not exist in your namespace" and must not acquire a
        second meaning.  Narrowing any of these is a contract change and has to
        change this test on purpose.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        unfiltered = await _read(engine, ns_id)
        filtered = await _read(engine, ns_id, statuses=[_STATUS_ACTIVE])

        # POSITIVE CONTROL: the filter really did something, so the equalities
        # below are not "nothing changed anywhere".
        assert _device_labels(filtered) != _device_labels(unfiltered)

        for key in ("design", "functional_locations", "edges", "geometry", "state", "version"):
            assert filtered[key] == unfiltered[key], f"{key!r} was narrowed by statuses"

        assert filtered["design"] is not None, (
            "a filtered read reported the design as absent, which is the signal "
            "for 'this design is not in your namespace' — two facts, one spelling"
        )
        # The filtered-out device is still visible as an edge target and still
        # carries its status, which is the whole justification above.
        assert any(e["object"] == _LABEL_PLANNED for e in filtered["edges"])
        assert filtered["state"][_LABEL_PLANNED]["status"] == _STATUS_PLANNED

    async def test_an_unknown_status_matches_nothing_and_is_not_an_error(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vocabulary is not validated here; it is validated once, in the DDL."""
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        # Matched verbatim: right value, wrong case, and a value from no
        # vocabulary at all — all three are well-formed and all three miss.
        for status in ("NONSENSE", _STATUS_ACTIVE.upper(), " active"):
            filtered = await _read(engine, ns_id, statuses=[status])
            assert filtered["devices"] == []
            assert filtered["racks"] == []
            assert filtered["cables"] == []
            # …while the structure is untouched, so this is a filter that matched
            # nothing rather than a read that failed.
            assert filtered["design"] is not None
            assert filtered["edges"]

    async def test_empty_and_absent_statuses_are_the_same_request(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``[]`` is "I named no statuses", not "match nothing".

        The REST adapter maps an absent query parameter to ``None`` through
        ``getlist(...) or None``, so the core has to agree with it or the two
        surfaces answer the same request differently.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        unfiltered = await _read(engine, ns_id)
        assert unfiltered["devices"], "the fixture seeded no device"

        # Annotated: mypy cannot infer ``list[str] | None`` from the tuple.
        no_filters: list[list[str] | None] = [[], None]
        for empty in no_filters:
            assert await _read(engine, ns_id, statuses=empty) == unfiltered, (
                f"statuses={empty!r} was treated as a filter"
            )

    async def test_state_is_scoped_to_this_design_not_the_whole_tenant(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``state`` is keyed by THIS design's in-scope nodes.

        A second design in the same namespace holds its own state rows.  Without
        the ``node_label = ANY($1::text[])`` predicate in
        ``_fetch_node_state_by_labels`` they all land in this design's ``state``
        map — not a tenancy leak, but a contract break: the map is documented as
        the design's, and a Copper canvas would show lifecycle for equipment that
        is not on it.

        The two-tenant fixture cannot see this, and the mutation sweep proved it:
        with one design per namespace the predicate is deletable and every other
        test in this file stays green.  That is why this test exists rather than
        an argument that the isolation tests cover it.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_id, engine = await self._tenant(pg_pool, make_namespace)
        other_label = await _seed_other_design(pg_pool, ns_id)

        # POSITIVE CONTROL: the other design's row really exists in this tenant,
        # so "it is not in the map" is a fact about scoping and not about the
        # seeding having quietly failed.
        rows = await _state_rows(pg_pool, ns_id)
        assert other_label in rows, "the second design authored no state row"
        assert rows[other_label]["status"] == _STATUS_ACTIVE

        payload = await _read(engine, ns_id)
        assert other_label not in payload["state"], (
            "another design's state row leaked into this design's state map"
        )
        assert other_label not in _device_labels(payload)
        # …and this design's own rows are still all there.
        assert _LABEL_ACTIVE in payload["state"]

    async def test_the_core_filters_without_the_dispatch_layer(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The filter lives in read.py's SQL, not in anything above it.

        Same claim, calling the core directly, so a future change that filtered
        at the dispatch or adapter layer could not make the predicate look
        unnecessary.
        """
        ns_id, engine = await self._tenant(pg_pool, make_namespace)

        result = await do_get_topology(
            engine,
            {"namespace_id": ns_id, "design_id": _DESIGN_ID, "statuses": [_STATUS_ACTIVE]},
        )
        assert _device_labels(result) == [_LABEL_ACTIVE]
        assert _LABEL_LEGACY not in result["state"]


# ---------------------------------------------------------------------------
# 4. Owner-pool tenant isolation on the READ — the two W16b predicates.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnerPoolIsolation:
    """Two tenants colliding on EVERY identifier, differing ONLY in content.

    ``nce_app`` serves no request in this deployment and migration 061's policy
    is written ``FOR ALL TO nce_app``, so every request runs on a role that
    policy does not cover.  What separates these two tenants is the explicit
    ``namespace_id`` predicate in each of the two W16b queries — nothing else.

    The one node whose *status* differs between the tenants
    (:data:`_REF_PER_TENANT`) is what makes the filter's own sub-query pin
    observable: with identical statuses in both tenants, deleting that pin
    changes no result at all and the test would be confounded.
    """

    async def _seed_both(
        self, pg_pool: Any, make_namespace: Any
    ) -> tuple[uuid.UUID, uuid.UUID, Any]:
        ns_a: uuid.UUID = await make_namespace()
        ns_b: uuid.UUID = await make_namespace()
        await _seed(pg_pool, ns_a, "ALPHA")
        await _seed(pg_pool, ns_b, "BETA")
        await _assert_fixture_is_what_it_claims(pg_pool, ns_a, "ALPHA")
        await _assert_fixture_is_what_it_claims(pg_pool, ns_b, "BETA")
        return ns_a, ns_b, _EngineStub(pg_pool)

    async def test_state_content_does_not_bleed(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_fetch_node_state_by_labels``' namespace predicate.

        Both tenants hold a row under the SAME ``node_label``, so without the
        predicate one silently overwrites the other in the by-label dict and both
        tenants then read the same (wrong) row consistently.  ``revision`` is the
        assertion because it differs; ``status`` is identical on most nodes by
        design and would pass either way.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_a, ns_b, engine = await self._seed_both(pg_pool, make_namespace)

        for ns_id, tag in ((ns_a, "ALPHA"), (ns_b, "BETA")):
            payload = await _read(engine, ns_id)
            state = payload["state"]
            for label in (_LABEL_ACTIVE, _LABEL_PLANNED, _LABEL_NULL_STATUS, _LABEL_RACK):
                assert state[label]["revision"] == _TENANT_REVISION[tag], (
                    f"{tag} read the other tenant's state row for {label}: "
                    f"{state[label]['revision']!r}"
                )
            assert state[_LABEL_ACTIVE]["salience"] == _TENANT_SALIENCE[tag]
            assert state[_LABEL_PER_TENANT]["status"] == _TENANT_STATUS[tag]
            # The absent row stays absent per tenant too: a leak that merged the
            # two tenants' maps would not create this key either way, so this is
            # asserted alongside the content rather than instead of it.
            assert _LABEL_LEGACY not in state

    async def test_the_filter_does_not_match_on_another_tenants_status(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The namespace pin INSIDE the ``statuses`` sub-query.

        ``DEV-TENANT`` is ``'staged'`` in ALPHA and ``'offline'`` in BETA under
        one byte-identical label.  Reading ALPHA with ``statuses=['offline']``
        must return nothing: the only row with that status belongs to BETA.
        Drop the pin and ALPHA's device is admitted on the strength of BETA's
        row — a cross-tenant read even though the foreign row's contents never
        leave the database.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_a, ns_b, engine = await self._seed_both(pg_pool, make_namespace)

        for ns_id, tag, foreign in ((ns_a, "ALPHA", "BETA"), (ns_b, "BETA", "ALPHA")):
            # POSITIVE CONTROL: the tenant's OWN status selects the device, so
            # the negative below is about the namespace and not about the filter
            # being broken for this node.
            own = await _read(engine, ns_id, statuses=[_TENANT_STATUS[tag]])
            assert _device_labels(own) == [_LABEL_PER_TENANT], (
                f"{tag} could not select its own device by its own status"
            )

            leaked = await _read(engine, ns_id, statuses=[_TENANT_STATUS[foreign]])
            assert _device_labels(leaked) == [], (
                f"{tag} matched a device on {foreign}'s status "
                f"({_TENANT_STATUS[foreign]!r}) — the namespace pin inside the "
                f"statuses sub-query is gone"
            )

    async def test_neither_tenant_sees_a_duplicated_node(
        self, pg_pool: Any, make_namespace: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_fetch_nodes_by_labels``' own predicate, on the FILTERED path.

        Every label collides, so losing the ``kg_nodes`` namespace pin returns
        each node twice.  Asserted on a filtered read specifically: the
        unfiltered path is already gated in ``tests/test_system_design_read.py``,
        and the filtered path runs different SQL.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)
        ns_a, ns_b, engine = await self._seed_both(pg_pool, make_namespace)

        for ns_id in (ns_a, ns_b):
            filtered = await _read(engine, ns_id, statuses=[_STATUS_ACTIVE])
            labels = _device_labels(filtered)
            assert labels == [_LABEL_ACTIVE], (
                f"expected exactly this tenant's one active device, got {labels}"
            )
            assert _rack_labels(filtered) == [_LABEL_RACK]
