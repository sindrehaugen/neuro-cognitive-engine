"""Coverage gate: a REST mutation must invalidate the MCP cache whenever its MCP twin does.

``tests/test_rest_cache_invalidation.py`` proves the *behaviour* for one route against a
real Redis.  This module proves *coverage*: it walks the real Starlette route table and the
real MCP tool registry and fails if a mutating REST route in ``nce/admin_handlers/`` invokes
the same domain core as a ``mutation=True`` MCP tool without calling
``bump_mcp_cache_generation``.

Why that criterion.  The cache generation counter is global, and
``nce/mcp_stdio_dispatch.py`` bumps it after every successful ``mutation=True`` tool call.
When one core is reachable from both surfaces, MCP provably invalidates and REST provably
must, or the two disagree for ``MCP_CACHE_TTL_S`` (300 s).  It is decidable from the
registry, which is what makes it gateable rather than a judgement call.

How cores are matched — this part is load-bearing, and an earlier version of this file got
it wrong.  Cores are resolved to function **objects** and compared by identity:

* Matching on a ``do_`` name prefix does NOT work.  Plenty of shared cores are named
  ``confirm``, ``reject``, ``create_grant``, ``boost_memory``.  A prefix filter silently
  excluded them and let this gate report green over proven stale-read pairs.
* Matching on bare names does NOT work either: ``get``/``execute``/``UUID`` collide across
  unrelated modules and produce nonsense.
* Several admin handlers import their core with a **function-local** ``from X import Y``,
  so module globals alone miss them; local bindings are resolved too.

A callee counts as a domain core only if it is ``async``, defined under ``nce.``, and
outside the infrastructure denylist below — domain writes are coroutines, while
``scoped_pg_session``/``validate_agent_id``/``UUID`` are shared plumbing, not the mutation.
``engine.<method>()`` calls are matched by method name when the receiver looks like an
engine, which is how ``boost_memory`` / ``manage_namespace`` / the migration verbs are
reached.

WHAT THIS TEST STILL DOES **NOT** GATE — read before trusting a green run:

* **Presence, not reachability.**  It asserts the call appears in the endpoint's AST.  A
  bump on a branch that never runs on the success path would pass here.  Only the
  behavioural test catches that, and it covers one route.
* **Only ``nce/admin_handlers/``.**  Mutations through other surfaces — A2A handlers,
  webhook receivers, cron jobs, the re-embedding worker, reactive automation subscribers —
  are outside the walk entirely.
* **Only cores shared with a ``mutation=True`` tool.**  A REST route that writes inline, or
  through a core with no MCP twin, is invisible to the criterion even when a
  ``cacheable=True`` tool reads the same rows.  ``PUT /api/sales/targets`` is the known
  benign example: it writes ``sales_targets``, which no MCP tool reads at all.
* **Not asynchronous follow-on writes.**  ``POST /api/replay/fork`` bumps for its run row;
  the background replay's own writes are invalidated by nobody, on either surface.

So green means "no dual-surface core regressed", not "no REST mutation can serve a stale
MCP read".

Pure static analysis — no Redis and no Postgres — so it runs in the unit job where the
behavioural test cannot.  The analysis runs once at import time so no single test pays it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

from nce.tool_registry import TOOL_REGISTRY

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_APP = REPO_ROOT / "nce" / "admin_app.py"
HANDLER_DIR = REPO_ROOT / "nce" / "admin_handlers"

BUMP_FN = "bump_mcp_cache_generation"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: Modules whose callables are shared plumbing rather than domain mutations.
INFRA_MODULES = {
    "nce.db_utils",
    "nce.auth",
    "nce.admin_http_support",
    "nce.admin_state",
    "nce.config",
    "nce.models",
    "nce.observability",
    "nce.mcp_errors",
    "nce.mcp_args",
    "nce.quotas",
    "nce.constants",
    "nce.admin_routes",
    "nce.temporal",
    "nce.background_task_manager",
    "nce.admin_handlers._shared",
}
INFRA_NAMES = {
    "UUID",
    "uuid4",
    "scoped_pg_session",
    "validate_agent_id",
    "set_namespace_context",
    "require_namespace_id",
}
#: Attribute calls too generic to imply a shared core.
GENERIC_ATTRS = {
    "get",
    "set",
    "execute",
    "fetch",
    "fetchrow",
    "fetchval",
    "acquire",
    "json",
    "dumps",
    "loads",
    "append",
    "update",
    "add",
    "pop",
    "strip",
    "lower",
    "upper",
    "close",
    "exception",
    "warning",
    "info",
    "error",
    "debug",
}
ENGINE_HINTS = ("engine", "eng")


def _is_domain_core(obj: object) -> bool:
    if not inspect.iscoroutinefunction(obj):
        return False
    module = getattr(obj, "__module__", "") or ""
    if not module.startswith("nce.") or module in INFRA_MODULES:
        return False
    return getattr(obj, "__name__", "") not in INFRA_NAMES


def _local_bindings(node: ast.AST) -> dict[str, object]:
    """Resolve function-local ``from M import N`` bindings to objects."""
    out: dict[str, object] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and sub.module:
            try:
                mod = importlib.import_module(sub.module)
            except Exception:
                continue
            for alias in sub.names:
                obj = getattr(mod, alias.name, None)
                if obj is not None:
                    out[alias.asname or alias.name] = obj
    return out


def _analyse(node: ast.AST, module: object) -> tuple[dict[int, str], set[str]]:
    """-> ({id(core_obj): call_name}, {engine method names})."""
    scope = dict(vars(module))
    scope.update(_local_bindings(node))
    cores: dict[int, str] = {}
    methods: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            obj = scope.get(func.id)
            if obj is not None and _is_domain_core(obj):
                cores[id(obj)] = func.id
        elif isinstance(func, ast.Attribute):
            if func.attr in GENERIC_ATTRS:
                continue
            try:
                receiver = ast.unparse(func.value).lower()
            except Exception:
                continue
            if any(hint in receiver for hint in ENGINE_HINTS):
                methods.add(func.attr)
    return cores, methods


def _mutation_tool_cores() -> tuple[dict[int, set[str]], dict[str, set[str]]]:
    """Cores (by object id) and engine-method names used by ``mutation=True`` tools."""
    by_core: dict[int, set[str]] = {}
    by_method: dict[str, set[str]] = {}
    for tool_name, spec in TOOL_REGISTRY.items():
        if not spec.mutation:
            continue
        qualname = getattr(spec.handler, "__qualname__", "")
        module_name, _, attr = qualname.rpartition(".")
        if not module_name:
            continue
        try:
            module = importlib.import_module(module_name)
            node = ast.parse(inspect.getsource(getattr(module, attr))).body[0]
        except Exception:
            continue
        cores, methods = _analyse(node, module)
        for core_id in cores:
            by_core.setdefault(core_id, set()).add(tool_name)
        for method in methods:
            by_method.setdefault(method, set()).add(tool_name)
    return by_core, by_method


def _routes() -> list[tuple[str, tuple[str, ...], str]]:
    tree = ast.parse(ADMIN_APP.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Route":
            continue
        path = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
        endpoint: str | None = None
        methods: tuple[str, ...] = ("GET",)
        for kw in node.keywords:
            if kw.arg == "endpoint":
                val = kw.value
                endpoint = val.attr if isinstance(val, ast.Attribute) else getattr(val, "id", None)
            elif kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                methods = tuple(e.value for e in kw.value.elts if isinstance(e, ast.Constant))
        if path and endpoint:
            found.append((path, methods, endpoint))
    return found


def _endpoints() -> dict[str, dict]:
    info: dict[str, dict] = {}
    for py in sorted(HANDLER_DIR.glob("*.py")):
        try:
            module = importlib.import_module(f"nce.admin_handlers.{py.stem}")
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            cores, methods = _analyse(node, module)
            info[node.name] = {
                "loc": f"nce/admin_handlers/{py.name}:{node.lineno}",
                "cores": cores,
                "methods": methods,
                "bumps": BUMP_FN in ast.unparse(node),
            }
    return info


# Computed once at import so no individual test pays the analysis cost.
_TOOL_CORES, _TOOL_METHODS = _mutation_tool_cores()
_ROUTES = _routes()
_ENDPOINTS = _endpoints()


def _dual_surface_rows():
    """Mutating REST routes that share a core with a mutation=True MCP tool."""
    rows = []
    for path, methods, endpoint in _ROUTES:
        if not (set(methods) & MUTATING_METHODS):
            continue
        info = _ENDPOINTS.get(endpoint)
        if not info:
            continue
        shared = set()
        for core_id, name in info["cores"].items():
            if core_id in _TOOL_CORES:
                shared.add((name, tuple(sorted(_TOOL_CORES[core_id]))))
        for method in info["methods"]:
            if method in _TOOL_METHODS:
                shared.add((f"engine.{method}", tuple(sorted(_TOOL_METHODS[method]))))
        if shared:
            rows.append((path, methods, endpoint, info, sorted(shared)))
    return rows


def test_analysis_inputs_are_live():
    """Guard the guard: if these collapse, the coverage assertion passes vacuously."""
    assert len(_TOOL_CORES) + len(_TOOL_METHODS) >= 15, (
        f"only {len(_TOOL_CORES)} cores / {len(_TOOL_METHODS)} engine methods resolved from "
        "mutation=True tools — the registry walk is broken, not the codebase"
    )
    assert len(_ROUTES) >= 100, f"only {len(_ROUTES)} routes parsed from admin_app.py"
    assert len(_ENDPOINTS) >= 100, f"only {len(_ENDPOINTS)} admin handler functions parsed"


def test_dual_surface_routes_are_detected():
    """The criterion must actually select routes, or the gate below is vacuous."""
    rows = _dual_surface_rows()
    assert len(rows) >= 15, (
        f"only {len(rows)} dual-surface mutating routes found; expected ~19 as of Batch 143. "
        "A sharp drop means core resolution broke, not that the bug class disappeared."
    )


def test_mutating_rest_routes_sharing_a_core_bump_the_cache_generation():
    """No mutating REST route may share a mutation-tool core without invalidating the cache."""
    offenders = []
    for path, methods, endpoint, info, shared in _dual_surface_rows():
        if info["bumps"]:
            continue
        offenders.append(
            f"  {'/'.join(methods):6} {path}\n"
            f"      endpoint : {endpoint}  ({info['loc']})\n"
            f"      core(s)  : {[n for n, _ in shared]}\n"
            f"      also MCP : {sorted({t for _, ts in shared for t in ts})} "
            f"(mutation=True -> dispatch bumps)\n"
            f"      missing  : await {BUMP_FN}(admin_state.engine, route=...)"
        )

    assert not offenders, (
        f"{len(offenders)} mutating REST route(s) share a core with a mutation=True MCP tool "
        f"but do not invalidate the MCP cache themselves.\nCacheable MCP reads will serve "
        f"pre-mutation data for up to MCP_CACHE_TTL_S (300 s):\n\n" + "\n\n".join(offenders)
    )
