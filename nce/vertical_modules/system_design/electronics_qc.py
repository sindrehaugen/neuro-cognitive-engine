"""
nce/vertical_modules/system_design/electronics_qc.py
===================================================
Electronics R&D, PCBA design validation, and EMS/Box-Build quality control
module adhering to European & international engineering standards:
  - ECSS-Q-ST-30-11C (Derating - EEE components)
  - IEC 61508 (Functional safety of electrical/electronic/programmable electronic safety-related systems)
  - ISO 26262 (Road vehicles - Functional safety)
  - EU RoHS 3 (Directive 2011/65/EU & Delegated Directive EU 2015/863 - 10 restricted substances)
  - EU REACH (Regulation EC 1907/2006 - SVHC candidate list)
  - IPC/JEDEC J-STD-033 (Handling, Packing, Shipping and Use of Moisture, Reflow, and Process Sensitive Devices)
  - EN 61340-5-1 / IEC 61340-5-1 (Protection of electronic devices from electrostatic phenomena - General requirements)
  - IPC-A-610 / IPC/WHMA-A-620 (Acceptability of Electronic Assemblies & Cable/Wire Harness Assemblies)

Deterministic engineering calculations, pure functional evaluation, zero external I/O.
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("nce.vertical_modules.system_design.electronics_qc")

# ---------------------------------------------------------------------------
# Standard Reference Metadata & Thresholds
# ---------------------------------------------------------------------------

ROHS3_RESTRICTED_SUBSTANCES: dict[str, dict[str, Any]] = {
    "lead": {"symbol": "Pb", "max_pct": 0.1, "max_ppm": 1000.0},
    "mercury": {"symbol": "Hg", "max_pct": 0.1, "max_ppm": 1000.0},
    "cadmium": {"symbol": "Cd", "max_pct": 0.01, "max_ppm": 100.0},
    "hexavalent_chromium": {"symbol": "Cr6+", "max_pct": 0.1, "max_ppm": 1000.0},
    "polybrominated_biphenyls": {"symbol": "PBB", "max_pct": 0.1, "max_ppm": 1000.0},
    "polybrominated_diphenyl_ethers": {"symbol": "PBDE", "max_pct": 0.1, "max_ppm": 1000.0},
    "bis_2_ethylhexyl_phthalate": {"symbol": "DEHP", "max_pct": 0.1, "max_ppm": 1000.0},
    "butyl_benzyl_phthalate": {"symbol": "BBP", "max_pct": 0.1, "max_ppm": 1000.0},
    "dibutyl_phthalate": {"symbol": "DBP", "max_pct": 0.1, "max_ppm": 1000.0},
    "diisobutyl_phthalate": {"symbol": "DIBP", "max_pct": 0.1, "max_ppm": 1000.0},
}

# Alias map for flexible substance resolution
_SUBSTANCE_ALIASES: dict[str, str] = {
    "pb": "lead",
    "lead": "lead",
    "hg": "mercury",
    "mercury": "mercury",
    "cd": "cadmium",
    "cadmium": "cadmium",
    "cr6+": "hexavalent_chromium",
    "cr6": "hexavalent_chromium",
    "cr(vi)": "hexavalent_chromium",
    "hexavalent_chromium": "hexavalent_chromium",
    "pbb": "polybrominated_biphenyls",
    "polybrominated_biphenyls": "polybrominated_biphenyls",
    "pbde": "polybrominated_diphenyl_ethers",
    "polybrominated_diphenyl_ethers": "polybrominated_diphenyl_ethers",
    "dehp": "bis_2_ethylhexyl_phthalate",
    "bbp": "butyl_benzyl_phthalate",
    "dbp": "dibutyl_phthalate",
    "dibp": "diisobutyl_phthalate",
}

# IPC/JEDEC J-STD-033 Floor Life limits (at <= 30Â°C / 60% RH or 85% RH for Level 1)
MSL_FLOOR_LIFE_HOURS: dict[str, float] = {
    "1": math.inf,
    "MSL 1": math.inf,
    "2": 8760.0,
    "MSL 2": 8760.0,
    "2A": 672.0,
    "MSL 2A": 672.0,
    "3": 168.0,
    "MSL 3": 168.0,
    "4": 72.0,
    "MSL 4": 72.0,
    "5": 48.0,
    "MSL 5": 48.0,
    "5A": 24.0,
    "MSL 5A": 24.0,
    "6": 0.0,
    "MSL 6": 0.0,
}

# EN 61340-5-1 ESD Protected Area (EPA) Limits
ESD_EPA_LIMITS = {
    "wrist_strap_min_mohm": 0.75,
    "wrist_strap_max_mohm": 35.0,
    "mat_to_ground_max_ohm": 1.0e9,
    "footwear_max_ohm": 1.0e8,
    "relative_humidity_min_pct": 30.0,
}

TIGHTENED_SAFETY_CLASSES = frozenset(
    {
        "automotive_iso26262",
        "safety_iec61508",
        "maritime_dnv",
        "aerospace_ecss",
        "automotive",
        "safety",
        "maritime",
    }
)


# ---------------------------------------------------------------------------
# 1. Component Derating Validation
# ---------------------------------------------------------------------------


def validate_component_derating(
    components: list[dict[str, Any]],
    safety_class: str = "industrial",
) -> dict[str, Any]:
    """Validate electronic BOM components against stress derating standards.

    Under standards ECSS-Q-ST-30-11C, IEC 61508, and ISO 26262:
      - Voltage stress ratio S_V = V_applied / V_rated <= max_ratio
        (default 0.60 for capacitors, 0.75 for semiconductors, tightened by 0.10
        for automotive_iso26262 / safety_iec61508 / maritime_dnv).
      - Current/power stress ratio S_P = P_applied / P_rated <= max_ratio
        (default 0.50 for resistors, 0.70 for semiconductors/inductors).
      - Thermal junction temperature:
        T_j = T_ambient_c + P_applied * R_theta_JA <= T_j_max - delta_T_margin
        (where delta_T_margin = 15Â°C for industrial, 25Â°C for safety/automotive/marine).

    :param components: List of component dictionaries with rating and applied values.
    :param safety_class: Safety classification profile (e.g. 'industrial',
                         'automotive_iso26262', 'safety_iec61508', 'maritime_dnv').
    :return: Summary dict with passed status, counts, violations, and evaluated lines.
    """
    safety_clean = (safety_class or "industrial").strip().lower()
    is_tightened = safety_clean in TIGHTENED_SAFETY_CLASSES
    tighten_delta = 0.10 if is_tightened else 0.0
    thermal_margin = 25.0 if is_tightened else 15.0

    violations: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    for idx, comp in enumerate(components):
        part_number = str(comp.get("part_number") or comp.get("part_no") or f"COMP-{idx + 1}")
        raw_type = str(comp.get("type") or comp.get("category") or "generic").strip().lower()

        comp_violations: list[str] = []
        metrics: dict[str, Any] = {"part_number": part_number, "type": raw_type}

        # 1. Determine stress ratio limits based on component category
        if raw_type in ("capacitor", "cap"):
            max_v_ratio = 0.60 - tighten_delta
            max_p_ratio = 0.60 - tighten_delta
        elif raw_type in ("resistor", "res"):
            max_v_ratio = 0.75 - tighten_delta
            max_p_ratio = 0.50 - tighten_delta
        elif raw_type in ("semiconductor", "diode", "transistor", "mosfet", "bjt", "ic"):
            max_v_ratio = 0.75 - tighten_delta
            max_p_ratio = 0.70 - tighten_delta
        elif raw_type in ("inductor", "choke"):
            max_v_ratio = 0.75 - tighten_delta
            max_p_ratio = 0.70 - tighten_delta
        elif raw_type in ("connector", "header"):
            max_v_ratio = 0.75 - tighten_delta
            max_p_ratio = 0.70 - tighten_delta
        else:
            max_v_ratio = 0.75 - tighten_delta
            max_p_ratio = 0.70 - tighten_delta

        # 2. Voltage Stress Check
        v_applied = (
            comp.get("voltage_applied") if "voltage_applied" in comp else comp.get("v_applied")
        )
        v_rated = comp.get("voltage_rated") if "voltage_rated" in comp else comp.get("v_rated")
        if v_applied is not None and v_rated is not None:
            v_app_f = float(v_applied)
            v_rat_f = float(v_rated)
            if v_rat_f <= 0.0:
                msg = f"Rated voltage must be > 0 (got {v_rat_f})"
                comp_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "component_type": raw_type,
                        "parameter": "voltage",
                        "applied_value": v_app_f,
                        "rated_value": v_rat_f,
                        "stress_ratio": math.inf,
                        "max_allowed_ratio": round(max_v_ratio, 3),
                        "message": msg,
                    }
                )
            else:
                s_v = v_app_f / v_rat_f
                metrics["voltage_stress_ratio"] = round(s_v, 4)
                metrics["max_voltage_stress_ratio"] = round(max_v_ratio, 4)
                if s_v > max_v_ratio:
                    msg = (
                        f"Voltage stress ratio {s_v:.3f} exceeds max allowable "
                        f"{max_v_ratio:.3f} under safety class {safety_clean}"
                    )
                    comp_violations.append(msg)
                    violations.append(
                        {
                            "part_number": part_number,
                            "component_type": raw_type,
                            "parameter": "voltage",
                            "applied_value": v_app_f,
                            "rated_value": v_rat_f,
                            "stress_ratio": round(s_v, 4),
                            "max_allowed_ratio": round(max_v_ratio, 4),
                            "message": msg,
                        }
                    )

        # 3. Power or Current Stress Check
        p_applied = None
        for k in ("power_applied", "p_applied", "power_applied_w"):
            if k in comp:
                p_applied = comp[k]
                break

        p_rated = None
        for k in ("power_rated", "p_rated", "power_rated_w"):
            if k in comp:
                p_rated = comp[k]
                break

        i_applied = None
        for k in ("current_applied", "i_applied", "current_applied_a"):
            if k in comp:
                i_applied = comp[k]
                break

        i_rated = None
        for k in ("current_rated", "i_rated", "current_rated_a"):
            if k in comp:
                i_rated = comp[k]
                break

        if p_applied is not None and p_rated is not None:
            p_app_f = float(p_applied)
            p_rat_f = float(p_rated)
            if p_rat_f <= 0.0:
                msg = f"Rated power must be > 0 (got {p_rat_f})"
                comp_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "component_type": raw_type,
                        "parameter": "power",
                        "applied_value": p_app_f,
                        "rated_value": p_rat_f,
                        "stress_ratio": math.inf,
                        "max_allowed_ratio": round(max_p_ratio, 3),
                        "message": msg,
                    }
                )
            else:
                s_p = p_app_f / p_rat_f
                metrics["power_stress_ratio"] = round(s_p, 4)
                metrics["max_power_stress_ratio"] = round(max_p_ratio, 4)
                if s_p > max_p_ratio:
                    msg = (
                        f"Power stress ratio {s_p:.3f} exceeds max allowable "
                        f"{max_p_ratio:.3f} under safety class {safety_clean}"
                    )
                    comp_violations.append(msg)
                    violations.append(
                        {
                            "part_number": part_number,
                            "component_type": raw_type,
                            "parameter": "power",
                            "applied_value": p_app_f,
                            "rated_value": p_rat_f,
                            "stress_ratio": round(s_p, 4),
                            "max_allowed_ratio": round(max_p_ratio, 4),
                            "message": msg,
                        }
                    )
        elif i_applied is not None and i_rated is not None:
            i_app_f = float(i_applied)
            i_rat_f = float(i_rated)
            if i_rat_f <= 0.0:
                msg = f"Rated current must be > 0 (got {i_rat_f})"
                comp_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "component_type": raw_type,
                        "parameter": "current",
                        "applied_value": i_app_f,
                        "rated_value": i_rat_f,
                        "stress_ratio": math.inf,
                        "max_allowed_ratio": round(max_p_ratio, 3),
                        "message": msg,
                    }
                )
            else:
                s_i = i_app_f / i_rat_f
                metrics["current_stress_ratio"] = round(s_i, 4)
                metrics["max_current_stress_ratio"] = round(max_p_ratio, 4)
                if s_i > max_p_ratio:
                    msg = (
                        f"Current stress ratio {s_i:.3f} exceeds max allowable "
                        f"{max_p_ratio:.3f} under safety class {safety_clean}"
                    )
                    comp_violations.append(msg)
                    violations.append(
                        {
                            "part_number": part_number,
                            "component_type": raw_type,
                            "parameter": "current",
                            "applied_value": i_app_f,
                            "rated_value": i_rat_f,
                            "stress_ratio": round(s_i, 4),
                            "max_allowed_ratio": round(max_p_ratio, 4),
                            "message": msg,
                        }
                    )

        # 4. Thermal Junction Temperature Check
        r_theta = comp.get("r_theta_ja") or comp.get("thermal_resistance_ja") or comp.get("r_th_ja")
        tj_max_val = comp.get("tj_max") or comp.get("t_j_max") or comp.get("max_junction_temp_c")
        if r_theta is not None and tj_max_val is not None:
            r_th_f = float(r_theta)
            tj_max_f = float(tj_max_val)
            t_amb_f = float(
                comp.get("t_ambient_c")
                or comp.get("ambient_temp_c")
                or comp.get("t_ambient")
                or 25.0
            )
            # Power dissipated in component
            p_therm = 0.0
            if p_applied is not None:
                p_therm = float(p_applied)
            elif v_applied is not None and i_applied is not None:
                p_therm = float(v_applied) * float(i_applied)

            t_j = t_amb_f + (p_therm * r_th_f)
            t_j_allowed = tj_max_f - thermal_margin
            metrics["t_junction_c"] = round(t_j, 2)
            metrics["t_junction_max_allowed_c"] = round(t_j_allowed, 2)

            if t_j > t_j_allowed:
                msg = (
                    f"Calculated junction temp T_j={t_j:.1f}Â°C exceeds max allowable "
                    f"derated limit {t_j_allowed:.1f}Â°C (T_j,max={tj_max_f:.1f}Â°C - "
                    f"margin {thermal_margin:.1f}Â°C for {safety_clean})"
                )
                comp_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "component_type": raw_type,
                        "parameter": "thermal_junction",
                        "applied_value": round(t_j, 2),
                        "rated_value": round(t_j_allowed, 2),
                        "stress_ratio": round(t_j / tj_max_f, 4) if tj_max_f > 0 else 1.0,
                        "max_allowed_ratio": round(t_j_allowed / tj_max_f, 4)
                        if tj_max_f > 0
                        else 1.0,
                        "message": msg,
                    }
                )

        metrics["passed"] = len(comp_violations) == 0
        metrics["violations"] = comp_violations
        details.append(metrics)

    compliant_count = sum(1 for d in details if d["passed"])
    violation_count = len(components) - compliant_count
    overall_passed = len(violations) == 0

    return {
        "passed": overall_passed,
        "safety_class": safety_clean,
        "derating_tightened": is_tightened,
        "total_components": len(components),
        "compliant_count": compliant_count,
        "violation_count": violation_count,
        "violations": violations,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 2. EU Electronics Compliance & MSL Floor Life Validation
# ---------------------------------------------------------------------------


def validate_eu_electronics_compliance(
    bom_lines: list[dict[str, Any]],
    require_rohs: bool = True,
    require_reach: bool = True,
    require_ce: bool = False,
) -> dict[str, Any]:
    """Validate electronics BOM lines for EU environmental and MSL standards.

    Under standards:
      - EU RoHS 3 (Directive 2011/65/EU & EU 2015/863): 10 restricted substances
        (Pb, Hg, Cd, Cr6+, PBB, PBDE, DEHP, BBP, DBP, DIBP).
      - EU REACH (EC 1907/2006): SVHC candidate list threshold <= 0.1% w/w (1000 ppm).
      - IPC/JEDEC J-STD-033: Moisture Sensitivity Level (MSL 1..6) floor life exposure.
      - CE Marking / EU Declaration of Conformity requirements if requested.

    :param bom_lines: List of BOM item dictionaries.
    :param require_rohs: Assert compliance with EU RoHS 3.
    :param require_reach: Assert compliance with EU REACH SVHC limits.
    :param require_ce: Assert CE compliance / declaration.
    :return: Summary dict with overall passed status, violation list, and counts.
    """
    violations: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    for idx, line in enumerate(bom_lines):
        part_number = str(
            line.get("part_number")
            or line.get("part_no")
            or line.get("line_ref")
            or f"LINE-{idx + 1}"
        )
        line_passed = True
        line_violations: list[str] = []

        # A. RoHS 3 Evaluation
        if require_rohs:
            # Check explicit boolean flag
            if line.get("rohs_compliant") is False:
                msg = "Component explicitly flagged as non-compliant with EU RoHS 3 (2011/65/EU)"
                line_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "standard": "RoHS 3 (2011/65/EU)",
                        "issue": "rohs_non_compliant",
                        "message": msg,
                    }
                )
                line_passed = False

            # Check individual substance concentrations
            substances = (
                line.get("substances")
                or line.get("rohs_substances")
                or line.get("chemical_composition")
            )
            if isinstance(substances, dict):
                for sub_key, sub_val in substances.items():
                    norm_key = _SUBSTANCE_ALIASES.get(str(sub_key).strip().lower())
                    if norm_key and norm_key in ROHS3_RESTRICTED_SUBSTANCES:
                        sub_def = ROHS3_RESTRICTED_SUBSTANCES[norm_key]
                        val_f = float(sub_val)
                        # Determine if provided as ppm or percentage:
                        # If val_f > 1.0, treat as ppm, otherwise percentage
                        if val_f > 1.0:
                            is_violation = val_f > sub_def["max_ppm"]
                            val_str = f"{val_f} ppm (limit {sub_def['max_ppm']} ppm)"
                        else:
                            max_pct = sub_def["max_pct"]
                            max_fraction = max_pct / 100.0
                            is_violation = (val_f > max_pct) or (val_f > max_fraction)
                            val_str = f"{val_f} (threshold {max_pct}% / {max_fraction} fraction)"

                        if is_violation:
                            msg = (
                                f"Restricted substance {sub_def['symbol']} ({norm_key}) "
                                f"exceeds RoHS 3 threshold: {val_str}"
                            )
                            line_violations.append(msg)
                            violations.append(
                                {
                                    "part_number": part_number,
                                    "standard": "RoHS 3 (EU 2015/863)",
                                    "substance": sub_def["symbol"],
                                    "issue": "substance_threshold_exceeded",
                                    "message": msg,
                                }
                            )
                            line_passed = False

        # B. REACH SVHC Evaluation
        if require_reach:
            if line.get("reach_compliant") is False or line.get("reach_svhc_exceeded") is True:
                msg = "Component exceeds EU REACH (EC 1907/2006) SVHC threshold (>0.1% w/w)"
                line_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "standard": "REACH (EC 1907/2006)",
                        "issue": "reach_svhc_exceeded",
                        "message": msg,
                    }
                )
                line_passed = False

            svhc_ppm = line.get("reach_svhc_ppm")
            svhc_pct = line.get("reach_svhc_pct")
            if svhc_ppm is not None and float(svhc_ppm) > 1000.0:
                msg = f"REACH SVHC concentration {svhc_ppm} ppm exceeds 1000 ppm (0.1% w/w) limit"
                line_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "standard": "REACH (EC 1907/2006)",
                        "issue": "reach_svhc_ppm_limit",
                        "message": msg,
                    }
                )
                line_passed = False
            elif svhc_pct is not None and float(svhc_pct) > 0.1:
                msg = f"REACH SVHC concentration {svhc_pct}% exceeds 0.1% limit"
                line_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "standard": "REACH (EC 1907/2006)",
                        "issue": "reach_svhc_pct_limit",
                        "message": msg,
                    }
                )
                line_passed = False

        # C. CE Marking Evaluation
        if require_ce:
            ce_val = (
                line.get("ce_compliant")
                if "ce_compliant" in line
                else line.get("ce_marked")
                if "ce_marked" in line
                else line.get("ce_certified")
            )
            if ce_val is False:
                msg = "Component lacks CE declaration of conformity or CE marking"
                line_violations.append(msg)
                violations.append(
                    {
                        "part_number": part_number,
                        "standard": "CE (EU Directives)",
                        "issue": "ce_non_compliant",
                        "message": msg,
                    }
                )
                line_passed = False

        # D. IPC/JEDEC J-STD-033 Moisture Sensitivity Level (MSL) Floor Life
        msl_raw = line.get("msl_level") or line.get("msl")
        if msl_raw is not None:
            msl_clean = str(msl_raw).strip().upper()
            if msl_clean.startswith("MSL-"):
                msl_clean = msl_clean.replace("MSL-", "MSL ")
            max_floor_hours = MSL_FLOOR_LIFE_HOURS.get(
                msl_clean, MSL_FLOOR_LIFE_HOURS.get(msl_clean.replace("MSL ", ""))
            )

            if max_floor_hours is not None:
                exposure_hours = (
                    line.get("exposure_hours")
                    or line.get("floor_life_hours")
                    or line.get("floor_life_hours_exposed")
                )
                if exposure_hours is not None:
                    exp_h_f = float(exposure_hours)
                    if exp_h_f > max_floor_hours:
                        msg = (
                            f"IPC/JEDEC J-STD-033 floor life exceeded for {msl_clean}: "
                            f"exposed {exp_h_f:.1f}h (max allowable {max_floor_hours:.1f}h). "
                            "Component requires baking before reflow."
                        )
                        line_violations.append(msg)
                        violations.append(
                            {
                                "part_number": part_number,
                                "standard": "IPC/JEDEC J-STD-033",
                                "issue": "msl_floor_life_exceeded",
                                "msl_level": msl_clean,
                                "exposure_hours": exp_h_f,
                                "max_floor_life_hours": max_floor_hours,
                                "message": msg,
                            }
                        )
                        line_passed = False

                # Level 6 is mandatory bake before use
                if msl_clean in ("6", "MSL 6"):
                    if not line.get("baked_before_use"):
                        msg = (
                            "IPC/JEDEC J-STD-033 MSL 6 component requires mandatory "
                            "bake before use prior to board assembly reflow."
                        )
                        line_violations.append(msg)
                        violations.append(
                            {
                                "part_number": part_number,
                                "standard": "IPC/JEDEC J-STD-033",
                                "issue": "msl6_bake_required",
                                "message": msg,
                            }
                        )
                        line_passed = False

        details.append(
            {
                "part_number": part_number,
                "passed": line_passed,
                "violations": line_violations,
            }
        )

    compliant_count = sum(1 for d in details if d["passed"])
    violation_count = len(bom_lines) - compliant_count
    overall_passed = len(violations) == 0

    return {
        "passed": overall_passed,
        "total_lines": len(bom_lines),
        "compliant_count": compliant_count,
        "violation_count": violation_count,
        "standards_evaluated": {
            "rohs_3": require_rohs,
            "reach_svhc": require_reach,
            "ce_marking": require_ce,
            "msl_j_std_033": True,
        },
        "violations": violations,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 3. EN 61340-5-1 ESD Workstation Validation
# ---------------------------------------------------------------------------


def validate_en61340_esd_workstation(
    wrist_strap_mohm: float,
    mat_to_ground_ohm: float,
    footwear_ohm: float | None = None,
    relative_humidity_pct: float = 45.0,
) -> dict[str, Any]:
    """Validate ESD Protected Area (EPA) workstation under EN 61340-5-1 / IEC 61340-5-1.

    Requirements:
      - Wrist strap resistance to ground: 0.75 MÎ© <= R_wrist <= 35.0 MÎ©.
        (< 0.75 MÎ© poses electrical safety risk; > 35.0 MÎ© fails to discharge static).
      - Worksurface/mat resistance to ground: R_mat < 1.0 x 10^9 Î© (1000 MÎ©).
      - Footwear resistance (if tested): R_footwear < 1.0 x 10^8 Î© (100 MÎ©).
      - Relative humidity: Recommended >= 30.0% to minimize triboelectric charging.

    :param wrist_strap_mohm: Measured wrist strap resistance in Mega-Ohms (MÎ©).
    :param mat_to_ground_ohm: Measured mat/table resistance to ground in Ohms (Î©).
    :param footwear_ohm: Measured ESD footwear resistance in Ohms (Î©) (optional).
    :param relative_humidity_pct: Ambient relative humidity percentage (%).
    :return: Dict containing passed boolean, measurement checks, violations, and warnings.
    """
    violations: list[str] = []
    warnings: list[str] = []

    # 1. Wrist Strap Resistance Check (0.75 MÎ© to 35.0 MÎ©)
    w_min = ESD_EPA_LIMITS["wrist_strap_min_mohm"]
    w_max = ESD_EPA_LIMITS["wrist_strap_max_mohm"]
    if wrist_strap_mohm < w_min:
        violations.append(
            f"Wrist strap resistance {wrist_strap_mohm:.2f} MÎ© is below minimum safety limit "
            f"of {w_min:.2f} MÎ© (operator electric shock hazard per EN 61340-5-1)"
        )
    elif wrist_strap_mohm > w_max:
        violations.append(
            f"Wrist strap resistance {wrist_strap_mohm:.2f} MÎ© exceeds maximum dissipation limit "
            f"of {w_max:.2f} MÎ© (insufficient ESD protection per EN 61340-5-1)"
        )

    # 2. Mat / Worksurface Resistance to Ground Check (< 1.0e9 Î©)
    mat_max = ESD_EPA_LIMITS["mat_to_ground_max_ohm"]
    if mat_to_ground_ohm >= mat_max:
        violations.append(
            f"Worksurface mat-to-ground resistance {mat_to_ground_ohm:.2e} Î© exceeds "
            f"maximum limit of {mat_max:.2e} Î© (1.0 GÎ© per EN 61340-5-1 Table 1)"
        )

    # 3. Footwear Resistance Check (< 1.0e8 Î© if provided)
    if footwear_ohm is not None:
        foot_max = ESD_EPA_LIMITS["footwear_max_ohm"]
        if footwear_ohm >= foot_max:
            violations.append(
                f"Footwear resistance {footwear_ohm:.2e} Î© exceeds "
                f"maximum limit of {foot_max:.2e} Î© (100 MÎ© per EN 61340-5-1)"
            )

    # 4. Relative Humidity Check (>= 30.0%)
    hum_min = ESD_EPA_LIMITS["relative_humidity_min_pct"]
    if relative_humidity_pct < hum_min:
        msg = (
            f"Ambient relative humidity {relative_humidity_pct:.1f}% is below {hum_min:.1f}% minimum; "
            "elevated triboelectric charge accumulation risk in EPA"
        )
        warnings.append(msg)
        violations.append(msg)

    passed = len(violations) == 0

    return {
        "passed": passed,
        "standard": "EN 61340-5-1 / IEC 61340-5-1",
        "measurements": {
            "wrist_strap_mohm": wrist_strap_mohm,
            "mat_to_ground_ohm": mat_to_ground_ohm,
            "footwear_ohm": footwear_ohm,
            "relative_humidity_pct": relative_humidity_pct,
        },
        "limits": ESD_EPA_LIMITS,
        "violations": violations,
        "warnings": warnings,
    }
