"""
tests/test_system_design_signal_flow.py
=======================================
Unit and integration tests for Wave SD-5 / Copper Contract-I:
Thin signal-flow layer over DEVICE/PORT/CABLE + system_design_device_capabilities.

Test Matrix
-----------
1. Pure function tests (unit, no DB):
   - Linear signal chain traversal (upstream / downstream / hop counts / cumulative lengths)
   - Format mismatch detection across chain hops (HDMI 2.0 -> HDMI 2.1)
   - Cycle detection and depth-bounded termination
   - Multi-hop branching and fan-out
   - Port-level signal inspection (capabilities, parent device, attached cables, dangling input)
   - Device-level signal inspection (port groupings, power/heat sums, peer devices)
   - Cable-level signal inspection (endpoints, active transmission direction, geometry properties)
   - Design-level signal inspection (all chains, format summary, dangling inputs)

2. Integration tests (live Postgres, @pytest.mark.integration):
   - Database roundtrip: author topology -> write geometry -> inspect signal flow
   - MCP dispatch: execute_call_tool("system_design_inspect_signal_flow", ...)
   - REST handler: api_system_design_inspect_signal_flow GET route
   - Owner-pool tenant isolation: colliding labels across namespaces isolated by SQL predicate
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nce.vertical_modules.system_design.signal_flow import (
    do_inspect_signal_flow,
    inspect_cable_signal_flow,
    inspect_design_signal_flow,
    inspect_device_signal_flow,
    inspect_port_signal_flow,
    trace_signal_chain,
)

# ===========================================================================
# 1. Pure Unit Tests
# ===========================================================================


class TestPureSignalFlowTraversal:
    """Unit tests for trace_signal_chain and pure inspection helpers."""

    def test_trace_signal_chain_linear_downstream_and_upstream(self) -> None:
        """Linear chain: Source -> Switch -> Display with cable lengths."""
        # Topology:
        # P_SRC_OUT (HDMI 2.1) -[connected_to]-> P_SW_IN (HDMI 2.1) via CABLE:1 (10m)
        # P_SW_OUT (HDMI 2.0)  -[connected_to]-> P_DISP_IN (HDMI 2.0) via CABLE:2 (15m)
        connections = [
            {"from_port": "PORT:SRC_OUT", "to_port": "PORT:SW_IN"},
            {"from_port": "PORT:SW_OUT", "to_port": "PORT:DISP_IN"},
        ]
        port_caps = {
            "PORT:SRC_OUT": {
                "signal_format": "HDMI",
                "signal_version": "2.1",
                "port_direction": "output",
            },
            "PORT:SW_IN": {
                "signal_format": "HDMI",
                "signal_version": "2.1",
                "port_direction": "input",
            },
            "PORT:SW_OUT": {
                "signal_format": "HDMI",
                "signal_version": "2.0",
                "port_direction": "output",
            },
            "PORT:DISP_IN": {
                "signal_format": "HDMI",
                "signal_version": "2.0",
                "port_direction": "input",
            },
        }
        port_to_device = {
            "PORT:SRC_OUT": "DEVICE:SRC",
            "PORT:SW_IN": "DEVICE:SW",
            "PORT:SW_OUT": "DEVICE:SW",
            "PORT:DISP_IN": "DEVICE:DISP",
        }
        device_nodes = {
            "DEVICE:SRC": {"label": "DEVICE:SRC", "properties": {"name": "Media Player"}},
            "DEVICE:SW": {"label": "DEVICE:SW", "properties": {"name": "Video Switcher"}},
            "DEVICE:DISP": {"label": "DEVICE:DISP", "properties": {"name": "Main Display"}},
        }
        cable_by_ports = {
            frozenset({"PORT:SRC_OUT", "PORT:SW_IN"}): "CABLE:1",
            frozenset({"PORT:SW_OUT", "PORT:DISP_IN"}): "CABLE:2",
        }
        geometry = {
            "CABLE:1": {"cable_length_m": 10.0, "cable_type": "HDMI-FIBER"},
            "CABLE:2": {"cable_length_m": 15.0, "cable_type": "HDMI-COPPER"},
        }

        # Downstream trace from SRC_OUT
        fwd_res = trace_signal_chain(
            start_port="PORT:SRC_OUT",
            connections=connections,
            port_caps=port_caps,
            port_to_device=port_to_device,
            device_nodes=device_nodes,
            cable_by_ports=cable_by_ports,
            geometry=geometry,
            direction="downstream",
        )
        assert not fwd_res["has_cycle"]
        assert len(fwd_res["downstream"]) == 1
        hop1 = fwd_res["downstream"][0]
        assert hop1["hop"] == 1
        assert hop1["port_label"] == "PORT:SW_IN"
        assert hop1["device_label"] == "DEVICE:SW"
        assert hop1["cable_label"] == "CABLE:1"
        assert hop1["cable_length_m"] == 10.0
        assert hop1["cable_type"] == "HDMI-FIBER"
        assert hop1["is_compatible"] is True
        assert fwd_res["total_downstream_length_m"] == 10.0

        # Upstream trace from DISP_IN
        bwd_res = trace_signal_chain(
            start_port="PORT:DISP_IN",
            connections=connections,
            port_caps=port_caps,
            port_to_device=port_to_device,
            device_nodes=device_nodes,
            cable_by_ports=cable_by_ports,
            geometry=geometry,
            direction="upstream",
        )
        assert not bwd_res["has_cycle"]
        assert len(bwd_res["upstream"]) == 1
        up_hop = bwd_res["upstream"][0]
        assert up_hop["port_label"] == "PORT:SW_OUT"
        assert up_hop["device_label"] == "DEVICE:SW"
        assert up_hop["cable_label"] == "CABLE:2"
        assert up_hop["cable_length_m"] == 15.0
        assert up_hop["is_compatible"] is True
        assert bwd_res["total_upstream_length_m"] == 15.0

    def test_trace_signal_chain_format_mismatch(self) -> None:
        """HDMI 2.0 source driving HDMI 2.1 sink is flagged incompatible."""
        connections = [{"from_port": "PORT:SRC", "to_port": "PORT:SINK"}]
        port_caps = {
            "PORT:SRC": {
                "signal_format": "HDMI",
                "signal_version": "2.0",
                "port_direction": "output",
            },
            "PORT:SINK": {
                "signal_format": "HDMI",
                "signal_version": "2.1",
                "port_direction": "input",
            },
        }
        res = trace_signal_chain(
            start_port="PORT:SRC",
            connections=connections,
            port_caps=port_caps,
            port_to_device={"PORT:SRC": "D:1", "PORT:SINK": "D:2"},
            device_nodes={"D:1": {}, "D:2": {}},
            cable_by_ports={},
            geometry={},
            direction="downstream",
        )
        assert len(res["downstream"]) == 1
        hop = res["downstream"][0]
        assert hop["is_compatible"] is False
        assert "version mismatch" in hop["compatibility_reason"]

    def test_trace_signal_chain_cycle_detection(self) -> None:
        """Cyclic connection A -> B -> C -> A must terminate and flag has_cycle."""
        connections = [
            {"from_port": "PORT:A", "to_port": "PORT:B"},
            {"from_port": "PORT:B", "to_port": "PORT:C"},
            {"from_port": "PORT:C", "to_port": "PORT:A"},
        ]
        port_caps = {p: {"signal_format": "Dante"} for p in ("PORT:A", "PORT:B", "PORT:C")}
        res = trace_signal_chain(
            start_port="PORT:A",
            connections=connections,
            port_caps=port_caps,
            port_to_device={p: "D" for p in port_caps},
            device_nodes={"D": {}},
            cable_by_ports={},
            geometry={},
            direction="downstream",
            max_depth=10,
        )
        assert res["has_cycle"] is True
        # Visited A -> visits B -> visits C -> sees A (cycle), terminates.
        assert len(res["downstream"]) == 2

    def test_inspect_port_signal_flow(self) -> None:
        """inspect_port_signal_flow reflects capabilities, parent device, connections, dangling."""
        port_caps = {
            "PORT:P1": {
                "signal_format": "HDMI",
                "signal_version": "2.1",
                "port_direction": "input",
                "poe_class": 3,
                "poe_watts": 15.4,
            },
            "PORT:P0": {
                "signal_format": "HDMI",
                "signal_version": "2.1",
                "port_direction": "output",
            },
        }
        port_nodes = {
            "PORT:P1": {"label": "PORT:P1", "entity_type": "PORT"},
            "PORT:P0": {"label": "PORT:P0", "entity_type": "PORT"},
        }
        port_to_device = {"PORT:P1": "DEVICE:DEV1", "PORT:P0": "DEVICE:DEV0"}
        device_nodes = {
            "DEVICE:DEV1": {"label": "DEVICE:DEV1", "properties": {"name": "Codec"}},
            "DEVICE:DEV0": {"label": "DEVICE:DEV0", "properties": {"name": "Camera"}},
        }
        device_caps = {
            "DEVICE:DEV1": {"manufacturer": "Cisco", "model_number": "RoomKit"},
            "DEVICE:DEV0": {"manufacturer": "Sony", "model_number": "BRC-X400"},
        }
        connections = [{"from_port": "PORT:P0", "to_port": "PORT:P1"}]
        cable_by_ports = {frozenset({"PORT:P0", "PORT:P1"}): "CABLE:CAM_HD"}
        geometry = {"CABLE:CAM_HD": {"cable_length_m": 8.5, "cable_type": "CAT6A"}}

        res = inspect_port_signal_flow(
            port_label="PORT:P1",
            port_caps=port_caps,
            port_nodes=port_nodes,
            port_to_device=port_to_device,
            device_nodes=device_nodes,
            device_caps=device_caps,
            connections=connections,
            cable_by_ports=cable_by_ports,
            geometry=geometry,
        )
        assert res["target_type"] == "PORT"
        assert res["port_label"] == "PORT:P1"
        assert res["capabilities"]["signal_format"] == "HDMI"
        assert res["parent_device"]["device_label"] == "DEVICE:DEV1"
        assert res["parent_device"]["capabilities"]["manufacturer"] == "Cisco"
        assert res["attached_cables"] == ["CABLE:CAM_HD"]
        assert len(res["inbound_connections"]) == 1
        assert res["inbound_connections"][0]["from_port"] == "PORT:P0"
        assert res["inbound_connections"][0]["is_compatible"] is True
        assert res["is_dangling_input"] is False

    def test_inspect_port_signal_flow_dangling_input(self) -> None:
        """Input port with no inbound connections is marked dangling."""
        port_caps = {"PORT:P_UNCONN": {"port_direction": "input", "signal_format": "SDI"}}
        port_nodes = {"PORT:P_UNCONN": {"label": "PORT:P_UNCONN"}}
        res = inspect_port_signal_flow(
            port_label="PORT:P_UNCONN",
            port_caps=port_caps,
            port_nodes=port_nodes,
            port_to_device={},
            device_nodes={},
            device_caps={},
            connections=[],
            cable_by_ports={},
            geometry={},
        )
        assert res["is_dangling_input"] is True

    def test_inspect_device_signal_flow(self) -> None:
        """inspect_device_signal_flow aggregates ports, power, heat, peer devices."""
        device_label = "DEVICE:DSP"
        device_nodes = {"DEVICE:DSP": {"label": "DEVICE:DSP", "properties": {"name": "Core 110f"}}}
        device_caps = {
            "DEVICE:DSP": {
                "power_draw_watts": 65.0,
                "heat_btu_hr": 220.0,
                "redundancy_role": "primary",
                "manufacturer": "QSC",
            }
        }
        device_to_ports = {"DEVICE:DSP": ["PORT:DSP_IN1", "PORT:DSP_OUT1", "PORT:DSP_IN_DANGLING"]}
        port_caps = {
            "PORT:DSP_IN1": {"port_direction": "input", "signal_format": "Dante"},
            "PORT:DSP_OUT1": {"port_direction": "output", "signal_format": "Dante"},
            "PORT:DSP_IN_DANGLING": {"port_direction": "input", "signal_format": "Analog"},
        }
        port_nodes = {p: {"label": p} for p in port_caps}
        connections = [
            {"from_port": "PORT:MIC_OUT", "to_port": "PORT:DSP_IN1"},
            {"from_port": "PORT:DSP_OUT1", "to_port": "PORT:AMP_IN"},
        ]
        port_to_device = {
            "PORT:MIC_OUT": "DEVICE:MIC",
            "PORT:DSP_IN1": "DEVICE:DSP",
            "PORT:DSP_OUT1": "DEVICE:DSP",
            "PORT:DSP_IN_DANGLING": "DEVICE:DSP",
            "PORT:AMP_IN": "DEVICE:AMP",
        }

        res = inspect_device_signal_flow(
            device_label=device_label,
            device_nodes=device_nodes,
            device_caps=device_caps,
            device_to_ports=device_to_ports,
            port_caps=port_caps,
            port_nodes=port_nodes,
            connections=connections,
            cable_by_ports={},
            port_to_device=port_to_device,
        )
        assert res["target_type"] == "DEVICE"
        assert res["device_label"] == device_label
        summary = res["summary"]
        assert summary["total_ports"] == 3
        assert summary["input_ports"] == 2
        assert summary["output_ports"] == 1
        assert summary["dangling_inputs"] == ["PORT:DSP_IN_DANGLING"]
        assert summary["upstream_devices"] == ["DEVICE:MIC"]
        assert summary["downstream_devices"] == ["DEVICE:AMP"]
        assert summary["power_draw_watts"] == 65.0
        assert summary["heat_btu_hr"] == 220.0

    def test_inspect_cable_signal_flow(self) -> None:
        """inspect_cable_signal_flow inspects endpoints, active transmission, and properties."""
        cable_label = "CABLE:SPK1"
        cable_nodes = {"CABLE:SPK1": {"label": "CABLE:SPK1", "entity_type": "CABLE"}}
        cable_to_ports = {"CABLE:SPK1": ["PORT:AMP_CH1", "PORT:SPK_IN"]}
        port_caps = {
            "PORT:AMP_CH1": {"signal_format": "Speaker", "port_direction": "output"},
            "PORT:SPK_IN": {"signal_format": "Speaker", "port_direction": "input"},
        }
        port_to_device = {
            "PORT:AMP_CH1": "DEVICE:AMP",
            "PORT:SPK_IN": "DEVICE:SPK",
        }
        device_nodes = {
            "DEVICE:AMP": {"label": "DEVICE:AMP", "properties": {"name": "Power Amp"}},
            "DEVICE:SPK": {"label": "DEVICE:SPK", "properties": {"name": "Ceiling Speaker"}},
        }
        connections = [{"from_port": "PORT:AMP_CH1", "to_port": "PORT:SPK_IN"}]
        geometry = {"CABLE:SPK1": {"cable_length_m": 35.0, "cable_type": "2x2.5mm2"}}
        node_state = {"CABLE:SPK1": {"status": "planned"}}

        res = inspect_cable_signal_flow(
            cable_label=cable_label,
            cable_nodes=cable_nodes,
            cable_to_ports=cable_to_ports,
            port_caps=port_caps,
            port_to_device=port_to_device,
            device_nodes=device_nodes,
            connections=connections,
            geometry=geometry,
            node_state=node_state,
        )
        assert res["target_type"] == "CABLE"
        assert res["cable_label"] == cable_label
        assert res["cable_length_m"] == 35.0
        assert res["cable_type"] == "2x2.5mm2"
        assert res["status"] == "planned"
        assert len(res["endpoints"]) == 2
        assert len(res["active_signals"]) == 1
        sig = res["active_signals"][0]
        assert sig["from_port"] == "PORT:AMP_CH1"
        assert sig["to_port"] == "PORT:SPK_IN"
        assert sig["is_compatible"] is True

    def test_inspect_design_signal_flow(self) -> None:
        """inspect_design_signal_flow discovers root sources, chains, and system summary."""
        design_label = "DESIGN:ROOM_A"
        device_nodes = {
            "DEVICE:CAM": {"label": "DEVICE:CAM"},
            "DEVICE:CODEC": {"label": "DEVICE:CODEC"},
        }
        device_caps = {
            "DEVICE:CAM": {"power_draw_watts": 12.0, "heat_btu_hr": 40.0},
            "DEVICE:CODEC": {"power_draw_watts": 48.0, "heat_btu_hr": 160.0},
        }
        port_nodes = {
            "PORT:CAM_OUT": {"label": "PORT:CAM_OUT"},
            "PORT:CODEC_IN": {"label": "PORT:CODEC_IN"},
            "PORT:CODEC_HDMI2": {"label": "PORT:CODEC_HDMI2"},
        }
        port_caps = {
            "PORT:CAM_OUT": {
                "port_direction": "output",
                "signal_format": "HDMI",
                "signal_version": "2.0",
            },
            "PORT:CODEC_IN": {
                "port_direction": "input",
                "signal_format": "HDMI",
                "signal_version": "2.0",
            },
            "PORT:CODEC_HDMI2": {
                "port_direction": "input",
                "signal_format": "HDMI",
                "signal_version": "2.0",
            },
        }
        cable_nodes = {"CABLE:1": {"label": "CABLE:1"}}
        device_to_ports = {
            "DEVICE:CAM": ["PORT:CAM_OUT"],
            "DEVICE:CODEC": ["PORT:CODEC_IN", "PORT:CODEC_HDMI2"],
        }
        port_to_device = {
            "PORT:CAM_OUT": "DEVICE:CAM",
            "PORT:CODEC_IN": "DEVICE:CODEC",
            "PORT:CODEC_HDMI2": "DEVICE:CODEC",
        }
        cable_by_ports = {frozenset({"PORT:CAM_OUT", "PORT:CODEC_IN"}): "CABLE:1"}
        cable_to_ports = {"CABLE:1": ["PORT:CAM_OUT", "PORT:CODEC_IN"]}
        connections = [{"from_port": "PORT:CAM_OUT", "to_port": "PORT:CODEC_IN"}]
        geometry = {"CABLE:1": {"cable_length_m": 5.0, "cable_type": "HDMI"}}

        res = inspect_design_signal_flow(
            design_label=design_label,
            device_nodes=device_nodes,
            device_caps=device_caps,
            port_nodes=port_nodes,
            port_caps=port_caps,
            cable_nodes=cable_nodes,
            device_to_ports=device_to_ports,
            port_to_device=port_to_device,
            cable_by_ports=cable_by_ports,
            cable_to_ports=cable_to_ports,
            connections=connections,
            geometry=geometry,
            node_state={},
        )
        assert res["target_type"] == "DESIGN"
        assert res["summary"]["total_devices"] == 2
        assert res["summary"]["total_ports"] == 3
        assert res["summary"]["dangling_inputs"] == ["PORT:CODEC_HDMI2"]
        assert res["summary"]["total_power_draw_watts"] == 60.0
        assert res["summary"]["total_heat_btu_hr"] == 200.0
        assert len(res["signal_chains"]) == 1
        chain = res["signal_chains"][0]
        assert chain["source_port"] == "PORT:CAM_OUT"
        assert chain["source_device"] == "DEVICE:CAM"
        assert len(chain["downstream_hops"]) == 1


# ===========================================================================
# 2. Integration Tests (Live PostgreSQL)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
class TestSignalFlowIntegration:
    """Integration tests running against the live PostgreSQL instance."""

    async def test_signal_flow_db_roundtrip(self, pg_pool: Any, make_namespace: Any) -> None:
        """Author topology + geometry -> inspect signal flow over DB."""
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.devices import (
            cable_label,
            device_label,
            do_author_device_topology,
            port_label,
        )
        from nce.vertical_modules.system_design.geometry import upsert_node_geometry
        from nce.vertical_modules.system_design.graph import _design_label

        test_ns = await make_namespace()
        design_id = f"des_{test_ns.hex[:8]}"
        design_lbl = _design_label(design_id)

        dev_mp = device_label(design_id, "MP")
        dev_tv = device_label(design_id, "TV")
        port_mp = port_label(design_id, "MP", "HDMI")
        port_tv = port_label(design_id, "TV", "HDMI")
        cbl = cable_label(design_id, "RUN")

        # Seed DESIGN node in kg_nodes
        async with scoped_pg_session(pg_pool, test_ns) as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'DESIGN', $2)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                design_lbl,
                test_ns,
            )

            # Author device topology: Media Player -> Display with cable
            devices_payload = [
                {
                    "device_ref": "MP",
                    "capability": {
                        "device_category": "Media Player",
                        "manufacturer": "Apple",
                        "model_number": "AppleTV 4K",
                        "power_draw_watts": 15.0,
                        "heat_btu_hr": 50.0,
                    },
                    "ports": [
                        {
                            "port_ref": "HDMI",
                            "capability": {
                                "signal_format": "HDMI",
                                "signal_version": "2.1",
                                "port_direction": "output",
                            },
                        }
                    ],
                },
                {
                    "device_ref": "TV",
                    "capability": {
                        "device_category": "Flat Panel",
                        "manufacturer": "Samsung",
                        "model_number": "QM85R",
                        "power_draw_watts": 280.0,
                        "heat_btu_hr": 950.0,
                    },
                    "ports": [
                        {
                            "port_ref": "HDMI",
                            "capability": {
                                "signal_format": "HDMI",
                                "signal_version": "2.0",
                                "port_direction": "input",
                            },
                        }
                    ],
                },
            ]
            connections_payload = [
                {
                    "from_device_ref": "MP",
                    "from_port_ref": "HDMI",
                    "to_device_ref": "TV",
                    "to_port_ref": "HDMI",
                    "cable_ref": "RUN",
                }
            ]

            await do_author_device_topology(
                conn,
                test_ns,
                design_id=design_id,
                devices=devices_payload,
                connections=connections_payload,
            )

            # Author geometry for cable
            await upsert_node_geometry(
                conn,
                test_ns,
                cbl,
                {
                    "cable_length_m": 12.5,
                    "cable_type": "HDMI-ACTIVE",
                },
            )

        class FakeEngine:
            def __init__(self, p):
                self.pg_pool = p

        engine = FakeEngine(pg_pool)

        # 1. Whole design inspection
        res_design = await do_inspect_signal_flow(
            engine,
            {"namespace_id": str(test_ns), "design_id": design_id},
        )
        assert res_design["target_type"] == "DESIGN"
        assert res_design["summary"]["total_devices"] == 2
        assert res_design["summary"]["total_ports"] == 2
        assert res_design["summary"]["total_cables"] == 1
        assert res_design["summary"]["total_power_draw_watts"] == 295.0
        assert len(res_design["signal_chains"]) == 1
        assert res_design["signal_chains"][0]["total_length_m"] == 12.5

        # 2. Port inspection on TV_HDMI
        res_port = await do_inspect_signal_flow(
            engine,
            {
                "namespace_id": str(test_ns),
                "design_id": design_id,
                "node_label": port_tv,
            },
        )
        assert res_port["target_type"] == "PORT"
        assert res_port["port_label"] == port_tv
        assert res_port["parent_device"]["device_label"] == dev_tv
        assert len(res_port["inbound_connections"]) == 1
        assert res_port["inbound_connections"][0]["from_port"] == port_mp
        assert (
            res_port["inbound_connections"][0]["is_compatible"] is True
        )  # HDMI 2.1 source drives HDMI 2.0 sink
        assert res_port["is_dangling_input"] is False

        # 3. Device inspection on MP
        res_dev = await do_inspect_signal_flow(
            engine,
            {
                "namespace_id": str(test_ns),
                "design_id": design_id,
                "node_label": dev_mp,
            },
        )
        assert res_dev["target_type"] == "DEVICE"
        assert res_dev["summary"]["total_ports"] == 1
        assert res_dev["summary"]["output_ports"] == 1
        assert res_dev["summary"]["downstream_devices"] == [dev_tv]

        # 4. Cable inspection
        res_cable = await do_inspect_signal_flow(
            engine,
            {
                "namespace_id": str(test_ns),
                "design_id": design_id,
                "node_label": cbl,
            },
        )
        assert res_cable["target_type"] == "CABLE"
        assert res_cable["cable_length_m"] == 12.5
        assert res_cable["cable_type"] == "HDMI-ACTIVE"
        assert len(res_cable["endpoints"]) == 2
        assert len(res_cable["active_signals"]) == 1

    async def test_signal_flow_mcp_dispatch(self, pg_pool: Any, make_namespace: Any) -> None:
        """Call system_design_inspect_signal_flow through handle_system_design_inspect_signal_flow."""
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.graph import _design_label
        from nce.vertical_modules.system_design.mcp_handlers import (
            handle_system_design_inspect_signal_flow,
        )

        test_ns = await make_namespace()
        design_id = f"des_mcp_{test_ns.hex[:8]}"
        design_lbl = _design_label(design_id)

        async with scoped_pg_session(pg_pool, test_ns) as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'DESIGN', $2)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                design_lbl,
                test_ns,
            )

        class FakeEngine:
            def __init__(self, p):
                self.pg_pool = p

        engine = FakeEngine(pg_pool)

        output_json = await handle_system_design_inspect_signal_flow(
            engine,
            {"namespace_id": str(test_ns), "design_id": design_id},
        )
        data = json.loads(output_json)
        assert data["target_type"] == "DESIGN"
        assert data["design_label"] == design_lbl

    async def test_signal_flow_rest_route(self, pg_pool: Any, make_namespace: Any) -> None:
        """Verify REST api_system_design_inspect_signal_flow returns 200 JSON."""
        from nce.admin_handlers._shared import admin_state
        from nce.admin_handlers.system_design import api_system_design_inspect_signal_flow
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.graph import _design_label

        test_ns = await make_namespace()
        design_id = f"des_rest_{test_ns.hex[:8]}"
        design_lbl = _design_label(design_id)

        async with scoped_pg_session(pg_pool, test_ns) as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'DESIGN', $2)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                design_lbl,
                test_ns,
            )

        class FakeEngine:
            def __init__(self, p):
                self.pg_pool = p

        admin_state.engine = FakeEngine(pg_pool)

        class FakeRequest:
            def __init__(self, q):
                self.query_params = q

        req = FakeRequest(
            {
                "namespace_id": str(test_ns),
                "design_id": design_id,
            }
        )

        resp = await api_system_design_inspect_signal_flow(req)
        assert resp.status_code == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body["status"] == "ok"
        assert body["signal_flow"]["target_type"] == "DESIGN"

    async def test_owner_pool_tenant_isolation(self, pg_pool: Any, make_namespace: Any) -> None:
        """Colliding labels across two namespaces stay isolated by SQL predicate."""
        from nce.db_utils import scoped_pg_session
        from nce.vertical_modules.system_design.devices import (
            device_label,
            do_author_device_topology,
        )
        from nce.vertical_modules.system_design.graph import _design_label

        ns_a = await make_namespace()
        ns_b = await make_namespace()

        design_id = "COLLIDING_DESIGN"
        design_lbl = _design_label(design_id)

        async with scoped_pg_session(pg_pool, ns_a) as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'DESIGN', $2)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                design_lbl,
                ns_a,
            )
            # Namespace A has 1 device with 1 port
            await do_author_device_topology(
                conn,
                ns_a,
                design_id=design_id,
                devices=[
                    {
                        "device_ref": "SHARED",
                        "capability": {"manufacturer": "TenantA_Mfg"},
                        "ports": [{"port_ref": "P1"}],
                    }
                ],
            )

        async with scoped_pg_session(pg_pool, ns_b) as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id)
                VALUES ($1, 'DESIGN', $2)
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                design_lbl,
                ns_b,
            )
            # Namespace B has 2 devices with 2 ports
            await do_author_device_topology(
                conn,
                ns_b,
                design_id=design_id,
                devices=[
                    {
                        "device_ref": "SHARED",
                        "capability": {"manufacturer": "TenantB_Mfg"},
                        "ports": [{"port_ref": "P1"}],
                    },
                    {
                        "device_ref": "EXTRA_B",
                        "capability": {"manufacturer": "TenantB_Mfg2"},
                        "ports": [{"port_ref": "P2"}],
                    },
                ],
            )

        class FakeEngine:
            def __init__(self, p):
                self.pg_pool = p

        engine = FakeEngine(pg_pool)
        dev_shared = device_label(design_id, "SHARED")

        # Inspect Tenant A
        res_a = await do_inspect_signal_flow(
            engine,
            {"namespace_id": str(ns_a), "design_id": design_id},
        )
        assert res_a["summary"]["total_devices"] == 1
        assert res_a["summary"]["total_ports"] == 1

        dev_a = await do_inspect_signal_flow(
            engine,
            {"namespace_id": str(ns_a), "design_id": design_id, "node_label": dev_shared},
        )
        assert dev_a["capabilities"]["manufacturer"] == "TenantA_Mfg"

        # Inspect Tenant B
        res_b = await do_inspect_signal_flow(
            engine,
            {"namespace_id": str(ns_b), "design_id": design_id},
        )
        assert res_b["summary"]["total_devices"] == 2
        assert res_b["summary"]["total_ports"] == 2

        dev_b = await do_inspect_signal_flow(
            engine,
            {"namespace_id": str(ns_b), "design_id": design_id, "node_label": dev_shared},
        )
        assert dev_b["capabilities"]["manufacturer"] == "TenantB_Mfg"
