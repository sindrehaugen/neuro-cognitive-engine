#!/usr/bin/env python3
"""
AST-based linter: detects `with ContextManager(): pass` anti-patterns.

A ``with`` block whose body is ONLY ``pass`` (or no-op) destroys the context
manager's ``__enter__``/``__exit__`` lifecycle.  The context manager is
entered and immediately exited, making its resource management, rollback
logic, or instrumentation dead code.

This is a CI / pre-commit gate.  Returns exit code 1 if any violation is found.

Usage:
    python scripts/check_empty_with.py [paths...]

    If no paths given, scans nce/ server.py admin_server.py start_worker.py.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import NamedTuple


class Violation(NamedTuple):
    file: str
    line: int
    col: int
    message: str


# ── pass variants: what we consider "empty" body ──
# A with body is empty if it consists solely of:
#   - pass
#   - ... (Ellipsis literal)
#   - a string literal (docstring) followed by pass/...
PASS_NODES = (ast.Pass,)
ELLIPSIS_NODES = (ast.Expr,)  # ast.Expr whose value is ast.Constant(Ellipsis)


def _is_pass_or_ellipsis(stmt: ast.stmt) -> bool:
    """Return True if *stmt* is `pass`, `...`, or a docstring followed by pass/..."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr):
        value = stmt.value
        # ``...`` literal
        if isinstance(value, ast.Constant) and value.value is Ellipsis:
            return True
        # String literal (docstring) — considered "empty" if it's the only statement
        # (we only count bodies that are purely pass/.../strings as empty)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return True
    return False


def _body_is_empty(body: list[ast.stmt]) -> bool:
    """Return True if *body* consists only of pass, Ellipsis, or docstrings."""
    if not body:
        return True  # truly empty body
    return all(_is_pass_or_ellipsis(stmt) for stmt in body)


def check_file(filepath: Path) -> list[Violation]:
    """Parse *filepath* and return all empty-with violations."""
    violations: list[Violation] = []

    try:
        source = filepath.read_text(encoding="utf-8")
    except Exception:
        return violations

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        # We only care about With and AsyncWith
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue

        if _body_is_empty(node.body):
            kind = "async with" if isinstance(node, ast.AsyncWith) else "with"
            # Build a human-readable context-manager expression
            items = []
            for item in node.items:
                src = ast.get_source_segment(source, item.context_expr) or "<expr>"
                if item.optional_vars:
                    target = ast.get_source_segment(source, item.optional_vars) or "<name>"
                    items.append(f"{src} as {target}")
                else:
                    items.append(src)
            ctx_str = ", ".join(items)

            violations.append(
                Violation(
                    file=str(filepath),
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"EMPTY_WITH: {kind} {ctx_str}: body is empty (pass/...). "
                        f"The context manager's __enter__/__exit__ lifecycle is dead code. "
                        f"Either remove the with block or place real logic inside it."
                    ),
                )
            )

    return violations


def main() -> int:
    default_paths = ["nce", "server.py", "admin_server.py", "start_worker.py"]

    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        targets = default_paths

    all_violations: list[Violation] = []

    for target in targets:
        p = Path(target)
        if p.is_dir():
            for py_file in sorted(p.rglob("*.py")):
                all_violations.extend(check_file(py_file))
        elif p.is_file() and p.suffix == ".py":
            all_violations.extend(check_file(p))
        # else: silently skip non-.py files

    if all_violations:
        print(f"\n{len(all_violations)} empty 'with' block(s) found:\n")
        for v in all_violations:
            print(f"  {v.file}:{v.line}:{v.col}: {v.message}")
        print("\nFix: remove the empty with block or place real logic inside it.\n")
        return 1

    print("✓ No empty 'with' blocks found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
