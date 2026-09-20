"""AST census: every test that checks membership against internal-cores.json must
assert a discovery floor on the loaded data first (inert-instrument audit, 2026-09-20).

Why this file exists
---------------------
Seven test functions across seven files -- ``test_product_p2_p3_ratchet.py``,
``test_project_pj1_sd3_ratchet.py``, ``test_wave5_sd2_rs3_ft4_a1_ratchet.py``,
``test_sales_ai_surface.py``, ``test_support_ecosystem_surface.py``,
``test_project_case_study_edge_ratchet.py``, ``test_system_design_one_validator.py`` --
each loaded ``nce/config_data/internal-cores.json`` and asserted
``"some/path.py::do_thing" not in <the loaded data>`` without ever checking the load
produced anything. An empty or malformed file satisfied every one of those assertions
vacuously -- they were fixed (this same PR) with a discovery-floor assertion, but
nothing stopped an eighth wave from writing the same check the same way next week.

This is the class fix, not another instance fix: a census that fails the moment a NEW
test does the same thing without a floor, so the mistake can only be made once more,
not indefinitely. Modeled directly on ``test_swallowed_exception_census.py``'s shape
(shrink-only allowlist with owner/reason/remediation, standing positive controls, a
discovery floor on the scanner itself) rather than inventing a new pattern.

Scope, deliberately narrow -- and two things this census does NOT catch
-------------------------------------------------------------------------
This does NOT try to detect "any test that checks membership in any config file
without a floor" -- that is a much larger, fuzzier claim this session has not measured
and would risk a high false-positive rate against unrelated fixtures. It detects one,
specific, demonstrated shape: a function that (a) references ``internal-cores.json``,
(b) directly calls ``open(``/``json.load(``/``json.loads(`` in its OWN body (not
through a helper it merely calls), and (c) contains a membership comparison, without
(d) an ``assert <name>`` floor on a bare name in that same function.

Two real gaps, found and left open rather than hidden, per S7:

1. **Indirected loads are invisible to this scanner.** ``tests/test_surface_parity.py``
   loads the file through a shared ``_load_allowlist()`` helper, not inline -- this
   census's "same function" requirement means it would not have caught that file's own
   real vacuity (fixed directly in this same PR, once found by hand). A scanner that
   followed call graphs to find indirected loads would close this gap; it was not
   built here because this pass had exactly one confirmed indirected instance to
   generalize from, and one instance is not enough to trust a call-graph heuristic
   against false positives.
2. **``tests/unit/test_waves_landed_ratchet.py`` references internal-cores.json but
   through ``git grep <baseline> -- nce/ tests/`` against the committed git tree**
   (``scripts/gen_waves_landed.py::scan_tree_stamps``), not a direct filesystem read.
   A working-tree mutation (write a scratch copy, empty it, run pytest) is invisible
   to this mechanism by design -- it reads the last commit, not disk. Confirmed
   empirically: emptying the on-disk file did not change its test's outcome. Whether
   that file's check is vacuous would require a commit-based mutation (stage the
   emptied file, commit, run, then reset --soft) that this pass did not attempt, since
   polluting history for a speculative check was judged worse than leaving the
   question open. Filed, not answered.

Widening this census to catch either gap, or to other allowlist files entirely, is a
separate, unmeasured claim and is out of scope here.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_TESTS_DIR: Final[Path] = _REPO_ROOT / "tests"

# Shrink-only allowlist of sites that reference internal-cores.json without (yet) having
# a floor check, if a fix is deliberately deferred. Empty by design after this PR fixes
# all seven known sites -- a new entry must be justified the same way the swallowed-
# exception census requires, not just added to make a red test green.
KNOWN_UNFLOORED_INTERNAL_CORES_CHECKS: Final[dict[str, dict[str, str]]] = {}


def _get_enclosing_function_name(node: ast.AST, parent_map: dict[ast.AST, ast.AST]) -> str:
    curr: ast.AST | None = node
    while curr in parent_map:
        curr = parent_map[curr]
        if isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return curr.name
    return "<module>"


def _function_bodies(tree: ast.AST) -> list[ast.AST]:
    """Return every FunctionDef/AsyncFunctionDef node in the tree."""
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _references_internal_cores(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "internal-cores.json" in node.value:
                return True
    return False


def _has_direct_load_call(fn: ast.AST) -> bool:
    """True only for an inline ``open(...)``/``json.load(...)``/``json.loads(...)``
    call in this function's own body -- deliberately excludes loads reached only
    through a helper function it calls (see module docstring, gap 1)."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if fname in ("open", "load", "loads"):
                return True
    return False


def _has_membership_check(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                return True
    return False


def _has_bare_name_floor_assert(fn: ast.AST) -> bool:
    """A floor looks like ``assert allowlist`` or ``assert allowlist, "..."`` --
    a bare Name (or Name inside a BoolOp/UnaryOp-free test) as the assertion itself,
    not a comparison. This is the exact shape used to fix all seven known sites."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            test = node.test
            if isinstance(test, ast.Name):
                return True
    return False


_SELF_PATH: Final[Path] = Path(__file__).resolve()


def _scan_file(path: Path) -> list[dict[str, str]]:
    if path.resolve() == _SELF_PATH:
        return []  # this file's own helpers/fixtures mention the target string
    rel_path = path.relative_to(_REPO_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_bytes())
    except Exception:
        return []

    findings: list[dict[str, str]] = []
    for fn in _function_bodies(tree):
        if not _references_internal_cores(fn):
            continue
        if not _has_direct_load_call(fn):
            continue
        if not _has_membership_check(fn):
            continue
        if _has_bare_name_floor_assert(fn):
            continue
        fn_name = fn.name
        findings.append(
            {
                "file": rel_path,
                "function": fn_name,
                "site_id": f"{rel_path}::{fn_name}",
            }
        )
    return findings


def _scan_all_test_files() -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for py_path in sorted(_TESTS_DIR.rglob("*.py")):
        findings.extend(_scan_file(py_path))
    return findings


def _floored_sites() -> list[dict[str, str]]:
    """Functions that reference internal-cores.json, load it inline, check
    membership, AND already have a floor -- used as the discovery floor for the
    scanner itself (guard-the-guard): if this comes back empty, the file-content or
    membership matcher broke, not the estate having stopped writing floored checks."""
    findings: list[dict[str, str]] = []
    for py_path in sorted(_TESTS_DIR.rglob("*.py")):
        if py_path.resolve() == _SELF_PATH:
            continue
        rel_path = py_path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(py_path.read_bytes())
        except Exception:
            continue
        for fn in _function_bodies(tree):
            if (
                _references_internal_cores(fn)
                and _has_direct_load_call(fn)
                and _has_membership_check(fn)
                and _has_bare_name_floor_assert(fn)
            ):
                findings.append({"file": rel_path, "function": fn.name})
    return findings


def test_internal_cores_membership_checks_have_a_discovery_floor() -> None:
    """No test may check membership against internal-cores.json without first
    asserting the loaded data is non-empty. See module docstring for why."""
    findings = _scan_all_test_files()
    discovered_site_ids = {f["site_id"] for f in findings}

    unallowlisted = discovered_site_ids - set(KNOWN_UNFLOORED_INTERNAL_CORES_CHECKS.keys())
    assert not unallowlisted, (
        f"Found {len(unallowlisted)} internal-cores.json membership check(s) with no "
        f"discovery floor: {sorted(unallowlisted)}. An empty or malformed "
        f"internal-cores.json would satisfy this check vacuously. Add "
        f"`assert <the loaded allowlist name>, \"internal-cores.json parsed empty -- "
        f"the loader broke, not the estate\"` before the membership assertion, or "
        f"justify deferring it in KNOWN_UNFLOORED_INTERNAL_CORES_CHECKS with an owner, "
        f"reason, and remediation, per the swallowed-exception census precedent."
    )

    stale = set(KNOWN_UNFLOORED_INTERNAL_CORES_CHECKS.keys()) - discovered_site_ids
    assert not stale, (
        f"Allowlist contains remediated sites that no longer lack a floor: {stale}. "
        "Remove them from KNOWN_UNFLOORED_INTERNAL_CORES_CHECKS to ratchet down the count."
    )


def test_discovery_floor_for_internal_cores_floor_scanner() -> None:
    """Guard-the-guard: the scanner must find at least the 7 sites this PR just
    floored, or the file-content/membership matcher itself is broken."""
    floored = _floored_sites()
    assert len(floored) >= 7, (
        f"Expected to find at least 7 floored internal-cores.json membership checks "
        f"(the sites this PR fixed), found {len(floored)}: {floored}. "
        "The scanner's string or membership matcher likely broke, not the estate "
        "having removed the floors."
    )


def test_positive_control_detects_unfloored_membership_check() -> None:
    """Standing positive control: reproduces the exact bug class this census exists
    to catch (the shape all seven original sites had) and confirms it is flagged."""
    bad_code = """
def test_bad_prune_check():
    with open("nce/config_data/internal-cores.json", encoding="utf-8") as f:
        data = json.load(f)
    allowlist = set(data.keys())
    assert "nce/vertical_modules/x.py::do_thing" not in allowlist
"""
    tree = ast.parse(bad_code)
    findings = []
    for fn in _function_bodies(tree):
        if (
            _references_internal_cores(fn)
            and _has_direct_load_call(fn)
            and _has_membership_check(fn)
            and not _has_bare_name_floor_assert(fn)
        ):
            findings.append(fn.name)
    assert findings == ["test_bad_prune_check"], (
        f"Positive control failed to flag the known-bad shape: {findings}"
    )


def test_positive_control_permits_floored_membership_check() -> None:
    """Standing positive control: the exact fix shape applied to all seven sites
    must NOT be flagged."""
    good_code = """
def test_good_prune_check():
    with open("nce/config_data/internal-cores.json", encoding="utf-8") as f:
        data = json.load(f)
    allowlist = set(data.keys())
    assert allowlist, "internal-cores.json parsed empty -- the loader broke, not the estate"
    assert "nce/vertical_modules/x.py::do_thing" not in allowlist
"""
    tree = ast.parse(good_code)
    findings = []
    for fn in _function_bodies(tree):
        if (
            _references_internal_cores(fn)
            and _has_direct_load_call(fn)
            and _has_membership_check(fn)
            and not _has_bare_name_floor_assert(fn)
        ):
            findings.append(fn.name)
    assert findings == [], f"Positive control over-fired on a correctly-floored check: {findings}"


def test_positive_control_ignores_unrelated_membership_checks() -> None:
    """A function with a membership check but no internal-cores.json reference at
    all must never be flagged -- otherwise this census would fire on nearly every
    test file in the suite."""
    unrelated_code = """
def test_unrelated():
    names = {"a", "b", "c"}
    assert "d" not in names
"""
    tree = ast.parse(unrelated_code)
    findings = []
    for fn in _function_bodies(tree):
        if _references_internal_cores(fn) and _has_membership_check(fn):
            findings.append(fn.name)
    assert findings == [], f"Positive control over-fired on an unrelated check: {findings}"


def test_positive_control_ignores_prose_only_mentions() -> None:
    """A docstring that merely mentions "internal-cores.json" in prose, with no
    inline load call, must not be flagged (gap 1's mirror image): this is the exact
    false-positive this census's first draft produced against
    test_advertised_capability_ratchet.py and test_deployment_topology_parity.py,
    whose docstrings compare their own separate, unrelated allowlists to
    internal-cores.json's shape without ever loading that file."""
    prose_only_code = '''
def test_mentions_internal_cores_json_in_prose():
    """Mirrors internal-cores.json: no stale entries, every entry reasoned."""
    allowlist = {"a": 1, "b": 2}
    assert "c" not in allowlist
'''
    tree = ast.parse(prose_only_code)
    findings = []
    for fn in _function_bodies(tree):
        if (
            _references_internal_cores(fn)
            and _has_direct_load_call(fn)
            and _has_membership_check(fn)
            and not _has_bare_name_floor_assert(fn)
        ):
            findings.append(fn.name)
    assert findings == [], f"Positive control over-fired on a prose-only mention: {findings}"


def test_positive_control_ignores_indirected_loads() -> None:
    """A load reached only through a helper function, not inline, must not be
    flagged (gap 1, named rather than silently missed): this is
    tests/test_surface_parity.py's real shape, which this census does not catch by
    design -- see module docstring. Fixed by hand in this same PR; this control
    documents that the census knows it would not have caught it, rather than
    implying broader coverage than it has."""
    indirected_code = """
def _load_allowlist():
    with open("nce/config_data/internal-cores.json", encoding="utf-8") as f:
        return json.load(f)

def test_uses_helper_to_load():
    allowlist = _load_allowlist()
    assert "nce/vertical_modules/x.py::do_thing" not in allowlist
"""
    tree = ast.parse(indirected_code)
    findings = []
    for fn in _function_bodies(tree):
        if (
            _references_internal_cores(fn)
            and _has_direct_load_call(fn)
            and _has_membership_check(fn)
            and not _has_bare_name_floor_assert(fn)
        ):
            findings.append(fn.name)
    assert findings == [], (
        f"Census unexpectedly caught an indirected load -- module docstring's gap 1 "
        f"claim is now wrong and should be corrected: {findings}"
    )
