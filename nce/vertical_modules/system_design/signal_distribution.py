"""
nce/vertical_modules/system_design/signal_distribution.py
=========================================================
Config-as-IP signal distribution rules and decision engine for the
System Design vertical module (Wave C-5 / Delivery Lane).

Responsibilities
----------------
* Curated signal distribution rules:
  - Maps meeting room inputs (cable containment, distance, USB-C alt-mode,
    wireless requirement, charging requirement, ecosystem) to recommended
    distribution roles/topologies.
* Standard distribution roles:
  - ``USBC_DIRECT``: Direct USB-C cable (<=2m passive, <=5m active).
  - ``USBC_ACTIVE_EXTENDER``: Active optical USB-C or active booster (5m-15m).
  - ``CAT_EXTENDER_GENERIC``: Dual video+USB over Cat6A transmitter/receiver (15m-70m).
  - ``CAT_EXTENDER_SIMPLE``: Video-only or USB-only Cat extension without laptop charging.
  - ``CAT_EXTENDER_VENDOR``: Vendor-native ecosystem extender (e.g. Yealink VCH, Crestron DM).
  - ``HDMI_PLUS_USB``: Separate HDMI video and USB-B peripheral runs.
  - ``DOCK_DESK``: Under-table or desktop docking hub.
  - ``AVOIP``: 1GbE / 10GbE network encoder and decoder distribution.
  - ``WIRELESS``: Wireless presentation dongle/protocol (AirPlay, Miracast, Cast, WPP).
* Decision evaluation engine:
  - ``evaluate_signal_distribution(params)``: deterministic pure evaluation over rules.
  - ``do_get_signal_rules(engine, params)``: inspect raw decision rules and roles or evaluate if inputs provided.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("nce.vertical_modules.system_design.signal_distribution")

SIGNAL_ROLES: list[dict[str, Any]] = [
    {
        "role": "USBC_DIRECT",
        "label": "Direct USB-C Cable",
        "description": "Single-cable USB-C connection providing DisplayPort Alt-Mode, USB 3.2 data, and Power Delivery.",
        "max_distance_m": 5.0,
        "max_passive_m": 2.0,
        "supports_charging": True,
        "cable_category": "USB-C Active / Passive Certified",
    },
    {
        "role": "USBC_ACTIVE_EXTENDER",
        "label": "Active Optical USB-C Extender",
        "description": "Active optical hybrid copper/fiber USB-C cable for runs between 5m and 15m with Alt-Mode video.",
        "max_distance_m": 15.0,
        "max_passive_m": 0.0,
        "supports_charging": True,
        "cable_category": "USB-C Active Optical (AOC)",
    },
    {
        "role": "CAT_EXTENDER_GENERIC",
        "label": "Cat6A HDBaseT / USB Extender",
        "description": "Point-to-point category cable transmitter/receiver set carrying 4K video, USB peripherals, and remote power.",
        "max_distance_m": 70.0,
        "max_passive_m": 0.0,
        "supports_charging": True,
        "cable_category": "Category 6A Shielded (F/UTP or S/FTP)",
    },
    {
        "role": "CAT_EXTENDER_SIMPLE",
        "label": "Basic Category Extender (No Charging)",
        "description": "Cost-effective HDMI or USB extender over Cat6 without host power delivery.",
        "max_distance_m": 50.0,
        "max_passive_m": 0.0,
        "supports_charging": False,
        "cable_category": "Category 6 UTP / STP",
    },
    {
        "role": "CAT_EXTENDER_VENDOR",
        "label": "Vendor-Specific Ecosystem Extender",
        "description": "Native ecosystem category extension matching the room system (e.g. Yealink VCH51 or Crestron DM).",
        "max_distance_m": 40.0,
        "max_passive_m": 0.0,
        "supports_charging": True,
        "cable_category": "Category 6 / 6A STP",
    },
    {
        "role": "HDMI_PLUS_USB",
        "label": "Discrete HDMI Video + USB Data",
        "description": "Traditional dual-cable run with separate HDMI cable for display and USB-A to USB-B for camera/touchback.",
        "max_distance_m": 15.0,
        "max_passive_m": 5.0,
        "supports_charging": False,
        "cable_category": "HDMI 2.0 + USB 3.0 Type B",
    },
    {
        "role": "DOCK_DESK",
        "label": "Tabletop / Under-Desk Docking Station",
        "description": "Central docking hub at the table consolidating video, USB, network, and high-wattage laptop charging.",
        "max_distance_m": 3.0,
        "max_passive_m": 1.0,
        "supports_charging": True,
        "cable_category": "USB-C Thunderbolt / USB4 Host Cable",
    },
    {
        "role": "AVOIP",
        "label": "Networked AV over IP (1GbE / 10GbE)",
        "description": "Encoder and decoder nodes transmitting low-latency video and USB routing over managed LAN infrastructure.",
        "max_distance_m": 100.0,
        "max_passive_m": 0.0,
        "supports_charging": False,
        "cable_category": "Category 6A STP / Multimode Fiber",
    },
    {
        "role": "WIRELESS",
        "label": "Wireless Presentation & BYOD Sharing",
        "description": "Zero-cable presentation via network protocol (AirPlay, Miracast, Google Cast) or wireless USB dongle.",
        "max_distance_m": 10.0,
        "max_passive_m": 0.0,
        "supports_charging": False,
        "cable_category": "Wi-Fi 6 / 802.11ax RF",
    },
]

# Ordered decision rules (first matching rule wins)
SIGNAL_RULES: list[dict[str, Any]] = [
    {
        "id": "RULE-NO-PATHWAY-WIRELESS",
        "name": "No cable pathway with wireless requested",
        "match": {
            "containment": "none",
            "wants_wireless": True,
        },
        "outcome": "WIRELESS",
        "rationale": "No containment or conduit available; wireless presentation is the only viable transmission method.",
    },
    {
        "id": "RULE-NO-PATHWAY-AVOIP",
        "name": "No physical floor pathway, building network available",
        "match": {
            "containment": "none",
        },
        "outcome": "AVOIP",
        "rationale": "Without floor containment or table trenching, existing local network ports must be utilized via AVoIP.",
    },
    {
        "id": "RULE-VENDOR-YEALINK-SHORT",
        "name": "Yealink ecosystem with distance <= 40m and charging",
        "match": {
            "vendor_ecosystem": "yealink",
            "distance_max_m": 40.0,
            "wants_charging": True,
        },
        "outcome": "CAT_EXTENDER_VENDOR",
        "rationale": "Native Yealink VCH51 category extender optimizes compatibility, touchback, and charging.",
    },
    {
        "id": "RULE-USBC-SHORT-DIRECT",
        "name": "Short distance with USB-C Alt-Mode supported",
        "match": {
            "altmode": True,
            "allow_usbc": True,
            "distance_max_m": 3.0,
        },
        "outcome": "USBC_DIRECT",
        "rationale": "Short run within certified passive/active direct USB-C limits allows clean single-cable table connection.",
    },
    {
        "id": "RULE-USBC-MEDIUM-ACTIVE",
        "name": "Medium distance with USB-C Alt-Mode supported",
        "match": {
            "altmode": True,
            "allow_usbc": True,
            "distance_max_m": 15.0,
        },
        "outcome": "USBC_ACTIVE_EXTENDER",
        "rationale": "Distance exceeds passive USB-C limits (3m) but within active optical cable certified range (15m).",
    },
    {
        "id": "RULE-CAT6A-LONG-CHARGING",
        "name": "Long distance with charging and video over conduit",
        "match": {
            "distance_max_m": 70.0,
            "wants_charging": True,
        },
        "outcome": "CAT_EXTENDER_GENERIC",
        "rationale": "Runs between 15m and 70m requiring laptop power delivery mandate HDBaseT / USB Category 6A extenders.",
    },
    {
        "id": "RULE-CAT-LONG-NOCHARGING",
        "name": "Long distance without charging requirement",
        "match": {
            "distance_max_m": 60.0,
            "wants_charging": False,
        },
        "outcome": "CAT_EXTENDER_SIMPLE",
        "rationale": "Long run without host charging requirement can utilize lightweight category extension.",
    },
    {
        "id": "RULE-SURFACE-DUAL-RUN",
        "name": "Surface trunking with discrete HDMI and USB",
        "match": {
            "containment": "surface",
            "allow_usbc": False,
            "distance_max_m": 15.0,
        },
        "outcome": "HDMI_PLUS_USB",
        "rationale": "Surface raceway/trunking accommodates dual discrete cable runs for legacy PC and display.",
    },
    {
        "id": "RULE-DEFAULT-FALLBACK",
        "name": "General fallback to Category Extender",
        "match": {},
        "outcome": "CAT_EXTENDER_GENERIC",
        "rationale": "Standard high-reliability commercial AV topology suitable for typical conference spaces.",
    },
]


# ---------------------------------------------------------------------------
# Decision Evaluation Functions
# ---------------------------------------------------------------------------


def normalize_signal_inputs(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize caller parameters into strict matching attributes."""
    containment = str(params.get("containment") or "").strip().lower()
    if containment not in ("none", "surface", "in_wall", "floor_box"):
        containment = "in_wall" if containment else "unknown"

    altmode = params.get("altmode")
    if altmode is None:
        altmode_bool = True
    elif isinstance(altmode, str):
        altmode_bool = altmode.strip().lower() in (
            "yes",
            "true",
            "1",
            "dp",
            "dp_altmode",
            "thunderbolt",
        )
    else:
        altmode_bool = bool(altmode)

    allow_usbc = params.get("allow_usbc")
    if allow_usbc is None:
        allow_usbc_bool = True
    else:
        allow_usbc_bool = bool(allow_usbc)

    distance_m = params.get("distance_m")
    try:
        dist = float(distance_m) if distance_m is not None else 5.0
    except (ValueError, TypeError):
        dist = 5.0

    laptop_watt = params.get("laptop_watt")
    try:
        watt = float(laptop_watt) if laptop_watt is not None else 65.0
    except (ValueError, TypeError):
        watt = 65.0

    wants_wireless = bool(params.get("wants_wireless", False))
    wants_charging = bool(params.get("wants_charging", True))
    vendor_eco = str(params.get("vendor_ecosystem") or "").strip().lower()

    return {
        "containment": containment,
        "altmode": altmode_bool,
        "allow_usbc": allow_usbc_bool,
        "distance_m": dist,
        "wants_wireless": wants_wireless,
        "wants_charging": wants_charging,
        "laptop_watt": watt,
        "vendor_ecosystem": vendor_eco,
    }


def _rule_matches(rule_match: dict[str, Any], norm: dict[str, Any]) -> bool:
    """Evaluate whether rule criteria are satisfied by normalized inputs."""
    for k, v in rule_match.items():
        if k == "distance_max_m":
            if norm["distance_m"] > float(v):
                return False
        elif k == "distance_min_m":
            if norm["distance_m"] < float(v):
                return False
        elif k in norm:
            if norm[k] != v:
                return False
    return True


def evaluate_signal_distribution(params: dict[str, Any]) -> dict[str, Any]:
    """Pure evaluation of signal distribution rules for meeting room parameters."""
    norm = normalize_signal_inputs(params)
    roles_map = {r["role"]: r for r in SIGNAL_ROLES}

    matched_rule = None
    for r in SIGNAL_RULES:
        if _rule_matches(r.get("match", {}), norm):
            matched_rule = r
            break

    if not matched_rule:
        matched_rule = SIGNAL_RULES[-1]  # fallback

    primary_role = matched_rule["outcome"]
    role_info = roles_map.get(primary_role, {})

    secondary_roles: list[str] = []
    if norm["wants_wireless"] and primary_role != "WIRELESS":
        secondary_roles.append("WIRELESS")

    power_status = "sufficient"
    if norm["wants_charging"]:
        if not role_info.get("supports_charging", False):
            power_status = "unsupported"
        elif norm["laptop_watt"] > 85.0 and primary_role in ("USBC_DIRECT", "USBC_ACTIVE_EXTENDER"):
            power_status = "warning_high_wattage"

    return {
        "recommended_role": primary_role,
        "role_details": role_info,
        "secondary_roles": secondary_roles,
        "power_delivery_status": power_status,
        "required_cable_category": role_info.get("cable_category", "Category 6A"),
        "max_certified_distance_m": role_info.get("max_distance_m", 50.0),
        "rationale": matched_rule.get("rationale", ""),
        "rule_id": matched_rule.get("id"),
        "normalized_inputs": norm,
    }


def do_get_signal_rules(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Public core: return curated signal distribution rules and roles, or evaluate when inputs provided."""
    if params and any(
        k in params
        for k in ("containment", "distance_m", "altmode", "wants_wireless", "wants_charging")
    ):
        return evaluate_signal_distribution(params)
    return {
        "roles": SIGNAL_ROLES,
        "rules": SIGNAL_RULES,
        "total_rules": len(SIGNAL_RULES),
        "total_roles": len(SIGNAL_ROLES),
    }
