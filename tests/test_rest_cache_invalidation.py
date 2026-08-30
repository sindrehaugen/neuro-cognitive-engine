"""REST-surface mutations must invalidate the MCP response cache.

Background
----------
MCP tool results are cached in Redis under a **global generation counter**
(``mcp_cache_generation``).  ``nce/mcp_stdio_dispatch.py`` bumps that counter
after any successful ``mutation=True`` tool call, which is what makes stale
``cacheable=True`` reads unreachable.

The REST routes in ``nce/admin_handlers/*`` call the very same domain cores
directly and never go through the dispatch loop.  Without an explicit bump, a
mutation performed over REST leaves stale MCP cache entries alive for up to
``MCP_CACHE_TTL_S`` (300 s) — a client reading over MCP sees pre-mutation data
for five minutes, with no error and nothing in any log.

Two routes are covered behaviourally here, deliberately chosen to be different
shapes: ``assets_advance_lifecycle`` (core named ``do_advance_lifecycle``) and
``merge_queue_confirm`` (core named plainly ``confirm``).  The second exists
because a ``do_``-prefix criterion silently excluded that whole family, and
``merge_queue_list`` is ``cacheable=True`` while filtering on the exact ``status``
column ``confirm`` writes — the sharpest stale-read pair in the codebase.
Structural coverage for the remaining routes is in
``tests/test_rest_cache_invalidation_coverage.py``.

Why this test needs a real Redis
--------------------------------
The cache is a *no-op* without Redis, and a mock ``redis_client`` (as used by
``tests/test_mcp_cache.py``) does not store or return values — so a mocked
version of this test would pass on broken code and prove nothing.  This module
therefore talks to a real Redis and **fails rather than skips** when one is not
reachable (project rule §6.4: green is not evidence, RED is).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis.asyncio as aioredis

from nce import admin_state
from nce.admin_handlers import assets as assets_routes
from nce.admin_handlers import entity_resolution as er_routes
from nce.auth import _mcp_bound_namespace_id
from nce.entity_resolution import mcp_handlers as er_cores
from nce.mcp_stdio_dispatch import execute_call_tool
from nce.vertical_modules.assets import mcp_handlers as assets_cores

pytestmark = pytest.mark.integration


#: Redis logical database this test is allowed to write to and clean up.
#: Never the app's own cache db — see :func:`_redis_url`.
_TEST_REDIS_DB = 15


def _redis_url() -> str:
    """Test Redis DSN, pinned to a scratch database.

    ``NCE_TEST_REDIS_URL`` is taken verbatim when set — that is an explicit
    opt-in and the caller owns the consequences.  Otherwise the ambient
    ``REDIS_URL`` is reused for host and credentials only, with its database
    **forced to** :data:`_TEST_REDIS_DB`.

    That rewrite is not cosmetic: this module deletes keys during cleanup, and
    the repo's own ``.env`` points ``REDIS_URL`` at ``…/1`` — the live dev
    cache.  Honouring it as-is would let a routine test run clear a running
    instance's cache.
    """
    explicit = os.environ.get("NCE_TEST_REDIS_URL")
    if explicit:
        return explicit

    ambient = os.environ.get("REDIS_URL")
    if not ambient:
        return f"redis://localhost:6379/{_TEST_REDIS_DB}"

    parts = urlsplit(ambient)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/{_TEST_REDIS_DB}", parts.query, parts.fragment)
    )


async def _clear_test_keys(client: Any) -> None:
    """Remove only this module's keys — never ``flushdb``.

    Scoped deletion keeps the blast radius to the MCP cache namespace even if
    someone points ``NCE_TEST_REDIS_URL`` at a shared database.
    """
    keys = await client.keys("mcp_cache:v*")
    if keys:
        await client.delete(*keys)
    await client.delete("mcp_cache_generation")


class _StubRequest:
    """Minimal duck-typed Starlette request.

    The routes under test read exactly two things off the request: a path
    parameter and ``await request.json()``.  Supplying those directly exercises
    the real route body — including the cache bump — without standing up the
    admin ASGI app and its HMAC middleware.  Route *wiring* (that these
    endpoints are the ones actually mounted at those paths) is covered
    separately by ``tests/test_rest_cache_invalidation_coverage.py``, which
    reads the real route table.
    """

    def __init__(self, path_params: dict[str, str], body: dict[str, Any]) -> None:
        self.path_params = path_params
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _StubEngine:
    """Engine surface touched by the dispatch loop once the cores are stubbed."""

    def __init__(self, redis_client: Any) -> None:
        self.redis_client = redis_client
        self.pg_pool = None


async def _open_redis():
    url = _redis_url()
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment failure path
        try:
            await client.aclose()
        finally:
            pytest.fail(
                f"A real Redis is required at {url!r} but is unreachable ({exc!r}). "
                "This test asserts a caching behaviour that is a silent no-op "
                "without Redis, so it fails instead of skipping — a skip here "
                "would prove nothing (§6.4). Set NCE_TEST_REDIS_URL to override."
            )
    await _clear_test_keys(client)
    return client


@pytest.mark.asyncio
async def test_rest_lifecycle_mutation_invalidates_mcp_asset_cache(monkeypatch):
    """Read (MCP, cacheable) -> mutate (REST) -> read (MCP) must show the mutation.

    On unmodified ``main`` the second read is served from the pre-mutation
    cache entry, because ``api_assets_advance_lifecycle`` never bumps the
    generation counter.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    redis_client = await _open_redis()
    # Honour an ambient NCE_MCP_NAMESPACE_ID: when the server is pinned to a
    # tenant, `enforce_mcp_tool_auth` rejects any other namespace_id outright.
    namespace_id = _mcp_bound_namespace_id() or str(uuid.uuid4())
    asset_id = str(uuid.uuid4())

    # Single mutable source of truth, standing in for the `assets` table.
    db_state = {"lifecycle_state": "RECEIVED"}

    async def fake_do_get_asset(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "asset": {"id": asset_id, "lifecycle_state": db_state["lifecycle_state"]},
        }

    async def fake_do_advance_lifecycle(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
        previous = db_state["lifecycle_state"]
        db_state["lifecycle_state"] = params["target_state"]  # the "commit"
        return {
            "ok": True,
            "changed": True,
            "asset_id": params["asset_id"],
            "previous_state": previous,
            "new_state": params["target_state"],
            "error": None,
        }

    # `handle_assets_get` resolves `do_get_asset` as a module global.
    monkeypatch.setattr(assets_cores, "do_get_asset", fake_do_get_asset)
    # `nce.admin_handlers.assets` imported the core into its own namespace.
    monkeypatch.setattr(assets_routes, "do_advance_lifecycle", fake_do_advance_lifecycle)

    engine = _StubEngine(redis_client)
    monkeypatch.setattr(admin_state, "engine", engine, raising=False)

    read_args = {
        "namespace_id": namespace_id,
        "agent_id": "u1",
        "asset_id": asset_id,
    }

    try:
        # 1. Read through the cacheable MCP path — populates the cache.
        first = await execute_call_tool(engine, "assets_get", dict(read_args))
        first_payload = json.loads(first[0].text)
        assert first_payload["asset"]["lifecycle_state"] == "RECEIVED", first_payload

        cached_keys = await redis_client.keys("mcp_cache:v*")
        assert cached_keys, (
            "Precondition failed: the first MCP read did not populate the cache, "
            "so this test could not detect staleness either way."
        )

        # 2. Mutate through the REST route (bypasses the MCP dispatch loop).
        request = _StubRequest(
            path_params={"id": asset_id},
            body={"namespace_id": namespace_id, "target_state": "VERIFIED"},
        )
        response = await assets_routes.api_assets_advance_lifecycle(request)
        assert response.status_code == 200, getattr(response, "body", response)
        assert db_state["lifecycle_state"] == "VERIFIED", "the mutation did not commit"

        # 3. Read again through MCP — must reflect the mutation, not the cache.
        second = await execute_call_tool(engine, "assets_get", dict(read_args))
        second_payload = json.loads(second[0].text)

        assert second_payload["asset"]["lifecycle_state"] == "VERIFIED", (
            "STALE MCP CACHE: the REST lifecycle mutation committed "
            f"({db_state['lifecycle_state']!r}) but the cacheable MCP tool "
            f"`assets_get` still served {second_payload['asset']['lifecycle_state']!r}. "
            "The REST route did not bump `mcp_cache_generation`, so the "
            "pre-mutation entry stays readable for the full MCP_CACHE_TTL_S."
        )
    finally:
        await _clear_test_keys(redis_client)
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_rest_merge_queue_confirm_invalidates_mcp_queue_listing(monkeypatch):
    """Second surface, and the sharpest stale-read pair in the codebase.

    ``merge_queue_list`` is ``cacheable=True`` and filters on ``status = 'pending'``;
    ``confirm`` writes exactly that column.  So a human-review queue listing keeps
    showing an already-decided row as pending for the full TTL.

    This route was invisible to the first version of the coverage guard: its shared
    core is named ``confirm``, not ``do_confirm``, and the guard filtered candidate
    cores on a ``do_`` prefix.  Covering it behaviourally — not just structurally —
    is the point of this test.
    """
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    redis_client = await _open_redis()
    namespace_id = _mcp_bound_namespace_id() or str(uuid.uuid4())
    queue_id = str(uuid.uuid4())

    # Row shape mirrors what `handle_merge_queue_list` projects out of the table.
    rows = [
        {
            "id": queue_id,
            "node_type": "Vendor",
            "candidate_payload": {"name": "Acme AS"},
            "target_node_id": None,
            "score": 0.91,
            "status": "pending",
            "created_at": datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
        }
    ]

    @contextlib.asynccontextmanager
    async def fake_session(pool, ns):
        yield object()  # no Postgres: the cache mechanism is what is under test

    async def fake_list_pending(conn, namespace_id, **kw):
        return [dict(r) for r in rows if r["status"] == "pending"]

    async def fake_confirm(conn, *, namespace_id, queue_id, decided_by):
        for r in rows:
            if r["id"] == str(queue_id):
                r["status"] = "confirmed"  # the "commit"
        return None

    monkeypatch.setattr(er_cores, "scoped_pg_session", fake_session)
    monkeypatch.setattr(er_cores, "list_pending", fake_list_pending)
    monkeypatch.setattr(er_routes, "scoped_pg_session", fake_session)
    monkeypatch.setattr(er_routes, "confirm", fake_confirm)

    engine = _StubEngine(redis_client)
    monkeypatch.setattr(admin_state, "engine", engine, raising=False)

    read_args = {"namespace_id": namespace_id, "agent_id": "u1"}

    try:
        # 1. Read the pending queue through the cacheable MCP tool.
        first = json.loads(
            (await execute_call_tool(engine, "merge_queue_list", dict(read_args)))[0].text
        )
        assert len(first["pending"]) == 1, first

        assert await redis_client.keys("mcp_cache:v*"), (
            "Precondition failed: the first MCP read did not populate the cache."
        )

        # 2. Confirm the row through the REST route.
        response = await er_routes.api_entity_resolution_queue_confirm(
            _StubRequest(
                path_params={"queue_id": queue_id},
                body={"namespace_id": namespace_id, "decided_by": "reviewer-1"},
            )
        )
        assert response.status_code == 200, getattr(response, "body", response)
        assert rows[0]["status"] == "confirmed", "the mutation did not commit"

        # 3. The queue listing must no longer show the row as pending.
        second = json.loads(
            (await execute_call_tool(engine, "merge_queue_list", dict(read_args)))[0].text
        )

        assert second["pending"] == [], (
            "STALE MCP CACHE: the row was confirmed over REST, but the cacheable MCP "
            f"tool `merge_queue_list` still lists it as pending: {second['pending']!r}. "
            "A reviewer keeps seeing a decided row for the full MCP_CACHE_TTL_S."
        )
    finally:
        await _clear_test_keys(redis_client)
        await redis_client.aclose()
