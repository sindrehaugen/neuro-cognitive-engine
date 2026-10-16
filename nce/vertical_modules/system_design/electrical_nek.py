"""
nce/vertical_modules/system_design/electrical_nek.py
=====================================================
Deterministic Norwegian (NEK) and European (CENELEC / EN / ISO / CPR) electrical
engineering compliance module for system design and installation.

Governing Standards Cited:
--------------------------
1. NEK 400:2022 / CENELEC HD 60364:
   - Part 3: Earthing systems (IT 230V, TN-S 400V, TN-C-S 400V, TT 230V, SELV/PELV DC)
   - Part 4-41: Protection against electric shock (RCD 30mA, disconnection times)
   - Part 4-43: Protection against overcurrent (Ib <= In <= Iz, I2 <= 1.45 * Iz, adiabatic withstand)
   - Part 5-52: Wiring systems (Table 52B reference ampacity Iz0, derating factors kt, kg, ki, voltage drop § 525)
   - Part 7-701 / 7-722: Special locations (wet rooms, EV charging stations)
2. NEK 700 / EN 50174-2:
   - Information technology cabling - Installation specification and quality assurance.
   - EMC segregation distance between LV power cables and ICT/AV signal cables.
3. CPR (EU) 305/2011 & EN 50575:
   - European Construction Products Regulation - Power, control and communication cables
   - Fire reaction Euroclasses (Aca > B1ca > B2ca > Cca > Dca > Eca > Fca).
4. EN 61439-1 / EN 61439-2:
   - Low-voltage switchgear and controlgear assemblies.
   - Rated Diversity Factor (RDF, Table 101) & enclosure thermal dissipation calculation (§ 10.10).
5. EN 60204-1 & ISO 13849-1:
   - Safety of machinery - Electrical equipment of machines.
   - Protective bonding circuit continuity (resistance <= 0.1 Ohm, § 18.2.2).
   - Emergency stop categories (0, 1, 2, § 9.2.2).
   - Functional safety Performance Levels (PL a to PL e, ISO 13849-1).

Pure, deterministic domain core: zero LLM calls, zero network/HTTP imports, zero DB dependencies.
"""

from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("nce.vertical_modules.system_design.electrical_nek")

# ---------------------------------------------------------------------------
# Supported Earthing Systems (NEK 400-3 / HD 60364-3)
# ---------------------------------------------------------------------------

SUPPORTED_EARTHING_SYSTEMS: frozenset[str] = frozenset(
    {
        "IT_230V_3P",
        "IT_230V_1P",
        "TN_S_400V_3P",
        "TN_C_S_400V_3P",
        "TN_S_230V_1P",
        "TT_230V",
        "DC_24V",
        "DC_48V",
    }
)

# ---------------------------------------------------------------------------
# Material Constants (NEK 400-5-52 & IEC 60364-5-52)
# ---------------------------------------------------------------------------

# Conductor resistivity at operating temperature (Ohm * mm2 / m)
# NEK 400 standard operating temperature values:
# Copper at 70°C/90°C average loaded operating condition: rho = 0.0225 Ohm*mm2/m
# Aluminum at operating condition: rho = 0.036 Ohm*mm2/m
RHO_CU: float = 0.0225
RHO_AL: float = 0.036

# Cable reactance per unit length (Ohm / m)
REACTANCE_PER_METER: float = 0.00008  # 0.08 mOhm/m

# Adiabatic factor k per NEK 400-4-43 § 434.5.2 / IEC 60364-4-43
# k values for conductors (A * s^(1/2) / mm2):
K_FACTORS: dict[tuple[str, str], float] = {
    ("Cu", "XLPE"): 143.0,
    ("Cu", "PVC"): 115.0,
    ("Al", "XLPE"): 94.0,
    ("Al", "PVC"): 76.0,
}

# ---------------------------------------------------------------------------
# Reference Ampacities (NEK 400-5-52 Tables 52B)
# Reference ambient: 30°C in air. Base installation methods:
# Method C: Single-core or multi-core cable on wall or unperforated/perforated tray.
# ---------------------------------------------------------------------------

# Table 52B-1 & 52B-2 Method C - Cu / XLPE (90°C conductor temp)
# Format: cross_section_mm2: (2_loaded_conductors_A, 3_loaded_conductors_A)
_AMPACITY_CU_XLPE_METHOD_C: dict[float, tuple[float, float]] = {
    1.5: (24.0, 23.0),
    2.5: (33.0, 31.0),
    4.0: (45.0, 42.0),
    6.0: (58.0, 54.0),
    10.0: (80.0, 75.0),
    16.0: (107.0, 100.0),
    25.0: (138.0, 127.0),
    35.0: (171.0, 158.0),
    50.0: (209.0, 192.0),
    70.0: (269.0, 246.0),
    95.0: (328.0, 298.0),
    120.0: (382.0, 346.0),
    150.0: (441.0, 399.0),
    185.0: (506.0, 456.0),
    240.0: (599.0, 538.0),
}

# Cu / PVC (70°C conductor temp) Method C
_AMPACITY_CU_PVC_METHOD_C: dict[float, tuple[float, float]] = {
    1.5: (19.5, 17.5),
    2.5: (27.0, 24.0),
    4.0: (36.0, 32.0),
    6.0: (46.0, 41.0),
    10.0: (63.0, 57.0),
    16.0: (85.0, 76.0),
    25.0: (112.0, 96.0),
    35.0: (138.0, 119.0),
    50.0: (168.0, 144.0),
    70.0: (213.0, 184.0),
    95.0: (258.0, 223.0),
    120.0: (299.0, 259.0),
    150.0: (344.0, 299.0),
    185.0: (392.0, 341.0),
    240.0: (461.0, 403.0),
}

# Al / XLPE Method C (NEK 400 § 524 forbids Al < 16 mm2)
_AMPACITY_AL_XLPE_METHOD_C: dict[float, tuple[float, float]] = {
    16.0: (84.0, 78.0),
    25.0: (108.0, 99.0),
    35.0: (135.0, 124.0),
    50.0: (164.0, 151.0),
    70.0: (211.0, 193.0),
    95.0: (257.0, 235.0),
    120.0: (300.0, 274.0),
    150.0: (346.0, 316.0),
    185.0: (397.0, 362.0),
    240.0: (470.0, 427.0),
}

# Method factors relative to Method C
_METHOD_DERATING: dict[str, float] = {
    "A1": 0.77,  # Insulated wall in conduit
    "A2": 0.73,  # Insulated wall multi-core
    "B1": 0.91,  # Conduit on wooden wall
    "B2": 0.86,  # Conduit on wall multi-core
    "C": 1.00,  # Clipped direct / cable tray
    "E": 1.08,  # Free air
}

SUPPORTED_CROSS_SECTIONS: tuple[float, ...] = (
    1.5,
    2.5,
    4.0,
    6.0,
    10.0,
    16.0,
    25.0,
    35.0,
    50.0,
    70.0,
    95.0,
    120.0,
    150.0,
    185.0,
    240.0,
)

# ---------------------------------------------------------------------------
# CPR Euroclasses Ranking (EN 50575 / EU CPR 305/2011)
# ---------------------------------------------------------------------------

CPR_CLASS_RANKING: dict[str, int] = {
    "Fca": 1,
    "Eca": 2,
    "Dca": 3,
    "Cca": 4,
    "B2ca": 5,
    "B1ca": 6,
    "Aca": 7,
}

# ---------------------------------------------------------------------------
# Functional Safety Performance Levels (ISO 13849-1)
# ---------------------------------------------------------------------------

ISO13849_PL_RANKING: dict[str, int] = {
    "a": 1,
    "b": 2,
    "c": 3,
    "d": 4,
    "e": 5,
}


# ===========================================================================
# 1. Overload & Ampacity (NEK 400-4-43 & NEK 400-5-52)
# ===========================================================================


def calculate_design_current(
    power_w: float,
    voltage_v: float,
    phases: int = 1,
    cos_phi: float = 0.95,
) -> float:
    """Calculate design current Ib per NEK 400-4-43.

    Formulas:
      - 3-phase: Ib = P / (sqrt(3) * Un * cos_phi)
      - 1-phase: Ib = P / (Un * cos_phi)
      - DC (phases=0): Ib = P / Un

    Parameters
    ----------
    power_w:
        Active power in Watts.
    voltage_v:
        Nominal system voltage in Volts (e.g. 230, 400, 24, 48).
    phases:
        Number of phases: 3 for 3-phase, 1 for single phase, 0 for DC.
    cos_phi:
        Power factor (cos phi), defaults to 0.95.

    Returns
    -------
    float
        Design current in Amperes, rounded to 2 decimal places.
    """
    if voltage_v <= 0:
        raise ValueError(f"calculate_design_current: voltage_v must be positive, got {voltage_v}")
    if power_w < 0:
        raise ValueError(f"calculate_design_current: power_w must be non-negative, got {power_w}")

    if phases == 0 or voltage_v <= 48.0:
        # DC circuit
        ib = power_w / voltage_v
    elif phases == 3:
        if cos_phi <= 0:
            raise ValueError(f"calculate_design_current: cos_phi must be positive, got {cos_phi}")
        ib = power_w / (math.sqrt(3.0) * voltage_v * cos_phi)
    elif phases == 1:
        if cos_phi <= 0:
            raise ValueError(f"calculate_design_current: cos_phi must be positive, got {cos_phi}")
        ib = power_w / (voltage_v * cos_phi)
    else:
        raise ValueError(f"calculate_design_current: phases must be 0 (DC), 1, or 3; got {phases}")

    return round(ib, 2)


def calculate_derated_ampacity(
    cross_section_mm2: float,
    material: str = "Cu",
    insulation: str = "XLPE",
    installation_method: str = "C",
    kt: float = 1.0,
    kg: float = 1.0,
    ki: float = 1.0,
    loaded_conductors: int = 2,
) -> float:
    """Calculate derated cable current-carrying capacity Iz per NEK 400-5-52.

    Formula:
        Iz = Iz0 * kt * kg * ki

    Where:
      - Iz0 is base ampacity from Table 52B.
      - kt is ambient temperature correction factor (Table 52B-14).
      - kg is grouping correction factor for bundled cables (Table 52B-17).
      - ki is thermal insulation installation factor.

    Parameters
    ----------
    cross_section_mm2:
        Conductor cross-sectional area in mm2.
    material:
        Conductor material: "Cu" (Copper) or "Al" (Aluminum).
    insulation:
        Insulation type: "XLPE" (90°C) or "PVC" (70°C).
    installation_method:
        NEK 400 installation method: "A1", "A2", "B1", "B2", "C", "E".
    kt:
        Ambient temperature derating factor (default 1.0).
    kg:
        Grouping derating factor (default 1.0).
    ki:
        Thermal insulation derating factor (default 1.0).
    loaded_conductors:
        Number of loaded conductors: 2 (single phase) or 3 (three phase).

    Returns
    -------
    float
        Derated ampacity Iz in Amperes, rounded to 2 decimal places.
    """
    mat = material.strip().capitalize()
    ins = insulation.strip().upper()
    method = installation_method.strip().upper()

    if mat == "Al" and cross_section_mm2 < 16.0:
        raise ValueError(
            f"calculate_derated_ampacity: NEK 400 § 524.1 forbids Aluminum conductors "
            f"< 16 mm2 for fixed installations (got {cross_section_mm2} mm2)"
        )

    # Base table lookup for Method C
    idx = 1 if loaded_conductors >= 3 else 0

    if mat == "Cu":
        if ins == "XLPE":
            table = _AMPACITY_CU_XLPE_METHOD_C
        else:
            table = _AMPACITY_CU_PVC_METHOD_C
    elif mat == "Al":
        table = _AMPACITY_AL_XLPE_METHOD_C
    else:
        raise ValueError(f"calculate_derated_ampacity: unsupported material '{material}'")

    if cross_section_mm2 not in table:
        raise ValueError(
            f"calculate_derated_ampacity: unsupported cross section {cross_section_mm2} mm2. "
            f"Supported: {sorted(table.keys())}"
        )

    iz0_method_c = table[cross_section_mm2][idx]

    # Apply method factor
    method_factor = _METHOD_DERATING.get(method, 1.0)
    iz0 = iz0_method_c * method_factor

    # Derated ampacity
    iz = iz0 * kt * kg * ki
    return round(iz, 2)


def validate_overload_coordination(
    ib_a: float,
    in_a: float,
    iz_a: float,
    i2_factor: float = 1.45,
) -> dict[str, Any]:
    """Validate overload protection coordination per NEK 400-4-43 § 433.1.

    Conditions enforced:
      Condition 1: Ib <= In <= Iz
      Condition 2: I2 <= 1.45 * Iz  (where I2 = i2_factor * In)

    Parameters
    ----------
    ib_a:
        Design current in Amperes.
    in_a:
        Rated current of the protective device (breaker) in Amperes.
    iz_a:
        Continuous current-carrying capacity of the derated cable in Amperes.
    i2_factor:
        Conventional operating current factor for the breaker (typically 1.45).

    Returns
    -------
    dict
        {
            "passed": bool,
            "condition_1": bool,
            "condition_2": bool,
            "ib_a": float,
            "in_a": float,
            "iz_a": float,
            "i2_a": float,
            "max_allowed_i2_a": float,
            "violations": list[str],
        }
    """
    violations: list[str] = []

    c1_ib_in = ib_a <= in_a
    c1_in_iz = in_a <= iz_a
    condition_1 = c1_ib_in and c1_in_iz

    if not c1_ib_in:
        violations.append(
            f"Overload violation (Ib > In): Design current {ib_a:.2f} A exceeds "
            f"protective device rating {in_a:.2f} A (NEK 400-4-43 § 433.1)"
        )
    if not c1_in_iz:
        violations.append(
            f"Overload coordination violation (In > Iz): Breaker rating {in_a:.2f} A "
            f"exceeds cable derated ampacity {iz_a:.2f} A (NEK 400-4-43 § 433.1)"
        )

    i2_a = round(i2_factor * in_a, 2)
    max_allowed_i2 = round(1.45 * iz_a, 2)
    condition_2 = i2_a <= max_allowed_i2

    if not condition_2:
        violations.append(
            f"Thermal trip violation (I2 > 1.45*Iz): Breaker conventional trip current "
            f"I2={i2_a:.2f} A exceeds cable thermal withstand 1.45*Iz={max_allowed_i2:.2f} A "
            f"(NEK 400-4-43 § 433.1 Condition 2)"
        )

    passed = condition_1 and condition_2
    return {
        "passed": passed,
        "condition_1": condition_1,
        "condition_2": condition_2,
        "ib_a": round(ib_a, 2),
        "in_a": round(in_a, 2),
        "iz_a": round(iz_a, 2),
        "i2_a": i2_a,
        "max_allowed_i2_a": max_allowed_i2,
        "violations": violations,
    }


# ===========================================================================
# 2. Voltage Drop (NEK 400-5-52 § 525 / HD 60364-5-52)
# ===========================================================================


def calculate_voltage_drop(
    length_m: float,
    ib_a: float,
    cross_section_mm2: float,
    voltage_v: float = 230.0,
    phases: int = 1,
    material: str = "Cu",
    cos_phi: float = 0.95,
    circuit_type: str = "other",
) -> dict[str, Any]:
    """Calculate voltage drop delta-U and verify compliance with NEK 400-5-52 § 525.

    Governing Limits:
      - Lighting circuits: Delta-U % <= 3.0 %
      - Other circuits (sockets, AV equipment, power): Delta-U % <= 4.0 %

    Formulas:
      - Single-phase (1P): Delta-U = 2 * Ib * L * (rho/S * cos_phi + x * sin_phi)
      - Three-phase (3P):  Delta-U = sqrt(3) * Ib * L * (rho/S * cos_phi + x * sin_phi)
      - DC (0P):          Delta-U = 2 * Ib * L * (rho/S)

    Parameters
    ----------
    length_m:
        One-way cable route length in meters.
    ib_a:
        Design load current in Amperes.
    cross_section_mm2:
        Conductor cross-sectional area in mm2.
    voltage_v:
        Nominal operating voltage in Volts (defaults to 230V).
    phases:
        Number of phases: 1 (single phase), 3 (three-phase), or 0 (DC).
    material:
        Conductor material: "Cu" (Copper) or "Al" (Aluminum).
    cos_phi:
        Load power factor (defaults to 0.95).
    circuit_type:
        "lighting" (limit 3.0%) or "other" (limit 4.0%).

    Returns
    -------
    dict
        {
            "passed": bool,
            "delta_u_v": float,
            "delta_u_pct": float,
            "max_allowed_pct": float,
            "resistance_ohm": float,
            "reactance_ohm": float,
            "circuit_type": str,
            "violations": list[str],
        }
    """
    if length_m <= 0:
        raise ValueError(f"calculate_voltage_drop: length_m must be positive, got {length_m}")
    if cross_section_mm2 <= 0:
        raise ValueError(
            f"calculate_voltage_drop: cross_section_mm2 must be positive, got {cross_section_mm2}"
        )
    if voltage_v <= 0:
        raise ValueError(f"calculate_voltage_drop: voltage_v must be positive, got {voltage_v}")

    mat = material.strip().capitalize()
    rho = RHO_AL if mat == "Al" else RHO_CU
    x = REACTANCE_PER_METER

    # Sin phi
    sin_phi = math.sqrt(max(0.0, 1.0 - (cos_phi**2)))

    # Conductor resistance & reactance
    r_single = (rho * length_m) / cross_section_mm2
    x_single = x * length_m

    if phases == 0 or voltage_v <= 48.0:
        # DC: 2 conductors, pure resistance
        r_total = 2.0 * r_single
        x_total = 0.0
        delta_u = ib_a * r_total
    elif phases == 3:
        # 3-phase line-to-line drop
        r_total = r_single
        x_total = x_single
        delta_u = math.sqrt(3.0) * ib_a * (r_total * cos_phi + x_total * sin_phi)
    elif phases == 1:
        # 1-phase loop (phase + neutral)
        r_total = 2.0 * r_single
        x_total = 2.0 * x_single
        delta_u = ib_a * (r_total * cos_phi + x_total * sin_phi)
    else:
        raise ValueError(f"calculate_voltage_drop: invalid phases={phases}")

    delta_u_pct = (delta_u / voltage_v) * 100.0

    c_type = circuit_type.strip().lower()
    max_allowed = 3.0 if c_type == "lighting" else 4.0

    violations: list[str] = []
    if delta_u_pct > max_allowed:
        violations.append(
            f"Voltage drop violation: Delta-U={delta_u_pct:.2f}% exceeds maximum allowed "
            f"{max_allowed:.1f}% for '{circuit_type}' circuit (NEK 400-5-52 § 525)"
        )

    return {
        "passed": delta_u_pct <= max_allowed,
        "delta_u_v": round(delta_u, 2),
        "delta_u_pct": round(delta_u_pct, 2),
        "max_allowed_pct": max_allowed,
        "resistance_ohm": round(r_total, 4),
        "reactance_ohm": round(x_total, 4),
        "circuit_type": circuit_type,
        "violations": violations,
    }


# ===========================================================================
# 3. Short-Circuit & Fault Protection (NEK 400-4-41 & NEK 400-4-43 § 434.5.2)
# ===========================================================================


def validate_short_circuit_and_rcd(
    ik_min_a: float,
    ik_max_a: float,
    in_a: float,
    curve: str = "C",
    cross_section_mm2: float = 2.5,
    insulation: str = "XLPE",
    material: str = "Cu",
    disconnection_time_s: float = 0.04,
    rcd_ma: float | None = None,
    circuit_context: str = "socket_outlet",
    earthing_system: str = "IT_230V_3P",
) -> dict[str, Any]:
    """Validate short-circuit breaking capability, adiabatic withstand, and RCD protection.

    Governing Rules:
      1. Magnetic trip threshold Im:
         - Curve B: 5 * In
         - Curve C: 10 * In
         - Curve D: 20 * In
         Checks: Ik_min >= Im (fault current must reliably trip breaker instantly).
      2. Adiabatic thermal withstand per NEK 400-4-43 § 434.5.2:
         Checks: Ik_max^2 * t <= k^2 * S^2
      3. RCD Protection per NEK 400-4-41 / 7-701 / 7-722:
         - 30mA RCD mandatory on socket outlets <= 32A, wet rooms, EV chargers.
         - Norwegian IT distribution (IT_230V_3P / IT_230V_1P) enforces 30mA RCD on all
           final sub-circuits due to high fault-loop impedance on ungrounded systems.

    Parameters
    ----------
    ik_min_a:
        Minimum prospective short-circuit current at end of cable in Amperes.
    ik_max_a:
        Maximum prospective short-circuit current at breaker terminals in Amperes.
    in_a:
        Breaker rated current in Amperes.
    curve:
        Breaker trip curve: "B", "C", or "D".
    cross_section_mm2:
        Conductor cross section in mm2.
    insulation:
        Insulation material: "XLPE" or "PVC".
    material:
        Conductor material: "Cu" or "Al".
    disconnection_time_s:
        Fault clearing time in seconds (typically 0.1s to 0.4s).
    rcd_ma:
        RCD trip current rating in milliamperes (mA) or None if no RCD.
    circuit_context:
        Context: "socket_outlet", "wet_room", "ev_charger", "lighting", "fixed_equipment".
    earthing_system:
        Earthing system identifier (e.g. "IT_230V_3P", "TN_S_400V_3P").

    Returns
    -------
    dict
    """
    violations: list[str] = []

    # 1. Magnetic trip threshold
    c_upper = curve.strip().upper()
    trip_factors = {"B": 5.0, "C": 10.0, "D": 20.0}
    factor = trip_factors.get(c_upper, 10.0)
    im = factor * in_a

    magnetic_passed = ik_min_a >= im
    if not magnetic_passed:
        violations.append(
            f"Magnetic trip violation: Minimum short-circuit current Ik_min={ik_min_a:.1f} A "
            f"is below {c_upper}-curve magnetic trip threshold Im={im:.1f} A ({factor}*In). "
            f"Breaker may fail to clear fault within {disconnection_time_s}s (NEK 400-4-41 § 411.3.2)"
        )

    # 2. Adiabatic thermal withstand (Ik_max^2 * t <= k^2 * S^2)
    mat = material.strip().capitalize()
    ins = insulation.strip().upper()
    k = K_FACTORS.get((mat, ins), 143.0)

    cable_withstand = (k * cross_section_mm2) ** 2
    let_through_energy = (ik_max_a**2) * disconnection_time_s

    adiabatic_passed = let_through_energy <= cable_withstand
    if not adiabatic_passed:
        violations.append(
            f"Adiabatic thermal withstand exceeded: Ik_max^2 * t ({let_through_energy:.0f} A2s) "
            f"> k2 * S2 ({cable_withstand:.0f} A2s) for {cross_section_mm2} mm2 {mat}/{ins} cable "
            f"(NEK 400-4-43 § 434.5.2)"
        )

    # 3. RCD Protection enforcement
    # Mandatory 30mA contexts per NEK 400-4-41 / 7-701 / 7-722
    ctx = circuit_context.strip().lower()
    earth_sys = earthing_system.strip().upper()

    rcd_mandatory_contexts = {
        "socket_outlet",
        "stikkontakt",
        "wet_room",
        "bad",
        "ev_charger",
        "elbil",
        "outdoor",
        "ute",
    }

    # In Norwegian IT earthing, RCD 30mA is enforced across all final subcircuits
    is_norwegian_it = "IT" in earth_sys
    requires_30ma = (ctx in rcd_mandatory_contexts and in_a <= 32.0) or (
        is_norwegian_it and in_a <= 32.0
    )

    rcd_passed = True
    if requires_30ma:
        if rcd_ma is None:
            rcd_passed = False
            violations.append(
                f"Missing RCD protection: 30 mA RCD is mandatory for '{circuit_context}' "
                f"under {earthing_system} (NEK 400-4-41 § 411.3.3 / NEK 400-7-701 / NEK 400-7-722)"
            )
        elif rcd_ma > 30.0:
            rcd_passed = False
            violations.append(
                f"RCD rating violation: RCD trip threshold {rcd_ma} mA exceeds maximum allowed "
                f"30 mA for '{circuit_context}' (NEK 400-4-41 § 411.3.3)"
            )

    passed = magnetic_passed and adiabatic_passed and rcd_passed
    return {
        "passed": passed,
        "magnetic_trip_passed": magnetic_passed,
        "adiabatic_withstand_passed": adiabatic_passed,
        "rcd_passed": rcd_passed,
        "ik_min_a": round(ik_min_a, 1),
        "im_threshold_a": round(im, 1),
        "ik_max_a": round(ik_max_a, 1),
        "thermal_energy_let_through": round(let_through_energy, 1),
        "cable_thermal_withstand": round(cable_withstand, 1),
        "rcd_ma": rcd_ma,
        "violations": violations,
    }


# ===========================================================================
# 4. NEK 700 / EN 50174-2 EMC Segregation & CPR Fire Class
# ===========================================================================


def validate_nek700_emc_segregation(
    power_cable_class: str,
    signal_cable_shielding: str,
    separation_mm: float,
    divider_material: str = "none",
) -> dict[str, Any]:
    """Validate EMC segregation distance between power and data/AV cabling per EN 50174-2 / NEK 700.

    Governing Standards:
      - EN 50174-2 Table 5: Minimum separation distances between power and ICT cables.
      - Segregation classification of ICT/AV cables (A=UTP, B=FTP, C=STP, D=Coax, Fiber=Immune).
      - Divider reduction factors:
        * none: 1.0 (no barrier)
        * perforated_steel: 0.5 (perforated metal divider)
        * solid_steel: 0.25 (solid metallic partition)
        * aluminum: 0.5

    Parameters
    ----------
    power_cable_class:
        "standard_power" (230V/400V <= 20A) or "vfd_dimmer" (VFD drives, high-current, dimmers).
    signal_cable_shielding:
        "UTP", "FTP", "STP", "S_FTP", "COAX", or "FIBER".
    separation_mm:
        Actual measured or designed separation distance in millimeters.
    divider_material:
        "none", "perforated_steel", "solid_steel", or "aluminum".

    Returns
    -------
    dict
    """
    sig = signal_cable_shielding.strip().upper().replace("-", "_").replace("/", "_")
    pwr = power_cable_class.strip().lower()
    div = divider_material.strip().lower()

    # Base separation in mm for standard power (EN 50174-2 Table 5)
    base_table: dict[str, float] = {
        "FIBER": 0.0,
        "OPTICAL": 0.0,
        "COAX": 20.0,
        "STP": 50.0,
        "S_FTP": 50.0,
        "FTP": 100.0,
        "F_UTP": 100.0,
        "UTP": 200.0,
    }

    base_dist = base_table.get(sig, 200.0)

    # Power multiplier for noisy circuits (VFD, dimmers, thyristor power)
    pwr_multiplier = (
        1.5 if ("vfd" in pwr or "dimmer" in pwr or "class_4" in pwr or "high" in pwr) else 1.0
    )

    # Divider reduction factor
    div_factors: dict[str, float] = {
        "none": 1.0,
        "perforated_steel": 0.5,
        "perforated": 0.5,
        "solid_steel": 0.25,
        "steel": 0.25,
        "aluminum": 0.5,
    }
    div_factor = div_factors.get(div, 1.0)

    required_mm = base_dist * pwr_multiplier * div_factor

    violations: list[str] = []
    if separation_mm < required_mm:
        violations.append(
            f"EMC segregation violation: Separation distance {separation_mm:.1f} mm is below "
            f"required {required_mm:.1f} mm for {pwr} alongside {sig} cables "
            f"(divider={divider_material}) per NEK 700 / EN 50174-2 § 6.2"
        )

    return {
        "passed": separation_mm >= required_mm,
        "separation_mm": round(separation_mm, 1),
        "required_separation_mm": round(required_mm, 1),
        "base_separation_mm": round(base_dist, 1),
        "divider_factor": div_factor,
        "violations": violations,
    }


def validate_cpr_fire_class(
    cable_cpr_class: str,
    required_cpr_class: str = "Cca",
) -> dict[str, Any]:
    """Validate cable CPR fire reaction Euroclass per EN 50575 / EU CPR 305/2011.

    Hierarchy:
      Aca (7) > B1ca (6) > B2ca (5) > Cca (4) > Dca (3) > Eca (2) > Fca (1)

    Standard Norwegian requirements (TEK17 / NEK):
      - Escape routes & public buildings: Cca-s1,d1,a1 (minimum Cca)
      - General office / residential: Dca or Cca

    Parameters
    ----------
    cable_cpr_class:
        Cable declared CPR Euroclass (e.g. "Cca", "Cca-s1,d1,a1", "B2ca", "Dca").
    required_cpr_class:
        Required threshold Euroclass (defaults to "Cca").

    Returns
    -------
    dict
    """
    # Extract base Euroclass (before any subclass hyphen)
    actual_base = cable_cpr_class.strip().split("-")[0].capitalize()
    req_base = required_cpr_class.strip().split("-")[0].capitalize()

    actual_rank = CPR_CLASS_RANKING.get(actual_base, 0)
    req_rank = CPR_CLASS_RANKING.get(req_base, 4)

    violations: list[str] = []
    if actual_rank < req_rank:
        violations.append(
            f"CPR Fire class violation: Cable Euroclass '{cable_cpr_class}' (rank {actual_rank}) "
            f"does not satisfy required '{required_cpr_class}' (rank {req_rank}) "
            f"per EN 50575 / EU CPR 305/2011"
        )

    return {
        "passed": actual_rank >= req_rank,
        "cable_cpr_class": cable_cpr_class,
        "required_cpr_class": required_cpr_class,
        "cable_rank": actual_rank,
        "required_rank": req_rank,
        "violations": violations,
    }


# ===========================================================================
# 5. EN 61439 Switchboard & ISO 13849-1 Machinery Safety
# ===========================================================================


def validate_en61439_switchboard(
    circuits: list[dict[str, Any]],
    rated_current_in_a: float,
    max_dissipation_w: float | None = None,
) -> dict[str, Any]:
    """Validate low-voltage switchboard assembly per EN 61439-1 / EN 61439-2.

    Computes:
      1. Rated Diversity Factor (RDF per EN 61439-1 Table 101):
         - 2 to 3 circuits: RDF = 0.9
         - 4 to 5 circuits: RDF = 0.8
         - 6 to 9 circuits: RDF = 0.7
         - 10 or more circuits: RDF = 0.6
      2. Diversified total load current: Id = RDF * sum(In_circuits) <= Rated_In
      3. Total heat dissipation vs. enclosure thermal capacity (§ 10.10).

    Parameters
    ----------
    circuits:
        List of outgoing circuit dictionaries, each with 'in_a' and optional 'power_loss_w'.
    rated_current_in_a:
        Main switchboard / busbar rated current In in Amperes.
    max_dissipation_w:
        Maximum allowable enclosure thermal dissipation in Watts (optional).

    Returns
    -------
    dict
    """
    n = len(circuits)
    if n <= 1:
        rdf = 1.0
    elif n <= 3:
        rdf = 0.9
    elif n <= 5:
        rdf = 0.8
    elif n <= 9:
        rdf = 0.7
    else:
        rdf = 0.6

    sum_in = sum(float(c.get("in_a", 0.0)) for c in circuits)
    diversified_load = rdf * sum_in

    violations: list[str] = []
    if diversified_load > rated_current_in_a:
        violations.append(
            f"EN 61439 Switchboard overload: Diversified load {diversified_load:.1f} A "
            f"(sum(In)={sum_in:.1f} A * RDF={rdf:.2f}) exceeds main incomer rating "
            f"{rated_current_in_a:.1f} A (EN 61439-1 Table 101)"
        )

    # Estimate heat dissipation if not explicitly supplied
    total_dissipation_w: float = 0.0
    for c in circuits:
        if "power_loss_w" in c:
            total_dissipation_w += float(c["power_loss_w"])
        else:
            # Estimate breaker loss at nominal load: ~2.5W per pole for standard MCB
            in_val = float(c.get("in_a", 16.0))
            ib_val = float(c.get("ib_a", in_val * 0.8))
            poles = int(c.get("phases", 1)) or 1
            ratio = (ib_val / in_val) if in_val > 0 else 0.8
            circuit_loss = 2.5 * poles * (ratio**2)
            total_dissipation_w += circuit_loss

    if max_dissipation_w is not None and total_dissipation_w > max_dissipation_w:
        violations.append(
            f"Thermal dissipation violation: Total switchboard heat loss {total_dissipation_w:.1f} W "
            f"exceeds maximum enclosure limit {max_dissipation_w:.1f} W (EN 61439-1 § 10.10)"
        )

    passed = len(violations) == 0
    return {
        "passed": passed,
        "rdf": rdf,
        "circuit_count": n,
        "sum_in_a": round(sum_in, 2),
        "diversified_load_a": round(diversified_load, 2),
        "rated_current_in_a": round(rated_current_in_a, 2),
        "total_dissipation_w": round(total_dissipation_w, 2),
        "max_dissipation_w": max_dissipation_w,
        "violations": violations,
    }


def validate_iso13849_machinery_safety(
    required_pl: str,
    achieved_pl: str,
    estop_category: int = 1,
    bonding_resistance_ohm: float = 0.05,
) -> dict[str, Any]:
    """Validate functional safety and machinery electrical safety per ISO 13849-1 & EN 60204-1.

    Requirements:
      1. Performance Level (PL): Achieved PL >= Required PL (PL a < b < c < d < e).
      2. Emergency Stop Category (EN 60204-1 § 9.2.2):
         - Cat 0: Immediate uncontrolled removal of power.
         - Cat 1: Controlled stop with power retained during deceleration, then removed.
         - Cat 2: Controlled stop with power maintained (standstill).
         Must be 0, 1, or 2.
      3. Protective Bonding Continuity (EN 60204-1 § 18.2.2):
         Resistance between PE terminal and exposed conductive parts must be <= 0.1 Ohm (100 mOhm).

    Parameters
    ----------
    required_pl:
        Required Performance Level ("a", "b", "c", "d", "e").
    achieved_pl:
        Achieved Performance Level ("a", "b", "c", "d", "e").
    estop_category:
        Emergency stop category: 0, 1, or 2.
    bonding_resistance_ohm:
        Measured or verified protective earth bonding resistance in Ohms.

    Returns
    -------
    dict
    """
    req = required_pl.strip().lower()
    ach = achieved_pl.strip().lower()

    req_rank = ISO13849_PL_RANKING.get(req, 3)
    ach_rank = ISO13849_PL_RANKING.get(ach, 1)

    violations: list[str] = []

    pl_passed = ach_rank >= req_rank
    if not pl_passed:
        violations.append(
            f"ISO 13849-1 Functional Safety violation: Achieved Performance Level '{achieved_pl}' "
            f"(rank {ach_rank}) is below required '{required_pl}' (rank {req_rank})"
        )

    estop_passed = estop_category in {0, 1, 2}
    if not estop_passed:
        violations.append(
            f"EN 60204-1 E-Stop violation: Invalid emergency stop category {estop_category}. "
            f"Must be 0 (uncontrolled), 1 (controlled stop then off), or 2 (controlled stop held) "
            f"(EN 60204-1 § 9.2.2)"
        )

    bonding_passed = bonding_resistance_ohm <= 0.1
    if not bonding_passed:
        violations.append(
            f"EN 60204-1 Bonding violation: Protective earth bonding resistance "
            f"{bonding_resistance_ohm:.3f} Ohm exceeds maximum allowable limit 0.100 Ohm "
            f"(EN 60204-1 § 18.2.2)"
        )

    passed = pl_passed and estop_passed and bonding_passed
    return {
        "passed": passed,
        "pl_passed": pl_passed,
        "estop_passed": estop_passed,
        "bonding_passed": bonding_passed,
        "required_pl": required_pl,
        "achieved_pl": achieved_pl,
        "estop_category": estop_category,
        "bonding_resistance_ohm": bonding_resistance_ohm,
        "violations": violations,
    }


# ===========================================================================
# 6. Kursfortegnelse (Circuit Schedule) Generator
# ===========================================================================


def generate_kursfortegnelse(
    circuits: list[dict[str, Any]],
    earthing_system: str = "TN_S_400V_3P",
    switchboard_id: str = "TAVLE-01",
    switchboard_rating_a: float = 63.0,
    max_dissipation_w: float | None = None,
) -> dict[str, Any]:
    """Generate Norwegian Kursfortegnelse (circuit schedule) with full electrical validation.

    For each circuit row, performs deterministic calculation and checks:
      1. Design current Ib (calculate_design_current)
      2. Derated ampacity Iz (calculate_derated_ampacity)
      3. Overload coordination Ib <= In <= Iz and I2 <= 1.45*Iz (validate_overload_coordination)
      4. Voltage drop Delta-U % <= 3.0% / 4.0% (calculate_voltage_drop)
      5. Fault protection & RCD 30mA (validate_short_circuit_and_rcd)
      6. Overall EN 61439 switchboard aggregation (validate_en61439_switchboard)

    Parameters
    ----------
    circuits:
        List of circuit definitions.
    earthing_system:
        Earthing system identifier (e.g. "TN_S_400V_3P", "IT_230V_3P").
    switchboard_id:
        Identifier string for the distribution board (defaults to "TAVLE-01").
    switchboard_rating_a:
        Rated current for switchboard main incomer (defaults to 63A).
    max_dissipation_w:
        Enclosure thermal dissipation limit (optional).

    Returns
    -------
    dict
        {
            "switchboard_id": str,
            "earthing_system": str,
            "total_circuits": int,
            "passed": bool,
            "circuit_rows": list[dict],
            "en61439_summary": dict,
            "all_violations": list[str],
        }
    """
    if earthing_system not in SUPPORTED_EARTHING_SYSTEMS:
        log.warning(
            "generate_kursfortegnelse: earthing_system '%s' not in official list %s",
            earthing_system,
            sorted(SUPPORTED_EARTHING_SYSTEMS),
        )

    circuit_rows: list[dict[str, Any]] = []
    all_violations: list[str] = []

    for idx, c in enumerate(circuits, start=1):
        course_no = c.get("course_no", idx)
        description = c.get("description", f"Kurs {course_no}")
        in_a = float(c.get("in_a", 16.0))
        curve = str(c.get("curve", "C"))
        rcd_ma = float(c["rcd_ma"]) if c.get("rcd_ma") is not None else None
        cable_mm2 = float(c.get("cable_mm2", 2.5))
        cable_material = str(c.get("cable_material", "Cu"))
        insulation = str(c.get("insulation", "XLPE"))
        installation_method = str(c.get("installation_method", "C"))
        length_m = float(c.get("length_m", 20.0))
        power_w = float(c.get("power_w", 2300.0))
        phases = int(c.get("phases", 1))
        cos_phi = float(c.get("cos_phi", 0.95))
        circuit_type = str(c.get("circuit_type", "other"))

        # Default nominal voltage based on phases & earthing system
        default_voltage = (
            400.0
            if (phases == 3 and "400V" in earthing_system)
            else (24.0 if "24V" in earthing_system else 230.0)
        )
        voltage_v = float(c.get("voltage_v", default_voltage))

        row_violations: list[str] = []

        # 1. Design current
        ib_a = calculate_design_current(
            power_w=power_w,
            voltage_v=voltage_v,
            phases=phases,
            cos_phi=cos_phi,
        )

        # 2. Derated ampacity
        loaded_cond = 3 if phases == 3 else 2
        iz_a = calculate_derated_ampacity(
            cross_section_mm2=cable_mm2,
            material=cable_material,
            insulation=insulation,
            installation_method=installation_method,
            loaded_conductors=loaded_cond,
        )

        # 3. Overload coordination
        i2_factor = float(c.get("i2_factor", 1.45))
        overload_res = validate_overload_coordination(
            ib_a=ib_a,
            in_a=in_a,
            iz_a=iz_a,
            i2_factor=i2_factor,
        )
        row_violations.extend(overload_res.get("violations", []))

        # 4. Voltage drop
        vdrop_res = calculate_voltage_drop(
            length_m=length_m,
            ib_a=ib_a,
            cross_section_mm2=cable_mm2,
            voltage_v=voltage_v,
            phases=phases,
            material=cable_material,
            cos_phi=cos_phi,
            circuit_type=circuit_type,
        )
        row_violations.extend(vdrop_res.get("violations", []))

        # 5. Short-circuit & RCD
        # If Ik values provided, evaluate; otherwise synthesize reasonable defaults
        ik_min_a = float(c.get("ik_min_a", in_a * 15.0))
        ik_max_a = float(c.get("ik_max_a", 1000.0))
        disconnection_time_s = float(c.get("disconnection_time_s", 0.04))
        sc_res = validate_short_circuit_and_rcd(
            ik_min_a=ik_min_a,
            ik_max_a=ik_max_a,
            in_a=in_a,
            curve=curve,
            cross_section_mm2=cable_mm2,
            insulation=insulation,
            material=cable_material,
            disconnection_time_s=disconnection_time_s,
            rcd_ma=rcd_ma,
            circuit_context=circuit_type,
            earthing_system=earthing_system,
        )
        row_violations.extend(sc_res.get("violations", []))

        row_passed = len(row_violations) == 0
        if not row_passed:
            all_violations.extend(
                [f"Circuit {course_no} ({description}): {v}" for v in row_violations]
            )

        circuit_rows.append(
            {
                "course_no": course_no,
                "description": description,
                "in_a": in_a,
                "curve": curve,
                "rcd_ma": rcd_ma,
                "cable_mm2": cable_mm2,
                "cable_material": cable_material,
                "length_m": length_m,
                "ib_a": ib_a,
                "iz_a": iz_a,
                "voltage_drop_pct": vdrop_res["delta_u_pct"],
                "voltage_drop_v": vdrop_res["delta_u_v"],
                "passed": row_passed,
                "violations": row_violations,
            }
        )

    # 6. EN 61439 Switchboard evaluation
    en61439_summary = validate_en61439_switchboard(
        circuits=circuits,
        rated_current_in_a=switchboard_rating_a,
        max_dissipation_w=max_dissipation_w,
    )
    all_violations.extend(en61439_summary.get("violations", []))

    overall_passed = all(row["passed"] for row in circuit_rows) and en61439_summary["passed"]

    return {
        "switchboard_id": switchboard_id,
        "earthing_system": earthing_system,
        "total_circuits": len(circuit_rows),
        "passed": overall_passed,
        "circuit_rows": circuit_rows,
        "en61439_summary": en61439_summary,
        "all_violations": all_violations,
    }
