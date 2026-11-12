"""REST-surface mutations must invalidate the Pure-Core MCP response cache.

Covers Pure-Core REST mutations (`api_admin_namespaces_update_metadata`) invalidating
cacheable Pure-Core MCP tool reads (`graph_search`) against a real Redis instance,
with zero dependency on vertical-engine tables or modules.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis.asyncio as aioredis

from nce import admin_state
from nce.admin_handlers import fleet as fleet_routes
from nce.admin_handlers._shared import bump_mcp_cache_generation
from nce.auth import _mcp_bound_namespace_id
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


class _StubCoreEngine:
    def __init__(self, redis_client: Any) -> None:
        self.redis_client = redis_client
        self.pg_pool = None
        self.state_label = "before-rest-mutation"
        self.graph_search_calls = 0

    async def graph_search(self, req: Any) -> dict[str, Any]:
        self.graph_search_calls += 1
        return {
            "namespace_id": str(req.namespace_id),
            "query": req.query,
            "nodes": [{"label": self.state_label, "entity_type": "CONCEPT"}],
            "edges": [],
        }

    async def manage_namespace(self, payload: Any, admin_identity: str = "admin") -> dict[str, Any]:
        self.state_label = "after-rest-mutation"
        return {
            "status": "ok",
            "namespace_id": str(payload.namespace_id),
            "admin_identity": admin_identity,
        }


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
async def test_rest_namespace_metadata_mutation_invalidates_mcp_graph_search_cache(monkeypatch):
    """Updating namespace metadata over REST must invalidate cached `graph_search` MCP responses."""
    monkeypatch.setattr("nce.quotas.cfg.NCE_QUOTAS_ENABLED", False)

    redis_client = await _open_redis()
    namespace_id = _mcp_bound_namespace_id() or str(uuid.uuid4())
    engine = _StubCoreEngine(redis_client)
    monkeypatch.setattr(admin_state, "engine", engine, raising=False)

    read_args = {"namespace_id": namespace_id, "agent_id": "u1", "query": "core-graph"}

    try:
        first = json.loads(
            (await execute_call_tool(engine, "graph_search", dict(read_args)))[0].text
        )
        assert first["nodes"][0]["label"] == "before-rest-mutation", first
        assert engine.graph_search_calls == 1

        assert await redis_client.keys("mcp_cache:v*"), (
            "Precondition failed: the first MCP read did not populate the Redis cache."
        )

        # Second identical read before REST mutation must be served from Redis cache.
        cached = json.loads(
            (await execute_call_tool(engine, "graph_search", dict(read_args)))[0].text
        )
        assert cached["nodes"][0]["label"] == "before-rest-mutation"
        assert engine.graph_search_calls == 1

        response = await fleet_routes.api_admin_namespaces_update_metadata(
            _StubRequest(
                path_params={"namespace_id": namespace_id},
                body={"temporal_retention_days": 120},
            )
        )
        assert response.status_code == 200, getattr(response, "body", response)
        assert engine.state_label == "after-rest-mutation", "the REST mutation did not commit"

        after = json.loads(
            (await execute_call_tool(engine, "graph_search", dict(read_args)))[0].text
        )
        assert after["nodes"][0]["label"] == "after-rest-mutation", (
            "STALE MCP CACHE: namespace metadata was updated over REST, but cacheable MCP "
            f"tool `graph_search` still returned pre-mutation state: {after!r}."
        )
        assert engine.graph_search_calls == 2

        await bump_mcp_cache_generation(engine, route="api_settings_update")
    finally:
        await _clear_test_keys(redis_client)
        await redis_client.aclose()
