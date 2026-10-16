"""
tests/unit/test_system_design_validation.py
============================================
Unit tests for Wave E-1:
- Norwegian NEK 400:2022 / CENELEC HD 60364 Electrical Installation Rules
- NEK 700 / EN 50174-2 EMC Cable Segregation
- EN 61439 Switchboard Rated Diversity Factor & Thermal Dissipation
- EN 60204-1 & ISO 13849-1 Machinery Safety & Protective Bonding Continuity
- CPR (EU) 305/2011 & EN 50575 Cable Fire Reaction Euroclasses
- Automated Kursfortegnelse (Circuit Schedule) Generator
- Integration into validation_queries.py and sow.py
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce.vertical_modules.system_design.electrical_nek import (
    calculate_derated_ampacity,
    calculate_design_current,
    calculate_voltage_drop,
    generate_kursfortegnelse,
    validate_cpr_fire_class,
    validate_en61439_switchboard,
    validate_iso13849_machinery_safety,
    validate_nek700_emc_segregation,
    validate_overload_coordination,
    validate_short_circuit_and_rcd,
)
from nce.vertical_modules.system_design.sow import generate_sow
from nce.vertical_modules.system_design.validation_queries import (
    check_electrical_nek_compliance,
    run_all_validations,
    validate_design_graph,
)

# ===========================================================================
# 1. calculate_design_current
# ===========================================================================


class TestCalculateDesignCurrent:
    """Tests for calculate_design_current (NEK 400-4-43)."""

    def test_single_phase_230v_standard(self) -> None:
        # Ib = P / (Un * cos_phi) = 2300 / (230 * 0.95) = 10.526... -> 10.53
        ib = calculate_design_current(power_w=2300.0, voltage_v=230.0, phases=1, cos_phi=0.95)
        assert ib == 10.53

    def test_three_phase_400v_standard(self) -> None:
        # Ib = P / (sqrt(3) * Un * cos_phi) = 11000 / (1.732 * 400 * 0.95) = 16.712... -> 16.71
        expected = round(11000.0 / (math.sqrt(3.0) * 400.0 * 0.95), 2)
        ib = calculate_design_current(power_w=11000.0, voltage_v=400.0, phases=3, cos_phi=0.95)
        assert ib == expected

    def test_dc_24v_and_48v(self) -> None:
        # DC: Ib = P / Un
        ib_24 = calculate_design_current(power_w=240.0, voltage_v=24.0, phases=0)
        assert ib_24 == 10.0
        ib_48 = calculate_design_current(power_w=480.0, voltage_v=48.0, phases=0)
        assert ib_48 == 10.0

    def test_zero_power(self) -> None:
        ib = calculate_design_current(power_w=0.0, voltage_v=230.0, phases=1)
        assert ib == 0.0

    def test_negative_voltage_raises(self) -> None:
        with pytest.raises(ValueError, match="voltage_v must be positive"):
            calculate_design_current(power_w=1000.0, voltage_v=-230.0)

    def test_negative_power_raises(self) -> None:
        with pytest.raises(ValueError, match="power_w must be non-negative"):
            calculate_design_current(power_w=-500.0, voltage_v=230.0)

    def test_invalid_phases_raises(self) -> None:
        with pytest.raises(ValueError, match="phases must be 0 .* 1, or 3"):
            calculate_design_current(power_w=1000.0, voltage_v=230.0, phases=2)

    def test_non_positive_cos_phi_raises(self) -> None:
        with pytest.raises(ValueError, match="cos_phi must be positive"):
            calculate_design_current(power_w=1000.0, voltage_v=230.0, phases=1, cos_phi=0.0)


# ===========================================================================
# 2. calculate_derated_ampacity
# ===========================================================================


class TestCalculateDeratedAmpacity:
    """Tests for calculate_derated_ampacity (NEK 400-5-52 Tables 52B)."""

    def test_standard_cu_xlpe_method_c_2_5mm2(self) -> None:
        # Base ampacity for 2.5 mm2 Cu/XLPE Method C (2 loaded) is 33A
        iz = calculate_derated_ampacity(
            cross_section_mm2=2.5,
            material="Cu",
            insulation="XLPE",
            installation_method="C",
            loaded_conductors=2,
        )
        assert iz == 33.0

    def test_standard_cu_pvc_method_c_1_5mm2(self) -> None:
        # Base ampacity for 1.5 mm2 Cu/PVC Method C (2 loaded) is 19.5A
        iz = calculate_derated_ampacity(
            cross_section_mm2=1.5,
            material="Cu",
            insulation="PVC",
            installation_method="C",
            loaded_conductors=2,
        )
        assert iz == 19.5

    def test_derating_factors_applied(self) -> None:
        # Iz = Iz0 * kt * kg * ki
        # 2.5 mm2 Cu/XLPE: 33 * 0.9 * 0.8 * 1.0 = 23.76
        iz = calculate_derated_ampacity(
            cross_section_mm2=2.5,
            material="Cu",
            insulation="XLPE",
            installation_method="C",
            kt=0.9,
            kg=0.8,
            ki=1.0,
        )
        assert iz == 23.76

    def test_installation_method_b1_derating(self) -> None:
        # Method B1 factor 0.91 on 2.5 mm2 (33 * 0.91 = 30.03)
        iz = calculate_derated_ampacity(
            cross_section_mm2=2.5,
            installation_method="B1",
        )
        assert iz == 30.03

    def test_aluminum_under_16mm2_forbidden(self) -> None:
        with pytest.raises(ValueError, match="NEK 400 § 524.1 forbids Aluminum"):
            calculate_derated_ampacity(cross_section_mm2=10.0, material="Al")

    def test_aluminum_16mm2_supported(self) -> None:
        iz = calculate_derated_ampacity(
            cross_section_mm2=16.0,
            material="Al",
            insulation="XLPE",
            installation_method="C",
        )
        assert iz == 84.0

    def test_unsupported_cross_section_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported cross section"):
            calculate_derated_ampacity(cross_section_mm2=3.0)


# ===========================================================================
# 3. validate_overload_coordination
# ===========================================================================


class TestValidateOverloadCoordination:
    """Tests for validate_overload_coordination (NEK 400-4-43 § 433.1)."""

    def test_compliant_coordination_passes(self) -> None:
        # Ib = 10.5A <= In = 16A <= Iz = 24A, I2 = 1.45*16 = 23.2A <= 1.45*24 = 34.8A
        res = validate_overload_coordination(ib_a=10.5, in_a=16.0, iz_a=24.0)
        assert res["passed"] is True
        assert res["condition_1"] is True
        assert res["condition_2"] is True
        assert res["violations"] == []

    def test_ib_greater_than_in_fails(self) -> None:
        res = validate_overload_coordination(ib_a=18.0, in_a=16.0, iz_a=24.0)
        assert res["passed"] is False
        assert res["condition_1"] is False
        assert any("Ib > In" in v for v in res["violations"])

    def test_in_greater_than_iz_fails(self) -> None:
        res = validate_overload_coordination(ib_a=12.0, in_a=25.0, iz_a=20.0)
        assert res["passed"] is False
        assert res["condition_1"] is False
        assert any("In > Iz" in v for v in res["violations"])

    def test_i2_greater_than_145_iz_fails(self) -> None:
        # Custom i2_factor causing I2 to exceed 1.45*Iz
        res = validate_overload_coordination(
            ib_a=10.0, in_a=16.0, iz_a=16.0, i2_factor=1.60
        )  # I2 = 25.6 > 1.45*16 = 23.2
        assert res["passed"] is False
        assert res["condition_2"] is False
        assert any("I2 > 1.45*Iz" in v for v in res["violations"])


# ===========================================================================
# 4. calculate_voltage_drop
# ===========================================================================


class TestCalculateVoltageDrop:
    """Tests for calculate_voltage_drop (NEK 400-5-52 § 525)."""

    def test_lighting_circuit_under_3_pct_passes(self) -> None:
        # Short route, low load: 15m, 4A, 1.5 mm2, 230V
        res = calculate_voltage_drop(
            length_m=15.0,
            ib_a=4.0,
            cross_section_mm2=1.5,
            voltage_v=230.0,
            phases=1,
            circuit_type="lighting",
        )
        assert res["passed"] is True
        assert res["max_allowed_pct"] == 3.0
        assert res["delta_u_pct"] < 3.0

    def test_other_circuit_over_4_pct_fails(self) -> None:
        # Long route 60m, 16A on 2.5 mm2: Delta-U will exceed 4%
        res = calculate_voltage_drop(
            length_m=60.0,
            ib_a=16.0,
            cross_section_mm2=2.5,
            voltage_v=230.0,
            phases=1,
            circuit_type="other",
        )
        assert res["passed"] is False
        assert res["max_allowed_pct"] == 4.0
        assert res["delta_u_pct"] > 4.0
        assert any("Voltage drop violation" in v for v in res["violations"])

    def test_three_phase_400v_calculation(self) -> None:
        res = calculate_voltage_drop(
            length_m=25.0,
            ib_a=16.0,
            cross_section_mm2=4.0,
            voltage_v=400.0,
            phases=3,
        )
        assert res["passed"] is True
        assert res["delta_u_pct"] < 4.0

    def test_dc_voltage_drop(self) -> None:
        res = calculate_voltage_drop(
            length_m=10.0,
            ib_a=5.0,
            cross_section_mm2=2.5,
            voltage_v=24.0,
            phases=0,
        )
        # R = 2 * (0.0225 * 10 / 2.5) = 0.18 Ohm -> Delta-U = 5 * 0.18 = 0.9V -> 0.9/24 = 3.75%
        assert res["delta_u_v"] == 0.9
        assert res["delta_u_pct"] == 3.75

    def test_negative_length_raises(self) -> None:
        with pytest.raises(ValueError, match="length_m must be positive"):
            calculate_voltage_drop(length_m=-10.0, ib_a=10.0, cross_section_mm2=2.5)


# ===========================================================================
# 5. validate_short_circuit_and_rcd
# ===========================================================================


class TestValidateShortCircuitAndRCD:
    """Tests for validate_short_circuit_and_rcd (NEK 400-4-41 & NEK 400-4-43 § 434.5.2)."""

    def test_compliant_c_curve_with_rcd(self) -> None:
        # In=16A, C-curve -> Im=160A. Ik_min=250A >= 160A.
        # k=143, S=2.5 -> k2*S2 = (143*2.5)^2 = 127806 A2s
        # Ik_max=1000A, t=0.1s -> 1000^2 * 0.1 = 100000 A2s <= 127806 -> passes.
        # rcd=30mA on socket_outlet under TN-S -> passes.
        res = validate_short_circuit_and_rcd(
            ik_min_a=250.0,
            ik_max_a=1000.0,
            in_a=16.0,
            curve="C",
            cross_section_mm2=2.5,
            disconnection_time_s=0.1,
            rcd_ma=30.0,
            circuit_context="socket_outlet",
            earthing_system="TN_S_400V_3P",
        )
        assert res["passed"] is True
        assert res["magnetic_trip_passed"] is True
        assert res["adiabatic_withstand_passed"] is True
        assert res["rcd_passed"] is True
        assert res["violations"] == []

    def test_magnetic_trip_failure(self) -> None:
        # C-curve In=16A -> Im=160A. Ik_min=100A < 160A -> fails.
        res = validate_short_circuit_and_rcd(
            ik_min_a=100.0,
            ik_max_a=1000.0,
            in_a=16.0,
            curve="C",
            cross_section_mm2=2.5,
            disconnection_time_s=0.1,
            rcd_ma=30.0,
        )
        assert res["passed"] is False
        assert res["magnetic_trip_passed"] is False
        assert any("Magnetic trip violation" in v for v in res["violations"])

    def test_adiabatic_withstand_failure(self) -> None:
        # Ik_max=3000A, t=0.4s -> 3000^2 * 0.4 = 3,600,000 A2s > 127,806 A2s
        res = validate_short_circuit_and_rcd(
            ik_min_a=200.0,
            ik_max_a=3000.0,
            in_a=16.0,
            curve="B",
            cross_section_mm2=2.5,
            disconnection_time_s=0.4,
            rcd_ma=30.0,
        )
        assert res["passed"] is False
        assert res["adiabatic_withstand_passed"] is False
        assert any("Adiabatic thermal withstand exceeded" in v for v in res["violations"])

    def test_missing_rcd_on_socket_outlet_fails(self) -> None:
        res = validate_short_circuit_and_rcd(
            ik_min_a=200.0,
            ik_max_a=1000.0,
            in_a=16.0,
            curve="B",
            cross_section_mm2=2.5,
            disconnection_time_s=0.1,
            rcd_ma=None,
            circuit_context="socket_outlet",
        )
        assert res["passed"] is False
        assert res["rcd_passed"] is False
        assert any("Missing RCD protection" in v for v in res["violations"])

    def test_rcd_over_30ma_on_wet_room_fails(self) -> None:
        res = validate_short_circuit_and_rcd(
            ik_min_a=200.0,
            ik_max_a=1000.0,
            in_a=16.0,
            curve="B",
            cross_section_mm2=2.5,
            disconnection_time_s=0.1,
            rcd_ma=100.0,
            circuit_context="wet_room",
        )
        assert res["passed"] is False
        assert res["rcd_passed"] is False
        assert any("RCD rating violation" in v for v in res["violations"])

    def test_norwegian_it_earthing_mandates_rcd(self) -> None:
        # In IT_230V_3P, all final subcircuits mandate 30mA RCD
        res = validate_short_circuit_and_rcd(
            ik_min_a=200.0,
            ik_max_a=1000.0,
            in_a=16.0,
            curve="C",
            cross_section_mm2=2.5,
            rcd_ma=None,
            circuit_context="fixed_equipment",
            earthing_system="IT_230V_3P",
        )
        assert res["passed"] is False
        assert res["rcd_passed"] is False


# ===========================================================================
# 6. validate_nek700_emc_segregation & validate_cpr_fire_class
# ===========================================================================


class TestEMCSegregationAndCPR:
    """Tests for NEK 700 / EN 50174-2 and CPR EN 50575."""

    def test_emc_utp_no_divider_passes_and_fails(self) -> None:
        # UTP base separation is 200 mm without divider
        pass_res = validate_nek700_emc_segregation(
            power_cable_class="standard_power",
            signal_cable_shielding="UTP",
            separation_mm=210.0,
            divider_material="none",
        )
        assert pass_res["passed"] is True
        assert pass_res["required_separation_mm"] == 200.0

        fail_res = validate_nek700_emc_segregation(
            power_cable_class="standard_power",
            signal_cable_shielding="UTP",
            separation_mm=150.0,
            divider_material="none",
        )
        assert fail_res["passed"] is False
        assert any("EMC segregation violation" in v for v in fail_res["violations"])

    def test_emc_solid_steel_divider_reduction(self) -> None:
        # Solid steel divider has 0.25 factor -> UTP requires 200 * 0.25 = 50 mm
        res = validate_nek700_emc_segregation(
            power_cable_class="standard_power",
            signal_cable_shielding="UTP",
            separation_mm=60.0,
            divider_material="solid_steel",
        )
        assert res["passed"] is True
        assert res["required_separation_mm"] == 50.0

    def test_emc_fiber_optical_immune(self) -> None:
        res = validate_nek700_emc_segregation(
            power_cable_class="vfd_dimmer",
            signal_cable_shielding="FIBER",
            separation_mm=0.0,
        )
        assert res["passed"] is True
        assert res["required_separation_mm"] == 0.0

    def test_cpr_fire_class_comparison(self) -> None:
        # Cca satisfies Cca
        assert validate_cpr_fire_class("Cca", "Cca")["passed"] is True
        # B2ca satisfies Cca (higher rank)
        assert validate_cpr_fire_class("B2ca", "Cca")["passed"] is True
        # Dca does not satisfy Cca (lower rank)
        fail_res = validate_cpr_fire_class("Dca", "Cca")
        assert fail_res["passed"] is False
        assert any("CPR Fire class violation" in v for v in fail_res["violations"])
        # With European subclass string
        assert validate_cpr_fire_class("Cca-s1,d1,a1", "Cca")["passed"] is True


# ===========================================================================
# 7. validate_en61439_switchboard & validate_iso13849_machinery_safety
# ===========================================================================


class TestSwitchboardAndMachinerySafety:
    """Tests for EN 61439, ISO 13849-1, and EN 60204-1."""

    def test_en61439_rdf_calculation(self) -> None:
        # 4 circuits -> RDF = 0.8
        circuits = [{"in_a": 16.0} for _ in range(4)]
        # sum(In) = 64A, Id = 0.8 * 64 = 51.2A <= 63A -> passes
        res = validate_en61439_switchboard(circuits=circuits, rated_current_in_a=63.0)
        assert res["passed"] is True
        assert res["rdf"] == 0.8
        assert res["diversified_load_a"] == 51.2

    def test_en61439_switchboard_overload_fails(self) -> None:
        # 10 circuits of 16A -> sum(In) = 160A, RDF = 0.6 -> Id = 96A > 63A -> fails
        circuits = [{"in_a": 16.0} for _ in range(10)]
        res = validate_en61439_switchboard(circuits=circuits, rated_current_in_a=63.0)
        assert res["passed"] is False
        assert any("Switchboard overload" in v for v in res["violations"])

    def test_en61439_thermal_dissipation_limit(self) -> None:
        circuits = [{"in_a": 16.0, "power_loss_w": 25.0} for _ in range(4)]
        # Total loss = 100W > max 80W
        res = validate_en61439_switchboard(
            circuits=circuits, rated_current_in_a=100.0, max_dissipation_w=80.0
        )
        assert res["passed"] is False
        assert any("Thermal dissipation violation" in v for v in res["violations"])

    def test_iso13849_machinery_safety_passes(self) -> None:
        # Required 'c', achieved 'd', estop=1, bonding=0.04 Ohm
        res = validate_iso13849_machinery_safety(
            required_pl="c",
            achieved_pl="d",
            estop_category=1,
            bonding_resistance_ohm=0.04,
        )
        assert res["passed"] is True
        assert res["pl_passed"] is True
        assert res["estop_passed"] is True
        assert res["bonding_passed"] is True

    def test_iso13849_insufficient_pl_fails(self) -> None:
        res = validate_iso13849_machinery_safety(
            required_pl="d",
            achieved_pl="b",
            estop_category=1,
            bonding_resistance_ohm=0.05,
        )
        assert res["passed"] is False
        assert res["pl_passed"] is False
        assert any("Performance Level" in v for v in res["violations"])

    def test_en60204_bonding_over_0_1_ohm_fails(self) -> None:
        res = validate_iso13849_machinery_safety(
            required_pl="c",
            achieved_pl="c",
            estop_category=0,
            bonding_resistance_ohm=0.15,
        )
        assert res["passed"] is False
        assert res["bonding_passed"] is False
        assert any("Bonding violation" in v for v in res["violations"])

    def test_invalid_estop_category_fails(self) -> None:
        res = validate_iso13849_machinery_safety(
            required_pl="c",
            achieved_pl="c",
            estop_category=3,  # Only 0, 1, 2 valid
            bonding_resistance_ohm=0.05,
        )
        assert res["passed"] is False
        assert res["estop_passed"] is False


# ===========================================================================
# 8. generate_kursfortegnelse
# ===========================================================================


class TestGenerateKursfortegnelse:
    """Tests for generate_kursfortegnelse generator."""

    def test_generate_kursfortegnelse_compliant_schedule(self) -> None:
        circuits = [
            {
                "course_no": 1,
                "description": "Belysning Møterom 1",
                "in_a": 10.0,
                "curve": "B",
                "cable_mm2": 1.5,
                "length_m": 15.0,
                "power_w": 600.0,
                "circuit_type": "lighting",
                "rcd_ma": 30.0,
            },
            {
                "course_no": 2,
                "description": "Stikkontakter Møterom 1",
                "in_a": 16.0,
                "curve": "C",
                "cable_mm2": 2.5,
                "length_m": 20.0,
                "power_w": 2300.0,
                "circuit_type": "socket_outlet",
                "rcd_ma": 30.0,
            },
        ]
        res = generate_kursfortegnelse(
            circuits=circuits,
            earthing_system="TN_S_400V_3P",
            switchboard_id="TAVLE-01",
            switchboard_rating_a=63.0,
        )
        assert res["passed"] is True, res["all_violations"]
        assert res["total_circuits"] == 2
        assert len(res["circuit_rows"]) == 2
        assert res["circuit_rows"][0]["passed"] is True
        assert res["circuit_rows"][1]["passed"] is True
        assert res["en61439_summary"]["passed"] is True
        assert res["all_violations"] == []

    def test_generate_kursfortegnelse_violating_schedule(self) -> None:
        circuits = [
            {
                "course_no": 1,
                "description": "Overloaded sockets",
                "in_a": 16.0,
                "curve": "C",
                "cable_mm2": 1.5,
                "length_m": 80.0,  # Excessive voltage drop
                "power_w": 4000.0,  # Exceeds 16A breaker
                "circuit_type": "socket_outlet",
                "rcd_ma": None,  # Missing mandatory 30mA RCD
            }
        ]
        res = generate_kursfortegnelse(
            circuits=circuits,
            earthing_system="TN_S_400V_3P",
        )
        assert res["passed"] is False
        assert len(res["all_violations"]) > 0


# ===========================================================================
# 9. validation_queries.py integration
# ===========================================================================


class TestValidationQueriesIntegration:
    """Tests for check_electrical_nek_compliance and run_all_validations."""

    def test_check_electrical_nek_compliance_empty_passes(self) -> None:
        res = check_electrical_nek_compliance(None)
        assert res["passed"] is True
        assert "skipped" in res["reasons"][0]

    def test_check_electrical_nek_compliance_with_circuits(self) -> None:
        circuits = [
            {
                "course_no": 1,
                "description": "AV Rack Power",
                "in_a": 16.0,
                "curve": "C",
                "cable_mm2": 2.5,
                "length_m": 15.0,
                "power_w": 2000.0,
                "rcd_ma": 30.0,
            }
        ]
        res = check_electrical_nek_compliance(circuits, "TN_S_400V_3P")
        assert res["passed"] is True
        assert len(res["reasons"]) == 1

    def test_run_all_validations_backward_compatibility(self) -> None:
        # When electrical_circuits is None, returns exactly the 5 baseline checks
        results = run_all_validations()
        assert len(results) == 5

        # When electrical_circuits is provided, returns 6 checks
        circuits = [
            {
                "course_no": 1,
                "in_a": 16.0,
                "curve": "C",
                "cable_mm2": 2.5,
                "length_m": 15.0,
                "power_w": 1500.0,
                "rcd_ma": 30.0,
            }
        ]
        results_with_elec = run_all_validations(electrical_circuits=circuits)
        assert len(results_with_elec) == 6
        assert results_with_elec[-1]["passed"] is True

    @pytest.mark.asyncio
    async def test_validate_design_graph_with_electrical_circuits(self) -> None:
        engine = AsyncMock()
        engine.pg_pool = AsyncMock()

        circuits = [
            {
                "course_no": 1,
                "description": "Display power",
                "in_a": 10.0,
                "curve": "B",
                "cable_mm2": 1.5,
                "length_m": 10.0,
                "power_w": 500.0,
                "rcd_ma": 30.0,
            }
        ]

        with (
            patch(
                "nce.vertical_modules.system_design.validation_queries.scoped_pg_session"
            ) as mock_sess,
            patch(
                "nce.vertical_modules.system_design.validation_queries._fetch_port_directions",
                return_value=([], set()),
            ),
            patch(
                "nce.vertical_modules.system_design.validation_queries._fetch_connections_and_capabilities",
                return_value=([], {}),
            ),
            patch(
                "nce.vertical_modules.system_design.validation_queries._fetch_device_capabilities",
                return_value=[],
            ),
            patch(
                "nce.vertical_modules.system_design.validation_queries._fetch_port_capabilities",
                return_value=[],
            ),
        ):
            mock_conn = AsyncMock()
            mock_sess.return_value.__aenter__.return_value = mock_conn

            params = {
                "namespace_id": "00000000-0000-0000-0000-000000000001",
                "design_id": "DES-001",
                "electrical_circuits": circuits,
                "earthing_system": "TN_S_400V_3P",
            }
            res = await validate_design_graph(engine, params)
            assert res["passed"] is True
            assert any("NEK 400" in r for r in res["reasons"])


# ===========================================================================
# 10. sow.py integration
# ===========================================================================


class TestSoWIntegration:
    """Tests for SoW generator Kursfortegnelse integration."""

    def _sample_sow_input(self) -> dict[str, Any]:
        return {
            "project": {
                "id": "PRJ-01",
                "name": "AV Upgrade",
                "customerName": "Acme Corp",
                "contractValue": 100000.0,
                "startDate": "2026-11-01",
                "endDate": "2026-11-30",
            },
            "rooms": [{"id": "ROOM-1", "name": "Boardroom", "type": "meeting_room"}],
            "bomLines": [],
        }

    def test_generate_sow_without_electrical_circuits(self) -> None:
        with patch(
            "nce.vertical_modules.system_design.sow._supplier_name",
            return_value="Acme Integrator AS",
        ):
            sow_in = self._sample_sow_input()
            doc = generate_sow(sow_in, version_number=1)
            assert "kursfortegnelse" not in doc

    def test_generate_sow_with_electrical_circuits(self) -> None:
        with patch(
            "nce.vertical_modules.system_design.sow._supplier_name",
            return_value="Acme Integrator AS",
        ):
            sow_in = self._sample_sow_input()
            sow_in["electrical_circuits"] = [
                {
                    "course_no": 1,
                    "description": "AV Rack Power",
                    "in_a": 16.0,
                    "curve": "C",
                    "cable_mm2": 2.5,
                    "length_m": 12.0,
                    "power_w": 1800.0,
                    "rcd_ma": 30.0,
                }
            ]
            sow_in["earthing_system"] = "TN_S_400V_3P"
            sow_in["switchboard_id"] = "TAVLE-01"

            doc = generate_sow(sow_in, version_number=1)
            assert "kursfortegnelse" in doc
            kurs = doc["kursfortegnelse"]
            assert kurs["switchboard_id"] == "TAVLE-01"
            assert kurs["earthing_system"] == "TN_S_400V_3P"
            assert kurs["total_circuits"] == 1
            assert kurs["passed"] is True
