"""H-1 / H-1b: the verb-mix floor — v1.6's programme progress bar.

v1.5 gave every engine a door and the doors are all GET/POST. NCE 3.0 needs each
engine to be updatable and archivable through its own resource, not through a
shared verb tool. This module measures, for every ``Route(...)`` mounted anywhere
in the estate, what fraction of **engine-scoped** route-methods are
PUT/PATCH/DELETE/``/archive`` — the verbs a resource needs to be edited and retired
in place — and asserts that fraction eventually reaches 25%.

**Engine-scoped, precisely (K-H1 union ruling, ML-orch, 2026-09-18, charter §13,
limb 3 added the same day after the residual pin's first run).** A route is
engine-scoped when ANY of three limbs holds:

1. **Module-owner** — the route's ``endpoint`` callable is defined in, or imported
   from, a module under ``nce/vertical_modules/<pkg>/``, where ``<pkg>`` is an entry
   in ``nce.engine_registry.VERTICAL_MODULE_NAMES`` (the count site of record,
   imported here, never re-listed).
2. **Path-slug** — the route's path is ``/api/<slug>`` where ``<slug>`` (hyphens
   normalised to underscores) also names an entry in ``VERTICAL_MODULE_NAMES``.
3. **Public-path-slug** — the route's path is ``/public-api/<slug>`` under the
   same slug rule. The estate's one public REST surface today
   (``/public-api/sales/quotes/{id}``) is where a customer-facing verb route is
   most likely to appear next (charter B-6 adds accept/decline/comment actions),
   and it would otherwise be invisible to this instrument forever.

No limb alone is sufficient (that is the whole finding, twice over): module-owner
alone would drop the three known hand-written verb routes, whose handlers live in
``nce/admin_handlers/<engine>.py`` — a parallel directory, not
``vertical_modules`` — so the floor would read a false 0%. Path-slug alone drops
``customer_portal``, whose REST routes mount at ``/api/portal/*`` rather than
``/api/customer_portal/*`` (``nce/vertical_modules/customer_portal/app.py``), so
its 11-and-counting real routes silently vanish into "platform." And plain
path-slug also drops the one ``/public-api/*`` route, since it does not start
with ``/api/`` at all — caught by the residual pin on its first run (below), not
by design foresight. The three limbs catch: a centrally-mounted internal engine
route (limb 2), a module-owned app under an unpredictable URL prefix (limb 1),
and a centrally-mounted *public* engine route (limb 3). No hand-maintained alias
table is used or wanted for any of the three — a limb matching, say,
``nce/admin_handlers/<stem>.py`` against the registry would catch the public-sales
handler too, but only by stripping a suffix, which is aliasing wearing a rule's
clothes (rejected for the same reason the ``customer_portal -> portal`` alias was
rejected). The path limb covers it cleanly instead.

**The residual is load-bearing, not informational.** Every route matched by NO
limb is "platform" (``/api/admin``, ``/api/me``, health, gc, search, replay, a2a,
tasks, well-known, static assets, and the separate ``nce/webhook_receiver``
FastAPI app). Its size is pinned below (``_RESIDUAL_COUNT_PIN``): a route that
should be an engine route but is mounted under a prefix no limb recognises, with
a handler defined somewhere no limb recognises, moves this pinned number and
fails the ratchet instead of silently disappearing into "platform" forever. It
has already done its job once — see above — which is the reason it exists as a
hard pin rather than a comment.

**Why AST, never a line grep (K-0):** several ``Route(...)`` calls in this tree
wrap their ``methods=`` keyword onto a following line. A single-line
``grep "Route("`` (the exact failure mode named in charter §14 K-0) undercounts.
This walks parsed ``ast.Call`` nodes instead, so line wrapping cannot hide a route.
Import resolution (limb 1) is similarly AST-based and deliberately shallow: it
reads the mounting file's own ``import`` / ``from ... import`` statements and
local ``def``/``async def`` names, one hop, and does not follow re-exports. That
is enough for every route in this tree today (verified: none of the four mounting
files re-export a handler through an intermediate alias module) and is reported
as a limitation here rather than silently assumed complete.
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
_VERTICAL_MODULES_PREFIX = "nce.vertical_modules."
_VERB_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


class RouteMethod(NamedTuple):
    file: str
    path: str
    method: str
    lineno: int
    defining_module: str | None
    mod_slug: str | None
    path_slug: str | None
    public_path_slug: str | None
    is_engine: bool
    resolved_slug: str | None


def _module_path_of(file_path: Path) -> str:
    """Dotted module path of a .py file, relative to the repo root."""
    rel = file_path.relative_to(_REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _local_def_names(tree: ast.Module) -> set[str]:
    return {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _build_import_map(tree: ast.Module, this_module: str) -> dict[str, str]:
    """local name -> best-effort dotted target (module, or module.member).

    One hop only: for ``from X import Y as Z``, records ``Z -> "X.Y"``, without
    following whether ``X.Y`` itself re-exports from a third module. Handles
    relative imports (``from . import Y`` / ``from .sibling import Y``) by
    resolving against the importing file's own containing package.
    """
    mapping: dict[str, str] = {}
    pkg_parts = this_module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mapping[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base_parts = (
                    pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                )
                base = ".".join(base_parts)
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                mapping[local] = f"{base}.{alias.name}" if base else alias.name
    return mapping


def _module_owner_slug(defining_module: str | None) -> str | None:
    if not defining_module or not defining_module.startswith(_VERTICAL_MODULES_PREFIX):
        return None
    rest = defining_module[len(_VERTICAL_MODULES_PREFIX) :]
    slug = rest.split(".")[0] if rest else None
    return slug if slug in _ENGINE_SLUGS else None


def _slug_under_prefix(path: str, prefix_segment: str) -> str | None:
    """slug of /<prefix_segment>/<slug>/... when <slug> names a registered engine."""
    parts = [seg for seg in path.split("/") if seg]
    if len(parts) < 2 or parts[0] != prefix_segment:
        return None
    slug = parts[1].replace("-", "_")
    return slug if slug in _ENGINE_SLUGS else None


def _path_slug(path: str) -> str | None:
    """Limb 2: /api/<slug>."""
    return _slug_under_prefix(path, "api")


def _public_path_slug(path: str) -> str | None:
    """Limb 3: /public-api/<slug> (K-H1 ruling, added after the residual pin's first run)."""
    return _slug_under_prefix(path, "public-api")


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
    """AST walk of every Route(...) call site under nce/, union-scoped.

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
        src = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=rel)
        this_module = _module_path_of(file_path)
        import_map = _build_import_map(tree, this_module)
        local_defs = _local_def_names(tree)

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

            # Limb 1: module-owner, resolved from the `endpoint=` (or 2nd
            # positional) argument's origin in this file.
            endpoint_expr = None
            for kw in node.keywords:
                if kw.arg == "endpoint":
                    endpoint_expr = kw.value
            if endpoint_expr is None and len(node.args) >= 2:
                endpoint_expr = node.args[1]

            defining_module: str | None = None
            if isinstance(endpoint_expr, ast.Name):
                if endpoint_expr.id in local_defs:
                    defining_module = this_module
                elif endpoint_expr.id in import_map:
                    defining_module = import_map[endpoint_expr.id]
            elif isinstance(endpoint_expr, ast.Attribute) and isinstance(
                endpoint_expr.value, ast.Name
            ):
                base = endpoint_expr.value.id
                if base in import_map:
                    defining_module = import_map[base]

            mod_slug = _module_owner_slug(defining_module)
            path_slug = _path_slug(path_value)
            public_path_slug = _public_path_slug(path_value)
            is_engine = bool(mod_slug) or bool(path_slug) or bool(public_path_slug)
            resolved_slug = mod_slug or path_slug or public_path_slug

            for method in methods or []:
                route_methods.append(
                    RouteMethod(
                        rel,
                        path_value,
                        method.upper(),
                        node.lineno,
                        defining_module,
                        mod_slug,
                        path_slug,
                        public_path_slug,
                        is_engine,
                        resolved_slug,
                    )
                )

    return route_methods, unresolved_paths, unresolved_methods


def _engine_route_methods() -> list[RouteMethod]:
    route_methods, _, _ = _extract_route_methods()
    return [rm for rm in route_methods if rm.is_engine]


def _residual_route_methods() -> list[RouteMethod]:
    route_methods, _, _ = _extract_route_methods()
    return [rm for rm in route_methods if not rm.is_engine]


def _is_verb_or_archive(rm: RouteMethod) -> bool:
    return rm.method in _VERB_METHODS or rm.path.rstrip("/").endswith("/archive")


# Shrink-only census of today's hand-written engine verb/archive routes (2026-09-18,
# commit e164415, measured by this same extraction). This is a documentation fixture,
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

# Pinned residual size (K-H1): every route-method matched by NONE of the three union
# limbs, measured 2026-09-18 @ e164415 (after limb 3 moved /public-api/sales/quotes/{id}
# into engine scope, dropping this from 108 to 107). A change in either direction must
# be explained — growth means a real engine route just got misclassified as platform
# (the K-H1 failure mode) and shrinkage means a platform route was retired or reclassified.
_RESIDUAL_COUNT_PIN = 107


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

    Protects against both limbs silently matching nothing (e.g. the
    VERTICAL_MODULE_NAMES import breaks) and the floor test below passing
    vacuously because its denominator collapsed to zero.
    """
    engine_routes = _engine_route_methods()
    assert len(engine_routes) >= 150, (
        f"Only {len(engine_routes)} engine-scoped route-methods found — expected at "
        "least 150 based on the 2026-09-18 union-rule census (224). Either a real "
        "regression removed most of the estate's REST surface, or both scope limbs broke."
    )
    matched_slugs = {rm.resolved_slug for rm in engine_routes}
    assert matched_slugs, "No engine slug matched any mounted route at all."


def test_residual_size_is_pinned() -> None:
    """The residual ("platform") bucket is a ratchet, not a silent catch-all (K-H1).

    A route counted here that is actually a real engine capability under a
    prefix or module none of the three limbs recognise moves this number and
    must be looked at, not left to accumulate invisibly. It already caught one
    this way: /public-api/sales/quotes/{id} sat here until limb 3 was added,
    which is exactly why the pin exists as a hard assertion rather than a
    comment.
    """
    residual = _residual_route_methods()
    assert len(residual) == _RESIDUAL_COUNT_PIN, (
        f"Residual (non-engine) route-methods = {len(residual)}, expected "
        f"{_RESIDUAL_COUNT_PIN}. If this grew, check whether a real engine route "
        "was just added under a prefix or module neither union limb recognises "
        "(update VERTICAL_MODULE_NAMES/the handler's home, not this pin). If it "
        "shrank, a platform route was retired or reclassified — lower the pin.\n"
        "Sample of current residual (first 15): "
        + ", ".join(f"{rm.method} {rm.path}" for rm in residual[:15])
    )


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
        "'Lane H'). Measured 2026-09-18 @ e164415 under the K-H1 three-limb union "
        "rule: 3 of 224 engine route-methods (1.34%). This xfail is meant to "
        "XPASS-and-fail-CI "
        "the day the floor is crossed, forcing whoever crosses it to remove this "
        "marker deliberately rather than let a moving target go unnoticed. See "
        "MLV16H.md."
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


def test_positive_control_module_owner_limb_catches_customer_portal() -> None:
    """U18 for limb 1 specifically: prove module-owner scoping is not vacuous either.

    customer_portal is the concrete case that motivated limb 1 (K-H1): its routes
    fail the path-slug limb entirely (mounted at /api/portal/*). If limb 1 ever
    regresses to a no-op, customer_portal's routes must disappear from the engine
    set — this proves they do not.
    """
    engine_routes = _engine_route_methods()
    customer_portal_routes = [rm for rm in engine_routes if rm.resolved_slug == "customer_portal"]
    assert customer_portal_routes, (
        "No customer_portal routes found via the union rule — limb 1 (module-owner) "
        "may have regressed, since these routes fail limb 2 (path-slug) by design."
    )
    assert all(rm.mod_slug == "customer_portal" for rm in customer_portal_routes), (
        "customer_portal routes were matched, but not via the module-owner limb as "
        "expected — verify limb 2 did not start matching /api/portal/* by accident."
    )


def test_positive_control_public_path_limb_catches_public_sales_route() -> None:
    """U18 for limb 3 specifically: prove the /public-api/<slug> limb is not vacuous.

    /public-api/sales/quotes/{id} is the concrete case that motivated limb 3
    (K-H1 follow-up): it fails both limb 1 (handler is in nce.admin_handlers,
    not vertical_modules) and limb 2 (the path does not start with /api/). If
    limb 3 ever regresses to a no-op, this route disappears from the engine set
    entirely — this proves it does not.
    """
    engine_routes = _engine_route_methods()
    public_routes = [rm for rm in engine_routes if rm.public_path_slug]
    assert public_routes, (
        "No routes matched via the /public-api/<slug> limb — limb 3 may have "
        "regressed, since /public-api/sales/quotes/{id} fails both other limbs "
        "by design."
    )
    assert all(rm.mod_slug is None and rm.path_slug is None for rm in public_routes), (
        "A /public-api route was matched, but also via limb 1 or limb 2 — verify "
        "those limbs did not start matching /public-api/* by accident."
    )
