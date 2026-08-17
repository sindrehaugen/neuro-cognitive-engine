#!/usr/bin/env python3
"""One-off helper to split server.py MCP sections into nce/mcp_stdio_*.py modules."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
lines = SERVER.read_text(encoding="utf-8").splitlines(keepends=True)


def _slice(start: int, end: int) -> str:
    return "".join(lines[start - 1 : end])


def main() -> None:
    tools_hdr = '''"""MCP tool definitions for the stdio transport server."""

from __future__ import annotations

from mcp.types import Tool

'''
    (ROOT / "nce" / "mcp_stdio_tools.py").write_text(
        tools_hdr + _slice(122, 1264),
        encoding="utf-8",
    )

    rpc_hdr = '''"""JSON-RPC helpers and admin/quota utilities for MCP stdio."""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.types import TextContent

from nce.mcp_errors import merge_client_error_data

'''
    (ROOT / "nce" / "mcp_stdio_rpc.py").write_text(
        rpc_hdr + _slice(61, 115) + _slice(1274, 1359),
        encoding="utf-8",
    )

    disp_hdr = '''"""MCP stdio tool dispatch (handler routing and error envelopes)."""

from __future__ import annotations

import logging
from typing import Any

from mcp.types import TextContent

from nce import (
    NCEEngine,
    a2a_mcp_handlers,
    admin_mcp_handlers,
    bridge_mcp_handlers,
    code_mcp_handlers,
    contradiction_mcp_handlers,
    graph_mcp_handlers,
    memory_mcp_handlers,
    migration_mcp_handlers,
    replay_mcp_handlers,
    snapshot_mcp_handlers,
)
from nce.auth import RateLimitError, ScopeError
from nce.config import cfg
from nce.mcp_errors import (
    McpError,
    UnknownToolError,
    client_visible_detail,
    internal_error_data,
)
from nce.mcp_stdio_rpc import (
    MCP_QUOTA_EXCEEDED_PREFIX,
    _check_admin,
    _consume_quota_for_mcp_tool,
    _extract_mcp_code,
    _jsonrpc_error_response,
    _try_cached_mcp_tool_response,
)

log = logging.getLogger("nce-mcp")


async def execute_call_tool(
    engine: NCEEngine | None,
    name: str,
    arguments: dict[str, Any],
) -> list[TextContent]:
'''
    body = _slice(1364, 1815)
    indented = "".join(("    " + ln) if ln.strip() else ln for ln in body.splitlines(keepends=True))
    (ROOT / "nce" / "mcp_stdio_dispatch.py").write_text(
        disp_hdr + indented,
        encoding="utf-8",
    )
    print("Wrote nce/mcp_stdio_tools.py, mcp_stdio_rpc.py, mcp_stdio_dispatch.py")


if __name__ == "__main__":
    main()
