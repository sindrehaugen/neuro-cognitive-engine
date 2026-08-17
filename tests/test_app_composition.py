"""Tests for the NCE-FE-1 app-composition seam (``build_app``).

Verifies a host can mount its own routes/middleware without editing
``nce/admin_app.py`` — see ``docs/FRONTEND_READINESS.md`` (NCE-FE-1).
"""

from __future__ import annotations

import pathlib

from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from nce.admin_app import build_admin_middleware, build_app, create_admin_app


async def _dummy_endpoint(request):  # pragma: no cover - trivial
    return JSONResponse({"ok": True})


class _DummyMiddleware:
    def __init__(self, app, **_kwargs):
        self.app = app

    async def __call__(self, scope, receive, send):  # pragma: no cover - passthrough
        await self.app(scope, receive, send)


def _paths(app) -> list[str]:
    return [r.path for r in app.routes if isinstance(r, Route)]


def test_build_app_no_args_matches_default_routes():
    """(a) With no extras the route set equals build_admin_routes()."""
    from nce.admin_app import build_admin_routes

    app_paths = sorted(_paths(build_app()))
    default_paths = sorted(r.path for r in build_admin_routes() if isinstance(r, Route))
    assert app_paths == default_paths
    # create_admin_app() delegates to build_app() and stays identical.
    assert sorted(_paths(create_admin_app())) == default_paths


def test_build_app_extra_routes_are_served_alongside_nce_routes():
    """(b) Host route is present AND all NCE routes remain."""
    dummy = Route("/__dummy__", endpoint=_dummy_endpoint, methods=["GET"])
    app = build_app(extra_routes=[dummy])
    paths = _paths(app)
    assert "/__dummy__" in paths  # host route mounted
    assert "/api/health" in paths  # NCE route still present
    assert "/healthz" in paths


def test_build_app_extra_middleware_is_outermost():
    """(c) Host middleware is present and sits outside NCE's own stack."""
    app = build_app(extra_middleware=[Middleware(_DummyMiddleware)])
    classes = [m.cls for m in app.user_middleware]
    assert _DummyMiddleware in classes
    # outermost (first) so cross-cutting middleware (e.g. CORS preflight) runs
    # before NCE authentication.
    assert app.user_middleware[0].cls is _DummyMiddleware
    # NCE's own middleware is still installed underneath.
    nce_classes = {m.cls for m in build_admin_middleware()}
    assert nce_classes.issubset(set(classes))


def test_admin_app_source_has_no_host_imports():
    """(d) NCE source carries no host-specific imports."""
    import nce.admin_app as admin_app

    source = pathlib.Path(admin_app.__file__).read_text(encoding="utf-8")
    for marker in ("steps_bff", "steps_mcp_tools", "nettailer", "bravo_hr"):
        assert marker not in source, f"host marker {marker!r} leaked into admin_app.py"
