"""
tests/test_system_design_retire.py
===================================
Module 6 Wave 17 (B067h) — ``system_design_delete_planned`` /
``DELETE /api/system-design/planned``: **the first delete path in this
codebase.**

The filename matches the ``tests/test_system_design_*.py`` CI glob B067a wired
into ``.github/workflows/ci.yml``, so this file runs in CI with no workflow
edit.  Any other filename would need a ``ci.yml`` change, which is a scope
change.

WHAT THESE TESTS ACTUALLY GATE
-------------------------------
1. **THE ONE-WAY DOOR: deny on absence, and deny on NULL.**  A node with no
   ``system_design_node_state`` row, and a node whose row has ``status IS
   NULL``, are BOTH refused.  Absence is the normal state of every node
   authored before W16 — migration 061 writes a row only for a node genuinely
   new to an authoring call or one the caller sent a lifecycle key for — so
   this guard is the only thing standing between this tool and the whole legacy
   as-built estate.  The two get SEPARATE tests, because they are separate
   branches and a single test over "not planned" would pass with either one
   deleted.

2. **Both directions.**  An over-strict guard is a defect too, so every denial
   test has a positive sibling: a genuinely ``'planned'`` node, with a valid
   actor, IS retired, and IS permanently deletable.

3. **D12 — the side tables, and the RESURRECTION they cause.**  No foreign key
   ties a capability / geometry / lifecycle row to its ``kg_nodes`` node (see
   migration 061's header).  Delete the node, leave the state row, re-author the
   same deterministic label, and the new node inherits the orphan's status
   through ``ON CONFLICT DO UPDATE``.  :class:`TestResurrection` drives that
   scenario end to end through the real authoring tool and asserts the
   re-authored node comes back ``'planned'`` with a freshly created row — not
   carrying whatever the deleted one was.

4. **Ports go with their device.**  A ``PORT`` has no lifecycle row but does
   have capability and geometry rows and an equally deterministic label, so the
   same defect exists one level down.  Gated by
   :class:`TestPermanentDeleteRemovesEverything`.

5. **The design's version row survives.**  ``system_design_geometry`` holds
   node-geometry rows AND the one per-design optimistic-concurrency version
   row, told apart only by ``version``.  Deleting the version row would reset
   the design's token to 0 and hand every stale client a free win.

6. **Owner-pool tenant isolation (§6.4).**  ``nce_app`` serves no request in
   this deployment; every request runs on an owner pool that ``FORCE ROW LEVEL
   SECURITY`` does not constrain.  What isolates tenants is the explicit
   ``namespace_id`` predicate on every statement in ``retire.py``.  The two
   tenants below collide on **every identifier** — same design id, same device
   / rack / cable refs, therefore byte-identical node labels — and differ
   **only in content**.  A fixture that gave them different labels could not
   detect a predicate that filters by label, and B067b failed TAG on exactly
   that.

7. **Transactionality.**  A failure part-way through leaves NOTHING deleted.
   Proved by injecting a failure after the deletes and before the commit, then
   reading every table back.

8. **``actor`` is mandatory on the permanent path**, and the refusal happens
   before anything is read or written.

9. **The name/behaviour mismatch is stated, not merely intended.**  The tool
   keeps the pinned name ``system_design_delete_planned`` and the pinned route
   ``DELETE /api/system-design/planned`` while defaulting to a soft retire, so
   :class:`TestTheNameIsADeliberateMismatch` asserts by BYTES that the first
   line of each docstring on the path says so.  A contract mismatch nobody
   wrote down is the defect; the test is what keeps it written down.

The per-predicate mutation table (one row per predicate, each RED for its own
reason) is in the wave report.  Every row was produced by mutating a single
predicate in a scratchpad COPY of the tree — never in the tree itself — and
asserting the edit landed before running.

All DB-dependent tests are ``@pytest.mark.integration`` (wave rule 9).

A NOTE ON ``NCE_ADMIN_OVERRIDE`` IN THE DISPATCH TESTS
-------------------------------------------------------
``system_design_delete_planned`` is ``admin_only=True``.  Batch 67L fixed the
drift between ``nce/auth.py``'s hardcoded ``MCP_ADMIN_TOOL_NAMES`` and the
registry's ``ADMIN_ONLY_TOOLS``: ``enforce_mcp_tool_auth`` now takes the
*admin* branch for a tool if it is in either set, so this tool is reachable
over MCP with a valid ``admin_api_key``.  These tests still set
``NCE_ADMIN_OVERRIDE`` as belt-and-braces so they do not depend on a
particular key being configured, but it is no longer the only way through.
:func:`test_admin_only_is_enforced_by_the_dispatch_loop` still pins the
fail-closed half: with the override off and no keys, the call is refused.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from typing import Any

import pytest

from nce.vertical_modules.system_design.devices import (
    cable_label,
    device_label,
    port_label,
    rack_label,
)
from nce.vertical_modules.system_design.retire import (
    DENY_NODE_ABSENT,
    DENY_STATE_ROW_ABSENT,
    DENY_STATUS_NOT_PLANNED,
    DENY_STATUS_UNDECLARED,
    RETIRABLE_NODE_TYPES,
    RETIRABLE_STATUS,
    RETIRE_STATUS_BY_NODE_TYPE,
    RETIRED_SALIENCE,
    RetireDeniedError,
    design_of_label,
    node_type_of_label,
)

_RETIRE_TOOL = "system_design_delete_planned"
_TOPOLOGY_TOOL = "system_design_author_topology"

# ---------------------------------------------------------------------------
# Fixture data.
#
# EVERY identifier below is shared by both tenants.  Only content differs.  See
# point 6 of the module docstring for why that is not a stylistic choice.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W17-RETIRE-001"
_DEVICE_REF = "SW-W17"
_PORT_REF = "ETH-1"
_RACK_REF = "RACK-W17"
_CABLE_REF = "CBL-W17"

_DEVICE_LABEL = device_label(_DESIGN_ID, _DEVICE_REF)
_PORT_LABEL = port_label(_DESIGN_ID, _DEVICE_REF, _PORT_REF)
_RACK_LABEL = rack_label(_DESIGN_ID, _RACK_REF)
_CABLE_LABEL = cable_label(_DESIGN_ID, _CABLE_REF)
_DESIGN_LABEL = f"DESIGN:{_DESIGN_ID}"

#: A DEVICE authored the way everything before W16 did — ``kg_nodes`` plus its
#: ``contains`` edge, and NO state row, because there was no table.  This is
#: what deny-on-absence protects, and the whole legacy estate looks like it.
_LEGACY_REF = "LEGACY-ASBUILT"
_LEGACY_LABEL = device_label(_DESIGN_ID, _LEGACY_REF)

#: Per-tenant CONTENT.  None of these is an identifier, so a namespace
#: predicate that went missing shows up as the wrong VALUE under the right key.
_TENANT_CONTENT: dict[str, dict[str, Any]] = {
    "ALPHA": {"revision": "ALPHA-REV-7", "salience": 0.25},
    "BETA": {"revision": "BETA-REV-91", "salience": 0.75},
}


class _NullCacheRedis:
    """Never a cache hit, but ``incr`` exists.

    ``mcp_stdio_dispatch`` calls ``bump_cache_generation`` unguarded after every
    successful ``mutation=True`` tool, so a ``None`` client turns every call
    here into ``McpError(-32603)`` *after* the write has already committed
    (defect D3, disclosed in the wave report; ``mcp_stdio_dispatch.py`` is
    outside this wave's ``Files:`` list).  Never returning a hit means every
    read-back below proves the query ran rather than that a payload was
    replayed.
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


class _StubRequest:
    """Minimal duck-typed Starlette request: the route reads only ``.json()``."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self.path_params: dict[str, str] = {}

    async def json(self) -> Any:
        return self._body


@pytest.fixture(autouse=True)
def _admin_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the dispatch loop reach an ``admin_only=True`` handler.

    See the module docstring: Batch 67L reconciled the registry's
    ``admin_only`` flag with ``nce/auth.py``'s ``MCP_ADMIN_TOOL_NAMES``, so a
    valid ``admin_api_key`` now reaches this tool without the override too.
    Setting it here is belt-and-braces, not load-bearing.
    :func:`test_admin_only_is_enforced_by_the_dispatch_loop` turns the
    override OFF and pins the refusal.
    """
    monkeypatch.setenv("NCE_ADMIN_OVERRIDE", "true")


async def _seed_ownership(pg_pool: Any, ns_id: uuid.UUID) -> None:
    """Seed the node-ownership registry so ``assert_owner`` permits the delete.

    Deliberately a separate step: the ownership-denial test is the same call
    with this omitted, which is the only way that test means anything.
    """
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


#: The three NetBox vocabularies, spelled in the TEST rather than imported from
#: the module under test: migration 061's CHECK is the one definition the write
#: path is validated against, and a second copy inside ``retire.py`` would make
#: these tests agree with the module by construction rather than with the
#: contract.  The helper below needs them because the vocabularies are DISJOINT
#: — ``'active'`` is legal for a DEVICE and a RACK and illegal for a CABLE — so
#: a fixture that stamped one status onto all three node types would be refused
#: by the database and every test built on it would fail for the wrong reason.
_DEVICE_VOCABULARY = frozenset(
    {"planned", "staged", "active", "offline", "decommissioning", "inventory", "failed"}
)
_CABLE_VOCABULARY = frozenset({"planned", "connected", "decommissioning"})
_RACK_VOCABULARY = frozenset({"reserved", "available", "planned", "active", "deprecated"})


#: A SECOND port that only one tenant's device carries.  It is the probe for
#: the port-expansion predicate: see
#: ``test_the_port_expansion_does_not_cross_the_tenant_boundary`` for why one
#: identifier has to differ there and why that does not weaken the collision
#: rule for everything else.
_EXTRA_PORT_REF = "ETH-2"
_EXTRA_PORT_LABEL = port_label(_DESIGN_ID, _DEVICE_REF, _EXTRA_PORT_REF)


def _topology_args(
    ns_id: uuid.UUID,
    tag: str = "ALPHA",
    *,
    status: str | None = "planned",
    extra_port: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    """The authoring bag: one device with a port in a rack, plus a cable.

    ``status`` is the DEVICE's.  The RACK and the CABLE take the same value only
    when it is in THEIR vocabulary and ``'planned'`` otherwise — see
    :data:`_CABLE_VOCABULARY`.  The alternative, stamping one value on all
    three, is refused by migration 061's composite CHECK and would turn every
    deny test that names a DEVICE status into an authoring failure.
    """
    content = _TENANT_CONTENT[tag]
    device: dict[str, Any] = {
        "device_ref": _DEVICE_REF,
        "ports": [{"port_ref": _PORT_REF, "capability": {"port_direction": "input"}}],
        "rack_ref": _RACK_REF,
        "capability": {"signal_format": "HDMI"},
        "geometry": {"x": 10, "y": 20},
        "revision": content["revision"],
        "salience": content["salience"],
    }
    rack: dict[str, Any] = {
        "rack_ref": _RACK_REF,
        "geometry": {"x": 1, "y": 2},
        "revision": content["revision"],
    }
    connection: dict[str, Any] = {
        "from_device_ref": _DEVICE_REF,
        "from_port_ref": _PORT_REF,
        "to_device_ref": _DEVICE_REF,
        "to_port_ref": _PORT_REF,
        "cable_ref": _CABLE_REF,
        "cable_revision": content["revision"],
    }
    if extra_port:
        device["ports"].append(
            {"port_ref": _EXTRA_PORT_REF, "capability": {"port_direction": "output"}}
        )
    if status is not None:
        assert status in _DEVICE_VOCABULARY, f"{status!r} is not a DEVICE status"
        device["status"] = status
        connection["cable_status"] = status if status in _CABLE_VOCABULARY else "planned"
        rack["status"] = status if status in _RACK_VOCABULARY else "planned"

    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": _DESIGN_ID,
        "devices": [device],
        "connections": [connection],
        "racks": [rack],
    }
    args.update(overrides)
    return args


async def _author(
    engine: Any, ns_id: uuid.UUID, tag: str = "ALPHA", **kwargs: Any
) -> dict[str, Any]:
    return await _dispatch_ok(engine, _TOPOLOGY_TOOL, _topology_args(ns_id, tag, **kwargs))


def _retire_args(ns_id: uuid.UUID, labels: list[str], **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "namespace_id": str(ns_id),
        "design_id": _DESIGN_ID,
        "node_labels": labels,
    }
    args.update(overrides)
    return args


async def _state_rows(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """``{node_label: row}`` for one namespace — the TEST's own read.

    The namespace predicate here is the test's, not the code's; the predicates
    actually under test are the ones inside ``retire.py``.  This exists so the
    test can look at one tenant at a time.
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


async def _node_labels(pg_pool: Any, ns_id: uuid.UUID) -> set[str]:
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            "SELECT label FROM kg_nodes WHERE namespace_id = $1::uuid", str(ns_id)
        )
    return {r["label"] for r in rows}


async def _edges(pg_pool: Any, ns_id: uuid.UUID) -> set[tuple[str, str, str]]:
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT subject_label, predicate, object_label
            FROM kg_edges WHERE namespace_id = $1::uuid
            """,
            str(ns_id),
        )
    return {(r["subject_label"], r["predicate"], r["object_label"]) for r in rows}


async def _capability_labels(pg_pool: Any, ns_id: uuid.UUID) -> set[str]:
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT node_label FROM system_design_device_capabilities
            WHERE namespace_id = $1::uuid
            """,
            str(ns_id),
        )
    return {r["node_label"] for r in rows}


async def _geometry_rows(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, Any]:
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT node_label, version FROM system_design_geometry
            WHERE namespace_id = $1::uuid
            """,
            str(ns_id),
        )
    return {r["node_label"]: r["version"] for r in rows}


async def _retire_events(pg_pool: Any, ns_id: uuid.UUID) -> list[dict[str, Any]]:
    """``system_design_authored`` params whose ``tool`` is the retire tool."""
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
    return [e for e in events if e.get("tool") == _RETIRE_TOOL]


async def _insert_pre_wave_node(pg_pool: Any, ns_id: uuid.UUID, label: str) -> None:
    """Author a node the way everything before W16 did: kg_nodes + its edge.

    The EDGE matters: the read surface walks out from the DESIGN label, so a
    node with no edge into the design is unreachable and a test built on one
    proves nothing about a real legacy node.  A real pre-wave author wrote both,
    and wrote NO state row.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        await conn.execute(
            """
            INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
            VALUES ($1, 'DEVICE', $2::uuid, 'sync')
            ON CONFLICT (label, namespace_id) DO NOTHING
            """,
            label,
            str(ns_id),
        )
        await conn.execute(
            """
            INSERT INTO kg_edges
                (subject_label, predicate, object_label, confidence,
                 namespace_id, change_origin)
            VALUES ($1, 'contains', $2, 1.0, $3::uuid, 'sync')
            ON CONFLICT (subject_label, predicate, object_label, namespace_id) DO NOTHING
            """,
            _DESIGN_LABEL,
            label,
            str(ns_id),
        )


# ===========================================================================
# 0. The surface contract — no DB.
# ===========================================================================


class TestTheNameIsADeliberateMismatch:
    """The tool says "delete" and defaults to not deleting.  That must be WRITTEN.

    Copper's contract pins both ``system_design_delete_planned`` and
    ``DELETE /api/system-design/planned``, so the mismatch cannot be resolved by
    renaming.  The only mitigation available is that every docstring on the path
    states it outright — which is a claim about bytes, and is therefore checked
    as one.  Without these tests the mismatch survives exactly as long as the
    person who introduced it remembers it.
    """

    def test_the_pinned_name_and_route_are_both_present(self) -> None:
        from nce.admin_app import build_admin_routes
        from nce.tool_registry import TOOL_REGISTRY

        assert _RETIRE_TOOL in TOOL_REGISTRY, (
            "the tool name is Copper's published contract; renaming it to match "
            "what it does breaks the front end"
        )
        by_key = {
            (getattr(route, "path", None), method): route.endpoint.__name__
            for route in build_admin_routes()
            for method in (getattr(route, "methods", None) or set())
        }
        assert by_key.get(("/api/system-design/planned", "DELETE")) == (
            "api_system_design_delete_planned"
        )

    @pytest.mark.parametrize(
        "where,text",
        [
            (
                "mcp handler",
                lambda: inspect.getdoc(
                    __import__(
                        "nce.vertical_modules.system_design.mcp_handlers",
                        fromlist=["handle_system_design_delete_planned"],
                    ).handle_system_design_delete_planned
                ),
            ),
            (
                "shared adapter",
                lambda: inspect.getdoc(
                    __import__(
                        "nce.vertical_modules.system_design.mcp_handlers",
                        fromlist=["retire_planned_from_arguments"],
                    ).retire_planned_from_arguments
                ),
            ),
            (
                "rest route",
                lambda: inspect.getdoc(
                    __import__(
                        "nce.admin_handlers.system_design",
                        fromlist=["api_system_design_delete_planned"],
                    ).api_system_design_delete_planned
                ),
            ),
        ],
    )
    def test_the_first_docstring_line_states_the_mismatch(self, where: str, text: Any) -> None:
        first_line = (text() or "").splitlines()[0]
        assert re.search(r"(?i)soft.?retire|mismatch", first_line), (
            f"the {where} docstring's FIRST line must say the name does not match the "
            f"behaviour — a caller who reads the name and stops reading is the one "
            f"this sentence protects. Got: {first_line!r}"
        )

    def test_the_mcp_tool_description_leads_with_the_mismatch(self) -> None:
        """``tools/list`` is what an agent reads; the warning has to be IN it."""
        from nce.mcp_stdio_tools import TOOLS

        tool = next((t for t in TOOLS if t.name == _RETIRE_TOOL), None)
        assert tool is not None, (
            f"{_RETIRE_TOOL} is dispatchable but not advertised: absent from TOOLS it "
            f"is invisible to tools/list, which is how a client discovers it"
        )
        head = tool.description[:200].upper()
        assert "SOFT-RETIRES BY DEFAULT" in head, (
            f"the advertised description must lead with the mismatch, not bury it: {head!r}"
        )
        assert "NOTHING IS REMOVED" in tool.description.upper()
        assert "OUT OF SCOPE" in tool.description.upper(), (
            "the description must say that retiring 'active' equipment is out of scope"
        )

    def test_the_registry_flags_are_the_contract(self) -> None:
        from nce.tool_registry import TOOL_REGISTRY

        spec = TOOL_REGISTRY[_RETIRE_TOOL]
        assert spec.mutation is True, "the cache generation must be bumped on a delete"
        assert spec.cacheable is False
        assert spec.migration is False
        assert spec.admin_only is True, (
            "admin_only is what separates this tool from the two authoring tools: "
            "those add and update, this is the only one that can take something away"
        )

    def test_the_adapter_calls_the_core_verbatim(self) -> None:
        """The surface reaches ``retire.do_retire_planned``, not a copy of it."""
        from nce.vertical_modules.system_design import mcp_handlers

        source = inspect.getsource(mcp_handlers.retire_planned_from_arguments)
        assert "do_retire_planned(" in source
        assert "bump_design_version(" in source, (
            "the version bump must happen on the retire path too: a destructive call "
            "that left the concurrency token where it was is invisible to exactly the "
            "clients that most need to notice it"
        )


class TestPureGuards:
    """The label helpers and the ``permanent`` flag — no DB needed."""

    def test_node_type_and_design_are_read_back_from_the_label(self) -> None:
        assert node_type_of_label(_DEVICE_LABEL) == "DEVICE"
        assert node_type_of_label(_RACK_LABEL) == "RACK"
        assert node_type_of_label(_CABLE_LABEL) == "CABLE"
        assert node_type_of_label(_PORT_LABEL) == "PORT"
        assert node_type_of_label("no-colon-here") is None
        assert design_of_label(_DEVICE_LABEL) == _DESIGN_ID.upper()
        assert design_of_label("DEVICE:ONLYTWO") is None

    def test_port_is_not_retirable(self) -> None:
        assert "PORT" not in RETIRABLE_NODE_TYPES
        assert "DESIGN" not in RETIRABLE_NODE_TYPES
        assert RETIRABLE_NODE_TYPES == {"DEVICE", "RACK", "CABLE"}

    def test_the_retired_status_is_per_node_type_because_rack_has_no_decommissioning(
        self,
    ) -> None:
        """A single universal constant would be refused by the DB on every rack.

        The vocabularies in migration 061's composite CHECK are disjoint.  This
        is spelled out here rather than left implicit because "just use
        'decommissioning'" is the obvious wrong answer.
        """
        assert RETIRE_STATUS_BY_NODE_TYPE["DEVICE"] == "decommissioning"
        assert RETIRE_STATUS_BY_NODE_TYPE["CABLE"] == "decommissioning"
        assert RETIRE_STATUS_BY_NODE_TYPE["RACK"] == "deprecated"
        assert set(RETIRE_STATUS_BY_NODE_TYPE) == RETIRABLE_NODE_TYPES
        assert RETIRABLE_STATUS == "planned"
        assert RETIRED_SALIENCE == 0

    @pytest.mark.parametrize("value", ["false", "true", "0", 0, 1, [], {}])
    def test_permanent_must_be_a_real_boolean(self, value: Any) -> None:
        """🔴 ``"false"`` is TRUTHY.  Coercion here would delete on a "no".

        A BFF that stringifies a flag — which a ``DELETE`` with a query string
        invites — would otherwise turn a request for the safe default into a
        permanent delete.
        """
        from nce.vertical_modules.system_design.mcp_handlers import permanent_of

        with pytest.raises(ValueError, match="permanent must be a JSON boolean"):
            permanent_of({"permanent": value})

    def test_permanent_defaults_to_the_safe_value(self) -> None:
        from nce.vertical_modules.system_design.mcp_handlers import permanent_of

        assert permanent_of({}) is False
        assert permanent_of({"permanent": None}) is False
        assert permanent_of({"permanent": False}) is False
        assert permanent_of({"permanent": True}) is True


# ===========================================================================
# 1. THE ONE-WAY DOOR — deny on absence, deny on NULL.  Separate tests.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestDenyOnAbsenceAndOnNull:
    """The two guards that stand between this tool and real installed equipment.

    They are DIFFERENT branches over DIFFERENT facts — "no row at all" versus
    "a row whose status was never declared" — and each gets its own test, so
    removing either one goes RED on its own.  A single test over "not planned"
    would stay green with one of them deleted.
    """

    async def test_a_node_with_no_state_row_is_denied(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """🔴 THE LOAD-BEARING GUARD.

        This node is authored exactly the way everything before W16 was —
        ``kg_nodes`` and its ``contains`` edge, no state row — which is the
        normal, permanent condition of the entire legacy as-built estate.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)

        assert _LEGACY_LABEL not in await _state_rows(pg_pool, namespace_id), (
            "fixture is wrong: the legacy node must have NO state row, or this "
            "test is not testing deny-on-absence"
        )

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_LEGACY_LABEL]))
        error = payload.get("error")
        assert error is not None, f"a legacy as-built node was retired: {payload}"
        assert error["data"]["reason"] == "retire_denied"
        assert error["data"]["denials"] == [
            {"node_label": _LEGACY_LABEL, "reason": DENY_STATE_ROW_ABSENT, "status": None}
        ]
        assert _LEGACY_LABEL in await _node_labels(pg_pool, namespace_id)
        assert _LEGACY_LABEL not in await _state_rows(pg_pool, namespace_id), (
            "the refusal minted a state row — deny must not write"
        )

    async def test_a_state_row_with_a_null_status_is_denied(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A row exists, but nobody ever declared a lifecycle.  NULL is not planned.

        The row is produced the way the writer really produces one: a
        PRE-EXISTING node re-authored with a ``revision`` and no ``status``.
        Inserting the row by hand would prove the guard against a shape the
        write path might not produce.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)

        await _dispatch_ok(
            engine,
            _TOPOLOGY_TOOL,
            {
                "namespace_id": str(namespace_id),
                "design_id": _DESIGN_ID,
                "devices": [{"device_ref": _LEGACY_REF, "revision": "REV-ONLY"}],
            },
        )
        row = (await _state_rows(pg_pool, namespace_id))[_LEGACY_LABEL]
        assert row["status"] is None, f"fixture is wrong: expected a NULL status, got {row}"

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_LEGACY_LABEL]))
        error = payload.get("error")
        assert error is not None, f"a node with a NULL status was retired: {payload}"
        assert error["data"]["denials"] == [
            {"node_label": _LEGACY_LABEL, "reason": DENY_STATUS_UNDECLARED, "status": None}
        ]
        assert (await _state_rows(pg_pool, namespace_id))[_LEGACY_LABEL]["status"] is None

    @pytest.mark.parametrize("status", ["active", "staged", "offline", "inventory", "failed"])
    async def test_a_declared_but_not_planned_status_is_denied(
        self, pg_pool: Any, namespace_id: uuid.UUID, status: str
    ) -> None:
        """Every other DEVICE value in the vocabulary, one at a time.

        ``'active'`` is the one that matters most — it is real installed
        equipment — but a guard that special-cased ``'active'`` would let the
        other four through, so all five are asserted.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id, status=status)

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        error = payload.get("error")
        assert error is not None, f"a {status!r} device was retired: {payload}"
        assert error["data"]["denials"] == [
            {"node_label": _DEVICE_LABEL, "reason": DENY_STATUS_NOT_PLANNED, "status": status}
        ]
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == status

    async def test_a_state_row_whose_node_is_gone_is_denied(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """An orphan state row is not a retirable node.

        Reporting a successful retirement of something that is not in the graph
        would put a false record in the WORM log.  The orphan is created the way
        one really arises — by deleting the node and leaving the row — which is
        precisely the D12 hazard this wave exists to close, so this test also
        documents what a *failure* of the permanent path looks like from the
        outside.
        """
        from nce.db_utils import scoped_pg_session

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            await conn.execute(
                "DELETE FROM kg_nodes WHERE namespace_id = $1::uuid AND label = $2",
                str(namespace_id),
                _DEVICE_LABEL,
            )
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned"

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        error = payload.get("error")
        assert error is not None, f"an orphan state row was retired: {payload}"
        assert error["data"]["denials"] == [
            {"node_label": _DEVICE_LABEL, "reason": DENY_NODE_ABSENT, "status": None}
        ]

    async def test_one_denied_label_denies_the_whole_call(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """All or nothing: the retirable node in the same call is NOT retired.

        This is the positive control for the deny tests above — it proves the
        refusal is the *request's*, not the label's, and that a caller cannot
        smuggle a delete through by pairing it with a denied node.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)

        payload = await _dispatch(
            engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL, _LEGACY_LABEL])
        )
        error = payload.get("error")
        assert error is not None
        assert [d["node_label"] for d in error["data"]["denials"]] == [_LEGACY_LABEL], (
            "only the legacy node is undeniable; the planned one is refused BY "
            "ASSOCIATION, which is the whole point"
        )
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned", (
            "the retirable node in a refused call was retired anyway"
        )

    async def test_every_denial_is_reported_not_just_the_first(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A canvas selecting many nodes needs many answers in one response."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id, status="active")
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)

        payload = await _dispatch(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL, _LEGACY_LABEL]),
        )
        reasons = {d["node_label"]: d["reason"] for d in payload["error"]["data"]["denials"]}
        assert reasons == {
            _DEVICE_LABEL: DENY_STATUS_NOT_PLANNED,
            _LEGACY_LABEL: DENY_STATE_ROW_ABSENT,
        }


# ===========================================================================
# 2. Both directions — a genuinely planned node IS retirable.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestSoftRetireIsTheDefault:
    """An over-strict guard is a defect too, and "delete" must not delete.

    Every assertion here is the positive half of a denial above: without them a
    guard of ``False`` — which refuses everything — would pass the whole deny
    suite.
    """

    async def test_a_planned_node_is_retired_and_nothing_is_removed(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        before_nodes = await _node_labels(pg_pool, namespace_id)
        before_edges = await _edges(pg_pool, namespace_id)
        before_caps = await _capability_labels(pg_pool, namespace_id)

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(
                namespace_id, [_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL], actor="ada@example.test"
            ),
        )

        assert payload["permanent"] is False
        assert payload["removed"] is None, "the DEFAULT path must not report removals"
        assert {r["node_label"]: r["to"] for r in payload["retired"]} == {
            _DEVICE_LABEL: "decommissioning",
            _RACK_LABEL: "deprecated",
            _CABLE_LABEL: "decommissioning",
        }
        assert all(r["from"] == "planned" for r in payload["retired"])

        # 🔴 Nothing removed.  This is what the tool's name gets wrong.
        assert await _node_labels(pg_pool, namespace_id) == before_nodes
        assert await _edges(pg_pool, namespace_id) == before_edges
        assert await _capability_labels(pg_pool, namespace_id) == before_caps

        rows = await _state_rows(pg_pool, namespace_id)
        assert rows[_DEVICE_LABEL]["status"] == "decommissioning"
        assert rows[_RACK_LABEL]["status"] == "deprecated", (
            "a RACK has no 'decommissioning' in its NetBox vocabulary; writing one "
            "would be refused by migration 061's composite CHECK"
        )
        assert rows[_CABLE_LABEL]["status"] == "decommissioning"

    async def test_the_salience_is_floored_to_zero_not_decayed(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """Absolute, not multiplicative.

        PostgreSQL ``numeric`` NaN survives multiplication and sorts above every
        finite value, so a decay would leave a retired node holding the highest
        salience in the tenant.  Migration 061's CHECK keeps NaN out, so this
        pins the defence rather than a live hole — and the fixture starts from a
        NON-ZERO salience so a floor that did nothing at all cannot pass.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        before = (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["salience"]
        assert before is not None and float(before) > 0, (
            f"fixture is wrong: the device must start with a positive salience, got {before!r}"
        )

        await _dispatch_ok(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))

        after = (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["salience"]
        assert after is not None and float(after) == 0.0, f"salience was not floored: {after!r}"

    async def test_actor_is_optional_on_the_soft_path(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """Rev 2 §1: ``actor`` is optional and is never invented.

        The permanent path is the ONE exception, and if that exception leaked
        onto the default path the safe operation would become unusable without
        attribution while the destructive one stayed reachable.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch_ok(
            engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL])
        )
        assert payload["permanent"] is False
        events = await _retire_events(pg_pool, namespace_id)
        assert "actor" not in events[-1], (
            "an omitted actor must be recorded as ABSENT, never as '' and never as a "
            "synthesised service identity"
        )

    async def test_the_design_version_moves_on_a_retire(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A client polling the token must be able to see that a retire happened."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        authored = await _author(engine, namespace_id)

        payload = await _dispatch_ok(
            engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL])
        )
        assert payload["version"] == authored["version"] + 1

    async def test_a_stale_expected_version_refuses_and_retires_nothing(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        authored = await _author(engine, namespace_id)

        payload = await _dispatch(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], expected_version=authored["version"] + 99),
        )
        error = payload.get("error")
        assert error is not None
        assert error["code"] == -32040, "a stale token is a conflict, not invalid params"
        assert error["data"]["reason"] == "version_conflict"
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned", (
            "the refused request retired the node anyway — the compare-and-swap is "
            "not inside the retire's transaction"
        )

    async def test_a_denial_and_a_stale_token_carry_different_codes(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A client must be able to tell "retry after a re-read" from "never".

        Both are conflicts and both are 409 over REST, so ``reason`` and the MCP
        code are the only discriminators; collapsing them makes a correct client
        either spin on a hopeless request or abandon a winnable one.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id, status="active")

        denied = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        assert denied["error"]["code"] == -32041
        assert denied["error"]["data"]["reason"] == "retire_denied"
        assert denied["error"]["code"] != -32040


# ===========================================================================
# 3. Argument-level refusals — a fact about the REQUEST, not about the graph.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestArgumentRefusals:
    """These are ``-32602`` / 422, deliberately NOT a denial.

    "You may not name this here" and "this node is not in a retirable state" are
    different facts and a caller acts on them differently — the first is a bug
    in their code, the second is something to show a user.
    """

    async def test_a_port_label_is_refused(self, pg_pool: Any, namespace_id: uuid.UUID) -> None:
        """NetBox has no lifecycle status for a port, so one can never be planned.

        Migration 061's composite CHECK cannot even store a PORT row, so a port
        is structurally undeclarable — refusing the label is what makes that
        legible instead of surfacing as a mysterious deny-on-absence.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_PORT_LABEL]))
        assert payload["error"]["code"] == -32602
        assert _PORT_LABEL in await _node_labels(pg_pool, namespace_id)

    async def test_a_label_from_another_design_is_refused(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """Labels are deterministic and therefore guessable.

        Without this, ``design_id`` would be decoration and a caller could reach
        any node in the tenant by naming a well-formed label.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        foreign = device_label("SOME-OTHER-DESIGN", _DEVICE_REF)
        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [foreign]))
        assert payload["error"]["code"] == -32602
        assert "does not belong to design" in payload["error"]["data"]["detail"]

    async def test_the_design_version_row_cannot_be_named(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """Deleting it would reset the token to 0 and hand every stale client a win."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DESIGN_LABEL]))
        assert payload["error"]["code"] == -32602
        assert _DESIGN_LABEL in await _geometry_rows(pg_pool, namespace_id)

    @pytest.mark.parametrize("labels", [[], "DEVICE:X:Y", [""], [None], [123]])
    async def test_a_malformed_node_labels_argument_is_refused(
        self, pg_pool: Any, namespace_id: uuid.UUID, labels: Any
    ) -> None:
        engine = _EngineStub(pg_pool)
        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, labels))
        assert payload["error"]["code"] == -32602

    async def test_an_unparseable_label_is_refused_AS_UNPARSEABLE(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """The MESSAGE is the gate here, and it has to be, for a stated reason.

        A label with no ``':'`` is refused three times over — the node-type
        branch, the ``RETIRABLE_NODE_TYPES`` branch and the design branch all
        reject it — because "no colon" implies "no parseable design id".  The
        first mutation sweep proved it: replacing the unparseable branch with a
        silent default left the whole suite GREEN, since the design check caught
        the same input one line later.

        So the OUTCOME is over-determined and only the caller-visible message
        distinguishes the branches.  That message is a real part of the
        contract — "this is not a node label" and "this label belongs to another
        design" send a client to completely different bugs in its own code — so
        it is asserted rather than the branch being deleted or the redundancy
        being written off as untestable.
        """
        engine = _EngineStub(pg_pool)
        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, ["no-colon"]))
        assert payload["error"]["code"] == -32602
        assert "is not a node label" in payload["error"]["data"]["detail"], (
            "an unparseable label must be reported as unparseable, not as a label "
            "belonging to another design"
        )

    async def test_the_ownership_registry_denies_by_default(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """No registry row -> refused.  ``_seed_ownership`` is deliberately absent.

        The positive control is every other test in this file, all of which seed
        it and succeed.
        """
        from nce.db_utils import scoped_pg_session

        engine = _EngineStub(pg_pool)
        # Author with ownership seeded, then remove the registry rows, so the
        # graph is in the retirable state and ONLY the ownership guard differs.
        await _seed_ownership(pg_pool, namespace_id)
        await _author(engine, namespace_id)
        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            await conn.execute(
                "DELETE FROM node_ownership_registry WHERE namespace_id = $1::uuid",
                str(namespace_id),
            )

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        assert "error" in payload, f"a delete ran with no ownership row: {payload}"
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned"


# ===========================================================================
# 4. actor is MANDATORY on the permanent path.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestActorIsMandatoryWhenPermanent:
    """The one place Rev 2 §1's "an omitted actor is fine" is refused.

    An unattributable permanent delete is exactly what should fail closed, and
    the refusal is enforced in the CORE rather than in one adapter, so both
    surfaces get it from the same line.
    """

    @pytest.mark.parametrize("actor", [None, "", "   "])
    async def test_permanent_without_an_actor_is_refused(
        self, pg_pool: Any, namespace_id: uuid.UUID, actor: Any
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        args = _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True)
        if actor is not None:
            args["actor"] = actor
        payload = await _dispatch(engine, _RETIRE_TOOL, args)

        assert payload["error"]["code"] == -32602, f"a delete ran with actor={actor!r}: {payload}"
        assert "actor is required when permanent=true" in payload["error"]["data"]["detail"]
        assert _DEVICE_LABEL in await _node_labels(pg_pool, namespace_id), (
            "the unattributed request deleted the node anyway"
        )

    async def test_the_refusal_happens_before_anything_is_read(self) -> None:
        """It is checked before the state read, so it cannot be reordered behind a write.

        Verified by BYTES rather than by behaviour: the behavioural test above
        cannot tell "refused first" from "refused after a harmless read", and
        the ordering is what stops a later edit from moving the check past a
        DELETE.
        """
        from nce.vertical_modules.system_design import retire

        source = inspect.getsource(retire.do_retire_planned)
        actor_check = source.index("actor is required when permanent=true")
        state_read = source.index("_fetch_state(")
        assert actor_check < state_read, (
            "the mandatory-actor refusal must come before the state read"
        )

    async def test_permanent_with_an_actor_succeeds(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """The positive control: without it, a guard of ``False`` passes the above."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert payload["permanent"] is True
        assert _DEVICE_LABEL not in await _node_labels(pg_pool, namespace_id)


# ===========================================================================
# 5. The permanent path — D12: every side table, in the same transaction.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestPermanentDeleteRemovesEverything:
    """🔴 No FK ties a side-table row to its node.  Every DELETE is obligatory.

    Each assertion below corresponds to one statement in
    ``retire._delete_permanently``, and each goes RED on its own when that
    statement is removed — which is the point: a single "the node is gone"
    assertion would stay green with all three side-table statements deleted, and
    that is precisely the orphan the resurrection test then exploits.
    """

    async def test_the_node_and_both_edge_directions_are_gone(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A node is the SUBJECT of some edges and the OBJECT of others.

        ``DESIGN -contains-> DEVICE`` makes the device an object;
        ``DEVICE -has_port-> PORT`` and ``DEVICE -mounted_in-> RACK`` make it a
        subject.  A subject-only delete leaves dangling halves that the read
        surface still walks, so both directions are asserted.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        before = await _edges(pg_pool, namespace_id)
        assert any(e[0] == _DEVICE_LABEL for e in before), "fixture: device must be a subject"
        assert any(e[2] == _DEVICE_LABEL for e in before), "fixture: device must be an object"

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )

        assert _DEVICE_LABEL not in await _node_labels(pg_pool, namespace_id)
        after = await _edges(pg_pool, namespace_id)
        assert not [e for e in after if _DEVICE_LABEL in (e[0], e[2])], (
            f"edges survived the delete: {[e for e in after if _DEVICE_LABEL in (e[0], e[2])]}"
        )

    async def test_the_state_row_is_deleted_with_the_node(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """D12, statement one.  This is the row that causes the resurrection."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        assert _DEVICE_LABEL in await _state_rows(pg_pool, namespace_id)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert _DEVICE_LABEL not in await _state_rows(pg_pool, namespace_id), (
            "the state row outlived its node — a later re-author of the same "
            "deterministic label will inherit its status through ON CONFLICT DO UPDATE"
        )

    async def test_the_capability_row_is_deleted_with_the_node(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """D12, statement two."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        assert _DEVICE_LABEL in await _capability_labels(pg_pool, namespace_id)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert _DEVICE_LABEL not in await _capability_labels(pg_pool, namespace_id)

    async def test_the_geometry_row_is_deleted_with_the_node(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """D12, statement three."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        assert _DEVICE_LABEL in await _geometry_rows(pg_pool, namespace_id)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert _DEVICE_LABEL not in await _geometry_rows(pg_pool, namespace_id)

    async def test_the_design_version_row_survives(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """The GRAIN GUARD.

        ``system_design_geometry`` holds node-geometry rows and the one
        per-design version row under the same natural key, distinguished only by
        ``version``.  A geometry delete that ignored the grain would reset the
        design's optimistic-concurrency token to nothing, silently, and every
        stale client would then win its next compare-and-swap.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        rows = await _geometry_rows(pg_pool, namespace_id)
        assert _DESIGN_LABEL in rows, "the design's version row was deleted with the node"
        assert rows[_DESIGN_LABEL] == payload["version"]

    async def test_the_geometry_grain_guard_spares_the_version_row_directly(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """The grain guard, gated through the PRIVATE — and here is why.

        ``system_design_geometry`` holds node-geometry rows (``version`` NULL)
        and the one per-design optimistic-concurrency version row under the same
        natural key.  A geometry DELETE that ignored the grain would wipe the
        version row and silently reset the design's token, handing every stale
        client its next compare-and-swap.

        🔴 **The guard is UNREACHABLE through the public surfaces**, because
        ``_validate_label_shape`` refuses a ``DESIGN:`` label before the delete
        is ever reached — which is exactly why the first mutation sweep found
        removing it left the whole suite GREEN.  Rather than record it as
        ungated, this test drives ``_delete_permanently`` directly with the one
        input the public API cannot produce, because that IS the guard's
        contract: *if* a DESIGN-labelled row ever reaches this function, the
        version row must survive.  It becomes live the day anything else writes
        a DESIGN-labelled geometry row — the same standing as
        ``bump_design_version``'s own grain guard, which ``geometry.py``
        documents in the same terms.

        The sibling test above (``test_the_design_version_row_survives``) covers
        the reachable half through the tool.
        """
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.retire import _delete_permanently

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        authored = await _author(engine, namespace_id)

        before = await _geometry_rows(pg_pool, namespace_id)
        assert before[_DESIGN_LABEL] == authored["version"]
        assert _DEVICE_LABEL in before and before[_DEVICE_LABEL] is None, (
            "fixture: a node geometry row must carry a NULL version, or the guard "
            "under test would spare it for the wrong reason"
        )

        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            await _delete_permanently(
                conn,
                namespace_id,
                [_DEVICE_LABEL, _DESIGN_LABEL],
                {_DEVICE_LABEL: "DEVICE", _DESIGN_LABEL: "DESIGN"},
            )

        after = await _geometry_rows(pg_pool, namespace_id)
        assert _DEVICE_LABEL not in after, "positive control: the node row must have gone"
        assert after.get(_DESIGN_LABEL) == authored["version"], (
            "the design's version row was deleted with the nodes: every stale client "
            "now wins its next compare-and-swap"
        )

    async def test_the_devices_ports_go_with_it(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A PORT has no state row but does have capability and geometry rows.

        Leaving ports behind orphans nodes whose ``has_port`` edge is gone, and
        re-authoring the device re-creates ports that inherit the orphans'
        capability rows — the same defect family one level down.  ``removed``
        reports the count so the caller can see it happened.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        assert _PORT_LABEL in await _node_labels(pg_pool, namespace_id)
        assert _PORT_LABEL in await _capability_labels(pg_pool, namespace_id)

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )

        assert payload["removed"]["ports"] == 1
        assert payload["removed"]["nodes"] == 2, "the device and its one port"
        assert _PORT_LABEL not in await _node_labels(pg_pool, namespace_id)
        assert _PORT_LABEL not in await _capability_labels(pg_pool, namespace_id)

    async def test_unnamed_nodes_are_untouched(self, pg_pool: Any, namespace_id: uuid.UUID) -> None:
        """The positive control for every "is gone" assertion above.

        A DELETE with a broken label predicate — or none — would empty the
        design and still pass all of them.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )

        survivors = await _node_labels(pg_pool, namespace_id)
        assert _RACK_LABEL in survivors
        assert _CABLE_LABEL in survivors
        rows = await _state_rows(pg_pool, namespace_id)
        assert rows[_RACK_LABEL]["status"] == "planned"
        assert rows[_CABLE_LABEL]["status"] == "planned"


# ===========================================================================
# 6. 🔴 D12 — THE RESURRECTION.  The scenario an auditor reproduced on 67g.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestResurrection:
    """Delete a node, re-author the same label, and it must come back CLEAN.

    Labels are deterministic: the same ``design_id`` and ``device_ref`` produce
    the same label forever.  If the permanent delete leaves the state row
    behind, the re-authored node lands on the orphan through
    ``ON CONFLICT DO UPDATE`` and silently inherits its status — a node that was
    permanently deleted while ``'planned'`` comes back already declared, and one
    retired as ``'decommissioning'`` comes back already dying.

    This test drives the whole scenario through the REAL authoring tool rather
    than asserting on ``retire.py``'s internals, because the inheritance happens
    in ``devices._upsert_node_state``, which this wave does not touch.
    """

    async def test_a_re_authored_label_does_not_inherit_a_deleted_nodes_status(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)

        # 1. Author, then soft-retire so the row carries a NON-DEFAULT status.
        #    Starting from 'planned' would make the test unable to tell an
        #    inherited status from a correctly seeded one — which is exactly the
        #    confound that lets this defect hide.
        await _author(engine, namespace_id)
        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], actor="ada@example.test"),
        )
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == (
            "decommissioning"
        )

        # 2. Now permanently delete it. (Its status is no longer 'planned', so
        #    put it back to planned first — through the authoring surface, the
        #    way a real operator would.)
        await _author(engine, namespace_id, status="planned")
        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert _DEVICE_LABEL not in await _node_labels(pg_pool, namespace_id)
        assert _DEVICE_LABEL not in await _state_rows(pg_pool, namespace_id), (
            "the orphan that causes the resurrection is still there"
        )

        # 3. Re-author the SAME design_id and device_ref -> the SAME label.
        await _author(engine, namespace_id, status=None)

        rows = await _state_rows(pg_pool, namespace_id)
        assert rows[_DEVICE_LABEL]["status"] == "planned", (
            "the re-authored node inherited a deleted node's status: the delete left "
            "an orphan state row and ON CONFLICT DO UPDATE landed on it"
        )

    async def test_the_authoring_event_reports_the_node_as_new_not_resurrected(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """``resurrected`` is 67g's marker, and it is CONSUMED here — as a gate.

        67g emits ``resurrected`` in the authoring delta and nothing reads it.
        This wave's answer: it is not consumed at runtime — there is nothing for
        the delete path to do with it, because the delete path's job is to make
        it impossible — but it IS consumed as the exact assertion that proves
        that.  ``resurrected: true`` after a correct permanent delete is the
        defect's signature, so asserting it stays false is a stronger statement
        than any status comparison, and it is the reason the flag was worth
        emitting.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)

        await _author(engine, namespace_id)
        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        await _author(engine, namespace_id, status=None)

        from nce.db_utils import scoped_pg_session

        async with scoped_pg_session(pg_pool, namespace_id) as conn:
            rows = await conn.fetch(
                """
                SELECT params FROM event_log
                WHERE namespace_id = $1::uuid AND event_type = 'system_design_authored'
                ORDER BY event_seq
                """,
                str(namespace_id),
            )
        events = [
            json.loads(r["params"]) if isinstance(r["params"], str) else dict(r["params"])
            for r in rows
        ]
        last_author = [e for e in events if e.get("tool") == _TOPOLOGY_TOOL][-1]
        deltas = {d["node_label"]: d for d in last_author.get("state_changes", [])}
        assert _DEVICE_LABEL in deltas, (
            "the re-author created the node, so it must have produced a state delta"
        )
        assert deltas[_DEVICE_LABEL]["resurrected"] is False, (
            "the re-authored node landed on an orphan state row"
        )
        assert deltas[_DEVICE_LABEL]["state_row_created"] is True
        assert deltas[_DEVICE_LABEL]["from"] is None


# ===========================================================================
# 7. Transactionality.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestTransactionality:
    """A failure part-way through leaves NOTHING deleted.

    The failure is injected at the audit-event append — after every DELETE has
    run and before the transaction commits — which is the only window where a
    non-transactional implementation would be visible.  A test that failed
    before the deletes would prove nothing at all.
    """

    async def test_a_failure_after_the_deletes_rolls_all_of_them_back(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        before_nodes = await _node_labels(pg_pool, namespace_id)
        before_edges = await _edges(pg_pool, namespace_id)
        before_state = await _state_rows(pg_pool, namespace_id)
        before_caps = await _capability_labels(pg_pool, namespace_id)
        before_geom = await _geometry_rows(pg_pool, namespace_id)

        boom = RuntimeError("injected: the audit append failed after the deletes")

        async def _explode(**kwargs: Any) -> None:
            raise boom

        monkeypatch.setattr(
            "nce.vertical_modules.system_design.mcp_handlers.append_event", _explode
        )

        payload = await _dispatch(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )
        assert "error" in payload, "the injected failure did not reach the caller"

        assert await _node_labels(pg_pool, namespace_id) == before_nodes, (
            "nodes stayed deleted through a failed transaction"
        )
        assert await _edges(pg_pool, namespace_id) == before_edges
        assert set(await _state_rows(pg_pool, namespace_id)) == set(before_state)
        assert await _capability_labels(pg_pool, namespace_id) == before_caps
        assert await _geometry_rows(pg_pool, namespace_id) == before_geom

    async def test_the_same_injection_does_not_roll_back_a_soft_retire_silently(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The soft path is in the same transaction as its audit row.

        Positive control for the test above: it shows the injection point really
        is inside the transaction rather than after it, because the status
        change is rolled back too.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        async def _explode(**kwargs: Any) -> None:
            raise RuntimeError("injected")

        monkeypatch.setattr(
            "nce.vertical_modules.system_design.mcp_handlers.append_event", _explode
        )
        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        assert "error" in payload
        assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned"


# ===========================================================================
# 8. Owner-pool tenant isolation (§6.4).
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestOwnerPoolTenantIsolation:
    """Two tenants that collide on EVERY identifier and differ only in content.

    ``nce_app`` serves no request in this deployment, so every statement runs on
    a pool that ``FORCE ROW LEVEL SECURITY`` does not constrain.  The boundary
    is the explicit ``namespace_id`` predicate on every statement in
    ``retire.py`` — the state read, the existence probe, the port expansion, the
    soft UPDATE and all five DELETEs.

    The labels below are BYTE-IDENTICAL between the tenants.  A fixture that
    differed on labels could not detect a predicate that filters by label, and
    B067b failed TAG on exactly that.
    """

    async def test_a_soft_retire_in_one_tenant_does_not_touch_the_other(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        alpha = await make_namespace()
        example = await make_namespace()
        assert alpha != example
        for ns in (alpha, example):
            await _seed_ownership(pg_pool, ns)
        engine = _EngineStub(pg_pool)

        await _author(engine, alpha, "ALPHA")
        await _author(engine, example, "BETA")

        # Same labels in both tenants — that is the point of the fixture.
        assert _DEVICE_LABEL in await _state_rows(pg_pool, alpha)
        assert _DEVICE_LABEL in await _state_rows(pg_pool, example)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(alpha, [_DEVICE_LABEL, _RACK_LABEL, _CABLE_LABEL], actor="ada@alpha.test"),
        )

        example_rows = await _state_rows(pg_pool, example)
        assert example_rows[_DEVICE_LABEL]["status"] == "planned", (
            "tenant ALPHA's retire changed tenant BETA's device"
        )
        assert example_rows[_RACK_LABEL]["status"] == "planned"
        assert example_rows[_CABLE_LABEL]["status"] == "planned"
        assert example_rows[_DEVICE_LABEL]["revision"] == _TENANT_CONTENT["BETA"]["revision"], (
            "content differs only per tenant; the wrong value here means the read "
            "crossed the boundary"
        )
        assert float(example_rows[_DEVICE_LABEL]["salience"]) > 0, (
            "tenant BETA's salience was floored by tenant ALPHA's retire"
        )

    async def test_a_permanent_delete_in_one_tenant_does_not_touch_the_other(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        alpha = await make_namespace()
        example = await make_namespace()
        for ns in (alpha, example):
            await _seed_ownership(pg_pool, ns)
        engine = _EngineStub(pg_pool)

        await _author(engine, alpha, "ALPHA")
        await _author(engine, example, "BETA")
        example_before = {
            "nodes": await _node_labels(pg_pool, example),
            "edges": await _edges(pg_pool, example),
            "caps": await _capability_labels(pg_pool, example),
            "geom": await _geometry_rows(pg_pool, example),
            "state": set(await _state_rows(pg_pool, example)),
        }

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(alpha, [_DEVICE_LABEL], permanent=True, actor="ada@alpha.test"),
        )

        assert _DEVICE_LABEL not in await _node_labels(pg_pool, alpha), (
            "positive control: the delete must actually have happened in ALPHA"
        )
        assert await _node_labels(pg_pool, example) == example_before["nodes"], (
            "tenant ALPHA's delete removed tenant BETA's nodes"
        )
        assert await _edges(pg_pool, example) == example_before["edges"]
        assert await _capability_labels(pg_pool, example) == example_before["caps"]
        assert await _geometry_rows(pg_pool, example) == example_before["geom"]
        assert set(await _state_rows(pg_pool, example)) == example_before["state"]

    async def test_the_port_expansion_does_not_cross_the_tenant_boundary(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The one predicate the byte-identical fixture CANNOT see, and the fix.

        The permanent path expands a DEVICE to its ports through the
        ``has_port`` edges.  Every other statement's namespace predicate is
        observable through content, but this one is not: when both tenants hold
        the SAME device with the SAME port, the expansion returns the same label
        either way, the set collapses it, and dropping the predicate changes
        nothing anyone can measure.  The first mutation sweep proved that —
        removing it left the whole suite green.

        So this test, and only this test, gives BETA one identifier ALPHA does
        not have: a second port.  That is not a retreat from the collision rule
        (§6.4) — every shared node still collides byte-for-byte, and the extra
        port is a PROBE, not a differentiator.  A predicate that filtered by
        label would still be invisible to it; what it detects is specifically a
        read that crossed the namespace boundary, which shows up as ALPHA
        reporting that it removed a port that only exists in BETA.
        """
        alpha = await make_namespace()
        example = await make_namespace()
        for ns in (alpha, example):
            await _seed_ownership(pg_pool, ns)
        engine = _EngineStub(pg_pool)

        await _author(engine, alpha, "ALPHA")
        await _author(engine, example, "BETA", extra_port=True)

        assert _EXTRA_PORT_LABEL in await _node_labels(pg_pool, example)
        assert _EXTRA_PORT_LABEL not in await _node_labels(pg_pool, alpha), (
            "fixture is wrong: the probe port must exist in BETA only"
        )

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(alpha, [_DEVICE_LABEL], permanent=True, actor="ada@alpha.test"),
        )
        assert payload["removed"]["ports"] == 1, (
            "ALPHA's port expansion saw BETA's port: the has_port read crossed the "
            "namespace boundary"
        )
        assert _EXTRA_PORT_LABEL in await _node_labels(pg_pool, example)

    async def test_a_tenant_cannot_reach_a_node_that_only_the_other_tenant_has(
        self, pg_pool: Any, make_namespace: Any
    ) -> None:
        """The complementary direction: ALPHA names a label only BETA holds.

        The two tests above prove ALPHA's write does not reach BETA's rows.
        This one proves ALPHA's *read* does not either — without it, a state read
        that crossed the boundary would look like a successful retire of a node
        ALPHA never had.
        """
        alpha = await make_namespace()
        example = await make_namespace()
        for ns in (alpha, example):
            await _seed_ownership(pg_pool, ns)
        engine = _EngineStub(pg_pool)

        await _author(engine, example, "BETA")  # ALPHA authors NOTHING.

        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(alpha, [_DEVICE_LABEL]))
        error = payload.get("error")
        assert error is not None, f"ALPHA retired a node that exists only in BETA: {payload}"
        assert error["data"]["denials"] == [
            {"node_label": _DEVICE_LABEL, "reason": DENY_NODE_ABSENT, "status": None}
        ]
        assert (await _state_rows(pg_pool, example))[_DEVICE_LABEL]["status"] == "planned"


# ===========================================================================
# 9. The audit record.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheAuditRecord:
    """A destructive call must leave a WORM row saying what it did and for whom.

    It goes to ``event_log`` (INSERT-only, HMAC-signed, Merkle-chained), not to
    ``outbox_events`` (a delivery queue the runtime role may UPDATE and DELETE).
    """

    async def test_a_permanent_delete_records_the_actor_and_the_counts(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test"),
        )

        events = await _retire_events(pg_pool, namespace_id)
        assert len(events) == 1
        event = events[0]
        assert event["actor"] == "ada@example.test"
        assert event["permanent"] is True
        assert event["design_id"] == _DESIGN_ID
        assert event["removed"]["nodes"] == 2
        assert [r["node_label"] for r in event["retired"]] == [_DEVICE_LABEL]
        assert event["retired"][0]["from"] == "planned"
        assert event["retired"][0]["to"] is None, "there is no status afterwards"

    async def test_permanent_false_is_recorded_positively_not_by_omission(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """ "This was the reversible one" must be an assertion, not an inference.

        Every other optional key on this payload follows absent-means-absent.
        ``permanent`` deliberately does not: a missing key could equally mean
        "the field predates this reader", and on a destructive path that
        ambiguity is not acceptable in the WORM log.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        await _dispatch_ok(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))

        event = (await _retire_events(pg_pool, namespace_id))[0]
        assert "permanent" in event and event["permanent"] is False
        assert "removed" not in event, "nothing was removed, so the key is absent"
        assert event["retired"][0]["to"] == "decommissioning"

    async def test_a_denied_call_writes_no_audit_row(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """A refusal is not an action.  The WORM log must not fill with non-events."""
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)

        await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_LEGACY_LABEL]))
        assert await _retire_events(pg_pool, namespace_id) == []


# ===========================================================================
# 10. Idempotence-ish behaviour and the second call.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestASecondCallIsRefusedNotRepeated:
    """Retiring is not idempotent, and it must not pretend to be.

    After a soft retire the node's status is no longer ``'planned'``, so the
    same call is DENIED rather than silently succeeding.  That is the correct
    shape: a caller who repeats a destructive request should learn that the
    first one landed, not be told "done" twice.
    """

    async def test_the_second_soft_retire_is_denied(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        await _dispatch_ok(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
        payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))

        assert payload["error"]["data"]["denials"] == [
            {
                "node_label": _DEVICE_LABEL,
                "reason": DENY_STATUS_NOT_PLANNED,
                "status": "decommissioning",
            }
        ]

    async def test_the_second_permanent_delete_is_denied_as_absent(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        args = _retire_args(namespace_id, [_DEVICE_LABEL], permanent=True, actor="ada@example.test")
        await _dispatch_ok(engine, _RETIRE_TOOL, dict(args))
        payload = await _dispatch(engine, _RETIRE_TOOL, dict(args))

        assert payload["error"]["data"]["denials"] == [
            {"node_label": _DEVICE_LABEL, "reason": DENY_NODE_ABSENT, "status": None}
        ]

    async def test_a_duplicated_label_is_counted_once(
        self, pg_pool: Any, namespace_id: uuid.UUID
    ) -> None:
        """De-duplication is not cosmetic: the counts are per DISTINCT label.

        A payload naming the same device twice must not read as two deletions in
        the audit record.
        """
        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)

        payload = await _dispatch_ok(
            engine,
            _RETIRE_TOOL,
            _retire_args(namespace_id, [_DEVICE_LABEL, _DEVICE_LABEL, _DEVICE_LABEL]),
        )
        assert len(payload["retired"]) == 1


# ===========================================================================
# 11. The REST surface.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
class TestRestSurface:
    """The route is the same adapter, so only the HTTP translation is tested here."""

    async def test_a_soft_retire_over_rest(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nce import admin_state
        from nce.admin_handlers.system_design import api_system_design_delete_planned

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await api_system_design_delete_planned(
            _StubRequest(
                {
                    "namespace_id": str(namespace_id),
                    "design_id": _DESIGN_ID,
                    "node_labels": [_DEVICE_LABEL],
                }
            )
        )
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["permanent"] is False
        assert body["removed"] is None
        assert _DEVICE_LABEL in await _node_labels(pg_pool, namespace_id)

    async def test_a_denial_is_409_with_the_same_discriminator_as_mcp(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """409, not 422 and not 403 — and the reason is read from one definition."""
        from nce import admin_state
        from nce.admin_handlers.system_design import api_system_design_delete_planned

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _insert_pre_wave_node(pg_pool, namespace_id, _LEGACY_LABEL)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await api_system_design_delete_planned(
            _StubRequest(
                {
                    "namespace_id": str(namespace_id),
                    "design_id": _DESIGN_ID,
                    "node_labels": [_LEGACY_LABEL],
                }
            )
        )
        assert response.status_code == 409, "a state conflict is not a validation failure"
        body = json.loads(response.body)
        assert body["reason"] == RetireDeniedError.reason == "retire_denied"
        assert body["denials"][0]["reason"] == DENY_STATE_ROW_ABSENT

    async def test_a_stringified_permanent_is_422_not_a_deletion(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 ``"false"`` is truthy.  This is the trap the body-not-query rule avoids."""
        from nce import admin_state
        from nce.admin_handlers.system_design import api_system_design_delete_planned

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        await _author(engine, namespace_id)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await api_system_design_delete_planned(
            _StubRequest(
                {
                    "namespace_id": str(namespace_id),
                    "design_id": _DESIGN_ID,
                    "node_labels": [_DEVICE_LABEL],
                    "permanent": "false",
                }
            )
        )
        assert response.status_code == 422
        assert _DEVICE_LABEL in await _node_labels(pg_pool, namespace_id), (
            'a request that said "false" deleted the node'
        )

    async def test_a_stale_token_is_409_with_the_other_reason(
        self, pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nce import admin_state
        from nce.admin_handlers.system_design import api_system_design_delete_planned

        await _seed_ownership(pg_pool, namespace_id)
        engine = _EngineStub(pg_pool)
        authored = await _author(engine, namespace_id)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await api_system_design_delete_planned(
            _StubRequest(
                {
                    "namespace_id": str(namespace_id),
                    "design_id": _DESIGN_ID,
                    "node_labels": [_DEVICE_LABEL],
                    "expected_version": authored["version"] + 99,
                }
            )
        )
        assert response.status_code == 409
        body = json.loads(response.body)
        assert body["reason"] == "version_conflict", (
            "the two 409s must stay distinguishable by reason"
        )

    async def test_the_route_requires_a_namespace_id(
        self, pg_pool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nce import admin_state
        from nce.admin_handlers.system_design import api_system_design_delete_planned

        monkeypatch.setattr(admin_state, "engine", _EngineStub(pg_pool), raising=False)
        response = await api_system_design_delete_planned(
            _StubRequest({"design_id": _DESIGN_ID, "node_labels": [_DEVICE_LABEL]})
        )
        assert response.status_code == 422


# ===========================================================================
# 12. The admin gate.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_admin_only_is_enforced_by_the_dispatch_loop(
    pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the dev override OFF and no admin key, the MCP call is refused.

    This is the FAIL-CLOSED half of the ``admin_only=True`` contract. Batch
    67L made ``enforce_mcp_tool_auth`` take the admin branch for this tool
    (its registry ``admin_only`` flag is now honoured alongside the hardcoded
    ``MCP_ADMIN_TOOL_NAMES`` list), but the admin branch itself must still
    refuse when no override and no admin key are present. This test pins that
    refusal so a future edit cannot quietly open the destructive tool to a
    tenant key.
    """
    monkeypatch.delenv("NCE_ADMIN_OVERRIDE", raising=False)
    monkeypatch.delenv("NCE_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("NCE_MCP_API_KEY", raising=False)

    await _seed_ownership(pg_pool, namespace_id)
    engine = _EngineStub(pg_pool)
    await _author(engine, namespace_id)

    payload = await _dispatch(engine, _RETIRE_TOOL, _retire_args(namespace_id, [_DEVICE_LABEL]))
    assert "error" in payload, f"an admin_only delete ran without admin scope: {payload}"
    assert (await _state_rows(pg_pool, namespace_id))[_DEVICE_LABEL]["status"] == "planned"
