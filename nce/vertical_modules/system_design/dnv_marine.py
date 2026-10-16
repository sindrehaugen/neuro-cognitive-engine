"""
nce/vertical_modules/system_design/dnv_marine.py
=================================================
Maritime & Offshore Electrical, Cabling, Hazardous Area, and Environmental
Compliance Engine (Wave E-2).

Rules & Standards Enforced:
---------------------------
- Maritime Electrical & Busbars:
  * DNV-RU-SHIP Pt.4 Ch.8 (Electrical installations)
  * DNV-RU-SHIP Pt.4 Ch.9 (Control and monitoring systems)
  * NEK 410 / IEC 60092-201 / IEC 60092-101 (Electrical installations in ships)
  * SOLAS II-1/42 & 43 (Emergency source of electrical power)
- Offshore & Industrial Electrical:
  * NORSOK E-001 (Electrical systems)
- Marine Cabling:
  * IEC 60092-350 / IEC 60092-360 (Sheathing materials: SHF1 / SHF2)
  * IEC 60332-3-22 (Flame retardance for bunched cables, Cat A)
  * IEC 60331-1 / IEC 60331-21 (Fire-resistant cables for emergency circuits, e.g. BFOU)
- Hazardous Areas (Ex):
  * ATEX Directive 2014/34/EU
  * EN 60079-0 / EN 60079-14 / NEK 420 (Explosive atmospheres, EPL & T-classes)
- Ingress, Mechanical & Corrosion Protection:
  * EN 60529 (IP codes)
  * EN 62262 (IK codes for mechanical impact)
  * ISO 12944 (Corrosion protection classes C1 to CX)
- Environmental Testing:
  * DNV-CG-0339 (Environmental test specification for electrical, electronic
    and programmable equipment: Temperature, Humidity, Vibration, EMC, Enclosure)

Invariants:
-----------
- Pure deterministic domain logic: zero network, zero database, zero external dependencies.
- Normalized input parsing accepting various case formats and acronyms.
- Clear error violations list and warning notifications.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Constants & Reference Tables
# ---------------------------------------------------------------------------

# THD Limits (DNV-RU-SHIP Pt.4 Ch.8 Sec.2, IEC 60092-101)
DNV_MAX_THD_VOLTAGE_PCT: float = 8.0
DNV_GENERAL_DISTRIBUTION_THD_PCT: float = 5.0

# Minimum UPS Autonomy Requirements (seconds)
# General essential consumers: 30 minutes (1800 s)
UPS_AUTONOMY_ESSENTIAL_SEC: float = 1800.0
# Emergency lighting and navigation aids: 18 hours (64800 s) per DNV / SOLAS
UPS_AUTONOMY_EMERGENCY_LIGHTING_SEC: float = 64800.0

# Earthing Systems
PROHIBITED_EARTHING_SYSTEMS: frozenset[str] = frozenset({"TN-C", "TNC", "PEN_HULL"})

IT_EARTHING_SYSTEMS: frozenset[str] = frozenset(
    {
        "IT",
        "MARINE_IT_230V",
        "MARINE_IT_440V",
        "MARINE_IT_690V",
        "IT_230V_3P",
        "IT_400V_3P",
        "IT_440V_3P",
        "IT_690V_3P",
        "IT_SYSTEM",
    }
)

# Cable Sheaths (IEC 60092-350 / 360, NEK 410)
VALID_MARINE_SHEATHS: frozenset[str] = frozenset({"SHF1", "SHF2", "SHF 1", "SHF 2"})

# Equipment Protection Levels (EPL) per Zone (EN 60079-0 / ATEX 2014/34/EU)
ZONE_PERMISSIBLE_EPLS: dict[str, set[str]] = {
    # Gas Zones
    "zone_0": {"Ga"},
    "0": {"Ga"},
    "zone_1": {"Ga", "Gb"},
    "1": {"Ga", "Gb"},
    "zone_2": {"Ga", "Gb", "Gc"},
    "2": {"Ga", "Gb", "Gc"},
    # Dust Zones
    "zone_20": {"Da"},
    "20": {"Da"},
    "zone_21": {"Da", "Db"},
    "21": {"Da", "Db"},
    "zone_22": {"Da", "Db", "Dc"},
    "22": {"Da", "Db", "Dc"},
}

ATEX_CAT_TO_EPL: dict[str, str] = {
    "1G": "Ga",
    "2G": "Gb",
    "3G": "Gc",
    "1D": "Da",
    "2D": "Db",
    "3D": "Dc",
    "M1": "Ma",
    "M2": "Mb",
}

# Temperature Classes: rank 6 (safest, lowest surface temp 85°C) down to 1 (450°C)
TEMP_CLASS_RANKINGS: dict[str, int] = {
    "T6": 6,
    "T5": 5,
    "T4": 4,
    "T3": 3,
    "T2": 2,
    "T1": 1,
}

# ISO 12944 Atmospheric Corrosivity Categories
ISO_12944_CORROSION_RANKS: dict[str, int] = {
    "C1": 1,
    "C2": 2,
    "C3": 3,
    "C4": 4,
    "C5": 5,
    "C5-I": 5,
    "C5-M": 5,
    "C5M": 5,
    "CX": 6,
}

# Location Preset Minimum Requirements
LOCATION_ENVIRONMENT_PRESETS: dict[str, dict[str, str]] = {
    "dry_indoor": {
        "min_ip_rating": "IP20",
        "min_ik_rating": "IK02",
        "min_corrosion_class": "C1",
    },
    "wet_room": {
        "min_ip_rating": "IP44",
        "min_ik_rating": "IK06",
        "min_corrosion_class": "C3",
    },
    "machinery_space": {
        "min_ip_rating": "IP44",
        "min_ik_rating": "IK07",
        "min_corrosion_class": "C3",
    },
    "open_deck": {
        "min_ip_rating": "IP56",
        "min_ik_rating": "IK08",
        "min_corrosion_class": "C5",
    },
    "offshore_splash_zone": {
        "min_ip_rating": "IP66",
        "min_ik_rating": "IK10",
        "min_corrosion_class": "CX",
    },
}

# DNV-CG-0339 Environmental Classes Ranking
DNV_CG_0339_RANKS: dict[str, dict[str, int]] = {
    "Temperature": {"A": 1, "B": 2, "C": 3, "D": 4},
    "Humidity": {"A": 1, "B": 2},
    "Vibration": {"A": 1, "B": 2},
    "EMC": {"A": 1, "B": 2},
    "Enclosure": {"A": 1, "B": 2, "C": 3, "D": 4},
}


# ---------------------------------------------------------------------------
# Helper Parsers
# ---------------------------------------------------------------------------


def _parse_ip_code(ip_str: str) -> tuple[int, int]:
    """Parse an IP rating string into (solids_digit, liquids_digit)."""
    s = str(ip_str or "").strip().upper()
    m = re.search(r"IP\s*([0-6X])([0-9X])", s)
    if not m:
        return 0, 0
    d1_raw, d2_raw = m.group(1), m.group(2)
    d1 = 0 if d1_raw == "X" else int(d1_raw)
    d2 = 0 if d2_raw == "X" else int(d2_raw)
    return d1, d2


def _parse_ik_code(ik_str: str) -> int:
    """Parse an IK code string into impact resistance level (0 to 10)."""
    s = str(ik_str or "").strip().upper()
    m = re.search(r"IK\s*([0-9]{1,2})", s)
    if m:
        return int(m.group(1))
    # If passed as bare integer
    try:
        return int(s)
    except ValueError:
        return 0


def _parse_autonomy_seconds(consumer: dict[str, Any]) -> float:
    """Extract UPS autonomy duration in seconds from various common keys."""
    if "ups_autonomy_seconds" in consumer:
        return float(consumer["ups_autonomy_seconds"])
    if "ups_autonomy_sec" in consumer:
        return float(consumer["ups_autonomy_sec"])
    if "ups_autonomy_hours" in consumer:
        return float(consumer["ups_autonomy_hours"]) * 3600.0
    if "ups_autonomy_hr" in consumer:
        return float(consumer["ups_autonomy_hr"]) * 3600.0
    if "ups_autonomy_minutes" in consumer:
        return float(consumer["ups_autonomy_minutes"]) * 60.0
    if "ups_autonomy_min" in consumer:
        return float(consumer["ups_autonomy_min"]) * 60.0
    if "ups_autonomy" in consumer:
        val = consumer["ups_autonomy"]
        try:
            num = float(val)
            # If value is >= 120, treat as seconds, else minutes
            return num if num >= 120.0 else num * 60.0
        except (ValueError, TypeError):
            pass
    return 0.0


# ---------------------------------------------------------------------------
# Validation Functions
# ---------------------------------------------------------------------------


def validate_dnv_switchboard_and_bus(
    earthing_system: str,
    has_imd: bool,
    msb_location_id: str,
    esb_location_id: str,
    essential_consumers: list[dict[str, Any]],
    thd_voltage_pct: float = 0.0,
) -> dict[str, Any]:
    """Validate maritime switchboard, earthing, essential bus feeds, and power quality.

    Enforces DNV-RU-SHIP Pt.4 Ch.8 & Ch.9, NEK 410 / IEC 60092-201, and SOLAS II-1.
    """
    violations: list[str] = []
    warnings: list[str] = []

    clean_earthing = str(earthing_system or "").strip().upper()

    # 1. Reject TN-C (PEN conductor / hull return)
    if (
        clean_earthing in PROHIBITED_EARTHING_SYSTEMS
        or "TN-C" in clean_earthing
        or "TNC" in clean_earthing
    ):
        violations.append(
            f"Earthing system '{earthing_system}' is prohibited on marine vessels: "
            "PEN conductor / hull return is forbidden per DNV-RU-SHIP Pt.4 Ch.8 Sec.2 and IEC 60092-201."
        )

    # 2. Require IMD on insulated IT systems
    is_it_system = clean_earthing in IT_EARTHING_SYSTEMS or clean_earthing.startswith("IT")
    if is_it_system and not has_imd:
        violations.append(
            f"Insulated IT system '{earthing_system}' requires an active Insulation "
            "Monitoring Device (IMD) per DNV-RU-SHIP Pt.4 Ch.8 Sec.2 and IEC 60092-201."
        )

    # 3. Require distinct fire zones/compartments for MSB and ESB
    clean_msb = str(msb_location_id or "").strip()
    clean_esb = str(esb_location_id or "").strip()
    if not clean_msb or not clean_esb or clean_msb == clean_esb:
        violations.append(
            f"Main Switchboard (MSB: '{clean_msb}') and Emergency Switchboard (ESB: '{clean_esb}') "
            "must be located in distinct fire zones and separate compartments "
            "(DNV-RU-SHIP Pt.4 Ch.8 Sec.2 / SOLAS II-1/42-43)."
        )

    # 4. Essential consumers dual feed and UPS autonomy check
    consumers_evaluated: list[dict[str, Any]] = []
    for idx, c in enumerate(essential_consumers):
        name = str(
            c.get("name") or c.get("consumer_id") or c.get("service") or f"consumer_{idx + 1}"
        )
        service_type = str(c.get("service_type") or c.get("type") or name).lower()
        feed_count = int(c.get("feed_count") or 1)
        has_emergency_feed = bool(c.get("has_emergency_feed") or False)
        autonomy_sec = _parse_autonomy_seconds(c)

        # Dual feed check
        feed_ok = feed_count >= 2 or has_emergency_feed
        if not feed_ok:
            violations.append(
                f"Essential consumer '{name}' requires dual independent feeds or emergency switchboard supply "
                f"(feed_count={feed_count}, has_emergency_feed={has_emergency_feed}) per DNV-RU-SHIP Pt.4 Ch.8."
            )

        # UPS autonomy check
        is_emergency_lighting_or_nav = any(
            kw in service_type
            for kw in (
                "emergency_lighting",
                "emergency lighting",
                "navigation",
                "nav_lights",
                "radio",
                "gmdss",
            )
        ) or bool(c.get("is_emergency_lighting"))

        required_autonomy_sec = (
            UPS_AUTONOMY_EMERGENCY_LIGHTING_SEC
            if is_emergency_lighting_or_nav
            else UPS_AUTONOMY_ESSENTIAL_SEC
        )

        autonomy_ok = autonomy_sec >= required_autonomy_sec
        if not autonomy_ok:
            violations.append(
                f"Essential consumer '{name}' UPS autonomy ({autonomy_sec:.0f}s) is below required "
                f"{required_autonomy_sec:.0f}s ({required_autonomy_sec / 3600:.1f}h) per DNV-RU-SHIP Pt.4 Ch.8."
            )

        consumers_evaluated.append(
            {
                "name": name,
                "service_type": service_type,
                "feed_ok": feed_ok,
                "autonomy_sec": autonomy_sec,
                "required_autonomy_sec": required_autonomy_sec,
                "autonomy_ok": autonomy_ok,
            }
        )

    # 5. Total Harmonic Distortion (THD) checks
    thd_ok = True
    if thd_voltage_pct > DNV_MAX_THD_VOLTAGE_PCT:
        thd_ok = False
        violations.append(
            f"Voltage THD ({thd_voltage_pct:.1f}%) exceeds maximum marine limit of "
            f"{DNV_MAX_THD_VOLTAGE_PCT:.1f}% per DNV-RU-SHIP Pt.4 Ch.8 Sec.2 and IEC 60092-101."
        )
    elif thd_voltage_pct > DNV_GENERAL_DISTRIBUTION_THD_PCT:
        warnings.append(
            f"Voltage THD ({thd_voltage_pct:.1f}%) exceeds {DNV_GENERAL_DISTRIBUTION_THD_PCT:.1f}% "
            "general distribution limit; only permissible for dedicated consumer zones."
        )

    return {
        "compliant": len(violations) == 0,
        "earthing_system": earthing_system,
        "has_imd": has_imd,
        "msb_location_id": msb_location_id,
        "esb_location_id": esb_location_id,
        "thd_voltage_pct": thd_voltage_pct,
        "thd_ok": thd_ok,
        "violations": violations,
        "warnings": warnings,
        "essential_consumer_count": len(essential_consumers),
        "details": {
            "earthing_ok": clean_earthing not in PROHIBITED_EARTHING_SYSTEMS
            and "TN-C" not in clean_earthing
            and (not is_it_system or has_imd),
            "switchboard_separation_ok": bool(clean_msb and clean_esb and clean_msb != clean_esb),
            "thd_voltage_pct": thd_voltage_pct,
            "consumers_evaluated": consumers_evaluated,
        },
    }


def validate_marine_cable_iec60092(
    cable_spec: dict[str, Any],
    is_emergency_circuit: bool = False,
) -> dict[str, Any]:
    """Validate marine cable specification against IEC 60092-350/360, IEC 60332-3, and IEC 60331.

    Enforces halogen-free sheath (SHF1/SHF2), flame retardancy, and fire resistance.
    """
    violations: list[str] = []

    sheath = (
        str(
            cable_spec.get("sheath_type")
            or cable_spec.get("sheath")
            or cable_spec.get("outer_sheath")
            or ""
        )
        .strip()
        .upper()
    )

    cable_type = (
        str(
            cable_spec.get("cable_type")
            or cable_spec.get("type")
            or cable_spec.get("part_no")
            or ""
        )
        .strip()
        .upper()
    )

    standards_str = str(cable_spec.get("standards") or "").upper()
    flame_rating = str(cable_spec.get("flame_rating") or "").upper()

    # 1. Halogen-free sheath check (SHF1 / SHF2 per IEC 60092-350 / IEC 60092-360)
    is_shf = sheath in VALID_MARINE_SHEATHS or any(s in sheath for s in ("SHF1", "SHF2"))
    is_halogen_free = bool(
        cable_spec.get("halogen_free")
        or cable_spec.get("lszh")
        or is_shf
        or "IEC 60092-360" in standards_str
    )

    if not is_shf and not is_halogen_free:
        violations.append(
            f"Cable sheath '{sheath or 'unspecified'}' is not recognized as halogen-free (SHF1 or SHF2) "
            "per IEC 60092-350 / IEC 60092-360 / NEK 410."
        )

    # 2. Bundled flame retardancy (IEC 60332-3-22 Cat A)
    is_flame_retardant = bool(
        cable_spec.get("flame_retardant")
        or cable_spec.get("iec_60332_3_22")
        or "IEC 60332-3-22" in standards_str
        or "IEC 60332-3" in standards_str
        or "CAT A" in flame_rating
        or "CATEGORY A" in flame_rating
        or "IEC 60332-3-22" in flame_rating
    )
    if not is_flame_retardant:
        violations.append(
            "Marine cable must be certified flame retardant for bunched cables per IEC 60332-3-22 (Category A)."
        )

    # 3. Fire resistance for emergency circuits (IEC 60331)
    is_fire_resistant = bool(
        cable_spec.get("fire_resistant")
        or cable_spec.get("iec_60331")
        or "IEC 60331" in standards_str
        or "BFOU" in cable_type
        or "MGCG-FR" in cable_type
        or "RFOU-FR" in cable_type
    )

    if is_emergency_circuit and not is_fire_resistant:
        violations.append(
            "Emergency circuit requires fire-resistant cable certified to IEC 60331 (e.g. BFOU or MGCG-FR) "
            "to maintain circuit integrity under fire conditions."
        )

    return {
        "compliant": len(violations) == 0,
        "is_emergency_circuit": is_emergency_circuit,
        "cable_type": cable_type,
        "sheath_type": sheath,
        "halogen_free": is_halogen_free,
        "flame_retardant": is_flame_retardant,
        "fire_resistant": is_fire_resistant,
        "violations": violations,
        "details": {
            "iec_60092_sheath_ok": is_shf or is_halogen_free,
            "iec_60332_flame_ok": is_flame_retardant,
            "iec_60331_fire_res_ok": not is_emergency_circuit or is_fire_resistant,
        },
    }


def validate_atex_and_environment(
    device_spec: dict[str, Any],
    location_env: dict[str, Any],
) -> dict[str, Any]:
    """Validate equipment against ATEX 2014/34/EU, EN 60079, EN 60529 (IP), EN 62262 (IK), and ISO 12944.

    Evaluates Equipment Protection Level (EPL), gas/dust temperature classes, IP rating,
    IK impact resistance, and corrosion classification.
    """
    violations: list[str] = []
    warnings: list[str] = []

    # 1. Resolve Location Preset & Requirements
    env_type = str(location_env.get("type") or location_env.get("environment") or "").lower()
    preset = LOCATION_ENVIRONMENT_PRESETS.get(env_type, {})

    req_ip = str(
        location_env.get("min_ip_rating")
        or location_env.get("required_ip")
        or preset.get("min_ip_rating")
        or "IP20"
    ).upper()
    req_ik = str(
        location_env.get("min_ik_rating")
        or location_env.get("required_ik")
        or preset.get("min_ik_rating")
        or "IK02"
    ).upper()
    req_corrosion = str(
        location_env.get("min_corrosion_class")
        or location_env.get("required_corrosion_class")
        or preset.get("min_corrosion_class")
        or "C1"
    ).upper()

    # 2. ATEX & Hazardous Zone Checks
    ex_zone_raw = location_env.get("ex_zone") or location_env.get("zone")
    atex_details: dict[str, Any] = {"required": bool(ex_zone_raw)}

    if ex_zone_raw:
        clean_zone = str(ex_zone_raw).strip().lower()
        if not clean_zone.startswith("zone_") and clean_zone in {"0", "1", "2", "20", "21", "22"}:
            clean_zone = f"zone_{clean_zone}"

        permissible_epls = ZONE_PERMISSIBLE_EPLS.get(clean_zone, set())

        # Resolve device EPL
        device_epl = str(
            device_spec.get("epl")
            or device_spec.get("atex_epl")
            or device_spec.get("equipment_protection_level")
            or ""
        ).strip()

        # If not direct, check ATEX category (e.g. 1G, 2G, 3G, 1D, 2D, 3D)
        if not device_epl:
            category = (
                str(device_spec.get("atex_category") or device_spec.get("category") or "")
                .strip()
                .upper()
            )
            if category in ATEX_CAT_TO_EPL:
                device_epl = ATEX_CAT_TO_EPL[category]

        # Or parse from atex_marking (e.g. "II 2 G Ex db IIC T4 Gb")
        atex_marking = str(device_spec.get("atex_marking") or "").strip()
        if not device_epl and atex_marking:
            m_epl = re.search(r"\b(G[abc]|D[abc]|M[ab])\b", atex_marking)
            if m_epl:
                device_epl = m_epl.group(1)
            else:
                m_cat = re.search(r"\b([123][GD])\b", atex_marking)
                if m_cat and m_cat.group(1) in ATEX_CAT_TO_EPL:
                    device_epl = ATEX_CAT_TO_EPL[m_cat.group(1)]

        atex_details["zone"] = clean_zone
        atex_details["device_epl"] = device_epl
        atex_details["permissible_epls"] = sorted(list(permissible_epls))

        if not device_epl:
            violations.append(
                f"Hazardous area {clean_zone} requires certified Ex equipment with EPL in "
                f"{sorted(list(permissible_epls))}, but device has no certified EPL."
            )
        elif device_epl not in permissible_epls:
            violations.append(
                f"Device EPL '{device_epl}' is insufficient for {clean_zone}. "
                f"Permissible EPLs are {sorted(list(permissible_epls))} per EN 60079-0 / ATEX 2014/34/EU."
            )

        # Temperature Class Check (T1 to T6)
        req_t_class = (
            str(location_env.get("required_temp_class") or location_env.get("min_temp_class") or "")
            .strip()
            .upper()
        )

        dev_t_class = (
            str(
                device_spec.get("temperature_class")
                or device_spec.get("t_class")
                or device_spec.get("temp_class")
                or ""
            )
            .strip()
            .upper()
        )

        if not dev_t_class and atex_marking:
            m_t = re.search(r"\b(T[1-6])\b", atex_marking)
            if m_t:
                dev_t_class = m_t.group(1)

        if req_t_class:
            dev_rank = TEMP_CLASS_RANKINGS.get(dev_t_class, 0)
            req_rank = TEMP_CLASS_RANKINGS.get(req_t_class, 0)
            atex_details["device_t_class"] = dev_t_class
            atex_details["required_t_class"] = req_t_class

            if dev_rank < req_rank:
                violations.append(
                    f"Device temperature class '{dev_t_class or 'unspecified'}' is less protective than "
                    f"required '{req_t_class}' for hazardous area {clean_zone} (max surface temperature exceeds limit)."
                )

    # 3. EN 60529 IP Rating Check
    dev_ip_str = str(
        device_spec.get("ip_rating") or device_spec.get("ip") or device_spec.get("enclosure") or ""
    ).upper()
    d_solids, d_liquids = _parse_ip_code(dev_ip_str)
    r_solids, r_liquids = _parse_ip_code(req_ip)

    ip_ok = d_solids >= r_solids and d_liquids >= r_liquids
    if not ip_ok:
        violations.append(
            f"Device IP rating '{dev_ip_str or 'unrated'}' does not meet required '{req_ip}' "
            f"(solids {d_solids} < {r_solids} or liquids {d_liquids} < {r_liquids}) per EN 60529."
        )

    # 4. EN 62262 IK Rating Check
    dev_ik_str = str(device_spec.get("ik_rating") or device_spec.get("ik") or "IK00").upper()
    dev_ik_val = _parse_ik_code(dev_ik_str)
    req_ik_val = _parse_ik_code(req_ik)

    ik_ok = dev_ik_val >= req_ik_val
    if not ik_ok:
        violations.append(
            f"Device IK impact rating '{dev_ik_str}' ({dev_ik_val}) is lower than required "
            f"'{req_ik}' ({req_ik_val}) per EN 62262."
        )

    # 5. ISO 12944 Corrosion Class Check
    dev_corr = str(
        device_spec.get("corrosion_class")
        or device_spec.get("iso12944_class")
        or device_spec.get("corrosivity")
        or "C1"
    ).upper()

    dev_corr_rank = ISO_12944_CORROSION_RANKS.get(dev_corr, 1)
    req_corr_rank = ISO_12944_CORROSION_RANKS.get(req_corrosion, 1)

    corr_ok = dev_corr_rank >= req_corr_rank
    if not corr_ok:
        violations.append(
            f"Device corrosion protection class '{dev_corr}' (rank {dev_corr_rank}) does not meet "
            f"required '{req_corrosion}' (rank {req_corr_rank}) per ISO 12944."
        )

    return {
        "compliant": len(violations) == 0,
        "environment_type": env_type,
        "violations": violations,
        "warnings": warnings,
        "details": {
            "atex": atex_details,
            "ip": {
                "device_ip": dev_ip_str,
                "required_ip": req_ip,
                "ok": ip_ok,
            },
            "ik": {
                "device_ik": dev_ik_str,
                "required_ik": req_ik,
                "ok": ik_ok,
            },
            "corrosion": {
                "device_corrosion": dev_corr,
                "required_corrosion": req_corrosion,
                "ok": corr_ok,
            },
        },
    }


def validate_dnv_location_classes(
    device_classes: dict[str, str],
    required_classes: dict[str, str],
) -> dict[str, Any]:
    """Validate environmental test location classes against DNV-CG-0339.

    Categories:
      - Temperature: A, B, C, D
      - Humidity: A, B
      - Vibration: A, B
      - EMC: A, B
      - Enclosure: A, B, C, D
    """
    violations: list[str] = []
    comparison: dict[str, Any] = {}

    for param, ranking_table in DNV_CG_0339_RANKS.items():
        req_val = str(required_classes.get(param) or "").strip().upper()
        if not req_val:
            continue

        dev_val = str(device_classes.get(param) or "").strip().upper()
        req_rank = ranking_table.get(req_val, 0)
        dev_rank = ranking_table.get(dev_val, 0)

        param_ok = dev_rank >= req_rank and dev_rank > 0
        comparison[param] = {
            "device": dev_val,
            "required": req_val,
            "device_rank": dev_rank,
            "required_rank": req_rank,
            "ok": param_ok,
        }

        if not param_ok:
            violations.append(
                f"DNV-CG-0339 {param} class '{dev_val or 'none'}' does not satisfy required class '{req_val}'."
            )

    return {
        "compliant": len(violations) == 0,
        "violations": violations,
        "device_classes": device_classes,
        "required_classes": required_classes,
        "details": comparison,
    }
