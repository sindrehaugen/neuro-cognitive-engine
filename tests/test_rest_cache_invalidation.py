"""REST-surface mutations must invalidate the MCP response cache.

Covers Core's entity-resolution REST mutation (`api_entity_resolution_queue_confirm`)
invalidating the cacheable MCP read (`merge_queue_list`) against a real Redis instance.
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
from nce.admin_handlers import entity_resolution as er_routes
from nce.admin_handlers._shared import bump_mcp_cache_generation
from nce.auth import _mcp_bound_namespace_id
from nce.entity_resolution import mcp_handlers as er_cores
from nce.mcp_stdio_dispatch import execute_call_tool

pytestmark = pytest.mark.integration

_TEST_REDIS_DB = 15


def _redis_url() -> str:
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
    keys = await client.keys("mcp_cache:v*")
    if keys:
        await client.delete(*keys)
    gen_keys = await client.keys("mcp_cache_generation*")
    if gen_keys:
        await client.delete(*gen_keys)


class _StubRequest:
    def __init__(self, path_params: dict[str, str], body: dict[str, Any]) -> None:
        self.path_params = path_params
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


class _StubEngine:
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
            pytest.fail(f"A real Redis is required at {url!r} but is unreachable ({exc!r}).")
    await _clear_test_keys(client)
    return client


@pytest.mark.asyncio
async def test_rest_merge_queue_confirm_invalidates_mcp_queue_listing(monkeypatch):
    """Confirming a merge queue row via REST must invalidate `merge_queue_list` MCP cache."""
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    redis_client = await _open_redis()
    namespace_id = _mcp_bound_namespace_id() or str(uuid.uuid4())
    queue_id = str(uuid.uuid4())

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
        yield object()

    async def fake_list_pending(conn, namespace_id, **kw):
        return [dict(r) for r in rows if r["status"] == "pending"]

    async def fake_confirm(conn, *, namespace_id, queue_id, decided_by):
        for r in rows:
            if r["id"] == str(queue_id):
                r["status"] = "confirmed"
        return None

    monkeypatch.setattr(er_cores, "scoped_pg_session", fake_session)
    monkeypatch.setattr(er_cores, "list_pending", fake_list_pending)
    monkeypatch.setattr(er_routes, "scoped_pg_session", fake_session)
    monkeypatch.setattr(er_routes, "confirm", fake_confirm)

    engine = _StubEngine(redis_client)
    monkeypatch.setattr(admin_state, "engine", engine, raising=False)

    read_args = {"namespace_id": namespace_id, "agent_id": "u1"}

    try:
        first = json.loads(
            (await execute_call_tool(engine, "merge_queue_list", dict(read_args)))[0].text
        )
        assert len(first["pending"]) == 1, first

        assert await redis_client.keys("mcp_cache:v*"), (
            "Precondition failed: the first MCP read did not populate the cache."
        )

        response = await er_routes.api_entity_resolution_queue_confirm(
            _StubRequest(
                path_params={"queue_id": queue_id},
                body={"namespace_id": namespace_id, "decided_by": "reviewer-1"},
            )
        )
        assert response.status_code == 200, getattr(response, "body", response)
        assert rows[0]["status"] == "confirmed", "the mutation did not commit"

        second = json.loads(
            (await execute_call_tool(engine, "merge_queue_list", dict(read_args)))[0].text
        )

        assert second["pending"] == [], (
            "STALE MCP CACHE: the row was confirmed over REST, but the cacheable MCP "
            f"tool `merge_queue_list` still lists it as pending: {second['pending']!r}."
        )

        await bump_mcp_cache_generation(engine, route="api_settings_update")
    finally:
        await _clear_test_keys(redis_client)
        await redis_client.aclose()
