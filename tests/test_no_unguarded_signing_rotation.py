"""No test may rotate a signing key without the disposable-database opt-in.

``nce.signing.rotate_key`` retires the database's active signing key and inserts a
replacement wrapped under the *calling process's* ``NCE_MASTER_KEY``.  Against the
deployed database that orphans the running stack: the containers hold the old key in
memory, keep reporting healthy, and silently cannot sign anything.

This happened twice on 2026-09-07 -- at 01:46:27 and again at 10:55:05.  Both
replacement keys were wrapped under ``tests/conftest.py``'s ``"x" * 32``, and the
second was created by the live Golden Thread gate in the rebuild runbook, which called
``rotate_key`` unconditionally.  It "passed" by destroying the key it needed.

``tests/conftest.py`` had the correct shape all along -- rotate only behind
``NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL``, otherwise ``pytest.skip``.  Two
other call sites were copies of that block with the guard removed, and both were
reachable through ``NCE_INTEGRATION_PG_DSN``.

This ratchet pins the invariant: every ``rotate_key`` call under ``tests/`` must sit in
a function that consults the opt-in flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

GUARD_NAMES = frozenset(
    {
        "_refresh_signing_when_decrypt_fails",
        "NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL",
    }
)

TESTS_ROOT = Path(__file__).resolve().parent


def _guard_tokens(node: ast.AST) -> set[str]:
    """Every name, attribute and string constant mentioned anywhere inside *node*."""

    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)
    return found


def _unguarded_rotate_calls(source: str, label: str) -> list[str]:
    """Return ``label:lineno`` for each ``rotate_key`` call with no guard in scope."""

    tree = ast.parse(source)

    # Map every node to its innermost enclosing function, so a call can be traced to
    # the scope that would hold the guard.
    enclosing: dict[ast.AST, ast.AST] = {}
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            for child in ast.walk(scope):
                enclosing.setdefault(child, scope)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "rotate_key":
            continue
        scope = enclosing.get(node, tree)
        if not (_guard_tokens(scope) & GUARD_NAMES):
            offenders.append(f"{label}:{node.lineno}")
    return offenders


def test_no_test_rotates_a_signing_key_without_the_opt_in() -> None:
    """Every ``rotate_key`` call under ``tests/`` consults the opt-in flag."""

    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        if "rotate_key" not in text:
            continue
        offenders.extend(_unguarded_rotate_calls(text, str(path.relative_to(TESTS_ROOT))))

    detail = "".join("\n  " + offender for offender in offenders)
    assert offenders == [], (
        "These tests call nce.signing.rotate_key without consulting "
        "NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL.  Against the deployed database "
        "each one retires the live signing key and re-wraps it under the test process's "
        "master key, orphaning the running stack:" + detail
    )


# ---------------------------------------------------------------------------
# Positive controls (U18): the detector must actually be able to fail.
# ---------------------------------------------------------------------------

_UNGUARDED = """
async def ensure_key(conn):
    try:
        await get_active_key(conn)
    except SigningKeyDecryptionError:
        await rotate_key(conn)
"""

_GUARDED_BY_HELPER = """
async def ensure_key(conn):
    try:
        await get_active_key(conn)
    except SigningKeyDecryptionError:
        if not _refresh_signing_when_decrypt_fails():
            pytest.skip("disposable databases only")
        await rotate_key(conn)
"""

_GUARDED_BY_ENV_STRING = """
import os

async def ensure_key(conn):
    try:
        await get_active_key(conn)
    except SigningKeyDecryptionError:
        if not os.getenv("NCE_INTEGRATION_REFRESH_SIGNING_ON_DECRYPT_FAIL"):
            pytest.skip("disposable databases only")
        await rotate_key(conn)
"""

_NO_ROTATION = """
async def ensure_key(conn):
    await get_active_key(conn)
"""


@pytest.mark.parametrize(
    ("label", "source", "expect_offenders"),
    [
        ("unguarded", _UNGUARDED, True),
        ("guarded_by_helper", _GUARDED_BY_HELPER, False),
        ("guarded_by_env_string", _GUARDED_BY_ENV_STRING, False),
        ("no_rotation", _NO_ROTATION, False),
    ],
)
def test_detector_positive_controls(label: str, source: str, expect_offenders: bool) -> None:
    """The detector flags an unguarded rotation and stays quiet on guarded ones.

    Without this, a detector that silently stopped matching ``rotate_key`` -- a rename,
    a decorator, an aliased import -- would report a clean tree forever.
    """

    offenders = _unguarded_rotate_calls(source, label)
    if expect_offenders:
        assert offenders, f"detector failed to flag the unguarded control {label!r}"
    else:
        assert offenders == [], f"detector falsely flagged {label!r}: {offenders}"
