"""Production code must not behave differently because it is being tested.

Found while reviewing T-6. ``customer_portal/app.py``'s ``verify_resource_ownership``
dispatches on three engine hooks -- ``verify_resource_access``,
``check_resource_access``, ``strict_resources`` -- that exist **only** in
``tests/unit/test_customer_portal_c3_adversarial.py``, and permits when none
match. A twelve-test cross-tenant refusal matrix therefore passes while
production skips every branch and returns. The tests prove the *fixture* refuses.

Sweeping for the general shape turned up something sharper: production code that
detects whether its collaborator is a ``MagicMock`` and takes a different path.

``sales/signing.py:43``::

    if hasattr(engine, "_mock_return_value") or type(engine).__name__ in ("MagicMock", "AsyncMock"):
        ...                       # one transport-resolution path
    injected = getattr(engine, "sign_transport", None)
    ...                           # a DIFFERENT one

Its docstring cites the kaizen about ``MagicMock`` auto-synthesizing child
attributes -- so a *testing* hazard was mitigated in the *product*. The cost is
that every test passing a mock engine exercises the mock branch, and the branch
that runs in production is exercised only by a test that passes something else.
Two code paths, and the tests cover the one that never ships.

🔴 **The right place to fix mock auto-synthesis is the fake, not the product.**
Build it with ``MagicMock(spec=NCEEngine)``, or use a plain object, so ``getattr``
cannot invent an attribute. Then production needs no branch at all.

This ratchet forbids three things in ``nce/``, each pinned shrink-only:

1. importing ``unittest.mock`` -- test scaffolding has no business in shipping code;
2. reading a mock-internal attribute (``_mock_return_value``, ``_mock_self``, ...);
3. branching on ``type(x).__name__`` being a mock class.

It deliberately does NOT forbid the word "mock". ``assets/health.py``'s
``is_mock_telemetry`` is a genuine product concept -- A-1 made "this reading came
from the mock adapter, not hardware" a fact the API reports -- and a ratchet that
cannot tell that apart from test detection would be noise.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NCE = REPO_ROOT / "nce"

# ---------------------------------------------------------------------------
# Shrink-only. 🔴 These lists may only get SHORTER.
# ---------------------------------------------------------------------------
KNOWN_MOCK_IMPORTS: dict[str, str] = {
    "nce/tasks.py": (
        "imports AsyncMock inside a function and then does "
        "isinstance(getattr(engine.pg_pool, 'fetchrow', None), AsyncMock) to decide "
        "behaviour. The fake should be built with a spec so this branch is unnecessary."
    ),
}

KNOWN_MOCK_ATTRIBUTE_PROBES: dict[str, str] = {
    "nce/vertical_modules/sales/signing.py": (
        "hasattr(engine, '_mock_return_value') selects a different transport-resolution "
        "path, so mock-based tests never exercise the production branch. Fix the fake "
        "with MagicMock(spec=...) and delete the branch."
    ),
    "nce/orchestrators/memory.py": (
        "hasattr(raw_coll, '_mock_self') guards a Mongo collection probe. Same remedy: "
        "give the fake a spec."
    ),
}

KNOWN_MOCK_TYPE_CHECKS: dict[str, str] = {
    "nce/vertical_modules/sales/signing.py": (
        "type(engine).__name__ in ('MagicMock', 'AsyncMock') -- the second half of the "
        "same branch as the attribute probe above."
    ),
}

_MOCK_ATTRS = re.compile(r"_mock_[a-z_]+")
_MOCK_CLASS_NAMES = {"MagicMock", "AsyncMock", "Mock", "NonCallableMock"}


def _py_files() -> list[pathlib.Path]:
    return sorted(p for p in NCE.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_the_scan_sees_the_tree() -> None:
    """Positive control: an empty file list would make every check below vacuous."""
    files = _py_files()
    assert len(files) >= 200, f"only found {len(files)} files under nce/; the scan is broken"
    assert any(_rel(p) == "nce/orchestrator.py" for p in files)


def test_production_does_not_import_unittest_mock() -> None:
    """Test scaffolding imported into shipping code is a behaviour fork waiting to happen."""
    offenders: list[str] = []
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                mod = next((a.name for a in node.names if a.name.startswith("unittest.mock")), None)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module if (node.module or "").startswith("unittest.mock") else None
            if mod and _rel(path) not in KNOWN_MOCK_IMPORTS:
                offenders.append(f"{_rel(path)}:{node.lineno} imports {mod}")
    assert not offenders, "unittest.mock imported in production code:\n  " + "\n  ".join(offenders)


def test_production_does_not_probe_mock_internals() -> None:
    """``hasattr(x, "_mock_...")`` means the code asks whether it is under test."""
    offenders: list[str] = []
    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") in ("hasattr", "getattr")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                continue
            if _MOCK_ATTRS.fullmatch(node.args[1].value) and _rel(path) not in (
                KNOWN_MOCK_ATTRIBUTE_PROBES
            ):
                offenders.append(f"{_rel(path)}:{node.lineno} probes {node.args[1].value!r}")
    assert not offenders, (
        "production code probes mock internals, so its tests exercise a branch that "
        "never ships:\n  " + "\n  ".join(offenders)
    )


def test_production_does_not_branch_on_a_mock_class_name() -> None:
    """``type(x).__name__ in ("MagicMock", ...)`` is the same fork spelled differently."""
    offenders: list[str] = []
    pattern = re.compile(
        r"type\([^)]*\)\.__name__\s*(?:==|in)\s*[^\n]*(?:" + "|".join(_MOCK_CLASS_NAMES) + ")"
    )
    for path in _py_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _rel(path) in KNOWN_MOCK_TYPE_CHECKS:
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{_rel(path)}:{lineno} {line.strip()[:90]}")
    assert not offenders, (
        "production code branches on whether a collaborator is a mock:\n  " + "\n  ".join(offenders)
    )


def test_every_allowlist_entry_still_matches_a_real_file() -> None:
    """A stale entry reserves permission for something already fixed."""
    missing = [
        rel
        for allowlist in (
            KNOWN_MOCK_IMPORTS,
            KNOWN_MOCK_ATTRIBUTE_PROBES,
            KNOWN_MOCK_TYPE_CHECKS,
        )
        for rel in allowlist
        if not (REPO_ROOT / rel).is_file()
    ]
    assert not missing, f"allowlisted files that no longer exist: {missing}"


def test_allowlists_carry_a_reason() -> None:
    """An allowlist without reasons becomes a list nobody can shrink."""
    for name, allowlist in (
        ("KNOWN_MOCK_IMPORTS", KNOWN_MOCK_IMPORTS),
        ("KNOWN_MOCK_ATTRIBUTE_PROBES", KNOWN_MOCK_ATTRIBUTE_PROBES),
        ("KNOWN_MOCK_TYPE_CHECKS", KNOWN_MOCK_TYPE_CHECKS),
    ):
        for rel, reason in allowlist.items():
            assert len(reason) > 40, f"{name}[{rel!r}] needs a real reason, not {reason!r}"
