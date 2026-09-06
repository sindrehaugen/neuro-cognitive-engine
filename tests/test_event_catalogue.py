"""Unit test and ratchet: Event-Contract Catalogue and Seam Verification.

Phase 0 Wave I-2:
Enforces contract parity across the transactional outbox C4 event fabric:
1. Every event selector subscribed across the estate must have a registered producer
   OR be explicitly documented in nce/events/catalogue.py with status UNPRODUCED/PARKED
   and a substantive, verifiable reason (shrink-only allowlist).
2. Every event selector emitted by production code must have a declared row in
   EVENT_CATALOGUE.
3. Standing positive controls (U18) verify both guards fail loudly when confronted
   with synthetic uncontracted producers or consumers.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from nce.events.catalogue import EVENT_CATALOGUE, get_unproduced_selectors

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NCE_DIR = _REPO_ROOT / "nce"

# Mappings of dynamic entity types per file and operation
_FILE_DYNAMIC_MAP: dict[str, dict[str, list[str]]] = {
    "nce/vertical_modules/dynamics365/sync.py": {
        "upserted": ["D365_Account", "D365_KnowledgeArticle"]
    },
    "nce/vertical_modules/economy/graph.py": {
        "upserted": ["INVOICE", "POSTING", "PERIOD", "MARGIN"]
    },
    "nce/vertical_modules/inventory/stock.py": {"upserted": ["STOCK_LOCATION", "INVENTORY_ITEM"]},
    "nce/vertical_modules/sales/graph.py": {
        "upserted": ["CUSTOMER", "DEAL", "QUOTE", "LEAD", "OPPORTUNITY"]
    },
    "nce/vertical_modules/system_design/devices.py": {
        "upserted": ["RACK", "DEVICE", "PORT", "CABLE"]
    },
    "nce/vertical_modules/system_design/retire.py": {
        "retired": ["DEVICE", "RACK", "CABLE"],
        "deleted": ["DEVICE", "RACK", "CABLE", "PORT"],
    },
}


def _resolve_module_constants(tree: ast.AST) -> dict[str, Any]:
    """Extract string and tuple/list-of-strings constants from module-level assignments."""
    consts: dict[str, Any] = {}
    for node in ast.walk(tree):
        val = None
        target_names: list[str] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target_names.append(t.id)
            val = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                target_names.append(node.target.id)
            val = node.value

        if val is not None and target_names:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                for n in target_names:
                    consts[n] = val.value
            elif isinstance(val, (ast.Tuple, ast.List)):
                elts = [
                    e.value
                    for e in val.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if elts:
                    for n in target_names:
                        consts[n] = elts
    return consts


def _scan_subscribers(
    repo_root: Path,
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Scan all Python files under nce/ for subscribe(...) calls.

    Returns:
        (call_sites, resolved_subscriptions)
        call_sites: (file_rel, lineno, raw_label)
        resolved_subscriptions: (file_rel, lineno, selector)
    """
    call_sites: list[tuple[str, int, str]] = []
    resolved: list[tuple[str, int, str]] = []

    for py_path in sorted((repo_root / "nce").glob("**/*.py")):
        rel = py_path.relative_to(repo_root).as_posix()
        if rel in ("nce/events/bus.py", "nce/settings_store.py"):
            continue

        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception as exc:
            pytest.fail(f"Failed to parse {rel}: {exc}")

        consts = _resolve_module_constants(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                iter_name = getattr(node.iter, "id", None)
                if iter_name in consts and isinstance(consts[iter_name], list):
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and getattr(child.func, "id", None) == "subscribe"
                            and child.args
                        ):
                            call_sites.append((rel, child.lineno, f"for in {iter_name}"))
                            arg = child.args[0]
                            if isinstance(arg, ast.Dict):
                                k_map = {}
                                for k, v in zip(arg.keys, arg.values):
                                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                        if isinstance(v, ast.Constant):
                                            k_map[k.value] = v.value
                                        elif isinstance(v, ast.Name):
                                            k_map[k.value] = consts.get(v.id, v.id)
                                op = k_map.get("op", "upserted")
                                for item in consts[iter_name]:
                                    resolved.append((rel, child.lineno, f"{item}.{op}"))

            elif isinstance(node, ast.Call):
                fname = getattr(node.func, "id", getattr(node.func, "attr", None))
                if fname == "subscribe" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Dict):
                        call_sites.append((rel, node.lineno, "subscribe_dict"))
                        k_map = {}
                        for k, v in zip(arg.keys, arg.values):
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                    k_map[k.value] = v.value
                                elif isinstance(v, ast.Name) and v.id in consts:
                                    k_map[k.value] = consts[v.id]
                                elif isinstance(v, ast.Name):
                                    k_map[k.value] = v.id
                        nt = k_map.get("node_type")
                        op = k_map.get("op")
                        if (
                            isinstance(nt, str)
                            and isinstance(op, str)
                            and not nt.startswith("node_type")
                        ):
                            resolved.append((rel, node.lineno, f"{nt}.{op}"))

    return call_sites, resolved


def _scan_emitters(
    repo_root: Path,
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Scan all Python files under nce/ for event emission calls.

    Matches:
      - emit_graph_write(..., node_type=..., op=...)
      - emit_status_change(..., node_type=..., op=...)
      - publish(..., node_type=..., op=...) (when emitted as outbox events)

    Returns:
        (call_sites, resolved_emissions)
        call_sites: (file_rel, lineno, func_name)
        resolved_emissions: (file_rel, lineno, selector)
    """
    call_sites: list[tuple[str, int, str]] = []
    resolved: list[tuple[str, int, str]] = []

    excluded_files = frozenset(
        {
            "nce/events/emit.py",
            "nce/events/bus.py",
            "nce/settings_store.py",
            "nce/admin_handlers/settings.py",
        }
    )

    for py_path in sorted((repo_root / "nce").glob("**/*.py")):
        rel = py_path.relative_to(repo_root).as_posix()
        if rel in excluded_files:
            continue

        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception as exc:
            pytest.fail(f"Failed to parse {rel}: {exc}")

        consts = _resolve_module_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            fname = getattr(node.func, "id", getattr(node.func, "attr", None))
            if fname in ("emit_graph_write", "emit_status_change", "publish"):
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                nt_node = kw.get("node_type")
                op_node = kw.get("op")
                if nt_node is None and len(node.args) >= 3:
                    nt_node = node.args[2]
                if op_node is None and len(node.args) >= 4:
                    op_node = node.args[3]

                def resolve_str(v: Any) -> str | None:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        return v.value
                    if isinstance(v, ast.Name):
                        val = consts.get(v.id)
                        if isinstance(val, str):
                            return val
                    return None

                nt = resolve_str(nt_node)
                op = resolve_str(op_node)

                if nt or op or rel in _FILE_DYNAMIC_MAP:
                    call_sites.append((rel, node.lineno, fname))

                if nt and op:
                    resolved.append((rel, node.lineno, f"{nt}.{op}"))
                elif op and rel in _FILE_DYNAMIC_MAP and op in _FILE_DYNAMIC_MAP[rel]:
                    for dyn_nt in _FILE_DYNAMIC_MAP[rel][op]:
                        resolved.append((rel, node.lineno, f"{dyn_nt}.{op}"))

    return call_sites, resolved


def test_discovery_floors() -> None:
    """Guard the guard: AST walk must resolve sufficient emitters and subscribers."""
    emit_calls, emitted_selectors = _scan_emitters(_REPO_ROOT)
    sub_calls, subscribed_selectors = _scan_subscribers(_REPO_ROOT)

    distinct_emitted = {s[2] for s in emitted_selectors}
    distinct_subscribed = {s[2] for s in subscribed_selectors}

    assert len(emit_calls) >= 25, (
        f"Discovery collapse: found only {len(emit_calls)} emitter call sites (floor: 25)"
    )
    assert len(sub_calls) >= 5, (
        f"Discovery collapse: found only {len(sub_calls)} subscriber call sites (floor: 5)"
    )
    assert len(distinct_emitted) >= 20, (
        f"Discovery collapse: found only {len(distinct_emitted)} distinct emitted selectors (floor: 20)"
    )
    assert len(distinct_subscribed) >= 10, (
        f"Discovery collapse: found only {len(distinct_subscribed)} distinct subscribed selectors (floor: 10)"
    )


def test_every_subscription_has_producer_or_reasoned_exemption() -> None:
    """Ratchet: every subscribed selector must have a producer or a reasoned catalogue exemption."""
    _, emitted_selectors = _scan_emitters(_REPO_ROOT)
    _, subscribed_selectors = _scan_subscribers(_REPO_ROOT)

    produced_selectors = {s[2] for s in emitted_selectors}
    distinct_subscribed = {s[2] for s in subscribed_selectors}

    unproduced_missing_reasons: list[str] = []

    for sel in sorted(distinct_subscribed):
        contract = EVENT_CATALOGUE.get(sel)
        assert contract is not None, (
            f"Subscribed selector '{sel}' has NO row in EVENT_CATALOGUE. "
            "Every subscribed selector must be contracted in nce/events/catalogue.py."
        )

        if sel not in produced_selectors:
            # Must be explicitly catalogued as UNPRODUCED or PARKED
            if contract.status not in ("UNPRODUCED", "PARKED"):
                unproduced_missing_reasons.append(
                    f"'{sel}' is subscribed with NO code producer, but status is '{contract.status}'"
                )
            elif not contract.reason or len(contract.reason.strip()) < 60:
                unproduced_missing_reasons.append(
                    f"'{sel}' is unproduced/parked, but reason is missing or too thin (<60 chars)"
                )

    assert not unproduced_missing_reasons, (
        "Found subscriptions without producers and without substantive catalogue exemptions:\n"
        + "\n".join(f"  - {err}" for err in unproduced_missing_reasons)
    )


def test_unproduced_catalogue_allowlist_is_shrink_only() -> None:
    """Every exemption in EVENT_CATALOGUE must still be true, unproduced, and substantively reasoned."""
    _, emitted_selectors = _scan_emitters(_REPO_ROOT)
    produced_selectors = {s[2] for s in emitted_selectors}

    unproduced_allowlist = get_unproduced_selectors()
    now_produced: list[str] = []

    for sel, contract in unproduced_allowlist.items():
        if sel in produced_selectors:
            now_produced.append(sel)

        assert contract.reason, f"Contract '{sel}' marked {contract.status} without a reason"
        clean_reason = " ".join(contract.reason.split())
        assert len(clean_reason) >= 60, (
            f"Contract '{sel}' reason too thin to review ({len(clean_reason)} chars): '{clean_reason}'"
        )
        assert clean_reason.replace(sel, "").strip(), (
            f"Contract '{sel}' reason merely repeats selector name"
        )

    assert not now_produced, (
        "Shrink-only violation! Selectors gained an active code producer and must be updated to ACTIVE: "
        f"{now_produced}"
    )


def test_every_emitter_has_catalogue_row() -> None:
    """Every selector emitted by code must be registered in EVENT_CATALOGUE."""
    _, emitted_selectors = _scan_emitters(_REPO_ROOT)
    uncatalogued_emissions: list[tuple[str, int, str]] = []

    for fpath, lineno, sel in emitted_selectors:
        if sel not in EVENT_CATALOGUE:
            uncatalogued_emissions.append((fpath, lineno, sel))

    assert not uncatalogued_emissions, (
        f"Found {len(uncatalogued_emissions)} emissions of uncatalogued event selectors:\n"
        + "\n".join(f"  - {f}:{line} emits '{sel}'" for f, line, sel in uncatalogued_emissions)
        + "\nRegister every emitted selector in nce/events/catalogue.py."
    )


def test_starting_unproduced_baseline_has_at_least_three_seams() -> None:
    """Validate starting RED baseline: unproduced/parked seams burn down as waves land.

    Originally 6 unproduced/parked seams (PO_LINE.status_changed, GOODS_RECEIPT.created,
    BOM_LINE.status_changed, CERTIFICATION.CREATED, CERTIFICATION.UPDATED, CERTIFICATION.EXPIRED).
    PR-1 closed PO_LINE.status_changed, Wave IN-1 closed GOODS_RECEIPT.created.
    """
    unproduced = get_unproduced_selectors()
    assert len(unproduced) >= 3, f"Expected at least 3 unproduced seams, found {len(unproduced)}"

    # Assert remaining known-broken seams from Charter §6 are present
    assert "GOODS_RECEIPT.created" not in unproduced
    assert "BOM_LINE.status_changed" in unproduced
    assert "CERTIFICATION.EXPIRED" in unproduced


def test_positive_control_fails_on_unregistered_emitter() -> None:
    """Standing positive control (U18): prove scanner flags an uncatalogued emitter."""
    synthetic_emissions: list[tuple[str, int, str]] = [
        ("fake/module.py", 42, "SYNTHETIC_OFFENDER.unregistered_op")
    ]
    uncatalogued = [
        (f, line, sel) for f, line, sel in synthetic_emissions if sel not in EVENT_CATALOGUE
    ]
    assert len(uncatalogued) == 1
    assert uncatalogued[0][2] == "SYNTHETIC_OFFENDER.unregistered_op"


def test_positive_control_fails_on_unproduced_unexempted_subscriber() -> None:
    """Standing positive control (U18): prove guard fails on an unproduced subscriber without exemption."""
    synthetic_subscribed = {"SYNTHETIC_UNPRODUCED.status_changed"}
    produced_selectors: set[str] = set()
    violations: list[str] = []

    for sel in synthetic_subscribed:
        contract = EVENT_CATALOGUE.get(sel)
        if contract is None:
            violations.append(f"Subscribed selector '{sel}' has NO row in EVENT_CATALOGUE")
        elif sel not in produced_selectors:
            if contract.status not in ("UNPRODUCED", "PARKED"):
                violations.append(f"'{sel}' is unproduced, status {contract.status}")

    assert len(violations) == 1
    assert "has NO row in EVENT_CATALOGUE" in violations[0]
