"""
Tri-Stack MCP Server
Wraps NCEEngine in the official MCP Python SDK (stdio transport).
Exposes MCP tools to any MCP-compatible LLM client (Claude Desktop, Cursor, etc.).
GC background task is co-launched on startup for absolute data purity.

HTTP HMAC auth and optional Redis-backed replay protection (``NonceStore``) apply
only to the Starlette **admin** stack in ``admin_server.py``. This process does not
mount ``HMACAuthMiddleware``.

MCP stdio tenant tools require ``mcp_api_key`` matching ``TRIMCP_MCP_API_KEY`` (required
in production via ``nce.config.validate``). Admin MCP tools require ``admin_api_key``.
Configure both in the MCP client ``env`` block (see ``mcp_config.json.example``).

When ``TRIMCP_DISTRIBUTED_REPLAY`` is truthy and ``REDIS_URL`` is configured, admins
should run the HTTP admin server with that env set so all instances share the same
nonce ledger.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from nce import NCEEngine
from nce.correlation import correlation_id_var
from nce.mcp_stdio_dispatch import execute_call_tool
from nce.mcp_stdio_rpc import _check_admin
from nce.mcp_stdio_tools import TOOLS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP] %(levelname)s %(message)s",
)
log = logging.getLogger("nce-mcp")

# --- Global engine instance (lifecycle managed by lifespan) ---
engine: NCEEngine | None = None
app = Server("nce-memory")

# Backward-compatible re-exports for tests and legacy imports.
__all__ = [
    "app",
    "engine",
    "call_tool",
    "list_tools",
    "_check_admin",
    "TOOLS",
    "main",
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    token = correlation_id_var.set(uuid.uuid4())
    try:
        return await execute_call_tool(engine, name, arguments)
    finally:
        correlation_id_var.reset(token)


async def main() -> None:
    from nce.mcp_stdio_main import run_stdio_server

    global engine
    engine = NCEEngine()
    try:
        await run_stdio_server(app=app, engine=engine)
    finally:
        engine = None


if __name__ == "__main__":
    asyncio.run(main())
