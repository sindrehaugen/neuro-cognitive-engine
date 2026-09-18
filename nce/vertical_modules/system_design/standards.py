"""
nce/vertical_modules/system_design/standards.py
===============================================
Config-as-IP reference library for AV standards and cabling specifications
for the System Design vertical module (Wave C-5 / Delivery Lane).

Responsibilities
----------------
* Curated AV industry standards:
  - HDMI (versions, bandwidth, max passive/active lengths, resolution support).
  - USB-C & USB4 (alt-mode, data throughput, power delivery profiles, AOC rules).
  - Category cabling (Cat5e, Cat6, Cat6A, STP/UTP, HDBaseT requirements).
  - Networked and analog audio cabling (Dante, AES67, balanced, microphone vs line).
  - Display mounting (VESA standards, weight ratings, ADA 2010 protrusion rules).
  - Power over Ethernet (IEEE 802.3af/at/bt PoE types, classes, wattage budgets).
* Read-only domain query function:
  - ``do_get_standards(engine, params)``: filter by category, standard_id, or search query.
* Pure, deterministic, zero-dependency lookup functions.

Design invariants (uncle-bob-craft)
------------------------------------
* Config-as-IP lives in this module directory, NEVER under ``nce/config_data/`` (Q-5).
* Read-only: no database mutations, no event log emissions.
* Pure data structures: unit-testable without database or network I/O.
* Zero host portal literals: strictly vendor-neutral AV engineering standards.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("nce.vertical_modules.system_design.standards")

# ---------------------------------------------------------------------------
# Curated Standards Registry (Config-as-IP)
# ---------------------------------------------------------------------------

AV_STANDARDS_CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "cabling_hdmi",
        "label": "HDMI Cabling & Bandwidth",
        "description": "HDMI specification standards, bandwidth ceilings, and passive/active length limits.",
    },
    {
        "key": "cabling_usbc",
        "label": "USB-C & USB4 Cabling",
        "description": "USB-C DisplayPort Alt-Mode, data throughput rates, and Power Delivery (PD) profiles.",
    },
    {
        "key": "cabling_category",
        "label": "Category & HDBaseT Cabling",
        "description": "Twisted pair standards (Cat6/Cat6A), shielding requirements, and HDBaseT certified distances.",
    },
    {
        "key": "cabling_audio",
        "label": "Audio Interconnects & Network Audio",
        "description": "Analog balanced/unbalanced levels, Dante/AES67 network audio QoS, and cable segregation.",
    },
    {
        "key": "mounting_display",
        "label": "Display Mounting & ADA Compliance",
        "description": "VESA hole patterns, hardware fastener specs, and ADA 2010 wall protrusion rules.",
    },
    {
        "key": "power_poe",
        "label": "Power over Ethernet (PoE)",
        "description": "IEEE 802.3af/at/bt standards, switch source power, and device draw classes.",
    },
]

AV_STANDARDS_ITEMS: list[dict[str, Any]] = [
    # HDMI
    {
        "id": "HDMI-2.1",
        "category": "cabling_hdmi",
        "name": "HDMI 2.1 Ultra High Speed",
        "bandwidth_gbps": 48.0,
        "max_resolution": "8K@60Hz 4:4:4 / 4K@120Hz 4:4:4",
        "max_passive_length_m": 3.0,
        "max_aoc_length_m": 30.0,
        "hdcp_version": "2.3",
        "usage_rule": "Passive runs exceeding 3.0 meters must use Active Optical Cables (AOC) or certified extenders to avoid packet loss at 48Gbps FRL.",
        "fire_rating": "CMP (Plenum) or LSZH required for permanent plenum-space conduit/ceiling runs.",
    },
    {
        "id": "HDMI-2.0",
        "category": "cabling_hdmi",
        "name": "HDMI 2.0 Premium High Speed",
        "bandwidth_gbps": 18.0,
        "max_resolution": "4K@60Hz 4:4:4",
        "max_passive_length_m": 5.0,
        "max_aoc_length_m": 50.0,
        "hdcp_version": "2.2",
        "usage_rule": "Standard commercial meeting room interconnect. Passive runs up to 5.0m; longer runs require active optical or HDBaseT extension.",
        "fire_rating": "CMP or LSZH for ceiling/riser distribution.",
    },
    {
        "id": "HDMI-1.4",
        "category": "cabling_hdmi",
        "name": "HDMI 1.4 High Speed",
        "bandwidth_gbps": 10.2,
        "max_resolution": "1080p@60Hz / 4K@30Hz 4:2:0",
        "max_passive_length_m": 10.0,
        "max_aoc_length_m": 50.0,
        "hdcp_version": "1.4",
        "usage_rule": "Legacy standard for auxiliary inputs and control monitors. Not recommended for new primary display signal distribution.",
        "fire_rating": "Standard PVC or CMP.",
    },
    # USB-C
    {
        "id": "USBC-DP-ALTMODE",
        "category": "cabling_usbc",
        "name": "USB-C with DisplayPort Alt-Mode & PD",
        "bandwidth_gbps": 20.0,
        "max_resolution": "4K@60Hz 4:4:4 + USB 3.2 data",
        "max_passive_length_m": 2.0,
        "max_aoc_length_m": 15.0,
        "power_delivery_watts": 100.0,
        "usage_rule": "Passive cables over 2.0m throttle to USB 2.0 (480Mbps). Conference table BYOD connections over 2.0m must use Active Optical or Extenders.",
        "fire_rating": "Commercial grade flexible jacket.",
    },
    {
        "id": "USB4-THUNDERBOLT4",
        "category": "cabling_usbc",
        "name": "USB4 / Thunderbolt 4 Certified",
        "bandwidth_gbps": 40.0,
        "max_resolution": "Dual 4K@60Hz / Single 8K@60Hz",
        "max_passive_length_m": 0.8,
        "max_aoc_length_m": 5.0,
        "power_delivery_watts": 100.0,
        "usage_rule": "High-performance docking connection. Passive runs strictly limited to 0.8m. E-Marker chip mandatory.",
        "fire_rating": "Commercial grade jacket.",
    },
    # Category / HDBaseT
    {
        "id": "CAT6A-STP-HDBASET",
        "category": "cabling_category",
        "name": "Category 6A Shielded (F/UTP or S/FTP)",
        "bandwidth_mhz": 500.0,
        "max_distance_m": 100.0,
        "hdbaset_certified": True,
        "poe_supported": "802.3bt Type 4 (90W)",
        "usage_rule": "Mandatory for HDBaseT 2.0/3.0 uncompressed 4K video distribution and 10Gbps AVoIP (SDVoE). Shield must be grounded at both ends.",
        "fire_rating": "CMP / Plenum or LSZH for plenum ceiling spaces.",
    },
    {
        "id": "CAT6-UTP",
        "category": "cabling_category",
        "name": "Category 6 Unshielded (U/UTP)",
        "bandwidth_mhz": 250.0,
        "max_distance_m": 100.0,
        "hdbaset_certified": False,
        "poe_supported": "802.3at PoE+ (30W)",
        "usage_rule": "Suitable for Gigabit Ethernet control and standard 1GbE Dante audio. Not certified for high-bandwidth 4K HDBaseT due to alien crosstalk.",
        "fire_rating": "CMR (Riser) or CMP (Plenum).",
    },
    # Audio
    {
        "id": "AUDIO-DANTE-IP",
        "category": "cabling_audio",
        "name": "Dante / AES67 Networked Audio",
        "transport": "IP / UDP over Cat6A/Cat6",
        "latency_ms": 1.0,
        "sample_rate_khz": 48.0,
        "max_channels_per_link": 512,
        "usage_rule": "Requires managed Gigabit switch with DiffServ QoS (DSCP 46 for PTP v1/v2 clocking, DSCP 34 for audio packets). EEE (802.3az) must be disabled.",
        "fire_rating": "Standard network category cabling.",
    },
    {
        "id": "AUDIO-BALANCED-ANALOG",
        "category": "cabling_audio",
        "name": "Balanced Analog Line/Mic (+4dBu / -60dBu)",
        "transport": "Twisted shielded pair (2-conductor + drain)",
        "max_distance_m": 100.0,
        "connector_types": ["XLR", "Euroblock (Phoenix)"],
        "usage_rule": "Common-mode rejection ratio (CMRR) eliminates EMI. Mic lines must maintain >=30cm separation from AC power lines or cross at 90 degrees.",
        "fire_rating": "CMP or LSZH.",
    },
    # Mounting & ADA
    {
        "id": "MOUNT-ADA-WALL",
        "category": "mounting_display",
        "name": "ADA 2010 Section 307 Wall Protrusion Limit",
        "max_protrusion_mm": 100.0,
        "applicable_height_range": "Between 27 in (685 mm) and 80 in (2030 mm) above finished floor",
        "usage_rule": "Any display or enclosure mounted along a circulation path whose lower edge is between 685mm and 2030mm AFF must not protrude >100mm (4 in). Use recessed in-wall backbox or ultra-slim mount.",
    },
    {
        "id": "MOUNT-VESA-STANDARD",
        "category": "mounting_display",
        "name": "VESA MIS-D, MIS-E, MIS-F Display Mounting",
        "patterns": ["100x100 (M4)", "200x200 (M6)", "400x400 (M8)", "600x400 (M8)"],
        "safety_factor": 4.0,
        "usage_rule": "Wall backing must support 4x total display + mount weight. Use structural wood/steel studs or unistrut; drywall anchors alone are prohibited.",
    },
    # PoE
    {
        "id": "POE-802.3BT-TYPE4",
        "category": "power_poe",
        "name": "IEEE 802.3bt Type 4 (PoE++)",
        "pse_power_watts": 90.0,
        "pd_power_watts": 71.3,
        "cable_requirement": "Cat6A recommended (all 4 pairs energized)",
        "usage_rule": "Intended for PTZ cameras with heaters, active video soundbars, and commercial touch panels. Derate bundle sizes in conduit to prevent thermal buildup.",
    },
    {
        "id": "POE-802.3AT-TYPE2",
        "category": "power_poe",
        "name": "IEEE 802.3at Type 2 (PoE+)",
        "pse_power_watts": 30.0,
        "pd_power_watts": 25.5,
        "cable_requirement": "Cat5e or Cat6 (2 pairs energized)",
        "usage_rule": "Standard power for AV over IP endpoints, ceiling microphones, and medium touch controllers.",
    },
]


# ---------------------------------------------------------------------------
# Public Domain Query Functions
# ---------------------------------------------------------------------------


def get_standards(
    category: str | None = None,
    standard_id: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Return curated standards filtered by category, id, or text query."""
    items = AV_STANDARDS_ITEMS
    if category:
        c_low = category.strip().lower()
        items = [x for x in items if x.get("category", "").lower() == c_low]

    if standard_id:
        s_low = standard_id.strip().lower()
        items = [x for x in items if x.get("id", "").lower() == s_low]

    if search:
        q = search.strip().lower()
        items = [
            x
            for x in items
            if q in x.get("name", "").lower()
            or q in x.get("id", "").lower()
            or q in x.get("usage_rule", "").lower()
        ]

    return {
        "categories": AV_STANDARDS_CATEGORIES,
        "standards": items,
        "total": len(items),
    }


def do_get_standards(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Public core function for System Design standards inspection."""
    category = params.get("category")
    standard_id = params.get("standard_id")
    search = params.get("search")
    return get_standards(category=category, standard_id=standard_id, search=search)
