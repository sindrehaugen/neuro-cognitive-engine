"""tests/test_nettailer_isolation.py

Licence-boundary ratchet for nce/vertical_modules/product/sources/nettailer.py
(Q-44, QUESTIONS_CHARTER.md). The adapter connects to Nettailer, a
third-party service; obligations arising from that connection attach to
this one module and its use, not to the NCE codebase as a whole (Sindre's
ruling, 2026-09-20). That boundary only holds if nothing in NCE core wires
the adapter in unconditionally -- an eager, top-level import anywhere in
nce/ would make Nettailer reachable as a side effect of importing something
else, defeating the "disabled until an operator connects it" posture no
matter what the module's own docstring says.

This is a SYNCHRONISATION POINT, not a permanent zero-import rule -- unlike
tests/test_mcp_transport_is_stdio_only.py's stdio check, which really does
expect zero legitimate alternatives forever. There is no source-adapter
registry anywhere in nce/vertical_modules/product/ (checked: sources/__init__.py
exports only the abstract SourceAdapter base, no dispatcher). The one sibling
precedent, ManufacturerApiAdapter, is wired into watchers.py by a plain,
direct, unconditional import (`from ...sources.manufacturer_api import
ManufacturerApiAdapter`) -- so the first real Nettailer caller is expected to
be a direct import too, and is expected to legitimately trip this test once.
That is this test doing its job, not a sign to delete it: when it happens,
gate the actual call to stream_nettailer_rows/NettailerAdapter behind
require_nettailer_source_enabled (nce/vertical_modules/product/_guard.py),
then add the new file to _KNOWN_LEGITIMATE_IMPORTERS below by name. An empty
allowlist today is a measured fact (see the "nothing calls it" test), not a
promise that it stays empty.

Derives which files reference the module today via AST (same technique as
#339), fails loudly if an un-allowlisted one appears, and proves the
detector isn't vacuous with a synthetic-file positive control.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NCE_ROOT = _REPO_ROOT / "nce"
_NETTAILER_MODULE_FILE = _NCE_ROOT / "vertical_modules" / "product" / "sources" / "nettailer.py"

# Files under nce/ (besides the module itself) allowed to import the
# Nettailer adapter, because their call site is confirmed to gate it behind
# require_nettailer_source_enabled first. Empty today -- see the module
# docstring above for what to do when this needs its first entry.
_KNOWN_LEGITIMATE_IMPORTERS: frozenset[Path] = frozenset()


def _references_nettailer(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any("nettailer" in alias.name.lower() for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom) and node.module and "nettailer" in node.module.lower()
        ):
            return True
    return False


def _core_files_importing_nettailer() -> set[Path]:
    """Every .py file under nce/ (excluding the module itself) that imports
    anything from the Nettailer adapter, found by re-deriving the search
    (K-0: validate the instrument before the count) rather than trusting a
    fixed allowlist."""
    hits: set[Path] = set()
    for path in _NCE_ROOT.rglob("*.py"):
        if path == _NETTAILER_MODULE_FILE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        if _references_nettailer(tree):
            hits.add(path)
    return hits


def test_nettailer_module_exists_where_expected():
    """Positive control for the scan's own target: if this file moves, the
    check below would silently scan for an import of nothing."""
    assert _NETTAILER_MODULE_FILE.is_file(), (
        f"{_NETTAILER_MODULE_FILE} not found -- update _NETTAILER_MODULE_FILE "
        f"in this test before trusting the isolation check below."
    )


def test_nettailer_has_no_unallowlisted_core_importer():
    """The real check: every file under nce/ that imports the Nettailer
    adapter must be named in _KNOWN_LEGITIMATE_IMPORTERS. Today that set is
    empty, measured directly (not assumed): no production code path calls
    stream_nettailer_rows or NettailerAdapter anywhere -- the module is a
    standalone generator, exercised only by its own tests.

    Tripping this test by adding a real caller is expected, eventually --
    see the module docstring for exactly what to do: gate the call site
    behind require_nettailer_source_enabled, then add the file here by name.
    Do not delete this test or empty its check to make an unguarded import
    pass; that defeats the licence boundary it exists to hold.
    """
    hits = _core_files_importing_nettailer() - _KNOWN_LEGITIMATE_IMPORTERS
    assert not hits, (
        f"nce core now imports the Nettailer adapter from an un-allowlisted "
        f"file: {sorted(str(p.relative_to(_REPO_ROOT)) for p in hits)}. "
        f"This adapter connects to a third-party service and must stay opt-in, "
        f"not reachable as a side effect of importing something else. Before "
        f"adding it to _KNOWN_LEGITIMATE_IMPORTERS: confirm this call site gates "
        f"the actual invocation behind require_nettailer_source_enabled "
        f"(nce/vertical_modules/product/_guard.py) -- not the engine-wide "
        f"require_product_enabled, which would also gate every other product "
        f"source on one flag."
    )


def test_positive_control_a_new_core_import_is_actually_caught(tmp_path):
    """U18-style positive control: prove the detector actually flags a real
    import, using a synthetic file under a throwaway nce/-shaped tree so the
    real production tree is never touched for this proof."""
    fake_nce_root = tmp_path / "nce"
    fake_vertical = fake_nce_root / "vertical_modules" / "some_engine"
    fake_vertical.mkdir(parents=True)
    (fake_nce_root / "vertical_modules" / "__init__.py").write_text("")
    (fake_nce_root / "vertical_modules" / "some_engine" / "__init__.py").write_text("")
    poisoned = fake_vertical / "sync.py"
    poisoned.write_text(
        "from nce.vertical_modules.product.sources.nettailer import NettailerAdapter\n"
    )

    hits: set[Path] = set()
    for path in fake_nce_root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        if _references_nettailer(tree):
            hits.add(path)

    assert hits == {poisoned}

    clean = fake_vertical / "unrelated.py"
    clean.write_text("import json\n\ndef f():\n    return json.dumps({})\n")
    assert not _references_nettailer(ast.parse(clean.read_text()))
