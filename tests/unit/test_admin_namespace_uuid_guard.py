"""A malformed ``namespace_id`` must be refused at the route, not at the database.

Why this file exists
--------------------
``nce/auth.py``'s ``validate_agent_id`` is documented "Never raises." -- it strips
whitespace, truncates to 128 characters and substitutes ``"default"`` for blank
input. ``procurement.py``, ``project.py`` and ``system_design.py`` nevertheless
guarded their ``namespace_id`` like this::

    try:
        validate_agent_id(namespace_id)      # return value discarded
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid namespace_id: {exc}"}, status_code=422)

The ``except`` can never fire and the return value was thrown away, so those
sixteen routes carried **no UUID-shape check at all**. A malformed id was handed
to the vertical core and, from there, to asyncpg's ``::uuid`` cast -- surfacing as
a 400 or a 500 depending only on whether that particular core happened to
re-validate. ``economy.py``, ``assets.py`` and ``agreements.py`` already carry
inline comments warning about exactly this.

Why the existing tests did not catch it
---------------------------------------
``tests/unit/test_project_pl_routes.py::test_routes_invalid_namespace_id`` patched
``validate_agent_id`` with ``side_effect=ValueError("bad uuid")`` -- an exception
the real function cannot raise -- and asserted 422. It passed over a route with no
validation whatsoever: it proved the ``except`` branch can format a response,
never that anything reaches it. The other surface tests in those modules assert a
status code on the happy path only.

What these tests gate
---------------------
1. Each of the sixteen routes answers 422 with the exact documented body.
2. The connection pool is never touched -- no ``acquire``/``fetch``/``execute``.
   Status code alone is not enough: a core that validates late can also return
   4xx *after* opening a session.
3. Object identity -- all three modules resolve to the one
   ``_shared._require_namespace_id``, so a module quietly reintroducing a local
   copy fails here instead of drifting (a naming convention is not a boundary).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_VALID_NS = "11111111-2222-3333-4444-555555555555"

#: Shapes that ``validate_agent_id`` waves through unchanged but ``uuid.UUID``
#: rejects. The third is 35 hex digits -- one short -- which is the shape most
#: likely to arrive from a real truncating caller.
_MALFORMED = [
    "x",
    "not-a-uuid",
    "12345678-1234-1234-1234-12345678901",
    "' OR 1=1 --",
    "z" * 400,
]

_MISSING_FIELD = {"error": "Missing required field: namespace_id"}
_MISSING_QUERY_PARAM = {"error": "Missing required query param: namespace_id"}

#: Every route in the three modules, with where it reads ``namespace_id`` from and
#: which "missing" dialect it answers. ``body``/``query`` decide how the request
#: mock is built; ``path_id`` marks routes that also need a path parameter.
_ROUTES: list[tuple[str, str, str, dict[str, Any], dict[str, str]]] = [
    # (module, function, source, extra request kwargs, expected "missing" body)
    ("procurement", "api_procurement_calculate_tco", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_rank_suppliers", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_evaluate_match", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_sync_now", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_sync_status", "query", {}, _MISSING_QUERY_PARAM),
    ("procurement", "api_procurement_forecast_rebate", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_recommend_move_spend", "body", {}, _MISSING_FIELD),
    ("procurement", "api_procurement_whatif_spend", "body", {}, _MISSING_FIELD),
    ("project", "api_project_convert_signed_quote", "body", {}, _MISSING_FIELD),
    ("project", "api_project_get_phase", "query", {"path_id": "PROJECT:1"}, _MISSING_QUERY_PARAM),
    ("project", "api_project_advance_phase", "body", {"path_id": "PROJECT:1"}, _MISSING_FIELD),
    ("project", "api_admin_project_my_day", "query", {}, _MISSING_QUERY_PARAM),
    ("project", "api_admin_project_capacity", "query", {}, _MISSING_QUERY_PARAM),
    (
        "project",
        "api_admin_project_scope_creep",
        "query",
        {"path_id": "PROJECT:1"},
        _MISSING_QUERY_PARAM,
    ),
    (
        "project",
        "api_admin_project_status_report",
        "query",
        {"path_id": "PROJECT:1"},
        _MISSING_QUERY_PARAM,
    ),
    ("system_design", "api_system_design_publish_design_docs", "body", {}, _MISSING_FIELD),
]

_ROUTE_IDS = [f"{mod}.{fn}" for mod, fn, _, _, _ in _ROUTES]

#: ``system_design`` refuses a blank ``design_id`` after ``namespace_id``, so the
#: request must carry one for the namespace assertions to be the thing under test.
_EXTRA_BODY_FIELDS: dict[str, dict[str, Any]] = {
    "api_system_design_publish_design_docs": {"design_id": "DESIGN:1"},
    "api_project_advance_phase": {"target_phase": "G1", "actor": "alice"},
}

_POOL_METHODS = ("acquire", "fetch", "fetchrow", "fetchval", "execute", "executemany")


def _unusable_pool() -> MagicMock:
    """A pool whose every entry point explodes if the guard lets a request past.

    ``scoped_pg_session`` funnels through ``pool.acquire``; the direct
    ``fetch``/``fetchrow``/``fetchval``/``execute`` methods asyncpg's ``Pool`` also
    exposes are covered so a core that skips the session helper cannot slip
    through. The routes wrap their cores in ``except Exception`` and would swallow
    these raises into a 500, so the call-count assertion -- not the raise -- is
    what actually gates the property.
    """
    pool = MagicMock()
    for method in _POOL_METHODS:
        getattr(pool, method).side_effect = AssertionError(
            f"malformed namespace_id reached the database via pg_pool.{method}"
        )
    return pool


def _make_request(
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> MagicMock:
    """Minimal Starlette-like request mock (mirrors test_admin_shared_namespace_id.py)."""
    req = MagicMock()
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    req.path_params = path_params or {}
    return req


def _build_request(function: str, source: str, extra: dict[str, Any], ns: str | None) -> MagicMock:
    payload: dict[str, Any] = dict(_EXTRA_BODY_FIELDS.get(function, {}))
    query: dict[str, str] = {}
    if ns is not None:
        if source == "body":
            payload["namespace_id"] = ns
        else:
            query["namespace_id"] = ns
    path_params = {"id": extra["path_id"]} if "path_id" in extra else {}
    return _make_request(path_params=path_params, query=query, body=payload)


async def _call(
    module: str, function: str, source: str, extra: dict[str, Any], ns: str | None
) -> tuple[Any, MagicMock]:
    """Invoke one route with an engine whose pool must never be reached."""
    import importlib

    mod = importlib.import_module(f"nce.admin_handlers.{module}")
    route: Callable[..., Any] = getattr(mod, function)
    engine = MagicMock()
    pool = _unusable_pool()
    engine.pg_pool = pool
    with patch.object(mod, "admin_state") as mock_state:
        mock_state.engine = engine
        response = await route(_build_request(function, source, extra, ns))
    return response, pool


# ---------------------------------------------------------------------------
# 1. A malformed namespace_id is refused with 422, before the pool is opened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("module,function,source,extra,_missing", _ROUTES, ids=_ROUTE_IDS)
@pytest.mark.parametrize("bad_ns", _MALFORMED)
async def test_malformed_namespace_id_is_422_and_never_reaches_the_pool(
    module: str,
    function: str,
    source: str,
    extra: dict[str, Any],
    _missing: dict[str, str],
    bad_ns: str,
) -> None:
    response, pool = await _call(module, function, source, extra, bad_ns)

    assert response.status_code == 422, (
        f"{module}.{function} answered {response.status_code} for namespace_id={bad_ns!r}; "
        "the UUID-shape check is missing or runs too late"
    )
    body = json.loads(response.body)
    assert set(body) == {"error"}
    assert body["error"].startswith("Invalid namespace_id: ")

    for method in _POOL_METHODS:
        assert getattr(pool, method).call_count == 0, (
            f"{module}.{function} opened pg_pool.{method} for a malformed namespace_id"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ns", _MALFORMED)
async def test_malformed_namespace_id_body_is_identical_across_all_routes(bad_ns: str) -> None:
    """One helper, one body: the surfaces must not disagree on the wording."""
    bodies = set()
    for module, function, source, extra, _missing in _ROUTES:
        response, _pool = await _call(module, function, source, extra, bad_ns)
        bodies.add(json.loads(response.body)["error"])
    assert len(bodies) == 1, f"surfaces disagree on the malformed body: {sorted(bodies)}"


# ---------------------------------------------------------------------------
# 2. The "missing" dialect each route already spoke is preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("module,function,source,extra,missing", _ROUTES, ids=_ROUTE_IDS)
async def test_absent_namespace_id_keeps_its_documented_422_body(
    module: str,
    function: str,
    source: str,
    extra: dict[str, Any],
    missing: dict[str, str],
) -> None:
    """Folding onto the shared helper must not silently reword these responses.

    Body routes answer ``Missing required field``; query routes answer ``Missing
    required query param``. That split is codebase-wide (``product.py``,
    ``vendors.py``, ``sales.py``) and is preserved through the helper's
    ``missing_error`` argument rather than flattened onto one message.
    """
    response, pool = await _call(module, function, source, extra, None)

    assert response.status_code == 422
    assert json.loads(response.body) == missing
    assert pool.acquire.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("module,function,source,extra,missing", _ROUTES, ids=_ROUTE_IDS)
async def test_blank_namespace_id_is_missing_not_invalid(
    module: str,
    function: str,
    source: str,
    extra: dict[str, Any],
    missing: dict[str, str],
) -> None:
    """Whitespace-only input must not be reported as a malformed UUID.

    ``validate_agent_id`` turns blank input into the literal ``"default"``, which
    would otherwise parse-fail and produce a misleading ``Invalid`` message.
    """
    response, _pool = await _call(module, function, source, extra, "   ")

    assert response.status_code == 422
    assert json.loads(response.body) == missing


# ---------------------------------------------------------------------------
# 3. A well-formed namespace_id still gets through the guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("module,function,source,extra,_missing", _ROUTES, ids=_ROUTE_IDS)
async def test_valid_namespace_id_passes_the_guard(
    module: str,
    function: str,
    source: str,
    extra: dict[str, Any],
    _missing: dict[str, str],
) -> None:
    """The guard must reject malformed input without also rejecting valid input.

    A guard that answered 422 unconditionally would satisfy every assertion above.
    The route is expected to fail *downstream* here (the mock pool raises), so the
    only assertion is that it did not stop at the namespace check.
    """
    response, _pool = await _call(module, function, source, extra, _VALID_NS)

    if response.status_code == 422:
        body = json.loads(response.body)
        assert "namespace_id" not in body.get("error", ""), (
            f"{module}.{function} rejected a well-formed namespace_id: {body}"
        )


# ---------------------------------------------------------------------------
# 4. One helper, not sixteen inline copies -- resolved by object identity
# ---------------------------------------------------------------------------


def test_all_three_modules_share_one_helper_object() -> None:
    from nce.admin_handlers import _shared, procurement, project, system_design

    for module in (procurement, project, system_design):
        assert module._require_namespace_id is _shared._require_namespace_id, (
            f"{module.__name__} no longer shares _shared._require_namespace_id"
        )


def test_folded_modules_do_not_reimport_validate_agent_id() -> None:
    """The fold removed each module's own ``validate_agent_id`` import.

    Its return value was discarded and its ``except ValueError`` was dead; a
    reappearance signals the dead guard being reintroduced alongside the real one.
    """
    from nce.admin_handlers import procurement, project, system_design

    for module in (procurement, project, system_design):
        assert not hasattr(module, "validate_agent_id"), (
            f"{module.__name__} re-imported validate_agent_id"
        )


def test_no_dead_validate_agent_id_guard_remains_in_the_folded_modules() -> None:
    """Source-level backstop: no ``try: validate_agent_id(...) / except ValueError``.

    The identity check above passes as soon as the helper is imported, even if a
    dead guard is left standing next to it. This reads the source instead.
    """
    import ast
    import inspect

    from nce.admin_handlers import procurement, project, system_design

    for module in (procurement, project, system_design):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [
                child
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "validate_agent_id"
            ]
            assert not calls, (
                f"{module.__name__} still wraps validate_agent_id in a try/except; "
                "it never raises, so that guard is dead"
            )
