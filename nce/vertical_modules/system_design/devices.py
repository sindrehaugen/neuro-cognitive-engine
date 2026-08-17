"""
nce/vertical_modules/system_design/devices.py
=============================================
Phase-2 (additive moat layer) — device-capability model for the System Design
vertical module.

Responsibilities
----------------
* Define DEVICE / PORT / SIGNAL_CHAIN / RACK / CABLE node types as kg_nodes
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
  DEVICE        -[uses_cable]-> CABLE    (optional physical cable reference)
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
DEVICE / PORT / SIGNAL_CHAIN / RACK / CABLE are owned by system_design.
Entries are in nce/config_data/node-ownership.json (appended in this wave).
"""

from __future__ import annotations

import logging
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
_NODE_TYPE_SIGNAL_CHAIN: str = "SIGNAL_CHAIN"
_NODE_TYPE_RACK: str = "RACK"
_NODE_TYPE_CABLE: str = "CABLE"

# Re-export so validation_queries.py can import them without string duplication.
NODE_TYPE_DEVICE = _NODE_TYPE_DEVICE
NODE_TYPE_PORT = _NODE_TYPE_PORT
NODE_TYPE_SIGNAL_CHAIN = _NODE_TYPE_SIGNAL_CHAIN
NODE_TYPE_RACK = _NODE_TYPE_RACK
NODE_TYPE_CABLE = _NODE_TYPE_CABLE

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


def signal_chain_label(design_id: str, chain_ref: str) -> str:
    """Canonical SIGNAL_CHAIN label: ``SIGNAL_CHAIN:<DESIGN_ID>:<CHAIN_REF>``."""
    return f"SIGNAL_CHAIN:{design_id.upper()}:{chain_ref.upper()}"


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
) -> None:
    """Upsert one owned graph node.  Always: assert_owner → INSERT → emit."""
    await assert_owner(conn, ns_uuid, entity_type, _SYSTEM_DESIGN_ENGINE)
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
            }

    connections:
        Optional list of port-to-port signal connections::

            {
                "from_device_ref": str,
                "from_port_ref": str,
                "to_device_ref": str,
                "to_port_ref": str,
                "confidence": float,        # default 1.0
                "cable_ref": str | None,    # optional cable label
            }

    racks:
        Optional list of rack dicts::

            {
                "rack_ref": str,
                "capability": { ... },  # same AVIXA param shape
            }

    source_id:
        Optional system_design source record ID for retirement tracking.

    Returns
    -------
    dict
        ``{"authored": {"nodes": int, "edges": int, "capabilities": int}}``
    """
    ns_uuid = UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id
    conn_list: list[dict[str, Any]] = connections or []
    rack_list: list[dict[str, Any]] = racks or []

    node_count = 0
    edge_count = 0
    cap_count = 0

    design_lbl = f"DESIGN:{design_id.upper()}"

    # ------------------------------------------------------------------
    # 1. Racks (optional — write before devices so mounted_in edges resolve)
    # ------------------------------------------------------------------
    for rack in rack_list:
        rack_ref: str = rack["rack_ref"]
        rack_lbl = rack_label(design_id, rack_ref)

        await _upsert_node(conn, ns_uuid, rack_lbl, _NODE_TYPE_RACK, source_id)
        node_count += 1

        await _upsert_edge(
            conn, ns_uuid, design_lbl, _PRED_HAS_RACK, rack_lbl, _STRUCTURAL_CONFIDENCE, source_id
        )
        edge_count += 1

        rack_cap = rack.get("capability", {})
        if rack_cap:
            await _upsert_capability(conn, ns_uuid, rack_lbl, rack_cap)
            cap_count += 1

    # ------------------------------------------------------------------
    # 2. Devices + their ports
    # ------------------------------------------------------------------
    for dev in devices:
        dev_ref: str = dev["device_ref"]
        dev_lbl = device_label(design_id, dev_ref)

        await _upsert_node(conn, ns_uuid, dev_lbl, _NODE_TYPE_DEVICE, source_id)
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

        # Rack mounting edge (optional).
        rack_ref_str: str | None = dev.get("rack_ref")
        if rack_ref_str:
            r_lbl = rack_label(design_id, rack_ref_str)
            await _upsert_edge(
                conn, ns_uuid, dev_lbl, _PRED_MOUNTED_IN, r_lbl, _STRUCTURAL_CONFIDENCE, source_id
            )
            edge_count += 1

        # Ports.
        for port in dev.get("ports", []):
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
    for cnx in conn_list:
        from_port_lbl = port_label(design_id, cnx["from_device_ref"], cnx["from_port_ref"])
        to_port_lbl = port_label(design_id, cnx["to_device_ref"], cnx["to_port_ref"])
        cnx_conf: float = float(cnx.get("confidence", _STRUCTURAL_CONFIDENCE))
        cnx_source: str | None = cnx.get("source_id") or source_id

        await _upsert_edge(
            conn, ns_uuid, from_port_lbl, _PRED_CONNECTED_TO, to_port_lbl, cnx_conf, cnx_source
        )
        edge_count += 1

        # Optional cable reference.
        cable_ref_str: str | None = cnx.get("cable_ref")
        if cable_ref_str:
            cable_lbl = cable_label(design_id, cable_ref_str)
            await _upsert_node(conn, ns_uuid, cable_lbl, _NODE_TYPE_CABLE, cnx_source)
            node_count += 1
            await _upsert_edge(
                conn,
                ns_uuid,
                from_port_lbl,
                _PRED_USES_CABLE,
                cable_lbl,
                _STRUCTURAL_CONFIDENCE,
                cnx_source,
            )
            edge_count += 1

    log.info(
        "do_author_device_topology: ns=%s design=%s nodes=%d edges=%d capabilities=%d",
        ns_uuid,
        design_id,
        node_count,
        edge_count,
        cap_count,
    )
    return {"authored": {"nodes": node_count, "edges": edge_count, "capabilities": cap_count}}
