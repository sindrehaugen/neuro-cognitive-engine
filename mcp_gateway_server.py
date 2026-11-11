"""Standalone ASGI entrypoint for Streamable HTTP /mcp Gateway server.

Listens on port 8005 by default (or MCP_GATEWAY_PORT) and exposes /mcp, /mcp/*,
and /healthz endpoints.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from nce import NCEEngine
from nce.mcp_http_gateway import create_mcp_gateway_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP-GATEWAY] %(levelname)s %(message)s",
)
logger = logging.getLogger("mcp_gateway_server")

# Instantiate global engine for database and redis connections
try:
    engine = NCEEngine()
except Exception as exc:
    logger.warning("Initializing NCEEngine in lazy/stub mode: %s", exc)
    engine = None


@asynccontextmanager
async def _gateway_lifespan(_app: Any):
    if engine is not None and getattr(engine, "pg_pool", None) is None:
        await engine.connect()
        logger.info("MCP Gateway connected NCEEngine.")
    try:
        yield
    finally:
        if engine is not None and getattr(engine, "pg_pool", None) is not None:
            await engine.disconnect()
            logger.info("MCP Gateway disconnected NCEEngine.")


app = create_mcp_gateway_app(engine=engine, lifespan=_gateway_lifespan)

__all__ = ["app", "engine"]

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("MCP_GATEWAY_PORT", "8005"))
    host = os.getenv("MCP_GATEWAY_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)
