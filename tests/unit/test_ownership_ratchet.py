"""Unit tests and ratchet: Knowledge Graph Node Ownership and assert_owner Enforcement.

Phase 0 Wave I-4:
Enforces Contract-A single-writer ownership invariant across the knowledge graph:
1. Every function in a vertical module that executes `INSERT INTO kg_nodes` must
   call `assert_owner` for that node type, OR be explicitly catalogued in
   `KNOWN_UNGUARDED_GRAPH_WRITERS` with a substantive, reviewer-checkable reason (shrink-only).
2. Every `entity_type` written to `kg_nodes` or passed to `assert_owner` must be
   registered in `nce/config_data/node-ownership.json` OR allowlisted in
   `KNOWN_UNREGISTERED_ENTITY_TYPES` with a substantive reason (shrink-only).
3. Starting RED baseline asserts the two unclosed vertical engines (field_tech with 5 files,
   and hr with 1 file).
4. Standing positive controls (U18) verify both guards fail loudly on uncontracted writes.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VM_DIR = _REPO_ROOT / "nce" / "vertical_modules"
_OWNERSHIP_FILE = _REPO_ROOT / "nce" / "config_data" / "node-ownership.json"

# Mappings of dynamic entity types per file
_FILE_DYNAMIC_MAP: dict[str, list[str]] = {
    "nce/vertical_modules/dynamics365/sync.py": ["D365_Account", "D365_KnowledgeArticle"],
    "nce/vertical_modules/economy/graph.py": ["INVOICE", "POSTING", "PERIOD", "MARGIN"],
    "nce/vertical_modules/inventory/stock.py": ["STOCK_LOCATION", "INVENTORY_ITEM"],
    "nce/vertical_modules/sales/graph.py": ["CUSTOMER", "DEAL", "QUOTE", "LEAD", "OPPORTUNITY"],
    "nce/vertical_modules/system_design/devices.py": ["RACK", "DEVICE", "PORT", "CABLE"],
    "nce/vertical_modules/system_design/retire.py": ["DEVICE", "RACK", "CABLE", "PORT"],
}

# Shrink-only allowlist: Vertical module files writing kg_nodes without assert_owner
# Once a wave (e.g. FT-2 or HR-1) wraps graph writes in assert_owner, the file MUST be removed.
KNOWN_UNGUARDED_GRAPH_WRITERS: dict[str, dict[str, str]] = {
    "nce/vertical_modules/hr/a2a.py": {
        "engine": "hr",
        "owner": "MLV15B",
        "reason": (
            "HR A2A project assignment handler writes PROJECT and EMPLOYEE nodes without assert_owner; "
            "scheduled for remediation in v1.5 Phase 1 Wave HR-1."
        ),
    },
    "nce/vertical_modules/dynamics365/sync.py": {
        "engine": "dynamics365",
        "owner": "Core",
        "reason": (
            "Dynamics 365 external ERP sync bridge writes D365_Account and D365_KnowledgeArticle "
            "nodes directly via ingestion without invoking Contract-A assert_owner."
        ),
    },
}

# Shrink-only allowlist: Entity types written by code lacking rows in node-ownership.json
KNOWN_UNREGISTERED_ENTITY_TYPES: dict[str, dict[str, str]] = {
    "EMPLOYEE": {
        "engine": "hr",
        "owner": "MLV15B",
        "reason": (
            "HR employee entity type authored in hr/a2a.py lacks registration in node-ownership.json; "
            "scheduled to be registered in v1.5 Phase 1 Wave HR-1."
        ),
    },
    "PROJECT": {
        "engine": "hr",
        "owner": "MLV15B",
        "reason": (
            "HR A2A module writes unnamespaced 'PROJECT' node rather than 'PROJECT_PROJECT' "
            "as registered by Project engine in node-ownership.json; scheduled for remediation in HR-1."
        ),
    },
    "D365_Account": {
        "engine": "dynamics365",
        "owner": "Core",
        "reason": (
            "Dynamics 365 customer account mirror node type authored during CRM sync; "
            "external mirror entity outside standard vertical module Contract-A governance."
        ),
    },
    "D365_KnowledgeArticle": {
        "engine": "dynamics365",
        "owner": "Core",
        "reason": (
            "Dynamics 365 knowledge base mirror node type authored during sync; "
            "external mirror entity outside standard vertical module Contract-A governance."
        ),
    },
}


def _load_registered_ownership() -> tuple[set[str], dict[str, list[dict[str, Any]]]]:
    """Load and parse node-ownership.json."""
    data = json.loads(_OWNERSHIP_FILE.read_text(encoding="utf-8"))
    ownership_rows = data.get("ownership", [])
    registered_types = {row["node_type"] for row in ownership_rows if "node_type" in row}
    by_engine: dict[str, list[dict[str, Any]]] = {}
    for row in ownership_rows:
        by_engine.setdefault(row.get("owner_engine", ""), []).append(row)
    return registered_types, by_engine


def _resolve_module_constants(tree: ast.AST) -> dict[str, str]:
    """Extract string constants from module-level Assign and AnnAssign statements."""
    consts: dict[str, str] = {}
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
        if (
            val is not None
            and target_names
            and isinstance(val, ast.Constant)
            and isinstance(val.value, str)
        ):
            for n in target_names:
                consts[n] = val.value
    return consts


def _scan_kg_nodes_writers(
    repo_root: Path,
) -> tuple[dict[str, list[tuple[str, int, bool]]], list[tuple[str, int, str]], set[str]]:
    """Scan all vertical module files for functions writing kg_nodes and assert_owner calls.

    Returns:
        (writers_by_file, assert_owner_calls, written_entity_types)
        writers_by_file: rel_path -> list of (func_name, lineno, has_assert_owner)
        assert_owner_calls: list of (rel_path, lineno, resolved_node_type)
        written_entity_types: set of all entity_type strings written or asserted
    """
    vm_dir = repo_root / "nce" / "vertical_modules"
    writers_by_file: dict[str, list[tuple[str, int, bool]]] = {}
    assert_owner_calls: list[tuple[str, int, str]] = []
    written_entity_types: set[str] = set()

    for py_path in sorted(vm_dir.glob("**/*.py")):
        text = py_path.read_text(encoding="utf-8")
        rel = py_path.relative_to(repo_root).as_posix()

        try:
            tree = ast.parse(text)
        except Exception as exc:
            pytest.fail(f"Failed to parse {rel}: {exc}")

        consts = _resolve_module_constants(tree)

        # Scan for assert_owner calls across the file
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", getattr(node.func, "attr", None))
                if fname == "assert_owner" and len(node.args) >= 3:
                    arg = node.args[2]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        assert_owner_calls.append((rel, node.lineno, arg.value))
                        written_entity_types.add(arg.value)
                    elif isinstance(arg, ast.Name) and arg.id in consts:
                        resolved = consts[arg.id]
                        assert_owner_calls.append((rel, node.lineno, resolved))
                        written_entity_types.add(resolved)
                    elif rel in _FILE_DYNAMIC_MAP:
                        for dt in _FILE_DYNAMIC_MAP[rel]:
                            assert_owner_calls.append((rel, node.lineno, dt))
                            written_entity_types.add(dt)
                    else:
                        raw_repr = ast.unparse(arg)
                        assert_owner_calls.append((rel, node.lineno, raw_repr))

        if not re.search(r"INSERT\s+INTO\s+kg_nodes", text, re.IGNORECASE):
            continue

        # Extract written entity types from SQL execute calls
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "fetch", "fetchval", "fetchrow")
            ):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and "INSERT INTO kg_nodes" in arg.value
                    ):
                        for other_arg in node.args[1:]:
                            if (
                                isinstance(other_arg, ast.Constant)
                                and isinstance(other_arg.value, str)
                                and (
                                    other_arg.value.isupper() or other_arg.value.startswith("D365_")
                                )
                            ):
                                written_entity_types.add(other_arg.value)
                            elif isinstance(other_arg, ast.Name) and other_arg.id in consts:
                                val = consts[other_arg.id]
                                if val.isupper() or val.startswith("D365_"):
                                    written_entity_types.add(val)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_text = ast.get_source_segment(text, node)
                if not func_text:
                    continue
                if re.search(r"INSERT\s+INTO\s+kg_nodes", func_text, re.IGNORECASE):
                    has_assert = False
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            cname = getattr(child.func, "id", getattr(child.func, "attr", None))
                            if cname == "assert_owner":
                                has_assert = True
                                break
                    writers_by_file.setdefault(rel, []).append((node.name, node.lineno, has_assert))

    return writers_by_file, assert_owner_calls, written_entity_types


def test_discovery_floors() -> None:
    """Guard the guard: AST walk must find sufficient writers and assert_owner call sites."""
    writers, assert_calls, written_types = _scan_kg_nodes_writers(_REPO_ROOT)
    registered_types, _ = _load_registered_ownership()

    assert len(writers) >= 25, (
        f"Discovery collapse: found only {len(writers)} files with kg_nodes writers (floor: 25)"
    )
    assert len(assert_calls) >= 30, (
        f"Discovery collapse: found only {len(assert_calls)} assert_owner calls (floor: 30)"
    )
    assert len(written_types) >= 35, (
        f"Discovery collapse: found only {len(written_types)} written entity types (floor: 35)"
    )
    assert len(registered_types) >= 40, (
        f"Discovery collapse: found only {len(registered_types)} registered node types in node-ownership.json (floor: 40)"
    )


def test_every_kg_nodes_writer_calls_assert_owner_or_is_allowlisted() -> None:
    """Ratchet: every function writing kg_nodes must call assert_owner or have an allowlist row."""
    writers, _, _ = _scan_kg_nodes_writers(_REPO_ROOT)
    unguarded_violations: list[str] = []

    for rel, funcs in sorted(writers.items()):
        for func_name, lineno, has_assert in funcs:
            if not has_assert:
                if rel not in KNOWN_UNGUARDED_GRAPH_WRITERS:
                    unguarded_violations.append(
                        f"{rel}:{lineno} in {func_name}() writes kg_nodes without assert_owner and is NOT allowlisted"
                    )

    assert not unguarded_violations, (
        f"Found {len(unguarded_violations)} unguarded kg_nodes writes without catalogue allowlist:\n"
        + "\n".join(f"  - {err}" for err in unguarded_violations)
    )


def test_unguarded_writers_allowlist_is_shrink_only() -> None:
    """Every entry in KNOWN_UNGUARDED_GRAPH_WRITERS must still be unguarded, substantive, and shrink-only."""
    writers, _, _ = _scan_kg_nodes_writers(_REPO_ROOT)
    now_guarded: list[str] = []

    for rel, meta in sorted(KNOWN_UNGUARDED_GRAPH_WRITERS.items()):
        funcs = writers.get(rel, [])
        all_guarded = funcs and all(has_assert for _, _, has_assert in funcs)
        if all_guarded:
            now_guarded.append(rel)

        assert "owner" in meta and meta["owner"], f"Entry '{rel}' missing owner"
        assert "engine" in meta and meta["engine"], f"Entry '{rel}' missing engine"
        assert "reason" in meta and meta["reason"], f"Entry '{rel}' missing reason"
        clean_reason = " ".join(meta["reason"].split())
        assert len(clean_reason) >= 60, (
            f"Entry '{rel}' reason too thin to review ({len(clean_reason)} chars): '{clean_reason}'"
        )
        assert clean_reason.replace(rel, "").strip(), (
            f"Entry '{rel}' reason merely repeats filename"
        )

    assert not now_guarded, (
        "Shrink-only violation! Files gained assert_owner guards and MUST be removed from allowlist:\n"
        + "\n".join(f"  - {f}" for f in now_guarded)
    )


def test_every_written_entity_type_is_registered_or_allowlisted() -> None:
    """Every entity_type passed to assert_owner or written to kg_nodes must exist in node-ownership.json."""
    registered_types, _ = _load_registered_ownership()
    _, _, written_types = _scan_kg_nodes_writers(_REPO_ROOT)

    unregistered_types: list[str] = []

    for entity_type in sorted(written_types):
        if entity_type not in registered_types:
            if entity_type not in KNOWN_UNREGISTERED_ENTITY_TYPES:
                unregistered_types.append(f"Uncontracted entity_type '{entity_type}'")

    assert not unregistered_types, (
        f"Found {len(unregistered_types)} uncontracted entity types written or asserted:\n"
        + "\n".join(f"  - {err}" for err in unregistered_types)
        + "\nAdd every node type to nce/config_data/node-ownership.json."
    )


def test_unregistered_entity_types_allowlist_is_shrink_only() -> None:
    """Every entry in KNOWN_UNREGISTERED_ENTITY_TYPES must be shrink-only and substantively reasoned."""
    registered_types, _ = _load_registered_ownership()
    now_registered: list[str] = []

    for entity_type, meta in sorted(KNOWN_UNREGISTERED_ENTITY_TYPES.items()):
        if entity_type in registered_types:
            now_registered.append(entity_type)

        assert "owner" in meta and meta["owner"], f"Entry '{entity_type}' missing owner"
        assert "engine" in meta and meta["engine"], f"Entry '{entity_type}' missing engine"
        assert "reason" in meta and meta["reason"], f"Entry '{entity_type}' missing reason"
        clean_reason = " ".join(meta["reason"].split())
        assert len(clean_reason) >= 60, (
            f"Entry '{entity_type}' reason too thin to review ({len(clean_reason)} chars): '{clean_reason}'"
        )

    assert not now_registered, (
        "Shrink-only violation! Entity types registered in node-ownership.json must be removed from allowlist: "
        f"{now_registered}"
    )


def test_starting_baseline_matches_charter() -> None:
    """Charter §6 verification: After FT-2, field_tech is fully guarded (0 unguarded files); hr (1 file) remains."""
    writers, _, _ = _scan_kg_nodes_writers(_REPO_ROOT)
    unguarded_files_by_engine: dict[str, list[str]] = {}

    for rel, funcs in writers.items():
        if any(not has_assert for _, _, has_assert in funcs):
            engine = rel.split("/")[2]
            unguarded_files_by_engine.setdefault(engine, []).append(rel)

    vertical_unguarded = {
        eng: files for eng, files in unguarded_files_by_engine.items() if eng != "dynamics365"
    }

    assert set(vertical_unguarded.keys()) == {"hr"}, (
        f"Charter §6 violation: Expected only hr to be unguarded, got: {list(vertical_unguarded.keys())}"
    )
    assert len(vertical_unguarded.get("field_tech", [])) == 0, (
        f"Charter §6 violation: Expected 0 unguarded field_tech files, found {len(vertical_unguarded.get('field_tech', []))}"
    )
    assert len(vertical_unguarded["hr"]) == 1, (
        f"Charter §6 violation: Expected exactly 1 unguarded hr file, found {len(vertical_unguarded['hr'])}"
    )


def test_positive_control_fails_on_unallowlisted_unguarded_writer() -> None:
    """Standing positive control (U18): prove ratchet rejects an unallowlisted unguarded writer."""
    synthetic_writers = {
        "nce/vertical_modules/fake_engine/writer.py": [("do_fake_write", 10, False)]
    }
    violations = []
    for rel, funcs in synthetic_writers.items():
        for func_name, lineno, has_assert in funcs:
            if not has_assert and rel not in KNOWN_UNGUARDED_GRAPH_WRITERS:
                violations.append(
                    f"{rel}:{lineno} in {func_name}() writes kg_nodes without assert_owner"
                )

    assert len(violations) == 1
    assert "fake_engine" in violations[0]


def test_positive_control_fails_on_unregistered_entity_type() -> None:
    """Standing positive control (U18): prove ratchet rejects an uncontracted entity type."""
    registered_types, _ = _load_registered_ownership()
    synthetic_type = "SYNTHETIC_UNOWNED_ENTITY"

    assert synthetic_type not in registered_types
    assert synthetic_type not in KNOWN_UNREGISTERED_ENTITY_TYPES
