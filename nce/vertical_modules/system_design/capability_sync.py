"""
nce/vertical_modules/system_design/capability_sync.py
=====================================================
Port data -> PRODUCT ETIM features & device-capability sync for the
System Design vertical module (Wave C-5 / Delivery Lane).

Responsibilities
----------------
* Ingest product catalog ETIM features and port specifications.
* Map product electrical, mechanical, and AV parameters to AVIXA Revit
  Parameter schema:
  - Signal formats (HDMI, DisplayPort, Dante, USB, HDBaseT).
  - Port directions (input, output, bidirectional).
  - PoE specifications (802.3 classes 0-8, wattage).
  - Network audio channels (Dante RX/TX counts).
  - Thermal / power budgets (watts, BTU/hr).
* Idempotently upsert capabilities into ``system_design_device_capabilities``
  (migration 039).
* Cross-lane coordination: reads product data via ``engine.modules["product"]``
  or direct untenanted ``product_catalog`` queries without modifying Product files.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.vertical_modules.system_design.devices import _upsert_capability

log = logging.getLogger("nce.vertical_modules.system_design.capability_sync")


def _extract_capabilities_from_etim(
    etim_specs: dict[str, Any],
    manufacturer: str | None = None,
    model_number: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract device-level capability and list of port-level capabilities from ETIM specs.

    Returns:
        (device_capability_dict, list_of_port_capability_dicts)
    """
    device_cap: dict[str, Any] = {
        "manufacturer": manufacturer or etim_specs.get("manufacturer"),
        "model_number": model_number
        or etim_specs.get("model_number")
        or etim_specs.get("mfr_part_no"),
        "device_category": etim_specs.get("device_category") or etim_specs.get("category"),
        "power_draw_watts": etim_specs.get("power_draw_watts") or etim_specs.get("power_watts"),
        "heat_btu_hr": etim_specs.get("heat_btu_hr") or etim_specs.get("heat_btu"),
        "redundancy_role": etim_specs.get("redundancy_role", "standalone"),
    }

    # Extract PoE if present on device level
    if "poe_class" in etim_specs:
        device_cap["poe_class"] = int(etim_specs["poe_class"])
    if "poe_watts" in etim_specs:
        device_cap["poe_watts"] = float(etim_specs["poe_watts"])

    # Extract Dante channel counts
    if "dante_rx_channels" in etim_specs:
        device_cap["dante_rx_channels"] = int(etim_specs["dante_rx_channels"])
    if "dante_tx_channels" in etim_specs:
        device_cap["dante_tx_channels"] = int(etim_specs["dante_tx_channels"])

    # Extract ports list
    raw_ports = etim_specs.get("ports") or etim_specs.get("connectors") or []
    port_caps: list[dict[str, Any]] = []

    if isinstance(raw_ports, list):
        for idx, p in enumerate(raw_ports, 1):
            if not isinstance(p, dict):
                continue
            port_name = p.get("name") or p.get("label") or f"port_{idx}"
            port_cap = {
                "port_name": port_name,
                "signal_format": p.get("signal_format") or p.get("format") or p.get("type"),
                "signal_version": p.get("signal_version") or p.get("version"),
                "port_direction": p.get("port_direction") or p.get("direction", "input"),
                "poe_class": p.get("poe_class"),
                "poe_watts": p.get("poe_watts"),
                "dante_rx_channels": p.get("dante_rx_channels"),
                "dante_tx_channels": p.get("dante_tx_channels"),
                "manufacturer": device_cap.get("manufacturer"),
                "model_number": device_cap.get("model_number"),
            }
            port_caps.append(port_cap)

    return device_cap, port_caps


async def do_sync_device_capabilities(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Sync device and port capabilities from product ETIM specs into system_design_device_capabilities.

    Parameters:
        - namespace_id: UUID or str
        - device_label: str (required, e.g. "device:display-1")
        - product_id: UUID or str (optional, product_catalog.id)
        - mfr_part_no: str (optional, used if product_id is omitted)
        - manufacturer: str (optional)
        - port_specs: list[dict] (optional explicit port specs overriding or augmenting catalog)
        - extra: dict (optional metadata)
    """
    raw_ns = params.get("namespace_id")
    if not raw_ns:
        raise ValueError("Missing required parameter: namespace_id")

    ns_uuid = UUID(str(raw_ns))
    device_label = str(params.get("device_label") or "").strip()
    if not device_label:
        raise ValueError("Missing required parameter: device_label")

    product_id = params.get("product_id")
    mfr_part_no = params.get("mfr_part_no")
    manufacturer = params.get("manufacturer")
    explicit_port_specs = params.get("port_specs") or []
    extra = params.get("extra") or {}

    etim_specs: dict[str, Any] = {}
    resolved_mfr = manufacturer
    resolved_part = mfr_part_no

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Resolve product from catalog if product_id or mfr_part_no provided
        if product_id:
            row = await conn.fetchrow(
                """
                SELECT id, manufacturer, mfr_part_no, etim_specs
                FROM product_catalog
                WHERE id = $1::uuid AND is_deleted = false
                """,
                UUID(str(product_id)),
            )
            if row:
                resolved_mfr = row["manufacturer"]
                resolved_part = row["mfr_part_no"]
                etim_specs = row["etim_specs"] if isinstance(row["etim_specs"], dict) else {}
        elif mfr_part_no:
            query = """
                SELECT id, manufacturer, mfr_part_no, etim_specs
                FROM product_catalog
                WHERE lower(mfr_part_no) = lower($1) AND is_deleted = false
            """
            args: list[Any] = [mfr_part_no]
            if manufacturer:
                query += " AND lower(manufacturer) = lower($2)"
                args.append(manufacturer)
            query += " LIMIT 1"
            row = await conn.fetchrow(query, *args)
            if row:
                resolved_mfr = row["manufacturer"]
                resolved_part = row["mfr_part_no"]
                etim_specs = row["etim_specs"] if isinstance(row["etim_specs"], dict) else {}

        # Merge explicit ETIM overrides from params
        if "etim_specs" in params and isinstance(params["etim_specs"], dict):
            etim_specs.update(params["etim_specs"])

        device_cap, catalog_port_caps = _extract_capabilities_from_etim(
            etim_specs,
            manufacturer=resolved_mfr,
            model_number=resolved_part,
        )

        if extra:
            device_cap["extra"] = extra

        # Upsert device capabilities
        await _upsert_capability(conn, ns_uuid, device_label, device_cap)

        # Merge catalog ports and explicit port specs
        all_port_caps = list(catalog_port_caps)
        if explicit_port_specs and isinstance(explicit_port_specs, list):
            for p in explicit_port_specs:
                if isinstance(p, dict):
                    all_port_caps.append(p)

        # Upsert port capabilities
        synced_ports: list[str] = []
        for p in all_port_caps:
            port_name = p.get("port_name") or p.get("name") or "port"
            port_node_label = f"{device_label}:{port_name}"
            await _upsert_capability(conn, ns_uuid, port_node_label, p)
            synced_ports.append(port_node_label)

    return {
        "status": "synced",
        "namespace_id": str(ns_uuid),
        "device_label": device_label,
        "manufacturer": resolved_mfr,
        "model_number": resolved_part,
        "device_capabilities": device_cap,
        "ports_synced_count": len(synced_ports),
        "synced_port_labels": synced_ports,
    }
