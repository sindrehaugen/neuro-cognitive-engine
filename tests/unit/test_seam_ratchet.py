"""Unit test and ratchet: forbid cross-engine hasattr(engine, "<engine_name>") probes.

Phase 0 Wave I-3 Seam Ratchet:
Eliminates runtime guesswork where one vertical module checks for another engine
via `hasattr(engine, "<engine>")`. Cross-engine interactions must strictly go through
`engine.modules["<engine>"]` (or `engine.modules.for_namespace(...)`) introduced in W-1,
or through catalogued event selectors.

Legitimate duck-typing (such as `hasattr(engine_or_pool, "pg_pool")` or core capability
checks like `hasattr(engine, "verify_memory")`) is explicitly permitted; the ratchet
strictly matches on `VERTICAL_MODULE_NAMES`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nce.engine_registry import VERTICAL_MODULE_NAMES
from nce.vertical_modules.customer_portal.actions import (
    do_raise_service_request,
    do_register_expansion_interest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERT_DIR = _REPO_ROOT / "nce" / "vertical_modules"


def _scan_hasattr_engine_probes(tree: ast.AST, file_path: str = "") -> list[tuple[str, int, str]]:
    """Scan an AST tree for calls to hasattr(..., "<engine_name>").

    Returns list of (file_path, line_number, engine_name).
    """
    engine_names = set(VERTICAL_MODULE_NAMES)
    violations: list[tuple[str, int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        fname = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else None)
        )
        if fname != "hasattr":
            continue

        if len(node.args) >= 2:
            second_arg = node.args[1]
            if isinstance(second_arg, ast.Constant) and isinstance(second_arg.value, str):
                attr_name = second_arg.value
                if attr_name in engine_names:
                    violations.append((file_path, node.lineno, attr_name))

    return violations


# Known pre-existing seam offenders being closed by MLV15B (Wave CP-1).
# This dictionary is shrink-only: once CP-1 closes an offender, the entry MUST be removed.
KNOWN_SEAM_OFFENDERS: dict[str, set[str]] = {
    "nce/vertical_modules/customer_portal/actions.py": {"support", "sales"},
}


def test_no_unlisted_hasattr_engine_probes_in_vertical_modules() -> None:
    """Ratchet: Ensure zero hasattr(..., "<engine_name>") calls exist outside known allowlist."""
    all_violations: list[tuple[str, int, str]] = []

    for py_path in sorted(_VERT_DIR.glob("**/*.py")):
        rel = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception as exc:
            pytest.fail(f"Failed to parse {rel}: {exc}")

        violations = _scan_hasattr_engine_probes(tree, rel)
        all_violations.extend(violations)

    unexpected_violations = [
        v for v in all_violations if v[2] not in KNOWN_SEAM_OFFENDERS.get(v[0], set())
    ]

    assert not unexpected_violations, (
        f"Found {len(unexpected_violations)} unexpected prohibited hasattr(..., '<engine_name>') probes:\n"
        + "\n".join(
            f"  - {f}:{line} hasattr(..., '{eng}')" for f, line, eng in unexpected_violations
        )
        + "\nCross-engine calls must use engine.modules['<engine>'] or catalogued events."
    )


def test_seam_offenders_allowlist_is_shrink_only() -> None:
    """Every entry in KNOWN_SEAM_OFFENDERS must still be an active violation.

    When MLV15B CP-1 replaces the seams, this test trips until the entry is removed.
    """
    active_violations: dict[str, set[str]] = {}
    for py_path in sorted(_VERT_DIR.glob("**/*.py")):
        rel = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"))
        except Exception as exc:
            pytest.fail(f"Failed to parse {rel}: {exc}")

        violations = _scan_hasattr_engine_probes(tree, rel)
        for _, _, eng in violations:
            active_violations.setdefault(rel, set()).add(eng)

    closed_entries: list[str] = []
    for fpath, engines in KNOWN_SEAM_OFFENDERS.items():
        active_for_file = active_violations.get(fpath, set())
        for eng in engines:
            if eng not in active_for_file:
                closed_entries.append(f"{fpath} ({eng})")

    assert not closed_entries, (
        "Shrink-only violation: Seam(s) closed and no longer use hasattr! "
        f"Remove from KNOWN_SEAM_OFFENDERS: {closed_entries}"
    )


def test_positive_control_ratchet_detects_probes() -> None:
    """Standing positive control (U18): prove the ratchet fails when an offender is present."""
    bad_code = """
def sample_handoff(engine):
    if hasattr(engine, "support"):
        return engine.support.do_open_ticket()
    if hasattr(engine, "sales"):
        return engine.sales.do_create_deal()
    if hasattr(engine, "pg_pool"):
        return engine.pg_pool
    if hasattr(engine, "verify_memory"):
        return True
"""
    tree = ast.parse(bad_code)
    violations = _scan_hasattr_engine_probes(tree, "synthetic_test.py")

    assert len(violations) == 2, f"Expected 2 violations, found {len(violations)}: {violations}"
    violating_attrs = {v[2] for v in violations}
    assert violating_attrs == {"support", "sales"}


@pytest.mark.asyncio
async def test_customer_portal_actions_use_modules_registry() -> None:
    """Verify customer portal actions execute cleanly when engine.modules is provided."""

    class MockEngine:
        def __init__(self) -> None:
            self.modules = {
                "support": object(),
                "sales": object(),
            }

    engine = MockEngine()

    params_service = {
        "customer_scope_id": "00000000-0000-0000-0000-000000000001",
        "request_id": "req-seam-test-1",
        "room_id": "room-1",
        "summary": "Display flickering",
        "contract_b_covered": True,
    }
    service_res = await do_raise_service_request(engine, params_service)
    assert service_res["request_id"] == "req-seam-test-1"

    params_expansion = {
        "customer_scope_id": "00000000-0000-0000-0000-000000000001",
        "category": "hardware",
        "description": "Additional microphone ceiling tiles",
    }
    expansion_res = await do_register_expansion_interest(engine, params_expansion)
    assert expansion_res["status"] == "recorded"
