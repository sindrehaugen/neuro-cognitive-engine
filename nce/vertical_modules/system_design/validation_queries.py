"""
nce/vertical_modules/system_design/validation_queries.py
=========================================================
Phase-2 (additive) design-validation graph queries for the System Design
vertical module.

Five design-quality checks — each split into:

1. A **pure function** over pre-fetched data structures (no DB, unit-testable):
   ``check_<name>(devices, ports, edges, capabilities) -> CheckResult``

2. A **thin fetch layer** that reads the graph + capability table via
   ``scoped_pg_session``, then calls the pure function.  Since M6.W13a the four
   readers themselves live in ``read.py`` — the module's one query set, shared
   with the ``system_design_get_topology`` tool — and are imported from there.

3. An **aggregator** ``validate_design_graph(engine, params)`` that runs all five
   checks and returns the same ``{passed: bool, reasons: list[str]}`` shape as
   Phase-1 ``do_validate_design``.

The five checks
---------------
1. **signal_flow_continuity** — every input PORT has at least one inbound
   ``connected_to`` edge (no dangling inputs in the signal chain).
2. **port_format_compatibility** — each ``connected_to`` edge connects ports
   whose ``signal_format`` / ``signal_version`` are compatible
   (HDMI 2.1 → 2.0 downgrade is a failure; Dante → Dante is fine).
3. **power_heat_budget** — aggregate ``power_draw_watts`` and ``heat_btu_hr``
   across all devices; returns reasons listing per-metric totals so an operator
   can set a budget ceiling (no hard-coded limit — the check is always
   ``passed=True`` but returns reasons with the totals for human review).
4. **spof_redundancy** — devices with ``redundancy_role='primary'`` must have at
   least one sibling with ``redundancy_role='secondary'`` in the same design;
   standalone devices are ignored.
5. **avixa_checkpoint_conformance** — every DEVICE node must have a non-null
   ``device_category``; every PORT node must have a non-null ``signal_format``
   and a valid ``port_direction``.

All checks return ``{"passed": bool, "reasons": list[str]}``.
``validate_design_graph`` ANDs all checks — if any fails, passed=False.

Design invariants (uncle-bob-craft)
------------------------------------
- SRP: each check is one function, one concern.
- Pure functions depend only on plain dicts/lists — no DB, no I/O.
- Fetch layer depends on pure function — not the reverse.
- No web / HTTP / admin imports.
- PROPOSE-ONLY: no auto-fix, no mutations — read-only queries.
- ENRICH-NEVER-REWRITE: Phase-1 ``do_validate_design`` is NOT touched.

Compatibility-version ordering
--------------------------------
The ``_hdmi_version_order`` dict encodes the known HDMI downgrade hierarchy.
Adding support for more formats follows the same pattern: add a mapping.
"""

from __future__ import annotations

import logging
from typing import Any

from nce.db_utils import scoped_pg_session

# The four graph/capability readers used by validate_design_graph were promoted
# into read.py (M6.W13a) so that the MCP/REST topology tool and these validation
# checks share ONE query set instead of two copies that could drift.  They are
# imported under their original private names, so this module's behaviour — and
# its tests — are unchanged.
from nce.vertical_modules.system_design.read import (
    _fetch_connections_and_capabilities,
    _fetch_device_capabilities,
    _fetch_port_capabilities,
    _fetch_port_directions,
)

log = logging.getLogger("nce.vertical_modules.system_design.validation_queries")

# ---------------------------------------------------------------------------
# Result type alias — mirrors do_validate_design's return shape.
# ---------------------------------------------------------------------------
CheckResult = dict[str, Any]  # {"passed": bool, "reasons": list[str]}

# ---------------------------------------------------------------------------
# Format/version compatibility tables.
# ---------------------------------------------------------------------------

# HDMI: higher version numbers are backward-compatible as OUTPUT → INPUT.
# A source at version X can drive a sink at version Y only if X >= Y.
# Stored as {version_str: numeric_ordinal}.
_HDMI_VERSION_ORDER: dict[str, int] = {
    "1.0": 10,
    "1.1": 11,
    "1.2": 12,
    "1.3": 13,
    "1.4": 14,
    "2.0": 20,
    "2.1": 21,
}

# DP: same pattern.
_DP_VERSION_ORDER: dict[str, int] = {
    "1.1": 11,
    "1.2": 12,
    "1.4": 14,
    "2.0": 20,
    "2.1": 21,
}

# Cross-format compatibility: (src_format, dst_format) pairs that are OK.
# E.g. Dante → AES67 is a defined standard mapping.
_CROSS_FORMAT_COMPAT: frozenset[tuple[str, str]] = frozenset(
    {
        ("DANTE", "AES67"),
        ("AES67", "DANTE"),
    }
)

# Valid port_direction values (mirrors the CHECK constraint in migration
# 039_system_design_device_capabilities.sql, which is what creates the table;
# 038 is 039's predecessor, system_design_source_id, and creates nothing here).
_VALID_PORT_DIRECTIONS: frozenset[str] = frozenset({"input", "output", "bidirectional"})


# ---------------------------------------------------------------------------
# Pure check #1 — signal-flow continuity.
# ---------------------------------------------------------------------------


def check_signal_flow_continuity(
    input_port_labels: list[str],
    connected_to_targets: set[str],
) -> CheckResult:
    """Check that every declared input PORT has at least one inbound connection.

    Parameters
    ----------
    input_port_labels:
        Labels of all PORT nodes whose ``port_direction == 'input'``.
    connected_to_targets:
        Set of PORT labels that appear as the object of at least one
        ``connected_to`` edge (i.e. they receive a signal).

    Returns
    -------
    CheckResult
        ``{"passed": bool, "reasons": list[str]}``
    """
    reasons: list[str] = []
    for label in input_port_labels:
        if label not in connected_to_targets:
            reasons.append(
                f"input port '{label}' has no inbound connected_to edge (dangling input)"
            )
    return {"passed": len(reasons) == 0, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pure check #2 — port/format compatibility.
# ---------------------------------------------------------------------------


def _formats_compatible(
    src_fmt: str | None,
    src_ver: str | None,
    dst_fmt: str | None,
    dst_ver: str | None,
) -> tuple[bool, str]:
    """Return (compatible, reason_if_not).

    Rules:
    - Identical formats: version check (src_ver >= dst_ver for versioned formats).
    - Cross-format: allowed only if in ``_CROSS_FORMAT_COMPAT``.
    - None values (unknown format): treated as compatible (warn-only).
    """
    if src_fmt is None or dst_fmt is None:
        return True, ""

    src_fmt_up = src_fmt.upper()
    dst_fmt_up = dst_fmt.upper()

    # Cross-format check first.
    if src_fmt_up != dst_fmt_up:
        if (src_fmt_up, dst_fmt_up) in _CROSS_FORMAT_COMPAT:
            return True, ""
        return False, (f"format mismatch: output '{src_fmt}' cannot drive input '{dst_fmt}'")

    # Same format — version check where applicable.
    version_table: dict[str, int] | None = None
    if src_fmt_up == "HDMI":
        version_table = _HDMI_VERSION_ORDER
    elif src_fmt_up in ("DP", "DISPLAYPORT"):
        version_table = _DP_VERSION_ORDER

    if version_table is None:
        return True, ""

    # Both versions present — compare.
    if src_ver is not None and dst_ver is not None:
        src_ord = version_table.get(src_ver, -1)
        dst_ord = version_table.get(dst_ver, -1)
        if src_ord < dst_ord:
            return False, (
                f"{src_fmt} version mismatch: source {src_ver!r} (ord={src_ord}) "
                f"cannot drive sink {dst_ver!r} (ord={dst_ord})"
            )

    # Dante channel count check (symmetric format).
    return True, ""


def check_port_format_compatibility(
    connections: list[dict[str, Any]],
    capability_by_label: dict[str, dict[str, Any]],
) -> CheckResult:
    """Check that every connected_to edge connects format-compatible ports.

    Parameters
    ----------
    connections:
        List of ``{"from_port": str, "to_port": str}`` dicts representing
        ``connected_to`` edges in the design.
    capability_by_label:
        Map of ``node_label -> capability dict`` for all PORT nodes.
        Each dict may include ``signal_format``, ``signal_version``,
        ``dante_rx_channels``, ``dante_tx_channels``.

    Returns
    -------
    CheckResult
    """
    reasons: list[str] = []

    for cnx in connections:
        src_label = cnx.get("from_port", "")
        dst_label = cnx.get("to_port", "")
        src_cap = capability_by_label.get(src_label, {})
        dst_cap = capability_by_label.get(dst_label, {})

        ok, reason = _formats_compatible(
            src_cap.get("signal_format"),
            src_cap.get("signal_version"),
            dst_cap.get("signal_format"),
            dst_cap.get("signal_version"),
        )
        if not ok:
            reasons.append(f"connection '{src_label}' -> '{dst_label}': {reason}")
            continue

        # Dante channel-count check: TX channels on source >= RX channels on sink.
        src_fmt_str = (src_cap.get("signal_format") or "").upper()
        dst_fmt_str = (dst_cap.get("signal_format") or "").upper()
        if src_fmt_str == "DANTE" and dst_fmt_str == "DANTE":
            tx = src_cap.get("dante_tx_channels")
            rx = dst_cap.get("dante_rx_channels")
            if tx is not None and rx is not None and tx < rx:
                reasons.append(
                    f"Dante channel mismatch on '{src_label}' -> '{dst_label}': "
                    f"source tx={tx} < sink rx={rx}"
                )

    return {"passed": len(reasons) == 0, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pure check #3 — PoE / power / heat budget.
# ---------------------------------------------------------------------------


def check_power_heat_budget(
    device_capabilities: list[dict[str, Any]],
) -> CheckResult:
    """Aggregate power and heat across devices; return totals in reasons.

    This check is informational — it always passes (the operator sets their
    own ceiling).  The reasons list carries the totals so the caller can
    present them to a human reviewer.

    Parameters
    ----------
    device_capabilities:
        List of capability dicts for DEVICE nodes (not ports).
        Each may include ``power_draw_watts`` and ``heat_btu_hr``.

    Returns
    -------
    CheckResult
        Always ``passed=True``; ``reasons`` lists aggregate totals.
    """
    total_power = sum(float(d.get("power_draw_watts") or 0) for d in device_capabilities)
    total_heat = sum(float(d.get("heat_btu_hr") or 0) for d in device_capabilities)

    reasons = [
        f"total power draw: {total_power:.1f} W across {len(device_capabilities)} device(s)",
        f"total heat dissipation: {total_heat:.1f} BTU/hr",
    ]
    return {"passed": True, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pure check #4 — SPOF / redundancy.
# ---------------------------------------------------------------------------


def check_spof_redundancy(
    device_capabilities: list[dict[str, Any]],
) -> CheckResult:
    """Check that every primary device has at least one secondary peer.

    Parameters
    ----------
    device_capabilities:
        List of capability dicts for DEVICE nodes.
        Each may include ``redundancy_role``.

    Returns
    -------
    CheckResult
    """
    primary_count = sum(1 for d in device_capabilities if d.get("redundancy_role") == "primary")
    secondary_count = sum(1 for d in device_capabilities if d.get("redundancy_role") == "secondary")

    reasons: list[str] = []
    if primary_count > 0 and secondary_count == 0:
        reasons.append(
            f"SPOF risk: {primary_count} primary device(s) found but no secondary "
            "devices are present in the design"
        )
    elif primary_count > secondary_count:
        reasons.append(
            f"SPOF risk: {primary_count} primary device(s) but only "
            f"{secondary_count} secondary device(s) — consider adding more secondaries"
        )
    return {"passed": len(reasons) == 0, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pure check #5 — AVIXA checkpoint conformance.
# ---------------------------------------------------------------------------


def check_avixa_checkpoint_conformance(
    device_caps: list[dict[str, Any]],
    port_caps: list[dict[str, Any]],
) -> CheckResult:
    """Check AVIXA parameter completeness for all devices and ports.

    Rules:
    - Every DEVICE must have a non-null ``device_category``.
    - Every PORT must have a non-null ``signal_format``.
    - Every PORT must have a ``port_direction`` in the valid set.

    Parameters
    ----------
    device_caps:
        List of ``{"node_label": str, <capability fields>}`` dicts for DEVICE nodes.
    port_caps:
        List of ``{"node_label": str, <capability fields>}`` dicts for PORT nodes.

    Returns
    -------
    CheckResult
    """
    reasons: list[str] = []

    for d in device_caps:
        label = d.get("node_label", "<unknown>")
        if not d.get("device_category"):
            reasons.append(f"DEVICE '{label}': missing 'device_category' (AVIXA required)")

    for p in port_caps:
        label = p.get("node_label", "<unknown>")
        if not p.get("signal_format"):
            reasons.append(f"PORT '{label}': missing 'signal_format' (AVIXA required)")
        direction = p.get("port_direction")
        if direction is not None and direction not in _VALID_PORT_DIRECTIONS:
            reasons.append(
                f"PORT '{label}': invalid 'port_direction' {direction!r}; "
                f"must be one of {sorted(_VALID_PORT_DIRECTIONS)}"
            )

    return {"passed": len(reasons) == 0, "reasons": reasons}


# ---------------------------------------------------------------------------
# Pure check #6 — electrical NEK 400 / EN 61439 compliance.
# ---------------------------------------------------------------------------


def check_electrical_nek_compliance(
    circuits: list[dict[str, Any]] | None = None,
    earthing_system: str = "TN_S_400V_3P",
) -> CheckResult:
    """Check electrical installation compliance per NEK 400 / EN 61439.

    Parameters
    ----------
    circuits:
        Optional list of circuit definitions. If None or empty, check passes cleanly.
    earthing_system:
        Earthing system identifier (defaults to "TN_S_400V_3P").

    Returns
    -------
    CheckResult
        ``{"passed": bool, "reasons": list[str]}``
    """
    from nce.vertical_modules.system_design.electrical_nek import generate_kursfortegnelse

    if not circuits:
        return {
            "passed": True,
            "reasons": ["No electrical circuits provided; NEK 400 compliance check skipped."],
        }

    kurs_res = generate_kursfortegnelse(circuits=circuits, earthing_system=earthing_system)
    reasons = list(kurs_res.get("all_violations", []))
    passed = bool(kurs_res.get("passed", False))
    if not reasons and passed:
        reasons = [
            f"NEK 400 / EN 61439 electrical compliance passed: {len(circuits)} circuits verified "
            f"under {earthing_system}."
        ]
    return {
        "passed": passed,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Pure check #7 — maritime & offshore DNV / IEC 60092 / ATEX compliance.
# ---------------------------------------------------------------------------


def check_dnv_marine_compliance(
    marine_config: dict[str, Any] | None = None,
) -> CheckResult:
    """Check maritime and offshore installation compliance per DNV / IEC 60092 / ATEX.

    Parameters
    ----------
    marine_config:
        Optional dictionary containing maritime switchboard parameters,
        cable specifications, and hazardous area / environmental specs.
        If None or empty, check passes cleanly with skipped notice.

    Returns
    -------
    CheckResult
        ``{"passed": bool, "reasons": list[str]}``
    """
    from nce.vertical_modules.system_design.dnv_marine import (
        validate_atex_and_environment,
        validate_dnv_switchboard_and_bus,
        validate_marine_cable_iec60092,
    )

    if not marine_config:
        return {
            "passed": True,
            "reasons": [
                "No maritime/offshore configuration provided; DNV compliance check skipped."
            ],
        }

    reasons: list[str] = []
    passed = True

    # 1. Switchboard and busbar validation (DNV-RU-SHIP Pt.4 Ch.8 / NEK 410 / SOLAS)
    sw_cfg = marine_config.get("switchboard") or marine_config
    if (
        "earthing_system" in sw_cfg
        or "essential_consumers" in sw_cfg
        or "msb_location_id" in sw_cfg
        or "esb_location_id" in sw_cfg
        or "switchboard" in marine_config
    ):
        earthing = str(sw_cfg.get("earthing_system") or "IT_230V_3P")
        has_imd = bool(sw_cfg.get("has_imd", True))
        msb_loc = str(sw_cfg.get("msb_location_id") or "ZONE_MSB")
        esb_loc = str(sw_cfg.get("esb_location_id") or "ZONE_ESB")
        consumers = sw_cfg.get("essential_consumers") or []
        thd_pct = float(sw_cfg.get("thd_voltage_pct", 0.0))

        sw_res = validate_dnv_switchboard_and_bus(
            earthing_system=earthing,
            has_imd=has_imd,
            msb_location_id=msb_loc,
            esb_location_id=esb_loc,
            essential_consumers=consumers,
            thd_voltage_pct=thd_pct,
        )
        if not sw_res.get("compliant", False):
            passed = False
            reasons.extend(sw_res.get("violations", []))

    # 2. Marine cabling validation (IEC 60092-350/360, IEC 60332-3-22, IEC 60331)
    cables = (
        marine_config.get("cables")
        or marine_config.get("marine_cables")
        or marine_config.get("cable_specs")
    )
    if not cables and "cable_spec" in marine_config:
        cables = [marine_config["cable_spec"]]

    if cables:
        for c in cables:
            is_emerg = bool(c.get("is_emergency_circuit", False))
            c_res = validate_marine_cable_iec60092(cable_spec=c, is_emergency_circuit=is_emerg)
            if not c_res.get("compliant", False):
                passed = False
                reasons.extend(c_res.get("violations", []))

    # 3. Hazardous area & environmental validation (ATEX 2014/34/EU, EN 60079, IP/IK/ISO 12944)
    atex_items = (
        marine_config.get("atex_checks")
        or marine_config.get("hazardous_areas")
        or marine_config.get("equipment")
    )
    if not atex_items and ("device_spec" in marine_config or "location_env" in marine_config):
        dev = marine_config.get("device_spec") or {}
        env = marine_config.get("location_env") or {}
        atex_items = [{"device_spec": dev, "location_env": env}]

    if atex_items:
        for item in atex_items:
            dev = item.get("device_spec") or item.get("device") or item
            env = (
                item.get("location_env")
                or item.get("environment")
                or marine_config.get("location_env", {})
            )
            a_res = validate_atex_and_environment(device_spec=dev, location_env=env)
            if not a_res.get("compliant", False):
                passed = False
                reasons.extend(a_res.get("violations", []))

    if passed and not reasons:
        reasons = [
            "DNV maritime & ATEX safety compliance passed: switchboard, marine cabling, "
            "and environmental protection verified."
        ]

    return {
        "passed": passed,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Pure check #8 — electronics QC, stress derating & RoHS/IPC/ESD compliance.
# ---------------------------------------------------------------------------


def check_electronics_qc_compliance(
    electronics_config: dict[str, Any] | None = None,
) -> CheckResult:
    """Check electronics R&D and PCBA quality control compliance per ECSS/RoHS/IPC/ESD.

    Parameters
    ----------
    electronics_config:
        Optional dictionary containing component derating specs, BOM lines for
        RoHS/REACH/MSL, and ESD workstation measurements.
        If None or empty, check passes cleanly with skipped notice.

    Returns
    -------
    CheckResult
        ``{"passed": bool, "reasons": list[str]}``
    """
    from nce.vertical_modules.system_design.electronics_qc import (
        validate_component_derating,
        validate_en61340_esd_workstation,
        validate_eu_electronics_compliance,
    )

    if not electronics_config:
        return {
            "passed": True,
            "reasons": [
                "No electronics QC configuration provided; electronics compliance check skipped."
            ],
        }

    reasons: list[str] = []
    passed = True

    # 1. Component derating validation (ECSS-Q-ST-30-11C / IEC 61508 / ISO 26262)
    comps = (
        electronics_config.get("components")
        or electronics_config.get("derating_components")
        or electronics_config.get("derating")
    )
    if comps and isinstance(comps, list):
        s_class = str(electronics_config.get("safety_class", "industrial"))
        derate_res = validate_component_derating(components=comps, safety_class=s_class)
        if not derate_res.get("passed", False):
            passed = False
            for v in derate_res.get("violations", []):
                reasons.append(v.get("message", str(v)) if isinstance(v, dict) else str(v))

    # 2. EU electronics compliance (RoHS 3, REACH SVHC, IPC/JEDEC J-STD-033 MSL)
    bom = electronics_config.get("bom_lines") or electronics_config.get("bom")
    if bom and isinstance(bom, list):
        rohs_req = bool(electronics_config.get("require_rohs", True))
        reach_req = bool(electronics_config.get("require_reach", True))
        ce_req = bool(electronics_config.get("require_ce", False))
        eu_res = validate_eu_electronics_compliance(
            bom_lines=bom,
            require_rohs=rohs_req,
            require_reach=reach_req,
            require_ce=ce_req,
        )
        if not eu_res.get("passed", False):
            passed = False
            for v in eu_res.get("violations", []):
                reasons.append(v.get("message", str(v)) if isinstance(v, dict) else str(v))

    # 3. EN 61340-5-1 ESD Workstation validation
    esd_cfg = (
        electronics_config.get("esd")
        or electronics_config.get("esd_workstation")
        or electronics_config
    )
    w_strap = esd_cfg.get("wrist_strap_mohm")
    mat_ground = esd_cfg.get("mat_to_ground_ohm")
    if w_strap is not None and mat_ground is not None:
        foot_ohm = esd_cfg.get("footwear_ohm")
        foot_val = float(foot_ohm) if foot_ohm is not None else None
        rh_pct = float(esd_cfg.get("relative_humidity_pct", 45.0))
        esd_res = validate_en61340_esd_workstation(
            wrist_strap_mohm=float(w_strap),
            mat_to_ground_ohm=float(mat_ground),
            footwear_ohm=foot_val,
            relative_humidity_pct=rh_pct,
        )
        if not esd_res.get("passed", False):
            passed = False
            reasons.extend(esd_res.get("violations", []))

    if passed and not reasons:
        reasons = [
            "Electronics QC compliance passed: stress derating, EU RoHS/REACH/MSL, "
            "and EN 61340-5-1 ESD protection verified."
        ]

    return {
        "passed": passed,
        "reasons": reasons,
    }


def run_all_validations(
    *,
    input_port_labels: list[str] | None = None,
    connected_to_targets: set[str] | None = None,
    connections: list[dict[str, Any]] | None = None,
    cap_by_label: dict[str, Any] | None = None,
    device_caps: list[dict[str, Any]] | None = None,
    port_caps: list[dict[str, Any]] | None = None,
    electrical_circuits: list[dict[str, Any]] | None = None,
    earthing_system: str = "TN_S_400V_3P",
    marine_config: dict[str, Any] | None = None,
    electronics_config: dict[str, Any] | None = None,
) -> list[CheckResult]:
    """Run pure validation checks on pre-fetched data structures.

    Returns a list of CheckResult dicts. If optional domain configurations
    (electrical_circuits, marine_config, electronics_config) are None,
    returns exactly the 5 baseline checks for full backward-compatibility.
    """
    r1 = check_signal_flow_continuity(input_port_labels or [], connected_to_targets or set())
    r2 = check_port_format_compatibility(connections or [], cap_by_label or {})
    r3 = check_power_heat_budget(device_caps or [])
    r4 = check_spof_redundancy(device_caps or [])
    r5 = check_avixa_checkpoint_conformance(device_caps or [], port_caps or [])
    checks = [r1, r2, r3, r4, r5]
    if electrical_circuits is not None:
        checks.append(check_electrical_nek_compliance(electrical_circuits, earthing_system))
    if marine_config is not None:
        checks.append(check_dnv_marine_compliance(marine_config))
    if electronics_config is not None:
        checks.append(check_electronics_qc_compliance(electronics_config))
    return checks


# ---------------------------------------------------------------------------
# Public aggregator — validate_design_graph
# ---------------------------------------------------------------------------


async def validate_design_graph(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run all five design-validation checks against the live graph.

    This function is additive on top of Phase-1 ``do_validate_design``:
    it runs the structural/AVIXA checks on the device topology layer added
    by Phase-2 ``do_author_device_topology``.  It does NOT call or alter
    ``do_validate_design``.

    Parameters
    ----------
    engine:
        NCEEngine instance with a live ``engine.pg_pool``.
    params:
        ``{
            "namespace_id": str | UUID,   # required
            "design_id": str,             # required
            "electrical_circuits": list,  # optional Wave E-1 circuits
            "earthing_system": str,       # optional Wave E-1 earthing system
            "marine_config": dict,        # optional Wave E-2 maritime/ATEX config
            "electronics_config": dict,   # optional Wave E-3 electronics QC config
        }``

    Returns
    -------
    dict
        ``{"passed": bool, "reasons": list[str]}``
        Mirrors the shape of Phase-1 ``do_validate_design``.
        ``passed`` is True only when all checks pass.
        ``reasons`` is the union of all failure reasons (and always includes
        the power/heat budget totals from check #3).

    Raises
    ------
    ValueError
        When required params are missing.
    """
    from uuid import UUID as _UUID

    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("validate_design_graph: 'namespace_id' is required in params")
    ns_uuid = _UUID(str(ns_raw)) if not isinstance(ns_raw, _UUID) else ns_raw

    design_id_raw: str = params.get("design_id", "")
    if not design_id_raw:
        raise ValueError("validate_design_graph: 'design_id' is required in params")

    design_lbl = f"DESIGN:{design_id_raw.upper()}"

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        # Fetch all data in one scoped session.  ns_uuid is threaded through
        # every helper so queries are namespace-scoped at the SQL level, not
        # just via RLS (which may be bypassed by owner pools in tests).
        input_port_labels, connected_to_targets = await _fetch_port_directions(
            conn, ns_uuid, design_lbl
        )
        connections, cap_by_label = await _fetch_connections_and_capabilities(
            conn, ns_uuid, design_lbl
        )
        device_caps = await _fetch_device_capabilities(conn, ns_uuid, design_lbl)
        port_caps = await _fetch_port_capabilities(conn, ns_uuid, design_lbl)

    # Run pure checks (no DB).
    r1 = check_signal_flow_continuity(input_port_labels, connected_to_targets)
    r2 = check_port_format_compatibility(connections, cap_by_label)
    r3 = check_power_heat_budget(device_caps)
    r4 = check_spof_redundancy(device_caps)
    r5 = check_avixa_checkpoint_conformance(device_caps, port_caps)

    all_checks = [r1, r2, r3, r4, r5]
    if params.get("electrical_circuits") is not None:
        r6 = check_electrical_nek_compliance(
            circuits=params.get("electrical_circuits"),
            earthing_system=params.get("earthing_system", "TN_S_400V_3P"),
        )
        all_checks.append(r6)
    if params.get("marine_config") is not None:
        r_marine = check_dnv_marine_compliance(params.get("marine_config"))
        all_checks.append(r_marine)
    if params.get("electronics_config") is not None:
        r_elec = check_electronics_qc_compliance(params.get("electronics_config"))
        all_checks.append(r_elec)

    all_reasons: list[str] = []
    for chk in all_checks:
        all_reasons.extend(chk.get("reasons", []))

    # Power/heat totals from r3 are always included (informational) but r3
    # never sets passed=False — overall passed is AND of all check.passed.
    passed = all(chk["passed"] for chk in all_checks)

    log.info(
        "validate_design_graph: ns=%s design=%s passed=%s checks=%d reasons=%d",
        ns_uuid,
        design_id_raw,
        passed,
        len(all_checks),
        len(all_reasons),
    )
    return {"passed": passed, "reasons": all_reasons}
