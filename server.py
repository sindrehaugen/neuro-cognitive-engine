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
import inspect
import logging
import os
import signal
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


def assert_local_stdio_allowed() -> None:
    """Ensure direct database stdio server is permitted in the current environment.

    In production (NCE_ENV=production or cfg.IS_PROD), direct stdio access with full
    database secrets is disallowed unless explicitly overridden via NCE_ALLOW_LOCAL_STDIO.
    Clients should connect via the zero-trust Streamable HTTP /mcp gateway instead.
    """
    from nce.config import cfg

    is_prod = bool(
        cfg.IS_PROD or os.environ.get("NCE_ENV", "").strip().lower() in ("production", "prod")
    )
    allowed = os.getenv("NCE_ALLOW_LOCAL_STDIO", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if is_prod and not allowed:
        raise RuntimeError(
            "Direct database stdio server.py is disabled in production (NCE_ALLOW_LOCAL_STDIO=false); "
            "use the zero-trust Streamable HTTP /mcp gateway."
        )


# Backward-compatible re-exports for tests and legacy imports.
__all__ = [
    "app",
    "engine",
    "call_tool",
    "list_tools",
    "_check_admin",
    "TOOLS",
    "main",
    "assert_local_stdio_allowed",
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
    assert_local_stdio_allowed()
    from nce.mcp_stdio_main import run_stdio_server

    global engine
    engine = NCEEngine()

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _signal_handler(sig_name: str) -> None:
        log.info("Received signal %s, initiating graceful MCP server shutdown...", sig_name)
        if main_task and not main_task.done():
            main_task.cancel()

    registered_signals: list[int] = []
    for sig, sig_name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig_name)
            registered_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await run_stdio_server(app=app, engine=engine)
    except asyncio.CancelledError:
        log.info("MCP server task cancelled (graceful shutdown).")
    finally:
        for sig in registered_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass

        if engine is not None:
            try:
                if hasattr(engine, "disconnect"):
                    res = engine.disconnect()
                    if inspect.isawaitable(res):
                        await res
                elif hasattr(engine, "close"):
                    res = engine.close()
                    if inspect.isawaitable(res):
                        await res
            except Exception:
                pass
        engine = None


if __name__ == "__main__":
    asyncio.run(main())
