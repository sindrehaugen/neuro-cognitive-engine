"""REST surface for the M6.W26 commercial cores (Batch 230a, REST half).

Two things the row's Accept line asks for:

  * a 422 per route for a missing or malformed ``namespace_id`` -- and here also
    for each route's second required argument, since a route that 422s on one and
    500s on the other is only half guarded;
  * one test proving a mutating route bumps the MCP cache generation.

The cache-generation test is the load-bearing one. Without the bump, a write
performed over HTTP leaves the cacheable ``system_design_get_topology`` MCP entry
readable for the full ``MCP_CACHE_TTL_S`` -- silently, with nothing in any log.
It is asserted per route rather than once, and the READ route is asserted NOT to
bump, because a test that only checks "somebody bumped" would pass if every route
bumped unconditionally.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nce import admin_state
from nce.admin_handlers import system_design as routes


class _StubRequest:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self) -> Any:
        return self._body


class _EngineStub:
    def __init__(self) -> None:
        self.pg_pool = None


#: route -> (second required argument, a complete body, bumps the cache?)
_ROUTES: dict[str, tuple[str, dict[str, Any], bool]] = {
    "api_system_design_from_quote": ("quote_id", {"quote_id": "QUOTE-1"}, True),
    "api_system_design_to_quote": ("design_id", {"design_id": "DESIGN-1"}, True),
    "api_system_design_generate_sow": ("design_id", {"design_id": "DESIGN-1"}, False),
    "api_system_design_enrich_design_lines": ("design_id", {"design_id": "DESIGN-1"}, True),
}

_CORE_FOR = {
    "api_system_design_from_quote": "do_design_from_quote",
    "api_system_design_to_quote": "do_design_to_quote",
    "api_system_design_generate_sow": "do_generate_sow",
    "api_system_design_enrich_design_lines": "do_enrich_design_lines",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_missing_namespace_id_is_422(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tenant is never defaulted."""
    _second, complete, _bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)

    response = await getattr(routes, route_name)(_StubRequest(dict(complete)))

    assert response.status_code == 422
    assert "namespace_id" in json.loads(response.body)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_missing_second_required_argument_is_422(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route guarded on one argument and not the other is half guarded."""
    second, _complete, _bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)

    response = await getattr(routes, route_name)(_StubRequest({"namespace_id": str(uuid.uuid4())}))

    assert response.status_code == 422
    assert second in json.loads(response.body)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_no_engine_is_503_not_a_crash(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _second, complete, _bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", None, raising=False)

    body = {"namespace_id": str(uuid.uuid4()), **complete}
    response = await getattr(routes, route_name)(_StubRequest(body))

    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_cache_generation_bump_matches_the_route_kind(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write bumps; the read does not.

    Asserted in BOTH directions on purpose. A test that only checked "the
    mutating routes bumped" would also pass if every route bumped
    unconditionally, which would make the read's cacheability meaningless -- so
    ``generate_sow`` is asserted NOT to bump.
    """
    second, complete, bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)

    core = AsyncMock(return_value={"ok": True})
    bump = AsyncMock()

    with (
        patch.object(routes, _CORE_FOR[route_name], new=core),
        patch.object(routes, "bump_mcp_cache_generation", new=bump),
    ):
        response = await getattr(routes, route_name)(
            _StubRequest({"namespace_id": str(uuid.uuid4()), **complete})
        )

    assert response.status_code == 200
    core.assert_awaited_once()

    if bumps:
        bump.assert_awaited_once()
        assert bump.await_args.kwargs["route"] == route_name, (
            "the bump must be keyed to the ROUTE, not to a tool name -- see the "
            "module docstring in nce/admin_handlers/system_design.py"
        )
    else:
        bump.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_the_core_result_is_returned_unchanged(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These routes are adapters. They add nothing and subtract nothing."""
    _second, complete, _bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)

    payload = {"deliberately": "arbitrary", "nested": {"a": [1, 2, 3]}}

    with (
        patch.object(routes, _CORE_FOR[route_name], new=AsyncMock(return_value=dict(payload))),
        patch.object(routes, "bump_mcp_cache_generation", new=AsyncMock()),
    ):
        response = await getattr(routes, route_name)(
            _StubRequest({"namespace_id": str(uuid.uuid4()), **complete})
        )

    assert json.loads(response.body) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", sorted(_ROUTES))
async def test_a_core_value_error_is_422_not_500(
    route_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad argument the ROUTE cannot see is still the caller's fault, not a 500."""
    _second, complete, _bumps = _ROUTES[route_name]
    monkeypatch.setattr(admin_state, "engine", _EngineStub(), raising=False)

    with (
        patch.object(
            routes,
            _CORE_FOR[route_name],
            new=AsyncMock(side_effect=ValueError("quote_id must be a string")),
        ),
        patch.object(routes, "bump_mcp_cache_generation", new=AsyncMock()) as bump,
    ):
        response = await getattr(routes, route_name)(
            _StubRequest({"namespace_id": str(uuid.uuid4()), **complete})
        )

    assert response.status_code == 422
    # A failed write must not bump the cache generation. Written as a bare call,
    # not `bump.assert_not_awaited(), "msg"` -- that form is a TUPLE expression,
    # not an assert with a message, so the string is dead weight that reads like
    # a failure explanation and never appears in one.
    bump.assert_not_awaited()
