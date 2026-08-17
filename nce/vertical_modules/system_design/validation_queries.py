"""
nce/vertical_modules/system_design/validation_queries.py
=========================================================
Phase-2 (additive) design-validation graph queries for the System Design
vertical module.

Five design-quality checks — each split into:

1. A **pure function** over pre-fetched data structures (no DB, unit-testable):
   ``check_<name>(devices, ports, edges, capabilities) -> CheckResult``

2. A **thin fetch layer** that reads the graph + capability table via
   ``scoped_pg_session``, then calls the pure function.

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

# Valid port_direction values (mirrors CHECK constraint in migration 038).
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
# Fetch helpers — read graph + capability table via scoped_pg_session.
# ---------------------------------------------------------------------------


async def _fetch_port_directions(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> tuple[list[str], set[str]]:
    """Return (input_port_labels, connected_to_targets).

    Fetches:
    - All PORT nodes reachable from the DESIGN via DEVICE -[has_port]-> PORT,
      filtered to those with port_direction='input'.
    - All target labels of ``connected_to`` edges within the design's scope.

    Both queries are explicitly scoped to ``ns_uuid`` so that identical design
    labels in different namespaces (e.g. multiple ``make_namespace()`` test runs)
    do not bleed into each other.  This avoids the ``LIMIT 1`` subquery that
    previously picked an arbitrary namespace for the same design label.
    """
    # Input ports: PORT nodes with direction='input' under this DESIGN,
    # all rows pinned to ns_uuid.
    input_rows = await conn.fetch(
        """
        SELECT kn.label
        FROM kg_nodes kn
        JOIN kg_edges dev_port
            ON dev_port.object_label = kn.label
            AND dev_port.predicate = 'has_port'
            AND dev_port.namespace_id = $2::uuid
        JOIN kg_edges design_dev
            ON design_dev.object_label = dev_port.subject_label
            AND design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        JOIN system_design_device_capabilities sddc
            ON sddc.node_label = kn.label
            AND sddc.namespace_id = $2::uuid
        WHERE kn.entity_type = 'PORT'
          AND kn.namespace_id = $2::uuid
          AND sddc.port_direction = 'input'
        """,
        design_label,
        ns_uuid,
    )
    input_port_labels = [r["label"] for r in input_rows]

    # connected_to targets within this namespace (any PORT that receives a signal).
    # Directly scoped — no label→namespace subquery needed.
    target_rows = await conn.fetch(
        """
        SELECT DISTINCT object_label
        FROM kg_edges
        WHERE predicate = 'connected_to'
          AND namespace_id = $1::uuid
        """,
        ns_uuid,
    )
    connected_to_targets = {r["object_label"] for r in target_rows}

    return input_port_labels, connected_to_targets


async def _fetch_connections_and_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (connections, capability_by_label) for the PORT format check.

    connections: list of {from_port, to_port}
    capability_by_label: {label: {signal_format, signal_version, dante_*}}

    All queries are explicitly scoped to ``ns_uuid``.
    """
    cnx_rows = await conn.fetch(
        """
        SELECT ke.subject_label AS from_port, ke.object_label AS to_port
        FROM kg_edges ke
        JOIN kg_edges design_dev
            ON design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        JOIN kg_edges dev_port
            ON dev_port.predicate = 'has_port'
            AND dev_port.subject_label = design_dev.object_label
            AND dev_port.namespace_id = $2::uuid
        WHERE ke.predicate = 'connected_to'
          AND ke.subject_label = dev_port.object_label
          AND ke.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    connections = [{"from_port": r["from_port"], "to_port": r["to_port"]} for r in cnx_rows]

    # Collect distinct port labels from this design.
    port_labels: set[str] = set()
    for c in connections:
        port_labels.add(c["from_port"])
        port_labels.add(c["to_port"])

    cap_by_label: dict[str, dict[str, Any]] = {}
    if port_labels:
        cap_rows = await conn.fetch(
            """
            SELECT node_label, signal_format, signal_version,
                   dante_rx_channels, dante_tx_channels
            FROM system_design_device_capabilities
            WHERE node_label = ANY($1::text[])
              AND namespace_id = $2::uuid
            """,
            list(port_labels),
            ns_uuid,
        )
        for r in cap_rows:
            cap_by_label[r["node_label"]] = dict(r)

    return connections, cap_by_label


async def _fetch_device_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> list[dict[str, Any]]:
    """Fetch capability rows for all DEVICE nodes under this DESIGN.

    Explicitly scoped to ``ns_uuid`` so parallel test namespaces do not bleed.
    """
    rows = await conn.fetch(
        """
        SELECT sddc.node_label, sddc.power_draw_watts, sddc.heat_btu_hr,
               sddc.redundancy_role, sddc.device_category
        FROM system_design_device_capabilities sddc
        JOIN kg_edges ke
            ON ke.object_label = sddc.node_label
            AND ke.predicate = 'contains'
            AND ke.subject_label = $1
            AND ke.namespace_id = $2::uuid
        JOIN kg_nodes kn
            ON kn.label = sddc.node_label
            AND kn.entity_type = 'DEVICE'
            AND kn.namespace_id = $2::uuid
        WHERE sddc.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    return [dict(r) for r in rows]


async def _fetch_port_capabilities(
    conn: Any,
    ns_uuid: Any,
    design_label: str,
) -> list[dict[str, Any]]:
    """Fetch capability rows for all PORT nodes under this DESIGN.

    Explicitly scoped to ``ns_uuid`` so parallel test namespaces do not bleed.
    """
    rows = await conn.fetch(
        """
        SELECT sddc.node_label, sddc.signal_format, sddc.port_direction
        FROM system_design_device_capabilities sddc
        JOIN kg_nodes kn
            ON kn.label = sddc.node_label
            AND kn.entity_type = 'PORT'
            AND kn.namespace_id = $2::uuid
        JOIN kg_edges dev_port
            ON dev_port.object_label = kn.label
            AND dev_port.predicate = 'has_port'
            AND dev_port.namespace_id = $2::uuid
        JOIN kg_edges design_dev
            ON design_dev.object_label = dev_port.subject_label
            AND design_dev.predicate = 'contains'
            AND design_dev.subject_label = $1
            AND design_dev.namespace_id = $2::uuid
        WHERE sddc.namespace_id = $2::uuid
        """,
        design_label,
        ns_uuid,
    )
    return [dict(r) for r in rows]


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
        }``

    Returns
    -------
    dict
        ``{"passed": bool, "reasons": list[str]}``
        Mirrors the shape of Phase-1 ``do_validate_design``.
        ``passed`` is True only when all five checks pass.
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
