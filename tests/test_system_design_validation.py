"""
tests/test_system_design_validation.py
=======================================
Phase-2 validation tests for Module 6 System Design.

Structure
---------
Pure-unit tests (no DB):
  - check_signal_flow_continuity
  - check_port_format_compatibility   (HDMI 2.1/2.0, Dante channels)
  - check_power_heat_budget
  - check_spof_redundancy
  - check_avixa_checkpoint_conformance

Integration tests (@pytest.mark.integration — require live Postgres):
  - TestDeviceTopologyIntegration:
      seeds a 3-device graph (display, DSP with Dante, switch) with ports and
      connections, then calls validate_design_graph and asserts:
        * signal_flow_continuity passes (all inputs connected)
        * port_format_compatibility passes for compatible connections
        * the full result shape is {passed: bool, reasons: list}
  - TestPhase1ContractUnchanged:
      imports do_validate_design and confirms it still operates correctly —
      proves enrich-not-rewrite.

All DB-dependent tests are @pytest.mark.integration.
Pure-unit tests carry no marks and run with ``-m "not integration"``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.system_design.validation_queries import (
    check_avixa_checkpoint_conformance,
    check_port_format_compatibility,
    check_power_heat_budget,
    check_signal_flow_continuity,
    check_spof_redundancy,
    validate_design_graph,
)

# ---------------------------------------------------------------------------
# Pure-unit tests — no DB, no marks
# ---------------------------------------------------------------------------


class TestCheckSignalFlowContinuity:
    """check_signal_flow_continuity: pure function."""

    def test_all_inputs_connected_passes(self) -> None:
        input_ports = ["PORT:D1:IN1", "PORT:D2:IN1"]
        targets = {"PORT:D1:IN1", "PORT:D2:IN1"}
        result = check_signal_flow_continuity(input_ports, targets)
        assert result["passed"] is True
        assert result["reasons"] == []

    def test_dangling_input_fails(self) -> None:
        input_ports = ["PORT:D1:IN1", "PORT:D2:IN1"]
        targets = {"PORT:D1:IN1"}  # D2:IN1 not connected
        result = check_signal_flow_continuity(input_ports, targets)
        assert result["passed"] is False
        assert any("PORT:D2:IN1" in r for r in result["reasons"])

    def test_no_input_ports_passes(self) -> None:
        result = check_signal_flow_continuity([], set())
        assert result["passed"] is True

    def test_all_inputs_dangling_fails(self) -> None:
        input_ports = ["PORT:D1:IN1"]
        result = check_signal_flow_continuity(input_ports, set())
        assert result["passed"] is False
        assert len(result["reasons"]) == 1


class TestCheckPortFormatCompatibility:
    """check_port_format_compatibility: pure function."""

    def _cap(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    def test_identical_hdmi_same_version_passes(self) -> None:
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="HDMI", signal_version="2.0"),
            "P_IN": self._cap(signal_format="HDMI", signal_version="2.0"),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True

    def test_hdmi_21_to_20_source_higher_passes(self) -> None:
        """HDMI 2.1 output into HDMI 2.0 input is valid (backward-compat)."""
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="HDMI", signal_version="2.1"),
            "P_IN": self._cap(signal_format="HDMI", signal_version="2.0"),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True

    def test_hdmi_20_to_21_source_lower_fails(self) -> None:
        """HDMI 2.0 output into HDMI 2.1 input is invalid."""
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="HDMI", signal_version="2.0"),
            "P_IN": self._cap(signal_format="HDMI", signal_version="2.1"),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is False
        assert any("2.0" in r and "2.1" in r for r in result["reasons"])

    def test_hdmi_to_dp_format_mismatch_fails(self) -> None:
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="HDMI", signal_version="2.0"),
            "P_IN": self._cap(signal_format="DP", signal_version="1.4"),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is False

    def test_dante_to_dante_passes(self) -> None:
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="Dante", dante_tx_channels=64),
            "P_IN": self._cap(signal_format="Dante", dante_rx_channels=32),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True

    def test_dante_tx_less_than_rx_fails(self) -> None:
        """Dante source with fewer TX channels than sink needs RX fails."""
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="Dante", dante_tx_channels=8),
            "P_IN": self._cap(signal_format="Dante", dante_rx_channels=16),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is False
        assert any("Dante" in r for r in result["reasons"])

    def test_dante_to_aes67_cross_format_passes(self) -> None:
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps = {
            "P_OUT": self._cap(signal_format="DANTE"),
            "P_IN": self._cap(signal_format="AES67"),
        }
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True

    def test_unknown_format_passes(self) -> None:
        """None signal_format is treated as compatible."""
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps: dict[str, dict[str, Any]] = {"P_OUT": {}, "P_IN": {}}
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True

    def test_missing_capability_entry_treated_as_empty(self) -> None:
        connections = [{"from_port": "P_OUT", "to_port": "P_IN"}]
        caps: dict[str, dict[str, Any]] = {}
        result = check_port_format_compatibility(connections, caps)
        assert result["passed"] is True


class TestCheckPowerHeatBudget:
    """check_power_heat_budget: pure, always passed=True, totals in reasons."""

    def test_returns_totals(self) -> None:
        devs = [
            {"power_draw_watts": 100.0, "heat_btu_hr": 341.2},
            {"power_draw_watts": 50.0, "heat_btu_hr": 170.6},
        ]
        result = check_power_heat_budget(devs)
        assert result["passed"] is True
        assert len(result["reasons"]) == 2
        assert any("150.0" in r for r in result["reasons"])

    def test_empty_devices_passes(self) -> None:
        result = check_power_heat_budget([])
        assert result["passed"] is True
        assert any("0.0" in r for r in result["reasons"])

    def test_none_values_treated_as_zero(self) -> None:
        devs = [{"power_draw_watts": None, "heat_btu_hr": None}]
        result = check_power_heat_budget(devs)
        assert result["passed"] is True


class TestCheckSpofRedundancy:
    """check_spof_redundancy: pure function."""

    def test_no_redundancy_devices_passes(self) -> None:
        devs = [{"redundancy_role": "standalone"}, {"redundancy_role": None}]
        result = check_spof_redundancy(devs)
        assert result["passed"] is True

    def test_primary_with_secondary_passes(self) -> None:
        devs = [
            {"redundancy_role": "primary"},
            {"redundancy_role": "secondary"},
        ]
        result = check_spof_redundancy(devs)
        assert result["passed"] is True

    def test_primary_without_secondary_fails(self) -> None:
        devs = [{"redundancy_role": "primary"}, {"redundancy_role": "standalone"}]
        result = check_spof_redundancy(devs)
        assert result["passed"] is False
        assert any("SPOF" in r for r in result["reasons"])

    def test_more_primaries_than_secondaries_fails(self) -> None:
        devs = [
            {"redundancy_role": "primary"},
            {"redundancy_role": "primary"},
            {"redundancy_role": "secondary"},
        ]
        result = check_spof_redundancy(devs)
        assert result["passed"] is False

    def test_empty_devices_passes(self) -> None:
        result = check_spof_redundancy([])
        assert result["passed"] is True


class TestCheckAvixaCheckpointConformance:
    """check_avixa_checkpoint_conformance: pure function."""

    def test_fully_populated_passes(self) -> None:
        device_caps = [
            {"node_label": "DEVICE:D1:CTRL", "device_category": "Communication Devices"},
        ]
        port_caps = [
            {
                "node_label": "PORT:D1:CTRL:HDMI_OUT",
                "signal_format": "HDMI",
                "port_direction": "output",
            },
        ]
        result = check_avixa_checkpoint_conformance(device_caps, port_caps)
        assert result["passed"] is True
        assert result["reasons"] == []

    def test_device_missing_category_fails(self) -> None:
        device_caps = [{"node_label": "DEVICE:D1:CTRL", "device_category": None}]
        port_caps = [
            {"node_label": "PORT:D1:CTRL:OUT", "signal_format": "HDMI", "port_direction": "output"},
        ]
        result = check_avixa_checkpoint_conformance(device_caps, port_caps)
        assert result["passed"] is False
        assert any("device_category" in r for r in result["reasons"])

    def test_port_missing_signal_format_fails(self) -> None:
        device_caps = [{"node_label": "DEVICE:D1", "device_category": "AV"}]
        port_caps = [{"node_label": "PORT:D1:P1", "signal_format": None, "port_direction": "input"}]
        result = check_avixa_checkpoint_conformance(device_caps, port_caps)
        assert result["passed"] is False
        assert any("signal_format" in r for r in result["reasons"])

    def test_port_invalid_direction_fails(self) -> None:
        device_caps = [{"node_label": "DEVICE:D1", "device_category": "AV"}]
        port_caps = [
            {"node_label": "PORT:D1:P1", "signal_format": "HDMI", "port_direction": "sideways"}
        ]
        result = check_avixa_checkpoint_conformance(device_caps, port_caps)
        assert result["passed"] is False
        assert any("port_direction" in r for r in result["reasons"])

    def test_empty_lists_passes(self) -> None:
        result = check_avixa_checkpoint_conformance([], [])
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# Phase-1 contract guard — proves enrich-not-rewrite (no DB needed)
# ---------------------------------------------------------------------------


class TestPhase1ContractIntact:
    """Guard that the Phase-1 do_validate_design contract is unchanged."""

    def test_phase1_do_validate_design_importable(self) -> None:
        """do_validate_design is importable and has the expected signature."""
        import inspect

        from nce.vertical_modules.system_design.validate import do_validate_design

        sig = inspect.signature(do_validate_design)
        params = list(sig.parameters.keys())
        assert "engine" in params
        assert "params" in params

    def test_phase1_validate_decisions_logic(self) -> None:
        """_validate_decisions raises on missing verdict (propose-only invariant)."""
        from nce.vertical_modules.system_design.validate import _validate_decisions

        errors = _validate_decisions([{"line_id": "L1", "verdict": "accept"}])
        assert errors == []

        errors = _validate_decisions([{"line_id": "L1", "verdict": ""}])
        assert len(errors) == 1
        assert "no auto-accept" in errors[0].lower() or "§9.3" in errors[0]

    def test_phase1_graph_label_helpers_unchanged(self) -> None:
        """Phase-1 label helpers produce the same output as when they were authored."""
        from nce.vertical_modules.system_design.graph import (
            _design_label,
            _design_line_label,
            _fl_label,
        )

        assert _design_label("proj-001") == "DESIGN:PROJ-001"
        assert _design_line_label("proj-001", "DL-A") == "DESIGN_LINE:PROJ-001:DL-A"
        assert _fl_label("acme", "site1") == "FL:ACME:SITE1"

    def test_phase2_devices_module_does_not_import_validate(self) -> None:
        """devices.py must NOT import from validate.py (enrich-not-rewrite)."""
        import ast
        import pathlib

        devices_path = (
            pathlib.Path(__file__).parent.parent
            / "nce"
            / "vertical_modules"
            / "system_design"
            / "devices.py"
        )
        source = devices_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert "validate" not in module, f"devices.py imports from validate: {module}"

    def test_phase2_validation_queries_does_not_import_validate(self) -> None:
        """validation_queries.py must NOT import from validate.py."""
        import ast
        import pathlib

        vq_path = (
            pathlib.Path(__file__).parent.parent
            / "nce"
            / "vertical_modules"
            / "system_design"
            / "validation_queries.py"
        )
        source = vq_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "validate" not in module, (
                    f"validation_queries.py imports from validate: {module}"
                )


# ---------------------------------------------------------------------------
# Integration tests — seeded device graph (signal-flow + format compat)
# ---------------------------------------------------------------------------


class _EngineStub:
    """Minimal engine stub exposing pg_pool (same pattern as Phase-1a tests)."""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool


@pytest.mark.integration
@pytest.mark.asyncio
class TestDeviceTopologyIntegration:
    """Seed a 3-device graph and validate signal-flow continuity + format compat.

    Device graph seeded
    -------------------
    LAPTOP (HDMI 2.1 output)
      -> SWITCH (HDMI 2.1 input + HDMI 2.1 output) [mounted in RACK-A]
        -> DISPLAY (HDMI 2.0 input)

    All connections are compatible (HDMI 2.1 → HDMI 2.1 or 2.0).
    All input ports are connected — signal_flow_continuity passes.
    All devices have device_category; all ports have signal_format.
    """

    _NS_SLUG = "w12-inttest"
    _DESIGN_ID = "DESIGN-W12-INTTEST-001"

    _DEVICES = [
        {
            "device_ref": "LAPTOP",
            "capability": {
                "device_category": "Communication Devices",
                "manufacturer": "Lenovo",
                "model_number": "ThinkPad-X1",
                "power_draw_watts": 65.0,
                "heat_btu_hr": 221.7,
                "redundancy_role": "standalone",
            },
            "ports": [
                {
                    "port_ref": "HDMI_OUT",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "output",
                    },
                }
            ],
            "rack_ref": None,
        },
        {
            "device_ref": "SWITCH",
            "capability": {
                "device_category": "AV Switchers",
                "manufacturer": "Extron",
                "model_number": "SW-4-HDMI",
                "power_draw_watts": 12.0,
                "heat_btu_hr": 41.0,
                "redundancy_role": "standalone",
            },
            "ports": [
                {
                    "port_ref": "HDMI_IN1",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "input",
                    },
                },
                {
                    "port_ref": "HDMI_OUT1",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.1",
                        "port_direction": "output",
                    },
                },
            ],
            "rack_ref": "RACK-A",
        },
        {
            "device_ref": "DISPLAY",
            "capability": {
                "device_category": "Displays",
                "manufacturer": "Samsung",
                "model_number": "QN85B",
                "power_draw_watts": 200.0,
                "heat_btu_hr": 682.4,
                "redundancy_role": "standalone",
            },
            "ports": [
                {
                    "port_ref": "HDMI_IN1",
                    "capability": {
                        "signal_format": "HDMI",
                        "signal_version": "2.0",
                        "port_direction": "input",
                    },
                }
            ],
            "rack_ref": None,
        },
    ]

    _RACKS = [
        {
            "rack_ref": "RACK-A",
            "capability": {"device_category": "Rack Enclosures", "manufacturer": "Middle Atlantic"},
        }
    ]

    _CONNECTIONS = [
        {
            "from_device_ref": "LAPTOP",
            "from_port_ref": "HDMI_OUT",
            "to_device_ref": "SWITCH",
            "to_port_ref": "HDMI_IN1",
            "confidence": 1.0,
        },
        {
            "from_device_ref": "SWITCH",
            "from_port_ref": "HDMI_OUT1",
            "to_device_ref": "DISPLAY",
            "to_port_ref": "HDMI_IN1",
            "confidence": 1.0,
        },
    ]

    async def test_device_topology_and_validation_pass(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Seed device graph and validate — all checks pass for a valid design."""
        from nce.auth import set_namespace_context
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.devices import do_author_device_topology
        from nce.vertical_modules.system_design.graph import do_author_functional_location

        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        # Seed ownership.
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_id)
                await seed_node_ownership_registry(conn, ns_id)

        _MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"
        _MOCK_EMIT_DEV = "nce.vertical_modules.system_design.devices.emit_graph_write"

        # Author a minimal functional location so the DESIGN node exists.
        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug=self._NS_SLUG,
                    design_id=self._DESIGN_ID,
                    site_name="SiteAlpha",
                    buildings=[
                        {
                            "name": "MainBuilding",
                            "floors": [
                                {
                                    "name": "Floor1",
                                    "rooms": [{"name": "ConfRoom101", "positions": ["POS-A"]}],
                                }
                            ],
                        }
                    ],
                )

        # Author device topology.
        with patch(_MOCK_EMIT_DEV, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                topology_result = await do_author_device_topology(
                    conn,
                    ns_id,
                    design_id=self._DESIGN_ID,
                    devices=self._DEVICES,
                    connections=self._CONNECTIONS,
                    racks=self._RACKS,
                )

        assert topology_result["authored"]["nodes"] >= 3
        assert topology_result["authored"]["edges"] >= 2
        assert topology_result["authored"]["capabilities"] >= 3

        # Validate — all checks should pass for this well-formed design.
        result = await validate_design_graph(
            engine,
            {"namespace_id": ns_id, "design_id": self._DESIGN_ID},
        )

        assert "passed" in result
        assert isinstance(result["passed"], bool)
        assert "reasons" in result
        assert isinstance(result["reasons"], list)

        # Check 1: signal-flow continuity — both input ports are connected.
        # Check 2: format compatibility — HDMI 2.1→2.1 and 2.1→2.0 both valid.
        # Collect only failure-shaped reasons (power/heat totals are always present).
        failure_reasons = [r for r in result["reasons"] if not r.startswith("total")]
        assert result["passed"] is True, (
            f"validate_design_graph returned passed=False; reasons: {result['reasons']}"
        )
        assert failure_reasons == [], f"Unexpected failure reasons: {failure_reasons}"

    async def test_signal_flow_fails_when_input_disconnected(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """Signal-flow continuity fails when an input PORT has no inbound connection."""
        from nce.auth import set_namespace_context
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.devices import do_author_device_topology
        from nce.vertical_modules.system_design.graph import do_author_functional_location

        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_id)
                await seed_node_ownership_registry(conn, ns_id)

        _MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"
        _MOCK_EMIT_DEV = "nce.vertical_modules.system_design.devices.emit_graph_write"

        design_id = "DESIGN-W12-DISCONNECTED"

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug="w12-disc",
                    design_id=design_id,
                    site_name="SiteDisconn",
                    buildings=[
                        {
                            "name": "Bld",
                            "floors": [{"name": "F1", "rooms": [{"name": "R1", "positions": []}]}],
                        }
                    ],
                )

        # Add display with input port but NO connection to it.
        devices_no_conn = [
            {
                "device_ref": "DISPLAY",
                "capability": {
                    "device_category": "Displays",
                    "manufacturer": "LG",
                    "model_number": "LG55",
                    "redundancy_role": None,
                },
                "ports": [
                    {
                        "port_ref": "HDMI_IN1",
                        "capability": {
                            "signal_format": "HDMI",
                            "signal_version": "2.0",
                            "port_direction": "input",
                        },
                    }
                ],
            }
        ]

        with patch(_MOCK_EMIT_DEV, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_device_topology(
                    conn,
                    ns_id,
                    design_id=design_id,
                    devices=devices_no_conn,
                    connections=[],  # no connections — DISPLAY:HDMI_IN1 is dangling
                )

        result = await validate_design_graph(
            engine,
            {"namespace_id": ns_id, "design_id": design_id},
        )

        assert result["passed"] is False
        assert any(
            "dangling" in r.lower() or "no inbound" in r.lower() for r in result["reasons"]
        ), f"Expected a dangling-input reason; got: {result['reasons']}"


@pytest.mark.integration
@pytest.mark.asyncio
class TestPhase1ContractUnchanged:
    """Prove enrich-not-rewrite: do_validate_design still works correctly."""

    async def test_do_validate_design_accept_passes(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_validate_design returns passed=True when all decisions are accept."""
        from nce.auth import set_namespace_context
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.graph import do_author_functional_location
        from nce.vertical_modules.system_design.validate import do_validate_design

        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_id)
                await seed_node_ownership_registry(conn, ns_id)

        design_id = "DESIGN-P1-CONTRACT-CHECK"
        _MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug="w12-p1c",
                    design_id=design_id,
                    site_name="SiteContract",
                    buildings=[
                        {
                            "name": "Bld",
                            "floors": [{"name": "F1", "rooms": [{"name": "R1", "positions": []}]}],
                        }
                    ],
                    design_lines=[
                        {
                            "line_ref": "DL-001",
                            "manufacturer": "Biamp",
                            "mfr_part_no": "TesiraFORTE",
                            "confidence": 1.0,
                        }
                    ],
                )

        result = await do_validate_design(
            engine,
            {
                "namespace_id": ns_id,
                "design_id": design_id,
                "decisions": [{"line_id": "DL-001", "verdict": "accept"}],
            },
        )

        assert result["passed"] is True
        assert result["decisions_recorded"] == 1
        assert result["design_version_bumped"] is True
        assert result["reasons"] == []

    async def test_do_validate_design_override_fails(
        self,
        pg_pool: Any,
        make_namespace: Any,
    ) -> None:
        """do_validate_design returns passed=False when a decision is override."""
        from nce.auth import set_namespace_context
        from nce.db_utils import scoped_pg_session
        from nce.entity_resolution.ownership_seed import seed_node_ownership_registry
        from nce.vertical_modules.system_design.graph import do_author_functional_location
        from nce.vertical_modules.system_design.validate import do_validate_design

        ns_id: uuid.UUID = await make_namespace()
        engine = _EngineStub(pg_pool)

        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await set_namespace_context(conn, ns_id)
                await seed_node_ownership_registry(conn, ns_id)

        design_id = "DESIGN-P1-OVERRIDE-CHECK"
        _MOCK_EMIT = "nce.vertical_modules.system_design.graph.emit_graph_write"

        with patch(_MOCK_EMIT, new_callable=AsyncMock):
            async with scoped_pg_session(pg_pool, ns_id) as conn:
                await do_author_functional_location(
                    conn,
                    ns_id,
                    namespace_slug="w12-p1o",
                    design_id=design_id,
                    site_name="SiteOverride",
                    buildings=[
                        {
                            "name": "Bld",
                            "floors": [{"name": "F1", "rooms": [{"name": "R1", "positions": []}]}],
                        }
                    ],
                )

        result = await do_validate_design(
            engine,
            {
                "namespace_id": ns_id,
                "design_id": design_id,
                "decisions": [
                    {"line_id": "DL-001", "verdict": "override", "reason": "wrong product"}
                ],
            },
        )

        assert result["passed"] is False
        assert len(result["reasons"]) == 1
        assert "wrong product" in result["reasons"][0]
