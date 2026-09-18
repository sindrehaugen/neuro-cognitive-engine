"""H-1: the verb-mix floor — v1.6's programme progress bar.

v1.5 gave every engine a door and the doors are all GET/POST. NCE 3.0 needs each
engine to be updatable and archivable through its own resource, not through a
shared verb tool. This module measures, for every ``Route(...)`` mounted anywhere
in the estate, what fraction of **engine-scoped** route-methods are
PUT/PATCH/DELETE/``/archive`` — the verbs a resource needs to be edited and retired
in place — and asserts that fraction eventually reaches 25%.

**Engine-scoped**, precisely (ML-orch ruling, 2026-09-18, charter §13): a route is
engine-scoped when its path is ``/api/<slug>`` where ``<slug>``, with hyphens
normalised to underscores, names an entry in
``nce.engine_registry.VERTICAL_MODULE_NAMES`` — the count site of record, imported
here rather than re-derived from a directory listing so this test cannot drift out
of sync with the registry. Everything else (``/api/admin``, ``/api/me``, health,
gc, search, replay, a2a, tasks, well-known, static assets, and the separate
``nce/webhook_receiver`` FastAPI app) is platform, not an engine, and is excluded
from both numerator and denominator.

**Known gap, reported not silently patched (bounded judgement, charter §9):** the
``customer_portal`` engine mounts its REST routes under ``/api/portal/*``, not
``/api/customer_portal/*`` (see ``nce/vertical_modules/customer_portal/app.py``).
Under the literal slug-matching rule above, those 11 routes do not count as
engine-scoped even though they plainly are. This underscopes both numerator and
denominator by the same amount that engine happens to contribute to each (0 verb
routes there today, so the floor percentage is unaffected at present) — flagged in
``MLV16H.md`` and charter §14 "Lane H" as a rule gap for ML-orch to rule on, not
patched here with a hand-authored alias table (that would be deciding a contract,
not measuring one).

**Why AST, never a line grep (K-0):** several ``Route(...)`` calls in this tree
wrap their ``methods=`` keyword onto a following line. A single-line
``grep "Route("`` (the exact failure mode named in charter §14 K-0) undercounts.
This walks parsed ``ast.Call`` nodes instead, so line wrapping cannot hide a route.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

from nce.engine_registry import VERTICAL_MODULE_NAMES

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NCE_DIR = _REPO_ROOT / "nce"

_ENGINE_SLUGS = frozenset(VERTICAL_MODULE_NAMES)
_VERB_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


class RouteMethod(NamedTuple):
    file: str
    path: str
    method: str
    lineno: int
    slug: str | None
    is_engine: bool


def _slug_of(path: str) -> str | None:
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) < 2 or parts[0] != "api":
        return None
    return parts[1].replace("-", "_")


def _route_mount_files() -> list[Path]:
    """Every .py file under nce/ that mounts at least one Starlette Route(...)."""
    return [
        p
        for p in sorted(_NCE_DIR.rglob("*.py"))
        if "Route(" in p.read_text(encoding="utf-8", errors="replace")
    ]


def _extract_route_methods() -> tuple[
    list[RouteMethod], list[tuple[str, int]], list[tuple[str, int]]
]:
    """AST walk of every Route(...) call site under nce/.

    Returns (route_methods, unresolved_paths, unresolved_methods). The latter two
    are the "what the instrument could NOT match" report (K-0 discipline): any
    non-empty return here means a Route(...) call exists whose path or methods
    could not be read statically, and the counts below must not be trusted until
    that is investigated by hand.
    """
    route_methods: list[RouteMethod] = []
    unresolved_paths: list[tuple[str, int]] = []
    unresolved_methods: list[tuple[str, int]] = []

    for file_path in _route_mount_files():
        rel = file_path.relative_to(_REPO_ROOT).as_posix()
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if fname != "Route":
                continue

            path_value: str | None = None
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                path_value = node.args[0].value
            if path_value is None:
                unresolved_paths.append((rel, node.lineno))
                continue

            methods: list[str] | None = None
            for kw in node.keywords:
                if kw.arg != "methods":
                    continue
                if isinstance(kw.value, ast.List):
                    elts = [
                        e.value
                        for e in kw.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    if len(elts) == len(kw.value.elts):
                        methods = elts
                    else:
                        unresolved_methods.append((rel, node.lineno))
                elif kw.value is not None:
                    unresolved_methods.append((rel, node.lineno))
            if methods is None and not any(
                rel == f and ln == node.lineno for f, ln in unresolved_methods
            ):
                methods = ["GET"]  # Starlette's own default when methods= is omitted

            slug = _slug_of(path_value)
            is_engine = slug in _ENGINE_SLUGS
            for method in methods or []:
                route_methods.append(
                    RouteMethod(rel, path_value, method.upper(), node.lineno, slug, is_engine)
                )

    return route_methods, unresolved_paths, unresolved_methods


def _engine_route_methods() -> list[RouteMethod]:
    route_methods, _, _ = _extract_route_methods()
    return [rm for rm in route_methods if rm.is_engine]


def _is_verb_or_archive(rm: RouteMethod) -> bool:
    return rm.method in _VERB_METHODS or rm.path.rstrip("/").endswith("/archive")


# Shrink-only census of today's hand-written engine verb/archive routes (2026-09-18,
# commit 57a1f73, measured by this same extraction). This is a documentation fixture,
# never the source of the count — the tests below always re-derive from the live tree
# and merely assert this census has not silently gone stale (a route disappearing
# without this list shrinking to match is exactly the drift H is here to catch).
_KNOWN_HANDWRITTEN_VERB_ROUTES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("PATCH", "/api/resources/{id}", "nce/admin_app.py"),
        ("PUT", "/api/sales/targets", "nce/admin_app.py"),
        ("DELETE", "/api/system-design/planned", "nce/admin_app.py"),
    }
)


def test_route_extraction_has_no_unresolved_call_sites() -> None:
    """Report what the AST walk could NOT match, beside what it did (K-0 discipline).

    A Route(...) call whose path or methods cannot be read statically would make
    every other assertion in this file silently wrong in the direction of
    undercounting. This is the guard against that: it must stay at zero, and if it
    is not, the fix belongs in the extractor above, not in a suppressed exception.
    """
    _, unresolved_paths, unresolved_methods = _extract_route_methods()
    assert not unresolved_paths, (
        f"{len(unresolved_paths)} Route(...) call site(s) had a non-literal path "
        f"the extractor could not resolve:\n"
        + "\n".join(f"  - {f}:{ln}" for f, ln in unresolved_paths)
    )
    assert not unresolved_methods, (
        f"{len(unresolved_methods)} Route(...) call site(s) had a methods= keyword "
        f"the extractor could not resolve as a literal list of strings:\n"
        + "\n".join(f"  - {f}:{ln}" for f, ln in unresolved_methods)
    )


def test_engine_route_discovery_floor() -> None:
    """Guard-the-guard: the engine-scoped denominator must stay in a plausible range.

    Protects against the slug filter silently matching nothing (e.g. VERTICAL_MODULE_NAMES
    import breaks, or every path stops starting with /api/) and the floor test below
    passing vacuously because its denominator collapsed to zero.
    """
    engine_routes = _engine_route_methods()
    assert len(engine_routes) >= 150, (
        f"Only {len(engine_routes)} engine-scoped route-methods found — expected at "
        "least 150 based on the 2026-09-18 census (211). Either a real regression "
        "removed most of the estate's REST surface, or the slug filter broke."
    )
    matched_slugs = {rm.slug for rm in engine_routes}
    assert matched_slugs, "No engine slug matched any mounted route at all."


def test_known_handwritten_verb_routes_still_exist() -> None:
    """Shrink-only: today's hand-written verb-route census must still be present.

    This does not assert the census is complete (new verb routes are welcome and
    do not need this list touched to pass) — it asserts the census never silently
    goes stale. If a route in the census is removed or moved, this fails until the
    census is edited down to match, so the removal is a conscious, reviewed edit.
    """
    engine_routes = _engine_route_methods()
    live = {(rm.method, rm.path, rm.file) for rm in engine_routes if _is_verb_or_archive(rm)}
    missing = _KNOWN_HANDWRITTEN_VERB_ROUTES - live
    assert not missing, (
        f"{len(missing)} route(s) from the shrink-only hand-written-verb-route "
        f"census no longer exist as measured:\n"
        + "\n".join(f"  - {m} {p} ({f})" for m, p, f in sorted(missing))
        + "\nIf this route was intentionally removed or replaced, shrink "
        "_KNOWN_HANDWRITTEN_VERB_ROUTES in this file to match."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "H-1 verb-mix floor not yet met (v1.6 programme progress bar, charter §9 "
        "'Lane H'). Measured 2026-09-18 @ 57a1f73: 3 of 211 engine route-methods "
        "(1.42%). This xfail is meant to XPASS-and-fail-CI the day the floor is "
        "crossed, forcing whoever crosses it to remove this marker deliberately "
        "rather than let a moving target go unnoticed. See MLV16H.md."
    ),
)
def test_verb_mix_floor_at_least_25_percent() -> None:
    engine_routes = _engine_route_methods()
    assert engine_routes, "No engine-scoped route-methods found; floor is undefined."
    numerator = sum(1 for rm in engine_routes if _is_verb_or_archive(rm))
    pct = 100.0 * numerator / len(engine_routes)
    assert pct >= 25.0, (
        f"Verb-mix floor is {pct:.2f}% ({numerator}/{len(engine_routes)}) — "
        "below the 25% target. This is expected today; see the xfail reason."
    )


def test_positive_control_verb_mix_floor_is_not_vacuous() -> None:
    """Standing positive control (U18): prove the floor computation reacts to real change.

    Never-RED is suspect (K-0). This proves the metric moves in both directions:
    removing a known verb route lowers the percentage, and a route list with zero
    verb/archive routes reads exactly 0%, never a default pass.
    """
    engine_routes = _engine_route_methods()
    numerator = sum(1 for rm in engine_routes if _is_verb_or_archive(rm))
    denominator = len(engine_routes)
    baseline_pct = 100.0 * numerator / denominator

    # Removing one real verb route must strictly lower the measured percentage.
    verb_routes = [rm for rm in engine_routes if _is_verb_or_archive(rm)]
    assert verb_routes, "No verb/archive route to remove for the positive control."
    reduced = [rm for rm in engine_routes if rm != verb_routes[0]]
    reduced_numerator = sum(1 for rm in reduced if _is_verb_or_archive(rm))
    reduced_pct = 100.0 * reduced_numerator / len(reduced)
    assert reduced_pct < baseline_pct, (
        "Removing a known verb route did not lower the computed floor percentage — "
        "the metric is vacuous."
    )

    # A synthetic all-GET route list must read exactly 0%, never a silent pass.
    all_get = [rm._replace(method="GET") for rm in engine_routes]
    all_get_numerator = sum(1 for rm in all_get if _is_verb_or_archive(rm))
    assert all_get_numerator == 0
    assert (100.0 * all_get_numerator / len(all_get)) == 0.0
