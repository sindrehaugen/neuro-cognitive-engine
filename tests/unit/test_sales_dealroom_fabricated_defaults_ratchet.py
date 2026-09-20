"""
tests/unit/test_sales_dealroom_fabricated_defaults_ratchet.py
=============================================================
Ratchet test suite for Wave S-3: Eradicate Fabricated Defaults in Sales DealRoom.

Enforces:
  1. No string, float, or int literal may appear as default in .get(_, default)
     in nce/vertical_modules/sales/dealroom.py.
  2. dealroom.py has strictly ZERO forbidden literal defaults (empty allowlist).
  3. No literal variable assignments for fabricated constants:
     - dg_pct = 0.3 (or any hardcoded margin constant)
     - manufacturer = "Unknown"
     - model = "Unknown"
     - is_optional = True
     - toggled = True
  4. Standing Positive Control: Asserts the AST scanner goes RED when a forbidden
     literal default is introduced.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEALROOM_FILE = _REPO_ROOT / "nce" / "vertical_modules" / "sales" / "dealroom.py"

# Empty allowlist: dealroom.py must have strictly 0 fabricated defaults
KNOWN_DEALROOM_DEFAULTS: dict[tuple[str, Any], str] = {}


def _scan_file_for_fabricated_defaults(file_path: Path) -> list[tuple[int, str, Any]]:
    """Scan file AST for:
    1. .get(key, literal) calls where literal is a non-empty/non-None constant.
    2. Variable assignments to forbidden constants:
       - dg_pct = <float>
       - manufacturer = "Unknown"
       - model = "Unknown"
       - is_optional = True
       - toggled = True (outside of explicit conditional param parsing)
    """
    with open(file_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(file_path))

    violations: list[tuple[int, str, Any]] = []

    for node in ast.walk(tree):
        # 1. Check .get(key, literal_default)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) >= 2
        ):
            key_arg = node.args[0]
            default_arg = node.args[1]

            if isinstance(key_arg, ast.Constant) and isinstance(key_arg.value, str):
                key_name = key_arg.value
            elif isinstance(key_arg, ast.Name):
                key_name = key_arg.id
            else:
                key_name = ast.dump(key_arg)

            if isinstance(default_arg, ast.Constant) and default_arg.value is not None:
                if isinstance(default_arg.value, (str, int, float)):
                    # Allow empty strings/dicts/lists or presentation defaults only if listed
                    if default_arg.value not in ("",):
                        violations.append((node.lineno, f".get({key_name})", default_arg.value))

        # 2. Check forbidden variable assignments
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    var_name = target.id
                    if (
                        var_name == "dg_pct"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, (float, int))
                    ):
                        violations.append((node.lineno, "dg_pct_assignment", node.value.value))
                    elif (
                        var_name == "manufacturer"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value == "Unknown"
                    ):
                        violations.append(
                            (node.lineno, "manufacturer_assignment", node.value.value)
                        )
                    elif (
                        var_name == "model"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value == "Unknown"
                    ):
                        violations.append((node.lineno, "model_assignment", node.value.value))
                    elif (
                        var_name == "is_optional"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        violations.append((node.lineno, "is_optional_assignment", node.value.value))
                    elif (
                        var_name == "toggled"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        violations.append((node.lineno, "toggled_assignment", node.value.value))

    return violations


def test_dealroom_has_strictly_zero_literal_defaults() -> None:
    """Wave S-3 requirement: dealroom.py must have strictly ZERO fabricated defaults."""
    assert _DEALROOM_FILE.exists(), f"Expected {_DEALROOM_FILE} to exist"
    violations = _scan_file_for_fabricated_defaults(_DEALROOM_FILE)
    unapproved = [v for v in violations if (v[1], v[2]) not in KNOWN_DEALROOM_DEFAULTS]
    assert len(unapproved) == 0, (
        f"dealroom.py contains {len(unapproved)} forbidden literal defaults: {unapproved}"
    )


def test_sales_dealroom_ratchet_standing_positive_control(tmp_path: Path) -> None:
    """Standing Positive Control: Asserts the AST scanner goes RED when forbidden defaults are introduced."""
    bad_code = """
def bad_function(doc):
    dg_pct = 0.3
    manufacturer = "Unknown"
    model = "Unknown"
    is_optional = True
    toggled = True
    v1 = doc.get("dg_pct", 0.3)
    v2 = doc.get("manufacturer", "Unknown")
"""
    test_file = tmp_path / "bad_dealroom.py"
    test_file.write_text(bad_code, encoding="utf-8")

    violations = _scan_file_for_fabricated_defaults(test_file)
    assert len(violations) == 7, f"Expected 7 violations, got {len(violations)}: {violations}"

    violation_keys = {(v[1], v[2]) for v in violations}
    assert ("dg_pct_assignment", 0.3) in violation_keys
    assert ("manufacturer_assignment", "Unknown") in violation_keys
    assert ("model_assignment", "Unknown") in violation_keys
    assert ("is_optional_assignment", True) in violation_keys
    assert ("toggled_assignment", True) in violation_keys
    assert (".get(dg_pct)", 0.3) in violation_keys
    assert (".get(manufacturer)", "Unknown") in violation_keys
