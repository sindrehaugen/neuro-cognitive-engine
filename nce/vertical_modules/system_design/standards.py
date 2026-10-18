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
    {
        "key": "electrical_nek_en",
        "label": "NEK 400 / CENELEC HD 60364 & EN 61439 Electrical",
        "description": "Norwegian NEK 400 low-voltage installation rules, CENELEC HD 60364, CPR EN 50575 fire classes, and EN 61439 switchboard assemblies.",
    },
    {
        "key": "cabling_nek700_emc",
        "label": "NEK 700 / EN 50174-2 ICT & EMC Segregation",
        "description": "Norwegian NEK 700 and European EN 50174-2 electromagnetic separation between LV power and ICT/AV cabling.",
    },
    {
        "key": "marine_dnv_norsok",
        "label": "DNV Maritime & NORSOK Offshore Electrical",
        "description": "DNV-RU-SHIP Pt.4 Ch.8/9, NEK 410 / IEC 60092 marine cables, DNV-CG-0339 location classes, and NORSOK E-001 offshore systems.",
    },
    {
        "key": "hazardous_atex_iso",
        "label": "ATEX 2014/34/EU, ISO 13849-1 & ISO 12944 Safety",
        "description": "ATEX / EN 60079 explosive atmospheres, EN 60204-1 / ISO 13849-1 machinery functional safety, and ISO 12944 corrosion protection.",
    },
    {
        "key": "electronics_ecss_ipc",
        "label": "ECSS / ISO 26262 Derating, RoHS 3 & IPC/ESD",
        "description": "ECSS-Q-ST-30-11C / IEC 61508 / ISO 26262 component derating, EU RoHS 3 / REACH, J-STD-033 MSL, and EN 61340-5-1 ESD.",
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
    # Electrical NEK & EN
    {
        "id": "NEK-400-2022",
        "category": "electrical_nek_en",
        "name": "NEK 400:2022 Low-Voltage Electrical Installations",
        "standard": "NEK 400:2022 / CENELEC HD 60364",
        "voltage_systems": ["TN-S 400V 3P+N", "IT 230V 3P"],
        "max_voltage_drop_lighting_pct": 3.0,
        "max_voltage_drop_other_pct": 4.0,
        "rcd_tripping_current_ma": 30.0,
        "usage_rule": "Enforces overload coordination Ib <= In <= Iz and I2 <= 1.45*Iz, 30mA RCD protection for socket outlets and wet rooms, and maximum 4% voltage drop for power circuits (3% for lighting).",
    },
    {
        "id": "EN-61439-SWITCHGEAR",
        "category": "electrical_nek_en",
        "name": "EN 61439 Low-Voltage Switchgear and Controlgear Assemblies",
        "standard": "EN 61439-1 / EN 61439-2",
        "rated_diversity_factor_rules": "RDF table: 2-3 circuits: 0.9, 4-5 circuits: 0.8, 6-9 circuits: 0.7, 10+ circuits: 0.6",
        "thermal_verification": "Mandatory temperature rise limits per EN 61439-1 clause 10.10",
        "usage_rule": "Switchboard assemblies must verify rated diversity factor (RDF) and thermal dissipation within enclosure limits under diversified continuous load.",
    },
    {
        "id": "CPR-EN-50575",
        "category": "electrical_nek_en",
        "name": "CPR (EU) 305/2011 & EN 50575 Cable Fire Reaction Euroclasses",
        "standard": "EN 50575 / EN 13501-6",
        "euroclasses": ["Aca", "B1ca", "B2ca", "Cca", "Dca", "Eca", "Fca"],
        "commercial_escape_route_min": "Cca-s1,d1,a1",
        "usage_rule": "Permanent building cables must satisfy Construction Products Regulation (CPR) Euroclasses. Escape routes and high-density commercial spaces mandate Cca-s1,d1,a1 or B2ca minimum.",
    },
    # Cabling NEK 700 & EMC
    {
        "id": "NEK-700-EN-50174-2",
        "category": "cabling_nek700_emc",
        "name": "NEK 700 / EN 50174-2 ICT & EMC Cabling Segregation",
        "standard": "NEK 700 / EN 50174-2 / ISO/IEC 14763-2",
        "base_separation_utp_mm": 200.0,
        "base_separation_stp_mm": 50.0,
        "solid_steel_divider_reduction": 0.25,
        "usage_rule": "Minimum 200mm segregation between unshielded data/AV cables and standard LV power cables without divider; reduced to 50mm with solid grounded steel divider. Fiber optic is fully immune (0mm).",
    },
    # Maritime DNV & NORSOK
    {
        "id": "DNV-RU-SHIP-PT4-CH8",
        "category": "marine_dnv_norsok",
        "name": "DNV Rules for Ships Pt.4 Ch.8 Electrical Installations",
        "standard": "DNV-RU-SHIP Pt.4 Ch.8 & Ch.9 / IEC 60092-201 / SOLAS II-1",
        "prohibited_earthing": ["TN-C", "PEN_HULL"],
        "max_voltage_thd_pct": 8.0,
        "min_ups_autonomy_essential_min": 30.0,
        "min_ups_autonomy_emergency_nav_hours": 18.0,
        "usage_rule": "TN-C (PEN / hull return) is strictly prohibited. Insulated IT systems require active IMD. MSB and ESB must be in separate fire zones. Essential consumers mandate dual feeds and UPS autonomy (30 min general, 18h emergency/nav).",
    },
    {
        "id": "IEC-60092-350-MARINE",
        "category": "marine_dnv_norsok",
        "name": "IEC 60092-350 / 360 Marine & Offshore Cabling",
        "standard": "IEC 60092-350 / IEC 60092-360 / NEK 410 / NORSOK E-001",
        "sheaths": ["SHF1", "SHF2"],
        "flame_retardance": "IEC 60332-3-22 Cat A",
        "fire_resistance_emergency": "IEC 60331-1/21 (BFOU, MGCG-FR)",
        "usage_rule": "Shipboard and offshore cabling requires halogen-free SHF1/SHF2 outer sheath, bunched flame retardancy per IEC 60332-3-22 Category A, and fire resistance per IEC 60331 for emergency circuits.",
    },
    # Hazardous ATEX & ISO
    {
        "id": "ATEX-2014-34-EU",
        "category": "hazardous_atex_iso",
        "name": "ATEX Directive 2014/34/EU & EN 60079 Explosive Atmospheres",
        "standard": "ATEX 2014/34/EU / EN 60079-0 / EN 60079-14 / NEK 420",
        "equipment_protection_levels": ["Ga", "Gb", "Gc", "Da", "Db", "Dc"],
        "gas_zones": ["Zone 0 (Ga)", "Zone 1 (Ga, Gb)", "Zone 2 (Ga, Gb, Gc)"],
        "temperature_classes": [
            "T1 (450C)",
            "T2 (300C)",
            "T3 (200C)",
            "T4 (135C)",
            "T5 (100C)",
            "T6 (85C)",
        ],
        "usage_rule": "Equipment installed in hazardous zones must match required EPL (Zone 0/20 requires Ga/Da; Zone 1/21 requires Gb/Db minimum) and surface temperature class (T1-T6) below auto-ignition point.",
    },
    {
        "id": "ISO-13849-1-EN-60204",
        "category": "hazardous_atex_iso",
        "name": "ISO 13849-1 & EN 60204-1 Machinery Safety & Electrical Equipment",
        "standard": "ISO 13849-1 / EN ISO 13849-1 / EN 60204-1",
        "performance_levels": ["PL a", "PL b", "PL c", "PL d", "PL e"],
        "emergency_stop_categories": [0, 1, 2],
        "max_protective_bonding_resistance_ohm": 0.1,
        "usage_rule": "Safety-related parts of control systems must achieve required Performance Level (PLr). Machinery protective bonding resistance to PE terminal must not exceed 0.10 Ohm.",
    },
    {
        "id": "ISO-12944-CORROSION",
        "category": "hazardous_atex_iso",
        "name": "ISO 12944 Paints and Varnishes - Corrosion Protection of Steel",
        "standard": "ISO 12944-2 / EN 60529 / EN 62262",
        "corrosivity_categories": ["C1", "C2", "C3", "C4", "C5", "CX"],
        "location_mappings": {
            "dry_indoor": "C1/IP20/IK02",
            "wet_room": "C3/IP44/IK06",
            "open_deck": "C5/IP56/IK08",
            "offshore_splash_zone": "CX/IP66/IK10",
        },
        "usage_rule": "Structural enclosures and equipment coatings must withstand atmospheric corrosivity category (C1 interior to CX extreme offshore/marine) alongside minimum IP and IK mechanical impact protection.",
    },
    # Electronics ECSS & IPC
    {
        "id": "ECSS-Q-ST-30-11C-DERATING",
        "category": "electronics_ecss_ipc",
        "name": "ECSS-Q-ST-30-11C / IEC 61508 / ISO 26262 Component Derating",
        "standard": "ECSS-Q-ST-30-11C / IEC 61508 / ISO 26262",
        "max_voltage_ratio_capacitor": 0.60,
        "max_power_ratio_resistor": 0.50,
        "max_stress_ratio_semiconductor": 0.70,
        "thermal_junction_safety_margin_c": 15.0,
        "tightened_safety_margin_c": 25.0,
        "usage_rule": "Electronic component electrical and thermal stress must be derated against maximum ratings. Tightened profiles (automotive ISO 26262, SIL IEC 61508, DNV marine) reduce ratios by 0.10 and require 25°C junction temperature margin.",
    },
    {
        "id": "ROHS3-REACH-JSTD033",
        "category": "electronics_ecss_ipc",
        "name": "EU RoHS 3, REACH SVHC & IPC/JEDEC J-STD-033 MSL",
        "standard": "Directive 2011/65/EU / EU 2015/863 / Regulation (EC) 1907/2006 / J-STD-033",
        "rohs_substances_count": 10,
        "lead_max_ppm": 1000.0,
        "cadmium_max_ppm": 100.0,
        "reach_svhc_threshold_pct": 0.1,
        "msl_floor_life": "MSL 1 (unlimited) down to MSL 6 (mandatory bake before use)",
        "usage_rule": "Enforces 10 EU RoHS 3 restricted substances, EU REACH Substances of Very High Concern (<0.1% w/w), and IPC/JEDEC J-STD-033 moisture sensitivity floor life control before reflow assembly.",
    },
    {
        "id": "EN-61340-5-1-ESD",
        "category": "electronics_ecss_ipc",
        "name": "EN 61340-5-1 / IEC 61340-5-1 Electrostatic Protected Area (EPA)",
        "standard": "EN 61340-5-1 / IEC 61340-5-1",
        "wrist_strap_resistance_mohm_range": [0.75, 35.0],
        "worksurface_mat_max_resistance_ohm": 1.0e9,
        "footwear_max_resistance_ohm": 1.0e8,
        "min_relative_humidity_pct": 30.0,
        "usage_rule": "Personnel grounding wrist straps in EPA must measure between 0.75 MΩ and 35.0 MΩ. Worksurface mats must measure < 1.0 GΩ to ground, and ambient relative humidity should remain >= 30%.",
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
