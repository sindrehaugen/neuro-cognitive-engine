"""Standalone ASGI entrypoint for Streamable HTTP /mcp Gateway server.

Listens on port 8005 by default (or MCP_GATEWAY_PORT) and exposes /mcp, /mcp/*,
and /healthz endpoints.
"""

from __future__ import annotations

import logging
import os

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

app = create_mcp_gateway_app(engine=engine)

__all__ = ["app", "engine"]

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("MCP_GATEWAY_PORT", "8005"))
    host = os.getenv("MCP_GATEWAY_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port)
