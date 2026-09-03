"""
tests/test_system_design_validate_surface.py
============================================
Module 6 Wave 13c — the ``system_design_validate_design_graph`` surface.

What these tests actually gate
------------------------------
1. **The validator stopped being unreachable.**  ``validate_design_graph`` has
   existed since Phase 2 and no MCP tool, REST route or A2A skill could call it.
   Every behavioural test below therefore runs through ``execute_call_tool`` —
   the real dispatch path (registry lookup, auth, governance, cache, quota,
   handler).  Calling the core directly would still pass if the tool were never
   registered, which is precisely the defect this wave exists to fix.

2. **The verdict is asserted by REASON, not by boolean.**  ``passed=False``
   alone is satisfied by *any* failing check, so a continuity test that asserted
   only the boolean would stay green if continuity broke and some unrelated
   check happened to fail instead.  Each behavioural test therefore pins the
   **exact** ``reasons`` list, which is what makes it discriminate between the
   five checks rather than merely between pass and fail.

3. **Two frozen semantics.**  ``validation_queries.py`` is not modified by this
   wave, and two of its behaviours are deliberate:

   * an **unknown signal format** does not fail the design, and
   * the **power/heat budget is informational** — it always contributes its
     totals to ``reasons`` and never sets ``passed=False``, because NCE holds no
     budget ceiling.

   Both are pinned here so that a later wave changing them has to change a test
   on purpose.  Each is paired with a *positive control* in the same test — a
   known-incompatible version pair that DOES fail, and an over-budget design
   whose totals are still reported — because "X does not fail" is vacuous
   evidence if the check that would fail X never runs at all.

4. **Owner-pool tenant isolation.**  ``nce_app`` is used for exactly one thing
   in this deployment (a boot-time WORM self-check) and never to serve a
   request, and the capability table's RLS policy is written ``FOR ALL TO
   nce_app`` — so every request runs on a pool that policy does not cover, and
   the guard that actually isolates tenants is the explicit ``namespace_id``
   predicate in ``read.py``'s SQL.

   The isolation fixture therefore collides **every identifier the two tenants
   share** — same design id, same namespace slug (so the FUNCTIONAL_LOCATION
   labels collide too), same site, same buildings, same device refs, same port
   refs — and differentiates the tenants **only on content**: manufacturer,
   model number, ``extra``, power/heat figures, one signal version, one absent
   ``signal_format``, one unconnected input port.  A fixture that gave the two
   tenants different *labels* could not detect a predicate that filters by
   label, because the label difference would be doing the filtering for it.

5. **The two surfaces agree.**  ``POST /api/system-design/validate`` is not only
   asserted to be mounted — it is called against the same seeded graph as the
   MCP tool and its payload compared to the MCP one.  A route that exists and
   returns the wrong thing would satisfy a mounted-ness assertion forever.

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

_TOOL_NAME = "system_design_validate_design_graph"

_MOCK_EMIT_GRAPH = "nce.vertical_modules.system_design.graph.emit_graph_write"
_MOCK_EMIT_DEVICES = "nce.vertical_modules.system_design.devices.emit_graph_write"

_BUILDINGS: list[dict[str, Any]] = [
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


# ---------------------------------------------------------------------------
# Label helpers — mirror devices.py so the expected reason strings are built the
# same way the production code builds them, not copy-pasted.
# ---------------------------------------------------------------------------


def _device_label(design_id: str, device_ref: str) -> str:
    return f"DEVICE:{design_id.upper()}:{device_ref.upper()}"


def _port_label(design_id: str, device_ref: str, port_ref: str) -> str:
    return f"PORT:{design_id.upper()}:{device_ref.upper()}:{port_ref.upper()}"


def _power_reason(total_watts: float, device_count: int) -> str:
    return f"total power draw: {total_watts:.1f} W across {device_count} device(s)"


def _heat_reason(total_btu: float) -> str:
    return f"total heat dissipation: {total_btu:.1f} BTU/hr"


class _StubRequest:
    """Minimal duck-typed Starlette request: the route reads only ``.json()``."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self.path_params: dict[str, str] = {}

    async def json(self) -> Any:
        return self._body


class _EngineStub:
    """Engine surface the dispatch loop touches.

    ``redis_client=None`` is deliberate: it makes the response cache a no-op, so
    a passing assertion proves the queries ran rather than that a cached payload
    was replayed.  (The tool is ``cacheable=False`` anyway; this removes the
    variable entirely.)
    """

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool
        self.redis_client = None


# ---------------------------------------------------------------------------
# Seeding — one design, authored through the real domain cores.
# ---------------------------------------------------------------------------


async def _seed(
    pg_pool: Any,
    ns_id: uuid.UUID,
    *,
    namespace_slug: str,
    design_id: str,
    devices: list[dict[str, Any]],
    connections: list[dict[str, Any]] | None = None,
    site_name: str = "SiteAlpha",
) -> None:
    """Author a DESIGN with its functional-location tree and the given devices."""
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
                namespace_slug=namespace_slug,
                design_id=design_id,
                site_name=site_name,
                buildings=_BUILDINGS,
            )

    with patch(_MOCK_EMIT_DEVICES, new_callable=AsyncMock):
        async with scoped_pg_session(pg_pool, ns_id) as conn:
            await do_author_device_topology(
                conn,
                ns_id,
                design_id=design_id,
                devices=devices,
                connections=connections or [],
            )


async def _validate_through_dispatch(
    engine: Any,
    ns_id: uuid.UUID,
    design_id: str,
) -> dict[str, Any]:
    """Call ``system_design_validate_design_graph`` through the real MCP dispatch path."""
    from nce.mcp_stdio_dispatch import execute_call_tool

    parts = await execute_call_tool(
        engine,
        _TOOL_NAME,
        {"namespace_id": str(ns_id), "design_id": design_id},
    )
    assert parts, "dispatch returned no content"
    payload = json.loads(parts[0].text)
    assert "error" not in payload, f"dispatch returned an error envelope: {payload}"
    return payload


# ---------------------------------------------------------------------------
# Fixture data builders.
# ---------------------------------------------------------------------------


def _source_device(
    *,
    device_ref: str = "SRC",
    manufacturer: str = "Extron",
    model_number: str = "SRC-1",
    power: float = 30.0,
    heat: float = 102.0,
    signal_format: str | None = "HDMI",
    signal_version: str | None = "2.1",
    redundancy_role: str = "standalone",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A device with exactly one OUTPUT port."""
    return {
        "device_ref": device_ref,
        "capability": {
            "device_category": "AV Sources",
            "manufacturer": manufacturer,
            "model_number": model_number,
            "power_draw_watts": power,
            "heat_btu_hr": heat,
            "redundancy_role": redundancy_role,
            "extra": extra or {},
        },
        "ports": [
            {
                "port_ref": "P1",
                "capability": {
                    "signal_format": signal_format,
                    "signal_version": signal_version,
                    "port_direction": "output",
                },
            }
        ],
        "rack_ref": None,
    }


def _sink_device(
    *,
    device_ref: str = "SINK",
    manufacturer: str = "Barco",
    model_number: str = "SINK-1",
    power: float = 20.0,
    heat: float = 68.0,
    ports: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A device with the given INPUT ports (default: one HDMI 2.0 input)."""
    return {
        "device_ref": device_ref,
        "capability": {
            "device_category": "AV Displays",
            "manufacturer": manufacturer,
            "model_number": model_number,
            "power_draw_watts": power,
            "heat_btu_hr": heat,
            "redundancy_role": "standalone",
            "extra": extra or {},
        },
        "ports": ports
        if ports is not None
        else [
            {
                "port_ref": "P1",
                "capability": {
                    "signal_format": "HDMI",
                    "signal_version": "2.0",
                    "port_direction": "input",
                },
            }
        ],
        "rack_ref": None,
    }


def _link(from_port: str, to_port: str) -> dict[str, Any]:
    return {
        "from_device_ref": "SRC",
        "from_port_ref": from_port,
        "to_device_ref": "SINK",
        "to_port_ref": to_port,
    }


# ---------------------------------------------------------------------------
# 1. Registration — the surface exists on BOTH registries and on REST.
#
# These are the unit-level half of the §6.4 rows "tool removed from
# TOOL_REGISTRY" and "tool removed from TOOLS": a tool missing from
# TOOL_REGISTRY is undispatchable, and one missing from TOOLS is invisible to
# tools/list — a client cannot discover what it is not told about.
# ---------------------------------------------------------------------------


def test_tool_is_dispatchable() -> None:
    """The tool is in TOOL_REGISTRY with Copper's exact flags."""
    from nce.tool_registry import TOOL_REGISTRY

    assert _TOOL_NAME in TOOL_REGISTRY
    spec = TOOL_REGISTRY[_TOOL_NAME]
    assert spec.cacheable is False, (
        "cacheable=False is Copper's contract: a design under active canvas "
        "editing must never be served a stale verdict."
    )
    assert spec.admin_only is False
    assert spec.mutation is False


def test_tool_is_advertised() -> None:
    """The tool is in TOOLS, so tools/list can see it."""
    from nce.mcp_stdio_tools import TOOLS

    advertised = {tool.name for tool in TOOLS}
    assert _TOOL_NAME in advertised, (
        "Tool is dispatchable but not advertised: absent from TOOLS it is "
        "invisible to tools/list, which is how a client discovers it."
    )


def test_rest_route_is_wired() -> None:
    """POST /api/system-design/validate is mounted on the admin app."""
    from nce.admin_app import app

    matches = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/system-design/validate"
    ]
    assert matches, "POST /api/system-design/validate is not mounted"
    assert "POST" in matches[0].methods


def test_wrapper_calls_the_frozen_core() -> None:
    """The handler wraps ``validate_design_graph`` itself, not a re-implementation."""
    from nce.vertical_modules.system_design import mcp_handlers, validation_queries

    assert mcp_handlers.validate_design_graph is validation_queries.validate_design_graph, (
        "the MCP adapter is not bound to validation_queries.validate_design_graph"
    )

    from nce.admin_handlers import system_design as system_design_routes

    assert system_design_routes.validate_design_graph is validation_queries.validate_design_graph, (
        "the REST adapter is not bound to validation_queries.validate_design_graph"
    )


# ---------------------------------------------------------------------------
# 2. Integration — the five checks, through dispatch.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestValidateThroughDispatch:
    _SLUG = "w13c-validate"

    async def test_continuity_violation_fails_with_its_own_reason(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dangling input port fails the design — and names *that* as the reason.

        The exact-``reasons`` assertion is the point.  ``passed is False`` alone
        would also be satisfied by a format mismatch, a SPOF finding or a missing
        AVIXA attribute, so it would not distinguish "continuity is checked" from
        "something, somewhere, failed".
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        design_id = "DESIGN-W13C-CONTINUITY"
        ns_id: uuid.UUID = await make_namespace()
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=design_id,
            devices=[_source_device(), _sink_device()],
            # No connections at all -> SINK:P1 is a dangling input.
            connections=[],
        )

        payload = await _validate_through_dispatch(_EngineStub(pg_pool), ns_id, design_id)

        assert payload["passed"] is False
        assert payload["reasons"] == [
            f"input port '{_port_label(design_id, 'SINK', 'P1')}' has no inbound "
            f"connected_to edge (dangling input)",
            _power_reason(50.0, 2),
            _heat_reason(170.0),
        ], (
            "the continuity violation must be reported as a continuity reason, "
            "and nothing else may have failed"
        )

    async def test_clean_design_passes(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A well-formed design passes all five checks.

        Its ``reasons`` is exactly the informational power/heat pair — a clean
        design still carries those, which is the whole point of check #3.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        design_id = "DESIGN-W13C-CLEAN"
        ns_id: uuid.UUID = await make_namespace()
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=design_id,
            devices=[_source_device(), _sink_device()],
            connections=[_link("P1", "P1")],
        )

        payload = await _validate_through_dispatch(_EngineStub(pg_pool), ns_id, design_id)

        assert payload["passed"] is True
        assert payload["reasons"] == [
            _power_reason(50.0, 2),
            _heat_reason(170.0),
        ]

    async def test_unknown_signal_format_does_not_fail_the_design(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FROZEN semantics: an unrecognised format is warn-only, never fatal.

        ``_formats_compatible`` has no version table for a format it does not
        know, so it cannot rank two versions of it and does not pretend to: the
        pair is accepted and contributes **no** failure reason.  That is
        deliberate (this wave changes nothing in ``validation_queries.py``).

        The second half of this test is the positive control.  "An unknown format
        does not fail" is vacuous evidence unless the format check is running at
        all — so the same fixture shape with a *known* format and an
        incompatible version pair is asserted to fail, in the same test.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        # --- unknown format, both ends: accepted, warn-only ---------------
        unknown_id = "DESIGN-W13C-UNKNOWNFMT"
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=unknown_id,
            devices=[
                _source_device(signal_format="SDI", signal_version="12G"),
                _sink_device(
                    ports=[
                        {
                            "port_ref": "P1",
                            "capability": {
                                "signal_format": "SDI",
                                "signal_version": "3G",
                                "port_direction": "input",
                            },
                        }
                    ]
                ),
            ],
            connections=[_link("P1", "P1")],
        )

        unknown = await _validate_through_dispatch(engine, ns_id, unknown_id)

        assert unknown["passed"] is True, (
            "an unrecognised signal format must not fail the design "
            "(validation_queries.py semantics are frozen)"
        )
        assert unknown["reasons"] == [
            _power_reason(50.0, 2),
            _heat_reason(170.0),
        ], (
            "an unrecognised format contributes NO failure reason — the only "
            "reasons on a warn-only design are the informational power/heat "
            "totals from check #3"
        )

        # --- positive control: a KNOWN format with an incompatible pair ----
        known_id = "DESIGN-W13C-KNOWNFMT"
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=known_id,
            devices=[
                # HDMI 2.0 source cannot drive an HDMI 2.1 sink.
                _source_device(signal_format="HDMI", signal_version="2.0"),
                _sink_device(
                    ports=[
                        {
                            "port_ref": "P1",
                            "capability": {
                                "signal_format": "HDMI",
                                "signal_version": "2.1",
                                "port_direction": "input",
                            },
                        }
                    ]
                ),
            ],
            connections=[_link("P1", "P1")],
        )

        known = await _validate_through_dispatch(engine, ns_id, known_id)

        assert known["passed"] is False, (
            "the format check must actually run — if a known incompatible pair "
            "passes, the warn-only assertion above proves nothing"
        )
        assert known["reasons"] == [
            f"connection '{_port_label(known_id, 'SRC', 'P1')}' -> "
            f"'{_port_label(known_id, 'SINK', 'P1')}': HDMI version mismatch: "
            f"source '2.0' (ord=20) cannot drive sink '2.1' (ord=21)",
            _power_reason(50.0, 2),
            _heat_reason(170.0),
        ]

    async def test_power_heat_is_informational_not_a_budget(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FROZEN semantics: an absurd power/heat load still returns passed=True.

        NCE holds no budget ceiling — the operator sets their own — so check #3
        reports the totals and never votes.  The figures below are far past any
        plausible room budget precisely so that a ceiling introduced later would
        turn this test RED instead of silently changing Copper's contract.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        design_id = "DESIGN-W13C-POWERHEAT"
        ns_id: uuid.UUID = await make_namespace()
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=design_id,
            devices=[
                _source_device(power=12000.0, heat=40944.0),
                _sink_device(power=8000.0, heat=27296.0),
            ],
            connections=[_link("P1", "P1")],
        )

        payload = await _validate_through_dispatch(_EngineStub(pg_pool), ns_id, design_id)

        assert payload["passed"] is True, (
            "power/heat is informational: it must never set passed=False"
        )
        assert payload["reasons"] == [
            _power_reason(20000.0, 2),
            _heat_reason(68240.0),
        ], "the totals must be reported verbatim, with no ceiling applied"

    async def test_unpaired_primary_device_is_a_spof(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``primary`` device with no ``secondary`` peer fails check #4.

        This case exists because the §6.4 matrix found it missing: with every
        fixture device marked ``standalone``, neutering ``check_spof_redundancy``
        left the whole file GREEN — the suite gated four of the five checks and
        said nothing about the fifth.  A check nobody's fixture reaches is a
        check nobody's test defends.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        design_id = "DESIGN-W13C-SPOF"
        ns_id: uuid.UUID = await make_namespace()
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=design_id,
            devices=[_source_device(redundancy_role="primary"), _sink_device()],
            connections=[_link("P1", "P1")],
        )

        payload = await _validate_through_dispatch(_EngineStub(pg_pool), ns_id, design_id)

        assert payload["passed"] is False
        assert payload["reasons"] == [
            _power_reason(50.0, 2),
            _heat_reason(170.0),
            "SPOF risk: 1 primary device(s) found but no secondary devices are "
            "present in the design",
        ], "the SPOF finding must be reported as a redundancy reason, and nothing else may fail"


@pytest.mark.integration
@pytest.mark.asyncio
class TestRestSurface:
    """The REST twin must return the same verdict, not merely be mounted.

    ``test_rest_route_is_wired`` proves the Route exists; it would keep proving
    that if the handler returned nonsense.  So the route is *called*, against the
    same seeded graph as the MCP path, and the two payloads are compared.  Two
    surfaces over one core is only true if they agree.
    """

    _SLUG = "w13c-rest"

    async def test_rest_returns_the_same_verdict_as_mcp(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        from nce import admin_state
        from nce.admin_handlers import system_design as routes

        design_id = "DESIGN-W13C-REST"
        ns_id: uuid.UUID = await make_namespace()
        await _seed(
            pg_pool,
            ns_id,
            namespace_slug=self._SLUG,
            design_id=design_id,
            devices=[_source_device(), _sink_device()],
            connections=[],  # dangling input -> a verdict with a specific reason
        )

        engine = _EngineStub(pg_pool)
        monkeypatch.setattr(admin_state, "engine", engine, raising=False)

        response = await routes.api_system_design_validate_design_graph(
            _StubRequest({"namespace_id": str(ns_id), "design_id": design_id})
        )
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "ok"

        via_mcp = await _validate_through_dispatch(engine, ns_id, design_id)
        assert body["validation"] == via_mcp, (
            "the REST route and the MCP tool disagree about the same design — "
            "two surfaces over one core must return one answer.\n"
            f"REST: {body['validation']}\nMCP:  {via_mcp}"
        )
        assert body["validation"]["passed"] is False
        assert (
            f"input port '{_port_label(design_id, 'SINK', 'P1')}' has no inbound "
            f"connected_to edge (dangling input)" in body["validation"]["reasons"]
        )


@pytest.mark.asyncio
async def test_rest_requires_design_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing design_id is a 422, not a 500 — and never reaches the database."""
    from nce import admin_state
    from nce.admin_handlers import system_design as routes

    monkeypatch.setattr(admin_state, "engine", _EngineStub(None), raising=False)
    response = await routes.api_system_design_validate_design_graph(
        _StubRequest({"namespace_id": str(uuid.uuid4())})
    )
    assert response.status_code == 422
    assert "design_id" in json.loads(response.body)["error"]


@pytest.mark.asyncio
async def test_rest_requires_namespace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing namespace_id is a 422 — the tenant is never defaulted."""
    from nce import admin_state
    from nce.admin_handlers import system_design as routes

    monkeypatch.setattr(admin_state, "engine", _EngineStub(None), raising=False)
    response = await routes.api_system_design_validate_design_graph(
        _StubRequest({"design_id": "DESIGN-X"})
    )
    assert response.status_code == 422
    assert "namespace_id" in json.loads(response.body)["error"]


# ---------------------------------------------------------------------------
# 3. Owner-pool tenant isolation — every identifier collides; only content differs.
# ---------------------------------------------------------------------------

_ISO_DESIGN_ID = "DESIGN-W13C-ISO"
_ISO_SLUG = "w13c-iso"
_ISO_SITE = "SiteShared"


def _alpha_devices() -> list[dict[str, Any]]:
    """Tenant ALPHA: an incompatible link, a port with no signal_format, a dangling input."""
    return [
        _source_device(
            manufacturer="ALPHA-CORP",
            model_number="ALPHA-SRC",
            power=100.0,
            heat=341.0,
            signal_format="HDMI",
            signal_version="2.0",
            extra={"tenant": "alpha"},
        ),
        _sink_device(
            manufacturer="ALPHA-CORP",
            model_number="ALPHA-SINK",
            power=25.0,
            heat=85.0,
            extra={"tenant": "alpha"},
            ports=[
                # 2.0 -> 2.1 is a downgrade the source cannot drive: check #2 fails.
                {
                    "port_ref": "P1",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "input",
                    },
                },
                # No signal_format at all: check #5 fails.
                {"port_ref": "P2", "capability": {"port_direction": "input"}},
                # Never connected below: check #1 fails.
                {
                    "port_ref": "P3",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "input",
                    },
                },
            ],
        ),
    ]


def _beta_devices() -> list[dict[str, Any]]:
    """Tenant BETA: the same labels throughout, but a clean design."""
    return [
        _source_device(
            manufacturer="BETA-CORP",
            model_number="BETA-SRC",
            power=7000.0,
            heat=23885.0,
            signal_format="HDMI",
            signal_version="2.1",
            extra={"tenant": "beta"},
        ),
        _sink_device(
            manufacturer="BETA-CORP",
            model_number="BETA-SINK",
            power=3000.0,
            heat=10236.0,
            extra={"tenant": "beta"},
            ports=[
                {
                    "port_ref": ref,
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.0",
                        "port_direction": "input",
                    },
                }
                for ref in ("P1", "P2", "P3")
            ],
        ),
    ]


def _alpha_expected_reasons() -> list[str]:
    return [
        # check #1 — P3 has no inbound edge.
        f"input port '{_port_label(_ISO_DESIGN_ID, 'SINK', 'P3')}' has no inbound "
        f"connected_to edge (dangling input)",
        # check #2 — HDMI 2.0 cannot drive HDMI 2.1.
        f"connection '{_port_label(_ISO_DESIGN_ID, 'SRC', 'P1')}' -> "
        f"'{_port_label(_ISO_DESIGN_ID, 'SINK', 'P1')}': HDMI version mismatch: "
        f"source '2.0' (ord=20) cannot drive sink '2.1' (ord=21)",
        # check #3 — informational.
        _power_reason(125.0, 2),
        _heat_reason(426.0),
        # check #5 — P2 has no signal_format.
        f"PORT '{_port_label(_ISO_DESIGN_ID, 'SINK', 'P2')}': missing "
        f"'signal_format' (AVIXA required)",
    ]


def _beta_expected_reasons() -> list[str]:
    return [
        _power_reason(10000.0, 2),
        _heat_reason(34121.0),
    ]


@pytest.mark.integration
@pytest.mark.asyncio
class TestOwnerPoolIsolation:
    """Two tenants, byte-identical labels, different content.

    Construction note (§6.4).  Both namespaces receive the **same** design id,
    the **same** namespace slug (so even the FUNCTIONAL_LOCATION labels, which
    are slug-prefixed, collide), the same site, the same buildings, the same
    device refs and the same port refs.  Nothing about the two graphs' *shape*
    or *naming* differs.  What differs is content: manufacturer, model number,
    ``extra``, the power and heat figures, one signal version, one absent
    ``signal_format``, and whether ``SINK:P3`` is connected.

    That construction is load-bearing.  B067b's fixture gave the two tenants
    different device refs, so only the DESIGN label actually collided — and a
    namespace predicate that filters on a label cannot be shown to matter by a
    fixture whose labels already differ.  Here every label is shared, so the
    explicit ``namespace_id = $n::uuid`` predicates in ``read.py``'s SQL are the
    only thing keeping the two verdicts apart.

    This runs on the ordinary integration pool — the owner-role pool that serves
    every request in this deployment, and one the capability table's ``FOR ALL
    TO nce_app`` policy does not cover.  RLS is therefore not what is being
    tested here; the SQL predicates are.
    """

    async def _seed_both(
        self,
        pg_pool: Any,
        ns_alpha: uuid.UUID,
        ns_beta: uuid.UUID,
    ) -> None:
        await _seed(
            pg_pool,
            ns_alpha,
            namespace_slug=_ISO_SLUG,
            design_id=_ISO_DESIGN_ID,
            site_name=_ISO_SITE,
            devices=_alpha_devices(),
            connections=[_link("P1", "P1"), _link("P1", "P2")],
        )
        await _seed(
            pg_pool,
            ns_beta,
            namespace_slug=_ISO_SLUG,
            design_id=_ISO_DESIGN_ID,
            site_name=_ISO_SITE,
            devices=_beta_devices(),
            connections=[_link("P1", "P1"), _link("P1", "P2"), _link("P1", "P3")],
        )

    async def test_each_tenant_validates_only_its_own_graph(
        self,
        pg_pool: Any,
        make_namespace: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Neither tenant's verdict may be influenced by the other's rows.

        Both verdicts are asserted, and both are asserted **exactly**.  A leak in
        either direction changes at least one of them: a duplicated join row
        repeats a reason or inflates the device count and the power/heat totals,
        and a leaked capability row flips a version comparison or introduces the
        other tenant's missing ``signal_format``.
        """
        monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

        ns_alpha: uuid.UUID = await make_namespace()
        ns_beta: uuid.UUID = await make_namespace()
        await self._seed_both(pg_pool, ns_alpha, ns_beta)

        engine = _EngineStub(pg_pool)
        alpha = await _validate_through_dispatch(engine, ns_alpha, _ISO_DESIGN_ID)
        beta = await _validate_through_dispatch(engine, ns_beta, _ISO_DESIGN_ID)

        assert alpha["reasons"] == _alpha_expected_reasons(), (
            "tenant ALPHA's verdict was influenced by tenant BETA's rows.\n"
            f"got:      {alpha['reasons']}\n"
            f"expected: {_alpha_expected_reasons()}"
        )
        assert alpha["passed"] is False

        assert beta["reasons"] == _beta_expected_reasons(), (
            "tenant BETA's verdict was influenced by tenant ALPHA's rows.\n"
            f"got:      {beta['reasons']}\n"
            f"expected: {_beta_expected_reasons()}"
        )
        assert beta["passed"] is True

    async def test_the_two_tenants_really_do_share_every_label(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Guard the guard: prove the fixture collides labels rather than assuming it.

        If the two tenants' node label sets ever stopped being identical, the
        isolation test above would still pass — on fixture uniqueness, gating
        nothing.  That is exactly how B067b's isolation test came to exercise one
        of five namespace predicates.  So the collision is asserted, not trusted.
        """
        from nce.db_utils import scoped_pg_session

        ns_alpha: uuid.UUID = await make_namespace()
        ns_beta: uuid.UUID = await make_namespace()
        await self._seed_both(pg_pool, ns_alpha, ns_beta)

        async def _labels(ns_id: uuid.UUID) -> set[str]:
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                rows = await conn.fetch(
                    "SELECT label FROM kg_nodes WHERE namespace_id = $1::uuid",
                    ns_id,
                )
            return {r["label"] for r in rows}

        labels_alpha = await _labels(ns_alpha)
        labels_beta = await _labels(ns_beta)

        assert labels_alpha, "tenant ALPHA seeded no nodes"
        assert labels_alpha == labels_beta, (
            "the two tenants no longer share every node label, so the isolation "
            "test above could pass on fixture uniqueness alone.\n"
            f"ALPHA only: {sorted(labels_alpha - labels_beta)}\n"
            f"BETA only: {sorted(labels_beta - labels_alpha)}"
        )
        # The DESIGN node and both devices and all four ports are in there.
        assert f"DESIGN:{_ISO_DESIGN_ID}" in labels_alpha
        assert _device_label(_ISO_DESIGN_ID, "SRC") in labels_alpha
        assert _port_label(_ISO_DESIGN_ID, "SINK", "P3") in labels_alpha


# ---------------------------------------------------------------------------
# 4. Argument validation (pure — no DB).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_namespace_id_is_invalid_params() -> None:
    """A missing namespace_id is an McpError(-32602), not a 500."""
    from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
    from nce.vertical_modules.system_design.mcp_handlers import (
        handle_system_design_validate_design_graph,
    )

    with pytest.raises(McpError) as exc_info:
        await handle_system_design_validate_design_graph(
            _EngineStub(None), {"design_id": "DESIGN-X"}
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS


@pytest.mark.asyncio
async def test_missing_design_id_is_invalid_params() -> None:
    """A missing design_id is an McpError(-32602), not a 500."""
    from nce.mcp_errors import MCP_INVALID_PARAMS, McpError
    from nce.vertical_modules.system_design.mcp_handlers import (
        handle_system_design_validate_design_graph,
    )

    with pytest.raises(McpError) as exc_info:
        await handle_system_design_validate_design_graph(
            _EngineStub(None), {"namespace_id": str(uuid.uuid4())}
        )
    assert exc_info.value.code == MCP_INVALID_PARAMS
