"""
nce/vertical_modules/system_design/signal_flow.py
=================================================
Thin signal-flow inspection and traversal layer for the System Design vertical
module (Wave SD-5 / Copper Contract-I).

Responsibilities
----------------
* Interpret the graph of DEVICE, PORT, and CABLE nodes alongside
  ``system_design_device_capabilities`` and ``system_design_geometry``.
* Serve Copper's canvas inspectors (NodeInspector / EdgeInspector / PathInspector)
  under Contract-I (intelligence integrated directly into inspector surfaces).
* Pure, deterministic graph traversal functions over pre-fetched structures:
  - ``trace_signal_chain``: Walk ``connected_to`` edges upstream (to root sources)
    and downstream (to leaf sinks) tracking hops, devices, interconnecting cables,
    format conversions, and cumulative cable lengths.
  - ``inspect_port_signal_flow``: Inspect a single PORT's AVIXA capabilities,
    owning DEVICE, connected CABLEs, direct peer connections, format
    compatibility, and end-to-end signal chain.
  - ``inspect_device_signal_flow``: Inspect a DEVICE's power/thermal budgets,
    ports grouped by direction, active connections, dangling inputs, and
    upstream/downstream device neighbors.
  - ``inspect_cable_signal_flow``: Inspect a CABLE's physical properties
    (length, type), terminating ports, owning devices, signal transmission
    direction, and format compatibility.
  - ``inspect_design_signal_flow``: Inspect whole-design signal flow, all end-to-end
    signal paths, format distribution, continuity issues, and system totals.
* Public async domain core:
  - ``do_inspect_signal_flow(engine, params)``: Tenant-isolated query layer
    enforcing explicit SQL ``namespace_id = $n::uuid`` predicates across all
    queried tables.

Design invariants (uncle-bob-craft)
------------------------------------
- Pure functions depend only on plain dicts/lists/sets — no DB, no I/O, unit-testable.
- Read-only queries: no mutations, no event log writes.
- PROPOSE-ONLY: no auto-fixing.
- Zero financial leaks (ADR-0017): cost, margin, and bid fields are never touched.
- Tenant isolation: every query carries an explicit ``namespace_id = $n::uuid``
  predicate on every joined relation (owner pools bypass FORCE RLS).
- Cycle protection: traversal tracks visited nodes and enforces a maximum depth bound.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.system_design.graph import _design_label
from nce.vertical_modules.system_design.read import (
    _ENTITY_CABLE,
    _ENTITY_DEVICE,
    _ENTITY_PORT,
    _fetch_capabilities_by_labels,
    _fetch_design_scope_labels,
    _fetch_edges_within,
    _fetch_nodes_by_labels,
    _group_nodes_by_type,
)
from nce.vertical_modules.system_design.validation_queries import _formats_compatible

log = logging.getLogger("nce.vertical_modules.system_design.signal_flow")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PRED_CONTAINS = "contains"
_PRED_HAS_PORT = "has_port"
_PRED_CONNECTED_TO = "connected_to"
_PRED_USES_CABLE = "uses_cable"

DEFAULT_MAX_DEPTH = 32
MAX_ALLOWED_DEPTH = 100


# ---------------------------------------------------------------------------
# Pure Traversal Functions
# ---------------------------------------------------------------------------


def trace_signal_chain(
    start_port: str,
    connections: list[dict[str, Any]],
    port_caps: dict[str, dict[str, Any]],
    port_to_device: dict[str, str],
    device_nodes: dict[str, dict[str, Any]],
    cable_by_ports: dict[frozenset[str], str],
    geometry: dict[str, dict[str, Any]],
    direction: str = "both",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Walk connected_to edges upstream and/or downstream from a starting port.

    Parameters
    ----------
    start_port:
        Label of the PORT node to start tracing from.
    connections:
        List of ``{"from_port": str, "to_port": str}`` dicts.
    port_caps:
        Map of ``port_label -> capability dict``.
    port_to_device:
        Map of ``port_label -> device_label``.
    device_nodes:
        Map of ``device_label -> node dict``.
    cable_by_ports:
        Map of ``frozenset({port_a, port_b}) -> cable_label``.
    geometry:
        Map of ``node_label -> geometry dict``.
    direction:
        ``"upstream"``, ``"downstream"``, or ``"both"``.
    max_depth:
        Maximum recursion/hop depth to prevent unbounded cycles.

    Returns
    -------
    dict
        ``{
            "upstream": list[dict],
            "downstream": list[dict],
            "total_upstream_length_m": float,
            "total_downstream_length_m": float,
            "root_sources": list[str],
            "leaf_sinks": list[str],
            "has_cycle": bool,
        }``
    """
    fwd_map: dict[str, list[str]] = {}
    bwd_map: dict[str, list[str]] = {}

    for cnx in connections:
        src = cnx.get("from_port", "")
        dst = cnx.get("to_port", "")
        if src and dst:
            fwd_map.setdefault(src, []).append(dst)
            bwd_map.setdefault(dst, []).append(src)

    has_cycle = False

    def _build_hop_info(
        curr_port: str,
        peer_port: str,
        hop_idx: int,
        is_upstream: bool,
    ) -> dict[str, Any]:
        """Construct hop metadata between curr_port and peer_port."""
        src_port = peer_port if is_upstream else curr_port
        dst_port = curr_port if is_upstream else peer_port

        src_cap = port_caps.get(src_port, {})
        dst_cap = port_caps.get(dst_port, {})

        is_compat, reason = _formats_compatible(
            src_cap.get("signal_format"),
            src_cap.get("signal_version"),
            dst_cap.get("signal_format"),
            dst_cap.get("signal_version"),
        )

        cable_lbl = cable_by_ports.get(frozenset({curr_port, peer_port}))
        cable_geo = geometry.get(cable_lbl, {}) if cable_lbl else {}
        length_val = cable_geo.get("cable_length_m")
        cable_length_m = float(length_val) if length_val is not None else None
        cable_type = cable_geo.get("cable_type")

        dev_lbl = port_to_device.get(peer_port)
        dev_node = device_nodes.get(dev_lbl, {}) if dev_lbl else {}

        return {
            "hop": hop_idx,
            "port_label": peer_port,
            "device_label": dev_lbl,
            "device_name": dev_node.get("properties", {}).get("name") or dev_lbl,
            "cable_label": cable_lbl,
            "cable_length_m": cable_length_m,
            "cable_type": cable_type,
            "signal_format": dst_cap.get("signal_format")
            if is_upstream
            else src_cap.get("signal_format"),
            "signal_version": dst_cap.get("signal_version")
            if is_upstream
            else src_cap.get("signal_version"),
            "port_direction": dst_cap.get("port_direction")
            if is_upstream
            else src_cap.get("port_direction"),
            "is_compatible": is_compat,
            "compatibility_reason": reason,
        }

    upstream_hops: list[dict[str, Any]] = []
    root_sources: list[str] = []
    tot_up_len: float = 0.0

    if direction in ("upstream", "both"):
        visited_up: set[str] = {start_port}
        queue_up: list[tuple[str, int]] = [(start_port, 1)]

        while queue_up:
            curr, depth = queue_up.pop(0)
            if depth > max_depth:
                break
            parents = bwd_map.get(curr, [])
            if not parents and curr != start_port:
                if curr not in root_sources:
                    root_sources.append(curr)

            for parent in parents:
                if parent in visited_up:
                    has_cycle = True
                    continue
                visited_up.add(parent)
                hop_info = _build_hop_info(curr, parent, depth, is_upstream=True)
                upstream_hops.append(hop_info)
                if hop_info["cable_length_m"]:
                    tot_up_len += hop_info["cable_length_m"]
                queue_up.append((parent, depth + 1))

    downstream_hops: list[dict[str, Any]] = []
    leaf_sinks: list[str] = []
    tot_down_len: float = 0.0

    if direction in ("downstream", "both"):
        visited_down: set[str] = {start_port}
        queue_down: list[tuple[str, int]] = [(start_port, 1)]

        while queue_down:
            curr, depth = queue_down.pop(0)
            if depth > max_depth:
                break
            children = fwd_map.get(curr, [])
            if not children and curr != start_port:
                if curr not in leaf_sinks:
                    leaf_sinks.append(curr)

            for child in children:
                if child in visited_down:
                    has_cycle = True
                    continue
                visited_down.add(child)
                hop_info = _build_hop_info(curr, child, depth, is_upstream=False)
                downstream_hops.append(hop_info)
                if hop_info["cable_length_m"]:
                    tot_down_len += hop_info["cable_length_m"]
                queue_down.append((child, depth + 1))

    return {
        "upstream": upstream_hops,
        "downstream": downstream_hops,
        "total_upstream_length_m": round(tot_up_len, 2),
        "total_downstream_length_m": round(tot_down_len, 2),
        "root_sources": root_sources,
        "leaf_sinks": leaf_sinks,
        "has_cycle": has_cycle,
    }


def inspect_port_signal_flow(
    port_label: str,
    port_caps: dict[str, dict[str, Any]],
    port_nodes: dict[str, dict[str, Any]],
    port_to_device: dict[str, str],
    device_nodes: dict[str, dict[str, Any]],
    device_caps: dict[str, dict[str, Any]],
    connections: list[dict[str, Any]],
    cable_by_ports: dict[frozenset[str], str],
    geometry: dict[str, dict[str, Any]],
    direction: str = "both",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Inspect signal flow for a single PORT node."""
    p_node = port_nodes.get(port_label, {})
    p_cap = port_caps.get(port_label, {})

    dev_label = port_to_device.get(port_label)
    d_node = device_nodes.get(dev_label, {}) if dev_label else {}
    d_cap = device_caps.get(dev_label, {}) if dev_label else {}

    attached_cables = [
        cable_lbl for pair, cable_lbl in cable_by_ports.items() if port_label in pair
    ]

    inbound: list[dict[str, Any]] = []
    for cnx in connections:
        if cnx.get("to_port") == port_label:
            src_lbl = cnx.get("from_port", "")
            src_cap = port_caps.get(src_lbl, {})
            src_dev = port_to_device.get(src_lbl)
            src_dev_node = device_nodes.get(src_dev, {}) if src_dev else {}
            cable_lbl = cable_by_ports.get(frozenset({src_lbl, port_label}))

            is_compat, reason = _formats_compatible(
                src_cap.get("signal_format"),
                src_cap.get("signal_version"),
                p_cap.get("signal_format"),
                p_cap.get("signal_version"),
            )
            inbound.append(
                {
                    "from_port": src_lbl,
                    "from_device": src_dev,
                    "from_device_name": src_dev_node.get("properties", {}).get("name") or src_dev,
                    "cable_label": cable_lbl,
                    "signal_format": src_cap.get("signal_format"),
                    "signal_version": src_cap.get("signal_version"),
                    "is_compatible": is_compat,
                    "compatibility_reason": reason,
                }
            )

    outbound: list[dict[str, Any]] = []
    for cnx in connections:
        if cnx.get("from_port") == port_label:
            dst_lbl = cnx.get("to_port", "")
            dst_cap = port_caps.get(dst_lbl, {})
            dst_dev = port_to_device.get(dst_lbl)
            dst_dev_node = device_nodes.get(dst_dev, {}) if dst_dev else {}
            cable_lbl = cable_by_ports.get(frozenset({port_label, dst_lbl}))

            is_compat, reason = _formats_compatible(
                p_cap.get("signal_format"),
                p_cap.get("signal_version"),
                dst_cap.get("signal_format"),
                dst_cap.get("signal_version"),
            )
            outbound.append(
                {
                    "to_port": dst_lbl,
                    "to_device": dst_dev,
                    "to_device_name": dst_dev_node.get("properties", {}).get("name") or dst_dev,
                    "cable_label": cable_lbl,
                    "signal_format": dst_cap.get("signal_format"),
                    "signal_version": dst_cap.get("signal_version"),
                    "is_compatible": is_compat,
                    "compatibility_reason": reason,
                }
            )

    chain = trace_signal_chain(
        start_port=port_label,
        connections=connections,
        port_caps=port_caps,
        port_to_device=port_to_device,
        device_nodes=device_nodes,
        cable_by_ports=cable_by_ports,
        geometry=geometry,
        direction=direction,
        max_depth=max_depth,
    )

    port_dir = p_cap.get("port_direction")
    is_dangling_input = port_dir == "input" and len(inbound) == 0

    return {
        "target_type": "PORT",
        "port_label": port_label,
        "node": p_node,
        "capabilities": p_cap,
        "parent_device": {
            "device_label": dev_label,
            "node": d_node,
            "capabilities": d_cap,
        }
        if dev_label
        else None,
        "attached_cables": sorted(attached_cables),
        "inbound_connections": inbound,
        "outbound_connections": outbound,
        "is_dangling_input": is_dangling_input,
        "chain": chain,
    }


def inspect_device_signal_flow(
    device_label: str,
    device_nodes: dict[str, dict[str, Any]],
    device_caps: dict[str, dict[str, Any]],
    device_to_ports: dict[str, list[str]],
    port_caps: dict[str, dict[str, Any]],
    port_nodes: dict[str, dict[str, Any]],
    connections: list[dict[str, Any]],
    cable_by_ports: dict[frozenset[str], str],
    port_to_device: dict[str, str],
) -> dict[str, Any]:
    """Inspect signal flow for a single DEVICE node."""
    d_node = device_nodes.get(device_label, {})
    d_cap = device_caps.get(device_label, {})
    ports = device_to_ports.get(device_label, [])

    port_summaries: list[dict[str, Any]] = []
    dangling_inputs: list[str] = []
    unconnected_outputs: list[str] = []

    upstream_devices: set[str] = set()
    downstream_devices: set[str] = set()

    inbound_by_port: dict[str, list[dict[str, Any]]] = {}
    outbound_by_port: dict[str, list[dict[str, Any]]] = {}
    for cnx in connections:
        src = cnx.get("from_port", "")
        dst = cnx.get("to_port", "")
        if src:
            outbound_by_port.setdefault(src, []).append(cnx)
        if dst:
            inbound_by_port.setdefault(dst, []).append(cnx)

    for p_lbl in sorted(ports):
        p_cap = port_caps.get(p_lbl, {})
        p_node = port_nodes.get(p_lbl, {})
        p_dir = p_cap.get("port_direction")

        in_edges = inbound_by_port.get(p_lbl, [])
        out_edges = outbound_by_port.get(p_lbl, [])

        if p_dir == "input" and not in_edges:
            dangling_inputs.append(p_lbl)
        if p_dir == "output" and not out_edges:
            unconnected_outputs.append(p_lbl)

        for in_e in in_edges:
            src_dev = port_to_device.get(in_e.get("from_port", ""))
            if src_dev and src_dev != device_label:
                upstream_devices.add(src_dev)

        for out_e in out_edges:
            dst_dev = port_to_device.get(out_e.get("to_port", ""))
            if dst_dev and dst_dev != device_label:
                downstream_devices.add(dst_dev)

        attached_cables = [cable_lbl for pair, cable_lbl in cable_by_ports.items() if p_lbl in pair]

        port_summaries.append(
            {
                "port_label": p_lbl,
                "node": p_node,
                "capabilities": p_cap,
                "port_direction": p_dir,
                "signal_format": p_cap.get("signal_format"),
                "signal_version": p_cap.get("signal_version"),
                "inbound_count": len(in_edges),
                "outbound_count": len(out_edges),
                "attached_cables": sorted(attached_cables),
            }
        )

    power_watts = float(d_cap.get("power_draw_watts") or 0.0)
    heat_btu = float(d_cap.get("heat_btu_hr") or 0.0)

    return {
        "target_type": "DEVICE",
        "device_label": device_label,
        "node": d_node,
        "capabilities": d_cap,
        "ports": port_summaries,
        "summary": {
            "total_ports": len(ports),
            "input_ports": sum(1 for p in port_summaries if p["port_direction"] == "input"),
            "output_ports": sum(1 for p in port_summaries if p["port_direction"] == "output"),
            "bidirectional_ports": sum(
                1 for p in port_summaries if p["port_direction"] == "bidirectional"
            ),
            "dangling_inputs": dangling_inputs,
            "unconnected_outputs": unconnected_outputs,
            "upstream_devices": sorted(upstream_devices),
            "downstream_devices": sorted(downstream_devices),
            "power_draw_watts": power_watts,
            "heat_btu_hr": heat_btu,
            "redundancy_role": d_cap.get("redundancy_role"),
        },
    }


def inspect_cable_signal_flow(
    cable_label: str,
    cable_nodes: dict[str, dict[str, Any]],
    cable_to_ports: dict[str, list[str]],
    port_caps: dict[str, dict[str, Any]],
    port_to_device: dict[str, str],
    device_nodes: dict[str, dict[str, Any]],
    connections: list[dict[str, Any]],
    geometry: dict[str, dict[str, Any]],
    node_state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Inspect signal flow across a CABLE node linking two ports."""
    c_node = cable_nodes.get(cable_label, {})
    ports = cable_to_ports.get(cable_label, [])
    geo = geometry.get(cable_label, {})
    state = node_state.get(cable_label, {})

    length_val = geo.get("cable_length_m")
    cable_length_m = float(length_val) if length_val is not None else None
    cable_type = geo.get("cable_type")
    status = state.get("status")

    endpoints_info: list[dict[str, Any]] = []
    for p_lbl in sorted(ports):
        p_cap = port_caps.get(p_lbl, {})
        d_lbl = port_to_device.get(p_lbl)
        d_node = device_nodes.get(d_lbl, {}) if d_lbl else {}
        endpoints_info.append(
            {
                "port_label": p_lbl,
                "device_label": d_lbl,
                "device_name": d_node.get("properties", {}).get("name") or d_lbl,
                "port_direction": p_cap.get("port_direction"),
                "signal_format": p_cap.get("signal_format"),
                "signal_version": p_cap.get("signal_version"),
                "poe_class": p_cap.get("poe_class"),
                "poe_watts": float(p_cap.get("poe_watts") or 0.0)
                if p_cap.get("poe_watts") is not None
                else None,
            }
        )

    port_set = set(ports)
    active_signals: list[dict[str, Any]] = []
    for cnx in connections:
        src = cnx.get("from_port", "")
        dst = cnx.get("to_port", "")
        if src in port_set and dst in port_set:
            src_cap = port_caps.get(src, {})
            dst_cap = port_caps.get(dst, {})
            is_compat, reason = _formats_compatible(
                src_cap.get("signal_format"),
                src_cap.get("signal_version"),
                dst_cap.get("signal_format"),
                dst_cap.get("signal_version"),
            )
            active_signals.append(
                {
                    "from_port": src,
                    "from_device": port_to_device.get(src),
                    "to_port": dst,
                    "to_device": port_to_device.get(dst),
                    "signal_format": src_cap.get("signal_format"),
                    "signal_version": src_cap.get("signal_version"),
                    "is_compatible": is_compat,
                    "compatibility_reason": reason,
                }
            )

    return {
        "target_type": "CABLE",
        "cable_label": cable_label,
        "node": c_node,
        "cable_length_m": cable_length_m,
        "cable_type": cable_type,
        "status": status,
        "endpoints": endpoints_info,
        "active_signals": active_signals,
    }


def inspect_design_signal_flow(
    design_label: str,
    device_nodes: dict[str, dict[str, Any]],
    device_caps: dict[str, dict[str, Any]],
    port_nodes: dict[str, dict[str, Any]],
    port_caps: dict[str, dict[str, Any]],
    cable_nodes: dict[str, dict[str, Any]],
    device_to_ports: dict[str, list[str]],
    port_to_device: dict[str, str],
    cable_by_ports: dict[frozenset[str], str],
    cable_to_ports: dict[str, list[str]],
    connections: list[dict[str, Any]],
    geometry: dict[str, dict[str, Any]],
    node_state: dict[str, dict[str, Any]],
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict[str, Any]:
    """Inspect whole-design signal flow, discovering all paths and summaries."""
    inbound_ports = {cnx.get("to_port") for cnx in connections}
    outbound_ports = {cnx.get("from_port") for cnx in connections}

    all_ports = list(port_nodes.keys())
    dangling_inputs: list[str] = []
    formats_count: dict[str, int] = {}

    for p_lbl in all_ports:
        cap = port_caps.get(p_lbl, {})
        fmt = cap.get("signal_format")
        if fmt:
            formats_count[fmt] = formats_count.get(fmt, 0) + 1
        if cap.get("port_direction") == "input" and p_lbl not in inbound_ports:
            dangling_inputs.append(p_lbl)

    root_ports = [p for p in outbound_ports if p not in inbound_ports]

    chains: list[dict[str, Any]] = []
    for r_port in sorted(root_ports):
        trace = trace_signal_chain(
            start_port=r_port,
            connections=connections,
            port_caps=port_caps,
            port_to_device=port_to_device,
            device_nodes=device_nodes,
            cable_by_ports=cable_by_ports,
            geometry=geometry,
            direction="downstream",
            max_depth=max_depth,
        )
        chains.append(
            {
                "source_port": r_port,
                "source_device": port_to_device.get(r_port),
                "downstream_hops": trace["downstream"],
                "leaf_sinks": trace["leaf_sinks"],
                "total_length_m": trace["total_downstream_length_m"],
                "has_cycle": trace["has_cycle"],
            }
        )

    connection_audits: list[dict[str, Any]] = []
    for cnx in connections:
        src = cnx.get("from_port", "")
        dst = cnx.get("to_port", "")
        src_cap = port_caps.get(src, {})
        dst_cap = port_caps.get(dst, {})
        is_compat, reason = _formats_compatible(
            src_cap.get("signal_format"),
            src_cap.get("signal_version"),
            dst_cap.get("signal_format"),
            dst_cap.get("signal_version"),
        )
        connection_audits.append(
            {
                "from_port": src,
                "from_device": port_to_device.get(src),
                "to_port": dst,
                "to_device": port_to_device.get(dst),
                "is_compatible": is_compat,
                "reason": reason,
            }
        )

    tot_power = sum(float(c.get("power_draw_watts") or 0.0) for c in device_caps.values())
    tot_heat = sum(float(c.get("heat_btu_hr") or 0.0) for c in device_caps.values())

    return {
        "target_type": "DESIGN",
        "design_label": design_label,
        "summary": {
            "total_devices": len(device_nodes),
            "total_ports": len(port_nodes),
            "total_cables": len(cable_nodes),
            "total_connections": len(connections),
            "dangling_inputs": sorted(dangling_inputs),
            "formats_in_use": formats_count,
            "total_power_draw_watts": round(tot_power, 2),
            "total_heat_btu_hr": round(tot_heat, 2),
            "chains_count": len(chains),
        },
        "signal_chains": chains,
        "connections": connection_audits,
    }


# ---------------------------------------------------------------------------
# Public Domain Core — do_inspect_signal_flow
# ---------------------------------------------------------------------------


async def do_inspect_signal_flow(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Inspect signal flow for a DESIGN or a specific DEVICE/PORT/CABLE node.

    Parameters
    ----------
    engine:
        NCEEngine instance with a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "design_id": str,             # required
            "node_label": str | None,     # optional node filter
            "direction": str | None,      # "upstream" | "downstream" | "both" (default "both")
            "max_depth": int | None,      # max hop traversal depth (default 32)
        }``

    Returns
    -------
    dict
        Structured signal flow analysis matching the inspected target.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("do_inspect_signal_flow: 'namespace_id' is required in params")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    design_id_raw: str = str(params.get("design_id") or "").strip()
    if not design_id_raw:
        raise ValueError("do_inspect_signal_flow: 'design_id' is required in params")

    design_lbl = _design_label(design_id_raw)
    node_label_raw = params.get("node_label")
    target_node_lbl = str(node_label_raw).strip() if node_label_raw else None

    direction = str(params.get("direction") or "both").lower()
    if direction not in ("upstream", "downstream", "both"):
        raise ValueError(
            f"do_inspect_signal_flow: invalid direction '{direction}'; must be 'upstream', 'downstream', or 'both'"
        )

    max_depth_raw = params.get("max_depth")
    max_depth = DEFAULT_MAX_DEPTH
    if max_depth_raw is not None:
        try:
            max_depth = int(max_depth_raw)
            if max_depth <= 0 or max_depth > MAX_ALLOWED_DEPTH:
                raise ValueError()
        except Exception:
            raise ValueError(
                f"do_inspect_signal_flow: 'max_depth' must be an integer between 1 and {MAX_ALLOWED_DEPTH}"
            )

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        scope_labels = await _fetch_design_scope_labels(conn, ns_uuid, design_lbl)
        nodes = await _fetch_nodes_by_labels(conn, ns_uuid, scope_labels, statuses=None)
        edges = await _fetch_edges_within(conn, ns_uuid, scope_labels)
        capabilities = await _fetch_capabilities_by_labels(conn, ns_uuid, scope_labels)

        rows_geo = await conn.fetch(
            """
            SELECT node_label, x, y, rack_position, rack_face, cable_length_m, cable_type, meta
            FROM system_design_geometry
            WHERE namespace_id = $1::uuid
              AND node_label = ANY($2::text[])
              AND version IS NULL
            """,
            ns_uuid,
            list(scope_labels),
        )
        geometry: dict[str, dict[str, Any]] = {
            r["node_label"]: {
                "x": float(r["x"]) if r["x"] is not None else None,
                "y": float(r["y"]) if r["y"] is not None else None,
                "rack_position": float(r["rack_position"])
                if r["rack_position"] is not None
                else None,
                "rack_face": r["rack_face"],
                "cable_length_m": float(r["cable_length_m"])
                if r["cable_length_m"] is not None
                else None,
                "cable_type": r["cable_type"],
                "meta": r["meta"] or {},
            }
            for r in rows_geo
        }

        rows_state = await conn.fetch(
            """
            SELECT node_label, status, revision, salience
            FROM system_design_node_state
            WHERE namespace_id = $1::uuid
              AND node_label = ANY($2::text[])
            """,
            ns_uuid,
            list(scope_labels),
        )
        node_state: dict[str, dict[str, Any]] = {
            r["node_label"]: {
                "status": r["status"],
                "revision": r["revision"],
                "salience": float(r["salience"]) if r["salience"] is not None else None,
            }
            for r in rows_state
        }

    nodes_by_type = _group_nodes_by_type(nodes)
    device_nodes = {d["label"]: d for d in nodes_by_type.get(_ENTITY_DEVICE, [])}
    port_nodes = {p["label"]: p for p in nodes_by_type.get(_ENTITY_PORT, [])}
    cable_nodes = {c["label"]: c for c in nodes_by_type.get(_ENTITY_CABLE, [])}

    port_to_device: dict[str, str] = {}
    device_to_ports: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("predicate") == _PRED_HAS_PORT:
            d_lbl = edge.get("subject", "")
            p_lbl = edge.get("object", "")
            if d_lbl and p_lbl:
                port_to_device[p_lbl] = d_lbl
                device_to_ports.setdefault(d_lbl, []).append(p_lbl)

    cable_by_ports: dict[frozenset[str], str] = {}
    cable_to_ports: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("predicate") == _PRED_USES_CABLE:
            p_lbl = edge.get("subject", "")
            c_lbl = edge.get("object", "")
            if p_lbl and c_lbl:
                cable_to_ports.setdefault(c_lbl, []).append(p_lbl)

    for c_lbl, p_list in cable_to_ports.items():
        if len(p_list) >= 2:
            cable_by_ports[frozenset({p_list[0], p_list[1]})] = c_lbl

    connections = [
        {"from_port": edge["subject"], "to_port": edge["object"]}
        for edge in edges
        if edge.get("predicate") == _PRED_CONNECTED_TO
    ]

    port_caps = {lbl: cap for lbl, cap in capabilities.items() if lbl in port_nodes}
    device_caps = {lbl: cap for lbl, cap in capabilities.items() if lbl in device_nodes}

    if target_node_lbl:
        if target_node_lbl in port_nodes:
            return inspect_port_signal_flow(
                port_label=target_node_lbl,
                port_caps=port_caps,
                port_nodes=port_nodes,
                port_to_device=port_to_device,
                device_nodes=device_nodes,
                device_caps=device_caps,
                connections=connections,
                cable_by_ports=cable_by_ports,
                geometry=geometry,
                direction=direction,
                max_depth=max_depth,
            )
        elif target_node_lbl in device_nodes:
            return inspect_device_signal_flow(
                device_label=target_node_lbl,
                device_nodes=device_nodes,
                device_caps=device_caps,
                device_to_ports=device_to_ports,
                port_caps=port_caps,
                port_nodes=port_nodes,
                connections=connections,
                cable_by_ports=cable_by_ports,
                port_to_device=port_to_device,
            )
        elif target_node_lbl in cable_nodes:
            return inspect_cable_signal_flow(
                cable_label=target_node_lbl,
                cable_nodes=cable_nodes,
                cable_to_ports=cable_to_ports,
                port_caps=port_caps,
                port_to_device=port_to_device,
                device_nodes=device_nodes,
                connections=connections,
                geometry=geometry,
                node_state=node_state,
            )
        else:
            raise ValueError(
                f"do_inspect_signal_flow: node '{target_node_lbl}' not found in design '{design_id_raw}' "
                "or is not a DEVICE, PORT, or CABLE node."
            )

    return inspect_design_signal_flow(
        design_label=design_lbl,
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
        node_state=node_state,
        max_depth=max_depth,
    )
