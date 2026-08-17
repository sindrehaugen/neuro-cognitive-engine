import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _stdio_smoke_enabled() -> bool:
    """Full MCP stdio smoke needs Postgres (pgvector) + Mongo + schema/RLS applied.

    Local ``pytest`` passes without Docker when this is unset; enable in CI or after
    ``docker compose up`` by setting ``NCE_STDIO_SMOKE=1``.
    """
    return os.environ.get("NCE_STDIO_SMOKE", "").strip() == "1"


def _stdio_server_params() -> StdioServerParameters:
    """Stdio MCP uses a restricted default env — propagate NCE_MASTER_KEY for server.py."""
    env = dict(get_default_environment())
    env["NCE_MASTER_KEY"] = os.environ.get("NCE_MASTER_KEY", "x" * 32)
    return StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        env=env,
        cwd=str(_REPO_ROOT),
    )


@pytest.mark.asyncio
async def test_stdio_smoke_indexing():
    if not _stdio_smoke_enabled():
        pytest.skip(
            "Set NCE_STDIO_SMOKE=1 with NCE stack running (Postgres pgvector, Mongo, schema)."
        )
    server_params = _stdio_server_params()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30.0)

            # Smoke test: index a file and check status
            index_res = await asyncio.wait_for(
                session.call_tool(
                    "index_code_file",
                    {
                        "filepath": "smoke_test_target.py",
                        "language": "python",
                        "raw_code": "def smoke_task():\n    return 42",
                    },
                ),
                timeout=30.0,
            )
            index_data = json.loads(index_res.content[0].text)
            assert index_data.get("status") in ["indexed", "skipped"]

            job_id = index_data.get("job_id")
            if job_id:
                status_res = await asyncio.wait_for(
                    session.call_tool("check_indexing_status", {"job_id": job_id}),
                    timeout=30.0,
                )
                assert status_res.content[0].text


@pytest.mark.asyncio
async def test_stdio_smoke_memory():
    if not _stdio_smoke_enabled():
        pytest.skip(
            "Set NCE_STDIO_SMOKE=1 with NCE stack running (Postgres pgvector, Mongo, schema)."
        )
    server_params = _stdio_server_params()

    # Use a fixed namespace ID for testing (must be valid UUID)
    test_ns = "00000000-0000-4000-8000-000000000001"

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30.0)

            res = await asyncio.wait_for(
                session.call_tool(
                    "store_memory",
                    {
                        "namespace_id": test_ns,
                        "agent_id": "smoke-agent",
                        "content": "Smoke test content",
                        "summary": "Smoke test summary",
                        "heavy_payload": "Smoke test payload",
                    },
                ),
                timeout=30.0,
            )
            res_data = json.loads(res.content[0].text)
            assert isinstance(res_data, dict), f"Expected dict, got {type(res_data)}"
            assert res_data.get("status") == "ok", f"Expected status=ok, got {res_data}"
            assert "payload_ref" in res_data, f"Missing payload_ref in {res_data}"
            assert len(res_data["payload_ref"]) == 24, (
                f"Expected 24-char ObjectId, got {res_data['payload_ref']!r}"
            )

            res_ctx = await asyncio.wait_for(
                session.call_tool(
                    "get_recent_context",
                    {"namespace_id": test_ns, "agent_id": "smoke-agent", "limit": 1},
                ),
                timeout=30.0,
            )
            ctx_data = json.loads(res_ctx.content[0].text)
            assert isinstance(ctx_data, dict), f"Expected dict, got {type(ctx_data)}"
            assert "context" in ctx_data, f"Missing 'context' key in {ctx_data}"
